"""Spec contract 5: a debit and its verdicts commit together, or neither of them does.

ADR-0044's shape applied to money. The proof is a killed process, not a review: a balance advanced
without an entry to explain it, or an entry written without the verdicts that describe it, is
discovered months later as a budget that did not bind, in a record nobody can reconstruct.

Two fault points, because they fail differently:

* ``entry_insert`` — every balance has been advanced and the entry is about to be written. A
  two-transaction implementation has already committed the balances by this moment.
* ``before_commit`` — everything is written and nothing is committed. The classic.

Two journal modes, because what a killed SQLite process leaves on disk depends on which one is in
force: a rollback journal (``delete``, the default) is replayed by the next connection, WAL is
recovered from the log. The contract has to hold under both, and the host — which owns the engine
— is free to choose either.

PostgreSQL is covered by the same assertions through the ``engine`` fixture in
``test_sql_ledger.py``'s transaction tests and by CI's ``db-matrix`` job; the killed-process form
is SQLite-only here because it needs a database file this process can open afterwards, and the
PostgreSQL equivalent is the server rolling back an aborted backend, which is the server's
contract rather than this package's.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from conftest import ManualClock, mounted, session_factory_for
from ledger_subprocess import CEILINGS, TOKENS_PER_DEBIT, WHEN, a_debit
from loadledger.sql import SqlLedger

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from loadledger.sql import LedgerTables

SCRIPT = Path(__file__).parent / "ledger_subprocess.py"

JOURNAL_MODES = ["delete", "wal"]
"""SQLite's default rollback journal, and WAL. Both are journal modes a host may choose."""


def rows_of(engine: Engine, tables: LedgerTables) -> dict[str, list[tuple[object, ...]]]:
    """Every row in the mounted set, keyed by table name."""
    with engine.connect() as connection:
        return {
            table.name: [
                tuple(row) for row in connection.execute(sa.select(table).order_by(*table.c))
            ]
            for table in tables.all_tables
        }


def a_populated_ledger(path: Path, journal_mode: str) -> tuple[Engine, LedgerTables]:
    """A database with one debit already in it, so "unchanged" is a meaningful assertion."""
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(f"PRAGMA journal_mode={journal_mode}")
    _, tables = mounted(engine)
    ledger = SqlLedger(session_factory_for(engine), CEILINGS, clock=lambda: WHEN)
    ledger.debit(a_debit("traj-survivor", 0))
    return engine, tables


def kill_a_debit(path: Path, journal_mode: str, where: str, mode: str) -> None:
    """Run the child that dies mid-debit, and assert it really did die by ``SIGKILL``."""
    finished = subprocess.run(  # noqa: S603 — a fixed argv, no shell, no user input
        [sys.executable, str(SCRIPT), "fault", str(path), journal_mode, where, mode],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert finished.returncode == -signal.SIGKILL, (
        f"the child exited {finished.returncode} rather than being killed at {where!r}; "
        f"the fault point was never reached.\n{finished.stderr.decode()}"
    )


@pytest.mark.parametrize("journal_mode", JOURNAL_MODES)
@pytest.mark.parametrize("where", ["entry_insert", "before_commit"])
def test_a_debit_killed_mid_write_leaves_the_ledger_exactly_as_it_was(
    tmp_path: Path, journal_mode: str, where: str
) -> None:
    path = tmp_path / "ledger.sqlite3"
    engine, tables = a_populated_ledger(path, journal_mode)
    before = rows_of(engine, tables)
    engine.dispose()

    kill_a_debit(path, journal_mode, where, "atomic")

    # Reopen the file the killed process left behind: whatever recovery the journal mode does,
    # this is what a restarted application would see.
    reopened = sa.create_engine(f"sqlite:///{path}")
    after = rows_of(reopened, tables)
    reopened.dispose()

    assert after == before, (
        f"a debit killed at {where!r} under journal_mode={journal_mode} left the ledger changed"
    )
    assert not any(row[0] == "traj-killed" for row in after["ledger_runs"])
    assert all(row[2] == TOKENS_PER_DEBIT for row in after["ledger_balances"]), (
        "a balance advanced for a debit that was never recorded"
    )


@pytest.mark.parametrize("journal_mode", JOURNAL_MODES)
def test_the_same_check_catches_an_implementation_that_uses_two_transactions(
    tmp_path: Path, journal_mode: str
) -> None:
    """The test above is only evidence if it can fail. This is the proof that it can.

    ``ledger_subprocess.broken_debit`` is the mistake this phase exists to avoid: balances
    committed in one transaction, the entry in a second. Killed at the same fault point, it leaves
    a ledger whose balances say a debit happened and whose entries do not — the exact defect
    contract 5 forbids, and the assertions above catch it.
    """
    path = tmp_path / "ledger.sqlite3"
    engine, tables = a_populated_ledger(path, journal_mode)
    before = rows_of(engine, tables)
    engine.dispose()

    kill_a_debit(path, journal_mode, "entry_insert", "broken")

    reopened = sa.create_engine(f"sqlite:///{path}")
    after = rows_of(reopened, tables)
    reopened.dispose()

    assert after != before
    assert any(row[0] == "traj-killed" for row in after["ledger_runs"])
    assert any(row[2] == TOKENS_PER_DEBIT * 2 for row in after["ledger_balances"]), (
        "the broken variant was supposed to commit a balance without its entry"
    )
    assert len(after["ledger_entries"]) == 1, "no entry should have been committed"


def test_a_debit_that_raises_mid_flight_rolls_the_whole_thing_back(engine: Engine) -> None:
    """The same contract without a signal: an exception inside the transaction unwinds all of it.

    This one runs on both dialects, which the killed-process form cannot, and it covers the
    failure that actually happens in production — a disk error, a dropped connection, a constraint
    — rather than the one that is easiest to stage. The fault is injected through the host's own
    engine, the same way ``ledger_subprocess`` does it and for the same reason: the library gets no
    test-only seam.
    """
    _, tables = mounted(engine)
    ledger = SqlLedger(session_factory_for(engine), CEILINGS, clock=ManualClock(WHEN))
    ledger.debit(a_debit("traj-1", 0))
    before = rows_of(engine, tables)

    class Interrupted(Exception):
        """Whatever goes wrong between the balances and the entry."""

    @event.listens_for(engine, "before_cursor_execute")
    def _fail_on_the_entry(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "INSERT INTO ledger_entries" in statement:
            raise Interrupted(statement)

    try:
        with pytest.raises(Interrupted):
            ledger.debit(a_debit("traj-1", 1))
    finally:
        event.remove(engine, "before_cursor_execute", _fail_on_the_entry)

    assert rows_of(engine, tables) == before
