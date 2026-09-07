"""Typed refusals — the error hierarchy LoadLedger raises, per spec §7 and §13.

Every error subclasses :class:`baseaicore.SuiteError`, so a caller that already handles suite
errors handles these, and every ``code`` is part of the public contract: codes appear in API
error envelopes, in stored event rows and in CLI exit-code mapping, so changing what one means is
a breaking change.

One code is deliberately **not** here. ``UNPRICED_EGRESS_REFUSED`` belongs to PromptCadence
(:doc:`ADR-0047 <adr>` §"Two ceilings"): LoadLedger surfaces the unpriced count on a verdict and
says what remains; refusing to spend on an unpriced step is the application's policy, and a
package that raised it would be making the decision the ADR assigns to its caller.
"""

from __future__ import annotations

from typing import ClassVar

from baseaicore import SuiteError

__all__ = [
    "CurrencyMismatch",
    "InvalidCeiling",
    "LedgerError",
    "PricingFileError",
    "UnknownRun",
    "UnsupportedDialect",
]


class LedgerError(SuiteError):
    """Base for every error this package raises.

    Nothing raises it directly; it exists so a caller can catch every LoadLedger refusal with one
    ``except`` without also catching unrelated suite errors.
    """

    code: ClassVar[str] = "LEDGER_ERROR"


class CurrencyMismatch(LedgerError):
    """A priced debit names a currency a ceiling covering it cannot accumulate.

    Refused rather than converted. Converting needs an exchange rate, which is time-varying
    external data outside the user's control (ADR-0030 rule 3), and a ceiling that quietly
    converted would report a number nobody could reproduce later. ``details`` names both
    currencies and the ceiling that could not take the debit.
    """

    code: ClassVar[str] = "LEDGER_CURRENCY_MISMATCH"


class InvalidCeiling(LedgerError):
    """A :class:`~loadledger.types.BudgetCeiling` could never bind anything.

    Raised at construction, not at evaluation: a ceiling with neither a money nor a token bound,
    or a ``PER_TAG`` ceiling with no tag, is a configuration mistake that would otherwise be
    discovered as a budget that never fired.
    """

    code: ClassVar[str] = "LEDGER_CEILING_INVALID"


class PricingFileError(LedgerError):
    """An ADR-0072 price catalogue could not be read, or holds a record the rules refuse.

    Raised only from ``loadledger.pricing``; the pure core never opens a file. ``details`` names
    the ``file`` and, where one applies, the ``record`` index and the ``field`` — because a
    refusal that does not say which line of a hand-maintained price list is wrong is a refusal an
    operator cannot act on.

    A caller reads its catalogue at startup precisely so that this is a refusal to start rather
    than a run that spends money nobody can cost, and it translates this into its own
    configuration vocabulary: *which* key named the unreadable file is the application's fact, not
    the reader's.
    """

    code: ClassVar[str] = "LEDGER_PRICING_FILE_INVALID"


class UnknownRun(LedgerError):
    """A balance was asked for a run this ledger has never seen.

    A run exists once it has been debited or explicitly declared
    (:meth:`~loadledger.core.Ledger.declare_run`). Answering "nothing spent" for an unknown run
    would be indistinguishable from answering it for a real run that has spent nothing, and the
    two are different facts — the first is a typo in a run id, the second is a fresh budget.
    """

    code: ClassVar[str] = "LEDGER_UNKNOWN_RUN"


class UnsupportedDialect(LedgerError):
    """A session was bound to a database this package has no statements for.

    The suite runs on exactly two dialects — SQLite and PostgreSQL, both first-class
    (:doc:`ADR-0006 <adr>`) — and :class:`~loadledger.sql.SqlLedger`'s balance upsert is written
    for both (spec §7's error list, §13's last row). A third dialect is refused at the first
    statement rather than discovered as a syntax error partway through a money transaction.
    ``details`` names the dialect that was bound.

    Raised only from ``loadledger.sql``; the pure core never sees a database.
    """

    code: ClassVar[str] = "LEDGER_UNSUPPORTED_DIALECT"
