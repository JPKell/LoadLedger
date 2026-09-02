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

__all__ = ["CurrencyMismatch", "InvalidCeiling", "LedgerError", "UnknownRun"]


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


class UnknownRun(LedgerError):
    """A balance was asked for a run this ledger has never seen.

    A run exists once it has been debited or explicitly declared
    (:meth:`~loadledger.core.Ledger.declare_run`). Answering "nothing spent" for an unknown run
    would be indistinguishable from answering it for a real run that has spent nothing, and the
    two are different facts — the first is a typo in a run id, the second is a fresh budget.
    """

    code: ClassVar[str] = "LEDGER_UNKNOWN_RUN"
