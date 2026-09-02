# LoadLedger — Development Plan

**Sequence position:** PromptCadence arc, stream P ([roadmap §4](../../roadmap/promptcadence-roadmap.md)).
Depends on `baseaicore>=0.4.1`; parallel with CutCtx and ToolYard.
**Target:** `loadledger 0.1.0` at the end of Phase 2.

The plan is deliberately small: LoadLedger's value is that it adds **nothing** to ADR-0030's cost
model except accumulation and ceilings, and a small package is the proof.

---

## Phase 1 — Core: ceilings, debits, verdicts, InMemoryLedger

**Goal:** the arithmetic and the honesty rules, pure and deterministic.

**Prerequisites:** the D-6 ADR accepted (mountable persistence models —
[roadmap §2](../../roadmap/promptcadence-roadmap.md)).

**Work**
* Repository skeleton (standard toolchain).
* `types.py`: `CeilingScope`, `BudgetCeiling` (validation: at least one bound; tag rules),
  `Debit`, `CeilingVerdict`, `LedgerEntry`.
* `core.py`: scope-window resolution (UTC days), incremental balance maintenance, verdict
  evaluation with most-restrictive semantics, `unpriced` propagation into money verdicts.
* `memory.py`: `InMemoryLedger` — the deterministic double, first-class supported API.
* `errors.py` per spec §7.

**Files/subsystems**
```text
src/loadledger/{__init__,__about__,types,core,memory,errors}.py
tests/unit/{test_types,test_verdicts,test_memory_ledger,test_windows}.py
```

**Tests**
* Integer exactness at large sums; per-currency separation; `CurrencyMismatch` on a mixed debit.
* Unpriced debits: tokens accumulate, money untouched, unpriced count surfaced on money verdicts.
* Every scope; several ceilings active at once; most-restrictive binding; the UTC midnight
  boundary with an injected clock.
* `would_exceed` side-effect-free (state hash before/after).
* Golden verdict serializations.

**Acceptance criteria**
1. `mypy --strict` clean; goldens stable across the CI matrix; coverage ≥ 95 %.
2. Re-costing test: entries re-priced under a corrected `ModelPricing` reproduce totals with no
   stored row changed (spec contract 1).

**Known risks:** window semantics (what "per-day" means) surprising an operator. Mitigated by UTC
in the field docs, the spec and a boundary test — stated three times, guessed zero.
**Likely failure modes:** recomputing balances by summation (performance) instead of maintaining
them; a float sneaking into a division for a "percentage used" convenience.
**Gold standards:** ADR-0030 rules 1–6 enforced by construction; no new cost primitives.
**Deferred:** SQL.

---

## Phase 2 — `loadledger.sql`, SqlLedger — publish 0.1.0

**Goal:** the mountable models and the durable implementation, proven on both dialects from a host
application's point of view.

**Prerequisites:** Phase 1.

**Work**
* `sql.py`: `mount_ledger_tables(metadata, prefix)` — plain-typed columns, no weightsdb import,
  no engine, no session, no Alembic history of its own; `SqlLedger` over a caller session
  factory; debit + verdicts in one transaction (spec contract 5).
* The `[sql]` extra; an upgrade-note template for host applications (the host owns migrations).
* README, quickstart (standalone mount into a script's own SQLite file); publish
  `loadledger 0.1.0`.

**Files/subsystems**
```text
src/loadledger/sql.py
tests/integration/{test_sql_ledger,test_mounting}.py     # SQLite always; PostgreSQL in CI
tests/integration/hostapp/                                # a miniature host: metadata + alembic env
```

**Tests**
* The miniature host mounts the tables, autogenerates a migration, upgrades, debits, queries —
  on SQLite and PostgreSQL.
* Two miniature hosts, two databases, one package version — no cross-talk.
* Atomicity: kill mid-debit on both dialects; entry and verdicts both present or both absent.
* Prefix respected everywhere (tables, indexes, constraints).
* 10 000-entry history query within budget.

**Acceptance criteria**
1. Spec §20 criteria 1–4 met (criterion 1 initially against the miniature host; re-verified
   inside PromptCadence P5).
2. `loadledger 0.1.0` published; `pip install loadledger[sql]` resolves standalone.

**Known risks:** the mountable pattern (D-6) meeting a host Alembic setup it has not seen.
Mitigated by the miniature host being a real Alembic project in this repository's tests, and by
PromptCadence P5 being the first true consumer before any 1.0 promise.
**Likely failure modes:** autogenerate emitting dialect-specific types; index names colliding
without the prefix.
**Gold standards:** application-owned data; one transaction per debit; both dialects green.
**Deferred:** IdeaPress adoption (roadmap §6); price-catalogue helper (ADR-0030's own trigger);
soft ceilings.
