"""
Evaluation of the AP sample set (tests/ap_samples) against the 3-way match engine.

Ground truth JSON is fed through the real service layer (PO import → GRN import →
invoice → deterministic match), so this exercises normalization, balances,
match outcomes and the reason codes — not just the pure engine.
"""
import importlib.util

import pytest

from ap_samples import eval_harness as harness
from backend.services.matching_engine import STATUS_EXCEPTION, STATUS_MATCHED, STATUS_REJECT

MANIFEST = harness.load_manifest()
SCENARIOS = [entry["scenario"] for entry in MANIFEST]


@pytest.mark.parametrize("entry", MANIFEST, ids=SCENARIOS)
def test_ap_sample_scenario_matches_manifest(db, entry):
    result = harness.run_truth_scenario(db, entry)
    assert result.passed, " | ".join(result.failures)


def test_s1_clean_invoice_reaches_payment_after_approval(db):
    entry = next(e for e in MANIFEST if e["scenario"] == "S1")
    result = harness.run_truth_scenario(db, entry)
    assert result.status == STATUS_MATCHED

    from backend.services import matching_service as svc

    match = db.query(svc.MatchResult).order_by(svc.MatchResult.id.desc()).first()
    roles = match.required_roles or []
    assert roles, "matched invoice should still require payment approval"

    approvers = ["finance-user", "manager-user", "director-user"]
    for approver in approvers[: match.approvals_required]:
        final = svc.submit_approval(db, match.id, approver, "finance", "APPROVED")
    assert final.approval_status == "APPROVED"

    rows = svc.payment_ready_rows(db)
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "KFT/26-27/0891"


def test_s2_partial_delivery_line_is_valid(db):
    entry = next(e for e in MANIFEST if e["scenario"] == "S2")
    result = harness.run_truth_scenario(db, entry)
    assert result.status == STATUS_EXCEPTION
    # line 1 is a legitimate partial delivery (3000 of 5000): only the rate is wrong
    assert "QTY_EXCEEDS_GRN" not in result.line_codes.get(1, [])
    assert "RATE_TOLERANCE_EXCEEDED" in result.line_codes.get(1, [])
    assert "QTY_EXCEEDS_GRN" in result.line_codes.get(3, [])


def test_s3_over_billed_line_is_caught(db):
    entry = next(e for e in MANIFEST if e["scenario"] == "S3")
    result = harness.run_truth_scenario(db, entry)
    assert result.status == STATUS_EXCEPTION
    assert "QTY_EXCEEDS_GRN" in result.line_codes.get(3, [])


def test_grn_uses_accepted_quantity_not_received():
    from backend.services.canonical import canonical_grn_from_dict

    grn = canonical_grn_from_dict(
        {
            "number": "GRN-X",
            "po_ref": "PO-X",
            "lines": [{"item_code": "A", "received_qty": 100, "accepted_qty": 92, "rejected_qty": 8}],
        }
    )
    assert grn.po_number == "PO-X"
    assert grn.lines[0].received_quantity == 92.0


@pytest.mark.skipif(importlib.util.find_spec("pymupdf") is None, reason="PDF dependencies not installed")
def test_pdf_invoice_field_report():
    """Reports digital-PDF extraction accuracy; does not gate on a fixed score."""
    report = harness.evaluate_pdf_invoices()
    assert report["fields_total"] > 0
    for row in report["rows"]:
        assert row["checks"], row["file"]
    print(f"PDF field accuracy: {report['fields_passed']}/{report['fields_total']} = {report['accuracy'] * 100:.1f}%")
