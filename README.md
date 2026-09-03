# LoadLedger

Budget accumulation and ceilings over ADR-0030's cost types: debits, per-scope balances, and
explicable ceiling verdicts. No pricing, no conversion, no policy.

**Status:** **0.1.0.** Ceilings, debits, verdicts, `InMemoryLedger`, the mountable tables and
`SqlLedger` on SQLite and PostgreSQL.

Part of the **Local AI Suite**.

## This package ships tables, not a database

The thing a new caller gets wrong, so it is above the fold. `loadledger.sql` does **not** own a
database. It adds four tables to a `MetaData` **you** own, which then appear in **your**
`alembic revision --autogenerate` beside the tables you wrote yourself, upgrade with **your**
history, and are backed up, restored and pruned by whoever owns that database:

```python
# your_app/models.py — at module import, beside your own tables
import sqlalchemy as sa
from loadledger.sql import mount_ledger_tables

metadata = sa.MetaData()
# ... your own tables ...
ledger_tables = mount_ledger_tables(metadata, prefix="ledger_")
```

LoadLedger holds no engine, opens no connection, reads no URL or environment variable, and runs no
migration — `create_all` appears nowhere in `src/`. Sessions arrive by injection. Two applications
mounting these tables have **two tables in two databases — never one** (ADR-0050).

**Mount eagerly, at import.** Autogenerate only sees what was mounted before the metadata was
inspected, so a host that mounts lazily gets a migration that silently *drops* the ledger. There is
a test in this repository that does exactly that and watches it happen.

## Install

```bash
pip install loadledger          # the pure core: ceilings, verdicts, InMemoryLedger
pip install "loadledger[sql]"   # adds SQLAlchemy, mount_ledger_tables and SqlLedger
```

The core resolves to `baseaicore` and nothing else — a consumer that only wants to add up tokens
does not acquire an ORM to do it.

See [docs/quickstart.md](docs/quickstart.md) for a runnable end-to-end script, and
[docs/mounted-table-upgrades.md](docs/mounted-table-upgrades.md) for what to do when a mounted
table changes shape.

## What it does, and what it refuses

BaseAiCore already has every primitive a budget needs — `Money`, `TokenUsage`, `ModelPricing`,
`CostEstimate`, `estimate_cost`. What nothing did was add them up across turns and say "stop".
LoadLedger is that accumulator, and it is deliberately nothing else:

* **It never prices.** Prices arrive as `ModelPricing` records the caller acquired; LoadLedger
  applies `baseaicore.estimate_cost` and invents no rate.
* **It never converts.** A USD ceiling and a EUR debit raise `CurrencyMismatch` (ADR-0030 rule 3).
* **It never decides.** It answers "would this exceed?" and "what remains?"; halting, pausing or
  re-approving is the caller's policy. Exceeding a ceiling is recorded, not raised.
* **It never stores money as the record of truth.** An entry holds `TokenUsage` and a
  `pricing_hash`; the money is re-derived, so a price correction has somewhere to go
  (ADR-0030 rule 1).
* **Unpriced is not free, and a partial price is a floor.** A debit with no estimate accumulates
  tokens and leaves every money balance untouched. A debit whose estimate did not total — the
  ordinary case for an adapter that leaves the cache classes unreported — adds the components that
  *were* priced and nothing for the rest. Every money verdict carries the counts that make its
  figure a floor, so "under budget" is never claimed over an incomplete sum without saying so
  (ADR-0016, ADR-0069). Render a floor as "at least", never as a bare figure.
* **The operator chooses how a floor binds.** By default a ceiling binds on the floor: `exceeded`
  is certain when `True` and not when `False`, so the brake may fire late by the unreported
  portion. `BudgetCeiling(..., partial_pricing=PartialPricing.STRICT)` reverses that for a hard
  budget — an estimate in the window that did not total counts as exceeding, at pre-flight too, and
  the cap is never crossed. Strict trips on an estimate that did not total, never on a local debit
  that carried no estimate.

## Quickstart

```python
from datetime import UTC, datetime

from baseaicore import Money, TokenUsage, utc_now
from loadledger import BudgetCeiling, CeilingScope, Debit, InMemoryLedger

ledger = InMemoryLedger(
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

# Ask before spending — side-effect-free, safe on an approval path at any frequency.
ledger.declare_run("traj-1")
if any(v.exceeded for v in ledger.would_exceed("traj-1", usage=TokenUsage(input_tokens=900_000))):
    ...  # your policy decides what happens here

entry = ledger.debit(
    Debit(
        run_id="traj-1",
        source_ref="turn-1",
        usage=TokenUsage(input_tokens=900_000, output_tokens=12_000),
        cost=None,  # a local model has no token price: unpriced, not free
    )
)
verdict = entry.verdicts[0]
print(verdict.tokens_spent, verdict.money_spent, verdict.unpriced_debit_count)
# 912000 None 1     -- '—', not '$0.00'
```

For a budget that must not be crossed, make the money ceiling strict:

```python
from loadledger import PartialPricing

hard = BudgetCeiling(
    scope=CeilingScope.PER_RUN,
    money=Money.from_decimal("USD", "5.00"),
    partial_pricing=PartialPricing.STRICT,  # a response the provider did not fully price
)  # counts as exceeding — never fires late
```

`PER_DAY` means a **UTC** calendar day. A budget that reset at the machine's local midnight would
be a different budget on every machine.

## Durability, and what is promised about concurrent writers

`SqlLedger` is `InMemoryLedger` with a different store: both evaluate through one `BalanceBook`, so
a consumer that tested against the in-memory double tested the arithmetic it will run in
production. A debit, the balances it moves and the verdicts it reports are **one transaction** —
a crash cannot leave spend on the books with no verdict beside it, and the test suite proves it by
killing a process mid-debit under both SQLite journal modes.

**No balance is ever read into Python, added to, and written back.** Each balance moves by a single
`INSERT … ON CONFLICT DO UPDATE SET x = x + excluded.x`, which both supported dialects execute
atomically, so two processes debiting one window add rather than overwrite. Writes are issued before
reads, and rows are locked in sorted key order, so concurrent debits cannot deadlock.

**Serializable isolation is not claimed.** Two debits committing at the same moment may each report
a verdict that omits the other's spend, so a cap can be crossed by at most the concurrent in-flight
debits before the next verdict sees it. Both totals are recorded exactly and the next verdict is
correct. A caller that must not cross a cap under concurrency serializes its own approvals — which
is what PromptCadence's plan gate does.

One thing a stored entry does not carry: `entry.debit.cost` reads back as `None`. A `CostEstimate`
is a derived figure, and the stored facts are the `TokenUsage` and the `pricing_hash` (ADR-0030
rule 1). Every verdict is stored whole, with the money it was decided on, so nothing about what the
ledger *decided* is lost.

## Documentation

Project documentation lives under [`docs/`](docs/README.md).

| Read this | For |
|---|---|
| [docs/packages/loadledger/spec.md](docs/packages/loadledger/spec.md) | Purpose, scope, non-goals, public contracts, acceptance criteria |
| [docs/packages/loadledger/development-plan.md](docs/packages/loadledger/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |
| [docs/quickstart.md](docs/quickstart.md) | A runnable script: mount, debit priced and unpriced usage, read honest balances |
| [docs/mounted-table-upgrades.md](docs/mounted-table-upgrades.md) | The upgrade-note template and recipe for a mounted-table change |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
