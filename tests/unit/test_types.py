"""The value objects: what each one refuses at construction, and what it serializes to."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from baseaicore import UNSUPPORTED, Money, TokenUsage, canonical_json

from conftest import MIDDAY, cost_of
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    CeilingVerdict,
    Debit,
    InvalidCeiling,
    PartialPricing,
)


def usd(amount: str) -> Money:
    return Money.from_decimal("USD", amount)


class TestBudgetCeiling:
    def test_binds_money_alone(self) -> None:
        ceiling = BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))
        assert ceiling.money == usd("5.00")
        assert ceiling.tokens is None

    def test_binds_tokens_alone(self) -> None:
        assert BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=2_000_000).tokens == 2_000_000

    def test_binds_both(self) -> None:
        ceiling = BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=2_000_000)
        assert (ceiling.money, ceiling.tokens) == (usd("5.00"), 2_000_000)

    def test_refuses_a_ceiling_that_binds_nothing(self) -> None:
        with pytest.raises(InvalidCeiling) as caught:
            BudgetCeiling(scope=CeilingScope.PER_RUN)
        assert caught.value.code == "LEDGER_CEILING_INVALID"
        assert "money, tokens, or both" in caught.value.message

    def test_zero_is_a_legitimate_bound_and_is_not_read_as_absent(self) -> None:
        # The trap this guards: `if ceiling.tokens:` treats a "spend nothing" cap as no cap.
        assert BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=0).tokens == 0
        assert BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("0")).money == usd("0")

    def test_refuses_a_negative_money_bound(self) -> None:
        with pytest.raises(InvalidCeiling, match="must not be negative"):
            BudgetCeiling(scope=CeilingScope.PER_RUN, money=Money("USD", -1))

    def test_refuses_a_negative_token_bound(self) -> None:
        with pytest.raises(InvalidCeiling, match="must not be negative"):
            BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=-1)

    def test_refuses_money_that_is_not_money(self) -> None:
        with pytest.raises(InvalidCeiling, match="must be Money or None"):
            BudgetCeiling(scope=CeilingScope.PER_RUN, money="5.00")  # type: ignore[arg-type]

    @pytest.mark.parametrize("tokens", [1.5, True, "2000"])
    def test_refuses_a_token_bound_that_is_not_a_whole_number(self, tokens: object) -> None:
        with pytest.raises(InvalidCeiling, match="whole number of tokens"):
            BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=tokens)  # type: ignore[arg-type]

    def test_per_tag_requires_a_tag(self) -> None:
        with pytest.raises(InvalidCeiling, match="must name the tag"):
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=10)

    def test_per_tag_refuses_a_blank_tag(self) -> None:
        with pytest.raises(InvalidCeiling, match="must name the tag"):
            BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=10, tag="   ")

    @pytest.mark.parametrize("scope", [CeilingScope.PER_RUN, CeilingScope.PER_DAY])
    def test_other_scopes_refuse_a_tag(self, scope: CeilingScope) -> None:
        with pytest.raises(InvalidCeiling, match="must not carry a tag"):
            BudgetCeiling(scope=scope, tokens=10, tag="tier:local_fast")

    def test_is_hashable_and_frozen(self) -> None:
        ceiling = BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10)
        assert {ceiling, BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10)} == {ceiling}
        with pytest.raises(AttributeError):
            ceiling.tokens = 20  # type: ignore[misc]

    def test_canonical_form(self) -> None:
        ceiling = BudgetCeiling(scope=CeilingScope.PER_TAG, money=usd("5.00"), tag="tier:remote")
        assert ceiling.as_canonical() == {
            "scope": "per_tag",
            "money": {"currency": "USD", "nanos": 5_000_000_000},
            "tokens": None,
            "tag": "tier:remote",
            "partial_pricing": "floor",
        }

    def test_partial_pricing_defaults_to_floor(self) -> None:
        assert BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10).partial_pricing is (
            PartialPricing.FLOOR
        )

    def test_partial_pricing_is_keyword_only(self) -> None:
        # A fifth positional argument is refused: the rule a ceiling binds under must be named.
        with pytest.raises(TypeError):
            BudgetCeiling(CeilingScope.PER_RUN, usd("5.00"), None, None, PartialPricing.STRICT)  # type: ignore[misc]

    def test_strict_is_carried_in_the_canonical_form(self) -> None:
        # An approval record must show which rule its verdict was judged under.
        ceiling = BudgetCeiling(
            scope=CeilingScope.PER_RUN, money=usd("5.00"), partial_pricing=PartialPricing.STRICT
        )
        assert ceiling.as_canonical()["partial_pricing"] == "strict"

    def test_strict_requires_a_money_bound(self) -> None:
        with pytest.raises(InvalidCeiling, match="must bind money"):
            BudgetCeiling(
                scope=CeilingScope.PER_RUN, tokens=10, partial_pricing=PartialPricing.STRICT
            )

    def test_partial_pricing_must_be_the_enum(self) -> None:
        with pytest.raises(InvalidCeiling, match="must be a PartialPricing"):
            BudgetCeiling(
                scope=CeilingScope.PER_RUN,
                money=usd("5.00"),
                partial_pricing="strict",  # type: ignore[arg-type]
            )


class TestDebit:
    def test_records_what_it_was_given(self) -> None:
        usage = TokenUsage(input_tokens=10, output_tokens=5)
        debit = Debit(run_id="r", source_ref="turn-1", usage=usage, cost=None)
        assert (debit.run_id, debit.source_ref, debit.usage, debit.cost) == (
            "r",
            "turn-1",
            usage,
            None,
        )
        assert debit.tags == ()
        assert debit.occurred_at is None

    @pytest.mark.parametrize("field_name", ["run_id", "source_ref"])
    def test_refuses_a_blank_identifier(self, field_name: str) -> None:
        kwargs: dict[str, object] = {"run_id": "r", "source_ref": "s"}
        kwargs[field_name] = "  "
        with pytest.raises(ValueError, match=f"Debit.{field_name}"):
            Debit(usage=TokenUsage(), cost=None, **kwargs)  # type: ignore[arg-type]

    def test_refuses_usage_that_is_not_token_usage(self) -> None:
        with pytest.raises(ValueError, match="must be a TokenUsage"):
            Debit(run_id="r", source_ref="s", usage=object(), cost=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("tags", [["a"], ("",), (1,)])
    def test_refuses_malformed_tags(self, tags: object) -> None:
        with pytest.raises(ValueError, match="tuple of non-blank strings"):
            Debit(run_id="r", source_ref="s", usage=TokenUsage(), cost=None, tags=tags)  # type: ignore[arg-type]

    def test_refuses_a_naive_instant(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Debit(
                run_id="r",
                source_ref="s",
                usage=TokenUsage(),
                cost=None,
                occurred_at=datetime(2026, 9, 2, 12, 0),  # noqa: DTZ001 — the point of the test
            )

    def test_accepts_a_non_utc_instant(self) -> None:
        # Any offset is fine; it is normalized when a window is resolved, not here.
        when = datetime(2026, 9, 2, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert (
            Debit(
                run_id="r", source_ref="s", usage=TokenUsage(), cost=None, occurred_at=when
            ).occurred_at
            == when
        )

    def test_canonical_form_names_counts_and_never_money(self) -> None:
        usage = TokenUsage(input_tokens=1_000, output_tokens=200, cache_write_tokens=0)
        debit = Debit(
            run_id="traj-1",
            source_ref="turn-3",
            usage=usage,
            cost=cost_of(usage),
            tags=("tier:remote",),
            occurred_at=MIDDAY,
        )
        canonical = debit.as_canonical()
        assert canonical == {
            "run_id": "traj-1",
            "source_ref": "turn-3",
            "usage": {
                "input": 1_000,
                "output": 200,
                "cache_write": 0,
                "cache_read": "unsupported",
            },
            "tags": ["tier:remote"],
            "occurred_at": "2026-09-02T12:00:00.000Z",
        }
        # ADR-0030 rule 1: usage and a pricing hash are the stored facts, never a money figure.
        assert "cost" not in canonical
        assert "nanos" not in canonical_json(canonical)

    def test_canonical_form_refuses_an_unresolved_instant(self) -> None:
        debit = Debit(run_id="r", source_ref="s", usage=TokenUsage(), cost=None)
        with pytest.raises(ValueError, match="resolved occurred_at"):
            debit.as_canonical()

    def test_unsupported_counts_survive_into_the_canonical_form(self) -> None:
        debit = Debit(
            run_id="r",
            source_ref="s",
            usage=TokenUsage(),
            cost=None,
            occurred_at=MIDDAY,
        )
        assert set(debit.as_canonical()["usage"].values()) == {"unsupported"}
        assert TokenUsage().total_tokens is UNSUPPORTED


class TestCeilingVerdict:
    def test_canonical_form_carries_all_three_honesty_counts(self) -> None:
        ceiling = BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"), tokens=100)
        verdict = CeilingVerdict(
            ceiling=ceiling,
            exceeded=False,
            money_spent=usd("1.25"),
            money_remaining=usd("3.75"),
            tokens_spent=40,
            tokens_remaining=60,
            unpriced_debit_count=2,
            untotalled_debit_count=1,
            unmetered_debit_count=1,
        )
        assert verdict.as_canonical() == {
            "ceiling": ceiling.as_canonical(),
            "exceeded": False,
            "money_spent": {"currency": "USD", "nanos": 1_250_000_000},
            "money_remaining": {"currency": "USD", "nanos": 3_750_000_000},
            "tokens_spent": 40,
            "tokens_remaining": 60,
            "unpriced_debit_count": 2,
            "untotalled_debit_count": 1,
            "unmetered_debit_count": 1,
        }

    def test_a_zero_money_spent_serializes_as_a_figure_not_as_absent(self) -> None:
        # `if self.money_spent` would erase a genuine zero here if Money ever grew a __bool__.
        ceiling = BudgetCeiling(scope=CeilingScope.PER_RUN, money=usd("5.00"))
        verdict = CeilingVerdict(
            ceiling=ceiling,
            exceeded=False,
            money_spent=usd("0"),
            money_remaining=usd("5.00"),
            tokens_spent=0,
            tokens_remaining=None,
        )
        assert verdict.as_canonical()["money_spent"] == {"currency": "USD", "nanos": 0}
