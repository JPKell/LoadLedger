"""Mountable tables and the durable ledger — ``loadledger[sql]`` (ADR-0050, spec §7).

This module ships **table shapes, not a database**. :func:`mount_ledger_tables` adds four tables
to a :class:`~sqlalchemy.MetaData` the *application* owns, so they appear in the application's own
``alembic revision --autogenerate`` beside the tables it wrote itself, upgrade with its own
history, and are backed up, restored and pruned by whoever owns that database. Two applications
mounting these tables have two tables in two databases — never one.

What this module deliberately does not have:

* **No engine, no URL, no session of its own.** :class:`SqlLedger` takes a callable returning a
  session and nothing else (ADR-0050 decision 3). It reads no environment variable, opens no file,
  and holds no connection between calls.
* **No migration history, and no ``create_all``.** Not on import, not lazily, not ever — a package
  that migrated an application's database would own half of a history nobody could reason about
  (ADR-0050 decision 5). Tests create tables; the library does not.
* **No sibling import.** In particular not ``weightsdb``, whose engine, pragma, migration-runner
  and backup machinery belongs to the application that owns the database (ADR-0050 decision 4,
  enforced by ``.importlinter``).

**Where the arithmetic lives.** Nowhere here. :class:`SqlLedger` is
:class:`~loadledger.memory.InMemoryLedger` with a different store: both evaluate through one
:class:`~loadledger.core.BalanceBook`, which is seeded from rows rather than from process memory.
Every honesty rule, every window boundary and every ``exceeded`` decision has exactly one
implementation, and a consumer that tested against the in-memory double tested the arithmetic it
will run in production.

**One observable difference, and it is ADR-0030 rule 1.** An entry read back through
:meth:`SqlLedger.entries` carries ``debit.cost is None``, because a
:class:`~baseaicore.CostEstimate` is not a stored fact: an entry's primary facts are its
``TokenUsage`` and its ``pricing_hash``, and the money is re-derived from those whenever a price is
corrected. What the ledger *decided* is not lost — every verdict is stored whole, with the money it
was decided on. See :meth:`SqlLedger.entries`.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from baseaicore import UNSUPPORTED, Money, TokenUsage, UlidGenerator, canonical_json
from sqlalchemy.dialects.postgresql import insert as _postgresql_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

from loadledger.core import (
    BalanceBook,
    ScopeKey,
    contribution_of,
    is_unpriced,
    resolved_debit,
)
from loadledger.errors import UnknownRun, UnsupportedDialect
from loadledger.types import (
    BudgetCeiling,
    CeilingScope,
    CeilingVerdict,
    Debit,
    LedgerEntry,
    PartialPricing,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from datetime import datetime

    from baseaicore import Clock, CostEstimate, TokenCount
    from sqlalchemy import MetaData, RowMapping, Table
    from sqlalchemy.orm import Session

    from loadledger.core import DebitContribution

__all__ = ["LedgerTables", "SqlLedger", "mount_ledger_tables"]

DEFAULT_TABLE_PREFIX = "ledger_"
"""The documented default prefix, part of the mounted contract (ADR-0050 consequences).

Changing it in a host that has already migrated is a table rename, not a configuration change.
"""

_USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_write_tokens",
    "cache_read": "cache_read_tokens",
}
"""Canonical usage key → :class:`~baseaicore.TokenUsage` field, for reading an entry back.

The canonical spelling is :meth:`baseaicore.TokenUsage.as_counts`'s, which is the one
``Debit.as_canonical`` writes; this mapping is its inverse and nothing else defines it.
"""


@dataclass(frozen=True, slots=True)
class LedgerTables:
    """The four tables :func:`mount_ledger_tables` added to a host's metadata (spec §7, §10).

    A handle, not a repository. The honest answer to "what does a host do with this?" is
    *hold it, or drop it on the floor*: the tables are already in the metadata the host passed,
    which is what autogenerate reads, so a host that mounts and discards the return value has lost
    nothing. It is returned so that a host can name the tables in a ``create_all(tables=...)``
    call, assert on the shapes in its own tests, or check the prefix it got — and so that
    :class:`SqlLedger` has one place to look them up.

    An application that reaches in for a :class:`~sqlalchemy.Table` in order to **join** it to one
    of its own entities is doing what ADR-0050 decision 2 forbids: these tables carry no foreign
    key out of the mounted set, ``run_id`` and ``source_ref`` are opaque strings, and a join
    against them freezes a shape this package is free to change under an upgrade note (spec §19).
    Read them through :class:`SqlLedger`.

    Attributes:
        prefix: The prefix every table, index and constraint in this set carries.
        entries: One row per recorded debit, with its verdicts. The append-only record.
        balances: One row per ``(scope, window_key)``: the token balance and the three honesty
            counts, maintained incrementally.
        balance_money: One row per ``(scope, window_key, currency)``: the nanos accumulated in
            that currency. Separate from :attr:`balances` because money is per-currency and the
            currency set is open — see :func:`mount_ledger_tables` for why that forces a table
            rather than a column.
        runs: One row per run this ledger has seen, declared or debited, so a run declared with
            nothing spent against it survives a restart (spec §13).
    """

    prefix: str
    entries: Table
    balances: Table
    balance_money: Table
    runs: Table

    @property
    def metadata(self) -> MetaData:
        """Return the host metadata these tables were mounted into.

        Reachable because it already is — ``tables.entries.metadata`` is the same object — and
        naming it here saves a host reaching through a table to find what it passed in.
        """
        return self.entries.metadata

    @property
    def all_tables(self) -> tuple[Table, ...]:
        """Return every mounted table, in dependency-safe creation order.

        The order a ``metadata.create_all(engine, tables=list(tables.all_tables))`` wants. There
        are no foreign keys between them (ADR-0050 decision 2), so any order would work; this one
        is stable so a test can assert on it.
        """
        return (self.entries, self.balances, self.balance_money, self.runs)


def mount_ledger_tables(metadata: MetaData, *, prefix: str = DEFAULT_TABLE_PREFIX) -> LedgerTables:
    """Add this package's tables to an application's metadata, and return handles to them.

    The whole of ADR-0050's mounting pattern. The application passes its **own**
    :class:`~sqlalchemy.MetaData` — the one its Alembic ``env.py`` names as ``target_metadata`` —
    and gets four tables in it that autogenerate sees exactly like tables the application wrote.
    Nothing is created here: no DDL is emitted, no connection is opened, and no migration is run
    (ADR-0050 decision 5).

    **Mount eagerly, at module import, in the host's model package.** Autogenerate only sees what
    was mounted before the metadata was inspected, so a host that mounts lazily — inside a request
    handler, or behind a feature flag — gets a migration that silently *drops* these tables. That
    is the named failure mode of this pattern, and it is why the miniature-host test in this
    repository autogenerates rather than merely creating tables.

    Why these four shapes — the table set and its keys are normative, in spec §10:

    * **Columns are plain and portable** — ``VARCHAR``, ``TEXT``, ``BIGINT``, ``BOOLEAN`` and
      ``TIMESTAMP WITH TIME ZONE``, and nothing else. No ORM base with domain meaning, no
      dialect-specific type, no ``JSONB``, no ``ENUM``, no foreign key leaving the set. A host's
      autogenerated revision must run unchanged on both supported dialects (ADR-0006), and a type
      that renders differently on each is how that stops being true.
    * **Every integer that accumulates is a ``BigInteger``.** This is the trap worth stating
      loudly. :class:`~baseaicore.Money` is a whole number of *nanos*, so one US dollar is
      1 000 000 000 and **$2.15 is 2 150 000 000 — already past a 4-byte integer's 2 147 483 647.**
      ``sa.Integer`` is 4 bytes on PostgreSQL, so a ceiling of a few dollars would raise
      ``DataError`` at a trivial spend; SQLite's dynamic typing stores the value regardless, so a
      SQLite-only test suite would never see it, and the first symptom would be in production on
      the other dialect. Token counts get the same width for the same reason: a ``PER_TAG``
      balance never resets and passes 2³¹ tokens without difficulty. **Any column added to these
      tables that accumulates is a ``BigInteger``.**
    * **Money is a table, not a column on the balance row.** A balance's money is *per currency*
      and the currency set is open, so the natural shape is a mapping — and a mapping in a JSON
      column cannot be incremented by the single atomic ``UPDATE … SET n = n + :delta`` statement
      that makes concurrent debits safe on both dialects without a read-modify-write. The extra
      table buys the concurrency story in :class:`SqlLedger`; see its class docstring.
    * **Instants are ``DateTime(timezone=True)``, and the ledger normalizes to UTC before
      binding.** SQLite has no timezone-aware storage: it writes whatever wall clock it is handed
      and returns a *naive* value. Storing UTC and attaching UTC on the way out makes the two
      dialects agree, keeps a string comparison on SQLite ordering correctly, and keeps the
      autogenerated revision free of a custom ``TypeDecorator`` the host would have to import.
    * **Records are ``TEXT`` holding canonical JSON**, not ``sa.JSON``. The bytes stored are the
      bytes spec contract 4 golden-tests, which makes "re-costing changed no stored row" a byte
      comparison rather than an interpretation; ``sa.JSON`` would store whatever the driver's
      ``json.dumps`` produced instead. It also sidesteps a real PostgreSQL trap: the ``json`` type
      has no equality operator, so ``SELECT DISTINCT`` or ``WHERE col = :x`` on such a column
      fails there and succeeds on SQLite. **The cost is real**: no server-side JSON operators and
      no index into a record's fields, on either dialect. A host that needs to query inside a
      verdict adds its own column, index or view in its own migration — it must not change these.

    Args:
        metadata: The application's own :class:`~sqlalchemy.MetaData`. Mutated: four tables are
            added to it.
        prefix: The string every table, index and primary-key constraint name begins with.
            Configurable so two mounts can coexist in one metadata, and so a host with a
            colliding name can move out of the way; collisions are the host's to avoid. Index
            names are global per schema on PostgreSQL, which is why they carry the prefix too.

    Returns:
        A :class:`LedgerTables` naming the four tables just added.

    Raises:
        ValueError: If ``prefix`` is empty or is not a plain SQL identifier prefix
            (``[A-Za-z_][A-Za-z0-9_]*``). An empty prefix would mount a table called ``entries``
            into an application's schema, which is a collision waiting for a second package.
        sqlalchemy.exc.InvalidRequestError: If a table of the same name is already in
            ``metadata`` — mounting the same prefix twice, usually because a host's model module
            was imported under two names. Mount once, at import.
    """
    if not prefix or not prefix[0].isascii() or not (prefix[0].isalpha() or prefix[0] == "_"):
        raise ValueError(
            f"mount_ledger_tables(prefix=...) must be a non-empty SQL identifier prefix "
            f"beginning with a letter or underscore; got {prefix!r}. An empty prefix would mount "
            f"a table named 'entries' into the application's schema."
        )
    if not all(
        character.isascii() and (character.isalnum() or character == "_") for character in prefix
    ):
        raise ValueError(
            f"mount_ledger_tables(prefix=...) must contain only ASCII letters, digits and "
            f"underscores; got {prefix!r}."
        )

    entries = sa.Table(
        f"{prefix}entries",
        metadata,
        sa.Column("entry_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unpriced", sa.Boolean(), nullable=False),
        sa.Column("pricing_hash", sa.String(), nullable=True),
        sa.Column("debit_json", sa.Text(), nullable=False),
        sa.Column("verdicts_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("entry_id", name=f"pk_{prefix}entries"),
        # (run_id, entry_id) rather than (run_id): entries are returned in entry_id order, so the
        # composite index answers the per-run history query from the index alone.
        sa.Index(f"ix_{prefix}entries_run", "run_id", "entry_id"),
        sa.Index(f"ix_{prefix}entries_occurred_at", "occurred_at"),
    )
    balances = sa.Table(
        f"{prefix}balances",
        metadata,
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("window_key", sa.String(), nullable=False),
        sa.Column("tokens_spent", sa.BigInteger(), nullable=False),
        sa.Column("unpriced_debit_count", sa.BigInteger(), nullable=False),
        sa.Column("untotalled_debit_count", sa.BigInteger(), nullable=False),
        sa.Column("unmetered_debit_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "window_key", name=f"pk_{prefix}balances"),
    )
    balance_money = sa.Table(
        f"{prefix}balance_money",
        metadata,
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("window_key", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("nanos_spent", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "scope", "window_key", "currency", name=f"pk_{prefix}balance_money"
        ),
    )
    runs = sa.Table(
        f"{prefix}runs",
        metadata,
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name=f"pk_{prefix}runs"),
    )
    return LedgerTables(
        prefix=prefix,
        entries=entries,
        balances=balances,
        balance_money=balance_money,
        runs=runs,
    )


class SqlLedger:
    """A ledger over tables an application mounted in its own database (spec §7).

    Observably :class:`~loadledger.memory.InMemoryLedger` with a different store. Every window
    boundary, honesty count and ``exceeded`` decision comes from the same
    :class:`~loadledger.core.BalanceBook`; this class only moves balances between that book and
    the host's rows. The one difference a caller can see is documented on :meth:`entries`.

    **Stateless and cheap to construct.** Nothing is cached between calls, so an application
    resolves its ceiling set *per operation* — the configured defaults, this run's own budget,
    this project's cap — and builds a view with it. That is only sound because the persisted
    balance key is ``(scope, window_key)`` and knows nothing about ceilings: a ceiling added
    tomorrow binds on the whole history, not on the history since it was configured.

    ## Concurrency: two processes debiting the same window

    A budget is one of the few places where the textbook lost update loses something that matters,
    so this is stated rather than assumed. **No balance is ever read, modified in Python and
    written back.** Each touched balance is advanced by a single statement —
    ``INSERT … ON CONFLICT (scope, window_key) DO UPDATE SET tokens_spent = tokens_spent +
    excluded.tokens_spent`` — which both supported dialects execute atomically, creating the row
    if it is new and incrementing it under the row lock if it is not. Two processes debiting the
    same window therefore add; neither overwrites.

    The statement order inside :meth:`debit` is part of the correctness argument and is not
    incidental:

    1. Every **write** is issued first, balances included.
    2. Only then are the balances **read back**, to compute the verdicts.

    On PostgreSQL at ``READ COMMITTED`` the upsert takes the row lock, a concurrent writer blocks
    on it until this transaction commits, and the read-back sees this transaction's own values —
    so each debit's verdicts are a self-consistent snapshot and the totals are exact. On SQLite the
    ordering matters more: pysqlite begins a transaction at the first DML statement, so a ``SELECT``
    issued *before* any write would run outside the transaction and could read a value another
    writer was about to change. Writing first opens the write transaction immediately; a concurrent
    writer waits on it (pysqlite's ``timeout``, five seconds by default) rather than racing. Rows
    are also upserted in sorted key order, giving every process the same lock order, so two debits
    touching two windows in common cannot deadlock.

    What this does **not** promise: serializable isolation. Two debits committing concurrently may
    each report a verdict that omits the other's spend, so a ceiling can be crossed by at most the
    concurrent in-flight debits before the next one sees it — the same "fires late" property a
    floor already has (ADR-0069), for a different reason. Both totals are recorded exactly and the
    next verdict is correct. A caller that must not cross a cap under concurrency serializes its
    own approvals, which is what PromptCadence's plan gate does, or uses
    :attr:`~loadledger.types.PartialPricing.STRICT` for the pricing half of the same problem.

    On SQLite specifically the host owns the engine, and two settings are its to make: a
    ``busy_timeout`` long enough for its write concurrency (pysqlite's five-second default is
    usually enough), and a journal mode. WAL lets readers run during a write and is the sensible
    choice for a ledger being polled by a UI; the atomicity contract holds under both WAL and the
    default rollback journal, and the test suite proves it under each.

    ## Sessions

    The factory must return a session **this ledger may own**: it commits it and closes it. A host
    that needs a debit inside its own unit of work passes a factory that returns a session joined
    to its transaction as a savepoint::

        session_factory=lambda: Session(bind=connection, join_transaction_mode="create_savepoint")

    in which case this class's ``commit`` releases the savepoint and the host's transaction still
    decides the outcome.
    """

    __slots__ = ("_book_ceilings", "_clock", "_ids", "_session_factory", "_tables")

    def __init__(
        self,
        session_factory: Callable[[], Session],
        ceilings: Sequence[BudgetCeiling],
        *,
        clock: Clock,
        table_prefix: str = DEFAULT_TABLE_PREFIX,
    ) -> None:
        """Build a ledger over a host's already-migrated tables.

        Args:
            session_factory: Returns a SQLAlchemy 2.0 :class:`~sqlalchemy.orm.Session` this ledger
                may commit and close. Injected, per ADR-0050 decision 3: this package opens no
                connection, reads no URL and holds no engine, so the application decides the
                dialect, the pool, the pragmas and the file.
            ceilings: The ceilings to evaluate, already validated by their own constructors. Order
                is preserved and is the order verdicts come back in. An empty sequence is
                legitimate — the ledger still accumulates and still answers :meth:`entries`, it
                simply never refuses.
            clock: Returns the current timezone-aware instant. Injected and required: a ledger
                that read the system clock could not be tested across a UTC midnight, which is the
                one boundary this package must get right.
            table_prefix: The prefix the host passed to :func:`mount_ledger_tables`. It must
                match; nothing here can check that it does until a statement fails, because the
                package never inspects the database's schema.

        Raises:
            ValueError: If ``table_prefix`` is not a valid identifier prefix — the same rule
                :func:`mount_ledger_tables` applies, checked here so a typo fails at construction
                rather than at the first debit.
        """
        self._session_factory = session_factory
        self._book_ceilings: tuple[BudgetCeiling, ...] = tuple(ceilings)
        self._clock: Clock = clock
        self._ids = UlidGenerator(clock=clock)
        # A private, throwaway MetaData, used only to build statements. The tables that exist are
        # the host's; these are the same shapes from the same function, and nothing here ever
        # emits DDL against them.
        self._tables = mount_ledger_tables(sa.MetaData(), prefix=table_prefix)

    @property
    def tables(self) -> LedgerTables:
        """Return the table shapes this ledger builds its statements against.

        The host's tables are separate objects in the host's own metadata; these are the identical
        shapes from :func:`mount_ledger_tables`, exposed so a test can assert the prefix matches
        what was mounted.
        """
        return self._tables

    def declare_run(self, run_id: str) -> None:
        """Register a run before anything has been debited against it.

        A run exists once it has been debited *or* declared (spec §13). Declaring one lets a caller
        ask what a fresh budget allows before spending anything, without :meth:`remaining` having
        to invent the difference between "this run has spent nothing" and "this run id is a typo".
        PromptCadence declares at trajectory creation, so its pre-flight never meets
        :class:`~loadledger.errors.UnknownRun`.

        Idempotent, and idempotent under concurrency: the insert is ``ON CONFLICT DO NOTHING``, so
        two processes declaring the same run at the same moment both succeed and the earlier
        ``declared_at`` stands.

        Args:
            run_id: The run identity to register. Opaque to this package.

        Raises:
            ValueError: If ``run_id`` is blank.
            UnsupportedDialect: If the session is bound to anything but SQLite or PostgreSQL.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"run_id must be a non-blank identifier; got {run_id!r}.")
        with self._writing() as session:
            self._note_run(session, run_id, at=self._clock())

    def debit(self, debit: Debit) -> LedgerEntry:
        """Record one debit and return it with every ceiling's verdict as of that debit.

        **One transaction.** The run record, every touched balance and the entry row carrying the
        verdicts commit together, so a crash can never leave spend on the books with no verdict
        beside it, nor a balance advanced with no entry to explain it (spec contract 5 — ADR-0044's
        shape applied to money). The currency check happens before anything is written, so a
        refused debit leaves no trace at all.

        ``PER_DAY`` verdicts are resolved against the debit's own ``occurred_at``, not against
        "now": a back-dated debit affects yesterday's window and the verdict it gets back describes
        the window it actually landed in.

        **Exceeding a ceiling is not an error.** The entry records ``exceeded=True`` verdicts and
        the debit stands; refusing work is the caller's policy, and :meth:`would_exceed` exists so
        it can refuse *before* spending (spec §13).

        Balances are advanced, never recomputed: the work is one upsert per window the debit
        touches plus one read per configured ceiling, independent of how long the run's history
        is. The debit into a run's ten-thousandth entry costs what the first one did.

        Args:
            debit: The debit to record. ``occurred_at`` is resolved from the injected clock when
                it is ``None``, and the stored row carries the resolved value.

        Returns:
            The stored :class:`~loadledger.types.LedgerEntry`, carrying the resolved debit — with
            the :class:`~baseaicore.CostEstimate` the caller passed still on it, unlike the copy
            :meth:`entries` reads back — the ``unpriced`` flag, the ``pricing_hash`` and one
            verdict per configured ceiling.

        Raises:
            CurrencyMismatch: If the debit is priced in a currency a money ceiling covering it caps
                in another currency. Refused, never converted (ADR-0030 rule 3), and refused
                before any row is written.
            UnsupportedDialect: If the session is bound to anything but SQLite or PostgreSQL.
        """
        occurred_at = debit.occurred_at if debit.occurred_at is not None else self._clock()
        resolved = resolved_debit(debit, occurred_at)

        book = BalanceBook(self._book_ceilings)
        # Before anything is written: a refused debit must leave no row behind.
        book.require_currency_compatible(
            resolved.cost, run_id=resolved.run_id, at=occurred_at, tags=resolved.tags
        )

        contribution = contribution_of(resolved.usage, resolved.cost)
        touched = BalanceBook.windows_touched(resolved.run_id, occurred_at, resolved.tags)
        entry_id = self._ids.new_id()

        with self._writing() as session:
            # Writes first, and in sorted key order — see the class docstring's concurrency note.
            self._note_run(session, resolved.run_id, at=occurred_at)
            for key in sorted(touched):
                self._advance_balance(session, key, contribution)
            # Only now read back, from inside the write transaction this ledger already holds.
            self._seed_from_rows(book, session, self._windows_read(resolved.run_id, occurred_at))
            verdicts = book.verdicts(run_id=resolved.run_id, at=occurred_at)
            entry = LedgerEntry(
                entry_id=entry_id,
                debit=resolved,
                unpriced=is_unpriced(resolved.cost),
                pricing_hash=resolved.cost.pricing_hash if resolved.cost is not None else None,
                verdicts=verdicts,
            )
            session.execute(
                sa.insert(self._tables.entries).values(
                    entry_id=entry.entry_id,
                    run_id=resolved.run_id,
                    source_ref=resolved.source_ref,
                    occurred_at=_as_utc(occurred_at),
                    unpriced=entry.unpriced,
                    pricing_hash=entry.pricing_hash,
                    debit_json=_dumps(resolved.as_canonical()),
                    verdicts_json=_dumps([verdict.as_canonical() for verdict in verdicts]),
                )
            )
        return entry

    def would_exceed(
        self,
        run_id: str,
        *,
        usage: TokenUsage | None = None,
        cost: CostEstimate | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[CeilingVerdict, ...]:
        """Report what every ceiling would say if this spend were recorded now.

        Side-effect-free (spec contract 6), and structurally so: the session this opens is rolled
        back and closed rather than committed, so even a future edit that wrote a row here could
        not persist one. In particular no balance row is lazily created for a window that has
        nothing in it — the easy way to break this contract, and the reason a missing row is read
        as an empty balance rather than inserted as a zero.

        Args:
            run_id: The run the prospective debit would belong to.
            usage: Its token counts, or ``None`` if not stated.
            cost: Its cost estimate, or ``None`` if no pricing was applied.
            tags: Its tags, deciding which ``PER_TAG`` ceilings it would land in.

        Returns:
            One verdict per configured ceiling, in configuration order, including the prospective
            spend. With both ``usage`` and ``cost`` ``None`` there is nothing prospective to add
            and the answer equals :meth:`remaining`.

        Raises:
            UnknownRun: If this ledger has never seen ``run_id``.
            CurrencyMismatch: If the prospective cost is in a currency a money ceiling covering it
                caps in another currency — so a caller learns before spending, not after.
            UnsupportedDialect: If the session is bound to anything but SQLite or PostgreSQL.
        """
        now = self._clock()
        with self._reading() as session:
            self._require_known(session, run_id)
            book = BalanceBook(self._book_ceilings)
            book.require_currency_compatible(cost, run_id=run_id, at=now, tags=tags)
            self._seed_from_rows(book, session, self._windows_read(run_id, now))
            return book.verdicts(run_id=run_id, at=now, usage=usage, cost=cost, tags=tags)

    def remaining(self, run_id: str) -> tuple[CeilingVerdict, ...]:
        """Report every ceiling's current balance for this run.

        ``PER_DAY`` ceilings are reported for the UTC day the injected clock is in — the window a
        debit made now would land in.

        Args:
            run_id: The run to report on.

        Returns:
            One verdict per configured ceiling, in configuration order.

        Raises:
            UnknownRun: If this ledger has never seen ``run_id``. Answering "nothing spent" for a
                mistyped run id would be indistinguishable from answering it for a real one.
            UnsupportedDialect: If the session is bound to anything but SQLite or PostgreSQL.
        """
        now = self._clock()
        with self._reading() as session:
            self._require_known(session, run_id)
            book = BalanceBook(self._book_ceilings)
            self._seed_from_rows(book, session, self._windows_read(run_id, now))
            return book.verdicts(run_id=run_id, at=now)

    def entries(
        self,
        *,
        run_id: str | None = None,
        tag: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[LedgerEntry]:
        """Return recorded entries, oldest first, narrowed by whichever filters are given.

        This is the input to a caller's own estimator and per-unit cost view (spec §6).

        **The one place a stored entry differs from the one :meth:`debit` returned** (spec §11
        contract 1): ``entry.debit.cost`` is ``None``, whatever it was when the debit was
        recorded. A
        :class:`~baseaicore.CostEstimate` is a derived figure, and ADR-0030 rule 1 says the stored
        facts are the ``TokenUsage`` and the ``pricing_hash`` — the money is re-derived from those
        whenever a price is corrected, which is the whole reason re-costing history is possible.
        Storing the estimate as well would put a second, contradictory serialization of a debit
        beside the canonical one, which deliberately omits it. Nothing about what the ledger
        *decided* is lost: each entry's verdicts are stored whole, with the money they were decided
        on, so the record still says what was refused and why. Read ``entry.unpriced`` and
        ``entry.pricing_hash`` for the pricing facts.

        Ordering is by ``entry_id``. Entry ids are ULIDs, lexicographically ordered by the instant
        they were minted, which is the instant the ledger recorded them — so this is insertion
        order for one writer and a stable total order for any number of them.

        Args:
            run_id: Keep only entries for this run. Pushed into SQL, and indexed.
            tag: Keep only entries whose debit carries this tag. Applied **in Python**, over the
                rows the other filters already narrowed: tags live inside the canonical debit
                record and the mounted set carries no tag index, because a per-tag *balance* needs
                no scan and a per-tag *history* is a secondary path. A host that needs an indexed
                tag query owns its own index and its own migration.
            since: Keep only entries at or after this instant. The window is half-open —
                ``occurred_at >= since`` — so two consecutive queries with touching bounds return
                each entry exactly once. Pushed into SQL, normalized to UTC first.

        Returns:
            A tuple, oldest first. Filters combine with AND.

        Raises:
            ValueError: If ``since`` is naive. Comparing a naive bound against stored UTC instants
                would silently shift the window by the reader's local offset.
        """
        if since is not None and (since.tzinfo is None or since.tzinfo.utcoffset(since) is None):
            raise ValueError(
                "entries(since=...) requires a timezone-aware instant; got a naive one."
            )
        table = self._tables.entries
        query = sa.select(table).order_by(table.c.entry_id.asc())
        if run_id is not None:
            query = query.where(table.c.run_id == run_id)
        if since is not None:
            query = query.where(table.c.occurred_at >= _as_utc(since))
        with self._reading() as session:
            rows = session.execute(query).mappings().all()
        recovered = tuple(_entry_from_row(row) for row in rows)
        if tag is None:
            return recovered
        return tuple(entry for entry in recovered if tag in entry.debit.tags)

    # -- internals -----------------------------------------------------------------------------

    @contextmanager
    def _writing(self) -> Iterator[Session]:
        """Yield a session whose work commits as one transaction, or not at all.

        The unit of work every mutating method runs inside. On any exception the transaction is
        rolled back before it propagates, which is what makes a refused or crashed debit leave no
        half-written balance behind.
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:  # noqa: BLE001 — re-raised; the rollback is the whole point
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def _reading(self) -> Iterator[Session]:
        """Yield a session that is rolled back on the way out, never committed.

        The mechanical half of spec contract 6: a read path physically cannot leave a row behind,
        so ``would_exceed`` stays side-effect-free even if a later edit forgets why it had to be.
        """
        session = self._session_factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def _windows_read(self, run_id: str, at: datetime) -> frozenset[ScopeKey]:
        """Return the balance windows the configured ceilings read, for this run at this instant.

        A subset of what a debit *writes*: a ``PER_TAG`` ceiling whose tag is not on this debit
        still reads its own window, and a window a ceiling reads may not exist at all — which is
        an empty balance, never a row to create.
        """
        return frozenset(
            BalanceBook.window_for(ceiling, run_id=run_id, at=at) for ceiling in self._book_ceilings
        )

    def _seed_from_rows(
        self, book: BalanceBook, session: Session, keys: frozenset[ScopeKey]
    ) -> None:
        """Load the named windows out of the host's tables and into ``book``.

        Windows with no row are left unseeded, which the book reads as an empty balance — the
        distinction ADR-0016 rests on. A currency with no row in ``balance_money`` is likewise
        absent rather than zero, so ``money_spent`` stays ``None`` until something is priced.
        """
        if not keys:
            return
        balances = self._tables.balances
        money = self._tables.balance_money
        nanos: dict[ScopeKey, dict[str, int]] = {}
        for row in session.execute(sa.select(money).where(_matches_any(money, keys))).mappings():
            key = (CeilingScope(row["scope"]), row["window_key"])
            nanos.setdefault(key, {})[row["currency"]] = row["nanos_spent"]
        for row in session.execute(
            sa.select(balances).where(_matches_any(balances, keys))
        ).mappings():
            key = (CeilingScope(row["scope"]), row["window_key"])
            book.seed(
                key,
                tokens_spent=row["tokens_spent"],
                nanos_by_currency=nanos.get(key, {}),
                unpriced_debit_count=row["unpriced_debit_count"],
                untotalled_debit_count=row["untotalled_debit_count"],
                unmetered_debit_count=row["unmetered_debit_count"],
            )

    def _advance_balance(
        self, session: Session, key: ScopeKey, contribution: DebitContribution
    ) -> None:
        """Add one debit's contribution to one window, in one atomic statement per table.

        Never a read-modify-write: the upsert creates the row or increments it in place under the
        row lock, so two processes debiting this window add rather than overwrite. The money row
        is written **only** when something was actually priced — an estimate that priced nothing
        must not bring a zero balance into existence, because ``money_spent`` distinguishes
        "nothing priced here" from "nothing spent here" (ADR-0016).
        """
        scope, window_key = key
        self._upsert_sum(
            session,
            self._tables.balances,
            keys={"scope": scope.value, "window_key": window_key},
            sums={
                "tokens_spent": contribution.tokens,
                "unpriced_debit_count": int(contribution.unpriced),
                "untotalled_debit_count": int(contribution.untotalled),
                "unmetered_debit_count": int(contribution.unmetered),
            },
        )
        if contribution.priced and contribution.currency is not None:
            self._upsert_sum(
                session,
                self._tables.balance_money,
                keys={
                    "scope": scope.value,
                    "window_key": window_key,
                    "currency": contribution.currency,
                },
                sums={"nanos_spent": contribution.nanos},
            )

    def _note_run(self, session: Session, run_id: str, *, at: datetime) -> None:
        """Record that this run exists, leaving an earlier ``declared_at`` alone."""
        insert = _insert_for(session)(self._tables.runs).values(
            run_id=run_id, declared_at=_as_utc(at)
        )
        session.execute(insert.on_conflict_do_nothing(index_elements=["run_id"]))

    def _upsert_sum(
        self,
        session: Session,
        table: Table,
        *,
        keys: Mapping[str, str],
        sums: Mapping[str, int],
    ) -> None:
        """Insert a row of sums, or add them to the row already there, in one statement."""
        insert = _insert_for(session)(table).values(**keys, **sums)
        session.execute(
            insert.on_conflict_do_update(
                index_elements=list(keys),
                set_={name: table.c[name] + insert.excluded[name] for name in sums},
            )
        )

    def _require_known(self, session: Session, run_id: str) -> None:
        """Raise :class:`~loadledger.errors.UnknownRun` unless this run has been seen."""
        runs = self._tables.runs
        found = session.execute(
            sa.select(runs.c.run_id).where(runs.c.run_id == run_id).limit(1)
        ).first()
        if found is None:
            raise UnknownRun(
                f"No run {run_id!r} in this ledger. A run exists once it has been debited or "
                "declared with declare_run(); reporting an empty balance for an unrecognised id "
                "would look exactly like reporting one for a run that has spent nothing.",
                details={"run_id": run_id},
            )


def _insert_for(session: Session) -> Any:
    """Return the dialect-specific ``insert()`` that supports ``ON CONFLICT``.

    The suite supports exactly two dialects and both spell this the same way in SQL —
    ``INSERT … ON CONFLICT (…) DO UPDATE SET x = x + excluded.x`` — but SQLAlchemy namespaces the
    construct per dialect, so the choice is made here, once, rather than at four call sites. There
    is no generic form in SQLAlchemy 2.0, and the alternatives (``UPDATE`` then ``INSERT`` on zero
    rows affected, or ``INSERT`` and catch the integrity error) each reintroduce a race on a new
    window and need a savepoint to recover on PostgreSQL.

    Args:
        session: The session whose bind names the dialect.

    Returns:
        ``sqlalchemy.dialects.sqlite.insert`` or ``sqlalchemy.dialects.postgresql.insert``.

    Raises:
        UnsupportedDialect: For anything else. ADR-0006 admits SQLite and PostgreSQL and nothing
            else, so a third dialect is refused loudly here rather than discovered as a syntax
            error inside a money transaction.
    """
    name = session.get_bind().dialect.name
    if name == "sqlite":
        return _sqlite_insert
    if name == "postgresql":
        return _postgresql_insert
    raise UnsupportedDialect(
        f"LoadLedger supports SQLite and PostgreSQL; this session is bound to {name!r}. "
        "The suite runs on exactly two dialects (ADR-0006), and the balance upsert this ledger "
        "depends on is written for both of them.",
        details={"dialect": name},
    )


def _matches_any(table: Table, keys: frozenset[ScopeKey]) -> sa.ColumnElement[bool]:
    """Build a ``WHERE`` matching any of these ``(scope, window_key)`` pairs.

    An ``OR`` of ``AND``s rather than a row-value ``IN``: the key set is one entry per configured
    ceiling — a handful — and row-value comparison is a SQLite version dependency this package has
    no reason to take on.
    """
    return sa.or_(
        *(
            sa.and_(table.c.scope == scope.value, table.c.window_key == window_key)
            for scope, window_key in keys
        )
    )


def _dumps(value: object) -> str:
    """Serialize a canonical mapping to the exact bytes spec contract 4 golden-tests."""
    return canonical_json(value)


def _as_utc(when: datetime) -> datetime:
    """Return ``when`` as a timezone-aware UTC instant, for binding to a column.

    Normalizing before the bind is what makes the two dialects agree. SQLite has no timezone-aware
    storage: it writes the wall clock of whatever offset it is handed and drops the offset, so an
    instant bound as ``23:30-05:00`` would come back as ``23:30`` on the wrong day. It also makes
    the stored string sort correctly, which is what the ``since`` filter and the ``occurred_at``
    index rely on.
    """
    return when.astimezone(UTC)


def _from_utc(when: datetime) -> datetime:
    """Return a stored instant as timezone-aware UTC.

    PostgreSQL hands back an aware value; SQLite hands back a naive one that *is* UTC, because
    :func:`_as_utc` normalized it on the way in. Attaching the timezone here rather than trusting
    the driver is what keeps a round-tripped ``occurred_at`` comparable to the one that went in,
    on both dialects.
    """
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def _count_from_canonical(value: object) -> TokenCount:
    """Return a canonical usage count as a number or :data:`~baseaicore.UNSUPPORTED`."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return UNSUPPORTED


def _money_from_canonical(value: object) -> Money | None:
    """Rebuild a :class:`~baseaicore.Money` from its canonical mapping, or ``None``."""
    if value is None:
        return None
    mapping = dict(value)  # type: ignore[call-overload] # a canonical Money is always a mapping
    return Money(currency=str(mapping["currency"]), nanos=int(mapping["nanos"]))


@lru_cache(maxsize=256)
def _ceiling_from_fields(
    scope: str,
    currency: str | None,
    nanos: int | None,
    tokens: int | None,
    tag: str | None,
    partial_pricing: str,
) -> BudgetCeiling:
    """Rebuild one ceiling from its primitive fields, reusing the object across entries.

    Cached because a history is one ceiling repeated: reading ten thousand entries under three
    ceilings rebuilds the same three caps thirty thousand times, and each rebuild re-runs
    :class:`~loadledger.types.BudgetCeiling`'s validation and constructs two
    :class:`~baseaicore.Money` objects. Sharing them is safe precisely because a ceiling is a
    frozen value object — two equal ceilings are interchangeable, so the cache changes how much
    work a read costs and nothing about what it returns.

    The cache is bounded and keyed only on the fields, so it holds a handful of small immutable
    objects for the life of the process and can never grow with the size of a history.
    """
    return BudgetCeiling(
        scope=CeilingScope(scope),
        money=None if currency is None or nanos is None else Money(currency=currency, nanos=nanos),
        tokens=tokens,
        tag=tag,
        partial_pricing=PartialPricing(partial_pricing),
    )


def _ceiling_from_canonical(document: Mapping[str, Any]) -> BudgetCeiling:
    """Rebuild the ceiling a stored verdict was decided under.

    Rebuilt from the verdict's own record, never looked up in the ledger's current configuration:
    a verdict read back after its ceiling was removed, retuned or renamed must still describe the
    cap that actually produced it. That is why the whole ceiling is serialized into every verdict
    rather than a reference to one.
    """
    money = document["money"]
    tokens = document["tokens"]
    return _ceiling_from_fields(
        document["scope"],
        None if money is None else str(money["currency"]),
        None if money is None else int(money["nanos"]),
        None if tokens is None else int(tokens),
        document["tag"],
        document["partial_pricing"],
    )


def _verdict_from_canonical(document: Mapping[str, Any]) -> CeilingVerdict:
    """Rebuild one stored :class:`~loadledger.types.CeilingVerdict`."""
    remaining = document["tokens_remaining"]
    return CeilingVerdict(
        ceiling=_ceiling_from_canonical(document["ceiling"]),
        exceeded=bool(document["exceeded"]),
        money_spent=_money_from_canonical(document["money_spent"]),
        money_remaining=_money_from_canonical(document["money_remaining"]),
        tokens_spent=int(document["tokens_spent"]),
        tokens_remaining=None if remaining is None else int(remaining),
        unpriced_debit_count=int(document["unpriced_debit_count"]),
        untotalled_debit_count=int(document["untotalled_debit_count"]),
        unmetered_debit_count=int(document["unmetered_debit_count"]),
    )


def _entry_from_row(row: RowMapping) -> LedgerEntry:
    """Rebuild one :class:`~loadledger.types.LedgerEntry` from its stored row.

    The canonical ``debit_json`` is the record; the ``run_id``, ``source_ref`` and ``occurred_at``
    columns beside it are a projection of the same facts, kept as columns so they can be indexed
    and filtered. A test asserts the two agree, because a projection that could drift from its
    record is a defect waiting for a query to find it.
    """
    debit_document = json.loads(row["debit_json"])
    usage = TokenUsage(
        **{
            field: _count_from_canonical(debit_document["usage"][name])
            for name, field in _USAGE_FIELDS.items()
        }
    )
    return LedgerEntry(
        entry_id=row["entry_id"],
        debit=Debit(
            run_id=debit_document["run_id"],
            source_ref=debit_document["source_ref"],
            usage=usage,
            cost=None,  # ADR-0030 rule 1: usage and the hash are stored; the money is re-derived.
            tags=tuple(debit_document["tags"]),
            occurred_at=_from_utc(row["occurred_at"]),
        ),
        unpriced=bool(row["unpriced"]),
        pricing_hash=row["pricing_hash"],
        verdicts=tuple(
            _verdict_from_canonical(document) for document in json.loads(row["verdicts_json"])
        ),
    )
