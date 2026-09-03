# LoadLedger — Quickstart

LoadLedger adds two things to what BaseAiCore already has, and deliberately nothing else: it
**adds spend up across a multi-step run**, and it **says when a ceiling is crossed**. No pricing,
no currency conversion, no policy.

The durable half of it — `loadledger.sql` — **ships tables, not a database.** You mount them into
your own `MetaData`, they appear in your own `alembic revision --autogenerate` beside the tables
you wrote yourself, and you own the rows, the backups and the retention. LoadLedger holds no
engine, opens no connection, reads no URL and runs no migration
([ADR-0050](packages/loadledger/spec.md)).

## Install

```bash
pip install loadledger          # the pure core: InMemoryLedger, ceilings, verdicts
pip install "loadledger[sql]"   # adds SQLAlchemy, mount_ledger_tables and SqlLedger
```

The core install pulls in `baseaicore` and nothing else — that is the point of the extra. A
consumer that only wants to add up tokens does not acquire an ORM to do it.

LoadLedger ships a PEP 561 `py.typed` marker, so `mypy --strict` in your own project reads its
annotations from the installed wheel.

## Run it

[`quickstart.py`](quickstart.py) in this directory is a standalone script that mounts the tables
into a SQLite file it creates, debits priced and unpriced usage, and prints honest balances. It
needs nothing but `loadledger[sql]`: no server, no framework, no configuration file.

```bash
pip install "loadledger[sql]"
python docs/quickstart.py
```

Its real output:

```text
mounted: ledger_entries, ledger_balances, ledger_balance_money, ledger_runs

after one local step (no price list applies):
  per_run  spent —                      tokens 912,000   exceeded=False
  per_day  spent —                      tokens 912,000   exceeded=False

remote estimate total: UNSUPPORTED  (input 0.6 USD, output 0.12 USD)

after one partly-priced remote step:
  per_run  spent at least 0.72 USD      tokens 1,120,000 exceeded=False
  per_day  spent at least 0.72 USD      tokens 1,120,000 exceeded=False

after a fully-priced remote step:
  per_run  spent at least 1.095 USD     tokens 1,225,000 exceeded=False
  per_day  spent at least 1.095 USD     tokens 1,225,000 exceeded=False

pre-flight for a 1.5M-token step — would_exceed writes nothing:
  per_run  exceeded=True
  per_day  exceeded=False

history (usage and pricing hash are the stored facts; the money is re-derived):
  turn-1  unpriced=True  pricing_hash=—
  turn-2  unpriced=True  pricing_hash=85b1ec5958b1
  turn-3  unpriced=False pricing_hash=85b1ec5958b1
```

Three things in that output are the package's whole argument, so they are worth reading slowly.

* **`—`, not `$0.00`.** After a local step, `money_spent` is `None`. Nothing has been *priced* in
  that window, which is a different fact from nothing having been *spent* — a local model is
  unpriced, not free ([ADR-0016](packages/loadledger/spec.md)). Render it as an em dash and your
  operator learns something true; render it as zero and they learn something false.
* **"at least", not a bare figure.** The remote estimate's `total` came back `UNSUPPORTED` because
  the provider reported input and output tokens and said nothing about the cache classes — the
  ordinary case, not an edge one. LoadLedger accumulates the components that *were* priced, so the
  money balance is a **floor**, and `unpriced_debit_count` on the same verdict says so. `exceeded`
  is then certain when `True` and not certain when `False`. If your budget must never be crossed,
  set `partial_pricing=PartialPricing.STRICT` and an estimate that did not total counts as
  exceeding.
* **The stored facts are usage and a hash.** No money figure is the record of truth. When a price
  list is corrected, you re-cost the history from `entries()` and every stored row stays exactly
  as it was ([ADR-0030](packages/loadledger/spec.md) rule 1).

## Mounting into your own application

The three lines that matter:

```python
# your_app/models.py — at module import, beside your own tables
import sqlalchemy as sa
from loadledger.sql import mount_ledger_tables

metadata = sa.MetaData()
# ... your own tables ...
ledger_tables = mount_ledger_tables(metadata, prefix="ledger_")
```

Then point your Alembic `env.py` at that same `metadata` as `target_metadata`, and
`alembic revision --autogenerate` picks the four tables up like any table you wrote.

**Mount eagerly, at import.** Autogenerate only sees what was mounted before the metadata was
inspected. A host that mounts lazily — inside a request handler, or behind a feature flag — gets a
migration that silently **drops** the ledger. That is the named failure mode of this pattern, and
this repository has a test (`tests/integration/test_hostapp.py`) that autogenerates against an
unmounted metadata and watches Alembic write `op.drop_table` for all four tables, so the hazard is
a fact rather than a warning.

Two applications mounting these tables have **two tables in two databases — never one**. No
application reads another's database.

## Using it

```python
from loadledger.sql import SqlLedger

ledger = SqlLedger(session_factory, ceilings, clock=utc_now, table_prefix="ledger_")
```

`session_factory` is a callable returning a SQLAlchemy 2.0 `Session` **this ledger may own**: it
commits it and closes it. If you need a debit inside your own unit of work, hand it a factory that
joins your transaction as a savepoint:

```python
session_factory = lambda: Session(bind=connection, join_transaction_mode="create_savepoint")
```

`SqlLedger` is stateless and cheap to construct, so resolve your ceiling set **per operation** —
the configured defaults, this run's own budget, this project's cap — and build a view with it. That
is sound because the persisted balance key is `(scope, window_key)` and knows nothing about
ceilings: a ceiling you add tomorrow binds on the whole history, not on the history since you
configured it.

### What is stored, and what is not

| | |
|---|---|
| `ledger_entries` | One row per debit: the canonical debit record, the `unpriced` flag, the `pricing_hash`, and every verdict, whole |
| `ledger_balances` | One row per `(scope, window_key)`: tokens and the three honesty counts, maintained incrementally |
| `ledger_balance_money` | One row per `(scope, window_key, currency)`: nanos. A currency with no row has had nothing priced in it, which is not zero |
| `ledger_runs` | One row per run, so a run you declared and never spent against survives a restart |

An entry read back through `entries()` has `debit.cost is None`, whatever it was when you recorded
it: a `CostEstimate` is a derived figure and is not a stored fact. Nothing about what the ledger
*decided* is lost — every verdict is stored with the money it was decided on. Read
`entry.unpriced` and `entry.pricing_hash` for the pricing facts, and re-derive the money from
`entry.debit.usage` and your price catalogue.

### Concurrency

No balance is ever read into Python, added to, and written back. Each balance moves by a single
`INSERT … ON CONFLICT DO UPDATE SET x = x + excluded.x`, which both supported dialects execute
atomically, so two processes debiting one window **add** rather than overwrite. Writes are issued
before reads, and rows are locked in sorted key order so concurrent debits cannot deadlock.

Serializable isolation is *not* claimed: two debits committing at the same moment may each report
a verdict that omits the other's spend, so a cap can be crossed by at most the concurrent in-flight
debits before the next verdict sees it. Both totals are recorded exactly. If you must not cross a
cap under concurrency, serialize your own approvals.

On SQLite the engine is yours, and two settings are yours to make: a `busy_timeout` long enough
for your write concurrency (pysqlite's five-second default is usually enough), and a journal mode.
WAL lets readers run during a write and suits a ledger a UI polls. The atomicity contract holds
under WAL and under the default rollback journal, and the test suite kills a process mid-debit
under each.

## When a mounted table changes

You own the migration; LoadLedger ships the recipe. See
[mounted-table-upgrades.md](mounted-table-upgrades.md).

## Where to go next

| Read this | For |
|---|---|
| [Specification](packages/loadledger/spec.md) | Purpose, scope, non-goals, public contracts, acceptance criteria |
| [Development plan](packages/loadledger/development-plan.md) | The phased build plan |
| [Mounted-table upgrades](mounted-table-upgrades.md) | What to do when a column in a mounted table changes |
