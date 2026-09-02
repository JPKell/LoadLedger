"""``InMemoryLedger`` as an implementation: its protocol surface, its refusals, its history.

Includes Phase 1 acceptance criterion 2 — entries re-priced under a corrected ``ModelPricing``
reproduce totals with no stored row changed (spec contract 1, ADR-0030 rule 1) — and the
side-effect-free assertion for ``would_exceed`` (spec contract 6).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from baseaicore import (
    ModelPricing,
    Money,
    TokenUsage,
    canonical_json,
    estimate_cost,
    is_supported,
    sha256_of,
)

from conftest import MIDDAY, ManualClock, cost_of, pricing, rates
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    Debit,
    InMemoryLedger,
    Ledger,
    LedgerEntry,
    UnknownRun,
)


def usd(amount: str) -> Money:
    return Money.from_decimal("USD", amount)


def counted(**counts: int) -> TokenUsage:
    return TokenUsage(
        input_tokens=counts.get("input_tokens", 0),
        output_tokens=counts.get("output_tokens", 0),
        cache_write_tokens=counts.get("cache_write_tokens", 0),
        cache_read_tokens=counts.get("cache_read_tokens", 0),
    )


def a_ledger(clock: ManualClock) -> InMemoryLedger:
    return InMemoryLedger(
        [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=2_000_000)],
        clock=clock,
    )


def local_debit(*, run_id: str = "traj-1", source_ref: str = "turn-1", tokens: int = 100) -> Debit:
    return Debit(
        run_id=run_id,
        source_ref=source_ref,
        usage=counted(input_tokens=tokens),
        cost=None,
    )


class TestProtocolConformance:
    def test_the_in_memory_ledger_satisfies_the_protocol(self, clock: ManualClock) -> None:
        assert isinstance(a_ledger(clock), Ledger)

    def test_it_is_a_first_class_implementation_not_a_reduced_stub(
        self, clock: ManualClock
    ) -> None:
        ledger = a_ledger(clock)
        for method in ("debit", "would_exceed", "remaining", "entries", "declare_run"):
            assert callable(getattr(ledger, method))


class TestRunExistence:
    @pytest.mark.parametrize("query", ["remaining", "would_exceed"])
    def test_an_unknown_run_is_refused(self, clock: ManualClock, query: str) -> None:
        with pytest.raises(UnknownRun) as caught:
            getattr(a_ledger(clock), query)("never-seen")
        assert caught.value.code == "LEDGER_UNKNOWN_RUN"
        assert caught.value.details["run_id"] == "never-seen"

    def test_a_debit_brings_a_run_into_existence(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.debit(local_debit())
        assert ledger.remaining("traj-1")[0].tokens_spent == 100

    def test_declaring_a_run_brings_it_into_existence_with_a_full_budget(
        self, clock: ManualClock
    ) -> None:
        ledger = a_ledger(clock)
        ledger.declare_run("traj-1")
        verdict = ledger.remaining("traj-1")[0]
        assert verdict.tokens_spent == 0
        assert verdict.tokens_remaining == 2_000_000
        assert verdict.money_spent is None, "nothing priced is not the same as nothing spent"
        assert verdict.money_remaining == usd("5.00")

    def test_declaring_twice_is_harmless(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.declare_run("traj-1")
        ledger.declare_run("traj-1")
        assert ledger.remaining("traj-1")[0].tokens_spent == 0

    @pytest.mark.parametrize("run_id", ["", "   ", 7])
    def test_declaring_a_blank_run_is_refused(self, clock: ManualClock, run_id: object) -> None:
        with pytest.raises(ValueError, match="non-blank identifier"):
            a_ledger(clock).declare_run(run_id)  # type: ignore[arg-type]


class TestDebitRecording:
    def test_the_entry_resolves_the_instant_from_the_injected_clock(
        self, clock: ManualClock
    ) -> None:
        ledger = a_ledger(clock)
        entry = ledger.debit(local_debit())
        assert entry.debit.occurred_at == MIDDAY

    def test_a_supplied_instant_is_kept(self, clock: ManualClock) -> None:
        when = MIDDAY - timedelta(hours=3)
        ledger = a_ledger(clock)
        entry = ledger.debit(
            Debit(
                run_id="traj-1",
                source_ref="turn-1",
                usage=counted(input_tokens=1),
                cost=None,
                occurred_at=when,
            )
        )
        assert entry.debit.occurred_at == when

    def test_entry_ids_are_unique_and_ordered(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ids = [ledger.debit(local_debit(source_ref=f"turn-{n}")).entry_id for n in range(50)]
        assert len(set(ids)) == 50
        assert ids == sorted(ids), "ULIDs from one generator sort in the order they were made"

    def test_the_pricing_hash_is_stored_and_the_money_is_not_the_record(
        self, clock: ManualClock
    ) -> None:
        usage = counted(input_tokens=1_000)
        price = pricing()
        ledger = a_ledger(clock)
        entry = ledger.debit(
            Debit(run_id="traj-1", source_ref="t", usage=usage, cost=cost_of(usage, price=price))
        )
        assert entry.pricing_hash == price.pricing_hash
        assert "nanos" not in canonical_json(entry.as_canonical()["debit"])

    def test_exceeding_a_ceiling_is_recorded_not_raised(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        entry = ledger.debit(local_debit(tokens=3_000_000))
        assert entry.verdicts[0].exceeded is True
        assert len(ledger.entries()) == 1, "the debit stands; refusing work is the caller's policy"

    def test_the_entry_carries_one_verdict_per_configured_ceiling(self, clock: ManualClock) -> None:
        ceilings = [
            BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10),
            BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=20),
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=30, tag="t"),
        ]
        ledger = InMemoryLedger(ceilings, clock=clock)
        assert len(ledger.debit(local_debit(tokens=1)).verdicts) == 3

    def test_concurrent_debits_all_land_exactly_once(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10**9)], clock=clock
        )

        def spend(worker: int) -> None:
            for n in range(50):
                ledger.debit(local_debit(source_ref=f"w{worker}-{n}", tokens=10))

        workers = [threading.Thread(target=spend, args=(w,)) for w in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert ledger.remaining("traj-1")[0].tokens_spent == 8 * 50 * 10
        assert len(ledger.entries()) == 400


class TestWouldExceedIsSideEffectFree:
    def state_hash(self, ledger: InMemoryLedger) -> str:
        """Hash everything the ledger would ever report, so any mutation changes it."""
        return sha256_of(
            {
                "entries": [entry.as_canonical() for entry in ledger.entries()],
                "remaining": [verdict.as_canonical() for verdict in ledger.remaining("traj-1")],
            }
        )

    def test_the_state_hash_is_unchanged_across_many_calls(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.debit(local_debit(tokens=1_000))
        before = self.state_hash(ledger)

        usage = counted(input_tokens=500_000, output_tokens=100_000)
        for _ in range(100):
            ledger.would_exceed("traj-1", usage=usage, cost=cost_of(usage))

        assert self.state_hash(ledger) == before

    def test_it_answers_about_the_prospective_debit_all_the_same(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.debit(local_debit(tokens=1_000))
        verdict = ledger.would_exceed("traj-1", usage=counted(input_tokens=2_500_000))[0]
        assert verdict.exceeded is True
        assert verdict.tokens_spent == 2_501_000
        assert ledger.remaining("traj-1")[0].tokens_spent == 1_000

    def test_the_pre_flight_refusal_and_the_crossing_debit_agree(self, clock: ManualClock) -> None:
        # Spec §20 criterion 1's shape: a $5.00 + 2M-token trajectory ceiling.
        ledger = a_ledger(clock)
        usage = counted(input_tokens=1_500_000, output_tokens=100_000)
        cost = cost_of(usage)
        ledger.debit(Debit(run_id="traj-1", source_ref="turn-1", usage=usage, cost=cost))

        pre_flight = ledger.would_exceed("traj-1", usage=usage, cost=cost)[0]
        assert pre_flight.exceeded is True, "the caller can refuse the step before spending"

        crossing = ledger.debit(
            Debit(run_id="traj-1", source_ref="turn-2", usage=usage, cost=cost)
        ).verdicts[0]
        assert crossing.exceeded is True
        assert crossing.money_spent == pre_flight.money_spent
        assert crossing.tokens_spent == pre_flight.tokens_spent

    def test_with_nothing_prospective_it_equals_remaining(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.debit(local_debit(tokens=42))
        assert ledger.would_exceed("traj-1") == ledger.remaining("traj-1")

    def test_a_cost_without_counts_is_unmetered_not_free(self, clock: ManualClock) -> None:
        ledger = a_ledger(clock)
        ledger.declare_run("traj-1")
        usage = counted(input_tokens=1_000_000)
        verdict = ledger.would_exceed("traj-1", cost=cost_of(usage))[0]
        assert verdict.money_spent == usd("3.00")
        assert verdict.unmetered_debit_count == 1


class TestEntries:
    def populated(self, clock: ManualClock) -> InMemoryLedger:
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(local_debit(run_id="traj-1", source_ref="a"))
        clock.advance(timedelta(hours=1))
        ledger.debit(
            Debit(
                run_id="traj-2",
                source_ref="b",
                usage=counted(input_tokens=1),
                cost=None,
                tags=("tier:remote",),
            )
        )
        clock.advance(timedelta(hours=1))
        ledger.debit(local_debit(run_id="traj-1", source_ref="c"))
        return ledger

    def test_unfiltered_returns_everything_in_insertion_order(self, clock: ManualClock) -> None:
        assert [e.debit.source_ref for e in self.populated(clock).entries()] == ["a", "b", "c"]

    def test_filtered_by_run(self, clock: ManualClock) -> None:
        entries = self.populated(clock).entries(run_id="traj-1")
        assert [e.debit.source_ref for e in entries] == ["a", "c"]

    def test_filtered_by_tag(self, clock: ManualClock) -> None:
        entries = self.populated(clock).entries(tag="tier:remote")
        assert [e.debit.source_ref for e in entries] == ["b"]

    def test_since_is_inclusive_so_a_half_open_window_returns_each_entry_once(
        self, clock: ManualClock
    ) -> None:
        ledger = self.populated(clock)
        boundary = MIDDAY + timedelta(hours=1)
        earlier = ledger.entries(since=MIDDAY)
        later = ledger.entries(since=boundary)
        assert [e.debit.source_ref for e in later] == ["b", "c"]
        assert len(earlier) == 3

    def test_filters_combine_with_and(self, clock: ManualClock) -> None:
        entries = self.populated(clock).entries(run_id="traj-2", tag="tier:remote")
        assert [e.debit.source_ref for e in entries] == ["b"]
        assert self.populated(clock).entries(run_id="traj-1", tag="tier:remote") == ()

    def test_a_naive_since_is_refused(self, clock: ManualClock) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            self.populated(clock).entries(since=MIDDAY.replace(tzinfo=None))

    def test_the_returned_sequence_cannot_mutate_the_ledger(self, clock: ManualClock) -> None:
        ledger = self.populated(clock)
        returned = ledger.entries()
        assert isinstance(returned, tuple)
        assert len(ledger.entries()) == 3


class TestRecosting:
    """Acceptance criterion 2 / spec contract 1: cost is derived, never stored."""

    USAGES = (
        (1_000_000, 100_000),
        (250_000, 33_333),
        (7, 1),
    )

    @staticmethod
    def expected_nanos(*, input_nanos_per_token: int, output_nanos_per_token: int) -> int:
        """Total the history in whole nanos, independently of the package under test."""
        return sum(
            inputs * input_nanos_per_token + outputs * output_nanos_per_token
            for inputs, outputs in TestRecosting.USAGES
        )

    @staticmethod
    def recost(entries: tuple[LedgerEntry, ...], price: ModelPricing) -> Money:
        """Re-derive the money from what was actually stored: counts plus a price."""
        total = Money.zero("USD")
        for entry in entries:
            occurred_at = entry.debit.occurred_at
            assert occurred_at is not None
            estimate = estimate_cost(entry.debit.usage, price, at=occurred_at)
            assert is_supported(estimate.total)
            total = total + estimate.total
        return total

    def test_history_recosts_under_a_corrected_price_with_no_stored_row_changing(
        self, clock: ManualClock
    ) -> None:
        wrong = pricing(rates("USD", input_per_million="3.00", output_per_million="15.00"))
        corrected = pricing(rates("USD", input_per_million="2.50", output_per_million="12.00"))
        assert wrong.pricing_hash != corrected.pricing_hash

        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("100.00"))], clock=clock
        )
        for index, (inputs, outputs) in enumerate(self.USAGES):
            usage = counted(input_tokens=inputs, output_tokens=outputs)
            ledger.debit(
                Debit(
                    run_id="traj-1",
                    source_ref=f"turn-{index}",
                    usage=usage,
                    cost=cost_of(usage, price=wrong),
                )
            )

        stored = tuple(ledger.entries(run_id="traj-1"))
        before = [canonical_json(entry.as_canonical()) for entry in stored]

        as_billed = ledger.remaining("traj-1")[0].money_spent
        assert as_billed == Money(
            "USD", self.expected_nanos(input_nanos_per_token=3_000, output_nanos_per_token=15_000)
        )

        assert self.recost(stored, wrong) == as_billed, (
            "the stored counts and hash reproduce the figure that was reported at the time"
        )
        recosted = self.recost(stored, corrected)
        assert recosted == Money(
            "USD", self.expected_nanos(input_nanos_per_token=2_500, output_nanos_per_token=12_000)
        )
        assert recosted != as_billed, "the correction has somewhere to go"

        # ADR-0030 rule 1: not one stored row moved to make that happen.
        assert [canonical_json(entry.as_canonical()) for entry in stored] == before
        assert ledger.entries(run_id="traj-1") == stored
        assert {entry.pricing_hash for entry in stored} == {wrong.pricing_hash}

    def test_the_stored_hash_names_the_price_that_produced_the_figure(
        self, clock: ManualClock
    ) -> None:
        price = pricing(rates("USD", input_per_million="1.00"))
        ledger = InMemoryLedger([], clock=clock)
        usage = counted(input_tokens=2_000_000)
        entry = ledger.debit(
            Debit(run_id="traj-1", source_ref="t", usage=usage, cost=cost_of(usage, price=price))
        )
        assert entry.pricing_hash == price.pricing_hash
        recosted = estimate_cost(entry.debit.usage, price, at=MIDDAY)
        assert recosted.total == usd("2.00")
        assert recosted.pricing_hash == entry.pricing_hash


def test_decimal_is_never_needed_at_the_boundary() -> None:
    """A guard on the module's own imports: nothing here converts through a float."""
    assert Money.from_decimal("USD", Decimal("0.019")).nanos == 19_000_000
