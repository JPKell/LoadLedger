"""A miniature host application, for the one test ADR-0050 decision 6 asks for.

Not a fixture and not a mock: a real application with its own ``MetaData``, its own table, its own
Alembic environment and its own ``versions/`` directory, which mounts this package's tables and
then migrates them with ``alembic revision --autogenerate`` and ``alembic upgrade head`` like any
table it wrote itself. The mounting pattern is only as good as the evidence that a host's
migration story survives it, and this is that evidence.
"""

from __future__ import annotations
