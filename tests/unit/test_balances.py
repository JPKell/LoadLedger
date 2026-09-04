"""A balance read that names no run: one window's spend, and a ledger-wide position.

Two reads with one property between them — they must never disagree with the verdicts the same
ledger already gives, because a dashboard reading one beside an approval reading the other would
otherwise show two truths about one window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import Money, TokenUsage

from conftest import DAY, MIDDAY, ManualClock, cost_of, pricing, rates
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    Debit,
    InMemoryLedger,
    InvalidCeiling,
    WindowBalance,
    utc_day_key,
)


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
    occurred_at: datetime | None = None,
) -> Debit:
    resolved = usage if usage is not None else counted(input_tokens=1_000, output_tokens=100)
    cost = cost_of(resolved, price=pricing(rates(currency))) if priced else None
    return Debit(
        run_id=run_id,
        source_ref=source_ref,
        usage=resolved,
        cost=cost,
        tags=tags,
        occurred_at=occurred_at,
    )


class TestBalancesNeedNoCeiling:
    def test_a_tag_with_no_ceiling_over_it_still_reports_its_balance(
        self, clock: ManualClock
    ) -> None:
        # The whole reason this read exists: PromptCadence configures no tier ceiling, and a tier
        # still has spend. A ledger with no ceilings at all answers.
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(debit(priced=True, tags=("tier:local_fast",)))

        balance = ledger.balances(scope=CeilingScope.PER_TAG, window_key="tier:local_fast")

        assert balance == WindowBalance(
            scope=CeilingScope.PER_TAG,
            window_key="tier:local_fast",
            tokens_spent=1_100,
            money_spent=(Money.from_decimal("USD", "0.0045"),),
        )

    def test_an_unknown_window_is_nothing_spent_and_not_an_error(self, clock: ManualClock) -> None:
        # UnknownRun cannot apply: this read names no run. "Nothing has been spent here" is a true
        # answer, and no money is reported at all rather than a zero that would read as "free".
        ledger = InMemoryLedger([], clock=clock)

        balance = ledger.balances(scope=CeilingScope.PER_TAG, window_key="tier:never_used")

        assert balance.tokens_spent == 0
        assert balance.money_spent == ()
        assert balance.unpriced_debit_count == 0

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_window_key_is_refused_rather_than_answered(
        self, clock: ManualClock, blank: str
    ) -> None:
        ledger = InMemoryLedger([], clock=clock)
        with pytest.raises(ValueError, match="non-blank"):
            ledger.balances(scope=CeilingScope.PER_TAG, window_key=blank)

    def test_asking_about_a_window_does_not_bring_it_into_existence(
        self, clock: ManualClock
    ) -> None:
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(debit(tags=("tier:local_fast",)))

        ledger.balances(scope=CeilingScope.PER_TAG, window_key="tier:never_used")

        # Reading the empty window left no trace: the real one is unchanged and the invented one
        # is still empty. (SqlLedger proves the same structurally, by row count.)
        assert (
            ledger.balances(scope=CeilingScope.PER_TAG, window_key="tier:local_fast").tokens_spent
            == 1_100
        )
        assert (
            ledger.balances(scope=CeilingScope.PER_TAG, window_key="tier:never_used").tokens_spent
            == 0
        )


class TestTheTwoReadsCannotDisagree:
    def test_the_three_counts_match_the_verdict_over_the_same_window(
        self, clock: ManualClock
    ) -> None:
        # One unpriced debit, one untotalled estimate and one unmetered usage, so all three counts
        # are non-zero and a mismatch cannot hide behind a zero.
        tag = "tier:local_fast"
        capped = BudgetCeiling(
            scope=CeilingScope.PER_TAG, tag=tag, money=Money.from_decimal("USD", "5.00")
        )
        ledger = InMemoryLedger([capped], clock=clock)
        partial = rates("USD", output_per_million=None)
        ledger.debit(debit(source_ref="turn-1", tags=(tag,)))
        ledger.debit(
            Debit(
                run_id="traj-1",
                source_ref="turn-2",
                usage=counted(input_tokens=1_000, output_tokens=100),
                cost=cost_of(
                    counted(input_tokens=1_000, output_tokens=100), price=pricing(partial)
                ),
                tags=(tag,),
            )
        )
        ledger.debit(
            debit(source_ref="turn-3", usage=TokenUsage(input_tokens=10), priced=True, tags=(tag,))
        )

        balance = ledger.balances(scope=CeilingScope.PER_TAG, window_key=tag)
        verdict = ledger.remaining("traj-1")[0]

        assert (
            (
                balance.unpriced_debit_count,
                balance.untotalled_debit_count,
                balance.unmetered_debit_count,
            )
            == (
                verdict.unpriced_debit_count,
                verdict.untotalled_debit_count,
                verdict.unmetered_debit_count,
            )
            == (3, 2, 1)
        )
        assert balance.tokens_spent == verdict.tokens_spent
        assert balance.money_spent == (verdict.money_spent,)

    def test_a_per_day_key_lands_where_a_debit_at_that_instant_did(
        self, clock: ManualClock
    ) -> None:
        yesterday = MIDDAY - DAY
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(debit(source_ref="turn-1", occurred_at=yesterday))
        ledger.debit(debit(source_ref="turn-2"))

        assert (
            ledger.balances(
                scope=CeilingScope.PER_DAY, window_key=utc_day_key(yesterday)
            ).tokens_spent
            == 1_100
        )
        assert (
            ledger.balances(scope=CeilingScope.PER_DAY, window_key=utc_day_key(MIDDAY)).tokens_spent
            == 1_100
        )

    def test_a_date_string_that_is_not_a_utc_day_key_names_an_empty_window(
        self, clock: ManualClock
    ) -> None:
        # Documented rather than corrected: the key is the caller's, and a key nothing landed in
        # holds nothing. utc_day_key is exported so a caller never has to guess the spelling.
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(debit())

        assert ledger.balances(scope=CeilingScope.PER_DAY, window_key="2026-9-2").tokens_spent == 0

    def test_a_mixed_currency_window_reports_both_and_sums_neither(
        self, clock: ManualClock
    ) -> None:
        # No money ceiling covers this tag, so no CurrencyMismatch is possible and both land.
        tag = "tier:mixed"
        ledger = InMemoryLedger([], clock=clock)
        ledger.debit(debit(source_ref="turn-1", priced=True, currency="USD", tags=(tag,)))
        ledger.debit(debit(source_ref="turn-2", priced=True, currency="EUR", tags=(tag,)))

        balance = ledger.balances(scope=CeilingScope.PER_TAG, window_key=tag)

        # Ascending by currency code, one figure each, no total — converting needs a rate this
        # package will not assume (ADR-0030 rule 3).
        assert balance.money_spent == (
            Money.from_decimal("EUR", "0.0045"),
            Money.from_decimal("USD", "0.0045"),
        )
        assert balance.as_canonical()["money_spent"] == [
            {"currency": "EUR", "nanos": 4_500_000},
            {"currency": "USD", "nanos": 4_500_000},
        ]


class TestPositionNamesNoRun:
    def ledger_wide(self, clock: ManualClock) -> InMemoryLedger:
        return InMemoryLedger(
            [
                BudgetCeiling(scope=CeilingScope.PER_DAY, money=Money.from_decimal("USD", "25.00")),
                BudgetCeiling(scope=CeilingScope.PER_TAG, tag="project:alpha", tokens=1_000_000),
            ],
            clock=clock,
        )

    def test_it_answers_what_remaining_answers_without_being_told_a_run(
        self, clock: ManualClock
    ) -> None:
        ledger = self.ledger_wide(clock)
        ledger.debit(debit(priced=True, tags=("project:alpha",)))

        assert ledger.position() == ledger.remaining("traj-1")

    def test_an_empty_ledger_reports_the_caps_with_nothing_spent(self, clock: ManualClock) -> None:
        # Not a fallback reached through UnknownRun: there is simply nothing in the windows.
        position = self.ledger_wide(clock).position()

        assert [verdict.tokens_spent for verdict in position] == [0, 0]
        assert position[0].money_spent is None
        assert position[0].money_remaining == Money.from_decimal("USD", "25.00")
        assert not any(verdict.exceeded for verdict in position)

    def test_a_per_run_ceiling_is_refused_rather_than_omitted(self, clock: ManualClock) -> None:
        # Omitting it would silently shorten a tuple whose positions are documented API; answering
        # it against an arbitrary run would report one run's spend under a ledger-wide heading.
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=2_000_000)], clock=clock
        )
        with pytest.raises(InvalidCeiling) as raised:
            ledger.position()

        assert raised.value.details["scope"] == "per_run"
        assert raised.value.details["ceiling_count"] == 1

    def test_the_per_day_window_is_the_clock_s_utc_day(self, clock: ManualClock) -> None:
        ledger = self.ledger_wide(clock)
        ledger.debit(debit(priced=True))
        assert ledger.position()[0].money_spent == Money.from_decimal("USD", "0.0045")

        clock.advance(DAY)

        # A new UTC day is a new window, and yesterday's spend is not in it.
        assert ledger.position()[0].money_spent is None
        assert ledger.balances(
            scope=CeilingScope.PER_DAY, window_key=utc_day_key(MIDDAY)
        ).money_spent == (Money.from_decimal("USD", "0.0045"),)


def test_a_balance_read_across_a_utc_midnight_follows_the_instant_not_the_clock(
    clock: ManualClock,
) -> None:
    # The boundary this package must get right: a debit one second before midnight is yesterday's,
    # whatever the reader's clock says when it asks.
    ledger = InMemoryLedger([], clock=clock)
    before = datetime(2026, 9, 2, 23, 59, 59, tzinfo=UTC)
    after = before + timedelta(seconds=1)
    ledger.debit(debit(source_ref="turn-1", occurred_at=before))
    ledger.debit(debit(source_ref="turn-2", occurred_at=after))

    assert utc_day_key(before) == "2026-09-02"
    assert utc_day_key(after) == "2026-09-03"
    assert (
        ledger.balances(scope=CeilingScope.PER_DAY, window_key="2026-09-02").tokens_spent == 1_100
    )
    assert (
        ledger.balances(scope=CeilingScope.PER_DAY, window_key="2026-09-03").tokens_spent == 1_100
    )
