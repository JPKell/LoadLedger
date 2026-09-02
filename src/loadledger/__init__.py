"""LoadLedger — budget accumulation and ceilings over ADR-0030's cost model.

Layer 3 capability package. It adds two things to what BaseAiCore already has, and deliberately
nothing else: it **adds spend up across a multi-step run**, and it **says when a ceiling is
crossed**. There is no pricing here (prices arrive as ``ModelPricing`` records the caller
acquired), no currency conversion (ADR-0030 rule 3), and no policy — the ledger answers "would
this exceed?" and "what remains?", and halting, pausing or re-approving is the caller's decision.

Phase 1 is pure: no I/O, no SQL, no logging, no environment. ``loadledger.sql`` and ``SqlLedger``
arrive in Phase 2 as the ``loadledger[sql]`` extra (ADR-0050).

    >>> from datetime import UTC, datetime
    >>> from baseaicore import Money, TokenUsage
    >>> from loadledger import BudgetCeiling, CeilingScope, Debit, InMemoryLedger
    >>> clock = lambda: datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    >>> ledger = InMemoryLedger(
    ...     [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=2_000_000)], clock=clock
    ... )
    >>> entry = ledger.debit(
    ...     Debit(
    ...         run_id="traj-1",
    ...         source_ref="turn-1",
    ...         usage=TokenUsage(input_tokens=1_200, output_tokens=340),
    ...         cost=None,
    ...     )
    ... )
    >>> entry.unpriced, entry.verdicts[0].tokens_spent, entry.verdicts[0].exceeded
    (True, 1540, False)

Anything not listed in ``__all__`` is private and may change without a version bump, whatever its
module happens to be named. ``Money`` is not re-exported: it is BaseAiCore's, and a second name
for one type is how two components stop agreeing about it.
"""

from __future__ import annotations

from loadledger.__about__ import __version__
from loadledger.core import BalanceBook, Ledger, utc_day_key, utc_day_start
from loadledger.errors import CurrencyMismatch, InvalidCeiling, LedgerError, UnknownRun
from loadledger.memory import InMemoryLedger
from loadledger.types import (
    BudgetCeiling,
    CeilingScope,
    CeilingVerdict,
    Debit,
    LedgerEntry,
    PartialPricing,
)

__all__ = [
    "BalanceBook",
    "BudgetCeiling",
    "CeilingScope",
    "CeilingVerdict",
    "CurrencyMismatch",
    "Debit",
    "InMemoryLedger",
    "InvalidCeiling",
    "Ledger",
    "LedgerEntry",
    "LedgerError",
    "PartialPricing",
    "UnknownRun",
    "__version__",
    "utc_day_key",
    "utc_day_start",
]
