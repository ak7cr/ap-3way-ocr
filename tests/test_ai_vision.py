"""Forced-AI extractions must keep their text and an audit record."""
from types import SimpleNamespace

from backend.services import ai_vision
from backend.services.ai_vision import finalise_forced_ai


def _ai_result(raw_text=""):
    return SimpleNamespace(
        raw_text=raw_text,
        metadata=SimpleNamespace(invoice_number="INV-1"),
        seller=SimpleNamespace(name="Kaveri Fasteners"),
        buyer=SimpleNamespace(name="Sri Venkateswara"),
        line_items=[SimpleNamespace(item_name="Hex Bolt", description="Hex Bolt")],
        document_preview_urls=[],
        bounding_boxes=[],
        status=None,
    )


def _patch(monkeypatch, captured, confidence=0.8):
    monkeypatch.setattr(
        ai_vision,
        "validate_invoice",
        lambda invoice: SimpleNamespace(
            passed=True,
            auto_acceptable=False,
            overall_confidence=confidence,
            errors=[],
            warnings=[],
        ),
    )
    monkeypatch.setattr(ai_vision, "learn_layout_proposal", lambda *args, **kwargs: None)

    def fake_log(invoice, **kwargs):
        captured.update(kwargs)
        invoice.extraction_log_id = 42

    monkeypatch.setattr(ai_vision, "log_extraction_event", fake_log)


def test_forced_ai_keeps_ocr_text_and_creates_the_extraction_record(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    result = _ai_result()

    out = finalise_forced_ai(
        result,
        content=b"image-bytes",
        db=None,
        raw_text="GOODS RECEIPT NOTE",
        timing={"total_processing_ms": 12.0},
    )

    assert out.raw_text == "GOODS RECEIPT NOTE"
    assert out.extraction_log_id == 42
    assert out.status == "NEEDS_REVIEW"
    assert out.validation_summary["overall_confidence"] == 0.8
    assert captured["match_strategy"] == "AI_FORCED"
    assert captured["raw_content"] == b"image-bytes"


def test_forced_ai_builds_bounding_boxes_from_ocr_words(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    result = _ai_result()

    out = finalise_forced_ai(
        result,
        content=b"image-bytes",
        db=None,
        raw_text="text",
        words_data=[{"text": "Kaveri", "x0": 0, "y0": 0, "x1": 10, "y1": 10}],
        img_w=100.0,
        img_h=100.0,
    )

    assert out.bounding_boxes[0].label == "seller_name"


def test_forced_ai_keeps_existing_text_when_no_ocr_text_is_available(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    result = _ai_result(raw_text="model text")

    out = finalise_forced_ai(result, content=b"x", db=None, raw_text="")

    assert out.raw_text == "model text"
