# Changelog

All notable changes to `loadledger` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
- `Ledger.balances(*, scope, window_key)` on the protocol, `InMemoryLedger` and `SqlLedger`, with
  the `WindowBalance` value object it returns: what one scope window has accumulated, **naming no
  run and reading through no ceiling**. Consults no ceiling at all, so a ledger built with none
  still answers — which is the point, because the consumer this exists for (`--scope tier` in
  PromptCadence) caps nothing per tier and still has spend to show. The alternatives it retires
  are summing `entries()` in the application (ledger arithmetic in a consumer, which ADR-0050's
  mount exists to prevent) and configuring a ceiling nobody intends to enforce purely to read a
  number through (a fabricated cap in the record).
- `Ledger.position()` on all three: `remaining` for **no particular run** — every configured
  ceiling, `PER_DAY` resolved at the injected clock's UTC day. This is the half that actually
  retires the reference-run workaround `F1_HANDOFF.md` §7 recorded, because `balances` alone fixes
  a scope with no ceiling while a scope *with* one needs headroom, and deriving headroom outside
  the package would put the floor rule and the `exceeded` decision in a consumer. An empty ledger
  reports the configured caps with nothing spent — true as a fact, where the previous answer was
  identical but reached through an `UnknownRun` fallback.
- `WindowBalance` carries all three honesty counts, and they are the same numbers a
  `CeilingVerdict` over the same window reports — asserted on both implementations, because a
  dashboard reading one beside an approval reading the other must not see two truths about one
  window. Its money is a **tuple, one figure per currency, ascending by code and never summed
  across them** (ADR-0030 rule 3): a window's currency set is open, so one total would be a
  conversion. An empty tuple means nothing at all has been priced here; an absent currency has had
  nothing priced in it, which is not zero (ADR-0016).
- Spec §15 budgets for both reads (≤ 2 ms each, measured ~0.2 ms and ~0.3 ms with 10 000 entries
  behind them), asserted in `tests/performance/`. Neither moves with the size of the history:
  both are primary-key lookups over `{prefix}balances` and `{prefix}balance_money` and neither
  touches `{prefix}entries`.

### Changed
- Spec §11 contract 6 now covers all three read paths rather than `would_exceed` alone, and states
  the proof: the session is rolled back, and a window with no row is read as an empty balance
  rather than inserted as a zero — asserted by row count.

### Notes
- **No table, column or index changed; hosts need no migration.** `{prefix}balances` is already
  keyed `(scope, window_key)` and `{prefix}balance_money` `(scope, window_key, currency)`, which is
  exactly the query these reads expose. Nothing is owed to
  `docs/mounted-table-upgrades.md`.
- `window_keys(scope)` was considered and **not** shipped. It is cheap on both dialects, but the
  consumer this release exists for knows its tier names from its own configuration and never asks;
  an unused public method in a 0.x package is surface that has to be kept and versioned.
- `position()` **refuses** a `PER_RUN` ceiling with `InvalidCeiling` rather than omitting it from
  the result. Omitting would silently shorten a tuple whose positional correspondence with
  `ceilings` is documented API; answering it against some arbitrary run would report one run's
  spend under a ledger-wide heading. A caller holding one ledger for every ceiling it knows about
  builds a second over the ledger-wide subset, which is free — `SqlLedger` caches nothing.
- `balances()` refuses a blank `window_key` with `ValueError`, on `declare_run`'s precedent: a
  blank key names a window nothing can land in, so an empty balance would look exactly like a real
  one.

## [0.1.0] — 2026-09-02

### Added
- Repository scaffold: toolchain copied from `py/WeightsDB` (hatchling, ruff, mypy strict,
  import-linter, pytest with `pytest-randomly`, hash-pinned `requirements/` locks, CI and release
  workflows), with the PostgreSQL matrix job dropped — this package touches no database.
- Phase 1, the pure core: `CeilingScope`, `BudgetCeiling`, `Debit`, `CeilingVerdict`,
  `LedgerEntry`, the `Ledger` protocol, `BalanceBook`, `InMemoryLedger`, `utc_day_start`,
  `utc_day_key`, and the `LedgerError` / `CurrencyMismatch` / `InvalidCeiling` / `UnknownRun`
  hierarchy. No I/O, no SQL, no logging, no environment reads.
- `CeilingVerdict.unpriced_debit_count` and `CeilingVerdict.unmetered_debit_count`, beyond the
  shape in spec §7: contract 2 requires the unpriced count to ride on the money verdict, and the
  same honesty is owed to the token balance when a provider leaves a token class unreported.
- `Ledger.declare_run`, beyond the protocol in spec §7: spec §13 defines a run as existing "once
  debited **or declared**", and §7 gave no way to declare one.
- `as_canonical()` on `BudgetCeiling`, `Debit`, `CeilingVerdict` and `LedgerEntry`, following
  BaseAiCore's own pattern — spec contract 4 requires byte-identical verdict serializations, and
  `baseaicore.canonical_json` needs a mapping form to produce them. `Debit.as_canonical` omits
  the cost deliberately: usage and `pricing_hash` are the stored facts (ADR-0030 rule 1).
- `PartialPricing` and the keyword-only `BudgetCeiling.partial_pricing` (default `FLOOR`;
  `STRICT` requires a money bound, else `InvalidCeiling`), and `CeilingVerdict.untotalled_debit_count`
  — the subset of the unpriced count that carried an estimate which did not total, which is what
  a strict ceiling fires on. Both appear in the canonical forms, so the goldens changed (ADR-0069).

### Changed
- A debit whose estimate did not total now accumulates the components that *were* priced into the
  money balance as a floor, instead of adding nothing (ADR-0069, reversing spec contract 2 as
  first written). On a floor, `exceeded` is certain when `True` and not when `False`; a `STRICT`
  ceiling treats an untotalled estimate in its window as exceeding, at pre-flight too. A debit
  with no estimate still touches no money balance, and never trips a strict ceiling.

- Phase 2, the durable half: `loadledger.sql` under the new `loadledger[sql]` extra (ADR-0050).
  - `mount_ledger_tables(metadata, *, prefix="ledger_") -> LedgerTables` adds four tables —
    `ledger_entries`, `ledger_balances`, `ledger_balance_money`, `ledger_runs` — to an
    application's own `MetaData`, so they appear in the application's own Alembic autogenerate and
    the application owns the rows, the backups and the retention. The package owns no engine, no
    session, no URL, no environment variable, no file and no migration history; `create_all`
    appears nowhere in `src/`, and nothing is created on import.
  - `SqlLedger(session_factory, ceilings, *, clock, table_prefix="ledger_")` implements the whole
    `Ledger` protocol, `declare_run` included, over one injected session factory. It evaluates
    through the same `BalanceBook` as `InMemoryLedger`, so the arithmetic and the honesty rules
    have one implementation and not one per backend.
  - `LedgerTables`, a frozen handle with `prefix`, `entries`, `balances`, `balance_money`, `runs`,
    `metadata` and `all_tables`.
  - `UnsupportedDialect` (`LEDGER_UNSUPPORTED_DIALECT`): ADR-0006 admits SQLite and PostgreSQL, and
    a third dialect is refused at the first statement rather than found as a syntax error inside a
    money transaction. Now in spec §7's error list and §13's table.
  - `loadledger.core` gains the seams a durable ledger needs, all documented: `DebitContribution`,
    `contribution_of`, `BalanceBook.windows_touched` / `window_for` / `seed`, `resolved_debit` and
    `is_unpriced`. The package's top-level `__all__` is unchanged.
- `docs/quickstart.md` and the standalone `docs/quickstart.py` it publishes the output of (spec §20
  acceptance criterion 2), with a test that runs the script so it cannot rot.
- `docs/mounted-table-upgrades.md`: the upgrade-note template and migration recipe LoadLedger ships
  when a mounted table changes shape, since the host owns every migration (spec §19, ADR-0050
  decision 5), with one worked example.
- CI gains a `db-matrix` job running the integration tests against PostgreSQL 16 with
  `LOADLEDGER_REQUIRE_POSTGRES=1`, so a dialect cannot be skipped into a green run.

### Changed
- `.importlinter`'s `no-sql-in-phase-1` is **replaced** by
  `only-the-sql-module-imports-sqlalchemy`: same forbidden modules, one ignored import for
  `loadledger.sql -> sqlalchemy`, and no exemption at all for `alembic`. The `no-sibling-packages`
  contract's `spotcheck` entry gains `commissioner`, the package's name since `7077cc4`; the old
  spelling is kept, because a forbidden module that no longer exists forbids nothing.
- `pytest` `addopts` gains `-ra`, so a skipped PostgreSQL leg always names itself in the summary.

### Specification
- Spec §7, §10, §11, §13 and §15 were amended to describe what Phase 2 built, and the amendments
  were accepted before release:
  - §7 gains `LedgerTables`'s field list and `UnsupportedDialect`, and states what a durable
    ledger's `entries()` returns.
  - §10 names the four mounted tables and their keys, says why money is a table rather than a
    column, and makes the `BigInteger` width part of the mounted contract.
  - §11 contract 1 states that a durable ledger does not persist the `CostEstimate` — the one
    place a consumer swapping `InMemoryLedger` for `SqlLedger` sees a difference.
  - §13 gains rows for the prefix `ValueError` and for `UnsupportedDialect`.
  - §15's single `entries` budget is split: for `SqlLedger` on SQLite, the query is ≤ 100 ms and
    full materialization ≤ 250 ms. `InMemoryLedger` keeps ≤ 100 ms. The old single figure was set
    before `SqlLedger` existed and was never about constructing ten thousand value objects.

### Performance, as measured
- `debit` with three ceilings ~1.5 ms (budget 5 ms) and flat as history grows — balances are
  maintained, not recomputed. `would_exceed` ~0.4 ms (budget 2 ms). `entries` over a 10 000-entry
  run: ~17 ms for the query (budget 100 ms), ~155 ms fully materialized (budget 250 ms). All
  inside the amended §15.
