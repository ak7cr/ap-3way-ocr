"""
Integration tests for the match service on an in-memory SQLite database.

Covers the parts the pure engine cannot: PO/GRN persistence, cumulative line
balances across partial deliveries, exception workflow,
segregation of duties, payment export and metrics.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.services import matching_service as svc
from backend.services.canonical import CanonicalInvoice, canonical_invoice_from_extraction
from backend.services.matching_engine import STATUS_EXCEPTION, STATUS_MATCHED, STATUS_REJECT
from backend.schemas import InvoiceData


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_vendor_po(db, ordered=100, received=None, rate=62.50):
    vendor = svc.upsert_vendor(
        db,
        {"code": "V-1", "name": "Acme Steel Pvt Ltd", "gstin": "27AABCU9603R1ZM", "risk_tier": "LOW"},
    )
    po = svc.import_purchase_order(
        db,
        {
            "po_number": "PO-100",
            "vendor_name": vendor.name,
            "vendor_gstin": "27AABCU9603R1ZM",
            "po_date": "2026-11-01",
            "currency": "INR",
            "status": "OPEN",
            "lines": [
                {"line_no": 1, "item_code": "STL-100", "description": "MS Angle 50x50x6", "quantity": ordered, "unit": "KG", "rate": rate, "tax_rate": 18},
            ],
        },
        source="TEST",
    )
    if received:
        svc.import_grn(
            db,
            {
                "grn_number": "GRN-1",
                "po_number": "PO-100",
                "receipt_date": "2026-11-05",
                "lines": [{"line_no": 1, "po_line_no": 1, "item_code": "STL-100", "received_quantity": received}],
            },
            source="TEST",
        )
    return vendor, po


def make_invoice(qty, rate=62.50, number="INV-1", date="2026-11-08", tax=18):
    amount = Decimal(str(qty)) * Decimal(str(rate))
    tax_amount = (amount * Decimal(str(tax)) / Decimal("100")).quantize(Decimal("0.01"))
    return CanonicalInvoice(
        vendor_name="Acme Steel Pvt Ltd",
        vendor_gstin="27AABCU9603R1ZM",
        invoice_number=number,
        invoice_date=date,
        po_number="PO-100",
        currency="INR",
        subtotal=float(amount),
        total_tax=float(tax_amount),
        cgst_total=float((tax_amount / 2).quantize(Decimal("0.01"))),
        sgst_total=float((tax_amount / 2).quantize(Decimal("0.01"))),
        grand_total=float(amount + tax_amount),
        lines=[
            {
                "line_no": 1,
                "item_code": "STL-100",
                "description": "MS Angle 50x50x6",
                "quantity": qty,
                "unit": "kgs",
                "rate": rate,
                "tax_rate": tax,
                "amount": float(amount),
            }
        ],
    )


def test_full_po_grn_invoice_match(db):
    seed_vendor_po(db, ordered=100, received=100)
    doc = svc.create_invoice_document(db, make_invoice(100))
    result = svc.run_match_for_invoice(db, doc.id)
    assert result.status == STATUS_MATCHED
    assert result.reason_codes == []
    assert result.approval_status == "PENDING"
    assert result.approvals_required >= 1


def test_partial_delivery_updates_balance_and_allows_second_invoice(db):
    seed_vendor_po(db, ordered=100, received=100)
    first = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(40, number="INV-1")).id)
    assert first.status == STATUS_MATCHED

    po = db.query(svc.PurchaseOrder).filter(svc.PurchaseOrder.po_number == "PO-100").first()
    balance = po.lines[0].balance
    assert Decimal(balance.invoiced_qty) == Decimal("40")

    second = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(60, number="INV-2")).id)
    assert second.status == STATUS_MATCHED
    db.refresh(balance)
    assert Decimal(balance.invoiced_qty) == Decimal("100")


def test_bill_more_than_received_is_exception(db):
    seed_vendor_po(db, ordered=100, received=60)
    doc = svc.create_invoice_document(db, make_invoice(80))
    result = svc.run_match_for_invoice(db, doc.id)
    assert result.status == STATUS_EXCEPTION
    assert "QTY_EXCEEDS_GRN" in result.reason_codes


def test_repeating_an_invoice_number_is_not_a_duplicate_check(db):
    """Duplicate detection was removed; matching judges the quantities instead."""
    seed_vendor_po(db, ordered=100, received=100)
    first = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(50, number="INV-DUP")).id)
    assert first.status == STATUS_MATCHED

    second = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(50, number="INV-DUP")).id)

    assert "DUPLICATE_INVOICE" not in second.reason_codes
    assert second.duplicate_of_invoice_id is None


def test_closed_po_rejects_invoice(db):
    vendor, po = seed_vendor_po(db, ordered=100, received=100)
    po.status = "CLOSED"
    db.commit()
    result = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(10)).id)
    assert result.status == STATUS_REJECT
    assert "PO_NOT_OPEN" in result.reason_codes


def test_override_requires_reason_and_prevents_self_approval(db):
    seed_vendor_po(db, ordered=100, received=40)
    result = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(60)).id)
    assert result.status == STATUS_EXCEPTION

    with pytest.raises(ValueError):
        svc.apply_exception_action(db, result.id, "ACCEPT_OVERRIDE", "", "alice")

    resolved = svc.apply_exception_action(db, result.id, "ACCEPT_OVERRIDE", "Vendor confirmed 60 shipped", "alice")
    assert resolved.resolution == "OVERRIDDEN"

    with pytest.raises(ValueError):
        svc.submit_approval(db, resolved.id, "alice", "finance", "APPROVED")

    approved = svc.submit_approval(db, resolved.id, "bob", "finance", "APPROVED")
    assert approved.approval_status in {"APPROVED", "PENDING"}


def test_approval_requires_exception_to_be_resolved(db):
    seed_vendor_po(db, ordered=100, received=40)
    result = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(60)).id)
    with pytest.raises(ValueError):
        svc.submit_approval(db, result.id, "bob", "finance", "APPROVED")


def test_approval_completes_and_exports_payment(db):
    seed_vendor_po(db, ordered=100, received=100)
    result = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(100)).id)

    for approver in ("alice", "bob")[: result.approvals_required]:
        final = svc.submit_approval(db, result.id, approver, "finance", "APPROVED")
    assert final.approval_status == "APPROVED"

    rows = svc.payment_ready_rows(db)
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "INV-1"
    assert rows[0]["po_number"] == "PO-100"

    csv_text = svc.payment_csv(db)
    assert "INV-1" in csv_text and "vendor_gstin" in csv_text.splitlines()[0]

    xml_text = svc.payment_tally_xml(db)
    assert "<VOUCHERNUMBER>INV-1</VOUCHERNUMBER>" in xml_text


def test_metrics_report_straight_through_and_exceptions(db):
    seed_vendor_po(db, ordered=100, received=100)
    svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(10, number="INV-1")).id)
    svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(200, number="INV-2")).id)

    metrics = svc.compute_metrics(db)
    assert metrics["total_invoices"] == 2
    assert metrics["matched"] == 1
    assert metrics["exceptions"] >= 1
    assert 0.0 <= metrics["straight_through_rate"] <= 1.0
    assert "QTY_EXCEEDS_GRN" in metrics["exception_counts_by_reason"]


def test_po_and_grn_csv_import(db):
    svc.upsert_vendor(db, {"code": "V-CSV", "name": "CSV Vendor", "gstin": "27AABCU9603R1Z9"})
    po_csv = (
        "po_number,vendor_name,vendor_gstin,po_date,currency,status,item_code,description,quantity,unit,rate,tax_rate\n"
        "PO-CSV-1,CSV Vendor,27AABCU9603R1Z9,2026-11-01,INR,OPEN,ITEM-1,Widget,10,PCS,100,18\n"
    )
    pos = svc.import_po_csv(db, po_csv)
    assert len(pos) == 1 and pos[0].po_number == "PO-CSV-1"

    grn_csv = "grn_number,po_number,receipt_date,item_code,received_qty\nGRN-CSV-1,PO-CSV-1,2026-11-03,ITEM-1,10\n"
    grns = svc.import_grn_csv(db, grn_csv)
    assert len(grns) == 1

    po = db.query(svc.PurchaseOrder).filter(svc.PurchaseOrder.po_number == "PO-CSV-1").first()
    assert Decimal(po.lines[0].balance.received_qty) == Decimal("10")


def test_side_by_side_highlights_mismatched_fields(db):
    seed_vendor_po(db, ordered=100, received=40)
    result = svc.run_match_for_invoice(db, svc.create_invoice_document(db, make_invoice(60)).id)
    view = svc.build_side_by_side(db, result)
    assert view["status"] == STATUS_EXCEPTION
    assert view["lines"][0]["mismatched_fields"]
    assert view["invoice"]["invoice_number"] == "INV-1"
    assert view["workflow"]["approval_status"] == "PENDING"


def test_canonical_adapter_maps_ocr_invoice_data():
    ocr = InvoiceData(
        metadata={"invoice_number": "OCR-1", "invoice_date": "2026-11-08", "po_number": "PO-9", "currency": "INR"},
        seller={"name": "Acme Steel Pvt Ltd", "gstin": "27AABCU9603R1ZM"},
        line_items=[{"sno": "1", "description": "MS Angle", "quantity": 2, "unit_price": 10, "tax_rate": 18, "total_amount": 20}],
        summary={"subtotal": 20, "total_gst": 3.6, "grand_total": 23.6},
    )
    canonical = canonical_invoice_from_extraction(ocr)
    assert canonical.invoice_number == "OCR-1"
    assert canonical.po_number == "PO-9"
    assert canonical.lines[0].quantity == 2
    assert canonical.field_source["invoice_number"] == "OCR"


def test_route_approval_uses_amount_bands_and_risk():
    from backend.database import MatchResult

    low = MatchResult(status=STATUS_MATCHED)
    svc.route_approval(low, Decimal("1000"), "LOW", [])
    assert low.required_roles == ["finance"]

    high_value = MatchResult(status=STATUS_MATCHED)
    svc.route_approval(high_value, Decimal("500000"), "LOW", [])
    assert set(high_value.required_roles) == {"finance", "director"}

    risky = MatchResult(status=STATUS_MATCHED)
    svc.route_approval(risky, Decimal("1000"), "HIGH", ["QTY_EXCEEDS_PO"])
    assert "manager" in risky.required_roles and "director" in risky.required_roles

    rejected = MatchResult(status=STATUS_REJECT)
    svc.route_approval(rejected, Decimal("1000"), "LOW", ["PO_NOT_FOUND"])
    assert rejected.approval_status == "NOT_REQUIRED"


def test_tolerances_resolve_from_category_and_vendor():
    from backend.config import resolve_tolerances

    default = resolve_tolerances()
    commodity = resolve_tolerances(category="commodity")
    vendor = resolve_tolerances(vendor_name="Acme", vendor_override={"price_pct": "0.05"})

    assert default.price_pct == Decimal("0.02")
    assert commodity.price_pct == Decimal("0.03")
    assert vendor.price_pct == Decimal("0.05")


def test_normalization_helpers():
    from backend.services.normalization import (
        normalize_gstin,
        normalize_item_code,
        normalize_uom,
        resolve_vendor,
        similarity,
        VendorRef,
    )

    assert normalize_uom("kgs") == "KG"
    assert normalize_uom("Kilogram") == "KG"
    assert normalize_uom("nos") == "PCS"
    assert normalize_item_code("stl-100") == "STL100"
    assert normalize_gstin("27 aabcu9603r1zm") == "27AABCU9603R1ZM"
    assert similarity("MS Angle 50x50x6", "MS ANGLE 50 x 50 x 6") > 0.85

    vendors = [
        VendorRef(id=1, code="V-1", name="Acme Steel Pvt Ltd", gstin="27AABCU9603R1ZM"),
        VendorRef(id=2, code="V-2", name="Zenith Traders", gstin="29ZZZZZ1234Z1Z5"),
    ]
    assert resolve_vendor(vendors, "Different Name", "27AABCU9603R1ZM").method == "GSTIN"
    assert resolve_vendor(vendors, "Zenith Traders", None).method == "NAME_EXACT"
    assert resolve_vendor(vendors, "Zenith Trader", None).vendor.id == 2
    assert resolve_vendor(vendors, "Unknown Enterprises", None).vendor is None


def test_storage_roundtrip(tmp_path, monkeypatch):
    from backend.services import storage

    monkeypatch.setitem(storage.STORAGE, "root", str(tmp_path))
    key = storage.save_bytes(b"hello", "txt", prefix="test")
    assert storage.read_bytes(key) == b"hello"
    assert storage.public_url(key) == f"/api/files/{key}"
    assert storage.read_bytes("../etc/passwd") is None


def test_recanonicalizing_same_extraction_is_idempotent(db):
    from backend.database import ExtractionLog, MatchResult

    seed_vendor_po(db, ordered=100, received=100)
    ocr_payload = {
        "metadata": {"invoice_number": "INV-OCR-1", "invoice_date": "2026-11-08", "currency": "INR"},
        "seller": {"name": "Acme Steel Pvt Ltd", "gstin": "27AABCU9603R1ZM"},
        "line_items": [
            {"sno": "1", "description": "MS Angle 50x50x6", "quantity": 40, "unit_price": 62.50, "tax_rate": 18, "total_amount": 2500},
        ],
        "summary": {"subtotal": 2500, "total_gst": 450, "grand_total": 2950},
    }
    log = ExtractionLog(engine_used="test", extracted_data=ocr_payload, original_filename="x.pdf")
    db.add(log)
    db.commit()

    first_doc = svc.canonicalize_extraction(db, log, po_number="PO-100")
    first_result = svc.run_match_for_invoice(db, first_doc.id)
    assert first_result.status == STATUS_MATCHED

    po = db.query(svc.PurchaseOrder).filter(svc.PurchaseOrder.po_number == "PO-100").first()
    db.refresh(po.lines[0].balance)
    assert Decimal(po.lines[0].balance.invoiced_qty) == Decimal("40")

    # Re-running with the same PO number must reuse the document, clear the old
    # match and undo its balance instead of double-counting the invoice.
    second_doc = svc.canonicalize_extraction(db, log, po_number="PO-100")
    assert second_doc.id == first_doc.id
    assert db.query(MatchResult).filter(MatchResult.invoice_document_id == first_doc.id).count() == 0
    db.refresh(po.lines[0].balance)
    assert Decimal(po.lines[0].balance.invoiced_qty) == Decimal("0")


def test_po_import_creates_missing_vendor_from_gstin(db):
    svc.import_purchase_order(
        db,
        {
            "po_number": "PO-NEW-VENDOR",
            "vendor_name": "Brand New Supplier",
            "vendor_gstin": "27AABCU9603R1ZX",
            "currency": "INR",
            "lines": [{"line_no": 1, "item_code": "A1", "description": "Item", "quantity": 1, "unit": "PCS", "rate": 10, "tax_rate": 18}],
        },
    )
    vendors = db.query(svc.Vendor).all()
    assert len(vendors) == 1
    assert vendors[0].gstin == "27AABCU9603R1ZX"
    po = db.query(svc.PurchaseOrder).filter(svc.PurchaseOrder.po_number == "PO-NEW-VENDOR").first()
    assert po.vendor_id == vendors[0].id


def test_reimporting_the_same_grn_replaces_instead_of_double_counting(db):
    po = {
        "po_number": "PO-GRN-1",
        "vendor_name": "Kaveri Fasteners & Tools",
        "lines": [
            {"line_no": 1, "item_code": "BLT-M10-60", "description": "Hex Bolt", "quantity": 2000, "rate": 6.5},
            {"line_no": 4, "item_code": "ALN-SET9", "description": "Allen Key", "quantity": 25, "rate": 185},
        ],
    }
    svc.import_purchase_order(db, po)
    payload = {
        "grn_number": "GRN/26-27/0733",
        "po_number": "PO-GRN-1",
        "lines": [
            {"line_no": 1, "item_code": "BLT-M10-60", "received_quantity": 2000},
            {"line_no": 4, "item_code": "ALN-SET9", "received_quantity": 25},
        ],
    }

    svc.import_grn(db, payload)
    # Same receipt, different punctuation from a second scan.
    svc.import_grn(db, {**payload, "grn_number": "GRN 26 27 0733"})

    from backend.database import GoodsReceipt, POLineBalance

    assert db.query(GoodsReceipt).count() == 1
    balances = {
        balance.po_line.line_no: float(balance.received_qty)
        for balance in db.query(POLineBalance).all()
    }
    assert balances == {1: 2000.0, 4: 25.0}


def test_grn_po_line_numbers_keep_lines_apart_when_codes_collide(db):
    """HSN codes are shared by several PO lines; po_line_no must win."""
    po = {
        "po_number": "PO-GRN-2",
        "vendor_name": "Kaveri Fasteners & Tools",
        "lines": [
            {"line_no": n, "item_code": "7318", "description": desc, "quantity": 2000, "rate": 1}
            for n, desc in [(1, "Hex Bolt"), (2, "Hex Nut"), (3, "Spring Washer")]
        ],
    }
    svc.import_purchase_order(db, po)

    grn = svc.import_grn(
        db,
        {
            "grn_number": "GRN-X",
            "po_number": "PO-GRN-2",
            "lines": [
                {"line_no": 1, "po_line_no": 1, "received_quantity": 2000},
                {"line_no": 2, "po_line_no": 2, "received_quantity": 2000},
                {"line_no": 3, "po_line_no": 3, "received_quantity": 2000},
            ],
        },
    )

    assert [line.line_no for line in grn.lines] == [1, 2, 3]
    assert len({line.po_line_id for line in grn.lines}) == 3



def test_reuploading_the_same_invoice_reuses_the_document(db):
    """A second upload of the same invoice updates it instead of double-billing."""
    from backend.database import ExtractionLog, InvoiceDocument

    seed_vendor_po(db, ordered=100, received=100)
    ocr_payload = {
        "metadata": {"invoice_number": "INV-OCR-2", "invoice_date": "2026-11-08", "currency": "INR"},
        "seller": {"name": "Acme Steel Pvt Ltd", "gstin": "27AABCU9603R1ZM"},
        "line_items": [
            {"sno": "1", "description": "MS Angle 50x50x6", "quantity": 40, "unit_price": 62.50, "tax_rate": 18, "total_amount": 2500},
        ],
        "summary": {"subtotal": 2500, "total_gst": 450, "grand_total": 2950},
    }

    first_log = ExtractionLog(engine_used="test", extracted_data=ocr_payload, original_filename="a.pdf")
    db.add(first_log)
    db.commit()
    first_doc = svc.canonicalize_extraction(db, first_log, po_number="PO-100")
    assert svc.run_match_for_invoice(db, first_doc.id).status == STATUS_MATCHED

    # The same invoice arrives again as a new upload (new extraction log).
    second_log = ExtractionLog(engine_used="test", extracted_data=ocr_payload, original_filename="a.pdf")
    db.add(second_log)
    db.commit()
    second_doc = svc.canonicalize_extraction(db, second_log, po_number="PO-100")

    assert second_doc.id == first_doc.id
    assert db.query(InvoiceDocument).count() == 1

    result = svc.run_match_for_invoice(db, second_doc.id)
    assert "DUPLICATE_INVOICE" not in result.reason_codes
    po = db.query(svc.PurchaseOrder).filter(svc.PurchaseOrder.po_number == "PO-100").first()
    db.refresh(po.lines[0].balance)
    assert Decimal(po.lines[0].balance.invoiced_qty) == Decimal("40")


def test_reupload_after_shadow_recording_deletes_the_old_match(db):
    """
    A shadow-mode recommendation references the match result, so re-matching the
    same invoice must clear that row first or the delete hits a foreign key.
    """
    from backend.database import DecisionObservation, ExtractionLog, InvoiceDocument

    seed_vendor_po(db, ordered=100, received=100)
    payload = {
        "metadata": {"invoice_number": "INV-SHADOW", "invoice_date": "2026-11-08", "currency": "INR"},
        "seller": {"name": "Acme Steel Pvt Ltd", "gstin": "27AABCU9603R1ZM"},
        "line_items": [
            {"sno": "1", "description": "MS Angle 50x50x6", "quantity": 40, "unit_price": 62.50, "tax_rate": 18, "total_amount": 2500},
        ],
        "summary": {"subtotal": 2500, "total_gst": 450, "grand_total": 2950},
    }

    first_log = ExtractionLog(engine_used="test", extracted_data=payload, original_filename="a.pdf")
    db.add(first_log)
    db.commit()
    first_doc = svc.canonicalize_extraction(db, first_log, po_number="PO-100")
    first_match = svc.run_match_for_invoice(db, first_doc.id)
    assert db.query(DecisionObservation).filter_by(match_result_id=first_match.id).count() == 1

    second_log = ExtractionLog(engine_used="test", extracted_data=payload, original_filename="a.pdf")
    db.add(second_log)
    db.commit()
    second_doc = svc.canonicalize_extraction(db, second_log, po_number="PO-100")

    assert second_doc.id == first_doc.id
    assert db.query(InvoiceDocument).count() == 1
    assert db.query(DecisionObservation).count() == 0
    assert svc.run_match_for_invoice(db, second_doc.id).status == STATUS_MATCHED
