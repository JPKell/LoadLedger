"""Budget assertions, excluded from the default run (`-m "not live and not performance"`).

The development plan names the likely failure mode outright: recomputing a balance by summing the
entry history on every debit. That is correct and quadratic, and it stays invisible in a unit test
because a hundred debits are fast either way. These tests are what would notice.
"""

from __future__ import annotations

import time

import pytest
from baseaicore import Money, TokenUsage

from conftest import MIDDAY, ManualClock
from loadledger import BudgetCeiling, CeilingScope, Debit, InMemoryLedger

DEBITS = 20_000


def a_debit(index: int) -> Debit:
    return Debit(
        run_id="traj-1",
        source_ref=f"turn-{index}",
        usage=TokenUsage(
            input_tokens=10, output_tokens=1, cache_write_tokens=0, cache_read_tokens=0
        ),
        cost=None,
        occurred_at=MIDDAY,
    )


def elapsed_ms_over(ledger: InMemoryLedger, start: int, stop: int) -> float:
    began = time.perf_counter_ns()
    for index in range(start, stop):
        ledger.debit(a_debit(index))
    return (time.perf_counter_ns() - began) / 1_000_000


@pytest.mark.performance
def test_debit_cost_does_not_grow_with_the_length_of_the_history() -> None:
    ledger = InMemoryLedger(
        [
            BudgetCeiling(scope=CeilingScope.PER_RUN, money=Money.from_decimal("USD", "500.00")),
            BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=10**12),
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=10**12, tag="tier:local_fast"),
        ],
        clock=ManualClock(),
    )
    first_slice = elapsed_ms_over(ledger, 0, 1_000)
    elapsed_ms_over(ledger, 1_000, DEBITS - 1_000)
    last_slice = elapsed_ms_over(ledger, DEBITS - 1_000, DEBITS)

    # Maintained balances make these two slices the same work. A summing implementation would
    # make the last slice roughly twenty times the first.
    assert last_slice < first_slice * 5 + 5, (
        f"first 1000 debits {first_slice:.1f} ms, last 1000 {last_slice:.1f} ms — "
        "balances look like they are being recomputed rather than maintained"
    )


@pytest.mark.performance
def test_a_ten_thousand_entry_history_queries_within_budget() -> None:
    """Spec §15: `entries` for a 10 000-entry run in ≤ 100 ms."""
    ledger = InMemoryLedger([], clock=ManualClock())
    for index in range(10_000):
        ledger.debit(a_debit(index))
    began = time.perf_counter_ns()
    found = ledger.entries(run_id="traj-1")
    assert len(found) == 10_000
    assert (time.perf_counter_ns() - began) / 1_000_000 <= 100
