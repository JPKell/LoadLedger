"""``InMemoryLedger`` — the process-local ledger, and the deterministic double (spec §7, §10).

First-class, not a stub. It implements the whole :class:`~loadledger.core.Ledger` protocol with
the same :class:`~loadledger.core.BalanceBook` :class:`~loadledger.sql.SqlLedger` uses, so a
consumer that tests against this one is testing the arithmetic it will run in production. What it
does not do is survive the process — it owns no storage, and it says so here rather than letting
a caller discover it after a restart (spec §10: LoadLedger owns no data). For a ledger that does
survive, mount the tables into an application's own database and use
:class:`~loadledger.sql.SqlLedger`; it is observably this ledger with a different store.

Determinism is the reason it exists. Given the same clock, the same ceilings and the same debits,
it produces the same verdicts in the same order, byte-identical when serialized (spec contract 4).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from baseaicore import UlidGenerator

from loadledger.core import BalanceBook, is_unpriced, resolved_debit
from loadledger.errors import UnknownRun
from loadledger.types import LedgerEntry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from baseaicore import Clock, CostEstimate, TokenUsage

    from loadledger.types import BudgetCeiling, CeilingScope, CeilingVerdict, Debit, WindowBalance

__all__ = ["InMemoryLedger"]


class InMemoryLedger:
    """A ledger held in this process's memory, for the life of this process.

    Thread-safe: every public method takes one lock, so a debit and the verdicts it reports are
    computed against one consistent state and commit together (spec contract 5, the in-memory
    reading of it).

    Attributes are private; the ledger's surface is the four protocol methods plus
    :meth:`declare_run`.
    """

    __slots__ = ("_book", "_clock", "_entries", "_ids", "_lock", "_runs")

    def __init__(self, ceilings: Sequence[BudgetCeiling], *, clock: Clock) -> None:
        """Build a ledger over a fixed set of ceilings.

        Args:
            ceilings: The ceilings to evaluate on every debit and every query, already validated
                by their own constructors. Order is preserved and is the order verdicts come back
                in. An empty sequence is legitimate: a ledger with no ceilings still accumulates
                and still answers :meth:`entries`, it simply never refuses.
            clock: Returns the current timezone-aware instant. Injected and required — a ledger
                that read the system clock directly could not be tested across a UTC midnight,
                which is the one boundary this package must get right.
        """
        self._book = BalanceBook(ceilings)
        self._clock: Clock = clock
        self._entries: list[LedgerEntry] = []
        self._ids = UlidGenerator(clock=clock)
        self._lock = threading.Lock()
        self._runs: set[str] = set()

    def declare_run(self, run_id: str) -> None:
        """Register a run before anything has been debited against it.

        A run exists once it has been debited *or* declared (spec §13). Declaring one lets a
        caller ask what a fresh budget allows before spending anything, without
        :meth:`remaining` having to invent the difference between "this run has spent nothing"
        and "this run id is a typo".

        Args:
            run_id: The run identity to register. Declaring an existing run does nothing.

        Raises:
            ValueError: If ``run_id`` is blank.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"run_id must be a non-blank identifier; got {run_id!r}.")
        with self._lock:
            self._runs.add(run_id)

    def debit(self, debit: Debit) -> LedgerEntry:
        """Record one debit and return it with every ceiling's verdict as of that debit.

        The entry and its verdicts are produced together under one lock, so no caller ever sees
        spend recorded without the verdicts that describe it (spec contract 5).

        ``PER_DAY`` verdicts are resolved against the debit's own ``occurred_at``, not against
        "now": a debit back-dated into yesterday affects yesterday's window, and the verdict it
        gets back describes the window it actually landed in.

        **Exceeding a ceiling is not an error.** The entry records ``exceeded=True`` verdicts and
        the debit stands; refusing work is the caller's policy, and :meth:`would_exceed` exists so
        it can refuse *before* spending (spec §13).

        Args:
            debit: The debit to record. ``occurred_at`` is resolved from the injected clock when
                it is ``None``, and the entry stores the resolved value.

        Returns:
            The stored :class:`~loadledger.types.LedgerEntry`, carrying the resolved debit, the
            ``unpriced`` flag, the ``pricing_hash`` and one verdict per configured ceiling.

        Raises:
            CurrencyMismatch: If the debit is priced in a currency a money ceiling covering it
                caps in another currency. Refused, never converted (ADR-0030 rule 3).
        """
        with self._lock:
            occurred_at = debit.occurred_at if debit.occurred_at is not None else self._clock()
            resolved = resolved_debit(debit, occurred_at)
            self._book.require_currency_compatible(
                resolved.cost, run_id=resolved.run_id, at=occurred_at, tags=resolved.tags
            )
            self._book.record(resolved, occurred_at=occurred_at)
            self._runs.add(resolved.run_id)
            entry = LedgerEntry(
                entry_id=self._ids.new_id(),
                debit=resolved,
                unpriced=is_unpriced(resolved.cost),
                pricing_hash=resolved.cost.pricing_hash if resolved.cost is not None else None,
                verdicts=self._book.verdicts(run_id=resolved.run_id, at=occurred_at),
            )
            self._entries.append(entry)
            return entry

    def would_exceed(
        self,
        run_id: str,
        *,
        usage: TokenUsage | None = None,
        cost: CostEstimate | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[CeilingVerdict, ...]:
        """Report what every ceiling would say if this spend were recorded now.

        Side-effect-free (spec contract 6): no balance moves, no entry is written, no id is
        drawn, and the ledger's state hashes identically before and after. Safe to call from an
        approval path at any frequency.

        Args:
            run_id: The run the prospective debit would belong to.
            usage: Its token counts, or ``None`` if not stated.
            cost: Its cost estimate, or ``None`` if no pricing was applied.
            tags: Its tags, deciding which ``PER_TAG`` ceilings it would land in.

        Returns:
            One verdict per configured ceiling, in configuration order, including the prospective
            spend. With both ``usage`` and ``cost`` ``None`` there is nothing prospective to add
            and the answer equals :meth:`remaining`.

        Raises:
            UnknownRun: If this ledger has never seen ``run_id``.
            CurrencyMismatch: If the prospective cost is in a currency a money ceiling covering it
                caps in another currency — so a caller learns before spending, not after.
        """
        with self._lock:
            self._require_known(run_id)
            now = self._clock()
            self._book.require_currency_compatible(cost, run_id=run_id, at=now, tags=tags)
            return self._book.verdicts(run_id=run_id, at=now, usage=usage, cost=cost, tags=tags)

    def remaining(self, run_id: str) -> tuple[CeilingVerdict, ...]:
        """Report every ceiling's current balance for this run.

        ``PER_DAY`` ceilings are reported for the UTC day the injected clock is in — the window a
        debit made now would land in.

        Args:
            run_id: The run to report on.

        Returns:
            One verdict per configured ceiling, in configuration order.

        Raises:
            UnknownRun: If this ledger has never seen ``run_id``. Answering "nothing spent" for a
                mistyped run id would be indistinguishable from answering it for a real one.
        """
        with self._lock:
            self._require_known(run_id)
            return self._book.verdicts(run_id=run_id, at=self._clock())

    def balances(self, *, scope: CeilingScope, window_key: str) -> WindowBalance:
        """Report what one window has accumulated, naming no run and reading through no ceiling.

        The read a *view* needs, and the one :meth:`remaining` cannot give: a ``per_tag`` window
        with no ceiling over it has a balance, it simply has nothing to be measured against. No
        ceiling is consulted here at all, so a ledger built with none still answers.

        Side-effect-free, and no window is created by being asked about — asking about a tag
        nothing has been debited under leaves this ledger holding exactly the windows it held
        before.

        Args:
            scope: Which kind of window to report.
            window_key: The window within that scope — a ``run_id`` for ``PER_RUN``, a UTC day key
                for ``PER_DAY``, a tag for ``PER_TAG``. ``PER_DAY`` keys are the ones
                :func:`~loadledger.core.utc_day_key` produces; a raw date string in some other
                shape names a window nothing landed in, and gets an empty balance rather than a
                correction.

        Returns:
            The window's :class:`~loadledger.types.WindowBalance`, with the three honesty counts
            — identical to what a :class:`~loadledger.types.CeilingVerdict` over the same window
            reports, because both come from the same balance.

        Raises:
            ValueError: If ``window_key`` is blank. A blank key names a window nothing can land
                in, so an empty balance would look exactly like a real one and hide the bug.
        """
        _require_window_key(window_key)
        with self._lock:
            return self._book.balance_for((scope, window_key))

    def position(self) -> tuple[CeilingVerdict, ...]:
        """Report every configured ceiling's current balance, for no particular run.

        The ledger-wide counterpart of :meth:`remaining`, for a dashboard that is not about one
        run. ``PER_DAY`` ceilings are reported for the UTC day the injected clock is in — the
        window a debit made now would land in.

        Returns:
            One verdict per configured ceiling, in configuration order, with nothing prospective
            added. An empty ledger reports the configured caps with nothing spent, which is true
            rather than a fallback.

        Raises:
            InvalidCeiling: If any configured ceiling is ``PER_RUN``. A per-run cap has no window
                without a run; :meth:`remaining` is where to ask about one.
        """
        with self._lock:
            return self._book.verdicts_without_run(at=self._clock())

    def entries(
        self,
        *,
        run_id: str | None = None,
        tag: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[LedgerEntry]:
        """Return recorded entries, oldest first, narrowed by whichever filters are given.

        This is the input to a caller's own estimator and per-unit cost view (spec §6). It
        returns entries — usage and pricing hashes — not money, because the money is derived
        from those and re-derived whenever a price is corrected (ADR-0030 rule 1).

        Args:
            run_id: Keep only entries for this run.
            tag: Keep only entries whose debit carries this tag.
            since: Keep only entries at or after this instant. The window is half-open —
                ``occurred_at >= since`` — so two consecutive queries with touching bounds return
                each entry exactly once.

        Returns:
            A tuple in insertion order, which is the order the ledger recorded them. Filters
            combine with AND.

        Raises:
            ValueError: If ``since`` is naive. Comparing a naive bound against stored UTC
                instants would silently shift the window by the reader's local offset.
        """
        if since is not None and (since.tzinfo is None or since.tzinfo.utcoffset(since) is None):
            raise ValueError(
                "entries(since=...) requires a timezone-aware instant; got a naive one."
            )
        with self._lock:
            snapshot = tuple(self._entries)
        return tuple(
            entry
            for entry in snapshot
            if (run_id is None or entry.debit.run_id == run_id)
            and (tag is None or tag in entry.debit.tags)
            and (since is None or _at(entry) >= since)
        )

    def _require_known(self, run_id: str) -> None:
        """Raise :class:`~loadledger.errors.UnknownRun` unless this run has been seen."""
        if run_id not in self._runs:
            raise UnknownRun(
                f"No run {run_id!r} in this ledger. A run exists once it has been debited or "
                "declared with declare_run(); reporting an empty balance for an unrecognised id "
                "would look exactly like reporting one for a run that has spent nothing.",
                details={"run_id": run_id},
            )


def _require_window_key(window_key: str) -> None:
    """Raise :class:`ValueError` unless ``window_key`` names a window something could land in."""
    if not isinstance(window_key, str) or not window_key.strip():
        raise ValueError(f"window_key must be a non-blank window identifier; got {window_key!r}.")


def _at(entry: LedgerEntry) -> datetime:
    """Return an entry's resolved instant; a stored entry always has one."""
    occurred_at = entry.debit.occurred_at
    if occurred_at is None:  # pragma: no cover — a recorded entry always has a resolved instant.
        raise ValueError("A recorded entry must carry a resolved occurred_at.")
    return occurred_at
