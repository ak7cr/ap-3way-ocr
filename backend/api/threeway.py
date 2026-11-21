"""
HTTP surface for the 3-way match: imports, matching, exception queue, approvals,
payment export and metrics.

Boundary rules enforced here:
  * an exception must be resolved before it can be approved;
  * the approver cannot be the person who overrode it (segregation of duties);
  * payment export only contains invoices that passed approval.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..database import (
    ExtractionLog,
    GoodsReceipt,
    InvoiceDocument,
    InvoiceTemplate,
    MatchResult,
    POLineBalance,
    PurchaseOrder,
    Vendor,
    get_db,
)
from ..schemas import ApprovalRequest, MatchActionRequest, VendorCreate
from ..services import matching_service as svc
from ..services.canonical import CanonicalInvoice, validate_canonical_invoice
from ..services.llm_assist import draft_vendor_email, explain_exception, suggest_line_mappings
from ..services.matching_engine import STATUS_EXCEPTION, STATUS_REJECT
from ..services import grn_intake
from ..services.decision import APPROVE, REJECT, REVIEW
from ..services.shadow import agreement_report, vendor_agreement
from ..services.storage import public_url
from ..services.template_engine import confirm_proposed_template, confirm_template, reject_candidate_template, template_trust_view

router = APIRouter(prefix="/api", tags=["3-way-match"])


def _http_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)


# ---------------------------------------------------------------------------
# Mixed-document ingest (single upload box for PO / GRN / invoice)
# ---------------------------------------------------------------------------
@router.post("/documents/ingest")
async def ingest_documents(
    files: List[UploadFile] = File(...),
    extraction_method: str = Form("auto"),
    api_key: Optional[str] = Form(None),
    po_number: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Runs OCR (+AI fallback) on every file, recognises PO / GRN / invoice, then
    imports the PO/GRN ledger entries and matches the invoices.
    """
    from ..services.ingest_service import ingest_upload, summarise

    results = []
    for upload in files:
        results.append(await ingest_upload(db, upload, extraction_method, api_key, po_number))
    return {"documents": results, "summary": summarise(results)}


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------
@router.get("/vendors")
def list_vendors(db: Session = Depends(get_db)):
    vendors = db.query(Vendor).order_by(Vendor.name.asc()).all()
    return [
        {
            "id": v.id,
            "code": v.code,
            "name": v.name,
            "gstin": v.gstin,
            "risk_tier": v.risk_tier,
            "category": v.category,
            "payment_terms": v.payment_terms,
            "tolerance_price_pct": v.tolerance_price_pct,
            "tolerance_qty": v.tolerance_qty,
            "is_active": v.is_active,
        }
        for v in vendors
    ]


@router.post("/vendors")
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db)):
    try:
        vendor = svc.upsert_vendor(db, payload.model_dump())
    except ValueError as exc:
        raise _http_error(exc)
    return {"id": vendor.id, "code": vendor.code, "name": vendor.name, "gstin": vendor.gstin}


@router.post("/vendors/import")
def import_vendors(payload: List[Dict[str, Any]] = Body(...), db: Session = Depends(get_db)):
    results = []
    imported = 0
    for item in payload:
        try:
            vendor = svc.upsert_vendor(db, item)
            results.append({"id": vendor.id, "code": vendor.code, "name": vendor.name})
            imported += 1
        except ValueError as exc:
            results.append({"error": str(exc), "payload": item})
    return {"imported": imported, "results": results}


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------
@router.get("/purchase-orders")
def list_purchase_orders(db: Session = Depends(get_db)):
    pos = db.query(PurchaseOrder).order_by(PurchaseOrder.po_number.asc()).all()
    out = []
    for po in pos:
        lines = []
        for line in po.lines:
            balance = db.query(POLineBalance).filter(POLineBalance.po_line_id == line.id).first()
            lines.append(
                {
                    "line_no": line.line_no,
                    "item_code": line.item_code,
                    "description": line.description,
                    "unit": line.unit,
                    "ordered_qty": str(line.ordered_qty),
                    "rate": str(line.rate),
                    "tax_rate": str(line.tax_rate),
                    "received_qty": str(balance.received_qty) if balance else "0",
                    "invoiced_qty": str(balance.invoiced_qty) if balance else "0",
                    "credited_qty": str(balance.credited_qty) if balance else "0",
                }
            )
        out.append(
            {
                "id": po.id,
                "po_number": po.po_number,
                "vendor": po.vendor.name if po.vendor else None,
                "status": po.status,
                "currency": po.currency,
                "po_date": po.po_date.isoformat() if po.po_date else None,
                "grand_total": str(po.grand_total),
                "source": po.source,
                "lines": lines,
            }
        )
    return out


@router.post("/purchase-orders")
def create_purchase_order(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    try:
        po = svc.import_purchase_order(db, payload, source=payload.get("source") or "API")
    except ValueError as exc:
        raise _http_error(exc)
    return {"id": po.id, "po_number": po.po_number, "lines": len(po.lines)}


@router.post("/purchase-orders/import")
async def import_purchase_orders(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Imports POs from a CSV or a JSON file (array of PO objects)."""
    try:
        text = (await file.read()).decode("utf-8-sig")
        if file.filename and file.filename.lower().endswith(".csv"):
            pos = svc.import_po_csv(db, text)
        else:
            import json

            pos = [svc.import_purchase_order(db, item, source="IMPORT") for item in json.loads(text)]
    except ValueError as exc:
        raise _http_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Import failed: {exc}")
    return {"imported": len(pos), "po_numbers": [p.po_number for p in pos]}


@router.post("/purchase-orders/bulk")
def import_purchase_orders_bulk(payload: List[Dict[str, Any]] = Body(...), db: Session = Depends(get_db)):
    """Imports POs from a JSON array in the request body (ERP push)."""
    try:
        pos = [svc.import_purchase_order(db, item, source="IMPORT") for item in payload]
    except ValueError as exc:
        raise _http_error(exc)
    return {"imported": len(pos), "po_numbers": [p.po_number for p in pos]}


# ---------------------------------------------------------------------------
# GRNs
# ---------------------------------------------------------------------------
@router.get("/grns")
def list_grns(db: Session = Depends(get_db)):
    grns = db.query(GoodsReceipt).order_by(GoodsReceipt.created_at.desc()).all()
    return [
        {
            "id": g.id,
            "grn_number": g.grn_number,
            "po_number": g.purchase_order.po_number if g.purchase_order else None,
            "receipt_date": g.receipt_date.isoformat() if g.receipt_date else None,
            "lines": [
                {
                    "line_no": line.line_no,
                    "item_code": line.item_code,
                    "description": line.description,
                    "received_qty": str(line.received_qty),
                }
                for line in g.lines
            ],
        }
        for g in grns
    ]


@router.post("/grns")
def create_grn(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    try:
        grn = svc.import_grn(db, payload, source=payload.get("source") or "API")
    except ValueError as exc:
        raise _http_error(exc)
    return {"id": grn.id, "grn_number": grn.grn_number, "lines": len(grn.lines)}


@router.post("/grns/import")
async def import_grns(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        text = (await file.read()).decode("utf-8-sig")
        grns = svc.import_grn_csv(db, text)
    except ValueError as exc:
        raise _http_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"GRN import failed: {exc}")
    return {"imported": len(grns), "grn_numbers": [g.grn_number for g in grns]}


@router.get("/grns/entry/{po_number:path}")
def grn_entry_rows(po_number: str, db: Session = Depends(get_db)):
    """
    The rows a store person sees when recording a receipt by hand.

    This is the recommended path for customers without an ERP: entering received
    quantities against a PO they already have beats scanning a handwritten GRN.
    """
    try:
        return {
            "po_number": po_number,
            "lines": grn_intake.po_lines_for_entry(db, po_number),
        }
    except grn_intake.GrnIntakeError as exc:
        raise _http_error(exc)


@router.post("/grns/manual")
def create_grn_manual(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    Records a goods receipt entered by hand (or from a mobile form):
    {po_number, receipt_date?, grn_number?, lines:[{po_line_no|item_code, received_qty}]}
    """
    try:
        canonical = grn_intake.build_manual_grn_payload(
            db,
            po_number=payload.get("po_number"),
            rows=payload.get("lines") or payload.get("rows") or [],
            grn_number=payload.get("grn_number"),
            receipt_date=payload.get("receipt_date"),
            note=payload.get("note"),
        )
        grn = svc.import_grn(db, canonical, source="MANUAL")
    except grn_intake.GrnIntakeError as exc:
        raise _http_error(exc)
    except ValueError as exc:
        raise _http_error(exc)
    return {
        "id": grn.id,
        "grn_number": grn.grn_number,
        "po_number": canonical.get("po_number"),
        "lines": [
            {"line_no": line.line_no, "item_code": line.item_code, "received_qty": str(line.received_qty)}
            for line in grn.lines
        ],
    }


@router.post("/grns/manual/import")
async def import_grn_sheet(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Bulk receipt entry from a spreadsheet (CSV/TSV, or XLSX when openpyxl is
    installed). One row per PO line; grouped into one GRN per GRN number.
    """
    content = await file.read()
    try:
        text = grn_intake.table_to_csv_text(file.filename or "receipt.csv", content)
        groups = grn_intake.group_receipt_rows(text)
        imported = []
        for group in groups:
            canonical = grn_intake.build_manual_grn_payload(
                db,
                po_number=group["po_number"],
                rows=group["rows"],
                grn_number=group.get("grn_number"),
                receipt_date=group.get("receipt_date"),
                note=group.get("note"),
            )
            grn = svc.import_grn(db, canonical, source="SHEET")
            imported.append({"grn_number": grn.grn_number, "po_number": canonical["po_number"], "lines": len(grn.lines)})
    except grn_intake.GrnIntakeError as exc:
        raise _http_error(exc)
    except ValueError as exc:
        raise _http_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Receipt import failed: {exc}")
    return {"imported": len(imported), "receipts": imported}


# ---------------------------------------------------------------------------
# Invoice canonicalization + matching
# ---------------------------------------------------------------------------
@router.post("/extractions/{extraction_id}/canonicalize")
def canonicalize(extraction_id: int, po_number: Optional[str] = Query(None), db: Session = Depends(get_db)):
    log = db.query(ExtractionLog).filter(ExtractionLog.id == extraction_id).first()
    if log is None:
        raise HTTPException(status_code=404, detail=f"Extraction #{extraction_id} not found")
    try:
        doc = svc.canonicalize_extraction(db, log, po_number=po_number, source_file_url=log.source_file_url)
    except ValueError as exc:
        raise _http_error(exc)
    return {
        "invoice_document_id": doc.id,
        "invoice_number": doc.invoice_number,
        "po_number": doc.po_number,
        "validation_errors": doc.validation_errors,
        "status": doc.status,
    }


@router.post("/extractions/{extraction_id}/match")
def match_extraction(
    extraction_id: int,
    po_number: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    log = db.query(ExtractionLog).filter(ExtractionLog.id == extraction_id).first()
    if log is None:
        raise HTTPException(status_code=404, detail=f"Extraction #{extraction_id} not found")
    try:
        doc = svc.canonicalize_extraction(db, log, po_number=po_number, source_file_url=log.source_file_url)
        result = svc.run_match_for_invoice(db, doc.id)
    except ValueError as exc:
        raise _http_error(exc)
    return _match_response(db, result)


@router.post("/match")
def match_inline(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    Runs a match for an invoice supplied as canonical JSON. Useful for ERP pushes
    and for replaying hand-built scenarios without OCR.
    """
    try:
        invoice_payload = payload.get("invoice", payload)
        canonical = CanonicalInvoice(**invoice_payload)
        errors = validate_canonical_invoice(canonical)
        doc = svc.create_invoice_document(
            db,
            canonical,
            original_filename=payload.get("original_filename"),
            source_file_url=payload.get("source_file_url"),
        )
        result = svc.run_match_for_invoice(db, doc.id)
    except ValueError as exc:
        raise _http_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Match failed: {exc}")
    response = _match_response(db, result)
    response["contract_errors"] = errors
    return response


def _match_response(db: Session, result: MatchResult) -> Dict[str, Any]:
    detail = svc.build_side_by_side(db, result)
    detail["match_duration_ms"] = result.match_duration_ms
    return detail


@router.get("/matches")
def list_matches(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(MatchResult).order_by(MatchResult.created_at.desc())
    if status:
        query = query.filter(MatchResult.status == status.upper())
    results = query.limit(limit).all()
    return [
        {
            "id": r.id,
            "invoice_document_id": r.invoice_document_id,
            "invoice_number": r.invoice_document.invoice_number if r.invoice_document else None,
            "vendor": (r.invoice_document.canonical_data or {}).get("vendor_name") if r.invoice_document else None,
            "grand_total": str(r.invoice_document.grand_total) if r.invoice_document else None,
            "currency": r.invoice_document.currency if r.invoice_document else None,
            "status": r.status,
            "reason_codes": r.reason_codes or [],
            "resolution": r.resolution,
            "approval_status": r.approval_status,
            "approvals_required": r.approvals_required,
            "approvals_received": r.approvals_received,
            "matched_at": r.matched_at.isoformat() if r.matched_at else None,
        }
        for r in results
    ]


@router.get("/matches/{match_result_id}")
def get_match(match_result_id: int, db: Session = Depends(get_db)):
    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    return _match_response(db, result)


@router.get("/exceptions")
def exception_queue(
    reason: Optional[str] = Query(None),
    include_resolved: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(MatchResult).filter(MatchResult.status.in_([STATUS_EXCEPTION, STATUS_REJECT]))
    if not include_resolved:
        query = query.filter(MatchResult.resolution.is_(None))
    results = query.order_by(MatchResult.created_at.desc()).all()

    queue = []
    for result in results:
        codes = result.reason_codes or []
        if reason and reason not in codes:
            continue
        doc = result.invoice_document
        canonical = (doc.canonical_data or {}) if doc else {}
        queue.append(
            {
                "id": result.id,
                "invoice_number": doc.invoice_number if doc else None,
                "vendor": canonical.get("vendor_name"),
                "invoice_date": doc.invoice_date.strftime("%Y-%m-%d") if doc and doc.invoice_date else None,
                "grand_total": str(doc.grand_total) if doc else None,
                "currency": doc.currency if doc else None,
                "status": result.status,
                "reason_codes": codes,
                "primary_reason": codes[0] if codes else None,
                "resolution": result.resolution,
                "approval_status": result.approval_status,
                "po_number": canonical.get("po_number"),
                "source_file_url": public_url(doc.source_file_url) if doc and doc.source_file_url else None,
                "created_at": result.created_at.isoformat() if result.created_at else None,
            }
        )
    return queue


@router.post("/matches/{match_result_id}/actions")
def act_on_exception(match_result_id: int, payload: MatchActionRequest, db: Session = Depends(get_db)):
    try:
        result = svc.apply_exception_action(
            db,
            match_result_id,
            payload.action,
            payload.reason,
            payload.actor or "accountant",
        )
    except ValueError as exc:
        raise _http_error(exc)
    return _match_response(db, result)


@router.get("/matches/{match_result_id}/line-suggestions")
def line_suggestions(match_result_id: int, db: Session = Depends(get_db)):
    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    doc = result.invoice_document
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first() if result.po_id else None
    if not doc or not po:
        return {"suggestions": []}

    def line_view(line, qty_attr, amount_attr):
        return type(
            "Line",
            (),
            {
                "line_no": line.line_no,
                "item_code": line.item_code,
                "description": line.description,
                "quantity": float(getattr(line, qty_attr) or 0),
                "rate": float(line.rate or 0),
                "tax_rate": float(line.tax_rate or 0),
                "amount": float(getattr(line, amount_attr) or 0),
            },
        )()

    invoice_lines = [line_view(l, "quantity", "amount") for l in doc.lines]
    po_lines = [line_view(l, "ordered_qty", "line_amount") for l in po.lines]
    return {"suggestions": suggest_line_mappings(invoice_lines, po_lines)}


@router.post("/matches/{match_result_id}/explain")
def explain(match_result_id: int, api_key: Optional[str] = Query(None), db: Session = Depends(get_db)):
    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    payload = svc.build_side_by_side(db, result)
    return explain_exception(payload, api_key)


@router.post("/matches/{match_result_id}/draft-email")
def draft_email(match_result_id: int, api_key: Optional[str] = Query(None), db: Session = Depends(get_db)):
    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    payload = svc.build_side_by_side(db, result)
    return draft_vendor_email(payload, api_key)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
@router.get("/approvals")
def pending_approvals(db: Session = Depends(get_db)):
    results = (
        db.query(MatchResult)
        .filter(MatchResult.approval_status == "PENDING")
        .order_by(MatchResult.created_at.asc())
        .all()
    )
    out = []
    for result in results:
        doc = result.invoice_document
        canonical = (doc.canonical_data or {}) if doc else {}
        out.append(
            {
                "id": result.id,
                "invoice_number": doc.invoice_number if doc else None,
                "vendor": canonical.get("vendor_name"),
                "grand_total": str(doc.grand_total) if doc else None,
                "currency": doc.currency if doc else None,
                "match_status": result.status,
                "resolution": result.resolution,
                "resolved_by": result.resolved_by,
                "reason_codes": result.reason_codes or [],
                "required_roles": result.required_roles or [],
                "approvals_required": result.approvals_required,
                "approvals_received": result.approvals_received,
                "approvers": [a.approver for a in result.approvals],
                "matched_at": result.matched_at.isoformat() if result.matched_at else None,
            }
        )
    return out


@router.post("/matches/{match_result_id}/approve")
def approve(match_result_id: int, payload: ApprovalRequest, db: Session = Depends(get_db)):
    try:
        result = svc.submit_approval(
            db,
            match_result_id,
            payload.approver,
            payload.role,
            payload.decision,
            payload.note,
        )
    except ValueError as exc:
        raise _http_error(exc)
    return _match_response(db, result)


# ---------------------------------------------------------------------------
# Payment export + metrics
# ---------------------------------------------------------------------------
@router.get("/payments/export.csv", response_class=PlainTextResponse)
def export_payments_csv(db: Session = Depends(get_db)):
    return PlainTextResponse(
        svc.payment_csv(db),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_ready.csv"},
    )


@router.get("/payments/tally.xml", response_class=PlainTextResponse)
def export_payments_tally(db: Session = Depends(get_db)):
    return PlainTextResponse(
        svc.payment_tally_xml(db),
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=payment_ready_tally.xml"},
    )


@router.post("/payments/mark-paid")
def mark_paid(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    ids = payload.get("match_result_ids") or []
    updated = 0
    for match_id in ids:
        result = db.query(MatchResult).filter(MatchResult.id == int(match_id)).first()
        if result and result.invoice_document and result.approval_status == "APPROVED":
            result.invoice_document.status = "PAID"
            updated += 1
    db.commit()
    return {"marked_paid": updated}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    return svc.compute_metrics(db)


# ---------------------------------------------------------------------------
# Review queue (approve / review / reject with the recommendation shown)
# ---------------------------------------------------------------------------
@router.get("/review/queue")
def review_queue(
    decision: Optional[str] = Query(None, description="APPROVE, REVIEW or REJECT"),
    include_approved: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    Everything the gate, the match engine or shadow mode sent to a human.

    The engine's recommendation travels with each item so the reviewer sees what
    the system would have done — that is the point of running in shadow mode.
    """
    query = db.query(MatchResult)
    if decision:
        query = query.filter(MatchResult.decision == decision.upper())
    elif not include_approved:
        query = query.filter(MatchResult.decision.in_([REVIEW, REJECT]))
    results = query.order_by(MatchResult.created_at.desc()).limit(limit).all()

    queue = []
    for result in results:
        doc = result.invoice_document
        canonical = (doc.canonical_data or {}) if doc else {}
        queue.append(
            {
                "id": result.id,
                "invoice_number": doc.invoice_number if doc else None,
                "vendor": canonical.get("vendor_name"),
                "vendor_id": doc.vendor_id if doc else None,
                "grand_total": str(doc.grand_total) if doc and doc.grand_total is not None else None,
                "currency": doc.currency if doc else None,
                "status": result.status,
                "decision": result.decision,
                "decision_reasons": result.decision_reasons or [],
                "decision_explanation": result.decision_explanation,
                "recommendation": result.decision,
                "reason_codes": result.reason_codes or [],
                "validation_passed": result.validation_passed,
                "approval_status": result.approval_status,
                "resolution": result.resolution,
                "po_number": canonical.get("po_number"),
                "source_file_url": public_url(doc.source_file_url) if doc and doc.source_file_url else None,
                "created_at": result.created_at.isoformat() if result.created_at else None,
            }
        )
    return queue


@router.get("/review/queue/{match_result_id}/validation")
def review_validation(match_result_id: int, db: Session = Depends(get_db)):
    """The full check list for one document, so the reviewer sees why it stopped."""
    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    doc = result.invoice_document
    return {
        "match_result_id": result.id,
        "document_id": doc.id if doc else None,
        "decision": result.decision,
        "decision_reasons": result.decision_reasons or [],
        "decision_explanation": result.decision_explanation,
        "validation": (doc.validation_report if doc else None) or result.validation_report or {},
    }


@router.post("/review/queue/{match_result_id}/decision")
def review_decision(match_result_id: int, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    A human confirms or overturns the engine's recommendation.

    APPROVE goes through the normal approval chain (segregation of duties still
    applies); REVIEW parks it; REJECT closes it. Every decision is written to
    the shadow ledger so agreement can be measured per vendor.
    """
    from ..services.shadow import record_human_decision

    decision = (payload.get("decision") or "").upper()
    if decision not in {APPROVE, REVIEW, REJECT}:
        raise HTTPException(status_code=400, detail="decision must be APPROVE, REVIEW or REJECT")
    actor = (payload.get("actor") or "").strip()
    if not actor:
        raise HTTPException(status_code=400, detail="actor is required")
    note = payload.get("note")

    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail=f"Match result #{match_result_id} not found")
    doc = result.invoice_document

    try:
        if decision == APPROVE:
            if result.status == STATUS_EXCEPTION and result.resolution not in {"OVERRIDDEN", "CREDIT_NOTE"}:
                raise ValueError("Resolve the exception (override or credit note) before approving")
            if svc.SEGREGATION_OF_DUTIES and result.resolved_by and result.resolved_by.lower() == actor.lower():
                raise ValueError("Segregation of duties: the person who overrode the exception cannot approve it")
            result.approval_status = "APPROVED"
            result.approved_at = datetime.utcnow()
            if doc:
                doc.status = "APPROVED"
        elif decision == REJECT:
            result.approval_status = "REJECTED"
            result.resolution = result.resolution or "REJECTED"
            result.resolved_by = actor
            result.resolved_at = datetime.utcnow()
            if doc:
                doc.status = "REJECT"
        else:
            result.approval_status = "PENDING"

        if result.decision_observation_id:
            record_human_decision(
                db,
                observation_id=result.decision_observation_id,
                human_decision=decision,
                actor=actor,
                note=note,
            )
        result.human_decision = decision
        result.human_decision_by = actor
        result.human_decision_at = datetime.utcnow()
        db.commit()
    except ValueError as exc:
        raise _http_error(exc)

    svc._audit(
        db,
        "match_result",
        result.id,
        f"REVIEW_{decision}",
        actor,
        None,
        {"note": note, "recommendation": result.decision},
    )
    db.commit()
    db.refresh(result)
    return {
        "id": result.id,
        "decision": result.decision,
        "human_decision": decision,
        "approval_status": result.approval_status,
        "resolution": result.resolution,
    }


# ---------------------------------------------------------------------------
# Template trust
# ---------------------------------------------------------------------------
@router.get("/templates/candidates")
def template_candidates(db: Session = Depends(get_db)):
    """Candidate layouts and AI proposals waiting for a human to confirm them."""
    templates = (
        db.query(InvoiceTemplate)
        .filter(InvoiceTemplate.status.in_(["CANDIDATE", "DRAFT", "QUARANTINED"]))
        .order_by(InvoiceTemplate.updated_at.desc())
        .limit(100)
        .all()
    )
    proposals = (
        db.query(ExtractionLog)
        .filter(ExtractionLog.proposed_template_status == "AWAITING_CONFIRMATION")
        .filter(ExtractionLog.proposed_template.isnot(None))
        .order_by(ExtractionLog.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "templates": [template_trust_view(t) for t in templates],
        "proposals": [
            {
                "extraction_log_id": log.id,
                "invoice_number": log.invoice_number,
                "vendor": log.vendor_name,
                "created_at": log.created_at.isoformat() if log.created_at else None,
                "anchor_keywords": (log.proposed_template or {}).get("anchor_keywords") or [],
            }
            for log in proposals
        ],
    }


@router.post("/templates/{template_id}/confirm")
def confirm_template_endpoint(template_id: int, payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    try:
        template = confirm_template(db, template_id, actor=payload.get("actor"), note=payload.get("note"))
    except ValueError as exc:
        raise _http_error(exc)
    return template_trust_view(template)


@router.post("/templates/{template_id}/reject")
def reject_template_endpoint(template_id: int, payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    try:
        template = reject_candidate_template(db, template_id, actor=payload.get("actor"), note=payload.get("note"))
    except ValueError as exc:
        raise _http_error(exc)
    return template_trust_view(template)


@router.post("/templates/proposals/{extraction_log_id}/confirm")
def confirm_proposal_endpoint(extraction_log_id: int, payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    accept = payload.get("accept", True)
    try:
        template = confirm_proposed_template(db, extraction_log_id, actor=payload.get("actor"), accept=bool(accept))
    except ValueError as exc:
        raise _http_error(exc)
    if template is None:
        return {"accepted": False, "extraction_log_id": extraction_log_id}
    return {"accepted": True, "template": template_trust_view(template)}


# ---------------------------------------------------------------------------
# Shadow mode + job queue
# ---------------------------------------------------------------------------
@router.get("/shadow/agreement")
def shadow_agreement(db: Session = Depends(get_db)):
    from ..config import DEFAULT_TENANT

    return agreement_report(db, DEFAULT_TENANT)


@router.get("/vendors/{vendor_id}/agreement")
def vendor_agreement_endpoint(vendor_id: int, db: Session = Depends(get_db)):
    from ..config import DEFAULT_TENANT

    return vendor_agreement(db, DEFAULT_TENANT, vendor_id)


@router.get("/jobs")
def list_jobs(status: Optional[str] = Query(None), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    from .. import jobs as job_queue
    from ..database import Job

    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status.upper())
    rows = query.order_by(Job.created_at.desc()).limit(limit).all()
    return {
        "stats": job_queue.queue_stats(db),
        "jobs": [
            {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "error": job.error,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in rows
        ],
    }


# ---------------------------------------------------------------------------
# Demo seed (pilot data for the UI)
# ---------------------------------------------------------------------------
@router.post("/threeway/seed-demo")
def seed_demo(db: Session = Depends(get_db)):
    """
    Creates a vendor, an open PO with a GRN, one perfectly matching invoice and one
    over-billed / wrong-rate invoice so the UI can be exercised end to end.
    """
    vendor = svc.upsert_vendor(
        db,
        {
            "code": "V-ACME",
            "name": "Acme Steel Pvt Ltd",
            "gstin": "27AABCU9603R1ZN",
            "risk_tier": "LOW",
            "category": "commodity",
        },
    )

    po = svc.import_purchase_order(
        db,
        {
            "po_number": "PO-2026-001",
            "vendor_name": vendor.name,
            "vendor_gstin": "27AABCU9603R1ZN",
            "po_date": "2026-11-01",
            "currency": "INR",
            "status": "OPEN",
            "category": "commodity",
            "lines": [
                {"line_no": 1, "item_code": "STL-100", "description": "MS Angle 50x50x6", "quantity": 100, "unit": "KG", "rate": 62.50, "tax_rate": 18},
                {"line_no": 2, "item_code": "STL-200", "description": "MS Plate 6mm", "quantity": 50, "unit": "KG", "rate": 71.00, "tax_rate": 18},
                {"line_no": 3, "item_code": "STL-300", "description": "GI Pipe 2 inch", "quantity": 40, "unit": "MTR", "rate": 155.00, "tax_rate": 18},
            ],
        },
        source="DEMO",
    )

    grn = svc.import_grn(
        db,
        {
            "grn_number": "GRN-2026-001",
            "po_number": "PO-2026-001",
            "receipt_date": "2026-11-05",
            "currency": "INR",
            "lines": [
                {"line_no": 1, "po_line_no": 1, "item_code": "STL-100", "received_quantity": 100},
                {"line_no": 2, "po_line_no": 2, "item_code": "STL-200", "received_quantity": 50},
                {"line_no": 3, "po_line_no": 3, "item_code": "STL-300", "received_quantity": 40},
            ],
        },
        source="DEMO",
    )

    subtotal = 100 * 62.50 + 50 * 71.00 + 40 * 155.00
    tax = round(subtotal * 0.18, 2)
    clean_invoice = {
        "vendor_name": "Acme Steel Pvt Ltd",
        "vendor_gstin": "27AABCU9603R1ZN",
        "invoice_number": "ACME/2026/0001",
        "invoice_date": "2026-11-08",
        "po_number": "PO-2026-001",
        "currency": "INR",
        "subtotal": subtotal,
        "total_tax": tax,
        "cgst_total": round(tax / 2, 2),
        "sgst_total": round(tax / 2, 2),
        "grand_total": round(subtotal + tax, 2),
        "lines": [
            {"line_no": 1, "item_code": "STL100", "description": "MS Angle 50x50x6", "quantity": 100, "unit": "kgs", "rate": 62.50, "tax_rate": 18, "amount": 6250.00},
            {"line_no": 2, "item_code": "STL200", "description": "MS Plate 6mm", "quantity": 50, "unit": "KG", "rate": 71.00, "tax_rate": 18, "amount": 3550.00},
            {"line_no": 3, "item_code": "STL300", "description": "GI Pipe 2 inch", "quantity": 40, "unit": "MTR", "rate": 155.00, "tax_rate": 18, "amount": 6200.00},
        ],
    }
    doc_clean = svc.create_invoice_document(db, CanonicalInvoice(**clean_invoice), original_filename="demo_clean.pdf")
    result_clean = svc.run_match_for_invoice(db, doc_clean.id)

    # Second PO so the exception example is independent of the clean one.
    svc.import_purchase_order(
        db,
        {
            "po_number": "PO-2026-002",
            "vendor_name": vendor.name,
            "vendor_gstin": "27AABCU9603R1ZN",
            "po_date": "2026-11-01",
            "currency": "INR",
            "status": "OPEN",
            "category": "commodity",
            "lines": [
                {"line_no": 1, "item_code": "STL-100", "description": "MS Angle 50x50x6", "quantity": 100, "unit": "KG", "rate": 62.50, "tax_rate": 18},
            ],
        },
        source="DEMO",
    )
    svc.import_grn(
        db,
        {
            "grn_number": "GRN-2026-002",
            "po_number": "PO-2026-002",
            "receipt_date": "2026-11-05",
            "lines": [{"line_no": 1, "po_line_no": 1, "item_code": "STL-100", "received_quantity": 100}],
        },
        source="DEMO",
    )

    bad_subtotal = 120 * 68.00
    bad_tax = round(bad_subtotal * 0.18, 2)
    bad_invoice = {
        "vendor_name": "Acme Steel Pvt Ltd",
        "vendor_gstin": "27AABCU9603R1ZN",
        "invoice_number": "ACME/2026/0002",
        "invoice_date": "2026-11-09",
        "po_number": "PO-2026-002",
        "currency": "INR",
        "subtotal": bad_subtotal,
        "total_tax": bad_tax,
        "cgst_total": round(bad_tax / 2, 2),
        "sgst_total": round(bad_tax / 2, 2),
        "grand_total": round(bad_subtotal + bad_tax, 2),
        "lines": [
            {"line_no": 1, "item_code": "STL-100", "description": "MS Angle 50x50x6", "quantity": 120, "unit": "KGS", "rate": 68.00, "tax_rate": 18, "amount": bad_subtotal},
        ],
    }
    doc_bad = svc.create_invoice_document(db, CanonicalInvoice(**bad_invoice), original_filename="demo_exception.pdf")
    result_bad = svc.run_match_for_invoice(db, doc_bad.id)

    return {
        "vendor_id": vendor.id,
        "po_number": po.po_number,
        "grn_number": grn.grn_number,
        "exception_po_number": "PO-2026-002",
        "clean_match": {"match_result_id": result_clean.id, "status": result_clean.status},
        "exception_match": {
            "match_result_id": result_bad.id,
            "status": result_bad.status,
            "reason_codes": result_bad.reason_codes,
        },
    }
