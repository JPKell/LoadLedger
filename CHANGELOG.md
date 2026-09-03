# Changelog

All notable changes to `loadledger` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

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
    money transaction. Beyond spec §7's error table; amendment proposed in `C3_HANDOFF.md`.
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

### Known limitation
- `entries()` for a 10 000-entry run materializes in ~155 ms against spec §15's 100 ms. The query
  itself takes ~17 ms; the overshoot is constructing ten thousand validated value objects, and no
  indexing changes it. A split of that §15 row — query ≤ 100 ms, full materialization ≤ 250 ms — is
  proposed in `C3_HANDOFF.md`. `debit` (~1.5 ms against 5 ms) and `would_exceed` (~0.4 ms against
  2 ms) are inside budget, and `debit` does not slow down as history grows.
