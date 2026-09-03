"""A standalone LoadLedger budget over a SQLite file this script owns.

Needs nothing but `pip install "loadledger[sql]"`. No server, no framework, no configuration file.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from baseaicore import (
    UNSUPPORTED,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    TokenUsage,
    estimate_cost,
    utc_now,
)
from sqlalchemy.orm import sessionmaker

from loadledger import BudgetCeiling, CeilingScope, Debit
from loadledger.sql import SqlLedger, mount_ledger_tables

# ---------------------------------------------------------------- 1. your own database
database = Path("budget.sqlite3")
database.unlink(missing_ok=True)
engine = sa.create_engine(f"sqlite:///{database}")

# Your MetaData, your tables. In a real application these are mounted at module import,
# beside your own models, and migrated by your own Alembic history.
metadata = sa.MetaData()
tables = mount_ledger_tables(metadata)
metadata.create_all(engine, tables=list(tables.all_tables))
print("mounted:", ", ".join(table.name for table in tables.all_tables))

# ---------------------------------------------------------------- 2. your ceilings
ledger = SqlLedger(
    sessionmaker(bind=engine),
    [
        BudgetCeiling(
            scope=CeilingScope.PER_RUN,
            money=Money.from_decimal("USD", "5.00"),
            tokens=2_000_000,
        ),
        BudgetCeiling(scope=CeilingScope.PER_DAY, money=Money.from_decimal("USD", "25.00")),
    ],
    clock=utc_now,
)
ledger.declare_run("traj-1")


def report(label: str) -> None:
    """Print each ceiling's balance the way it must be shown to a person."""
    print(f"\n{label}")
    for verdict in ledger.remaining("traj-1"):
        if verdict.money_spent is None:
            # Nothing has been priced in this window. Not zero: nothing (ADR-0016).
            money = "—"
        elif verdict.unpriced_debit_count:
            # The sum is a floor: some spend in this window could not be fully priced.
            money = f"at least {verdict.money_spent}"
        else:
            money = str(verdict.money_spent)
        print(
            f"  {verdict.ceiling.scope.value:<8} "
            f"spent {money:<22} tokens {verdict.tokens_spent:<9,} "
            f"exceeded={verdict.exceeded}"
        )


# ---------------------------------------------------------------- 3. an unpriced local step
ledger.debit(
    Debit(
        run_id="traj-1",
        source_ref="turn-1",
        usage=TokenUsage(input_tokens=900_000, output_tokens=12_000),
        cost=None,  # a local model has no token price: unpriced, not free
    )
)
report("after one local step (no price list applies):")

# ---------------------------------------------------------------- 4. a priced remote step
rates = TokenRates(
    currency="USD",
    input_per_million_tokens=Money.from_decimal("USD", Decimal("3.00")),
    output_per_million_tokens=Money.from_decimal("USD", Decimal("15.00")),
    cache_write_per_million_tokens=Money.from_decimal("USD", Decimal("3.75")),
    cache_read_per_million_tokens=Money.from_decimal("USD", Decimal("0.30")),
)
price = ModelPricing(
    identity=ModelIdentity(ProviderKind.OPENAI_COMPATIBLE, "remote-model-1"),
    rates=rates,
    source=PricingSource.PROVIDER_PUBLISHED,
    observed_at=datetime(2026, 9, 1, tzinfo=UTC),
)

# The ordinary remote case: the provider reported input and output and said nothing about the
# cache classes, so the estimate prices what it can and refuses to claim a total.
partial_usage = TokenUsage(input_tokens=200_000, output_tokens=8_000)
partial = estimate_cost(partial_usage, price, at=utc_now())
print(
    f"\nremote estimate total: {partial.total!r}  (input {partial.input_cost}, "
    f"output {partial.output_cost})"
)
ledger.debit(Debit(run_id="traj-1", source_ref="turn-2", usage=partial_usage, cost=partial))
report("after one partly-priced remote step:")

# A provider that reports every class produces a real total.
complete_usage = TokenUsage(
    input_tokens=100_000, output_tokens=5_000, cache_write_tokens=0, cache_read_tokens=0
)
complete = estimate_cost(complete_usage, price, at=utc_now())
assert complete.total is not UNSUPPORTED
ledger.debit(Debit(run_id="traj-1", source_ref="turn-3", usage=complete_usage, cost=complete))
report("after a fully-priced remote step:")

# ---------------------------------------------------------------- 5. ask before spending
huge = TokenUsage(input_tokens=1_500_000, output_tokens=0)
print("\npre-flight for a 1.5M-token step — would_exceed writes nothing:")
for verdict in ledger.would_exceed("traj-1", usage=huge):
    print(f"  {verdict.ceiling.scope.value:<8} exceeded={verdict.exceeded}")

# ---------------------------------------------------------------- 6. the record
print("\nhistory (usage and pricing hash are the stored facts; the money is re-derived):")
for entry in ledger.entries(run_id="traj-1"):
    print(
        f"  {entry.debit.source_ref}  unpriced={str(entry.unpriced):<5} "
        f"pricing_hash={(entry.pricing_hash or '—')[:12]}"
    )

database.unlink(missing_ok=True)
