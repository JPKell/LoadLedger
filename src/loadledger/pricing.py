"""loadledger.pricing — reading an ADR-0072 price catalogue, and resolving a record from it.

[ADR-0030](../../adr/0030-model-cost-and-pricing.md) rule 1 is that **cost is derived and never
stored**, which only works if the price the derivation used can be found again.
[ADR-0072](../../adr/0072-the-model-pricing-record-file.md) fixed the file that holds it — JSON, a
``records`` array, one object per observation — and left the reader in the first consumer that
needed one. This module is that reader, moved here at the second
([ADR-0110](../../adr/0110-the-pricing-file-reader-is-a-loadledger-surface.md), ADR-0011's
extraction rule).

What it owns is the whole of the format: parsing, validation, the refusals, and the resolution
rules that pick one record for one call. What it deliberately does **not** own is where the path
came from — a tier's ``pricing_file``, an application's ``[pricing] file``, a flag — or what a
missing file means. That is configuration, it differs per application, and an application catches
:class:`~loadledger.errors.PricingFileError` and reports it in its own vocabulary.

``pricing_hash`` is not derived here: it is :attr:`baseaicore.ModelPricing.pricing_hash`, over the
record this module built. That is the whole reason one reader matters. The hash is the join
between a stored usage and the price it was costed under (ADR-0030), so two applications that
parsed the same file differently — disagreeing about nothing more than whether an omitted rate was
free — would file two different prices under one hash, and nobody would notice until a re-costing
across both failed to reconcile.

Three rules of the format are load-bearing rather than stylistic, and the tests assert each:

* **Rates are decimal strings, never floats.** ``"2.50"`` goes through
  :meth:`baseaicore.Money.from_decimal` to whole nanos. A JSON number is a float in every parser
  this suite will meet, and a price that arrived as a float has already lost the value the integer
  arithmetic everywhere else exists to protect. A number in a rate position is **refused**.
* **An omitted rate is UNSUPPORTED, not zero.** A price list stating no cache-read rate cannot
  price a call that read from cache; :func:`baseaicore.estimate_cost` returns an untotalled
  estimate, which is the floor
  [ADR-0069](../../adr/0069-a-partial-price-is-a-floor-and-a-money-ceiling-chooses-how-it-binds.md)
  accumulates. A rate stated as ``"0"`` is a real zero and a different claim.
* **A record is an observation, not a fact about a model.** Several records may name the same
  weights — a standard tier and a batch tier, two regions, a superseded price and its replacement.
  :func:`price_for_model` resolves them by the window they claim and, among those still claiming
  the instant, by which was observed most recently.

**No network, ever** (ADR-0072 §7). A catalogue is read from disk, by a caller that reads it at
startup, so a file that cannot be read is a refusal to start rather than real spend nobody can
cost.

This module is **not** imported from ``loadledger/__init__.py``: it does file I/O, and the package
root promises it does none. Import it explicitly, as with ``loadledger.sql``::

    from loadledger.pricing import load_pricing_records, price_for_model
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from baseaicore import (
    UNSUPPORTED,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    ValidationError,
    from_rfc3339,
    normalize_digest,
)

from loadledger.errors import PricingFileError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

__all__ = ["load_pricing_records", "price_for_model", "records_claiming"]

_RATE_FIELDS: Final = (
    "input_per_million_tokens",
    "output_per_million_tokens",
    "cache_write_per_million_tokens",
    "cache_read_per_million_tokens",
)


def _refuse(message: str, **details: Any) -> PricingFileError:
    """Build the one refusal shape this module raises, naming the file and the field."""
    return PricingFileError(message, details=details)


def _rates_of(document: Mapping[str, Any], *, path: Path, index: int) -> TokenRates:
    """Read one record's ``rates`` block, keeping "not stated" distinct from "free"."""
    block = document.get("rates")
    if not isinstance(block, Mapping):
        message = f"{path}: record {index} has no 'rates' object"
        raise _refuse(message, file=str(path), record=index, field="rates")
    currency = block.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        message = f"{path}: record {index} states no 'rates.currency'"
        raise _refuse(message, file=str(path), record=index, field="rates.currency")
    stated: dict[str, Money] = {}
    for name in _RATE_FIELDS:
        raw = block.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            message = (
                f"{path}: record {index} states rates.{name}={raw!r}; a rate is a decimal "
                '*string* such as "2.50". A JSON number is a float, and a price that arrived as '
                "a float has already lost the value this suite's integer arithmetic protects."
            )
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}")
        try:
            stated[name] = Money.from_decimal(currency, raw)
        except ValidationError as exc:
            message = f"{path}: record {index} states an unreadable rates.{name}: {exc.message}"
            raise _refuse(message, file=str(path), record=index, field=f"rates.{name}") from exc
    try:
        return TokenRates(
            currency=currency,
            input_per_million_tokens=stated.get("input_per_million_tokens", UNSUPPORTED),
            output_per_million_tokens=stated.get("output_per_million_tokens", UNSUPPORTED),
            cache_write_per_million_tokens=stated.get(
                "cache_write_per_million_tokens", UNSUPPORTED
            ),
            cache_read_per_million_tokens=stated.get("cache_read_per_million_tokens", UNSUPPORTED),
        )
    except ValidationError as exc:
        message = f"{path}: record {index} has unusable rates: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field="rates") from exc


def _instant(document: Mapping[str, Any], name: str, *, path: Path, index: int) -> datetime | None:
    """Read one RFC 3339 field, refusing a value that is present but unreadable."""
    raw = document.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        message = f"{path}: record {index} states {name}={raw!r}; expected an RFC 3339 string"
        raise _refuse(message, file=str(path), record=index, field=name)
    try:
        return from_rfc3339(raw)
    except ValidationError as exc:
        message = f"{path}: record {index} states an unreadable {name}: {exc.message}"
        raise _refuse(message, file=str(path), record=index, field=name) from exc


def _record_of(document: Mapping[str, Any], *, path: Path, index: int) -> ModelPricing:
    """Build one :class:`~baseaicore.ModelPricing` from one record object."""
    kind_raw = str(document.get("provider_kind"))
    try:
        kind = ProviderKind(kind_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in ProviderKind))
        message = (
            f"{path}: record {index} names provider_kind={kind_raw!r}, which is not a provider "
            f"this suite knows ({known})"
        )
        raise _refuse(message, file=str(path), record=index, field="provider_kind") from exc
    name = document.get("provider_model_name")
    if not isinstance(name, str) or not name.strip():
        message = f"{path}: record {index} names no provider_model_name"
        raise _refuse(message, file=str(path), record=index, field="provider_model_name")
    source_raw = str(document.get("source"))
    try:
        source = PricingSource(source_raw)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in PricingSource))
        message = (
            f"{path}: record {index} names source={source_raw!r}; a price without stated "
            f"provenance cannot be weighed (ADR-0030). Expected one of: {known}"
        )
        raise _refuse(message, file=str(path), record=index, field="source") from exc
    observed_at = _instant(document, "observed_at", path=path, index=index)
    if observed_at is None:
        message = (
            f"{path}: record {index} states no observed_at; a price with no date is a price "
            "nobody can tell has gone stale"
        )
        raise _refuse(message, file=str(path), record=index, field="observed_at")
    digest_raw = document.get("artifact_digest")
    try:
        digest = normalize_digest(digest_raw) if isinstance(digest_raw, str) else None
        identity = ModelIdentity(
            provider_kind=kind, provider_model_name=name, artifact_digest=digest
        )
        return ModelPricing(
            identity=identity,
            rates=_rates_of(document, path=path, index=index),
            source=source,
            observed_at=observed_at,
            effective_from=_instant(document, "effective_from", path=path, index=index),
            effective_until=_instant(document, "effective_until", path=path, index=index),
            price_tier=document.get("price_tier") or None,
            region=document.get("region") or None,
        )
    except ValidationError as exc:
        message = f"{path}: record {index} is not a usable price observation: {exc.message}"
        raise _refuse(message, file=str(path), record=index) from exc


def load_pricing_records(path: Path) -> tuple[ModelPricing, ...]:
    """Read one ADR-0072 pricing file into price observations.

    Args:
        path: The catalogue to read. Which configuration key named it is the caller's business;
            this function is handed a path and nothing else.

    Returns:
        Every record in the file, in file order. An empty ``records`` array is legitimate and
        loads to an empty tuple — a file that states no prices is a file that prices nothing, and
        the refusal for that belongs to whatever named the file, not to the reader.

    Raises:
        PricingFileError: If the file is missing, is not readable, is not JSON, is not an object
            with a ``records`` array, or holds a record these rules cannot turn into a
            :class:`~baseaicore.ModelPricing`. ``details`` names the file and, where one applies,
            the record index and the field. Every one of these is a startup refusal by design
            (ADR-0072 §7): a price list discovered to be unreadable mid-run would leave real
            spend that cannot be costed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"pricing file {path} cannot be read: {exc.strerror or exc}"
        raise _refuse(message, file=str(path)) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        message = f"pricing file {path} is not valid JSON: {exc}"
        raise _refuse(message, file=str(path)) from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        message = (
            f"pricing file {path} must be a JSON object with a 'records' array of "
            "ModelPricing observations"
        )
        raise _refuse(message, file=str(path), field="records")
    records = []
    for index, entry in enumerate(document["records"]):
        if not isinstance(entry, dict):
            message = f"{path}: record {index} is not an object"
            raise _refuse(message, file=str(path), record=index)
        records.append(_record_of(entry, path=path, index=index))
    return tuple(records)


def records_claiming(records: Sequence[ModelPricing], *, at: datetime) -> tuple[ModelPricing, ...]:
    """Return every record that says it applies at ``at``, whatever weights it names.

    What a **pre-flight estimate** has to work with when the model that will answer is not yet
    known. The caller costs its estimate against all of these and takes the largest, because the
    only estimate that cannot under-state a budget is the worst case (ADR-0072 §6). Under-stating
    is the failure that matters: an over-stated estimate refuses a step that would have fitted and
    says which cap refused it, while an under-stated one crosses the cap and says nothing.

    Args:
        records: The catalogue, or the slice of it the caller considers in scope.
        at: The instant to price at.

    Returns:
        The records still claiming ``at``, in the order given. Empty when every record has
        expired, or has not taken effect, or when there were none.
    """
    return tuple(record for record in records if _claims(record, at))


def price_for_model(
    records: Sequence[ModelPricing], *, canonical_id: str, at: datetime
) -> ModelPricing | None:
    """Return the observation to cost a call on ``canonical_id`` at ``at``.

    Matching is on the identity's two stable halves — the provider kind and the provider's own
    model name — with the artifact digest as an *optional* narrowing: a record that states a digest
    matches only that digest, and a record that states none matches those weights under any digest
    (ADR-0072 §5). That asymmetry is deliberate and it is the rule most likely to be got backwards.
    A price list is usually written against a provider's product name, which survives a retag;
    pinning every record to a digest would make a routine retag silently unpriceable, while
    ignoring a digest a record *did* state would price one set of weights at another's rates.

    Args:
        records: The catalogue, or the slice of it the caller considers in scope — one tier's
            file, or an application's whole list.
        canonical_id: ``provider/name`` or ``provider/name@sha256:…``
            ([ADR-0008](../../adr/0008-canonical-model-identity.md)).
        at: The instant to price at, normally when the call happened, so re-costing history later
            finds the same record.

    Returns:
        The most recently *observed* record still claiming ``at``, or ``None`` when nothing here
        covers these weights. ``None`` is **not free**: the caller records the usage unpriced and
        says why, and LoadLedger counts it as an unpriced debit (ADR-0016, ADR-0069).
    """
    candidates = [
        record for record in records if _matches(record, canonical_id) and _claims(record, at)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: record.observed_at)


def _split(canonical_id: str) -> tuple[str, str, str | None]:
    """Split ``provider/name`` or ``provider/name@sha256:…`` without parsing the name itself."""
    prefix, _, remainder = canonical_id.partition("/")
    name, at, digest = remainder.rpartition("@")
    if not at or not digest.startswith("sha256:"):
        return prefix, remainder, None
    return prefix, name, digest


def _matches(record: ModelPricing, canonical_id: str) -> bool:
    """Whether one record's identity names the weights ``canonical_id`` names."""
    kind, name, digest = _split(canonical_id)
    if record.identity.provider_kind.value != kind or record.identity.provider_model_name != name:
        return False
    return record.identity.artifact_digest in (None, digest)


def _claims(record: ModelPricing, at: datetime) -> bool:
    """Whether one record says it applies at ``at``. An unstated bound is not a bound."""
    if record.effective_from is not None and at < record.effective_from:
        return False
    return not (record.effective_until is not None and at > record.effective_until)
