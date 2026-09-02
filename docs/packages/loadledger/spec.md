# LoadLedger — Specification

**Type:** Python package · **Import/distribution name:** `loadledger` · **Layer:** 3 (capability package)
**Status:** Specified, not implemented. Part of the PromptCadence arc
([roadmap](../../roadmap/promptcadence-roadmap.md)); builds entirely on
[ADR-0030](../../adr/0030-model-cost-and-pricing.md)'s types — no new cost primitives — and
introduces no persistence of its own beyond the mountable-models pattern (roadmap §2, D-6).

---

## 1. Purpose

Accumulate cost and usage across a multi-step run and enforce ceilings against it. BaseAiCore
already has every primitive a budget needs (`Money`, `TokenUsage`, `ModelPricing`, `CostEstimate`,
`estimate_cost`); what nothing does today is add them up across turns and say "stop". LoadLedger
is that accumulator: PromptCadence debits every turn and tool call against per-trajectory, per-day and
per-tier ceilings, and IdeaPress — named in ADR-0030's own context ("IdeaPress wants to show what
a piece of content cost to produce") — accumulates the same entries per unit and per project.

## 2. Scope

* `BudgetCeiling`: a cap — money and/or tokens — with a scope (`per-run`, `per-day` UTC,
  `per-tag`). A window spans exactly its own dimension: `per-run` is one run; `per-day` is one
  UTC day across every run in the ledger; `per-tag` is every debit carrying the tag across every
  run and every day, and never resets. A ledger is the one an application mounts in its own
  database, so "across every run" means across that application. Finer windows (a tag within a
  run, a tag within a day) are new scope values when needed, never a reinterpretation of these.
* `LedgerEntry`: one debit — `TokenUsage`, the `CostEstimate` (or its unsupported reasons), the
  source reference, and the running balances after, per active ceiling.
* The `Ledger` protocol: `debit()`, `remaining()`, `would_exceed()`, `entries()`,
  `declare_run()`.
* `InMemoryLedger` (the deterministic test double, first-class) and `SqlLedger` over the mountable
  models in `loadledger.sql`.
* Ceiling evaluation: multiple active ceilings, most restrictive binds, every verdict explicable.

## 3. Explicit non-goals

* **No pricing.** Prices arrive as `ModelPricing` records the caller acquired
  ([ADR-0030](../../adr/0030-model-cost-and-pricing.md) — acquisition belongs to applications);
  LoadLedger applies `baseaicore.estimate_cost` and never invents, converts or extrapolates a rate.
* **No currency conversion**, inherited from ADR-0030 rule 3.
* **No policy.** LoadLedger answers "would this exceed?" and "what remains?"; halting, pausing or
  re-approving is the caller's decision.
* No forecasting or estimation of future steps — the caller's estimator (PromptCadence lifecycle §6)
  queries history through `entries()`; the maths of prediction lives with its policy.
* No engine, session or migration ownership: `loadledger.sql` models mount into the
  **application's** metadata and Alembic history.

## 4. Responsibilities

| Responsibility | Detail |
|---|---|
| Accumulation | Sum `TokenUsage` exactly; sum `Money` exactly per currency; never sum across currencies |
| Unsupported honesty | A debit with no estimate accumulates tokens, flags `unpriced=True`, and touches no money balance — never zeroed ([ADR-0016](../../adr/0016-unavailable-is-not-zero.md)). A debit whose estimate did not total adds the components that were priced and nothing for the rest, so the money balance is a floor and the verdict's counts say so ([ADR-0069](../../adr/0069-a-partial-price-is-a-floor-and-a-money-ceiling-chooses-how-it-binds.md)) |
| Ceilings | Evaluate every active ceiling on `debit` and `would_exceed`; report each verdict with numbers |
| Record | Every entry stores `TokenUsage` + `pricing_hash`, per ADR-0030 rule 1 — history is re-costable |
| History | `entries()` filtered by scope, tag, time window — the input to PromptCadence's historical estimator and IdeaPress's per-unit cost view |

## 5. Dependencies

`baseaicore`. `loadledger.sql` additionally imports `sqlalchemy>=2,<3` (an optional extra:
`loadledger[sql]`). No sibling capability package — deliberately not `weightsdb`, whose
engine/session/migration machinery belongs to the application that owns the database.

## 6. Consumers

PromptCadence (trajectory budgets), IdeaPress (per-unit and per-project cost, adoption phase in the
[roadmap §6](../../roadmap/promptcadence-roadmap.md)).

## 7. Public API

```python
class CeilingScope(StrEnum): PER_RUN = "per_run"; PER_DAY = "per_day"; PER_TAG = "per_tag"
class PartialPricing(StrEnum): FLOOR = "floor"; STRICT = "strict"     # ADR-0069

@dataclass(frozen=True, slots=True)
class BudgetCeiling:
    scope: CeilingScope
    money: Money | None = None         # binds priced usage in this currency
    tokens: int | None = None          # binds all usage
    tag: str | None = None             # required for PER_TAG (PromptCadence: the tier name)
    partial_pricing: PartialPricing = PartialPricing.FLOOR
                                       # keyword-only. FLOOR: bind on what was priced, may fire
                                       # late. STRICT: an estimate in scope that did not total
                                       # counts as exceeding; requires a money bound
    # at least one of money/tokens required; both permitted

@dataclass(frozen=True, slots=True)
class Debit:
    run_id: str                        # trajectory / project / unit — the caller's run identity
    source_ref: str                    # turn id, tool invocation id, stage attempt id
    usage: TokenUsage
    cost: CostEstimate | None          # None ⇒ no pricing applied (local); unsupported reasons ride along
    tags: tuple[str, ...] = ()         # e.g. ("tier:local_fast",)
    occurred_at: datetime | None = None    # default: injected clock

@dataclass(frozen=True, slots=True)
class CeilingVerdict:
    ceiling: BudgetCeiling
    exceeded: bool                     # strictly greater than a bound; under STRICT, also true when
                                       # untotalled_debit_count > 0
    money_spent: Money | None          # None when nothing priced yet in this scope; a floor when
                                       # unpriced_debit_count > 0 — render as "at least"
    money_remaining: Money | None
    tokens_spent: int                  # the classes providers reported; a floor when unmetered > 0
    tokens_remaining: int | None
    unpriced_debit_count: int = 0      # no estimate, or an estimate that did not total
    untotalled_debit_count: int = 0    # the subset that carried an estimate which did not total
    unmetered_debit_count: int = 0     # left at least one token class unreported

@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    debit: Debit
    unpriced: bool                     # cost absent or unsupported
    pricing_hash: str | None
    verdicts: tuple[CeilingVerdict, ...]   # every active ceiling, after this debit

class Ledger(Protocol):
    def debit(self, debit: Debit) -> LedgerEntry: ...
    def would_exceed(self, run_id: str, *, usage: TokenUsage | None = None,
                     cost: CostEstimate | None = None,
                     tags: tuple[str, ...] = ()) -> tuple[CeilingVerdict, ...]: ...
    def remaining(self, run_id: str) -> tuple[CeilingVerdict, ...]: ...
    def entries(self, *, run_id: str | None = None, tag: str | None = None,
                since: datetime | None = None) -> Sequence[LedgerEntry]: ...
    def declare_run(self, run_id: str) -> None: ...
        # a run exists once debited or declared (§13); idempotent; a blank id is refused.
        # PromptCadence declares at trajectory creation, so pre-flight never meets UnknownRun

InMemoryLedger(ceilings: Sequence[BudgetCeiling], *, clock: Clock)
SqlLedger(session_factory, ceilings: Sequence[BudgetCeiling], *, clock: Clock,
          table_prefix: str = "ledger_")

# loadledger.sql — mountable models (roadmap §2, D-6)
def mount_ledger_tables(metadata: MetaData, *, prefix: str = "ledger_") -> LedgerTables: ...
# plain-typed columns only; the application includes them in its own Alembic history and owns
# the data. Two applications mounting these tables have two tables in two databases — never one.

# Errors (subclass baseaicore.SuiteError)
LedgerError                LEDGER_ERROR
├── CurrencyMismatch       LEDGER_CURRENCY_MISMATCH   # ceiling USD, debit EUR — refused, not converted
├── InvalidCeiling         LEDGER_CEILING_INVALID     # incl. STRICT partial pricing with no money bound
└── UnknownRun             LEDGER_UNKNOWN_RUN
```

## 8. Inputs

Ceiling configuration from the application; `Debit`s built from LoadCoach/provider responses;
an injected clock.

## 9. Outputs

`LedgerEntry`s with per-ceiling verdicts, remaining-balance reports, entry history, typed errors.

## 10. Data ownership

None of its own. `loadledger.sql` defines the table shapes; the **application** owns the database,
the rows and the retention (PromptCadence's `ledger_entries`, IdeaPress's when it adopts). The
`InMemoryLedger` never survives the process and says so.

## 11. Public contracts

1. **Store usage; derive cost** (ADR-0030 rule 1): an entry's primary facts are `TokenUsage` and
   `pricing_hash`; monetary balances are recomputable from entries and a price catalogue, and a
   test proves re-costing history changes no stored row.
2. **Unpriced is never zero, and a partial price is a floor**
   ([ADR-0069](../../adr/0069-a-partial-price-is-a-floor-and-a-money-ceiling-chooses-how-it-binds.md)):
   a debit with no estimate leaves every money balance untouched; a debit whose estimate did not
   total adds the components that were priced and nothing for the rest. Both set `unpriced=True`
   on the entry. Every money verdict carries `unpriced_debit_count` (so `money_spent` is a floor
   whenever it is non-zero, rendered as "at least"), `untotalled_debit_count` (the estimates that
   did not total) and `unmetered_debit_count`. On a floor, `exceeded` is certain when true and not
   when false; a `STRICT` money ceiling treats `untotalled_debit_count > 0` as exceeded instead, so
   its cap is never crossed. "Under budget" is never claimed over an incomplete sum without saying
   so.
3. **Exact arithmetic**: token and nano sums are integer-exact; a mixed-currency debit against a
   ceiling raises `CurrencyMismatch` rather than converting (ADR-0030 rule 3).
4. **Deterministic verdicts**: same ceilings + same entries ⇒ same verdicts, byte-identical in
   serialized form — golden-tested, because verdicts appear in approval records.
5. **`debit` is atomic with its verdicts**: the entry and the balances it reports commit together
   (in `SqlLedger`, one transaction), so a crash cannot record spend without its verdict — the
   ADR-0044 shape applied to money.
6. **`would_exceed` has no side effects** and is safe to call from an approval path at any
   frequency.
7. `PER_DAY` windows are UTC calendar days, stated in the field docs and tested across a midnight
   boundary — a budget that resets at a machine-local midnight is a different budget on every
   machine.

## 12. Configuration

Constructor arguments only.

## 13. Error behaviour

| Condition | Behaviour |
|---|---|
| Ceiling with neither money nor tokens | `InvalidCeiling` at construction |
| `PER_TAG` ceiling without a tag | `InvalidCeiling` |
| `STRICT` partial pricing on a ceiling with no money bound | `InvalidCeiling` — strictness is a statement about the money bound |
| Debit currency ≠ ceiling currency | `CurrencyMismatch`, both named |
| `remaining`/`would_exceed` for an unknown run | `UnknownRun` (a run exists once debited or declared) |
| Debit exceeding a ceiling | **Not an error.** The entry records `exceeded=True` verdicts; refusing work is the caller's policy. `would_exceed` exists so the caller can refuse *before* spending |

## 14. Security considerations

Entries carry references and hashes, never prompt or response content. `source_ref` is an opaque
id. No I/O beyond the caller-supplied session. Nothing here logs.

## 15. Performance

| Measure | Target |
|---|---|
| `debit` with 3 active ceilings (`SqlLedger`, SQLite) | ≤ 5 ms |
| `would_exceed` | ≤ 2 ms |
| `entries` for a 10 000-entry run | ≤ 100 ms |

Balances are maintained incrementally per scope, not recomputed by summing all rows per debit.

## 16. Cross-platform

Pure Python; fully portable. Determinism goldens run across the CI matrix.

## 17. Observability

No logging. `LedgerEntry` is exactly the body of the suite's `budget.debited` event.

## 18. Test strategy

| Area | Tests |
|---|---|
| Arithmetic | Integer exactness at large sums; per-currency separation; token/money independence |
| Unsupported | Debits with no estimate: tokens accumulate, money untouched. Estimates that did not total: the priced components accumulate as a floor, all three counts surfaced; a partial sum never claims completeness. `STRICT` fires on an untotalled estimate, at pre-flight too, and not on a debit with no estimate |
| Ceilings | Each scope; multiple active; most-restrictive binding; UTC day boundary; per-tag isolation |
| Re-costing | Entries re-costed under a corrected price list reproduce totals without any row changing |
| Atomicity | Kill mid-debit (SQLite + PostgreSQL): entry and verdicts both present or both absent |
| Determinism | Golden verdict serializations across the matrix |
| Mounting | Two schemas in two databases from one `mount_ledger_tables`; Alembic autogenerate in a host app picks the tables up; prefix respected |
| Errors | Every §13 row |

Coverage floor: **95 %**.

## 19. Compatibility and versioning

Semver, pre-1.0 `0.x`. The mounted table shapes are versioned with the package; a column change
ships with an upgrade note and a migration recipe for host applications (the host owns the actual
migration, so the recipe is documentation plus a helper, never an auto-migration).

## 20. Acceptance criteria

1. PromptCadence enforces a $5.00 + 2 M-token trajectory ceiling: the crossing debit's entry shows the
   verdicts, and the pre-flight `would_exceed` refuses the step that would cross.
2. A standalone script with `loadledger[sql]` + `baseaicore` mounts the tables into its own
   SQLite database, debits priced and unpriced usage, and prints honest balances (`—`, not `$0`,
   for the unpriced local model; "at least" for a floor).
3. Re-costing test (contract 1) passes.
4. `mypy --strict`, `ruff`, `lint-imports` clean; coverage ≥ 95 %.

## 21. Future extensions

* IdeaPress adoption: per-unit and per-project scopes are `PER_RUN`/`PER_TAG` as-is; the adoption
  phase decides its tags and sets a per-output and a per-project ceiling from IdeaPress's
  configuration (roadmap row J1).
* Composite windows (`PER_RUN` × tag, `PER_DAY` × tag) as additional scope values, if a consumer
  needs a tag cap that resets; the persisted balance key (scope, window key) already admits them.
* A price-catalogue helper, only at ADR-0030's own trigger (two consumers independently
  implementing acquisition).
* Soft ceilings (warn thresholds below the hard cap) when a UI consumer wants them.
