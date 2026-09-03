"""Shared fixtures: a hand-driven clock, the price records, and two real databases.

Nothing here reads the system clock or the network. Every instant in the suite's tests comes from
:class:`ManualClock`, because the behaviour most worth testing in this package — which UTC day a
debit lands in — is invisible against a clock that moves on its own.

The second half is the database harness the integration tests share. It lives in this file rather
than in ``tests/integration/conftest.py`` deliberately: with no ``__init__.py`` under ``tests/``,
two files named ``conftest`` put two different modules on ``sys.path`` under one name, and
``from conftest import MIDDAY`` then resolves to whichever pytest inserted last. One conftest, one
name.

WeightsDB ships exactly these helpers (``weightsdb.testing.temporary_postgres``,
``MigrationHarness``) and this package may not import them: ADR-0050 decision 4 forbids the sibling
import and ``.importlinter`` asserts it, for the substantive reason that a budget accumulator must
not drag an engine, a migration runner and a backup implementation into its dependency footprint.
The pattern is copied by hand instead, which is what the ADR intends.

Every integration test runs on **both** dialects (ADR-0006: two, both first-class). PostgreSQL
needs a reachable server: locally there is none, so those legs skip with a reason that names the
URL, and pytest's ``-ra`` summary — on by default in this repository's ``addopts`` — prints every
one of them. Setting ``LOADLEDGER_REQUIRE_POSTGRES=1`` turns the skip into a failure, which is what
CI's ``db-matrix`` job does: a silently skipped dialect is an untested dialect, and the
both-dialects promise is only as good as its enforcement.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
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
from sqlalchemy.orm import sessionmaker

from loadledger.sql import mount_ledger_tables

if TYPE_CHECKING:
    from sqlalchemy import Engine, MetaData
    from sqlalchemy.orm import Session

    from loadledger.sql import LedgerTables

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


DEFAULT_POSTGRES_URL = "postgresql+psycopg://loadledger:loadledger@localhost:5432/loadledger_test"
"""Where the PostgreSQL legs look for a server, unless ``LOADLEDGER_POSTGRES_URL`` says otherwise.

CI's ``db-matrix`` job starts a service with exactly these credentials and sets the variable
anyway, so the job states which server it is testing rather than inheriting a default.
"""


def postgres_url() -> str:
    """Return a reset, empty PostgreSQL database's URL, or skip the test that asked for one.

    Resets by dropping and recreating the ``public`` schema rather than assuming a pristine
    database: the server is reused across tests, and a previous test's ``alembic_version`` table
    would otherwise make the next autogenerate a no-op.

    Raises:
        Failed: If ``LOADLEDGER_REQUIRE_POSTGRES=1`` and no server is reachable.
    """
    url = os.environ.get("LOADLEDGER_POSTGRES_URL", DEFAULT_POSTGRES_URL)
    required = os.environ.get("LOADLEDGER_REQUIRE_POSTGRES") == "1"
    try:
        probe = sa.create_engine(url)
        try:
            with probe.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
        finally:
            probe.dispose()
    except Exception as exc:  # noqa: BLE001 — any failure means "no usable server", by design
        if required:
            pytest.fail(f"LOADLEDGER_REQUIRE_POSTGRES=1 but {url} is unreachable: {exc}")
        pytest.skip(f"POSTGRESQL LEG SKIPPED — no server at {url}: {exc}")
    reset = sa.create_engine(url)
    try:
        with reset.begin() as connection:
            connection.execute(sa.text("DROP SCHEMA public CASCADE"))
            connection.execute(sa.text("CREATE SCHEMA public"))
    finally:
        reset.dispose()
    return url


@pytest.fixture(params=["sqlite", "postgresql"])
def database_url(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> str:
    """Yield one URL per supported dialect, so every test using it runs on both.

    SQLite is a real **file**, never ``:memory:``: an in-memory database has no journal, survives
    no process boundary and would silently exempt the atomicity tests from the thing they claim to
    prove.
    """
    if request.param == "sqlite":
        directory = tmp_path_factory.mktemp("loadledger-sqlite")
        return f"sqlite:///{directory / 'ledger.sqlite3'}"
    return postgres_url()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """An engine on a fresh, empty database of one dialect, disposed on exit."""
    made = sa.create_engine(database_url)
    try:
        yield made
    finally:
        made.dispose()


def mounted(engine: Engine, *, prefix: str = "ledger_") -> tuple[MetaData, LedgerTables]:
    """Mount the ledger tables into a throwaway host metadata and create them.

    ``create_all`` lives here, in a test, and never in ``src/`` — the package ships shapes, not a
    database (ADR-0050 decision 5). The host in ``tests/integration/hostapp`` does it properly,
    through Alembic; this is the shortcut for tests about the ledger rather than about mounting.
    """
    metadata = sa.MetaData()
    tables = mount_ledger_tables(metadata, prefix=prefix)
    metadata.create_all(engine, tables=list(tables.all_tables))
    return metadata, tables


def session_factory_for(engine: Engine) -> sessionmaker[Session]:
    """Return the callable ``SqlLedger`` takes: a factory of sessions it may own."""
    return sessionmaker(bind=engine)
