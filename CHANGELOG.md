# Changelog

All notable changes to `loadledger` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

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

### Deferred
- `loadledger.sql`, `mount_ledger_tables`, `SqlLedger` and the `[sql]` extra — Phase 2 (ADR-0050).
