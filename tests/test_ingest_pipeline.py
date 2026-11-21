import asyncio
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from backend.services import grn_lines, ingest_service, matching_service
from backend.services.canonical import CanonicalInvoice, CanonicalLine, harmonise_extracted_amounts
from backend.services.doc_classifier import GRN, INVOICE, PURCHASE_ORDER, UNKNOWN, classify_document
from backend.services import document_text_ocr
from backend.services.ingest_service import _enrich_line_codes

SCANNED_GRN_TEXT = """SRI VENKATESWARA AUTO COMPONENTS PVT LTD
GOODS RECEIPT NOTE
GRN No.: GRN/26-27/0733 Supplier: Kaveri Fasteners & Tools
GRN Date: 07-Nov-2026 Supplier DC/Challan No.: DC-1187
PO No.: SVAC/PO/26-27/0412 Vehicle No.: TN 09 CE 4471
Description PO Qty Challan Received Accepted Rejected
Qty Qty Qty Qty
"""

PO_TEXT = """Lakshmi Precision Castings
GST: 33AAFFL7733C1ZL
P.O.
Supplier
Annapoorna Traders
P.O. No.: LPC/PO/0267
P.O. Date: 03/11/2026
Delivery Schedule: By 12/11/2026
Net Total: 1,23,600.00    Taxes: CGST 9%: 9,819.00
"""

GRN_TEXT = """MATERIAL RECEIPT NOTE
MRR No.: MRR-2026-0581
Against P.O. No.: LPC/PO/0267
Qty Accepted   Qty Received
"""

INVOICE_TEXT = """ANNAPOORNA TRADERS
GSTIN: 33ACTPA6618L1ZC
BILL / CASH-CREDIT MEMO
Bill No.: AT/0376
Date: 12.11.26
Your Order No.: LPC/PO/0267
"""


def test_classifier_handles_dotted_po_header():
    result = classify_document(PO_TEXT)
    assert result.doc_type == PURCHASE_ORDER
    assert result.document_number == "LPC/PO/0267"


def test_classifier_reads_grn_and_invoice_numbers():
    grn = classify_document(GRN_TEXT)
    assert grn.doc_type == GRN
    assert grn.po_reference == "LPC/PO/0267"

    invoice = classify_document(INVOICE_TEXT)
    assert invoice.doc_type == INVOICE
    assert invoice.document_number == "AT/0376"


def test_classifier_normalizes_wrapped_labels_and_grn_signals():
    result = classify_document(SCANNED_GRN_TEXT)

    assert result.doc_type == GRN
    assert not result.needs_llm
    assert result.document_number == "GRN/26-27/0733"
    assert result.po_reference == "SVAC/PO/26-27/0412"
    assert result.vendor_name == "Kaveri Fasteners & Tools"


def test_classifier_recognizes_invoice_number_hash_label():
    result = classify_document(
        "BILL TO SHIP TO Invoice #: SGP/2627/01184\n"
        "Date: 09/11/2026\nPO Ref: SVAC/PO/26-27/0418\n"
        "Grand Total 2,08,388.00"
    )

    assert result.doc_type == INVOICE
    assert result.document_number == "SGP/2627/01184"
    assert not result.needs_llm


def test_invoice_metadata_alone_does_not_override_weak_document_text():
    class Metadata:
        invoice_number = "INV/123"

    class Extracted:
        metadata = Metadata()

    result = classify_document("PO No: SVAC/0412\nSupplier: Kaveri Fasteners", Extracted())

    assert result.doc_type == UNKNOWN
    assert result.needs_llm


def test_document_text_ocr_reports_attempt_and_returns_text(monkeypatch):
    class Completed:
        returncode = 0
        stdout = b"GOODS RECEIPT NOTE"
        stderr = b""

    monkeypatch.setattr(document_text_ocr.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(document_text_ocr.subprocess, "run", lambda *args, **kwargs: Completed())

    text, attempt = document_text_ocr.extract_document_text(b"image", "grn.jpg", "image/jpeg")

    assert text == "GOODS RECEIPT NOTE"
    assert attempt["engine"] == "tesseract"
    assert attempt["ok"]
    assert attempt["text_characters"] == len(text)


def test_document_text_ocr_marks_near_empty_output(monkeypatch):
    class Completed:
        returncode = 0
        stdout = b"GRN No"
        stderr = b""

    monkeypatch.setattr(document_text_ocr.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(document_text_ocr.subprocess, "run", lambda *args, **kwargs: Completed())

    text, attempt = document_text_ocr.extract_document_text(b"image", "grn.jpg", "image/jpeg")

    assert text == "GRN No"
    assert not attempt["ok"]
    assert attempt["reason"] == "near_empty"


def test_document_text_ocr_reports_missing_tesseract(monkeypatch):
    monkeypatch.setattr(document_text_ocr.shutil, "which", lambda _: None)

    text, attempt = document_text_ocr.extract_document_text(b"image", "grn.jpg", "image/jpeg")

    assert text == ""
    assert attempt["reason"] == "not_installed"


def test_enrich_line_codes_prefers_document_code_over_hsn():
    extracted = {
        "line_items": [
            {"item_code": "4819", "description": "Code: CTN-5P-450"},
            {"item_code": None, "description": "Bubble wrap (Code: BUB-1M100)"},
        ]
    }
    enriched = _enrich_line_codes(copy.deepcopy(extracted), raw_text="no codes in this text")
    assert [line["item_code"] for line in enriched["line_items"]] == ["CTN-5P-450", "BUB-1M100"]
    # the caller's payload must not be mutated when a copy is passed
    assert extracted["line_items"][0]["item_code"] == "4819"


def test_enrich_line_codes_pairs_positionally_when_counts_match():
    text = "Code: CTN-5P-450\nCode: BUB-1M100\n"
    extracted = {"line_items": [{"description": "Box"}, {"description": "Bubble wrap"}]}
    enriched = _enrich_line_codes(copy.deepcopy(extracted), raw_text=text)
    assert [line["item_code"] for line in enriched["line_items"]] == ["CTN-5P-450", "BUB-1M100"]


def test_harmonise_rebuilds_gross_and_scaled_line_amounts():
    invoice = CanonicalInvoice(
        invoice_number="SGP/2627/01184",
        lines=[
            # amount is gross of 18% tax and an extra zero
            CanonicalLine(line_no=1, quantity=3000, rate=40.2, tax_rate=18, amount=1423080, tax_amount=21708),
            # consistent line, left untouched
            CanonicalLine(line_no=2, quantity=40, rate=920, tax_rate=18, amount=36800, tax_amount=6624),
        ],
        subtotal=1760000,
        total_tax=31788,
        grand_total=2088000,
    )

    harmonised = harmonise_extracted_amounts(invoice)

    assert [line.amount for line in harmonised.lines] == [120600.0, 36800.0]
    assert [line.tax_amount for line in harmonised.lines] == [21708.0, 6624.0]
    assert harmonised.subtotal == 157400.0
    assert harmonised.total_tax == 28332.0
    assert harmonised.grand_total == 185732.0


# ---------------------------------------------------------------------------
# Scanned GRN intake uses the table reader before falling back to extraction
# ---------------------------------------------------------------------------
requires_ocr = pytest.mark.skipif(
    not grn_lines.available(), reason="OpenCV and Tesseract are required"
)
SAMPLES = Path(__file__).resolve().parent / "ap_samples"

SCANNED_GRN_B_TEXT = """Lakshmi Precision Castings
MATERIAL RECEIPT REPORT
MRR No: MRR-2026-0581  Date: 12.11.26  Against P.O.: LPC/PO/0267
Qty as per DC   Qty Received   Qty Accepted   Condition / Remarks
"""


def _stub_extraction(monkeypatch, text=SCANNED_GRN_B_TEXT):
    """The table reader is under test, so the invoice extractor returns nothing."""

    async def fake_extract(*args, **kwargs):
        return SimpleNamespace(
            raw_text=text,
            line_items=[],
            engine_used="stub",
            validation_summary={},
            extraction_log_id=None,
            summary=None,
            metadata=SimpleNamespace(currency="INR"),
        )

    monkeypatch.setattr(ingest_service, "extract_document_text", lambda *a, **k: (text, {"engine": "tesseract", "ok": True}))
    monkeypatch.setattr(ingest_service, "_extract", fake_extract)
    monkeypatch.setattr(ingest_service, "save_bytes", lambda *a, **k: "test/source")


def _ingest_scanned_grn(db, scenario):
    data = (SAMPLES / "scanned" / f"grn_{scenario}_scan.jpg").read_bytes()
    upload = UploadFile(
        filename=f"grn_{scenario}_scan.jpg",
        file=io.BytesIO(data),
        headers={"content-type": "image/jpeg"},
    )
    return asyncio.run(ingest_service.ingest_upload(db, upload, extraction_method="local"))


@requires_ocr
def test_scanned_grn_b_uses_the_table_reader(db, monkeypatch):
    truth = json.loads((SAMPLES / "truth" / "po_S3.json").read_text())
    matching_service.import_purchase_order(db, truth, source="TEST")
    _stub_extraction(monkeypatch)

    result = _ingest_scanned_grn(db, "S3")

    assert result["action"] == "grn_imported"
    assert result["record"]["lines"] == 3
    assert [line["received_qty"] for line in result["record"]["line_items"]] == [4.0, 6.0, 8.0]
    assert not result["review_required"]


@requires_ocr
def test_scanned_grn_misread_accepted_digit_is_repaired(db, monkeypatch):
    """S1's Accepted cell reads "26" against received 25; re-reading it fixes it."""
    truth = json.loads((SAMPLES / "truth" / "po_S1.json").read_text())
    matching_service.import_purchase_order(db, truth, source="TEST")
    _stub_extraction(
        monkeypatch,
        text="SRI VENKATESWARA AUTO COMPONENTS PVT LTD\nGOODS RECEIPT NOTE\n"
        "GRN No.: GRN/26-27/0733\nPO No.: SVAC/PO/26-27/0412\n"
        "Description PO Qty Challan Received Accepted Rejected\n",
    )

    result = _ingest_scanned_grn(db, "S1")

    assert result["action"] == "grn_imported"
    assert not result["review_required"]
    assert "accepted 26" not in result["message"]


# ---------------------------------------------------------------------------
# Header extraction that survives wrapped OCR lines
# ---------------------------------------------------------------------------
def test_classifier_finds_po_reference_when_label_and_value_wrap():
    text = (
        "Tax Invoice\nInvoice No. ire ed Dated ;\n"
        "KFT/26-27/0891 | 06-Nov-2026\n"
        "Buyer's Order No ; Dated\nSri Venkateswara Auto Components Pvt Ltd\n"
        "SVAC/PO/26-27/0412 28-Oct-2026\n"
    )

    result = classify_document(text)

    assert result.doc_type == INVOICE
    assert result.document_number == "KFT/26-27/0891"
    assert result.po_reference == "SVAC/PO/26-27/0412"


def test_classifier_reads_vendor_from_the_line_after_the_label():
    text = "PURCHASE ORDER\nTo (Supplier): PO No.: SVAC/PO/26-27/0412\nKaveri Fasteners & Tools PO Date: 28-Oct-2026\n"

    result = classify_document(text)

    assert result.doc_type == PURCHASE_ORDER
    assert result.vendor_name == "Kaveri Fasteners & Tools"


@requires_ocr
def test_reuploading_a_grn_does_not_double_count_or_claim_it_over_receives(db, monkeypatch):
    truth = json.loads((SAMPLES / "truth" / "po_S1.json").read_text())
    matching_service.import_purchase_order(db, truth, source="TEST")
    _stub_extraction(
        monkeypatch,
        text="SRI VENKATESWARA AUTO COMPONENTS PVT LTD\nGOODS RECEIPT NOTE\n"
        "GRN No.: GRN/26-27/0733\nPO No.: SVAC/PO/26-27/0412\n"
        "Description PO Qty Challan Received Accepted Rejected\n",
    )

    _ingest_scanned_grn(db, "S1")
    second = _ingest_scanned_grn(db, "S1")

    from backend.database import GoodsReceipt, POLineBalance

    assert not second["review_required"], second["message"]
    assert "exceeds" not in second["message"]
    assert db.query(GoodsReceipt).count() == 1
    balances = {
        balance.po_line.line_no: float(balance.received_qty)
        for balance in db.query(POLineBalance).all()
    }
    assert balances == {1: 2000.0, 2: 2000.0, 3: 2000.0, 4: 25.0}


def test_invoice_number_is_taken_from_the_page_when_the_model_invents_one():
    from backend.services.doc_classifier import Classification
    from backend.services.ingest_service import _reconcile_extracted_number

    extracted = {"metadata": {"invoice_number": "6T/26-27/0891"}}
    cls = Classification(doc_type=INVOICE, confidence=0.9, document_number="KFT/26-27/0891")

    fixed = _reconcile_extracted_number(extracted, cls, "KFT/26-27/0891 | 06-Nov-2026")

    assert fixed["metadata"]["invoice_number"] == "KFT/26-27/0891"


def test_invoice_number_already_on_the_page_is_kept():
    from backend.services.doc_classifier import Classification
    from backend.services.ingest_service import _reconcile_extracted_number

    extracted = {"metadata": {"invoice_number": "KFT/26-27/0891"}}
    cls = Classification(doc_type=INVOICE, confidence=0.9, document_number="KFT/26-27/0891")

    fixed = _reconcile_extracted_number(extracted, cls, "KFT/26-27/0891")

    assert fixed["metadata"]["invoice_number"] == "KFT/26-27/0891"


def test_document_text_ocr_uses_a_pdf_text_layer():
    data = (SAMPLES / "pdf" / "grn_S1.pdf").read_bytes()

    text, attempt = document_text_ocr.extract_document_text(data, "grn_S1.pdf", "application/pdf")

    assert "GOODS RECEIPT NOTE" in text.upper()
    assert attempt["engine"] == "pymupdf"
    assert attempt["ok"]


@requires_ocr
def test_document_text_ocr_rasterises_a_pdf_without_a_text_layer():
    import pymupdf

    image = (SAMPLES / "scanned" / "grn_S1_scan.jpg").read_bytes()
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=image)
    data = document.tobytes()
    document.close()

    text, attempt = document_text_ocr.extract_document_text(data, "scan.pdf", "application/pdf")

    assert attempt["engine"] == "tesseract-pdf"
    assert attempt["ok"]
    assert "RECEIPT" in text.upper()


@requires_ocr
def test_digital_pdf_grn_uses_the_table_reader(db, monkeypatch):
    truth = json.loads((SAMPLES / "truth" / "po_S1.json").read_text())
    matching_service.import_purchase_order(db, truth, source="TEST")
    _stub_extraction(
        monkeypatch,
        text="SRI VENKATESWARA AUTO COMPONENTS PVT LTD\nGOODS RECEIPT NOTE\n"
        "GRN No.: GRN/26-27/0733\nPO No.: SVAC/PO/26-27/0412\n",
    )
    data = (SAMPLES / "pdf" / "grn_S1.pdf").read_bytes()
    upload = UploadFile(
        filename="grn_S1.pdf",
        file=io.BytesIO(data),
        headers={"content-type": "application/pdf"},
    )

    result = asyncio.run(ingest_service.ingest_upload(db, upload, extraction_method="local"))

    assert result["action"] == "grn_imported"
    assert result["record"]["lines"] == 4
    assert not result["review_required"], result["message"]
