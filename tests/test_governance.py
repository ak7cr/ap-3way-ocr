"""
Tests for the validation gate, the approve/review/reject decision, template
trust levels, shadow-mode agreement and manual GRN intake.

These are the checks that stand between a bad extraction and a payment, so they
are tested against deliberately broken arithmetic rather than happy paths only.
"""
from decimal import Decimal

import pytest

from backend.config import CANDIDATE, ShadowPolicy, TRUSTED, normalise_template_status
from backend.database import InvoiceTemplate
from backend.schemas import InvoiceData, InvoiceMetadata, InvoiceSummary, LineItem, PartyDetails
from backend.services import grn_intake
from backend.services import matching_service as svc
from backend.services.decision import APPROVE, REJECT, REVIEW, decide
from backend.services.template_engine import confirm_template, save_or_update_template
from backend.services.validation_gate import (
    gstin_check_digit,
    gstin_is_valid,
    po_number_is_valid,
    validate_document,
)

VALID_GSTIN = "27AABCU9603R1ZN"


def make_invoice(**overrides):
    """A small, arithmetically clean invoice that individual tests then break."""
    lines = overrides.pop(
        "lines",
        [
            LineItem(item_code="STL100", description="MS Angle", quantity=10, unit="KG", unit_price=100, tax_rate=18, total_amount=1000),
            LineItem(item_code="STL200", description="MS Plate", quantity=5, unit="KG", unit_price=200, tax_rate=18, total_amount=1000),
        ],
    )
    subtotal = overrides.pop("subtotal", 2000.0)
    total_tax = overrides.pop("total_tax", 360.0)
    grand_total = overrides.pop("grand_total", 2360.0)
    return InvoiceData(
        metadata=InvoiceMetadata(
            invoice_number=overrides.pop("invoice_number", "INV/2026/001"),
            invoice_date=overrides.pop("invoice_date", "05/11/2026"),
            po_number=overrides.pop("po_number", "PO-2026-001"),
            currency="INR",
        ),
        seller=PartyDetails(name="Acme Steel", gstin=overrides.pop("gstin", VALID_GSTIN)),
        buyer=PartyDetails(name="Buyer Ltd", gstin="33AAECS4821K1ZD"),
        line_items=lines,
        summary=InvoiceSummary(
            subtotal=subtotal,
            total_gst=total_tax,
            cgst_total=overrides.pop("cgst_total", total_tax / 2),
            sgst_total=overrides.pop("sgst_total", total_tax / 2),
            igst_total=overrides.pop("igst_total", 0.0),
            grand_total=grand_total,
        ),
        raw_text="TAX INVOICE",
        engine_used="Test Engine",
    )


# ---------------------------------------------------------------------------
# GSTIN checksum
# ---------------------------------------------------------------------------
def test_gstin_check_digit_matches_real_gstins():
    assert gstin_check_digit(VALID_GSTIN) == VALID_GSTIN[-1]
    assert gstin_is_valid(VALID_GSTIN)
    assert gstin_is_valid("33ACTPA6618L1ZC")


def test_gstin_with_bad_check_digit_is_invalid():
    tampered = VALID_GSTIN[:-1] + ("A" if VALID_GSTIN[-1] != "A" else "B")
    assert not gstin_is_valid(tampered)


def test_po_number_format_check():
    assert po_number_is_valid("PO-2026-001")
    assert po_number_is_valid("SVAC/PO/26-27/0412")
    assert not po_number_is_valid("No")
    assert not po_number_is_valid("1234")


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
def test_clean_invoice_passes_the_gate():
    result = validate_document(make_invoice())
    assert result.passed, result.errors
    assert "LINE_SUM_MATCHES_TAXABLE" in [c.code for c in result.checks if c.status == "PASS"]


def test_line_sum_mismatch_blocks_the_document():
    invoice = make_invoice(subtotal=2500.0)  # lines still sum to 2000
    result = validate_document(invoice)
    assert not result.passed
    assert "LINE_SUM_MISMATCH" in result.errors


def test_tax_recompute_mismatch_blocks_the_document():
    # Lines imply 18% of 2000 = 360; the document claims 500.
    invoice = make_invoice(total_tax=500.0, cgst_total=250.0, sgst_total=250.0, grand_total=2500.0)
    result = validate_document(invoice)
    assert not result.passed
    assert "TAX_RECOMPUTE_MISMATCH" in result.errors


def test_grand_total_mismatch_blocks_the_document():
    invoice = make_invoice(grand_total=9000.0)
    result = validate_document(invoice)
    assert not result.passed
    assert "GRAND_TOTAL_MISMATCH" in result.errors


def test_invalid_gstin_checksum_is_a_hard_failure():
    invoice = make_invoice(gstin="27AABCU9603R1ZZ")
    result = validate_document(invoice)
    assert not result.passed
    assert "GSTIN_CHECKSUM_INVALID" in result.errors


def test_missing_invoice_number_blocks():
    invoice = make_invoice(invoice_number="")
    result = validate_document(invoice)
    assert "MISSING_INVOICE_NUMBER" in result.errors


def test_no_line_items_blocks():
    invoice = make_invoice(lines=[])
    result = validate_document(invoice)
    assert not result.passed
    assert "NO_LINE_ITEMS" in result.errors


def test_document_with_no_extraction_fails():
    result = validate_document(None)
    assert not result.passed
    assert "NO_EXTRACTION" in result.errors


# ---------------------------------------------------------------------------
# Approve / review / reject
# ---------------------------------------------------------------------------
SHADOW_OFF = ShadowPolicy(mode="shadow")
LIVE_EARNED = ShadowPolicy(mode="live")


def test_clean_match_is_reviewed_in_shadow_mode_not_approved():
    outcome = decide(
        validation_passed=True,
        match_status="MATCHED",
        reason_codes=[],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        policy=SHADOW_OFF,
    )
    assert outcome.decision == REVIEW
    assert "SHADOW_MODE" in outcome.reason_codes
    assert not outcome.auto_approve_allowed


def test_duplicate_is_rejected_outright():
    outcome = decide(
        validation_passed=True,
        match_status="REJECT",
        reason_codes=["DUPLICATE_INVOICE"],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REJECT
    assert "DUPLICATE_INVOICE" in outcome.reason_codes


def test_invalid_gstin_is_rejected_even_with_no_match_reasons():
    outcome = decide(
        validation_passed=False,
        validation_errors=["GSTIN_CHECKSUM_INVALID"],
        match_status=None,
        reason_codes=[],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REJECT


def test_missing_po_is_rejected():
    outcome = decide(
        validation_passed=True,
        match_status="REJECT",
        reason_codes=["PO_NOT_FOUND"],
        vendor_known=True,
        po_exists=False,
        po_open=False,
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REJECT


def test_quantity_exception_goes_to_review_not_reject():
    outcome = decide(
        validation_passed=True,
        match_status="EXCEPTION",
        reason_codes=["QTY_EXCEEDS_GRN"],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REVIEW
    assert "QTY_EXCEEDS_GRN" in outcome.reason_codes


def test_unknown_vendor_goes_to_review():
    outcome = decide(
        validation_passed=True,
        match_status="MATCHED",
        reason_codes=[],
        vendor_known=False,
        po_exists=True,
        po_open=True,
        vendor_agreement={"decisions": 100, "agreement": 1.0},
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REVIEW
    assert "UNKNOWN_VENDOR" in outcome.reason_codes


def test_live_mode_approves_a_vendor_that_earned_it():
    outcome = decide(
        validation_passed=True,
        match_status="MATCHED",
        reason_codes=[],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        vendor_agreement={"decisions": 50, "agreement": 0.98},
        policy=LIVE_EARNED,
    )
    assert outcome.decision == APPROVE
    assert outcome.auto_approve_allowed


def test_live_mode_still_holds_a_vendor_without_history():
    outcome = decide(
        validation_passed=True,
        match_status="MATCHED",
        reason_codes=[],
        vendor_known=True,
        po_exists=True,
        po_open=True,
        vendor_agreement={"decisions": 3, "agreement": 1.0},
        policy=LIVE_EARNED,
    )
    assert outcome.decision == REVIEW
    assert outcome.reason_codes == ["VENDOR_NOT_EARNED"]


# ---------------------------------------------------------------------------
# Template trust
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stored,expected",
    [("DRAFT", CANDIDATE), ("ACTIVE", TRUSTED), ("CANDIDATE", CANDIDATE), ("TRUSTED", TRUSTED), ("QUARANTINED", "QUARANTINED")],
)
def test_legacy_statuses_map_onto_candidate_trusted(stored, expected):
    assert normalise_template_status(stored) == expected


def _make_template(db, status=CANDIDATE, confirmations=0):
    template = InvoiceTemplate(
        template_name="Layout A",
        status=status,
        anchor_keywords=["TAX INVOICE"],
        extraction_rules={},
        table_columns=[],
        confirmations=confirmations,
        known_vendors=["Acme Steel"],
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def test_candidate_needs_confirmations_before_it_is_trusted(db):
    template = _make_template(db)
    assert template.status == CANDIDATE

    from backend.config import TEMPLATE_TRUST

    for _ in range(TEMPLATE_TRUST.candidate_confirmations - 1):
        template = confirm_template(db, template.id, actor="accountant")
        assert template.status == CANDIDATE

    template = confirm_template(db, template.id, actor="accountant")
    assert template.status == TRUSTED
    assert template.is_verified


# ---------------------------------------------------------------------------
# Shadow ledger + ingestion of a failed gate
# ---------------------------------------------------------------------------
def seed_po_and_grn(db, rate=100.0, qty=10):
    vendor = svc.upsert_vendor(db, {"code": "V-1", "name": "Acme Steel", "gstin": VALID_GSTIN})
    svc.import_purchase_order(
        db,
        {
            "po_number": "PO-2026-001",
            "vendor_name": vendor.name,
            "vendor_gstin": VALID_GSTIN,
            "status": "OPEN",
            "lines": [
                {"line_no": 1, "item_code": "STL100", "description": "MS Angle", "quantity": qty, "rate": rate, "tax_rate": 18},
            ],
        },
    )
    svc.import_grn(
        db,
        {
            "grn_number": "GRN-1",
            "po_number": "PO-2026-001",
            "lines": [{"po_line_no": 1, "item_code": "STL100", "received_quantity": qty}],
        },
    )


def test_match_records_a_recommendation_for_shadow_mode(db):
    from backend.database import DecisionObservation

    seed_po_and_grn(db)
    doc = svc.create_invoice_document(
        db,
        _canonical_clean_invoice(),
        original_filename="clean.pdf",
        validation_report={"passed": True, "errors": [], "warnings": []},
    )
    result = svc.run_match_for_invoice(db, doc.id)

    assert result.status == "MATCHED"
    # Shadow mode: a human still decides, but the recommendation is stored.
    assert result.decision == REVIEW
    observations = db.query(DecisionObservation).all()
    assert len(observations) == 1
    assert observations[0].recommendation == REVIEW
    assert observations[0].agreement == "PENDING"


def test_human_approval_closes_the_shadow_observation(db):
    from backend.database import DecisionObservation

    seed_po_and_grn(db)
    doc = svc.create_invoice_document(
        db,
        _canonical_clean_invoice(),
        original_filename="clean.pdf",
        validation_report={"passed": True, "errors": [], "warnings": []},
    )
    result = svc.run_match_for_invoice(db, doc.id)
    svc.submit_approval(db, result.id, approver="finance@acme", role="finance", decision="APPROVED")

    observation = db.query(DecisionObservation).first()
    assert observation.human_decision == "APPROVE"
    # We said review, they approved: appropriately cautious, so that is agreement.
    assert observation.agreement == "AGREED"


def test_failed_gate_never_reaches_the_match_engine(db):
    seed_po_and_grn(db)
    report = {"passed": False, "errors": ["LINE_SUM_MISMATCH"], "warnings": []}
    doc = svc.create_invoice_document(db, _canonical_clean_invoice(), original_filename="bad.pdf", validation_report=report)
    result = svc.record_validation_failure(db, doc, report, po_number="PO-2026-001")

    assert result.status == "EXCEPTION"
    assert result.reason_codes == ["LINE_SUM_MISMATCH"]
    assert result.line_findings == []           # the engine did not run
    assert result.decision == REVIEW
    assert result.approval_status == "PENDING"
    assert doc.status == "VALIDATION_FAILED"


def _canonical_clean_invoice():
    from backend.services.canonical import CanonicalInvoice

    return CanonicalInvoice(
        vendor_name="Acme Steel",
        vendor_gstin=VALID_GSTIN,
        invoice_number="ACME/1",
        invoice_date="2026-11-08",
        po_number="PO-2026-001",
        subtotal=1000.0,
        total_tax=180.0,
        cgst_total=90.0,
        sgst_total=90.0,
        grand_total=1180.0,
        lines=[
            {
                "line_no": 1,
                "item_code": "STL100",
                "description": "MS Angle",
                "quantity": 10,
                "rate": 100.0,
                "tax_rate": 18,
                "amount": 1000.0,
            }
        ],
    )


# ---------------------------------------------------------------------------
# Manual GRN intake (the no-OCR path)
# ---------------------------------------------------------------------------
def test_manual_receipt_entry_creates_a_grn(db):
    seed_po_and_grn(db, qty=10)
    payload = grn_intake.build_manual_grn_payload(
        db,
        po_number="PO-2026-001",
        rows=[{"po_line_no": 1, "received_qty": "4"}],
        receipt_date="2026-11-12",
    )
    assert Decimal(payload["lines"][0]["received_quantity"]) == Decimal("4")
    assert payload["grn_number"].startswith("MANUAL/PO-2026-001")

    grn = svc.import_grn(db, payload, source="MANUAL")
    assert len(grn.lines) == 1


def test_manual_entry_rejects_a_line_that_is_not_on_the_po(db):
    seed_po_and_grn(db)
    with pytest.raises(grn_intake.GrnIntakeError) as excinfo:
        grn_intake.build_manual_grn_payload(
            db, po_number="PO-2026-001", rows=[{"item_code": "NOPE-1", "received_qty": "5"}]
        )
    assert "not on PO" in str(excinfo.value)


def test_manual_entry_rejects_zero_quantity_and_unknown_po(db):
    seed_po_and_grn(db)
    with pytest.raises(grn_intake.GrnIntakeError):
        grn_intake.build_manual_grn_payload(db, po_number="PO-2026-001", rows=[{"po_line_no": 1, "received_qty": 0}])
    with pytest.raises(grn_intake.GrnIntakeError):
        grn_intake.build_manual_grn_payload(db, po_number="PO-DOES-NOT-EXIST", rows=[{"po_line_no": 1, "received_qty": 1}])


def test_receipt_sheet_rows_are_grouped_per_grn(db):
    seed_po_and_grn(db)
    text = "po_number,po_line_no,item_code,received_qty\nPO-2026-001,1,STL100,6\n"
    groups = grn_intake.group_receipt_rows(text)
    assert len(groups) == 1
    payload = grn_intake.build_manual_grn_payload(
        db, po_number=groups[0]["po_number"], rows=groups[0]["rows"], receipt_date="2026-11-12"
    )
    assert Decimal(payload["lines"][0]["received_quantity"]) == Decimal("6")
