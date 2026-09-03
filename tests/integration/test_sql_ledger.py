"""`SqlLedger` is `InMemoryLedger` with a different store — and the ways that is proven.

Every test here runs on both dialects through the ``engine`` fixture. The centrepiece is the
parity test: the same script of debits, against both implementations, must produce byte-identical
canonical entries. Everything after it is a property that parity alone would not pin down —
durability across a restart, the absence of side effects on a read path, an integer width, a
currency that never got converted.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import pytest
import sqlalchemy as sa
from baseaicore import Money, TokenUsage, canonical_json, estimate_cost, is_supported

from conftest import DAY, MIDDAY, ManualClock, cost_of, mounted, pricing, rates, session_factory_for
from loadledger import (
    BudgetCeiling,
    CeilingScope,
    CurrencyMismatch,
    Debit,
    InMemoryLedger,
    PartialPricing,
    UnknownRun,
    UnsupportedDialect,
)
from loadledger.sql import SqlLedger

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from loadledger import LedgerEntry


def ceilings() -> list[BudgetCeiling]:
    """The three-ceiling configuration spec §15's budgets are stated against."""
    return [
        BudgetCeiling(
            scope=CeilingScope.PER_RUN, money=Money.from_decimal("USD", "5.00"), tokens=2_000_000
        ),
        BudgetCeiling(scope=CeilingScope.PER_DAY, money=Money.from_decimal("USD", "25.00")),
        BudgetCeiling(scope=CeilingScope.PER_TAG, tokens=1_000_000, tag="tier:local_fast"),
    ]


def sql_ledger(engine: Engine, *, clock: ManualClock, prefix: str = "ledger_") -> SqlLedger:
    """Mount, create and return a ledger over ``engine``."""
    mounted(engine, prefix=prefix)
    return SqlLedger(session_factory_for(engine), ceilings(), clock=clock, table_prefix=prefix)


def without_ids(entries: object) -> list[dict[str, Any]]:
    """Canonical entries with the ULID removed, so two ledgers' streams are comparable.

    Two ledgers draw ids from two generators, so the ids differ by construction and prove nothing;
    everything else in the record is the thing under test.
    """
    documents = [dict(entry.as_canonical()) for entry in cast("list[LedgerEntry]", entries)]
    for document in documents:
        document.pop("entry_id")
    return documents


def script(clock: ManualClock) -> list[Debit]:
    """A run that exercises every honesty rule: priced, unpriced, untotalled, back-dated, tagged."""
    complete = pricing(rates())
    return [
        # Fully priced: every class reported, so the estimate totals.
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=TokenUsage(
                input_tokens=120_000, output_tokens=4_000, cache_write_tokens=0, cache_read_tokens=0
            ),
            cost=cost_of(
                TokenUsage(
                    input_tokens=120_000,
                    output_tokens=4_000,
                    cache_write_tokens=0,
                    cache_read_tokens=0,
                ),
                price=complete,
            ),
            tags=("tier:remote_slow",),
            occurred_at=MIDDAY,
        ),
        # No estimate at all: a local model. Tokens accumulate, money is untouched.
        Debit(
            run_id="traj-1",
            source_ref="turn-2",
            usage=TokenUsage(input_tokens=900, output_tokens=200),
            cost=None,
            tags=("tier:local_fast",),
            occurred_at=MIDDAY + timedelta(minutes=1),
        ),
        # An estimate that did not total — the ordinary remote case (ADR-0069): the priced
        # components accumulate as a floor and the counts say so.
        Debit(
            run_id="traj-1",
            source_ref="turn-3",
            usage=TokenUsage(input_tokens=50_000, output_tokens=1_500),
            cost=cost_of(TokenUsage(input_tokens=50_000, output_tokens=1_500), price=complete),
            tags=("tier:remote_slow",),
            occurred_at=MIDDAY + timedelta(minutes=2),
        ),
        # Back-dated into the previous UTC day: it must land in *that* day's window.
        Debit(
            run_id="traj-1",
            source_ref="turn-4",
            usage=TokenUsage(input_tokens=7, output_tokens=3),
            cost=None,
            tags=("tier:local_fast",),
            occurred_at=MIDDAY - DAY,
        ),
        # A second run, so the per-day and per-tag windows are proven to be ledger-wide.
        Debit(
            run_id="traj-2",
            source_ref="turn-1",
            usage=TokenUsage(input_tokens=11, output_tokens=13),
            cost=None,
            tags=("tier:local_fast",),
            occurred_at=MIDDAY + timedelta(minutes=3),
        ),
    ]


def test_it_records_exactly_what_the_in_memory_ledger_records(engine: Engine) -> None:
    memory_clock, sql_clock = ManualClock(), ManualClock()
    memory = InMemoryLedger(ceilings(), clock=memory_clock)
    durable = sql_ledger(engine, clock=sql_clock)

    returned_from_memory = [memory.debit(debit) for debit in script(memory_clock)]
    returned_from_sql = [durable.debit(debit) for debit in script(sql_clock)]

    assert without_ids(returned_from_memory) == without_ids(returned_from_sql)
    assert without_ids(memory.entries()) == without_ids(durable.entries())
    for run_id in ("traj-1", "traj-2"):
        assert [verdict.as_canonical() for verdict in memory.remaining(run_id)] == [
            verdict.as_canonical() for verdict in durable.remaining(run_id)
        ]


@pytest.mark.contract
def test_an_entry_survives_a_restart_and_reads_back_byte_identically(engine: Engine) -> None:
    clock = ManualClock()
    durable = sql_ledger(engine, clock=clock)
    written = [durable.debit(debit) for debit in script(clock)]

    # A second ledger over the same tables: a new process, as far as the rows are concerned.
    reopened = SqlLedger(session_factory_for(engine), ceilings(), clock=ManualClock())
    read_back = list(reopened.entries())

    assert [canonical_json(entry.as_canonical()) for entry in read_back] == [
        canonical_json(entry.as_canonical()) for entry in written
    ]


def test_a_declared_run_with_nothing_debited_survives_a_restart(engine: Engine) -> None:
    clock = ManualClock()
    durable = sql_ledger(engine, clock=clock)
    with pytest.raises(UnknownRun):
        durable.remaining("traj-1")

    durable.declare_run("traj-1")
    durable.declare_run("traj-1")  # idempotent

    reopened = SqlLedger(session_factory_for(engine), ceilings(), clock=clock)
    verdicts = reopened.remaining("traj-1")
    assert [verdict.tokens_spent for verdict in verdicts] == [0, 0, 0]
    # Nothing priced yet, so money_spent is None and the whole cap remains — not a fabricated zero.
    assert verdicts[0].money_spent is None
    assert verdicts[0].money_remaining == Money.from_decimal("USD", "5.00")


def test_declare_run_refuses_a_blank_id_and_writes_nothing(engine: Engine) -> None:
    _, tables = mounted(engine)
    durable = SqlLedger(session_factory_for(engine), ceilings(), clock=ManualClock())
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="non-blank"):
            durable.declare_run(blank)
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(tables.runs)).scalar() == 0


def snapshot(engine: Engine, tables: Any) -> list[list[tuple[Any, ...]]]:
    """Every row in the mounted set, for a before/after comparison."""
    with engine.connect() as connection:
        return [
            [tuple(row) for row in connection.execute(sa.select(table).order_by(*table.c))]
            for table in tables.all_tables
        ]


def test_would_exceed_writes_nothing_at_any_frequency(engine: Engine) -> None:
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(session_factory_for(engine), ceilings(), clock=clock)
    durable.debit(script(clock)[0])

    before = snapshot(engine, tables)
    for _ in range(100):
        durable.would_exceed(
            "traj-1", usage=TokenUsage(input_tokens=10), tags=("tier:never_seen_before",)
        )
        durable.remaining("traj-1")
        durable.entries(run_id="traj-1")
    assert snapshot(engine, tables) == before


def test_a_refused_currency_leaves_no_row_behind(engine: Engine) -> None:
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(session_factory_for(engine), ceilings(), clock=clock)
    durable.declare_run("traj-1")
    before = snapshot(engine, tables)

    euros = pricing(rates("EUR"))
    usage = TokenUsage(input_tokens=1_000, output_tokens=100)
    with pytest.raises(CurrencyMismatch) as raised:
        durable.debit(
            Debit(
                run_id="traj-1",
                source_ref="turn-1",
                usage=usage,
                cost=cost_of(usage, price=euros),
                occurred_at=MIDDAY,
            )
        )
    assert raised.value.details["debit_currency"] == "EUR"
    assert snapshot(engine, tables) == before


def test_money_and_tokens_past_a_four_byte_integer_round_trip(engine: Engine) -> None:
    """$2.15 is 2 150 000 000 nanos — already past 2**31 - 1. See `mount_ledger_tables`.

    SQLite's dynamic typing would accept the value into a 4-byte column and PostgreSQL would raise
    ``DataError``, so this has to run on both dialects to mean anything.
    """
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(
        session_factory_for(engine),
        [BudgetCeiling(scope=CeilingScope.PER_RUN, money=Money.from_decimal("USD", "1000.00"))],
        clock=clock,
    )
    # 1 000 000 input tokens at $3.00/M is exactly $3.00 = 3 000 000 000 nanos.
    usage = TokenUsage(
        input_tokens=1_000_000, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
    )
    entry = durable.debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=usage,
            cost=cost_of(usage, price=pricing(rates())),
            occurred_at=MIDDAY,
        )
    )
    spent = entry.verdicts[0].money_spent
    assert spent is not None
    assert spent.nanos == 3_000_000_000 > 2**31 - 1

    # And a token count that would overflow the same width.
    huge = TokenUsage(
        input_tokens=2_200_000_000, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
    )
    durable.debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-2",
            usage=huge,
            cost=None,
            occurred_at=MIDDAY,
        )
    )
    with engine.connect() as connection:
        nanos = connection.execute(sa.select(tables.balance_money.c.nanos_spent)).scalar()
        tokens = connection.execute(sa.select(tables.balances.c.tokens_spent)).scalars().all()
    assert nanos == 3_000_000_000
    assert all(count == 2_201_000_000 for count in tokens)


def test_a_debit_lands_in_the_utc_day_it_happened_in_across_a_midnight(engine: Engine) -> None:
    clock = ManualClock(MIDDAY)
    _, tables = mounted(engine)
    durable = SqlLedger(
        session_factory_for(engine),
        [BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=1_000_000)],
        clock=clock,
    )
    just_before = MIDDAY.replace(hour=23, minute=59, second=59)
    just_after = just_before + timedelta(seconds=1)
    for index, when in enumerate((just_before, just_after)):
        durable.debit(
            Debit(
                run_id="traj-1",
                source_ref=f"turn-{index}",
                usage=TokenUsage(input_tokens=100, output_tokens=0),
                cost=None,
                occurred_at=when,
            )
        )
    with engine.connect() as connection:
        rows: dict[str, int] = {
            str(window_key): int(tokens)
            for window_key, tokens in connection.execute(
                sa.select(tables.balances.c.window_key, tables.balances.c.tokens_spent)
            ).all()
        }
    assert rows == {"2026-09-02": 100, "2026-09-03": 100, "traj-1": 200}

    # And the stored instant comes back timezone-aware and equal on both dialects.
    stored = [entry.debit.occurred_at for entry in durable.entries()]
    assert stored == [just_before, just_after]
    assert all(when is not None and when.tzinfo is not None for when in stored)


def test_entries_filters_narrow_the_same_way_the_in_memory_ledger_narrows(engine: Engine) -> None:
    clock = ManualClock()
    memory = InMemoryLedger(ceilings(), clock=ManualClock())
    durable = sql_ledger(engine, clock=clock)
    for debit in script(clock):
        memory.debit(debit)
        durable.debit(debit)

    for kwargs in (
        {"run_id": "traj-1"},
        {"run_id": "traj-2"},
        {"tag": "tier:local_fast"},
        {"tag": "tier:remote_slow"},
        {"tag": "no-such-tag"},
        {"since": MIDDAY},
        {"since": MIDDAY + timedelta(minutes=2)},
        {"run_id": "traj-1", "tag": "tier:local_fast", "since": MIDDAY},
    ):
        assert without_ids(memory.entries(**kwargs)) == without_ids(durable.entries(**kwargs)), (
            kwargs
        )


def test_entries_since_is_inclusive_and_refuses_a_naive_bound(engine: Engine) -> None:
    clock = ManualClock()
    durable = sql_ledger(engine, clock=clock)
    for debit in script(clock):
        durable.debit(debit)

    at_midday = durable.entries(run_id="traj-1", since=MIDDAY)
    assert [entry.debit.source_ref for entry in at_midday] == ["turn-1", "turn-2", "turn-3"]

    with pytest.raises(ValueError, match="timezone-aware"):
        durable.entries(since=MIDDAY.replace(tzinfo=None))


def test_re_costing_history_changes_no_stored_row(engine: Engine) -> None:
    """Spec contract 1 / acceptance criterion 3, proven at the row level, not just the total."""
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(
        session_factory_for(engine),
        [BudgetCeiling(scope=CeilingScope.PER_RUN, money=Money.from_decimal("USD", "500.00"))],
        clock=clock,
    )
    wrong = pricing(rates(input_per_million="30.00", output_per_million="150.00"))
    usages = [
        TokenUsage(
            input_tokens=1_000 * index,
            output_tokens=100 * index,
            cache_write_tokens=0,
            cache_read_tokens=0,
        )
        for index in range(1, 6)
    ]
    for index, usage in enumerate(usages):
        durable.debit(
            Debit(
                run_id="traj-1",
                source_ref=f"turn-{index}",
                usage=usage,
                cost=cost_of(usage, price=wrong, at=MIDDAY),
                occurred_at=MIDDAY,
            )
        )
    before = snapshot(engine, tables)

    # Re-cost from what is stored: usage plus the pricing hash, never a stored money figure.
    corrected = pricing(rates())
    recosted = Money(currency="USD", nanos=0)
    for entry in durable.entries(run_id="traj-1"):
        assert entry.pricing_hash == wrong.pricing_hash
        assert entry.debit.cost is None  # the estimate is not a stored fact (ADR-0030 rule 1)
        estimate = estimate_cost(entry.debit.usage, corrected, at=MIDDAY)
        assert is_supported(estimate.total)
        recosted = recosted + estimate.total

    # Exactly a tenth of the wrong price list, and not one row moved to say so.
    assert recosted == Money.from_decimal("USD", "0.0675")
    assert snapshot(engine, tables) == before


def test_a_verdict_still_describes_its_ceiling_after_that_ceiling_is_removed(
    engine: Engine,
) -> None:
    clock = ManualClock()
    strict = BudgetCeiling(
        scope=CeilingScope.PER_RUN,
        money=Money.from_decimal("USD", "0.01"),
        partial_pricing=PartialPricing.STRICT,
    )
    mounted(engine)
    durable = SqlLedger(session_factory_for(engine), [strict], clock=clock)
    usage = TokenUsage(input_tokens=50_000, output_tokens=1_000)
    written = durable.debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=usage,
            cost=cost_of(usage, price=pricing(rates())),
            occurred_at=MIDDAY,
        )
    )
    assert written.verdicts[0].exceeded  # strict fires on the untotalled estimate

    # The operator deletes the ceiling from configuration entirely.
    reconfigured = SqlLedger(session_factory_for(engine), [], clock=clock)
    (recovered,) = reconfigured.entries(run_id="traj-1")
    assert recovered.verdicts[0].ceiling == strict
    assert recovered.verdicts[0].exceeded
    assert reconfigured.remaining("traj-1") == ()


def test_an_unpriced_debit_creates_no_money_row(engine: Engine) -> None:
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(
        session_factory_for(engine),
        [BudgetCeiling(scope=CeilingScope.PER_RUN, money=Money.from_decimal("USD", "5.00"))],
        clock=clock,
    )
    durable.debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=TokenUsage(input_tokens=900, output_tokens=200),
            cost=None,
            occurred_at=MIDDAY,
        )
    )
    with engine.connect() as connection:
        money_rows = connection.execute(sa.select(tables.balance_money)).all()
    assert money_rows == []
    verdict = durable.remaining("traj-1")[0]
    assert verdict.money_spent is None  # '—', never '$0.00' (ADR-0016)
    assert verdict.money_remaining == Money.from_decimal("USD", "5.00")
    assert verdict.unpriced_debit_count == 1


def test_the_indexed_columns_agree_with_the_canonical_record(engine: Engine) -> None:
    clock = ManualClock()
    _, tables = mounted(engine)
    durable = SqlLedger(session_factory_for(engine), ceilings(), clock=clock)
    for debit in script(clock):
        durable.debit(debit)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(tables.entries)).mappings().all()
    assert rows
    for row in rows:
        document = json.loads(row["debit_json"])
        assert document["run_id"] == row["run_id"]
        assert document["source_ref"] == row["source_ref"]
        assert document["occurred_at"].startswith(row["occurred_at"].strftime("%Y-%m-%dT%H:%M:%S"))


def test_the_two_supported_dialects_and_only_those_have_an_upsert() -> None:
    """Covers the PostgreSQL arm without a PostgreSQL server, and the refusal for anything else.

    ``_insert_for`` is private, and a test reaching for it is a trade: the alternative is that the
    one line choosing PostgreSQL's ``insert()`` is exercised only where a server happens to be
    running, which on this machine is nowhere. The refusal below is the part that matters, and it
    is asserted through the public surface as well.
    """
    from loadledger.sql import _insert_for  # noqa: PLC0415 — see the docstring

    assert _insert_for(cast("Session", _StubSession("sqlite"))) is not _insert_for(
        cast("Session", _StubSession("postgresql"))
    )
    for dialect in ("sqlite", "postgresql"):
        assert callable(_insert_for(cast("Session", _StubSession(dialect))))


def test_a_stored_instant_comes_back_utc_from_either_driver() -> None:
    """SQLite hands back a naive value, PostgreSQL an aware one; both must read as the same UTC.

    The PostgreSQL arm is unreachable from a SQLite leg and this machine has no server, so the two
    driver behaviours are exercised directly against the helper that reconciles them.
    """
    from loadledger.sql import _from_utc  # noqa: PLC0415 — see the docstring

    naive_from_sqlite = MIDDAY.replace(tzinfo=None)
    aware_from_postgresql = MIDDAY.astimezone(timezone(timedelta(hours=-5)))
    assert _from_utc(naive_from_sqlite) == MIDDAY
    assert _from_utc(aware_from_postgresql) == MIDDAY
    assert _from_utc(aware_from_postgresql).tzinfo is UTC


class _StubSession:
    """The smallest thing ``_insert_for`` reads: a session whose bind names a dialect."""

    def __init__(self, dialect_name: str) -> None:
        self._dialect_name = dialect_name

    def get_bind(self) -> object:
        return type(
            "_Bind", (), {"dialect": type("_Dialect", (), {"name": self._dialect_name})()}
        )()

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def test_a_third_dialect_is_refused_rather_than_attempted() -> None:
    durable = SqlLedger(
        lambda: cast("Session", _StubSession("mysql")), ceilings(), clock=ManualClock()
    )
    with pytest.raises(UnsupportedDialect) as raised:
        durable.declare_run("traj-1")
    assert raised.value.details["dialect"] == "mysql"
    assert raised.value.code == "LEDGER_UNSUPPORTED_DIALECT"
