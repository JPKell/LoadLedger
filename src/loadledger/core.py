"""The arithmetic — scope windows, incremental balances, and ceiling evaluation (spec §7, §11).

This module is where a budget is actually kept. It holds no state that outlives a call except the
balances themselves, performs no I/O, reads no clock of its own, and contains not one float.

Three properties are load-bearing, and each is a named failure mode in the development plan:

* **Windows are resolved, never guessed.** ``PER_DAY`` means a UTC calendar day — half-open from
  00:00:00Z inclusive to the next 00:00:00Z exclusive. ``PER_RUN`` covers one ``run_id``;
  ``PER_TAG`` covers every debit carrying one tag, ledger-wide.
* **Balances are maintained, not recomputed.** :meth:`BalanceBook.record` updates one small
  record per scope the debit touches. Summing the entry history on every debit would be correct
  and quadratic, and the quadratic term is invisible until a run gets long.
* **Arithmetic is integer-exact.** :class:`~baseaicore.Money` is whole nanos, token counts are
  whole numbers, and nothing here divides. There is no "percentage used" helper, because the
  obvious implementation of one is a float.

The honesty rules are equally load-bearing, and one rule covers both sides (ADR-0069): **sum what
was reported, count what was not.** A debit with no estimate adds its tokens and touches no money
balance (ADR-0016). A debit whose estimate did not total adds the components that were priced and
nothing for the rest, and is counted as untotalled. A debit whose provider left a token class
unreported contributes the classes it did report and is counted as unmetered. Every count rides on
every verdict, so a balance that is a floor never presents itself as a total — and a ceiling whose
:class:`~loadledger.types.PartialPricing` is ``STRICT`` treats an untotalled estimate in its window
as exceeding, so a hard budget is never crossed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from baseaicore import Money, TokenUsage, is_supported

from loadledger.errors import CurrencyMismatch, InvalidCeiling
from loadledger.types import (
    BudgetCeiling,
    CeilingScope,
    CeilingVerdict,
    Debit,
    LedgerEntry,
    PartialPricing,
    WindowBalance,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from baseaicore import CostEstimate

__all__ = [
    "BalanceBook",
    "DebitContribution",
    "Ledger",
    "ScopeKey",
    "contribution_of",
    "is_unpriced",
    "resolved_debit",
    "utc_day_key",
    "utc_day_start",
]

type ScopeKey = tuple[CeilingScope, str]
"""What a balance is filed under: the scope and the value that identifies its window."""


def utc_day_start(when: datetime) -> datetime:
    """Return midnight UTC beginning the calendar day that contains ``when``.

    Args:
        when: A timezone-aware instant in any timezone. It is converted to UTC first, so an
            instant expressed as ``23:30-05:00`` lands in the *following* UTC day — which is the
            whole reason this function exists rather than a ``.replace(hour=0, ...)`` at the call
            site.

    Returns:
        The instant of 00:00:00 UTC on that day.

    Raises:
        ValueError: If ``when`` is naive. A naive instant belongs to whichever day the reader's
            machine says, which would make the same ledger produce different verdicts in
            different timezones.
    """
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(
            "utc_day_start requires a timezone-aware instant; got a naive one. Which UTC day a "
            "debit falls in decides which per_day ceiling it binds."
        )
    in_utc = when.astimezone(UTC)
    return in_utc.replace(hour=0, minute=0, second=0, microsecond=0)


def utc_day_key(when: datetime) -> str:
    """Return the ``YYYY-MM-DD`` key naming the UTC calendar day that contains ``when``.

    Args:
        when: A timezone-aware instant in any timezone, converted to UTC first.

    Returns:
        The UTC date, e.g. ``"2026-09-02"``. Sortable as a string, which is what makes it usable
        as :class:`~loadledger.sql.SqlLedger`'s persisted window key without a second
        representation.

    Raises:
        ValueError: If ``when`` is naive.
    """
    return utc_day_start(when).strftime("%Y-%m-%d")


def resolved_debit(debit: Debit, occurred_at: datetime) -> Debit:
    """Return ``debit`` with its instant resolved, or itself when it already carries one.

    Every ledger stores a debit whose ``occurred_at`` is a real instant, never ``None``: the
    canonical form refuses an unresolved one, and a persisted row has to say which UTC day it
    landed in. Resolution happens once, from the ledger's injected clock, at the moment of
    recording.

    ``dataclasses.replace`` is avoided deliberately: it re-runs ``__post_init__``, which is what
    we want, but it also reconstructs a slotted frozen dataclass field by field, and doing it
    explicitly here keeps the resolved shape visible at the one place it is created.

    Args:
        debit: The debit as the caller built it.
        occurred_at: The instant to use when the debit does not carry one.

    Returns:
        The same object when ``debit.occurred_at`` is already set, so a caller's back-dated
        instant is never overwritten; otherwise a copy carrying ``occurred_at``.
    """
    if debit.occurred_at is not None:
        return debit
    return type(debit)(
        run_id=debit.run_id,
        source_ref=debit.source_ref,
        usage=debit.usage,
        cost=debit.cost,
        tags=debit.tags,
        occurred_at=occurred_at,
    )


def is_unpriced(cost: CostEstimate | None) -> bool:
    """Report whether a cost is absent or could not be totalled (spec §7's ``unpriced``).

    Args:
        cost: The estimate, or ``None`` when no pricing was applied at all.

    Returns:
        ``True`` in both of the cases that make an entry's money incomplete — no estimate, and an
        estimate whose ``total`` is :data:`~baseaicore.UNSUPPORTED`. The two are distinguished by
        the verdict's ``untotalled_debit_count``, not here.
    """
    return cost is None or not is_supported(cost.total)


@dataclass(frozen=True, slots=True)
class DebitContribution:
    """What one debit adds to every scope balance it touches.

    Computed once per debit and applied to each scope, so the honesty rules are decided in one
    place rather than re-derived per ceiling.

    Attributes:
        tokens: The sum of the token classes the provider actually reported. Classes left
            unreported are excluded, never counted as zero.
        currency: The currency the cost estimate names, whether or not it produced a total.
            ``None`` when no pricing was applied at all.
        nanos: The amount to add to the balance for :attr:`currency`: the sum of the estimate's
            priced components. Equal to the total when the estimate totalled; a floor when it did
            not; zero when nothing was priced.
        priced: ``True`` when at least one component was priced. The one flag that may create a
            currency's balance — an estimate that priced nothing (a price list that does not cover
            the instant) leaves ``money_spent`` at ``None`` rather than creating a zero.
        unpriced: ``True`` when the debit added less than its full cost: no estimate, or an
            estimate that did not total. What the verdict's ``unpriced_debit_count`` counts.
        untotalled: ``True`` when an estimate was applied and did not total. The subset of
            :attr:`unpriced` a strict ceiling fires on; a debit with no estimate is not in it.
        unmetered: ``True`` when at least one token class was unreported.
    """

    tokens: int
    currency: str | None
    nanos: int
    priced: bool
    unpriced: bool
    untotalled: bool
    unmetered: bool


_NOTHING = DebitContribution(
    tokens=0,
    currency=None,
    nanos=0,
    priced=False,
    unpriced=False,
    untotalled=False,
    unmetered=False,
)
"""The contribution of no prospective debit at all — used by ``would_exceed`` with no usage."""


def contribution_of(usage: TokenUsage, cost: CostEstimate | None) -> DebitContribution:
    """Reduce a debit's usage and cost to what it adds to a balance (ADR-0069).

    Args:
        usage: The call's disjoint token counts.
        cost: The estimate, or ``None`` when no pricing was applied.

    Returns:
        The contribution. A cost of ``None`` yields ``unpriced=True`` and ``nanos=0``: no price
        was applied, so nothing is added to any money balance and nothing already in one is
        zeroed. A cost whose ``total`` is :data:`~baseaicore.UNSUPPORTED` yields ``nanos`` equal
        to the sum of the components that *were* priced — input and output cost for a response
        whose cache classes went unreported — with ``unpriced=True`` and ``untotalled=True``, so
        the balance it lands in is a floor and the verdict says so. A cost that names a currency
        reports that currency even when it priced nothing, because a ceiling in another currency
        must refuse it either way.
    """
    tokens = 0
    unmetered = False
    for count in usage.as_counts().values():
        if is_supported(count):
            tokens += count
        else:
            unmetered = True
    if cost is None:
        return DebitContribution(
            tokens=tokens,
            currency=None,
            nanos=0,
            priced=False,
            unpriced=True,
            untotalled=False,
            unmetered=unmetered,
        )
    nanos = 0
    priced = False
    for component in (
        cost.input_cost,
        cost.output_cost,
        cost.cache_write_cost,
        cost.cache_read_cost,
    ):
        if is_supported(component):
            nanos += component.nanos
            priced = True
    totalled = is_supported(cost.total)
    return DebitContribution(
        tokens=tokens,
        currency=cost.currency,
        nanos=nanos,
        priced=priced,
        unpriced=not totalled,
        untotalled=not totalled,
        unmetered=unmetered,
    )


@dataclass(slots=True)
class _ScopeBalance:
    """The running totals for one scope window.

    Mutable and private: it is the state :class:`BalanceBook` maintains incrementally. Money is
    kept per currency and never summed across currencies — a currency absent from
    :attr:`nanos_by_currency` has had nothing priced in it, which reads differently from zero.
    """

    tokens_spent: int = 0
    nanos_by_currency: dict[str, int] = field(default_factory=dict)
    unpriced_debit_count: int = 0
    untotalled_debit_count: int = 0
    unmetered_debit_count: int = 0


_EMPTY_BALANCE = _ScopeBalance()
"""Read-only stand-in for a window nothing has landed in. Never mutated; never stored."""


class BalanceBook:
    """Incremental per-scope balances and the verdicts a set of ceilings gives over them.

    The engine both :class:`~loadledger.memory.InMemoryLedger` and
    :class:`~loadledger.sql.SqlLedger` evaluate through, so the arithmetic and the honesty rules
    have one implementation rather than one per storage backend. A durable ledger loads the
    windows its ceilings read (:meth:`window_for`), installs them with :meth:`seed`, and asks
    for :meth:`verdicts` — the rest of this class does not know where a balance came from.

    Not thread-safe on its own: :meth:`record` mutates. Implementations that need concurrency
    serialize around it — the in-memory ledger takes a lock, and a SQL ledger's transaction is
    the serialization.
    """

    __slots__ = ("_balances", "_ceilings")

    def __init__(self, ceilings: Sequence[BudgetCeiling]) -> None:
        """Build a book over a fixed set of ceilings.

        Args:
            ceilings: The ceilings to evaluate, already validated by their own constructors.
                Order is preserved and is the order verdicts come back in, so a caller can pair
                verdicts with the configuration that produced them positionally.
        """
        self._ceilings: tuple[BudgetCeiling, ...] = tuple(ceilings)
        self._balances: dict[ScopeKey, _ScopeBalance] = {}

    @property
    def ceilings(self) -> tuple[BudgetCeiling, ...]:
        """Return the configured ceilings, in configuration order."""
        return self._ceilings

    def record(self, debit: Debit, *, occurred_at: datetime) -> None:
        """Add one debit to every scope balance it touches.

        Constant work per debit in the number of scopes it touches — one dictionary lookup and a
        handful of integer additions each. Nothing re-reads the entry history, which is what
        keeps a long run's debits from getting quadratically slower.

        Args:
            debit: The debit to record. Its currency must already have been checked against the
                ceilings by :meth:`require_currency_compatible`; this method assumes that and
                accumulates.
            occurred_at: The resolved instant the debit happened at, which decides the UTC day
                its ``PER_DAY`` balance lands in.
        """
        contribution = contribution_of(debit.usage, debit.cost)
        for key in self.windows_touched(debit.run_id, occurred_at, debit.tags):
            balance = self._balances.get(key)
            if balance is None:
                balance = _ScopeBalance()
                self._balances[key] = balance
            balance.tokens_spent += contribution.tokens
            if contribution.unpriced:
                balance.unpriced_debit_count += 1
            if contribution.untotalled:
                balance.untotalled_debit_count += 1
            if contribution.unmetered:
                balance.unmetered_debit_count += 1
            if contribution.priced and contribution.currency is not None:
                balance.nanos_by_currency[contribution.currency] = (
                    balance.nanos_by_currency.get(contribution.currency, 0) + contribution.nanos
                )

    def require_currency_compatible(
        self,
        cost: CostEstimate | None,
        *,
        run_id: str,
        at: datetime,
        tags: tuple[str, ...],
    ) -> None:
        """Refuse a priced debit no money ceiling covering it could ever accumulate.

        A money ceiling binds usage in its own currency. If a EUR debit landed in a scope capped
        in USD, that cap would go on reporting a USD balance that omitted real spend — an
        under-reporting budget, which is worse than a refusal. Converting instead is not an
        option: it needs an exchange rate, which is time-varying external data (ADR-0030 rule 3).

        The check is on the currency the estimate *names*, not on whether it produced a total: an
        estimate that failed to price today is re-costable tomorrow, and the ceiling would still
        not be able to see the result.

        Args:
            cost: The estimate, or ``None``. ``None`` names no currency and is never a mismatch —
                it is unpriced, which the verdict counts instead.
            run_id: The run the debit belongs to.
            at: The instant the debit falls at, deciding which ``PER_DAY`` window covers it.
            tags: The debit's tags, deciding which ``PER_TAG`` ceilings cover it.

        Raises:
            CurrencyMismatch: If any money ceiling covering this debit is in another currency.
                ``details`` names both currencies and the ceiling.
        """
        if cost is None:
            return
        touched = self.windows_touched(run_id, at, tags)
        for ceiling in self._ceilings:
            if ceiling.money is None:
                continue
            if self.window_for(ceiling, run_id=run_id, at=at) not in touched:
                continue
            if ceiling.money.currency != cost.currency:
                raise CurrencyMismatch(
                    f"This debit is priced in {cost.currency} but the {ceiling.scope.value} "
                    f"ceiling covering it is capped in {ceiling.money.currency}. Converting needs "
                    "an exchange rate this package will not assume; record the spend against a "
                    "ledger whose ceilings are in its own currency (ADR-0030 rule 3).",
                    details={
                        "debit_currency": cost.currency,
                        "ceiling_currency": ceiling.money.currency,
                        "ceiling_scope": ceiling.scope.value,
                        "ceiling_tag": ceiling.tag,
                    },
                )

    def verdicts(
        self,
        *,
        run_id: str,
        at: datetime,
        usage: TokenUsage | None = None,
        cost: CostEstimate | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[CeilingVerdict, ...]:
        """Evaluate every configured ceiling, optionally including a debit not yet recorded.

        Read-only. Nothing here mutates a balance, which is what makes ``would_exceed`` safe to
        call from an approval path at any frequency (spec contract 6).

        Args:
            run_id: The run whose ``PER_RUN`` window to report.
            at: The instant to resolve the ``PER_DAY`` window at.
            usage: The token counts of a prospective debit. ``None`` alongside a ``cost`` means
                the counts were not stated, which is recorded as unmetered rather than as zero.
            cost: The estimate of a prospective debit, or ``None``.
            tags: The tags of the prospective debit, deciding which ``PER_TAG`` ceilings it would
                land in. Ignored when there is no prospective debit.

        Returns:
            One verdict per configured ceiling, in configuration order. With both ``usage`` and
            ``cost`` left ``None`` there is no prospective debit at all and the balances are
            reported as they stand — so asking "would nothing exceed?" is the same question as
            "what remains?", and gets the same answer. The most restrictive ceiling binds: the
            caller takes any ``exceeded`` verdict as binding, and every verdict names the cap and
            the numbers it fired on.
        """
        prospective: DebitContribution | None = None
        touched: frozenset[ScopeKey] = frozenset()
        if usage is not None or cost is not None:
            prospective = contribution_of(usage if usage is not None else TokenUsage(), cost)
            touched = self.windows_touched(run_id, at, tags)
        return tuple(
            self._verdict_for(
                ceiling,
                run_id=run_id,
                at=at,
                touched=touched,
                prospective=prospective,
            )
            for ceiling in self._ceilings
        )

    def verdicts_without_run(self, *, at: datetime) -> tuple[CeilingVerdict, ...]:
        """Evaluate every configured ceiling, for a caller that names no run.

        The ledger-wide half of :meth:`verdicts`. A ``PER_DAY`` or ``PER_TAG`` window covers every
        run, so its balance is answerable without one — which is what a dashboard asks, and what
        it previously had to ask by naming an arbitrary known run to satisfy a signature.

        Read-only, like :meth:`verdicts`: nothing here mutates a balance.

        Args:
            at: The instant to resolve every ``PER_DAY`` window at.

        Returns:
            One verdict per configured ceiling, in configuration order — the same positional
            correspondence :meth:`verdicts` gives, so a caller can still pair a verdict with the
            configuration that produced it.

        Raises:
            InvalidCeiling: If any configured ceiling is :attr:`CeilingScope.PER_RUN`. A per-run
                cap has no window without a run, and the alternatives are both worse than
                refusing: omitting it silently shortens a tuple whose positions are documented
                API, and answering it against some arbitrary run reports one run's spend under a
                heading that says "everything".
        """
        self.require_no_run_scope()
        return tuple(
            self._verdict_for(
                ceiling,
                run_id="",
                at=at,
                touched=frozenset(),
                prospective=None,
            )
            for ceiling in self._ceilings
        )

    def balance_for(self, key: ScopeKey) -> WindowBalance:
        """Return what one window has accumulated, with no ceiling read through and no run named.

        A read of state this book already maintains: :meth:`record` files every debit under
        exactly these keys, so nothing is recomputed from history here (spec §15).

        Args:
            key: The ``(scope, window_key)`` to report, as :meth:`windows_touched` or
                :meth:`window_for` spells it.

        Returns:
            The window's :class:`~loadledger.types.WindowBalance`. A window nothing has landed in
            reports zero tokens, no money and no counts — "nothing has been spent here" is a true
            answer, and the window is **not** brought into existence by asking about it.
        """
        balance = self._balances.get(key, _EMPTY_BALANCE)
        scope, window_key = key
        return WindowBalance(
            scope=scope,
            window_key=window_key,
            tokens_spent=balance.tokens_spent,
            money_spent=tuple(
                Money(currency=currency, nanos=balance.nanos_by_currency[currency])
                for currency in sorted(balance.nanos_by_currency)
            ),
            unpriced_debit_count=balance.unpriced_debit_count,
            untotalled_debit_count=balance.untotalled_debit_count,
            unmetered_debit_count=balance.unmetered_debit_count,
        )

    def _verdict_for(
        self,
        ceiling: BudgetCeiling,
        *,
        run_id: str,
        at: datetime,
        touched: frozenset[ScopeKey],
        prospective: DebitContribution | None,
    ) -> CeilingVerdict:
        """Build one ceiling's verdict from its window's balance plus any prospective debit."""
        key = self.window_for(ceiling, run_id=run_id, at=at)
        balance = self._balances.get(key, _EMPTY_BALANCE)
        applies = prospective is not None and key in touched
        delta = prospective if applies and prospective is not None else _NOTHING

        tokens_spent = balance.tokens_spent + delta.tokens
        tokens_remaining = None if ceiling.tokens is None else ceiling.tokens - tokens_spent

        money_spent: Money | None = None
        money_remaining: Money | None = None
        if ceiling.money is not None:
            currency = ceiling.money.currency
            nanos = balance.nanos_by_currency.get(currency)
            if delta.priced and delta.currency == currency:
                nanos = (nanos or 0) + delta.nanos
            if nanos is None:
                # Nothing priced in this window yet. Reporting Money.zero() here would be the
                # fabricated zero ADR-0016 exists to prevent; the whole cap does remain.
                money_remaining = ceiling.money
            else:
                money_spent = Money(currency=currency, nanos=nanos)
                money_remaining = ceiling.money - money_spent

        untotalled_debit_count = balance.untotalled_debit_count + (1 if delta.untotalled else 0)

        exceeded = ceiling.tokens is not None and tokens_spent > ceiling.tokens
        if ceiling.money is not None and money_spent is not None:
            exceeded = exceeded or money_spent > ceiling.money
        if (
            ceiling.money is not None
            and ceiling.partial_pricing is PartialPricing.STRICT
            and untotalled_debit_count > 0
        ):
            # An amount that cannot be shown to be under the cap is treated as over it
            # (ADR-0069). Not on `unpriced_debit_count`: a local debit with no estimate is
            # outside the money bound's domain and must not halt a mixed trajectory.
            exceeded = True

        return CeilingVerdict(
            ceiling=ceiling,
            exceeded=exceeded,
            money_spent=money_spent,
            money_remaining=money_remaining,
            tokens_spent=tokens_spent,
            tokens_remaining=tokens_remaining,
            unpriced_debit_count=balance.unpriced_debit_count + (1 if delta.unpriced else 0),
            untotalled_debit_count=untotalled_debit_count,
            unmetered_debit_count=balance.unmetered_debit_count + (1 if delta.unmetered else 0),
        )

    @staticmethod
    def windows_touched(run_id: str, at: datetime, tags: tuple[str, ...]) -> frozenset[ScopeKey]:
        """Return every scope window a debit with these coordinates falls into.

        Independent of the configured ceilings, deliberately: a debit is recorded into every
        window it belongs to whether or not a ceiling reads that window today, so a ceiling added
        later binds on the full history rather than on the history since it was configured. A
        durable ledger persists exactly these keys.

        Args:
            run_id: The run the debit belongs to.
            at: The resolved instant the debit happened at, deciding its UTC day.
            tags: The debit's tags, each of which is a window of its own.

        Returns:
            Every ``(scope, window_key)`` the debit accumulates into — always its run and its UTC
            day, plus one per tag.
        """
        keys: set[ScopeKey] = {
            (CeilingScope.PER_RUN, run_id),
            (CeilingScope.PER_DAY, utc_day_key(at)),
        }
        keys.update((CeilingScope.PER_TAG, tag) for tag in tags)
        return frozenset(keys)

    @staticmethod
    def window_for(ceiling: BudgetCeiling, *, run_id: str, at: datetime) -> ScopeKey:
        """Return the window key one ceiling reads, for this run at this instant.

        The inverse of :meth:`windows_touched`: that method says where a debit is *written*, this
        one says where a ceiling *reads*. A durable ledger loads exactly these keys before
        evaluating, and creates none of them — a ceiling reading an empty window must not bring
        the window into existence (spec contract 6).

        Args:
            ceiling: The ceiling to locate.
            run_id: The run being reported on, used only by a ``PER_RUN`` ceiling.
            at: The instant to resolve a ``PER_DAY`` ceiling's window at.

        Returns:
            The single ``(scope, window_key)`` this ceiling's balance lives under.
        """
        if ceiling.scope is CeilingScope.PER_RUN:
            return (CeilingScope.PER_RUN, run_id)
        if ceiling.scope is CeilingScope.PER_DAY:
            return (CeilingScope.PER_DAY, utc_day_key(at))
        # PER_TAG: the tag is non-None by BudgetCeiling's own validation.
        return (CeilingScope.PER_TAG, ceiling.tag or "")

    def require_no_run_scope(self) -> None:
        """Refuse a run-free evaluation over a ceiling that only a run can locate.

        Raises:
            InvalidCeiling: If any configured ceiling is :attr:`CeilingScope.PER_RUN`.
                ``details`` names the offending scope and how many of them there are. A caller
                that holds one ledger for every ceiling it knows about builds a second over the
                ledger-wide subset, which is cheap: this class caches nothing between calls.
        """
        offenders = sum(1 for ceiling in self._ceilings if ceiling.scope is CeilingScope.PER_RUN)
        if offenders:
            raise InvalidCeiling(
                f"{offenders} configured ceiling(s) are {CeilingScope.PER_RUN.value} and cannot "
                "be evaluated without a run. Ask about a per-run ceiling through remaining(run_id)"
                ", and build a ledger over the ledger-wide ceilings for a position that names no "
                "run.",
                details={"scope": CeilingScope.PER_RUN.value, "ceiling_count": offenders},
            )

    def windows_without_run(self, at: datetime) -> frozenset[ScopeKey]:
        """Return the windows the configured ceilings read when no run is named.

        :meth:`window_for` over every ceiling, with no ``run_id`` to give it — sound only once
        :meth:`require_no_run_scope` has passed, because that is what guarantees no ceiling here
        needs one. A durable ledger loads exactly these keys and creates none of them.

        Args:
            at: The instant to resolve every ``PER_DAY`` window at.
        """
        return frozenset(self.window_for(ceiling, run_id="", at=at) for ceiling in self._ceilings)

    def seed(
        self,
        key: ScopeKey,
        *,
        tokens_spent: int,
        nanos_by_currency: Mapping[str, int],
        unpriced_debit_count: int,
        untotalled_debit_count: int,
        unmetered_debit_count: int,
    ) -> None:
        """Install one window's already-accumulated balance, as read out of durable storage.

        The seam a persistent ledger evaluates through. :class:`~loadledger.sql.SqlLedger` keeps
        the balances in its host's database rather than in this book, loads the windows its
        ceilings read, seeds them here, and calls :meth:`verdicts` — so the arithmetic and the
        honesty rules have exactly one implementation for both storage backends.

        Nothing is validated beyond the types: the caller is handing back numbers this package's
        own :meth:`record` produced. Seeding the same key twice **replaces** rather than adds, so
        a book seeded from a query is a snapshot of the store and never double-counts.

        Args:
            key: The ``(scope, window_key)`` this balance belongs to, as
                :meth:`windows_touched` or :meth:`window_for` spells it.
            tokens_spent: Tokens accumulated in this window, over the classes providers reported.
            nanos_by_currency: Nanos accumulated per currency. A currency absent from the mapping
                has had nothing priced in it, which reads differently from zero (ADR-0016) —
                so a store must omit the key rather than write a zero.
            unpriced_debit_count: Debits here that added less than their full cost.
            untotalled_debit_count: The subset of those that carried an estimate which did not
                total.
            unmetered_debit_count: Debits here that left at least one token class unreported.
        """
        self._balances[key] = _ScopeBalance(
            tokens_spent=tokens_spent,
            nanos_by_currency=dict(nanos_by_currency),
            unpriced_debit_count=unpriced_debit_count,
            untotalled_debit_count=untotalled_debit_count,
            unmetered_debit_count=unmetered_debit_count,
        )


@runtime_checkable
class Ledger(Protocol):
    """What every LoadLedger implementation offers (spec §7).

    :class:`~loadledger.memory.InMemoryLedger` and :class:`~loadledger.sql.SqlLedger` both
    implement it, over the same :class:`BalanceBook`. A caller written against this protocol never
    learns which one it holds, which is the point: the in-memory ledger is the deterministic
    double every later phase tests against, not a stub with a reduced surface.
    """

    def debit(self, debit: Debit) -> LedgerEntry:
        """Record one debit and return it with every ceiling's verdict as of that debit.

        Exceeding a ceiling is not an error: the entry records ``exceeded=True`` verdicts and the
        debit stands. Refusing work is the caller's policy.

        Raises:
            CurrencyMismatch: If the debit is priced in a currency a money ceiling covering it
                caps in another currency. Refused, never converted.
        """
        ...

    def would_exceed(
        self,
        run_id: str,
        *,
        usage: TokenUsage | None = None,
        cost: CostEstimate | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[CeilingVerdict, ...]:
        """Report what every ceiling would say if this spend were recorded now.

        Side-effect-free, and therefore safe to call from an approval path at any frequency.

        Raises:
            UnknownRun: If the ledger has never seen ``run_id``.
            CurrencyMismatch: If the prospective cost is in a currency a money ceiling covering
                it caps in another currency.
        """
        ...

    def remaining(self, run_id: str) -> tuple[CeilingVerdict, ...]:
        """Report every ceiling's current balance for this run.

        Raises:
            UnknownRun: If the ledger has never seen ``run_id``.
        """
        ...

    def balances(self, *, scope: CeilingScope, window_key: str) -> WindowBalance:
        """Report what one window has accumulated, naming no run and reading through no ceiling.

        The read a *view* needs. ``remaining`` and ``would_exceed`` answer "may this run spend"
        and "what has this run spent", both through a configured cap; neither can answer "what
        has been spent in this window", which is what a per-tier or per-day dashboard asks. The
        two ways to answer it from outside — summing ``entries`` in the consumer, or configuring
        a ceiling nobody intends to enforce purely to read a number through — are ledger
        arithmetic in an application and a fabricated cap in the record respectively.

        Side-effect-free: a window with no balance is reported as empty, never created.

        Raises:
            ValueError: If ``window_key`` is blank. A blank key names a window nothing can land
                in, so answering "nothing spent" would look exactly like answering it for a real
                window and would hide the caller's bug.
        """
        ...

    def position(self) -> tuple[CeilingVerdict, ...]:
        """Report every configured ceiling's current balance, for no particular run.

        The ledger-wide counterpart of :meth:`remaining`. ``PER_DAY`` ceilings are reported for
        the UTC day the injected clock is in.

        Raises:
            InvalidCeiling: If any configured ceiling is ``PER_RUN``. Such a cap has no window
                without a run; ask about it through :meth:`remaining` instead.
        """
        ...

    def entries(
        self,
        *,
        run_id: str | None = None,
        tag: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[LedgerEntry]:
        """Return recorded entries, oldest first, narrowed by whichever filters are given.

        ``since`` is inclusive, making the window half-open.

        Raises:
            ValueError: If ``since`` is naive.
        """
        ...

    def declare_run(self, run_id: str) -> None:
        """Register a run before anything has been debited against it.

        Spec §13 defines a run as existing "once debited or declared"; this is the declaring
        half, so a caller can ask what a fresh budget allows before spending against it.

        Raises:
            ValueError: If ``run_id`` is blank.
        """
        ...
