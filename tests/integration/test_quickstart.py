"""Spec §20 acceptance criterion 2, executed rather than described.

The criterion is "a standalone script with `loadledger[sql]` + `baseaicore` mounts the tables into
its own SQLite database, debits priced and unpriced usage, and prints honest balances (`—`, not
`$0`, for the unpriced local model; 'at least' for a floor)". `docs/quickstart.py` is that script,
and this runs it — in a temporary directory, in a child process, exactly as a reader would.

That the script resolves against `loadledger[sql]` and nothing else was checked in a clean
throwaway venv and is recorded in `C3_HANDOFF.md`; what this test guards is the other half, which
is the half that rots: a quickstart that stopped running would go on being published for as long
as nobody tried it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

QUICKSTART = Path(__file__).parents[2] / "docs" / "quickstart.py"


def without_coverage(environment: dict[str, str]) -> dict[str, str]:
    """Return ``environment`` with pytest-cov's subprocess hooks removed.

    This child runs with ``cwd`` set to a temporary directory — deliberately, since that is where
    the script writes its database — so it cannot find ``pyproject.toml`` and would measure itself
    **without** ``branch = true``. The resulting ``.coverage.*`` file then refuses to combine with
    the branch data every other process wrote ("Can't combine statement coverage data with branch
    data"), and the whole coverage run fails with an `INTERNALERROR` that says nothing about a
    working directory. The child's coverage is not wanted anyway: what is under test here is that
    the published script runs, not which of its lines did.
    """
    return {
        name: value
        for name, value in environment.items()
        if not name.startswith(("COV_CORE", "COVERAGE"))
    }


def test_the_published_quickstart_runs_and_prints_honest_balances(tmp_path: Path) -> None:
    finished = subprocess.run(  # noqa: S603 — a fixed argv, no shell, no user input
        [sys.executable, str(QUICKSTART)],
        cwd=tmp_path,
        env=without_coverage(dict(os.environ)),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert finished.returncode == 0, finished.stderr
    output = finished.stdout

    assert "ledger_entries, ledger_balances, ledger_balance_money, ledger_runs" in output
    # An unpriced local model shows nothing, not zero (ADR-0016).
    assert "spent —" in output
    assert "$0" not in output
    # A partly-priced window is a floor and says so (ADR-0069).
    assert "at least 0.72 USD" in output
    # And the pre-flight refuses the step that would cross the token cap, before spending.
    assert "exceeded=True" in output
    # The stored facts are usage and a hash; no money figure is the record.
    assert "pricing_hash=" in output

    # The script tidies up after itself: a reader's directory is left as it was found.
    assert list(tmp_path.iterdir()) == []
