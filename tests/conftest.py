"""Shared fixtures: a hand-driven clock and the price records the cost tests are built on.

Nothing here reads the system clock or the network. Every instant in the suite's tests comes from
:class:`ManualClock`, because the behaviour most worth testing in this package — which UTC day a
debit lands in — is invisible against a clock that moves on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from baseaicore import (
    UNSUPPORTED,
    CostEstimate,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    TokenUsage,
    Unsupported,
    estimate_cost,
)

MIDDAY = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
"""Well inside a UTC day, so a test that cares about a boundary has to ask for one."""

DAY = timedelta(days=1)


class ManualClock:
    """A clock the test moves by hand, satisfying :data:`baseaicore.Clock`."""

    def __init__(self, start: datetime = MIDDAY) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, when: datetime) -> None:
        self._now = when


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def rates(
    currency: str = "USD",
    *,
    input_per_million: str | None = "3.00",
    output_per_million: str | None = "15.00",
    cache_write_per_million: str | None = "3.75",
    cache_read_per_million: str | None = "0.30",
) -> TokenRates:
    """Build a price list; ``None`` for a class means the list states no rate for it."""

    def rate(value: str | None) -> Money | Unsupported:
        return UNSUPPORTED if value is None else Money.from_decimal(currency, Decimal(value))

    return TokenRates(
        currency=currency,
        input_per_million_tokens=rate(input_per_million),
        output_per_million_tokens=rate(output_per_million),
        cache_write_per_million_tokens=rate(cache_write_per_million),
        cache_read_per_million_tokens=rate(cache_read_per_million),
    )


def pricing(
    token_rates: TokenRates | None = None,
    *,
    model_name: str = "remote-fake-1",
    source: PricingSource = PricingSource.PROVIDER_PUBLISHED,
) -> ModelPricing:
    """Build one price observation, with no validity window so it applies at every instant."""
    return ModelPricing(
        identity=ModelIdentity(ProviderKind.OPENAI_COMPATIBLE, model_name),
        rates=token_rates if token_rates is not None else rates(),
        source=source,
        observed_at=MIDDAY,
    )


def cost_of(
    usage: TokenUsage,
    *,
    price: ModelPricing | None = None,
    at: datetime = MIDDAY,
) -> CostEstimate:
    """Cost one call's usage against a price observation, at an explicit instant."""
    return estimate_cost(usage, price if price is not None else pricing(), at=at)
