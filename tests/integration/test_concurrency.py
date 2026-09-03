"""Two writers, one window: the lost update, and why this ledger does not have one.

A read-modify-write on a balance row is the textbook lost update, and here the thing lost is money.
Three tests, in the order the argument runs:

1. The failure mode, staged deterministically, so it is a fact in this repository rather than a
   paragraph: two sessions read the same balance and both write it back, and one debit vanishes.
2. The statements a real debit issues, read off the wire: every balance moves by an increment the
   database performs, and no balance is read before the transaction's first write.
3. Two real processes hammering one window at once, which is the claim as a caller would test it.

What is *not* claimed is serializable isolation; see ``SqlLedger``'s class docstring. Two debits
committing concurrently may each report a verdict that omits the other's spend. Both are recorded
exactly, and the next verdict is correct.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import event

from conftest import mounted, session_factory_for
from ledger_subprocess import CEILINGS, TOKENS_PER_DEBIT, WHEN, a_debit
from loadledger.sql import SqlLedger

if TYPE_CHECKING:
    from sqlalchemy import Engine

SCRIPT = Path(__file__).parent / "ledger_subprocess.py"

DEBITS_PER_PROCESS = 40
WRITERS = 2


def tokens_in(engine: Engine, tables: object, window_key: str) -> int:
    """Read one window's token balance straight out of the row."""
    balances = tables.balances  # type: ignore[attr-defined] # a LedgerTables
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(balances.c.tokens_spent).where(balances.c.window_key == window_key)
            ).scalar_one()
        )


def test_a_read_modify_write_on_a_balance_row_loses_a_debit(engine: Engine) -> None:
    """The mistake, staged by hand — deterministic, and true on both dialects.

    No race is needed to show it: two writers that both *read* before either *writes* will each
    compute the same new total, and the second write silently discards the first. This is what a
    balance kept as ``balance = read(); write(balance + delta)`` does under any concurrency at all.
    """
    _, tables = mounted(engine)
    factory = session_factory_for(engine)
    with factory() as setup:
        setup.execute(
            sa.insert(tables.balances).values(
                scope="per_run",
                window_key="traj-1",
                tokens_spent=100,
                unpriced_debit_count=0,
                untotalled_debit_count=0,
                unmetered_debit_count=0,
            )
        )
        setup.commit()

    where = (tables.balances.c.scope == "per_run") & (tables.balances.c.window_key == "traj-1")
    with factory() as first, factory() as second:
        seen_by_first = first.execute(
            sa.select(tables.balances.c.tokens_spent).where(where)
        ).scalar_one()
        seen_by_second = second.execute(
            sa.select(tables.balances.c.tokens_spent).where(where)
        ).scalar_one()
        first.execute(
            sa.update(tables.balances).where(where).values(tokens_spent=seen_by_first + 11)
        )
        first.commit()
        second.execute(
            sa.update(tables.balances).where(where).values(tokens_spent=seen_by_second + 11)
        )
        second.commit()

    # Two debits of 11 went in; 11 came out. This is the defect the upsert exists to prevent.
    assert tokens_in(engine, tables, "traj-1") == 111


def test_the_statements_a_debit_issues_are_increments_and_they_come_before_the_reads(
    engine: Engine,
) -> None:
    """The mechanism, read off the wire: no read-modify-write, and no read before the writes.

    Both halves of ``SqlLedger``'s concurrency argument are properties of the SQL it emits, so
    they are asserted against the SQL it emits rather than against a description of it:

    * every balance is advanced by ``ON CONFLICT … DO UPDATE SET x = x + excluded.x``, an
      increment the database performs under the row lock — never a value Python read, added to and
      wrote back;
    * no ``SELECT`` touches a balance before the first balance write. On SQLite pysqlite begins the
      transaction at the first DML statement, so a read issued earlier would run outside it.
    """
    mounted(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.split()))

    ledger = SqlLedger(session_factory_for(engine), CEILINGS, clock=lambda: WHEN)
    try:
        ledger.debit(a_debit("traj-1", 0))
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    upserts = [line for line in statements if "INSERT INTO ledger_balances" in line]
    assert upserts, statements
    for line in upserts:
        assert "ON CONFLICT" in line
        for column in ("tokens_spent", "unpriced_debit_count", "unmetered_debit_count"):
            assert f"{column} = (ledger_balances.{column} + excluded.{column})" in line, line

    first_write = min(index for index, line in enumerate(statements) if line.startswith("INSERT"))
    reads_before = [
        line
        for line in statements[:first_write]
        if line.startswith("SELECT") and "ledger_balance" in line
    ]
    assert reads_before == [], (
        f"a balance was read before the transaction's first write: {reads_before}"
    )


def test_two_processes_debiting_one_window_lose_nothing(tmp_path: Path) -> None:
    """The claim as a caller would test it: two processes, one window, one exact total.

    Both children are started before either is waited on, so their write transactions genuinely
    overlap. On SQLite the second writer waits on the first's write lock (pysqlite's five-second
    default ``timeout``); on PostgreSQL it blocks on the balance row's lock. Either way it adds.
    """
    path = tmp_path / "ledger.sqlite3"
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=wal")
    _, tables = mounted(engine)

    children = [
        subprocess.Popen(  # noqa: S603 — a fixed argv, no shell, no user input
            [
                sys.executable,
                str(SCRIPT),
                "hammer",
                str(path),
                "wal",
                str(DEBITS_PER_PROCESS),
                "traj-1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(WRITERS)
    ]
    for child in children:
        _, errors = child.communicate(timeout=120)
        assert child.returncode == 0, errors.decode()

    expected_debits = WRITERS * DEBITS_PER_PROCESS
    assert tokens_in(engine, tables, "traj-1") == expected_debits * TOKENS_PER_DEBIT
    assert tokens_in(engine, tables, "2026-09-02") == expected_debits * TOKENS_PER_DEBIT
    with engine.connect() as connection:
        recorded = connection.execute(
            sa.select(sa.func.count()).select_from(tables.entries)
        ).scalar_one()
        unpriced = connection.execute(
            sa.select(tables.balances.c.unpriced_debit_count).where(
                tables.balances.c.window_key == "traj-1"
            )
        ).scalar_one()
    assert recorded == expected_debits
    assert unpriced == expected_debits
    engine.dispose()
