# Mounted-table upgrades — the template, and one worked example

LoadLedger's tables live in **your** database and **your** Alembic history. So when a mounted table
changes shape, LoadLedger cannot migrate it for you, and will not try: a package that ran a
migration would own half of a history nobody could reason about, and auto-migration on import is
forbidden outright ([ADR-0050](packages/loadledger/spec.md) decision 5).

What LoadLedger ships instead is this: an **upgrade note** saying exactly what changed, and a
**migration recipe** you paste into a revision of your own. That is more work than an owned
migration would be, and it is the price of your application keeping one history, one backup, one
restore and one retention policy.

The shapes are versioned with the package ([spec §19](packages/loadledger/spec.md)). A shape change
is never a patch release.

---

## The template

Every upgrade note LoadLedger publishes has these seven sections, in this order. Copy this
structure verbatim; the value of a template is that the person reading it at 3 a.m. already knows
where the answer is.

````markdown
# Upgrade note — loadledger X.Y.Z

**Affects:** the mounted tables named below, under whatever prefix you mounted them with.
**Applies to hosts upgrading from:** <previous version range>
**Data loss risk:** none / recoverable / irreversible — say which, in the first line.
**Downtime:** none / a table rebuild on SQLite / a lock on PostgreSQL — say which.

## What changed

One paragraph. The column or index, its old shape, its new shape, and the behaviour that made the
change necessary. No rationale beyond one sentence; the ADR or changelog entry carries that.

## What breaks if you do nothing

The concrete symptom, in the words it will appear in — an exception type, a wrong figure, a query
that silently returns fewer rows. A host that has not migrated must be able to recognise itself
here without reading the diff.

## Before you start

* The package version that introduces the shape, and the version of your own application that
  must ship with it. If the new code cannot run against the old shape, say so here in bold.
* Whether the new code tolerates the old shape (a *read-compatible* change) or does not (a
  *breaking* change). This decides whether you can deploy code and migration separately.
* Anything to check first — a row count, a disk-space headroom, a backup.

## The recipe

A complete Alembic revision body, `upgrade()` and `downgrade()`, that runs unchanged on **both**
SQLite and PostgreSQL. Use `op.batch_alter_table` for anything SQLite cannot do in place, which is
most alterations. Never reference `loadledger` from inside a revision: a migration must keep
working after the package is upgraded again.

## After you migrate

How to verify — a query whose result you can state in advance, and what the wrong answer looks
like.

## If you are not ready

What a host that cannot migrate now should pin, and until when.
````

Two rules that are not sections:

* **The prefix is yours.** Every recipe LoadLedger publishes writes `ledger_` because that is the
  documented default. If you mounted with another prefix, the recipe's table names are wrong for
  you and only you can fix them — search-and-replace before you run anything.
* **Recipes are self-contained.** A revision that imports `loadledger` breaks the day the package
  changes again, because Alembic replays old revisions against new code. Write the columns out.

---

## Worked example

This is a hypothetical `0.2.0` change, written out in full so the template has a shape to point at.
It is deliberately the *hardest* common case — a new `NOT NULL` column on a populated table — which
is the one where SQLite and PostgreSQL diverge and where a recipe earns its keep.

````markdown
# Upgrade note — loadledger 0.2.0

**Affects:** `ledger_entries`.
**Applies to hosts upgrading from:** loadledger 0.1.x.
**Data loss risk:** none. The change is additive and the downgrade is exact.
**Downtime:** a table rebuild on SQLite (fast; the table is rewritten once), a brief
`ACCESS EXCLUSIVE` lock on PostgreSQL.

## What changed

`ledger_entries` gains `entry_kind`, a non-null string naming what produced the debit — `"debit"`
for every row written by 0.1.x, and `"adjustment"` for the corrections `Ledger.adjust()` writes
from 0.2.0. Without a discriminator, a corrected balance and the debit it corrects are the same
shape, and a re-costing pass cannot tell them apart.

## What breaks if you do nothing

`SqlLedger` 0.2.0 against an un-migrated table raises `sqlalchemy.exc.OperationalError: no such
column: ledger_entries.entry_kind` on the first `debit()` — immediately, not subtly. There is no
window in which figures are quietly wrong.

## Before you start

* Ship this migration **with or before** the code: **loadledger 0.2.0 cannot run against the 0.1.x
  shape.** The reverse is fine — 0.1.x ignores the new column, so you may migrate first and deploy
  after.
* Check your row count. The rebuild rewrites `ledger_entries` once on SQLite:
  `SELECT count(*) FROM ledger_entries`.
* Take your ordinary backup. Nothing here needs a special one.

## The recipe

Generate an empty revision in your own history (`alembic revision -m "loadledger 0.2.0 entry_kind"`)
and paste this body. Replace `ledger_` throughout if you mounted with another prefix.

```python
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    # 1. Add it nullable, so the existing rows are legal while the backfill runs.
    op.add_column("ledger_entries", sa.Column("entry_kind", sa.String(), nullable=True))
    # 2. Backfill. Every row that exists predates adjustments, so all of them are debits.
    op.execute("UPDATE ledger_entries SET entry_kind = 'debit' WHERE entry_kind IS NULL")
    # 3. Make it NOT NULL. `batch_alter_table` is what makes this one revision run on both
    #    dialects: SQLite cannot alter a column in place and needs the table rebuilt, and Alembic
    #    does that rebuild here; on PostgreSQL it compiles to a plain ALTER.
    with op.batch_alter_table("ledger_entries") as batch:
        batch.alter_column("entry_kind", existing_type=sa.String(), nullable=False)

def downgrade() -> None:
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_column("entry_kind")
```

## After you migrate

```sql
SELECT entry_kind, count(*) FROM ledger_entries GROUP BY entry_kind;
```

Every pre-existing row must be `debit` and none may be null. A null here means the backfill in step
2 ran against a different prefix than step 1 — the most likely mistake, and the reason the prefix
warning is above.

## If you are not ready

Pin `loadledger>=0.1,<0.2`. 0.1.x is supported for the life of the 0.2 line.
````

---

## Changes that need no note

For completeness, because knowing what *isn't* a migration is half of this:

* **A new `CeilingScope`.** The persisted balance key is `(scope, window_key)` and both are plain
  strings, so composite windows ([spec §21](packages/loadledger/spec.md)) need no schema change at
  all — a new scope simply starts filing rows under a new key.
* **A new field on a verdict.** Verdicts are stored as canonical JSON in a `TEXT` column, so a
  verdict written by a newer version reads back with its new field and one written by an older
  version reads back without it. Your table does not change.
* **An index you want and LoadLedger does not ship** — an indexed tag lookup, say. That is yours
  to add in your own revision and yours to keep; LoadLedger will not drop it, because
  autogenerate compares your metadata against your database and your index is in neither of
  LoadLedger's.
