"""The value objects a budget is made of — ceilings, debits, verdicts and entries (spec §7).

Pure data. Nothing here performs I/O, reads a clock, or holds a price: LoadLedger adds
**accumulation and ceilings** to ADR-0030's cost model and nothing else, and these five types are
the whole of that addition's vocabulary.

Two rules run through every type in this module:

* **Integer arithmetic only.** :class:`~baseaicore.Money` is a whole number of nanos and token
  counts are whole numbers. No field here is a float, no method divides, and there is no
  "percentage used" convenience — a float in a budget is how a total stops equalling the sum of
  its own rows.
* **Absent is not zero** (ADR-0016). A count a provider never reported is
  :data:`~baseaicore.UNSUPPORTED`, and a scope in which nothing has been priced reports
  ``money_spent=None`` rather than a zero that would read as "this was free".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import Money, TokenUsage, is_supported, to_rfc3339

from loadledger.errors import InvalidCeiling

if TYPE_CHECKING:
    from datetime import datetime

    from baseaicore import CostEstimate

__all__ = [
    "BudgetCeiling",
    "CeilingScope",
    "CeilingVerdict",
    "Debit",
    "LedgerEntry",
    "PartialPricing",
    "WindowBalance",
]


class CeilingScope(StrEnum):
    """What a ceiling is a ceiling *over* — the window its balance accumulates in.

    A :class:`~enum.StrEnum` rather than a bare :class:`~enum.Enum`, matching every other
    enumeration in the suite (``ProviderKind``, ``PricingSource``, ``MetricKind``): the member's
    value is its serialized form, so a scope crossing a payload boundary spells itself the same
    way it does in code.
    """

    PER_RUN = "per_run"
    """One trajectory, project or unit — whatever the caller's ``run_id`` names. The balance
    covers every debit carrying that ``run_id``, for as long as the ledger holds it."""

    PER_DAY = "per_day"
    """One **UTC calendar day**, ledger-wide: every debit whose ``occurred_at`` falls between
    00:00:00Z inclusive and the next 00:00:00Z exclusive, from every run.

    UTC, not the machine's local midnight. A budget that reset at local midnight would be a
    different budget on every machine that read it, and the same ledger replayed in another
    timezone would produce different verdicts — which is the one thing a spend record may not do.
    """

    PER_TAG = "per_tag"
    """One tag, ledger-wide: every debit whose ``tags`` contain the ceiling's ``tag``, from every
    run and every day. PromptCadence uses it for the tier name (``"tier:local_fast"``)."""


class PartialPricing(StrEnum):
    """How a money ceiling treats an estimate in its window that did not total (ADR-0069).

    A provider that leaves a token class unreported — both real ModelRack adapters leave the cache
    classes unreported — produces a :class:`~baseaicore.CostEstimate` whose priced components are
    real money and whose ``total`` is :data:`~baseaicore.UNSUPPORTED`. The ledger accumulates the
    priced components as a **floor** either way; this enum decides what ``exceeded`` means while
    the sum is a floor. It is the operator's choice, carried on the ceiling so that
    ``would_exceed`` applies it at pre-flight and every approval record shows which rule the
    verdict was judged under.
    """

    FLOOR = "floor"
    """Bind on what was priced. ``exceeded`` is certain when ``True`` (the floor is already over
    the cap) and not certain when ``False``; the brake may fire late, by at most the unreported
    portion, and never early. The default."""

    STRICT = "strict"
    """An estimate in the window that did not total counts as exceeding the cap: an amount that
    cannot be shown to be under the limit is treated as over it. The brake fires early and the cap
    is never crossed. Trips on ``untotalled_debit_count``, never on a debit that carried no
    estimate at all, so a local step on a mixed trajectory does not halt it (ADR-0047 §3 governs
    local execution with the token ceiling). Requires a money bound."""


@dataclass(frozen=True, slots=True)
class BudgetCeiling:
    """A cap — money, tokens, or both — over one :class:`CeilingScope`.

    Both bounds may be set, and then both bind: the ceiling is exceeded when *either* is. Several
    ceilings may be active on one ledger at once, and the most restrictive binds, which needs no
    arithmetic of its own — every ceiling is evaluated and any ``exceeded`` verdict binds.

    A bound of zero is a legitimate ceiling ("spend nothing priced here"), which is why every
    check in this class is written against ``None`` and never against truthiness.

    Attributes:
        scope: The window the balance accumulates in.
        money: The monetary cap, in one currency. Binds only usage priced in **that** currency;
            a priced debit in another currency is refused rather than converted (ADR-0030 rule 3).
            ``None`` means this ceiling does not bind money at all.
        tokens: The token cap. Binds all usage, priced or not — it is the universal brake, and on
            a local tier it is the only ceiling that can bind anything (ADR-0047 §3). ``None``
            means this ceiling does not bind tokens.
        tag: The tag this ceiling is over. Required for :attr:`CeilingScope.PER_TAG` and refused
            for every other scope, because a tag on a per-run ceiling would silently do nothing.
        partial_pricing: What ``exceeded`` means while the money balance is a floor
            (:class:`PartialPricing`, ADR-0069). Keyword-only. :attr:`PartialPricing.FLOOR` binds
            on what was priced and may fire late; :attr:`PartialPricing.STRICT` treats an estimate
            that did not total as exceeding, and is refused on a ceiling with no money bound.

    Raises:
        InvalidCeiling: If neither bound is set; if ``money`` is not
            :class:`~baseaicore.Money` or is negative; if ``tokens`` is not a whole number or is
            negative; if a ``PER_TAG`` ceiling has no tag or a blank one; if any other scope was
            given a tag; if ``partial_pricing`` is not a :class:`PartialPricing`; or if it is
            ``STRICT`` on a ceiling that binds no money.
    """

    scope: CeilingScope
    money: Money | None = None
    tokens: int | None = None
    tag: str | None = None
    partial_pricing: PartialPricing = field(default=PartialPricing.FLOOR, kw_only=True)

    def __post_init__(self) -> None:
        """Validate the bounds and the tag rules.

        Raises:
            InvalidCeiling: As documented on the class. Every failure here is a configuration
                mistake that would otherwise surface as a budget which never fires.
        """
        if self.money is None and self.tokens is None:
            raise InvalidCeiling(
                f"A {self.scope.value} ceiling must bind money, tokens, or both; this one binds "
                "neither and could never be exceeded.",
                details={"scope": self.scope.value},
            )
        if self.money is not None:
            if not isinstance(self.money, Money):
                raise InvalidCeiling(
                    f"BudgetCeiling.money must be Money or None; got "
                    f"{type(self.money).__name__!r}. Build one with "
                    "Money.from_decimal('USD', '5.00').",
                    details={"field": "money", "value": repr(self.money)},
                )
            if self.money.nanos < 0:
                raise InvalidCeiling(
                    f"BudgetCeiling.money must not be negative; got {self.money}. A negative cap "
                    "is exceeded before anything is spent.",
                    details={"field": "money", "value": str(self.money)},
                )
        if self.tokens is not None:
            if isinstance(self.tokens, bool) or not isinstance(self.tokens, int):
                raise InvalidCeiling(
                    f"BudgetCeiling.tokens must be a whole number of tokens or None; got "
                    f"{self.tokens!r}.",
                    details={"field": "tokens", "value": repr(self.tokens)},
                )
            if self.tokens < 0:
                raise InvalidCeiling(
                    f"BudgetCeiling.tokens must not be negative; got {self.tokens}.",
                    details={"field": "tokens", "value": self.tokens},
                )
        if self.scope is CeilingScope.PER_TAG:
            if self.tag is None or not self.tag.strip():
                raise InvalidCeiling(
                    "A per_tag ceiling must name the tag it is over; got "
                    f"{self.tag!r}. Without one it would match every debit or none.",
                    details={"field": "tag", "scope": self.scope.value},
                )
        elif self.tag is not None:
            raise InvalidCeiling(
                f"A {self.scope.value} ceiling must not carry a tag; got {self.tag!r}. Only "
                "per_tag ceilings are filtered by tag, so a tag here would silently do nothing.",
                details={"field": "tag", "scope": self.scope.value, "value": self.tag},
            )
        if not isinstance(self.partial_pricing, PartialPricing):
            raise InvalidCeiling(
                f"BudgetCeiling.partial_pricing must be a PartialPricing; got "
                f"{self.partial_pricing!r}.",
                details={"field": "partial_pricing", "value": repr(self.partial_pricing)},
            )
        if self.partial_pricing is PartialPricing.STRICT and self.money is None:
            raise InvalidCeiling(
                f"A strict {self.scope.value} ceiling must bind money; this one binds no money. "
                "Strictness says what an estimate that did not total means for the money cap, "
                "and this ceiling has no money cap for it to mean anything to.",
                details={"field": "partial_pricing", "scope": self.scope.value},
            )

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside canonical JSON, and therefore inside goldens.

        Returns:
            ``{"scope": ..., "money": {"currency", "nanos"} | None, "tokens": ..., "tag": ...,
            "partial_pricing": ...}``. Equal ceilings always produce the same mapping, which is
            what spec contract 4's byte-identical verdict serialization rests on; the partial
            pricing rule is in it so an approval record shows which rule its verdict was judged
            under.
        """
        return {
            "scope": self.scope.value,
            "money": self.money.as_canonical() if self.money is not None else None,
            "tokens": self.tokens,
            "tag": self.tag,
            "partial_pricing": self.partial_pricing.value,
        }


@dataclass(frozen=True, slots=True)
class Debit:
    """One unit of spend to record: what was used, what it was estimated to cost, and where from.

    Attributes:
        run_id: The caller's run identity — a trajectory, a project, a unit. Opaque to this
            package, which never learns what one is.
        source_ref: The turn, tool invocation or stage attempt this spend came from. Opaque, and
            never prompt or response content (spec §14).
        usage: The call's disjoint token counts. Classes the provider did not report stay
            :data:`~baseaicore.UNSUPPORTED` and are excluded from the token balance rather than
            counted as zero; the verdict says how many debits that happened to.
        cost: The estimate produced by :func:`baseaicore.estimate_cost`, or ``None`` when no
            pricing was applied at all — the local-model case. Either way the money figure is
            **not** the stored fact: :attr:`LedgerEntry.pricing_hash` and ``usage`` are, and the
            money is re-derivable from them (ADR-0030 rule 1).
        tags: Free-form labels this debit carries, e.g. ``("tier:local_fast",)``. A ``PER_TAG``
            ceiling binds a debit only if its tag appears here.
        occurred_at: When the spend happened. ``None`` means "now", resolved once from the
            ledger's injected clock when the debit is recorded — the entry a ledger stores always
            carries a resolved, timezone-aware instant.

    Raises:
        ValueError: If ``run_id`` or ``source_ref`` is blank, if ``tags`` is not a tuple of
            non-blank strings, or if ``occurred_at`` is naive. A naive instant would place the
            debit in a different UTC day depending on who read it.
    """

    run_id: str
    source_ref: str
    usage: TokenUsage
    cost: CostEstimate | None
    tags: tuple[str, ...] = ()
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the identifiers, the tags and the instant.

        Raises:
            ValueError: As documented on the class.
        """
        for field_name in ("run_id", "source_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Debit.{field_name} must be a non-blank opaque identifier; got {value!r}."
                )
        if not isinstance(self.usage, TokenUsage):
            raise ValueError(
                f"Debit.usage must be a TokenUsage; got {type(self.usage).__name__!r}."
            )
        if not isinstance(self.tags, tuple) or any(
            not isinstance(tag, str) or not tag.strip() for tag in self.tags
        ):
            raise ValueError(
                f"Debit.tags must be a tuple of non-blank strings; got {self.tags!r}. A list "
                "would make this frozen dataclass unhashable, and a blank tag matches nothing."
            )
        if self.occurred_at is not None:
            tzinfo = self.occurred_at.tzinfo
            if tzinfo is None or tzinfo.utcoffset(self.occurred_at) is None:
                raise ValueError(
                    "Debit.occurred_at must be timezone-aware; got a naive datetime. Which UTC "
                    "day a debit lands in decides which per_day ceiling it binds, so a naive "
                    "instant has no defensible reading."
                )

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside canonical JSON.

        ``cost`` is deliberately absent. An entry's primary facts are its token counts and the
        ``pricing_hash`` that was applied (ADR-0030 rule 1); serializing a money figure here
        would put a derived number in the record of truth, and a later price correction would
        have nowhere to go.

        Returns:
            The run and source references, the four token counts (``"unsupported"`` where a
            class was not reported), the tags, and the instant as RFC 3339.

        Raises:
            ValueError: If ``occurred_at`` has not been resolved. Only a debit a ledger has
                recorded is canonical; an unresolved ``None`` would serialize as a different
                document every time the clock moved.
        """
        if self.occurred_at is None:
            raise ValueError(
                "Debit.as_canonical requires a resolved occurred_at. Ledgers resolve it from "
                "their injected clock when recording; a debit that has not been recorded has no "
                "canonical form."
            )
        return {
            "run_id": self.run_id,
            "source_ref": self.source_ref,
            "usage": {
                name: (count if is_supported(count) else "unsupported")
                for name, count in self.usage.as_counts().items()
            },
            "tags": list(self.tags),
            "occurred_at": to_rfc3339(self.occurred_at),
        }


@dataclass(frozen=True, slots=True)
class CeilingVerdict:
    """What one ceiling says about its scope, with the numbers it said it from.

    Every verdict is explicable: it names the ceiling, the balance in that ceiling's window, what
    is left, and how much of the window's spend could not be counted. Verdicts appear in approval
    records, so their serialized form is byte-stable (spec contract 4).

    Attributes:
        ceiling: The ceiling this verdict is about, including its :class:`PartialPricing` rule.
        exceeded: ``True`` when the balance is **strictly greater** than a bound this ceiling
            sets. Spending exactly the cap is not exceeding it. Under
            :attr:`PartialPricing.STRICT`, also ``True`` whenever :attr:`untotalled_debit_count`
            is non-zero: an amount that cannot be shown to be under the cap is treated as over it
            (ADR-0069). With several ceilings active the caller takes the most restrictive answer
            by taking any ``exceeded`` as binding — there is no arithmetic to get wrong, and each
            verdict says which cap fired. While :attr:`money_spent` is a floor, ``True`` is
            certain and ``False`` is not.
        money_spent: What has been spent, in the ceiling's currency, in this window: the sum of
            every priced component of every debit landing here. ``None`` when the ceiling binds
            no money, and ``None`` when nothing has been priced in this scope yet — which is not
            the same as zero, and is the reason this field is not ``Money.zero()`` (ADR-0016). A
            **floor** whenever :attr:`unpriced_debit_count` is non-zero; render it as "at least",
            never as a bare figure.
        money_remaining: ``ceiling.money`` less :attr:`money_spent`, or the whole cap when nothing
            has been priced yet. ``None`` when the ceiling binds no money. May be negative: a
            crossed budget reports how far past it went rather than clamping at zero. An upper
            bound on what is left whenever :attr:`money_spent` is a floor.
        tokens_spent: Tokens counted in this window, summed over the classes providers actually
            reported.
        tokens_remaining: ``ceiling.tokens`` less :attr:`tokens_spent`, or ``None`` when the
            ceiling binds no tokens. May be negative.
        unpriced_debit_count: How many debits in this window added less than their full cost: no
            estimate at all, or an estimate that did not total. Their tokens are in
            :attr:`tokens_spent`; only the components that were priced are in
            :attr:`money_spent`. A non-zero count means :attr:`money_spent` is a floor, not a
            total — so "under budget" is never claimed over an incomplete sum without saying so
            (spec contract 2). Deciding what to do about it is the application's policy, not this
            package's: see :mod:`loadledger.errors`.
        untotalled_debit_count: The subset of :attr:`unpriced_debit_count` that carried an
            estimate which did not total — a priced response the provider did not fully report,
            or a price list that does not cover the instant. This is what a
            :attr:`PartialPricing.STRICT` ceiling fires on; a debit with no estimate (a local
            model) is in the unpriced count and not here.
        unmetered_debit_count: How many debits in this window left at least one token class
            unreported. Those classes are excluded from :attr:`tokens_spent` rather than counted
            as zero, so a non-zero count means the token balance is a floor too.
    """

    ceiling: BudgetCeiling
    exceeded: bool
    money_spent: Money | None
    money_remaining: Money | None
    tokens_spent: int
    tokens_remaining: int | None
    unpriced_debit_count: int = 0
    untotalled_debit_count: int = 0
    unmetered_debit_count: int = 0

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside canonical JSON, and therefore inside goldens.

        Returns:
            Every field, with :class:`~baseaicore.Money` in its own canonical
            ``{"currency", "nanos"}`` form. Equal verdicts serialize byte-identically on every
            platform and Python version (spec contract 4).
        """
        return {
            "ceiling": self.ceiling.as_canonical(),
            "exceeded": self.exceeded,
            "money_spent": (
                self.money_spent.as_canonical() if self.money_spent is not None else None
            ),
            "money_remaining": (
                self.money_remaining.as_canonical() if self.money_remaining is not None else None
            ),
            "tokens_spent": self.tokens_spent,
            "tokens_remaining": self.tokens_remaining,
            "unpriced_debit_count": self.unpriced_debit_count,
            "untotalled_debit_count": self.untotalled_debit_count,
            "unmetered_debit_count": self.unmetered_debit_count,
        }


@dataclass(frozen=True, slots=True)
class WindowBalance:
    """What one scope window has accumulated, read without naming a run or a ceiling.

    The shape :class:`CeilingVerdict` cannot take. A verdict is about a **bound** — it exists to
    say whether one was crossed and how much of it is left — so reporting a window that has no
    ceiling through one would mean a verdict with no ceiling on it and two permanently meaningless
    fields, which invites a caller to ask whether something was exceeded when nothing here has a
    bound to exceed. This type answers the other question: what has landed in this window, and how
    complete that figure is.

    The honesty counts ride on it for the same reason they ride on every verdict (spec contract
    2): a balance handed over without them is a floor presenting itself as a total, and a view
    that cannot say "at least" is the failure ADR-0069 exists to prevent.

    Attributes:
        scope: Which kind of window this is.
        window_key: The window's key within that scope — a ``run_id`` for
            :attr:`CeilingScope.PER_RUN`, a ``YYYY-MM-DD`` UTC day key for
            :attr:`CeilingScope.PER_DAY` (:func:`~loadledger.core.utc_day_key` spells it), a tag
            for :attr:`CeilingScope.PER_TAG`.
        tokens_spent: Tokens counted in this window, summed over the classes providers actually
            reported. ``0`` for a window nothing has landed in, which is the true answer and not a
            fabrication: no debit means no tokens, whereas an *unreported* class is excluded
            rather than counted as zero and shows up in :attr:`unmetered_debit_count` instead.
        money_spent: One :class:`~baseaicore.Money` per currency anything in this window was
            priced in, ascending by currency code and at most one per currency. **Never summed
            across currencies** (ADR-0030 rule 3): converting needs an exchange rate this package
            will not assume, so a window priced in two currencies reports two figures and no
            total. A currency that is absent has had nothing priced in it, which reads differently
            from zero (ADR-0016), and an empty tuple means nothing at all has been priced here —
            which is why it is a tuple of what exists rather than a row per currency the caller
            asked about. Every figure is a **floor** whenever :attr:`unpriced_debit_count` is
            non-zero; render it as "at least", never as a bare figure.
        unpriced_debit_count: How many debits in this window added less than their full cost: no
            estimate at all, or an estimate that did not total. Their tokens are in
            :attr:`tokens_spent`; only the components that were priced are in
            :attr:`money_spent`.
        untotalled_debit_count: The subset of :attr:`unpriced_debit_count` that carried an
            estimate which did not total. This is what a :attr:`PartialPricing.STRICT` ceiling
            fires on — but nothing here fires, because nothing here has a cap.
        unmetered_debit_count: How many debits in this window left at least one token class
            unreported, making :attr:`tokens_spent` a floor too.
    """

    scope: CeilingScope
    window_key: str
    tokens_spent: int = 0
    money_spent: tuple[Money, ...] = ()
    unpriced_debit_count: int = 0
    untotalled_debit_count: int = 0
    unmetered_debit_count: int = 0

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside canonical JSON, and therefore inside goldens.

        Returns:
            Every field, with each :class:`~baseaicore.Money` in its own canonical
            ``{"currency", "nanos"}`` form and the money list in the order it is held, which is
            ascending by currency code — so equal balances serialize byte-identically on every
            platform and Python version (spec contract 4).
        """
        return {
            "scope": self.scope.value,
            "window_key": self.window_key,
            "tokens_spent": self.tokens_spent,
            "money_spent": [money.as_canonical() for money in self.money_spent],
            "unpriced_debit_count": self.unpriced_debit_count,
            "untotalled_debit_count": self.untotalled_debit_count,
            "unmetered_debit_count": self.unmetered_debit_count,
        }


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One recorded debit and the verdicts every active ceiling gave after it.

    The entry and its verdicts are one fact: a ledger records both together or neither, so a
    crash can never leave spend on the books with no verdict beside it (spec contract 5 —
    ADR-0044's shape applied to money).

    Attributes:
        entry_id: A ULID, ordered within the ledger that made it.
        debit: The debit as recorded, with :attr:`Debit.occurred_at` resolved.
        unpriced: ``True`` when the debit carried no cost, or a cost whose total could not be
            computed. Its tokens still accumulate; its money does not, and no balance was zeroed
            to make it fit.
        pricing_hash: The price record the cost was derived from, carried even when the estimate
            came out unpriced — knowing *which* price list failed to price a call is how the gap
            gets closed. ``None`` only when no pricing was applied at all.
        verdicts: One verdict per configured ceiling, in configuration order, as of this debit.
    """

    entry_id: str
    debit: Debit
    unpriced: bool
    pricing_hash: str | None
    verdicts: tuple[CeilingVerdict, ...]

    def as_canonical(self) -> dict[str, Any]:
        """Return the mapping form used inside canonical JSON.

        Returns:
            The entry id, the debit (token counts and references, never a money figure), the
            unpriced flag, the pricing hash and every verdict. This is exactly the body of the
            suite's ``budget.debited`` event (spec §17), and exactly the shape a re-costing pass
            reads: usage plus a hash, from which the money is derived again.
        """
        return {
            "entry_id": self.entry_id,
            "debit": self.debit.as_canonical(),
            "unpriced": self.unpriced,
            "pricing_hash": self.pricing_hash,
            "verdicts": [verdict.as_canonical() for verdict in self.verdicts],
        }
