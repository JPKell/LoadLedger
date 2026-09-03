"""The host's Alembic environment — an ordinary one, deliberately.

Nothing here knows that some of ``target_metadata``'s tables came from a package: that is the
claim ADR-0050 makes, and an ``env.py`` with a special case for mounted tables would quietly
withdraw it. Offline mode is kept because the portability test uses it to render this host's
generated revision as DDL for the *other* dialect without needing a server for it.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from hostapp.models import metadata

config = context.config
target_metadata = metadata


def run_migrations_offline() -> None:
    """Emit SQL for the configured URL's dialect without connecting to anything."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against a real database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
