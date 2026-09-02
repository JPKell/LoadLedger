# Security Policy

LoadLedger is part of the Local AI Suite, whose default posture is local-first. This component is
a pure library: it opens no socket, reads no file, reads no environment variable and writes no log,
so it has no network surface of its own. Phase 2 adds SQL models an application mounts into *its*
database, over a session the application injects — still no engine, no URL and no connection here.
See security standards for the suite's full trust-boundary model and
0014 authentication strategy and 0026 local http hardening for the design the
applications that consume this package follow.

Entries carry opaque references and hashes, never prompt or response content (spec §14).

## Reporting a vulnerability

Please do not open a public issue for a suspected security vulnerability.

Instead, report it privately to the maintainer with:

* A description of the issue and its potential impact.
* Steps to reproduce, including the ceilings, debits and clock involved — this package's
  whole configuration surface is its constructor arguments (spec §12).
* The installed package version (`pip show loadledger`). This package ships no CLI.

You should expect an acknowledgement within a reasonable time and, once a fix is available, credit in
the release notes unless you ask otherwise.

## Scope

In scope: this repository's own code and its documented configuration surface. Vulnerabilities in a
model provider (e.g. Ollama itself), in the operating system, or in a third-party dependency should
be reported to that project directly — `pip-audit` runs in this repository's CI to catch known
vulnerable dependency versions.

## Security-relevant design decisions

This component follows these security rules:

* security standards — trust boundaries, network exposure, input validation,
  filesystem safety, secrets handling.
* 0014 authentication strategy — bearer tokens, scopes, loopback-vs-LAN behaviour.
* 0026 local http hardening — Host header validation, CSRF, outbound-fetch allowlisting.
