"""Scope windows — and the one an operator guesses wrong: what "per day" means.

Every assertion here exists because ``PER_DAY`` is a **UTC** calendar day. A budget that reset at
the machine's local midnight would be a different budget on every machine, and the same ledger
replayed in another timezone would produce different verdicts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from baseaicore import Money, TokenUsage

from conftest import DAY, ManualClock, cost_of
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    Debit,
    InMemoryLedger,
    utc_day_key,
    utc_day_start,
)

MIDNIGHT = datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC)
ONE_MICROSECOND = timedelta(microseconds=1)


def a_debit(*, run_id: str = "traj-1", tokens: int, when: datetime | None = None) -> Debit:
    return Debit(
        run_id=run_id,
        source_ref=f"turn-{tokens}",
        usage=TokenUsage(
            input_tokens=tokens, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
        ),
        cost=None,
        occurred_at=when,
    )


class TestDayResolution:
    def test_day_start_is_utc_midnight(self) -> None:
        assert utc_day_start(datetime(2026, 9, 2, 23, 59, 59, tzinfo=UTC)) == datetime(
            2026, 9, 2, 0, 0, tzinfo=UTC
        )

    def test_an_offset_instant_is_converted_before_the_day_is_taken(self) -> None:
        # 23:30 in UTC-05:00 is 04:30 the *next* day in UTC, and belongs to that day's budget.
        evening_in_new_york = datetime(2026, 9, 2, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert utc_day_key(evening_in_new_york) == "2026-09-03"

    def test_the_west_of_utc_case_that_a_local_reading_would_get_wrong(self) -> None:
        # 01:00 in UTC+10:00 is 15:00 the *previous* day in UTC.
        morning_in_brisbane = datetime(2026, 9, 3, 1, 0, tzinfo=timezone(timedelta(hours=10)))
        assert utc_day_key(morning_in_brisbane) == "2026-09-02"

    def test_day_key_is_sortable_iso(self) -> None:
        keys = [utc_day_key(MIDNIGHT + n * DAY) for n in range(3)]
        assert keys == sorted(keys) == ["2026-09-03", "2026-09-04", "2026-09-05"]

    @pytest.mark.parametrize("resolve", [utc_day_start, utc_day_key])
    def test_a_naive_instant_is_refused(self, resolve: object) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            resolve(datetime(2026, 9, 2, 12, 0))  # type: ignore[operator]  # noqa: DTZ001


class TestPerDayCeilingBoundary:
    def per_day_ledger(self, clock: ManualClock) -> InMemoryLedger:
        return InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=1_000)], clock=clock
        )

    def test_the_last_microsecond_of_a_day_and_the_first_of_the_next_are_different_windows(
        self,
    ) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = self.per_day_ledger(clock)

        before = ledger.debit(a_debit(tokens=900))
        assert before.verdicts[0].tokens_spent == 900
        assert before.verdicts[0].exceeded is False

        clock.set(MIDNIGHT)
        after = ledger.debit(a_debit(tokens=900))

        # The new day starts empty: 900 + 900 would have exceeded a 1000-token cap in one window.
        assert after.verdicts[0].tokens_spent == 900
        assert after.verdicts[0].exceeded is False

    def test_the_previous_day_keeps_its_balance_when_a_debit_is_back_dated_into_it(self) -> None:
        clock = ManualClock(MIDNIGHT)
        ledger = self.per_day_ledger(clock)
        ledger.debit(a_debit(tokens=400))

        back_dated = ledger.debit(a_debit(tokens=900, when=MIDNIGHT - ONE_MICROSECOND))

        # The verdict describes the window the debit landed in — yesterday — not today's.
        assert back_dated.verdicts[0].tokens_spent == 900
        assert ledger.remaining("traj-1")[0].tokens_spent == 400

    def test_two_debits_either_side_of_midnight_never_share_a_balance(self) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = self.per_day_ledger(clock)
        ledger.debit(a_debit(tokens=600))
        clock.set(MIDNIGHT)
        ledger.debit(a_debit(tokens=600))
        assert ledger.remaining("traj-1")[0].tokens_spent == 600

    def test_a_ceiling_is_exceeded_within_one_day_and_clears_at_the_boundary(self) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = self.per_day_ledger(clock)
        ledger.debit(a_debit(tokens=600))
        crossing = ledger.debit(a_debit(tokens=600))
        assert crossing.verdicts[0].exceeded is True
        assert crossing.verdicts[0].tokens_remaining == -200

        clock.set(MIDNIGHT)
        assert ledger.remaining("traj-1")[0].exceeded is False
        assert ledger.remaining("traj-1")[0].tokens_remaining == 1_000

    def test_a_per_day_ceiling_is_ledger_wide_not_per_run(self) -> None:
        clock = ManualClock(MIDNIGHT)
        ledger = self.per_day_ledger(clock)
        ledger.debit(a_debit(run_id="traj-1", tokens=400))
        ledger.debit(a_debit(run_id="traj-2", tokens=400))
        assert ledger.remaining("traj-1")[0].tokens_spent == 800
        assert ledger.remaining("traj-2")[0].tokens_spent == 800


class TestPerRunAndPerTagWindows:
    def test_per_run_isolates_runs(self) -> None:
        clock = ManualClock()
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        ledger.debit(a_debit(run_id="traj-1", tokens=900))
        ledger.debit(a_debit(run_id="traj-2", tokens=100))
        assert ledger.remaining("traj-1")[0].tokens_spent == 900
        assert ledger.remaining("traj-2")[0].tokens_spent == 100

    def test_per_run_ignores_the_day_boundary(self) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=1_000)], clock=clock
        )
        ledger.debit(a_debit(tokens=600))
        clock.set(MIDNIGHT)
        ledger.debit(a_debit(tokens=600))
        assert ledger.remaining("traj-1")[0].tokens_spent == 1_200
        assert ledger.remaining("traj-1")[0].exceeded is True

    def test_per_tag_binds_only_debits_carrying_the_tag(self) -> None:
        clock = ManualClock()
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=1_000, tag="tier:local_fast")],
            clock=clock,
        )
        tagged = Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=TokenUsage(
                input_tokens=800, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
            ),
            cost=None,
            tags=("tier:local_fast",),
        )
        ledger.debit(tagged)
        ledger.debit(a_debit(tokens=800))  # untagged, and therefore outside this window
        assert ledger.remaining("traj-1")[0].tokens_spent == 800

    def test_per_tag_spans_runs_and_days(self) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=1_000, tag="tier:remote")],
            clock=clock,
        )

        def tagged(run_id: str) -> Debit:
            return Debit(
                run_id=run_id,
                source_ref="turn",
                usage=TokenUsage(
                    input_tokens=600, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
                ),
                cost=None,
                tags=("tier:remote",),
            )

        ledger.debit(tagged("traj-1"))
        clock.set(MIDNIGHT)
        crossing = ledger.debit(tagged("traj-2"))
        assert crossing.verdicts[0].tokens_spent == 1_200
        assert crossing.verdicts[0].exceeded is True


class TestMoneyFollowsTheSameWindows:
    def test_a_money_ceiling_resets_with_its_utc_day(self) -> None:
        clock = ManualClock(MIDNIGHT - ONE_MICROSECOND)
        ledger = InMemoryLedger(
            [BudgetCeiling(scope=CeilingScope.PER_DAY, money=Money.from_decimal("USD", "5.00"))],
            clock=clock,
        )
        usage = TokenUsage(
            input_tokens=1_000_000, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
        )
        yesterday = ledger.debit(
            Debit(run_id="traj-1", source_ref="turn-1", usage=usage, cost=cost_of(usage))
        )
        assert yesterday.verdicts[0].money_spent == Money.from_decimal("USD", "3.00")

        clock.set(MIDNIGHT)
        # A fresh window has nothing priced in it yet, which is not the same as $0.00 spent.
        today = ledger.remaining("traj-1")[0]
        assert today.money_spent is None
        assert today.money_remaining == Money.from_decimal("USD", "5.00")
