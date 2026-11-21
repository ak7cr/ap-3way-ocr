"""
Evaluation harness for the AP sample set (tests/ap_samples).

The sample kit ships four artefacts per scenario:
  pdf/     clean digital documents
  scanned/ degraded images (needs an OCR engine)
  truth/   ground-truth JSON
  manifest.json  which documents belong together + the expected match result

This module implements three independent evaluations:
  * truth mode  — feed ground-truth JSON through the real service layer (no OCR)
  * pdf mode    — run the digital-PDF extractor and score fields against truth
  * scanned mode— run OCR and score fields against truth (skips without an engine)
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.services import matching_service as svc
from backend.services.canonical import canonical_invoice_from_dict
from backend.services.matching_engine import STATUS_EXCEPTION, STATUS_MATCHED, STATUS_REJECT

SAMPLES_DIR = Path(__file__).resolve().parent


def load_manifest() -> List[Dict[str, Any]]:
    return json.loads((SAMPLES_DIR / "manifest.json").read_text())


def load_truth(name: str) -> Dict[str, Any]:
    return json.loads((SAMPLES_DIR / "truth" / f"{name}.json").read_text())


def scenario_docs(entry: Dict[str, Any]) -> Dict[str, str]:
    """Maps a manifest entry to truth-file stems (po_S1, grn_S1, inv_S1)."""
    files = entry["files"]
    return {
        "po": Path(files["po"]).stem,
        "grn": Path(files["grn"]).stem,
        "inv": Path(files["inv"]).stem,
        "inv_duplicate": Path(files["inv_duplicate"]).stem if files.get("inv_duplicate") else None,
    }


# ---------------------------------------------------------------------------
# Expectations, transcribed from manifest.json
# ---------------------------------------------------------------------------
@dataclass
class ScenarioExpectation:
    status: str
    required_codes: List[str] = field(default_factory=list)
    line_codes: Dict[int, List[str]] = field(default_factory=dict)
    forbidden_line_codes: Dict[int, List[str]] = field(default_factory=dict)
    duplicate_expected: bool = False


EXPECTATIONS: Dict[str, ScenarioExpectation] = {
    # S1: clean match, intra-state CGST+SGST.
    "S1": ScenarioExpectation(status=STATUS_MATCHED),
    # S2: rate +4.4% on line 1 (partial delivery is valid), invoice qty 600 vs accepted 540 on line 3.
    "S2": ScenarioExpectation(
        status=STATUS_EXCEPTION,
        required_codes=["RATE_TOLERANCE_EXCEEDED", "QTY_EXCEEDS_GRN"],
        line_codes={1: ["RATE_TOLERANCE_EXCEEDED"], 3: ["QTY_EXCEEDS_GRN"]},
        forbidden_line_codes={1: ["QTY_EXCEEDS_GRN", "QTY_EXCEEDS_PO"]},
    ),
    # S3: invoice qty 10 vs accepted 8 on line 3; the reprinted invoice must be a duplicate.
    "S3": ScenarioExpectation(
        status=STATUS_EXCEPTION,
        required_codes=["QTY_EXCEEDS_GRN"],
        line_codes={3: ["QTY_EXCEEDS_GRN"]},
        duplicate_expected=True,
    ),
}


@dataclass
class ScenarioResult:
    scenario: str
    status: str
    reason_codes: List[str]
    line_codes: Dict[int, List[str]]
    duplicate_status: Optional[str] = None
    duplicate_reason_codes: List[str] = field(default_factory=list)
    invoice_number: Optional[str] = None
    grand_total: Optional[str] = None
    failures: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _line_codes(finding: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    for item in finding:
        out[int(item["invoice_line_no"])] = list(item.get("reason_codes") or [])
    return out


def run_truth_scenario(db, entry: Dict[str, Any]) -> ScenarioResult:
    """
    Imports the scenario's PO + GRN from ground truth, creates the invoice from
    ground truth and runs the deterministic engine. No OCR involved.
    """
    docs = scenario_docs(entry)
    scenario = entry["scenario"]

    po_truth = load_truth(docs["po"])
    grn_truth = load_truth(docs["grn"])
    inv_truth = load_truth(docs["inv"])

    svc.import_purchase_order(db, po_truth, source="AP_SAMPLE")
    svc.import_grn(db, grn_truth, source="AP_SAMPLE")

    invoice = canonical_invoice_from_dict(inv_truth, source="AP_SAMPLE")
    doc = svc.create_invoice_document(db, invoice, original_filename=f"{docs['inv']}.pdf")
    result = svc.run_match_for_invoice(db, doc.id)

    out = ScenarioResult(
        scenario=scenario,
        status=result.status,
        reason_codes=list(result.reason_codes or []),
        line_codes=_line_codes(result.line_findings or []),
        invoice_number=doc.invoice_number,
        grand_total=str(doc.grand_total),
    )

    if docs.get("inv_duplicate"):
        dup_truth = load_truth(docs["inv_duplicate"])
        dup_doc = svc.create_invoice_document(
            db, canonical_invoice_from_dict(dup_truth, source="AP_SAMPLE"), original_filename=f"{docs['inv_duplicate']}.pdf"
        )
        dup_result = svc.run_match_for_invoice(db, dup_doc.id)
        out.duplicate_status = dup_result.status
        out.duplicate_reason_codes = list(dup_result.reason_codes or [])

    out.failures = check_expectations(out, EXPECTATIONS[scenario])
    return out


def check_expectations(result: ScenarioResult, expected: ScenarioExpectation) -> List[str]:
    failures: List[str] = []
    if result.status != expected.status:
        failures.append(f"status: expected {expected.status}, got {result.status} ({', '.join(result.reason_codes) or 'no codes'})")

    for code in expected.required_codes:
        if code not in result.reason_codes:
            failures.append(f"missing top-level reason {code}")

    for line_no, codes in expected.line_codes.items():
        actual = result.line_codes.get(line_no, [])
        for code in codes:
            if code not in actual:
                failures.append(f"line {line_no}: missing {code} (got {actual})")

    for line_no, codes in expected.forbidden_line_codes.items():
        actual = result.line_codes.get(line_no, [])
        for code in codes:
            if code in actual:
                failures.append(f"line {line_no}: unexpected {code}")

    if expected.duplicate_expected:
        if result.duplicate_status != STATUS_REJECT:
            failures.append(f"duplicate: expected {STATUS_REJECT}, got {result.duplicate_status}")
        if "DUPLICATE_INVOICE" not in result.duplicate_reason_codes:
            failures.append("duplicate: missing DUPLICATE_INVOICE")
    return failures


def run_all_truth_scenarios(db) -> List[ScenarioResult]:
    results: List[ScenarioResult] = []
    for entry in load_manifest():
        results.append(run_truth_scenario(db, entry))
    return results


# ---------------------------------------------------------------------------
# Field accuracy (PDF / scanned modes)
# ---------------------------------------------------------------------------
def _norm_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _num(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def score_extraction(extracted, truth: Dict[str, Any]) -> Tuple[Dict[str, bool], List[str]]:
    """
    Compares an extracted InvoiceData against ground truth.
    Returns per-field pass/fail and a list of human-readable mismatches.
    """
    checks: Dict[str, bool] = {}
    notes: List[str] = []

    checks["invoice_number"] = _norm_text(extracted.metadata.invoice_number) == _norm_text(truth.get("number"))
    if not checks["invoice_number"]:
        notes.append(f"invoice_number: {extracted.metadata.invoice_number!r} != {truth.get('number')!r}")

    truth_vendor = (truth.get("vendor") or {}).get("name")
    checks["vendor_name"] = _norm_text(extracted.seller.name) == _norm_text(truth_vendor)
    if not checks["vendor_name"]:
        notes.append(f"vendor_name: {extracted.seller.name!r} != {truth_vendor!r}")

    truth_gstin = _norm_text((truth.get("vendor") or {}).get("gstin"))
    checks["vendor_gstin"] = bool(truth_gstin) and _norm_text(extracted.seller.gstin) == truth_gstin
    if not checks["vendor_gstin"]:
        notes.append(f"vendor_gstin: {extracted.seller.gstin!r} != {truth.get('vendor', {}).get('gstin')!r}")

    truth_subtotal = _num(truth.get("subtotal"))
    checks["subtotal"] = abs(_num(extracted.summary.subtotal) - truth_subtotal) <= Decimal("1.00")
    if not checks["subtotal"]:
        notes.append(f"subtotal: {extracted.summary.subtotal} != {truth_subtotal}")

    truth_tax = sum((_num(c.get("amount")) for c in truth.get("tax_breakup") or []), Decimal("0"))
    checks["tax"] = abs(_num(extracted.summary.total_gst) - truth_tax) <= Decimal("2.00")
    if not checks["tax"]:
        notes.append(f"tax: {extracted.summary.total_gst} != {truth_tax}")

    truth_total = _num(truth.get("total"))
    checks["total"] = abs(_num(extracted.summary.grand_total) - truth_total) <= Decimal("2.00")
    if not checks["total"]:
        notes.append(f"total: {extracted.summary.grand_total} != {truth_total}")

    truth_lines = truth.get("lines") or []
    checks["line_count"] = len(extracted.line_items) == len(truth_lines)
    if not checks["line_count"]:
        notes.append(f"line_count: {len(extracted.line_items)} != {len(truth_lines)}")

    qty_ok = rate_ok = amount_ok = 0
    for extracted_line, truth_line in zip(extracted.line_items, truth_lines):
        if abs(_num(extracted_line.quantity) - _num(truth_line.get("qty"))) <= Decimal("0.01"):
            qty_ok += 1
        if abs(_num(extracted_line.unit_price) - _num(truth_line.get("rate"))) <= Decimal("0.01"):
            rate_ok += 1
        if abs(_num(extracted_line.total_amount) - _num(truth_line.get("taxable_value"))) <= Decimal("1.00"):
            amount_ok += 1

    total_truth_lines = max(1, len(truth_lines))
    checks["line_qty_accuracy"] = qty_ok / total_truth_lines >= 1.0
    checks["line_rate_accuracy"] = rate_ok / total_truth_lines >= 1.0
    checks["line_amount_accuracy"] = amount_ok / total_truth_lines >= 1.0
    notes.append(f"line accuracy: qty {qty_ok}/{len(truth_lines)}, rate {rate_ok}/{len(truth_lines)}, amount {amount_ok}/{len(truth_lines)}")
    return checks, notes


def evaluate_pdf_invoices() -> Dict[str, Any]:
    """
    Runs the digital-PDF extractor over pdf/inv_*.pdf and scores against truth.
    Returns a summary dict; raises ImportError when PDF dependencies are missing.
    """
    from backend.services.invoice_extractor import extract_invoice_local

    rows = []
    for pdf_path in sorted((SAMPLES_DIR / "pdf").glob("inv_*.pdf")):
        truth = load_truth(pdf_path.stem)
        extracted = extract_invoice_local(pdf_path.read_bytes(), filename=pdf_path.name)
        checks, notes = score_extraction(extracted, truth)
        rows.append({"file": pdf_path.name, "checks": checks, "notes": notes})

    all_fields = [key for row in rows for key in row["checks"]]
    passed = sum(1 for row in rows for ok in row["checks"].values() if ok)
    total = sum(len(row["checks"]) for row in rows)
    return {"rows": rows, "fields_passed": passed, "fields_total": total, "accuracy": passed / total if total else 0.0}


def evaluate_scanned_invoices(use_router: bool = True) -> Dict[str, Any]:
    """
    Runs OCR over scanned/inv_*.jpg and scores against truth.

    With `use_router` (the default) this goes through the same scanned-image
    router the API uses — external engines first, this repo's engine as the
    fallback — so the number measures deployed behaviour rather than a code path
    that is never exercised in production.
    """
    from backend.services.invoice_extractor import extract_invoice_from_image

    if not _ocr_available() and not (use_router and _external_available()):
        raise ImportError("No OCR engine available (easyocr/paddleocr or an external sidecar). Skip scanned evaluation.")

    rows = []
    engines: Dict[str, int] = {}
    for image_path in sorted((SAMPLES_DIR / "scanned").glob("inv_*_scan.jpg")):
        truth = load_truth(image_path.stem.replace("_scan", ""))
        if use_router:
            from backend.services.ocr_router import extract_image_document

            routed = extract_image_document(image_path.read_bytes(), image_path.name, "image/jpeg")
            extracted = routed.invoice
            engine = routed.engine
        else:
            extracted = extract_invoice_from_image(image_path.read_bytes(), preview_url="")
            engine = extracted.engine_used if extracted else "none"

        engines[engine] = engines.get(engine, 0) + 1
        if extracted is None:
            rows.append({"file": image_path.name, "checks": {}, "notes": ["no extraction"], "engine": engine})
            continue
        checks, notes = score_extraction(extracted, truth)
        rows.append({"file": image_path.name, "checks": checks, "notes": notes, "engine": engine})

    passed = sum(1 for row in rows for ok in row["checks"].values() if ok)
    total = sum(len(row["checks"]) for row in rows)
    return {
        "rows": rows,
        "fields_passed": passed,
        "fields_total": total,
        "accuracy": passed / total if total else 0.0,
        "engines": engines,
    }


def _external_available() -> bool:
    try:
        from backend.services.external_ocr import health

        return any(entry.get("available") for entry in health())
    except Exception:
        return False


def _ocr_available() -> bool:
    try:
        from backend.services.ocr_engine import get_easyocr_reader

        return get_easyocr_reader() is not None
    except Exception:
        return False
