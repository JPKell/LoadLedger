"""A child process that debits — and, on request, dies in the middle of doing it.

Run as a script, never imported by a test module (the name has no ``test_`` prefix, so pytest does
not collect it). It exists because the two properties it supports cannot be observed inside the
process that asserts them: a ``SIGKILL`` has to land somewhere, and "two processes" has to be two
processes.

**The fault point is not a seam in the library.** ``SqlLedger`` has no ``_after_insert`` hook, no
debug flag and no test-only branch: a hook in a production signature is a hook a future caller can
reach, and the atomicity contract would then be guarded by a promise not to use it. Instead this
script installs a SQLAlchemy **event listener on its own engine** — which is exactly the seam
ADR-0050 decision 3 already gives it, since the host owns the engine and the session factory. The
library is unmodified and unaware.

Usage::

    python ledger_subprocess.py fault  <db-path> <journal-mode> <entry_insert|before_commit> \\
                                       <atomic|broken>
    python ledger_subprocess.py hammer <db-path> <journal-mode> <debit-count> <run-id>
"""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime

import sqlalchemy as sa
from baseaicore import TokenUsage, canonical_json
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from loadledger import BudgetCeiling, CeilingScope, Debit, LedgerEntry
from loadledger.core import BalanceBook, contribution_of, is_unpriced, resolved_debit
from loadledger.sql import SqlLedger

WHEN = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
"""The one instant every debit here happens at, so every debit lands in one set of windows."""

CEILINGS = [
    BudgetCeiling(scope=CeilingScope.PER_RUN, tokens=10_000_000),
    BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=10_000_000),
]

USAGE = TokenUsage(input_tokens=10, output_tokens=1, cache_write_tokens=0, cache_read_tokens=0)
TOKENS_PER_DEBIT = 11


def engine_for(path: str, journal_mode: str) -> sa.Engine:
    """An engine on a real SQLite file, with the journal mode the test asked for.

    The journal mode is the host's to choose (this package owns no engine), and it changes what a
    killed process leaves behind: a rollback journal is replayed by the next connection, WAL is
    recovered from the write-ahead log. The atomicity contract has to hold under both, so the test
    runs under both.
    """
    made = sa.create_engine(f"sqlite:///{path}")

    @event.listens_for(made, "connect")
    def _apply_journal_mode(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined] # a DBAPI connection
        cursor.execute(f"PRAGMA journal_mode={journal_mode}")
        cursor.close()

    return made


def die_now() -> None:
    """Leave immediately, with no unwinding: no ``finally``, no rollback, no close."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def arm_fault(engine: sa.Engine, where: str, prefix: str) -> None:
    """Install the fault point on the caller's own engine or session class."""
    if where == "entry_insert":
        # After every balance has been advanced, before the entry that explains them is written.
        @event.listens_for(engine, "before_cursor_execute")
        def _kill_before_the_entry(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if f"INSERT INTO {prefix}entries" in statement:
                die_now()

    elif where == "before_commit":
        # Everything written, nothing committed — the classic shape of the crash that matters.
        @event.listens_for(Session, "before_commit")
        def _kill_before_the_commit(_session: Session) -> None:
            die_now()

    else:  # pragma: no cover — the caller is this file's own test module
        raise SystemExit(f"unknown fault point {where!r}")


class TwoTransactionLedger(SqlLedger):
    """``SqlLedger`` with the entry moved into a second transaction — the mistake, written down.

    Everything else is inherited, so the *only* difference between this and the real thing is
    where the transaction boundary falls. That is what makes it a fair test of the assertion: if
    the atomicity test passes against this class as well, the assertion is not detecting
    atomicity, it is detecting something else.
    """

    def debit(self, debit: Debit) -> LedgerEntry:
        """Advance the balances, commit, and only then write the entry that explains them."""
        occurred_at = debit.occurred_at if debit.occurred_at is not None else self._clock()
        resolved = resolved_debit(debit, occurred_at)
        book = BalanceBook(self._book_ceilings)
        contribution = contribution_of(resolved.usage, resolved.cost)
        touched = BalanceBook.windows_touched(resolved.run_id, occurred_at, resolved.tags)

        with self._writing() as session:  # transaction one: the money moves
            self._note_run(session, resolved.run_id, at=occurred_at)
            for key in sorted(touched):
                self._advance_balance(session, key, contribution)
            self._seed_from_rows(book, session, self._windows_read(resolved.run_id, occurred_at))
            verdicts = book.verdicts(run_id=resolved.run_id, at=occurred_at)

        entry = LedgerEntry(
            entry_id=self._ids.new_id(),
            debit=resolved,
            unpriced=is_unpriced(resolved.cost),
            pricing_hash=None,
            verdicts=verdicts,
        )
        with self._writing() as session:  # transaction two: the record nobody will ever see
            session.execute(
                sa.insert(self._tables.entries).values(
                    entry_id=entry.entry_id,
                    run_id=resolved.run_id,
                    source_ref=resolved.source_ref,
                    occurred_at=occurred_at,
                    unpriced=entry.unpriced,
                    pricing_hash=None,
                    debit_json=canonical_json(resolved.as_canonical()),
                    verdicts_json=canonical_json([v.as_canonical() for v in verdicts]),
                )
            )
        return entry


def a_debit(run_id: str, index: int) -> Debit:
    """One identical debit, so a total is the only thing a test has to count."""
    return Debit(
        run_id=run_id,
        source_ref=f"turn-{index}",
        usage=USAGE,
        cost=None,
        occurred_at=WHEN,
    )


def run_fault(path: str, journal_mode: str, where: str, mode: str) -> None:
    """Debit once, dying at ``where``. Never returns when the fault point is reached."""
    engine = engine_for(path, journal_mode)
    arm_fault(engine, where, "ledger_")
    build = TwoTransactionLedger if mode == "broken" else SqlLedger
    ledger = build(sessionmaker(bind=engine), CEILINGS, clock=lambda: WHEN)
    ledger.debit(a_debit("traj-killed", 1))
    raise SystemExit("the fault point was never reached")


def run_hammer(path: str, journal_mode: str, count: int, run_id: str) -> None:
    """Debit ``count`` times against a window another process is debiting at the same moment."""
    engine = engine_for(path, journal_mode)
    ledger = SqlLedger(sessionmaker(bind=engine), CEILINGS, clock=lambda: WHEN)
    for index in range(count):
        ledger.debit(a_debit(run_id, index))
    engine.dispose()


def main(argv: list[str]) -> None:
    """Dispatch on the first argument. See the module docstring for the two call shapes."""
    if argv[1] == "fault":
        run_fault(argv[2], argv[3], argv[4], argv[5])
    elif argv[1] == "hammer":
        run_hammer(argv[2], argv[3], int(argv[4]), argv[5])
    else:  # pragma: no cover — the caller is this file's own test module
        raise SystemExit(f"unknown mode {argv[1]!r}")


if __name__ == "__main__":
    main(sys.argv)
