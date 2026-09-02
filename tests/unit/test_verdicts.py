"""Verdict arithmetic: exactness, per-currency separation, honesty counts, and goldens."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from baseaicore import Money, TokenUsage, canonical_json

from conftest import MIDDAY, ManualClock, cost_of, pricing, rates
from loadledger import (
    BalanceBook,
    BudgetCeiling,
    CeilingScope,
    CurrencyMismatch,
    Debit,
    InMemoryLedger,
)


def usd(amount: str) -> Money:
    return Money.from_decimal("USD", amount)


def counted(**counts: int) -> TokenUsage:
    """A fully-reported usage: every class stated, so nothing is unmetered."""
    return TokenUsage(
        input_tokens=counts.get("input_tokens", 0),
        output_tokens=counts.get("output_tokens", 0),
        cache_write_tokens=counts.get("cache_write_tokens", 0),
        cache_read_tokens=counts.get("cache_read_tokens", 0),
    )


def debit(
    *,
    run_id: str = "traj-1",
    source_ref: str = "turn-1",
    usage: TokenUsage | None = None,
    priced: bool = False,
    currency: str = "USD",
    tags: tuple[str, ...] = (),
) -> Debit:
    resolved = usage if usage is not None else counted(input_tokens=1_000, output_tokens=100)
    cost = None
    if priced:
        cost = cost_of(resolved, price=pricing(rates(currency)))
    return Debit(run_id=run_id, source_ref=source_ref, usage=resolved, cost=cost, tags=tags)


class TestExactArithmetic:
    def test_token_sums_are_exact_at_large_counts(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10**15)], clock=clock
        )
        each = counted(input_tokens=999_999_999, output_tokens=1)
        for n in range(1_000):
            ledger.debit(debit(source_ref=f"turn-{n}", usage=each))
        assert ledger.remaining("traj-1")[0].tokens_spent == 1_000_000_000_000
        assert ledger.remaining("traj-1")[0].tokens_remaining == 10**15 - 10**12

    def test_money_sums_are_exact_at_nano_scale(self, clock: ManualClock) -> None:
        # 19 nanos per token is a real "per million tokens" figure; a float loses it by 1000 rows.
        cheap = rates(
            "USD",
            input_per_million="0.019",
            output_per_million=None,
            cache_write_per_million=None,
            cache_read_per_million=None,
        )
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("1.00"))], clock=clock
        )
        usage = counted(input_tokens=1)
        for n in range(1_000):
            ledger.debit(
                Debit(
                    run_id="traj-1",
                    source_ref=f"turn-{n}",
                    usage=usage,
                    cost=cost_of(usage, price=pricing(cheap)),
                )
            )
        assert ledger.remaining("traj-1")[0].money_spent == Money("USD", 19_000)

    def test_no_verdict_field_is_a_float(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=100)],
            clock=clock,
        )
        verdict = ledger.debit(debit(priced=True)).verdicts[0]
        assert isinstance(verdict.tokens_spent, int)
        assert isinstance(verdict.tokens_remaining, int)
        assert verdict.money_spent is not None
        assert isinstance(verdict.money_spent.nanos, int)
        assert "." not in canonical_json(verdict.as_canonical())


class TestPerCurrencySeparation:
    def test_two_currencies_accumulate_independently(self, clock: ManualClock) -> None:
        # Token-only ceilings, so neither currency is refused; the balances must not merge.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10_000_000)], clock=clock
        )
        usage = counted(input_tokens=1_000_000)
        ledger.debit(debit(source_ref="usd", usage=usage, priced=True, currency="USD"))
        ledger.debit(debit(source_ref="eur", usage=usage, priced=True, currency="EUR"))

        in_usd = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("10.00"))], clock=clock
        )
        in_usd.debit(debit(source_ref="usd", usage=usage, priced=True, currency="USD"))
        assert in_usd.remaining("traj-1")[0].money_spent == usd("3.00")
        # The EUR debit above never reached a USD balance, and no total mixed the two.
        assert ledger.remaining("traj-1")[0].tokens_spent == 2_000_000

    def test_a_mixed_currency_debit_is_refused_not_converted(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        with pytest.raises(CurrencyMismatch) as caught:
            ledger.debit(debit(priced=True, currency="EUR"))
        assert caught.value.code == "LEDGER_CURRENCY_MISMATCH"
        assert caught.value.details["debit_currency"] == "EUR"
        assert caught.value.details["ceiling_currency"] == "USD"
        assert "exchange rate" in caught.value.message

    def test_a_refused_debit_leaves_no_trace(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        ledger.declare_run("traj-1")
        with pytest.raises(CurrencyMismatch):
            ledger.debit(debit(priced=True, currency="EUR"))
        assert ledger.entries() == ()
        assert ledger.remaining("traj-1")[0].money_spent is None

    def test_would_exceed_refuses_the_same_mismatch_before_spending(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        ledger.declare_run("traj-1")
        usage = counted(input_tokens=1_000)
        with pytest.raises(CurrencyMismatch):
            ledger.would_exceed(
                "traj-1", usage=usage, cost=cost_of(usage, price=pricing(rates("EUR")))
            )

    def test_an_untotalled_estimate_in_another_currency_is_still_refused(
        self, clock: ManualClock
    ) -> None:
        # It adds nothing today, but re-costing it tomorrow produces EUR the USD cap cannot see.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        usage = TokenUsage(input_tokens=1_000)  # three classes unreported
        with pytest.raises(CurrencyMismatch):
            ledger.debit(
                Debit(
                    run_id="traj-1",
                    source_ref="turn-1",
                    usage=usage,
                    cost=cost_of(usage, price=pricing(rates("EUR"))),
                )
            )

    def test_a_tag_scoped_money_ceiling_only_refuses_debits_it_covers(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_TAG, money=usd("5.00"), tag="tier:usd")],
            clock=clock,
        )
        # Untagged: the USD-capped tag window does not cover it, so EUR is fine here.
        ledger.debit(debit(priced=True, currency="EUR"))
        with pytest.raises(CurrencyMismatch):
            ledger.debit(debit(source_ref="t2", priced=True, currency="EUR", tags=("tier:usd",)))


class TestUnpricedHonesty:
    def test_tokens_accumulate_while_money_is_untouched(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=1_000_000)],
            clock=clock,
        )
        entry = ledger.debit(debit(usage=counted(input_tokens=1_200, output_tokens=340)))
        verdict = entry.verdicts[0]

        assert entry.unpriced is True
        assert entry.pricing_hash is None
        assert verdict.tokens_spent == 1_540
        assert verdict.money_spent is None, "an unpriced debit must not create a zero balance"
        assert verdict.money_remaining == usd("5.00")
        assert verdict.unpriced_debit_count == 1
        assert verdict.exceeded is False

    def test_the_unpriced_count_rides_on_the_money_verdict(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        ledger.debit(debit(source_ref="local-1"))
        ledger.debit(debit(source_ref="local-2"))
        ledger.debit(debit(source_ref="remote-1", priced=True))

        verdict = ledger.remaining("traj-1")[0]
        assert verdict.money_spent == usd("0.004500")
        assert verdict.unpriced_debit_count == 2, (
            "'under budget' may not be claimed over an incomplete sum without saying so"
        )

    def test_an_estimate_that_could_not_be_totalled_counts_as_unpriced(
        self, clock: ManualClock
    ) -> None:
        # A price list that predates the provider's cache pricing: a real, non-zero cache read
        # with no rate for it. The total refuses; the hash of the price that failed is kept.
        gappy = rates("USD", cache_read_per_million=None)
        usage = counted(input_tokens=1_000, cache_read_tokens=500)
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        entry = ledger.debit(
            Debit(
                run_id="traj-1",
                source_ref="turn-1",
                usage=usage,
                cost=cost_of(usage, price=pricing(gappy)),
            )
        )
        assert entry.unpriced is True
        assert entry.pricing_hash is not None
        assert entry.verdicts[0].money_spent is None
        assert entry.verdicts[0].unpriced_debit_count == 1

    def test_an_unreported_token_class_is_excluded_not_zeroed(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        entry = ledger.debit(
            debit(usage=TokenUsage(input_tokens=100, output_tokens=50))  # cache classes absent
        )
        verdict = entry.verdicts[0]
        assert verdict.tokens_spent == 150
        assert verdict.unmetered_debit_count == 1, "the token balance is a floor, and says so"

    def test_a_fully_reported_debit_is_neither_unpriced_nor_unmetered(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=1_000)],
            clock=clock,
        )
        verdict = ledger.debit(debit(priced=True)).verdicts[0]
        assert (verdict.unpriced_debit_count, verdict.unmetered_debit_count) == (0, 0)

    def test_a_genuinely_free_call_is_a_zero_not_an_absence(self, clock: ManualClock) -> None:
        # Every class counted zero: nothing was used, so nothing was billed. That zero is honest.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        entry = ledger.debit(debit(usage=counted(), priced=True))
        assert entry.unpriced is False
        assert entry.verdicts[0].money_spent == usd("0")
        assert entry.verdicts[0].money_remaining == usd("5.00")


class TestCeilingBinding:
    def test_exactly_at_the_cap_is_not_exceeded(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        verdict = ledger.debit(debit(usage=counted(input_tokens=1_000))).verdicts[0]
        assert (verdict.exceeded, verdict.tokens_remaining) == (False, 0)

    def test_one_token_past_the_cap_is_exceeded(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        verdict = ledger.debit(debit(usage=counted(input_tokens=1_001))).verdicts[0]
        assert (verdict.exceeded, verdict.tokens_remaining) == (True, -1)

    def test_either_bound_of_a_two_bound_ceiling_can_fire(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=10)],
            clock=clock,
        )
        verdict = ledger.debit(debit(usage=counted(input_tokens=11), priced=True)).verdicts[0]
        assert verdict.exceeded is True
        assert verdict.money_spent is not None
        assert verdict.money_spent < usd("5.00"), "money is fine; the token bound is what fired"

    def test_the_most_restrictive_of_several_ceilings_binds(self, clock: ManualClock) -> None:
        ceilings = [
            BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=1_000_000),
            BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=2_000),
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=500, tag="tier:local_fast"),
        ]
        ledger = InMemoryLedger(ceilings, clock=clock)
        entry = ledger.debit(debit(usage=counted(input_tokens=800), tags=("tier:local_fast",)))
        by_scope = {verdict.ceiling.scope: verdict for verdict in entry.verdicts}

        assert [v.ceiling for v in entry.verdicts] == ceilings, "verdicts keep configured order"
        assert by_scope[CeilingScope.PER_DAY].exceeded is False
        assert by_scope[CeilingScope.PER_RUN].exceeded is False
        assert by_scope[CeilingScope.PER_TAG].exceeded is True
        assert any(verdict.exceeded for verdict in entry.verdicts)

    def test_a_zero_ceiling_binds_immediately(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger([BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=0)], clock=clock)
        assert ledger.debit(debit(usage=counted(input_tokens=1))).verdicts[0].exceeded is True

    def test_a_token_only_ceiling_reports_no_money_at_all(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        verdict = ledger.debit(debit(priced=True)).verdicts[0]
        assert (verdict.money_spent, verdict.money_remaining) == (None, None)

    def test_a_ledger_with_no_ceilings_records_and_never_refuses(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger([], clock=clock)
        entry = ledger.debit(debit(priced=True))
        assert entry.verdicts == ()
        assert len(ledger.entries()) == 1


class TestGoldenSerialization:
    """Spec contract 4 — verdicts appear in approval records, so their bytes are the contract."""

    def build(self) -> tuple[str, str]:
        clock = ManualClock(datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
        ceilings = [
            BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=2_000_000),
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=1_000, tag="tier:local_fast"),
        ]
        ledger = InMemoryLedger(ceilings, clock=clock)
        usage = counted(input_tokens=1_000_000, output_tokens=100_000)
        ledger.debit(
            Debit(
                run_id="traj-1",
                source_ref="turn-1",
                usage=usage,
                cost=cost_of(usage, at=MIDDAY),
                tags=("tier:local_fast",),
            )
        )
        ledger.debit(debit(source_ref="turn-2", usage=counted(input_tokens=600)))
        return tuple(  # type: ignore[return-value]
            canonical_json(verdict.as_canonical()) for verdict in ledger.remaining("traj-1")
        )

    @pytest.mark.contract
    def test_verdicts_serialize_to_their_golden_bytes(self) -> None:
        per_run, per_tag = self.build()
        assert per_run == (
            '{"ceiling":{"money":{"currency":"USD","nanos":5000000000},"scope":"per_run",'
            '"tag":null,"tokens":2000000},"exceeded":false,'
            '"money_remaining":{"currency":"USD","nanos":500000000},'
            '"money_spent":{"currency":"USD","nanos":4500000000},'
            '"tokens_remaining":899400,"tokens_spent":1100600,'
            '"unmetered_debit_count":0,"unpriced_debit_count":1}'
        )
        assert per_tag == (
            '{"ceiling":{"money":null,"scope":"per_tag","tag":"tier:local_fast","tokens":1000},'
            '"exceeded":true,"money_remaining":null,"money_spent":null,'
            '"tokens_remaining":-1099000,"tokens_spent":1100000,'
            '"unmetered_debit_count":0,"unpriced_debit_count":0}'
        )

    @pytest.mark.contract
    def test_the_same_inputs_reproduce_the_same_bytes(self) -> None:
        assert self.build() == self.build()


class TestBalanceBookDirectly:
    """The engine a SQL ledger will reuse in Phase 2, exercised without a ledger around it."""

    def test_it_keeps_the_configured_ceilings_in_order(self) -> None:
        ceilings = [
            BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=10),
            BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("1.00")),
        ]
        assert BalanceBook(ceilings).ceilings == tuple(ceilings)

    def test_it_holds_balances_and_not_a_history_to_re_sum(self) -> None:
        # The named failure mode is recomputing a balance by summing every past entry. This book
        # cannot do that even by accident: it keeps no entries at all, only one record per scope.
        book = BalanceBook([BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10)])
        for n in range(100):
            book.record(
                debit(source_ref=f"turn-{n}", usage=counted(input_tokens=1)), occurred_at=MIDDAY
            )
        assert book.verdicts(run_id="traj-1", at=MIDDAY)[0].tokens_spent == 100
        assert not any("entr" in slot for slot in BalanceBook.__slots__)

    def test_verdicts_do_not_move_a_balance(self) -> None:
        book = BalanceBook([BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10)])
        book.record(debit(usage=counted(input_tokens=3)), occurred_at=MIDDAY)
        for _ in range(10):
            book.verdicts(run_id="traj-1", at=MIDDAY, usage=counted(input_tokens=100))
        assert book.verdicts(run_id="traj-1", at=MIDDAY)[0].tokens_spent == 3
