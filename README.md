# LoadLedger

Budget accumulation and ceilings over ADR-0030's cost types: debits, per-scope balances, and
explicable ceiling verdicts. No pricing, no conversion, no policy.

**Status:** **Phase 1 complete, unreleased.** The pure core is implemented and tested — ceilings,
debits, verdicts and `InMemoryLedger`. `loadledger.sql`, `SqlLedger` and the `loadledger[sql]`
extra arrive in Phase 2, which is also when `0.1.0` publishes; see
[docs/packages/loadledger/development-plan.md](docs/packages/loadledger/development-plan.md).

Part of the **Local AI Suite**.

## Install

Not on PyPI yet. Until `0.1.0` publishes at the end of Phase 2:

```bash
pip install -e .
```

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
* **Unpriced is not free.** An unpriced debit accumulates tokens, leaves every money balance
  untouched, and puts its count on the money verdict — so "under budget" is never claimed over an
  incomplete sum without saying so (ADR-0016).

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

`PER_DAY` means a **UTC** calendar day. A budget that reset at the machine's local midnight would
be a different budget on every machine.

## Documentation

Project documentation lives under [`docs/`](docs/README.md).

| Read this | For |
|---|---|
| [docs/packages/loadledger/spec.md](docs/packages/loadledger/spec.md) | Purpose, scope, non-goals, public contracts, acceptance criteria |
| [docs/packages/loadledger/development-plan.md](docs/packages/loadledger/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

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
