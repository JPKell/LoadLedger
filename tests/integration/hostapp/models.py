"""The host's own model module — mounting at import, which is the whole point.

ADR-0050's named failure mode is import order: autogenerate only sees what was mounted before the
metadata was inspected, so a host that mounts lazily gets a migration that silently *drops* the
package's tables. A host mounts here, at module import, in the module its ``env.py`` imports —
and this file is what that looks like.
"""

from __future__ import annotations

import sqlalchemy as sa

from loadledger.sql import mount_ledger_tables

TABLE_PREFIX = "ledger_"

metadata = sa.MetaData()
"""The application's own metadata: what ``env.py`` names as ``target_metadata``."""

notes = sa.Table(
    "host_notes",
    metadata,
    sa.Column("note_id", sa.String(), primary_key=True),
    sa.Column("body", sa.Text(), nullable=False),
)
"""A table of the host's own, so the host is not merely the mounted set.

Without it the test would prove that a migration history containing only this package's tables
works, which is not the situation any real host is in.
"""

ledger_tables = mount_ledger_tables(metadata, prefix=TABLE_PREFIX)
"""Mounted eagerly, at import. See the module docstring."""
