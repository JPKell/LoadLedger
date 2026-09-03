"""The miniature host: mount, autogenerate, upgrade, debit, query — on both dialects.

ADR-0050 decision 6 in full. The host in ``tests/integration/hostapp`` is a real Alembic project
with its own metadata, its own table and its own ``versions/`` directory; these tests drive it the
way a person would, and then check the two things the development plan names as likely to go
wrong:

* **autogenerate emitting dialect-specific types** — checked on the *generated revision*, not on
  the metadata, and then proven by rendering that revision's DDL for the other dialect. Rendering
  is done through Alembic's offline mode, which needs no server, so the cross-dialect half of this
  runs on a machine with no PostgreSQL.
* **a host that mounts too late** — proven to be a real hazard by autogenerating against a
  metadata that has not mounted, and watching Alembic decide to drop the ledger.
"""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from _pytest.outcomes import Failed, Skipped
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from baseaicore import Money, TokenUsage

from conftest import MIDDAY, ManualClock, postgres_url, session_factory_for
from hostapp.models import TABLE_PREFIX
from loadledger import BudgetCeiling, CeilingScope, Debit
from loadledger.sql import SqlLedger

if TYPE_CHECKING:
    from sqlalchemy import Engine

HOSTAPP = Path(__file__).parent / "hostapp"

EXPECTED_TABLES = {
    "host_notes",
    f"{TABLE_PREFIX}entries",
    f"{TABLE_PREFIX}balances",
    f"{TABLE_PREFIX}balance_money",
    f"{TABLE_PREFIX}runs",
}

DIALECT_SPECIFIC = re.compile(r"\b(postgresql|sqlite|mysql|oracle|mssql)\.", re.IGNORECASE)
"""What a dialect-specific type looks like in a generated revision: ``postgresql.JSONB()``."""

PORTABLE_TYPE_CALLS = {
    "String",
    "Text",
    "BigInteger",
    "Boolean",
    "DateTime",
    "VARCHAR",
    "TEXT",
    "BIGINT",
    "BOOLEAN",
    "DATETIME",
    "TIMESTAMP",
}
"""Generic SQLAlchemy types Alembic is allowed to have written into the host's revision.

Alembic renders a reflected type by its DDL name (``sa.VARCHAR()``) and a metadata type by its
generic name (``sa.String()``); both spellings are portable, and both appear depending on which
side of the comparison a column came from.
"""


def host_config(script_location: Path, url: str) -> Config:
    """Build the Alembic configuration a host would keep in its ``alembic.ini``."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", url)
    return config


def a_host_project(tmp_path: Path, url: str, name: str = "host") -> Config:
    """Copy the miniature host's migration tree somewhere writable and point Alembic at it.

    Copied per test because ``revision --autogenerate`` writes a file into ``versions/``, and a
    repository whose checked-in ``versions/`` filled up as tests ran would be a repository whose
    tests depended on each other.
    """
    destination = tmp_path / name / "migrations"
    shutil.copytree(HOSTAPP / "migrations", destination)
    for stale in (destination / "versions").glob("*.py"):
        stale.unlink()
    return host_config(destination, url)


def sole_revision(config: Config) -> Path:
    """Return the one revision file the host's autogenerate produced."""
    versions = Path(ScriptDirectory.from_config(config).versions)
    (script,) = sorted(versions.glob("*.py"))
    return script


def test_the_host_autogenerates_upgrades_debits_and_queries(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    config = a_host_project(tmp_path, database_url)
    command.revision(config, message="initial", autogenerate=True)
    command.upgrade(config, "head")

    # The host's own table and all four mounted ones, in the host's single history.
    assert set(sa.inspect(engine).get_table_names()) >= EXPECTED_TABLES | {"alembic_version"}

    ledger = SqlLedger(
        session_factory_for(engine),
        [
            BudgetCeiling(
                scope=CeilingScope.PER_RUN,
                money=Money.from_decimal("USD", "5.00"),
                tokens=2_000_000,
            )
        ],
        clock=ManualClock(),
        table_prefix=TABLE_PREFIX,
    )
    ledger.declare_run("traj-1")
    entry = ledger.debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=TokenUsage(input_tokens=1_200, output_tokens=340),
            cost=None,
            occurred_at=MIDDAY,
        )
    )
    assert entry.verdicts[0].tokens_spent == 1_540
    assert [read.entry_id for read in ledger.entries(run_id="traj-1")] == [entry.entry_id]

    # And the host's own table still works beside them, in the same database.
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO host_notes (note_id, body) VALUES ('n1', 'hello')"))
        assert connection.execute(sa.text("SELECT body FROM host_notes")).scalar() == "hello"


def test_the_generated_revision_names_every_mounted_table_and_no_dialect_type(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    del engine  # required so the database exists; the assertion is on the generated file
    config = a_host_project(tmp_path, database_url)
    command.revision(config, message="initial", autogenerate=True)
    body = sole_revision(config).read_text(encoding="utf-8")

    for table in EXPECTED_TABLES:
        assert f"'{table}'" in body, f"autogenerate did not see {table}"
    assert not DIALECT_SPECIFIC.search(body), (
        "the host's generated revision names a dialect-specific type; it would then run on one "
        f"dialect only:\n{body}"
    )
    for call in set(re.findall(r"\bsa\.([A-Za-z_]+)\(", body)):
        if call in {"Column", "PrimaryKeyConstraint", "UniqueConstraint", "Index"}:
            continue
        assert call in PORTABLE_TYPE_CALLS, f"unexpected type sa.{call}() in the revision"

    # The two prefixed index names the plan warns about, spelled out in the migration.
    assert f"ix_{TABLE_PREFIX}entries_run" in body
    assert f"ix_{TABLE_PREFIX}entries_occurred_at" in body


def test_a_revision_generated_on_one_dialect_renders_for_the_other(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    """The point of the portability check: the *revision* runs elsewhere, not just here.

    Alembic's offline mode renders DDL for whatever dialect the URL names without connecting, so
    this proves the SQLite-generated revision is valid PostgreSQL — and, on the PostgreSQL leg,
    the reverse — on a machine with no PostgreSQL server at all.
    """
    config = a_host_project(tmp_path, database_url)
    command.revision(config, message="initial", autogenerate=True)
    del engine

    other = (
        "postgresql+psycopg://user:pw@localhost/other"
        if database_url.startswith("sqlite")
        else "sqlite:///" + str(tmp_path / "other.sqlite3")
    )
    rendered = io.StringIO()
    offline = host_config(Path(ScriptDirectory.from_config(config).dir), other)
    # Alembic's offline DDL goes to the Config's output buffer, not to `stdout`.
    offline.output_buffer = rendered
    command.upgrade(offline, "head", sql=True)
    sql = rendered.getvalue()

    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table}" in sql, f"{table} is missing from the rendered DDL"
    assert "BIGINT" in sql
    if other.startswith("postgresql"):
        assert "TIMESTAMP WITH TIME ZONE" in sql
        assert "BOOLEAN" in sql


def test_two_migrated_hosts_in_two_databases_do_not_see_each_other(tmp_path: Path) -> None:
    ledgers = {}
    engines = {}
    for name in ("alpha", "beta"):
        url = f"sqlite:///{tmp_path / f'{name}.sqlite3'}"
        config = a_host_project(tmp_path, url, name=name)
        command.revision(config, message="initial", autogenerate=True)
        command.upgrade(config, "head")
        engines[name] = sa.create_engine(url)
        ledgers[name] = SqlLedger(
            session_factory_for(engines[name]),
            [BudgetCeiling(scope=CeilingScope.PER_DAY, tokens=1_000_000)],
            clock=ManualClock(),
            table_prefix=TABLE_PREFIX,
        )

    ledgers["alpha"].debit(
        Debit(
            run_id="traj-1",
            source_ref="turn-1",
            usage=TokenUsage(input_tokens=2_500, output_tokens=0),
            cost=None,
            occurred_at=MIDDAY,
        )
    )
    ledgers["beta"].declare_run("traj-1")

    assert ledgers["alpha"].remaining("traj-1")[0].tokens_spent == 2_500
    assert ledgers["beta"].remaining("traj-1")[0].tokens_spent == 0
    for engine in engines.values():
        engine.dispose()


def test_a_host_that_mounts_too_late_generates_a_migration_that_drops_the_ledger(
    engine: Engine, database_url: str, tmp_path: Path
) -> None:
    """ADR-0050's named failure mode, proven to be real rather than described.

    Autogenerate compares the database against whatever is in ``target_metadata`` *at the moment
    it looks*. A host that mounts lazily has an empty metadata at that moment, so Alembic
    concludes the ledger tables are leftovers and writes ``drop_table`` for each. This is why
    ``hostapp/models.py`` mounts at import, and why this test exists rather than a comment.
    """
    config = a_host_project(tmp_path, database_url)
    command.revision(config, message="initial", autogenerate=True)
    command.upgrade(config, "head")

    import hostapp.models

    unmounted = sa.MetaData()
    sa.Table(
        "host_notes",
        unmounted,
        sa.Column("note_id", sa.String(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
    )
    original = hostapp.models.metadata
    try:
        hostapp.models.metadata = unmounted
        command.revision(config, message="late", autogenerate=True)
    finally:
        hostapp.models.metadata = original

    versions = Path(ScriptDirectory.from_config(config).versions)
    latest = max(versions.glob("*.py"), key=lambda path: path.stat().st_mtime)
    body = latest.read_text(encoding="utf-8")
    for table in EXPECTED_TABLES - {"host_notes"}:
        assert f"op.drop_table('{table}')" in body, (
            "mounting after the metadata was inspected should make autogenerate drop the ledger; "
            "if this stopped being true the hazard is gone and this test can go with it"
        )
    del engine


def test_a_missing_postgresql_server_fails_rather_than_skips_when_ci_demands_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped dialect must never read as a passing one.

    Locally the PostgreSQL legs skip, with a reason naming the URL, which pytest's ``-ra`` summary
    prints. CI's ``db-matrix`` job sets ``LOADLEDGER_REQUIRE_POSTGRES=1`` so the same absence is a
    failure there — otherwise a green CI run could be green because every PostgreSQL leg quietly
    skipped, which is exactly the outcome the both-dialects promise cannot survive.
    """
    monkeypatch.setenv("LOADLEDGER_POSTGRES_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")

    monkeypatch.delenv("LOADLEDGER_REQUIRE_POSTGRES", raising=False)
    with pytest.raises(Skipped, match="POSTGRESQL LEG SKIPPED"):
        postgres_url()

    monkeypatch.setenv("LOADLEDGER_REQUIRE_POSTGRES", "1")
    with pytest.raises(Failed, match="LOADLEDGER_REQUIRE_POSTGRES=1"):
        postgres_url()
