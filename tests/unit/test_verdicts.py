"""Verdict arithmetic: exactness, per-currency separation, honesty counts, and goldens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import (
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenUsage,
    canonical_json,
)

from conftest import MIDDAY, ManualClock, cost_of, pricing, rates
from loadledger import (
    BalanceBook,
    BudgetCeiling,
    CeilingScope,
    CurrencyMismatch,
    Debit,
    InMemoryLedger,
    PartialPricing,
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

    def test_an_estimate_that_could_not_be_totalled_adds_only_what_it_priced(
        self, clock: ManualClock
    ) -> None:
        # A price list that predates the provider's cache pricing: a real, non-zero cache read
        # with no rate for it. The total refuses; the input cost is real and accumulates as a
        # floor (ADR-0069); the hash of the price that failed is kept.
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
        verdict = entry.verdicts[0]
        assert entry.unpriced is True
        assert entry.pricing_hash is not None
        assert verdict.money_spent == usd("0.003"), "1 000 input tokens at $3.00/M, nothing else"
        assert verdict.money_remaining == usd("4.997")
        assert verdict.unpriced_debit_count == 1
        assert verdict.untotalled_debit_count == 1
        assert verdict.unmetered_debit_count == 0
        assert verdict.exceeded is False

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


def adapter_shaped() -> TokenUsage:
    """What both real ModelRack adapters emit: input and output, cache classes unreported."""
    return TokenUsage(input_tokens=1_000, output_tokens=500)


def expired_pricing() -> ModelPricing:
    """A price observation whose window closed a month before MIDDAY: it prices nothing at it."""
    return ModelPricing(
        identity=ModelIdentity(ProviderKind.OPENAI_COMPATIBLE, "remote-fake-1"),
        rates=rates(),
        source=PricingSource.PROVIDER_PUBLISHED,
        observed_at=MIDDAY - timedelta(days=90),
        effective_from=MIDDAY - timedelta(days=90),
        effective_until=MIDDAY - timedelta(days=30),
    )


class TestPartialPricing:
    """ADR-0069: a partial price is a floor, and a money ceiling chooses how the floor binds."""

    FLOOR_NANOS = 10_500_000
    """1 000 input at $3.00/M = 3 000 000 nanos, plus 500 output at $15.00/M = 7 500 000."""

    def priced_partially(self, *, source_ref: str = "turn-1") -> Debit:
        usage = adapter_shaped()
        return Debit(run_id="traj-1", source_ref=source_ref, usage=usage, cost=cost_of(usage))

    def test_an_adapter_shaped_response_accumulates_its_priced_components(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=1_000_000)],
            clock=clock,
        )
        entry = ledger.debit(self.priced_partially())
        verdict = entry.verdicts[0]

        assert entry.unpriced is True, "the estimate did not total"
        assert verdict.money_spent == Money("USD", self.FLOOR_NANOS)
        assert verdict.money_remaining == usd("5.00") - Money("USD", self.FLOOR_NANOS)
        assert verdict.tokens_spent == 1_500
        assert (
            verdict.unpriced_debit_count,
            verdict.untotalled_debit_count,
            verdict.unmetered_debit_count,
        ) == (1, 1, 1), "the floor says it is one, on both sides"
        assert verdict.exceeded is False

    def test_the_floor_is_the_sum_of_the_components_the_total_would_have_been(
        self, clock: ManualClock
    ) -> None:
        # For an estimate that does total, the floor and the total are one number, by BaseAiCore's
        # own rule that the components a caller displays sum to the total beside them.
        usage = counted(input_tokens=1_000, output_tokens=500)
        cost = cost_of(usage)
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        verdict = ledger.debit(
            Debit(run_id="traj-1", source_ref="t", usage=usage, cost=cost)
        ).verdicts[0]
        assert verdict.money_spent == cost.total == Money("USD", self.FLOOR_NANOS)
        assert verdict.untotalled_debit_count == 0

    def test_on_a_floor_exceeded_is_certain_when_true(self, clock: ManualClock) -> None:
        # The floor alone is over this cap, so the cap has been crossed whatever the cache cost.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("0.01"))], clock=clock
        )
        verdict = ledger.debit(self.priced_partially()).verdicts[0]
        assert verdict.exceeded is True
        assert verdict.money_remaining == usd("0.01") - Money("USD", self.FLOOR_NANOS)

    def test_on_a_floor_not_exceeded_is_not_certain_and_the_count_says_so(
        self, clock: ManualClock
    ) -> None:
        # The default may fire late: the true cost is unknown, and the verdict carries the count
        # that tells the reader "under budget" is a floor's opinion.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("0.02"))], clock=clock
        )
        verdict = ledger.debit(self.priced_partially()).verdicts[0]
        assert verdict.exceeded is False
        assert verdict.unpriced_debit_count == 1

    def test_a_strict_ceiling_fires_on_the_same_debit(self, clock: ManualClock) -> None:
        ledger = InMemoryLedger(
            [
                BudgetCeiling(
                    scope=CeilingScope.PER_RUN,
                    money=usd("0.02"),
                    partial_pricing=PartialPricing.STRICT,
                )
            ],
            clock=clock,
        )
        verdict = ledger.debit(self.priced_partially()).verdicts[0]
        assert verdict.exceeded is True, "cannot be shown to be under the cap, so it is over it"
        assert verdict.money_spent == Money("USD", self.FLOOR_NANOS), "the floor is still reported"
        assert verdict.untotalled_debit_count == 1

    def test_a_strict_ceiling_refuses_at_pre_flight_before_any_spend(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger(
            [
                BudgetCeiling(
                    scope=CeilingScope.PER_RUN,
                    money=usd("5.00"),
                    partial_pricing=PartialPricing.STRICT,
                )
            ],
            clock=clock,
        )
        ledger.declare_run("traj-1")
        usage = adapter_shaped()
        pre_flight = ledger.would_exceed("traj-1", usage=usage, cost=cost_of(usage))[0]
        assert pre_flight.exceeded is True
        assert pre_flight.untotalled_debit_count == 1

        standing = ledger.remaining("traj-1")[0]
        assert standing.exceeded is False, "nothing was recorded; the refusal was prospective"
        assert standing.money_spent is None

    def test_a_strict_ceiling_does_not_fire_on_a_debit_with_no_estimate(
        self, clock: ManualClock
    ) -> None:
        # A local model's cost is unsupported by design (ADR-0030); the token ceiling governs it
        # (ADR-0047 §3). A strict money ceiling on a mixed trajectory must not halt on it.
        ledger = InMemoryLedger(
            [
                BudgetCeiling(
                    scope=CeilingScope.PER_RUN,
                    money=usd("5.00"),
                    tokens=1_000_000,
                    partial_pricing=PartialPricing.STRICT,
                )
            ],
            clock=clock,
        )
        verdict = ledger.debit(debit(usage=counted(input_tokens=1_200, output_tokens=340)))
        assert verdict.verdicts[0].exceeded is False
        assert verdict.verdicts[0].unpriced_debit_count == 1
        assert verdict.verdicts[0].untotalled_debit_count == 0
        assert verdict.verdicts[0].money_spent is None

    def test_a_strict_ceiling_fires_on_an_estimate_that_priced_nothing(
        self, clock: ManualClock
    ) -> None:
        # A price list that does not cover the instant: every component unsupported. Nothing is
        # added and no zero is created — and a strict ceiling still fires, because an estimate
        # was applied and could not show the spend to be under the cap.
        usage = counted(input_tokens=1_000, output_tokens=500)
        stale = cost_of(usage, price=expired_pricing())
        floor = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))], clock=clock
        )
        strict = InMemoryLedger(
            [
                BudgetCeiling(
                    scope=CeilingScope.PER_RUN,
                    money=usd("5.00"),
                    partial_pricing=PartialPricing.STRICT,
                )
            ],
            clock=clock,
        )
        for ledger in (floor, strict):
            verdict = ledger.debit(
                Debit(run_id="traj-1", source_ref="t", usage=usage, cost=stale)
            ).verdicts[0]
            assert verdict.money_spent is None, "nothing priced is not a zero"
            assert verdict.money_remaining == usd("5.00")
            assert (verdict.unpriced_debit_count, verdict.untotalled_debit_count) == (1, 1)
        assert floor.remaining("traj-1")[0].exceeded is False
        assert strict.remaining("traj-1")[0].exceeded is True

    def test_strictness_is_scoped_like_the_ceiling(self, clock: ManualClock) -> None:
        # A strict per-tag ceiling on the remote tier fires on a partially priced remote response
        # and says nothing about a run-scoped floor ceiling beside it.
        ledger = InMemoryLedger(
            [
                BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00")),
                BudgetCeiling(
                    scope=CeilingScope.PER_TAG,
                    money=usd("5.00"),
                    tag="tier:remote_cheap",
                    partial_pricing=PartialPricing.STRICT,
                ),
            ],
            clock=clock,
        )
        usage = adapter_shaped()
        entry = ledger.debit(
            Debit(
                run_id="traj-1",
                source_ref="t",
                usage=usage,
                cost=cost_of(usage),
                tags=("tier:remote_cheap",),
            )
        )
        per_run, per_tag = entry.verdicts
        assert per_run.exceeded is False
        assert per_tag.exceeded is True
        assert per_run.money_spent == per_tag.money_spent == Money("USD", self.FLOOR_NANOS)


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
            '{"ceiling":{"money":{"currency":"USD","nanos":5000000000},'
            '"partial_pricing":"floor","scope":"per_run","tag":null,"tokens":2000000},'
            '"exceeded":false,'
            '"money_remaining":{"currency":"USD","nanos":500000000},'
            '"money_spent":{"currency":"USD","nanos":4500000000},'
            '"tokens_remaining":899400,"tokens_spent":1100600,'
            '"unmetered_debit_count":0,"unpriced_debit_count":1,"untotalled_debit_count":0}'
        )
        assert per_tag == (
            '{"ceiling":{"money":null,"partial_pricing":"floor","scope":"per_tag",'
            '"tag":"tier:local_fast","tokens":1000},'
            '"exceeded":true,"money_remaining":null,"money_spent":null,'
            '"tokens_remaining":-1099000,"tokens_spent":1100000,'
            '"unmetered_debit_count":0,"unpriced_debit_count":0,"untotalled_debit_count":0}'
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
