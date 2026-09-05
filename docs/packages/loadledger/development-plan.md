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
* Unpriced debits: tokens accumulate; a debit with no estimate leaves money untouched; an estimate
  that did not total accumulates its priced components as a floor; all three counts surfaced on
  money verdicts; a `STRICT` ceiling fires on an untotalled estimate and not on a local debit
  (ADR-0069).
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
  no engine, no session, no Alembic history of its own; a run record beside the entries, so a run
  declared with nothing debited survives a restart (`declare_run`, spec §7/§13); `SqlLedger` over
  a caller session factory; debit + verdicts in one transaction (spec contract 5).
* `SqlLedger` is stateless and cheap to construct: balances persist per window (scope, window
  key) whatever ceilings exist, so an application resolves its ceiling set per operation — the
  configured defaults, this run's own budget, this project's cap — and builds a ledger view with
  it. A ceiling added later binds on the full history. The window key is a string, so composite
  scopes (spec §21) need no schema change.
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

---

## Phase 3 — A balance read that names no run — publish 0.2.0

**Goal:** a caller can ask what one window has accumulated without naming a run or inventing a
ceiling to read it through.

**Prerequisites:** Phase 2; PromptCadence Phase 5 as built (row F1), which is where the gap was
found.

**Work**
* `balances(*, scope, window_key)` on the `Ledger` protocol, `InMemoryLedger` and `SqlLedger`,
  returning the accumulated tokens, the per-currency money and the three honesty counts for one
  window — **no `run_id`, no ceiling**. The arithmetic already exists: `BalanceBook` maintains
  exactly this record per `(scope, window_key)`, and `{prefix}balances` /
  `{prefix}balance_money` are keyed on it, so this is a read of state the package already holds.
* The counts ride on it. A balance returned without `unpriced_debit_count` is a floor presenting
  itself as a total, which spec contract 2 forbids; the whole point of the read is to answer a
  *view*, and a view that cannot say "at least" is the failure ADR-0069 exists to prevent.
* Both implementations evaluate through the one `BalanceBook`, as every other method does, so the
  in-memory double keeps answering what production answers.
* `position()` on the same three, alongside it: `remaining` for **no particular run** — every
  configured ceiling, `PER_DAY` resolved at the injected clock's UTC day, refusing a `PER_RUN`
  ceiling with `InvalidCeiling` because such a cap has no window without a run. Mechanically it is
  `BalanceBook.verdicts` minus its `run_id` and `SqlLedger._windows_read` minus the `PER_RUN` key:
  the same engine, the same honesty counts, no new storage. `balances()` alone does not close the
  gap `docs/history/F1_HANDOFF.md` §7 names — it fixes `--scope tier`, but `--scope day` and `--scope project`
  need **headroom against a configured ceiling**, and deriving that outside the package would put
  the floor rule and the `exceeded` decision in a consumer. Without this method the reference-run
  workaround survives.
* `window_keys(scope)` is **not** included. It is cheap on both backends, but the consumer this
  phase exists for knows its tier names from `[tiers.<name>]` configuration and never asks — and an
  unused public method in a 0.x package is surface that then has to be kept and versioned.

**Why this exists.** Spec §7's surface answers "may this run spend" and "what has this run spent".
It cannot answer "what has been spent in this window", which is what a dashboard asks and what
PromptCadence needed for its per-tier and per-day views (`docs/history/F1_HANDOFF.md` §7). The two ways to
answer it from outside are summing `entries()` in the application — ledger arithmetic in a
consumer, which is what ADR-0050's mount exists to prevent — or configuring a ceiling nobody
intends to enforce purely to read a number through, which puts a fabricated cap in the record.

**Tests**
* A window with no ceiling configured still reports its balance, on both implementations and both
  dialects.
* The three honesty counts match what a `CeilingVerdict` over the same window reports, so the two
  reads cannot disagree.
* An unknown `window_key` reports zero tokens and **no money at all** — nothing spent is not the
  same as no money spent (ADR-0016), and neither is an error. Money is per currency and the
  currency set is open, so the field is a tuple of the currencies something was priced in
  (ascending by code, never summed across them — ADR-0030 rule 3) and an empty tuple is the
  "nothing priced" answer a single `money=None` would otherwise have carried.
* `position()` answers exactly what `remaining()` answers for the same ledger-wide ceilings, on
  both implementations; it refuses a `PER_RUN` ceiling; and on an empty ledger it reports the caps
  with nothing spent, which is reached as a fact rather than as an `UnknownRun` fallback.
* A `per_day` key resolved through `utc_day_key` matches what a debit at that instant landed in.

**Acceptance criteria**
1. `promptcadence ledger show --scope tier` reports spend rather than debit counts, with no
   arithmetic in PromptCadence and no ceiling invented to read it through.

**Not in this phase:** a tier ceiling. PromptCadence configures none by design (its lifecycle §6);
this is a **view**, not a cap.
**Gold standards:** integer-exact, no float, no recomputation from history.

