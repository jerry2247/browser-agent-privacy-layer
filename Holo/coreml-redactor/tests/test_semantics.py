from __future__ import annotations

import pytest

from plva_coreml.ocr import OCRFinding
from plva_coreml.semantics import (
    SemanticPipeline,
    Span,
    _credible_person_hit,
    _detect_heuristics,
    _detect_sensitive_cues,
    _premask,
)


def test_structured_rules_emit_exact_values_for_future_vault_entries() -> None:
    text = "Email alice@example.com and card 4111-1111-1111-1111"

    spans = _detect_heuristics(text)

    assert [(span.label, span.text) for span in spans] == [
        ("CREDIT_CARD", "4111-1111-1111-1111"),
        ("EMAIL", "alice@example.com"),
    ]


def test_premask_preserves_projection_to_raw_value() -> None:
    raw = "Send to alice@example.com now"
    spans = _detect_heuristics(raw)

    masked, starts, ends = _premask(raw, spans)

    sentinel_start = masked.index("[EMAIL]")
    assert raw[starts[sentinel_start] : ends[sentinel_start]] == "alice@example.com"


def test_sensitive_cues_include_password_fields() -> None:
    assert _detect_sensitive_cues("Account password") == ["PASSWORD"]


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("phone +1 (415) 555-0136", "PHONE"),
        ("sk-live-abcdefgh1234", "API_KEY"),
        ("eyJabcd.efghijkl.signature", "AUTH_TOKEN"),
        ("GB82 WEST 1234 5698 7654 32", "BANK_ACCOUNT"),
    ],
)
def test_sensitive_cues_match_original_secret_patterns(text: str, label: str) -> None:
    assert label in _detect_sensitive_cues(text)


def test_semantic_findings_emit_exact_sensitive_value_for_vault() -> None:
    pipeline = object.__new__(SemanticPipeline)
    pipeline._detect_ner = lambda *args: []
    finding = OCRFinding(0, 0, 100, 20, "alice@example.com", 0.9, 0.95)

    result = pipeline.classify((finding,))

    assert result.findings[0].sensitive is True
    assert result.findings[0].labels == ("EMAIL",)
    assert [(value.label, value.value) for value in result.findings[0].values] == [
        ("EMAIL", "alice@example.com")
    ]


@pytest.mark.parametrize("value", ["e", "st", "int", "odel", "eger", "NAME"])
def test_person_ner_rejects_short_low_information_fragments(value: str) -> None:
    assert not _credible_person_hit(Span(0, len(value), "GIVEN_NAME", 0.99, "ner", value))


def test_person_ner_keeps_credible_names_and_does_not_filter_rules() -> None:
    assert _credible_person_hit(Span(0, 5, "GIVEN_NAME", 0.8, "ner", "Alice"))
    assert not _credible_person_hit(Span(0, 5, "GIVEN_NAME", 0.69, "ner", "Alice"))
    assert _credible_person_hit(Span(0, 3, "GIVEN_NAME", 0.2, "heuristic", "Amy"))


def test_person_ner_requires_full_word_boundaries_and_supports_short_name_context() -> None:
    model = "Model"
    assert not _credible_person_hit(Span(1, 5, "SURNAME", 0.99, "ner", "odel"), model)
    assert _credible_person_hit(Span(6, 8, "GIVEN_NAME", 0.8, "ner", "Li"), "Name: Li")
