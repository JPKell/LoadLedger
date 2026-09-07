# LoadLedger documentation

This directory contains the maintained documentation for LoadLedger.

## Mirrored from the suite's central documentation set

Both documents below are byte-identical mirrors of the authoritative copies; edit those first.

- [Development plan](packages/loadledger/development-plan.md)
- [Specification](packages/loadledger/spec.md)

## Written here

- [Quickstart](quickstart.md) — and [`quickstart.py`](quickstart.py), the standalone script it
  publishes the output of. A test runs the script, so it cannot rot.
- [Mounted-table upgrades](mounted-table-upgrades.md) — the template and migration recipe
  LoadLedger ships when a mounted table changes shape, since the host owns every migration.
