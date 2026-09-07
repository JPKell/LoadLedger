"""The ADR-0072 catalogue reader: the format, its refusals, resolution, and the hashes.

Every case here arrived with the reader (row K4). The refusal cases and the matching cases are the
union of what ``promptcadence.services.pricing`` and ``ideapress.services.pricing`` asserted while
each carried its own copy, transcribed rather than rewritten, so that a rule either survives the
move or fails visibly here.

The golden-hash test is the one that matters most, and it is why the fixture is a file on disk
rather than a dict built in Python. ``pricing_hash`` is the join between a stored usage and the
price it was costed under (ADR-0030 rule 1): a moved reader that produced an equal-looking record
with a different hash would silently re-price every debit already in two applications' databases.
The literals below were measured from **both applications' own loaders**, on this exact file,
before either adopted this module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import (
    UNSUPPORTED,
    ModelIdentity,
    ModelPricing,
    Money,
    PricingSource,
    ProviderKind,
    TokenRates,
    TokenUsage,
    estimate_cost,
    is_supported,
)

from loadledger import PricingFileError
from loadledger.pricing import load_pricing_records, price_for_model, records_claiming

_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "b" * 64
_OTHER_DIGEST = "sha256:" + "c" * 64

_CATALOGUE = Path(__file__).resolve().parents[1] / "data" / "adr0072_catalogue.json"

# Measured on 2026-09-07 from promptcadence.services.pricing and ideapress.services.pricing, both
# reading tests/data/adr0072_catalogue.json, before either adopted this module. The two agreed
# record for record, which is the precondition ADR-0030's re-derivation had been assuming.
_GOLDEN_HASHES = (
    ("openai_compatible/gpt-4o@unknown", "0aa11514c7c3fcf0"),
    ("ollama/gemma4:12b@unknown", "24a133ea7262ad3e"),
    ("ollama/qwen3:8b@sha256:bbbbbbbbbbbb", "79b6b3d231df3f5f"),
)


def _record(**overrides: Any) -> dict[str, Any]:
    """One complete, valid record, with whatever the case under test changes about it."""
    record: dict[str, Any] = {
        "provider_kind": "ollama",
        "provider_model_name": "qwen3:8b",
        "source": "provider_published",
        "observed_at": "2026-09-01T00:00:00Z",
        "rates": {
            "currency": "USD",
            "input_per_million_tokens": "2.50",
            "output_per_million_tokens": "10.00",
        },
    }
    record.update(overrides)
    return record


def _write(tmp_path: Path, *records: Any) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"records": list(records)}), encoding="utf-8")
    return path


# ---- the golden file --------------------------------------------------------------------------


def test_the_adr_0072_catalogue_loads_to_the_hashes_both_applications_measured() -> None:
    """The move must not re-price history: same file, same records, same ``pricing_hash``."""
    records = load_pricing_records(_CATALOGUE)
    measured = tuple((record.identity.canonical_id, record.pricing_hash) for record in records)
    assert measured == _GOLDEN_HASHES


def test_the_golden_catalogue_keeps_not_stated_distinct_from_free() -> None:
    """Record 2 states no cache rates; record 3 states cache read as ``"0"``. Different claims."""
    _openai, ollama, batch = load_pricing_records(_CATALOGUE)
    assert not is_supported(ollama.rates.cache_read_per_million_tokens)
    assert not is_supported(ollama.rates.cache_write_per_million_tokens)
    assert batch.rates.cache_read_per_million_tokens == Money(currency="EUR", nanos=0)
    assert not is_supported(batch.rates.cache_write_per_million_tokens)


# ---- the format -------------------------------------------------------------------------------


def test_a_complete_record_loads_with_its_provenance_intact(tmp_path: Path) -> None:
    (record,) = load_pricing_records(_write(tmp_path, _record(price_tier="standard")))
    assert record.identity.provider_kind is ProviderKind.OLLAMA
    assert record.identity.provider_model_name == "qwen3:8b"
    assert record.source is PricingSource.PROVIDER_PUBLISHED
    assert record.price_tier == "standard"
    assert record.region is None
    assert record.rates.input_per_million_tokens == Money.from_decimal("USD", "2.50")


def test_a_decimal_rate_becomes_whole_nanos(tmp_path: Path) -> None:
    (record,) = load_pricing_records(_write(tmp_path, _record()))
    input_rate = record.rates.input_per_million_tokens
    output_rate = record.rates.output_per_million_tokens
    assert is_supported(input_rate) and input_rate.nanos == 2_500_000_000
    assert is_supported(output_rate) and output_rate.nanos == 10_000_000_000


def test_an_omitted_rate_is_unsupported_and_makes_the_estimate_a_floor(tmp_path: Path) -> None:
    """ "Not stated" is not "free". A call using that class cannot be fully priced (ADR-0069)."""
    (record,) = load_pricing_records(_write(tmp_path, _record()))
    assert record.rates.cache_read_per_million_tokens is UNSUPPORTED
    estimate = estimate_cost(
        TokenUsage(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=500,
        ),
        record,
        at=_AT,
    )
    assert estimate.is_complete is False, "a used class with no rate cannot be totalled"
    assert estimate.input_cost == Money.from_decimal("USD", "2.50")


def test_a_json_number_rate_is_refused_not_coerced(tmp_path: Path) -> None:
    """A float has already lost the value the whole-nanos arithmetic protects."""
    record = _record()
    record["rates"] = dict(record["rates"], input_per_million_tokens=2.5)
    with pytest.raises(PricingFileError, match="decimal"):
        load_pricing_records(_write(tmp_path, record))


def test_an_empty_records_array_loads_to_nothing_rather_than_refusing(tmp_path: Path) -> None:
    """A file that states no prices prices nothing; the refusal belongs to whatever named it."""
    assert load_pricing_records(_write(tmp_path)) == ()


# ---- the refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "fragment", "field"),
    [
        ({"provider_kind": "not_a_provider"}, "not a provider this suite knows", "provider_kind"),
        ({"provider_model_name": ""}, "names no provider_model_name", "provider_model_name"),
        ({"source": "guessed"}, "without stated provenance", "source"),
        ({"observed_at": None}, "states no observed_at", "observed_at"),
        ({"observed_at": "yesterday"}, "unreadable observed_at", "observed_at"),
        ({"effective_from": "not a date"}, "unreadable effective_from", "effective_from"),
        ({"effective_until": 2026}, "expected an RFC 3339 string", "effective_until"),
        ({"rates": {}}, "states no 'rates.currency'", "rates.currency"),
        ({"rates": None}, "has no 'rates' object", "rates"),
    ],
)
def test_an_unusable_record_is_refused_naming_the_field(
    tmp_path: Path, overrides: dict[str, Any], fragment: str, field: str
) -> None:
    with pytest.raises(PricingFileError) as raised:
        load_pricing_records(_write(tmp_path, _record(**overrides)))
    assert fragment in str(raised.value)
    assert raised.value.details["field"] == field
    assert raised.value.details["record"] == 0
    assert raised.value.code == "LEDGER_PRICING_FILE_INVALID"


def test_an_unreadable_rate_names_the_field_it_could_not_read(tmp_path: Path) -> None:
    record = _record()
    record["rates"] = dict(record["rates"], input_per_million_tokens="two fifty")
    with pytest.raises(PricingFileError, match="unreadable rates.input_per_million_tokens"):
        load_pricing_records(_write(tmp_path, record))


def test_a_record_whose_fields_do_not_compose_an_observation_is_refused(tmp_path: Path) -> None:
    """The last refusal is `ModelPricing`'s own: a window that can never contain an instant."""
    inverted = _record(
        effective_from="2026-10-01T00:00:00Z", effective_until="2026-09-01T00:00:00Z"
    )
    with pytest.raises(PricingFileError, match="not a usable price observation") as raised:
        load_pricing_records(_write(tmp_path, inverted))
    assert raised.value.details["record"] == 0


def test_a_non_object_record_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PricingFileError, match="record 0 is not an object"):
        load_pricing_records(_write(tmp_path, "not an object"))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PricingFileError, match="cannot be read") as raised:
        load_pricing_records(tmp_path / "nope.json")
    assert raised.value.details["file"].endswith("nope.json")


def test_a_file_that_is_not_an_object_with_records_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(PricingFileError, match="must be a JSON object"):
        load_pricing_records(path)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(PricingFileError, match="not valid JSON"):
        load_pricing_records(path)
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")
    with pytest.raises(PricingFileError, match="must be a JSON object"):
        load_pricing_records(path)


# ---- resolution -------------------------------------------------------------------------------


def _pricing(**overrides: Any) -> ModelPricing:
    base: dict[str, Any] = {
        "identity": ModelIdentity(
            provider_kind=ProviderKind.OLLAMA, provider_model_name="qwen3:8b"
        ),
        "rates": TokenRates(currency="USD"),
        "source": PricingSource.USER_OVERRIDE,
        "observed_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return ModelPricing(**base)


def test_a_record_stating_a_digest_matches_only_that_digest(tmp_path: Path) -> None:
    """A record pinned to weights prices those weights, and no others that share the name."""
    records = load_pricing_records(_write(tmp_path, _record(artifact_digest=_DIGEST)))
    assert price_for_model(records, canonical_id=f"ollama/qwen3:8b@{_DIGEST}", at=_AT)
    assert price_for_model(records, canonical_id="ollama/qwen3:8b", at=_AT) is None
    assert price_for_model(records, canonical_id=f"ollama/qwen3:8b@{_OTHER_DIGEST}", at=_AT) is None


def test_a_record_stating_no_digest_matches_the_weights_under_any_digest(tmp_path: Path) -> None:
    """A price list is written against a product name, which survives a retag."""
    records = load_pricing_records(_write(tmp_path, _record()))
    assert price_for_model(records, canonical_id=f"ollama/qwen3:8b@{_DIGEST}", at=_AT)
    assert price_for_model(records, canonical_id="ollama/qwen3:8b", at=_AT)
    assert price_for_model(records, canonical_id="ollama/llama3:8b", at=_AT) is None
    assert price_for_model(records, canonical_id="vllm/qwen3:8b", at=_AT) is None


def test_nothing_covering_the_weights_returns_none_rather_than_a_free_price() -> None:
    assert price_for_model((_pricing(),), canonical_id="openai_compatible/other", at=_AT) is None
    assert price_for_model((), canonical_id="ollama/qwen3:8b", at=_AT) is None


def test_a_record_outside_its_effective_window_does_not_claim_the_instant(tmp_path: Path) -> None:
    """Extrapolating a price beyond the window it was quoted for is guessing (ADR-0030)."""
    records = load_pricing_records(
        _write(
            tmp_path,
            _record(effective_from="2026-10-01T00:00:00Z"),
            _record(effective_until="2026-08-01T00:00:00Z"),
        )
    )
    assert price_for_model(records, canonical_id="ollama/qwen3:8b", at=_AT) is None
    assert records_claiming(records, at=_AT) == ()
    later = datetime(2026, 10, 2, tzinfo=UTC)
    assert price_for_model(records, canonical_id="ollama/qwen3:8b", at=later)
    assert len(records_claiming(records, at=later)) == 1


def test_records_claiming_ignores_which_weights_a_record_names(tmp_path: Path) -> None:
    """A pre-flight estimate has no model yet, so every claiming record is a candidate."""
    records = load_pricing_records(
        _write(tmp_path, _record(), _record(provider_model_name="llama3:8b"))
    )
    assert len(records_claiming(records, at=_AT)) == 2


def test_among_records_claiming_the_instant_the_most_recently_observed_wins(
    tmp_path: Path,
) -> None:
    """Several observations of one model are a set, not a contradiction; recency decides."""
    old = _record(observed_at="2026-08-01T00:00:00Z")
    new = _record(observed_at="2026-09-02T00:00:00Z")
    new["rates"] = dict(new["rates"], input_per_million_tokens="3.00")
    records = load_pricing_records(_write(tmp_path, old, new))
    chosen = price_for_model(records, canonical_id="ollama/qwen3:8b", at=_AT)
    assert chosen is not None
    assert chosen.rates.input_per_million_tokens == Money.from_decimal("USD", "3.00")
