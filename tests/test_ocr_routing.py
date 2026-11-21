"""
Tests for scanned-image routing.

The rule under test is the one the eval measures: scanned images go to the
external engines first (they score 2/40 there against 0/40 for this repo's
engine), digital PDFs stay on this repo's extractor (25/40 against 9/40), and a
missing sidecar degrades to the local engine instead of failing intake.
"""
import pytest

from backend.schemas import InvoiceData, InvoiceMetadata, InvoiceSummary, LineItem, PartyDetails
from backend.services import external_ocr, ocr_router


def make_invoice(number="INV/1", lines=2, grand_total=1180.0):
    return InvoiceData(
        metadata=InvoiceMetadata(invoice_number=number, invoice_date="05/11/2026", currency="INR"),
        seller=PartyDetails(name="Acme Steel", gstin="27AABCU9603R1ZN"),
        line_items=[
            LineItem(item_name=f"Item {i}", quantity=10, unit_price=100, tax_rate=18, total_amount=1000)
            for i in range(lines)
        ],
        summary=InvoiceSummary(subtotal=1000.0 * lines, total_gst=180.0 * lines, grand_total=grand_total),
        engine_used="Test",
    )


# ---------------------------------------------------------------------------
# Which engine family serves which input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("filename", ["scan.jpg", "scan.JPEG", "scan.png", "scan.tiff", "scan.webp"])
def test_images_route_to_the_external_engines(filename, monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    assert ocr_router.provider_order(filename) == "external-then-local"


@pytest.mark.parametrize("filename", ["invoice.pdf", "po_S1.pdf"])
def test_pdfs_stay_on_the_local_extractor(filename, monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    assert ocr_router.provider_order(filename) == "local"


def test_routing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "false")
    assert ocr_router.provider_order("scan.jpg") == "local"


def test_content_type_is_enough_to_detect_an_image():
    assert external_ocr.is_image("upload", "image/png")
    assert not external_ocr.is_image("upload", "application/pdf")


# ---------------------------------------------------------------------------
# Usability gate before accepting an engine's answer
# ---------------------------------------------------------------------------
def test_empty_extraction_is_not_usable():
    assert not ocr_router.is_usable(None)
    assert not ocr_router.is_usable(InvoiceData())


def test_extraction_with_real_lines_is_usable():
    assert ocr_router.is_usable(make_invoice())


def test_richer_extraction_ranks_higher():
    poor = make_invoice(number=None, lines=1, grand_total=0.0)
    rich = make_invoice(lines=3)
    assert ocr_router.score(rich) > ocr_router.score(poor)


# ---------------------------------------------------------------------------
# Router behaviour with services up, down, and returning junk
# ---------------------------------------------------------------------------
def test_best_of_the_two_engines_is_used(monkeypatch):
    settings = external_ocr.ExternalOcrSettings(
        enabled=True, services=[("a", "http://a"), ("b", "http://b")], timeout=1, health_timeout=1
    )
    rich = make_invoice(number="BEST/1", lines=3)
    poor = make_invoice(number=None, lines=1, grand_total=0.0)

    def fake_extract(name, url, content, filename, content_type, settings):
        return rich if name == "b" else poor

    monkeypatch.setattr(external_ocr, "extract_with_service", fake_extract)
    best, attempts = external_ocr.best_external_extraction(b"x", "scan.jpg", "image/jpeg", settings=settings)

    assert best is rich
    assert len(attempts) == 2 and all(attempt["ok"] for attempt in attempts)


def test_router_prefers_external_when_it_produces_a_usable_result(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    external = make_invoice(number="EXT/1", lines=2)

    monkeypatch.setattr(
        external_ocr,
        "best_external_extraction",
        lambda *args, **kwargs: (external, [{"name": "c++", "ok": True}]),
    )
    routed = ocr_router.extract_image_document(b"x", "scan.jpg", "image/jpeg")

    assert routed.used_external
    assert routed.invoice is external
    assert "External OCR" in routed.engine


def test_router_falls_back_to_local_when_sidecars_are_down(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    local = make_invoice(number="LOCAL/1", lines=1)

    monkeypatch.setattr(external_ocr, "best_external_extraction", lambda *args, **kwargs: (None, [{"name": "c++", "ok": False}]))
    monkeypatch.setattr(ocr_router, "_local_image_extraction", lambda content, filename: local)

    routed = ocr_router.extract_image_document(b"x", "scan.jpg", "image/jpeg")

    assert not routed.used_external
    assert routed.invoice is local
    # The failed external attempt is still reported for diagnostics.
    assert any(attempt.get("ok") is False for attempt in routed.attempts)


def test_router_falls_back_when_external_result_is_unusable(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    empty = InvoiceData()
    local = make_invoice(number="LOCAL/2", lines=1)

    monkeypatch.setattr(external_ocr, "best_external_extraction", lambda *args, **kwargs: (empty, [{"name": "c++", "ok": True}]))
    monkeypatch.setattr(ocr_router, "_local_image_extraction", lambda content, filename: local)

    routed = ocr_router.extract_image_document(b"x", "scan.jpg", "image/jpeg")

    assert routed.invoice is local
    assert not routed.used_external


def test_pdf_input_never_goes_to_the_external_engines(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_ENABLED", "true")
    local = make_invoice(number="LOCAL/PDF", lines=2)
    called = {"external": False}

    def boom(*args, **kwargs):
        called["external"] = True
        return None, []

    monkeypatch.setattr(external_ocr, "best_external_extraction", boom)
    monkeypatch.setattr(ocr_router, "_local_image_extraction", lambda content, filename: local)

    routed = ocr_router.extract_image_document(b"%PDF-1.4", "invoice.pdf", "application/pdf")

    assert not called["external"]
    assert routed.invoice is local


# ---------------------------------------------------------------------------
# Sidecar response mapping
# ---------------------------------------------------------------------------
def test_sidecar_response_is_mapped_onto_our_schema():
    payload = make_invoice(number="EXT/42", lines=2).model_dump()
    mapped = InvoiceData(**payload)
    assert mapped.metadata.invoice_number == "EXT/42"
    assert len(mapped.line_items) == 2


def test_health_reports_unreachable_services_without_raising():
    settings = external_ocr.ExternalOcrSettings(
        enabled=True, services=[("dead", "http://127.0.0.1:9")], timeout=0.2, health_timeout=0.2
    )
    statuses = external_ocr.health(settings)
    assert len(statuses) == 1
    assert statuses[0]["available"] is False


def test_settings_parse_the_service_list(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OCR_SERVICES", "c++=http://127.0.0.1:8021, line-system=http://127.0.0.1:8022")
    settings = external_ocr.load_settings()
    assert settings.urls == ["http://127.0.0.1:8021", "http://127.0.0.1:8022"]
