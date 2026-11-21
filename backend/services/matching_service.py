"""
Database orchestration for the 3-way match.

The engine stays pure; this module loads vendors/POs/GRNs, feeds the engine,
persists the verdict, keeps the PO line balances correct across partial
deliveries, routes approvals, writes the audit trail and computes metrics.
"""
import csv
import difflib
import io
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..config import (
    APPROVAL_ESCALATED_REASON_CODES,
    APPROVAL_RULES,
    DEFAULT_TENANT,
    MATCH,
    SEGREGATION_OF_DUTIES,
    SHADOW,
    VENDOR_RISK_APPROVERS,
    resolve_tolerances,
)
from ..database import (
    ApprovalDecision,
    AuditLog,
    DecisionObservation,
    ExceptionAction,
    GoodsReceipt,
    GoodsReceiptLine,
    InvoiceDocument,
    InvoiceDocumentLine,
    MatchResult,
    POLineBalance,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from .canonical import (
    SOURCE_ERP,
    CanonicalInvoice,
    canonical_grn_from_dict,
    canonical_invoice_from_extraction,
    canonical_po_from_dict,
    parse_canonical_date,
    validate_canonical_invoice,
)
from .decision import APPROVE, REJECT, REVIEW, decide
from .shadow import record_human_decision, record_recommendation, vendor_agreement
from .matching_engine import (
    REASON_DESCRIPTIONS,
    STATUS_EXCEPTION,
    STATUS_MATCHED,
    STATUS_REJECT,
    MatchContext,
    POState,
    POLineState,
    run_three_way_match,
)
from .normalization import (
    VendorRef,
    normalize_gstin,
    normalize_invoice,
    normalize_item_code,
    normalize_po_lines,
    normalize_uom,
    resolve_vendor,
    similarity,
)
from .validator import to_decimal

# An OCR item code must be this close to a PO code before it is trusted.
GRN_CODE_MATCH_THRESHOLD = 0.85


def _dec(value: Any) -> Decimal:
    return to_decimal(value)


def _dt(value: Any) -> Optional[datetime]:
    parsed = parse_canonical_date(value)
    return datetime(parsed.year, parsed.month, parsed.day) if parsed else None


def _audit(db: Session, entity_type: str, entity_id: Optional[int], action: str, actor: Optional[str], before=None, after=None):
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            before=before,
            after=after,
        )
    )


# ---------------------------------------------------------------------------
# Vendor master
# ---------------------------------------------------------------------------
def upsert_vendor(db: Session, payload: Dict[str, Any]) -> Vendor:
    gstin = normalize_gstin(payload.get("gstin")) or None
    code = (payload.get("code") or gstin or payload.get("name") or "").strip()
    if not code:
        raise ValueError("Vendor needs at least a code, GSTIN or name")

    vendor = db.query(Vendor).filter(Vendor.code == code).first()
    if vendor is None and gstin:
        vendor = db.query(Vendor).filter(Vendor.gstin == gstin).first()

    if vendor is None:
        vendor = Vendor(code=code, name=payload.get("name") or code)
        db.add(vendor)

    vendor.name = payload.get("name") or vendor.name
    vendor.gstin = gstin or vendor.gstin
    vendor.risk_tier = (payload.get("risk_tier") or vendor.risk_tier or "LOW").upper()
    vendor.category = payload.get("category") or vendor.category
    vendor.payment_terms = payload.get("payment_terms") or vendor.payment_terms
    if payload.get("tolerance_price_pct") is not None:
        vendor.tolerance_price_pct = str(payload["tolerance_price_pct"])
    if payload.get("tolerance_qty") is not None:
        vendor.tolerance_qty = str(payload["tolerance_qty"])
    db.commit()
    db.refresh(vendor)
    _audit(db, "vendor", vendor.id, "UPSERT", "system", None, {"code": vendor.code, "name": vendor.name})
    db.commit()
    return vendor


def vendor_refs(db: Session) -> List[VendorRef]:
    vendors = db.query(Vendor).filter(Vendor.is_active.is_(True)).all()
    return [
        VendorRef(
            id=v.id,
            code=v.code,
            name=v.name,
            gstin=v.gstin,
            risk_tier=(v.risk_tier or "LOW").upper(),
            category=v.category,
            tolerance_override={
                k: v
                for k, v in {
                    "price_pct": v.tolerance_price_pct,
                    "quantity": v.tolerance_qty,
                }.items()
                if v
            },
        )
        for v in vendors
    ]


# ---------------------------------------------------------------------------
# PO / GRN import
# ---------------------------------------------------------------------------
def import_purchase_order(db: Session, payload: Dict[str, Any], source: str = SOURCE_ERP) -> PurchaseOrder:
    canonical = canonical_po_from_dict(payload, source=source)
    if not canonical.po_number:
        raise ValueError("PO number is required")

    vendor_id = None
    if canonical.vendor_gstin or canonical.vendor_name:
        resolution = resolve_vendor(vendor_refs(db), canonical.vendor_name, canonical.vendor_gstin)
        if resolution.vendor and resolution.vendor.id:
            vendor_id = resolution.vendor.id
        else:
            # A PO import is a legitimate onboarding source: create the vendor
            # master entry from it (GSTIN as the key) so later invoices resolve.
            code = canonical.vendor_gstin or canonical.vendor_name
            vendor = Vendor(
                code=code,
                name=canonical.vendor_name or code,
                gstin=canonical.vendor_gstin,
                risk_tier="LOW",
            )
            db.add(vendor)
            db.flush()
            vendor_id = vendor.id

    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == canonical.po_number).first()
    if po is None:
        po = PurchaseOrder(po_number=canonical.po_number)
        db.add(po)

    po.vendor_id = vendor_id or po.vendor_id
    po.status = canonical.status or "OPEN"
    po.currency = canonical.currency or "INR"
    po.po_date = _dt(canonical.po_date)
    po.category = canonical.category or po.category
    po.source = source

    subtotal = _dec(canonical.subtotal)
    total_tax = _dec(canonical.total_tax)
    if subtotal == 0 and canonical.lines:
        subtotal = sum((_dec(line.amount) or (_dec(line.quantity) * _dec(line.rate))) for line in canonical.lines)
    if total_tax == 0 and canonical.lines:
        total_tax = sum(
            ((_dec(line.amount) or (_dec(line.quantity) * _dec(line.rate))) * _dec(line.tax_rate) / Decimal("100"))
            for line in canonical.lines
        ).quantize(Decimal("0.01"))
    grand_total = _dec(canonical.grand_total) or (subtotal + total_tax)

    po.subtotal = subtotal
    po.total_tax = total_tax
    po.grand_total = grand_total
    db.flush()

    if canonical.lines:
        existing_by_line = {line.line_no: line for line in po.lines}
        seen = set()
        for line in normalize_po_lines(canonical.lines):
            po_line = existing_by_line.get(line.line_no)
            if po_line is None:
                po_line = PurchaseOrderLine(po_id=po.id, line_no=line.line_no)
                db.add(po_line)
            po_line.item_code = line.item_code
            po_line.description = line.description
            po_line.hsn_sac = line.hsn_sac
            po_line.unit = normalize_uom(line.unit)
            po_line.ordered_qty = _dec(line.quantity)
            po_line.rate = _dec(line.rate)
            po_line.tax_rate = _dec(line.tax_rate)
            po_line.line_amount = _dec(line.amount) or (_dec(line.quantity) * _dec(line.rate))
            db.flush()
            _ensure_balance(db, po_line)
            seen.add(line.line_no)
        for line_no, po_line in existing_by_line.items():
            if line_no not in seen:
                db.delete(po_line)
    else:
        for po_line in po.lines:
            _ensure_balance(db, po_line)

    db.commit()
    db.refresh(po)
    _audit(db, "purchase_order", po.id, "IMPORT", source, None, {"po_number": po.po_number, "lines": len(po.lines)})
    db.commit()
    return po


def _ensure_balance(db: Session, po_line: PurchaseOrderLine) -> POLineBalance:
    balance = po_line.balance
    if balance is None:
        balance = db.query(POLineBalance).filter(POLineBalance.po_line_id == po_line.id).first()
    if balance is None:
        balance = POLineBalance(po_line_id=po_line.id, received_qty=0, invoiced_qty=0, credited_qty=0)
        db.add(balance)
        db.flush()
    return balance


def _grn_number_key(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _fuzzy_po_line_by_code(code: Optional[str], po_lines: Sequence[PurchaseOrderLine]) -> Optional[PurchaseOrderLine]:
    """
    Match an OCR-mangled item code ("ALN-SETS") to a PO line.

    Only a clear best match is returned: a near-tie is left unmapped so the GRN
    goes to review instead of silently booking against the wrong item.
    """
    key = normalize_item_code(code)
    if not key:
        return None
    scored = []
    for line in po_lines:
        candidate = normalize_item_code(line.item_code)
        if not candidate:
            continue
        scored.append((difflib.SequenceMatcher(None, key, candidate).ratio(), line))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    if best_score < GRN_CODE_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and scored[1][0] >= best_score - 0.05:
        return None
    return best


def import_grn(db: Session, payload: Dict[str, Any], source: str = SOURCE_ERP) -> GoodsReceipt:
    canonical = canonical_grn_from_dict(payload, source=source)
    if not canonical.po_number:
        raise ValueError("GRN must reference a PO number")

    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == canonical.po_number).first()
    if po is None:
        raise ValueError(f"Purchase order {canonical.po_number} not found")

    # Re-uploading the same receipt must not double-count. Compare normalized
    # numbers (OCR/ERP variants of the same GRN) for this PO only.
    if canonical.grn_number:
        wanted = _grn_number_key(canonical.grn_number)
        for previous in db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all():
            if _grn_number_key(previous.grn_number) == wanted:
                db.delete(previous)
        db.flush()

    grn = GoodsReceipt(
        grn_number=canonical.grn_number,
        po_id=po.id,
        receipt_date=_dt(canonical.receipt_date) or datetime.utcnow(),
        currency=canonical.currency or "INR",
        source=source,
    )
    db.add(grn)
    db.flush()

    po_lines = po.lines
    for grn_line in canonical.lines:
        po_line = None
        if grn_line.po_line_no:
            po_line = next((l for l in po_lines if l.line_no == grn_line.po_line_no), None)
        if po_line is None and grn_line.item_code:
            code = normalize_item_code(grn_line.item_code)
            po_line = next((l for l in po_lines if normalize_item_code(l.item_code) == code), None)
        if po_line is None and grn_line.item_code:
            po_line = _fuzzy_po_line_by_code(grn_line.item_code, po_lines)
        if po_line is None and grn_line.description:
            best, best_score = None, 0.0
            for candidate in po_lines:
                score = similarity(grn_line.description, candidate.description)
                if score > best_score:
                    best, best_score = candidate, score
            if best is not None and best_score >= 0.72:
                po_line = best

        db.add(
            GoodsReceiptLine(
                grn_id=grn.id,
                po_line_id=po_line.id if po_line else None,
                line_no=po_line.line_no if po_line else grn_line.line_no,
                item_code=normalize_item_code(grn_line.item_code),
                description=grn_line.description,
                unit=normalize_uom(grn_line.unit),
                received_qty=_dec(grn_line.received_quantity),
            )
        )
    db.flush()
    # Rebuild received quantities from the receipts themselves, so repeated
    # uploads and deleted receipts cannot leave a drifted balance behind.
    for po_line in po.lines:
        total = (
            db.query(func.coalesce(func.sum(GoodsReceiptLine.received_qty), 0))
            .filter(GoodsReceiptLine.po_line_id == po_line.id)
            .scalar()
        )
        balance = _ensure_balance(db, po_line)
        balance.received_qty = _dec(total).quantize(Decimal("0.0001"))

    db.commit()
    db.refresh(grn)
    _audit(db, "goods_receipt", grn.id, "IMPORT", source, None, {"grn_number": grn.grn_number, "po": po.po_number})
    db.commit()
    return grn


def import_grn_csv(db: Session, text: str, source: str = "CSV") -> List[GoodsReceipt]:
    """
    Imports GRNs from a flat CSV:
    grn_number,po_number,receipt_date,item_code,description,received_qty,po_line_no,unit
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        grn_number = (row.get("grn_number") or row.get("grn") or "").strip()
        po_number = (row.get("po_number") or row.get("po") or "").strip()
        if not po_number:
            continue
        key = grn_number or f"{po_number}-{row.get('receipt_date', '')}"
        entry = grouped.setdefault(
            key,
            {
                "grn_number": grn_number or None,
                "po_number": po_number,
                "receipt_date": row.get("receipt_date") or row.get("date"),
                "lines": [],
            },
        )
        entry["lines"].append(
            {
                "line_no": row.get("line_no"),
                "po_line_no": row.get("po_line_no"),
                "item_code": row.get("item_code"),
                "description": row.get("description"),
                "unit": row.get("unit"),
                "received_quantity": row.get("received_qty") or row.get("quantity"),
            }
        )
    return [import_grn(db, payload, source=source) for payload in grouped.values()]


def import_po_csv(db: Session, text: str, source: str = "CSV") -> List[PurchaseOrder]:
    """
    Imports POs from a flat CSV:
    po_number,vendor_name,vendor_gstin,po_date,currency,status,item_code,description,quantity,unit,rate,tax_rate
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        po_number = (row.get("po_number") or row.get("po") or "").strip()
        if not po_number:
            continue
        entry = grouped.setdefault(
            po_number,
            {
                "po_number": po_number,
                "vendor_name": row.get("vendor_name") or row.get("vendor"),
                "vendor_gstin": row.get("vendor_gstin") or row.get("gstin"),
                "po_date": row.get("po_date") or row.get("date"),
                "currency": row.get("currency") or "INR",
                "status": row.get("status") or "OPEN",
                "category": row.get("category"),
                "lines": [],
            },
        )
        if row.get("description") or row.get("item_code"):
            qty = to_decimal(row.get("quantity") or row.get("ordered_qty"))
            rate = to_decimal(row.get("rate") or row.get("unit_price"))
            entry["lines"].append(
                {
                    "line_no": row.get("line_no") or len(entry["lines"]) + 1,
                    "item_code": row.get("item_code"),
                    "description": row.get("description"),
                    "quantity": str(qty),
                    "unit": row.get("unit"),
                    "rate": str(rate),
                    "tax_rate": row.get("tax_rate") or "0",
                    "amount": row.get("amount") or str((qty * rate).quantize(Decimal("0.01"))),
                }
            )
    return [import_purchase_order(db, payload, source=source) for payload in grouped.values()]


# ---------------------------------------------------------------------------
# Canonicalize an extraction into an invoice document
# ---------------------------------------------------------------------------
def _reverse_invoice_balances(db: Session, result: MatchResult) -> None:
    """Undo a previous match's balance impact before re-matching the same document."""
    if not result.balances_applied or not result.po_id:
        return
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first()
    if po is None:
        return
    lines_by_no = {line.line_no: line for line in po.lines}
    for finding in result.line_findings or []:
        po_line_no = finding.get("po_line_no")
        if po_line_no is None:
            continue
        po_line = lines_by_no.get(int(po_line_no))
        if po_line is None:
            continue
        balance = _ensure_balance(db, po_line)
        qty = _dec(finding.get("invoice_qty"))
        new_qty = (_dec(balance.invoiced_qty) - qty)
        balance.invoiced_qty = (new_qty if new_qty > 0 else Decimal("0")).quantize(Decimal("0.0001"))


def canonicalize_extraction(
    db: Session,
    extraction_log,
    po_number: Optional[str] = None,
    source_file_url: Optional[str] = None,
    validation_report: Optional[Dict[str, Any]] = None,
) -> InvoiceDocument:
    from ..schemas import InvoiceData

    if extraction_log is None:
        raise ValueError("Extraction log not found")

    invoice_data = InvoiceData(**extraction_log.extracted_data) if extraction_log.extracted_data else InvoiceData()
    canonical = canonical_invoice_from_extraction(invoice_data, po_number=po_number)
    if not canonical.po_number:
        canonical.po_number = po_number or (invoice_data.metadata.po_number if invoice_data.metadata else None)

    vendors = vendor_refs(db)
    canonical, resolution = normalize_invoice(canonical, vendors)
    errors = validate_canonical_invoice(canonical)

    # Re-canonicalizing the same extraction (e.g. after the user supplies a PO
    # number) updates the existing document instead of creating a duplicate.
    doc = (
        db.query(InvoiceDocument)
        .filter(InvoiceDocument.extraction_log_id == extraction_log.id)
        .order_by(InvoiceDocument.id.desc())
        .first()
    )
    if doc is None:
        doc = InvoiceDocument(extraction_log_id=extraction_log.id, tenant_id=DEFAULT_TENANT)
        db.add(doc)
    else:
        for previous in db.query(MatchResult).filter(MatchResult.invoice_document_id == doc.id).all():
            _reverse_invoice_balances(db, previous)
            db.delete(previous)
        db.query(InvoiceDocumentLine).filter(InvoiceDocumentLine.invoice_document_id == doc.id).delete()

    doc.vendor_id = resolution.vendor.id if resolution.vendor else None
    doc.invoice_number = canonical.invoice_number
    doc.invoice_date = _dt(canonical.invoice_date)
    doc.currency = canonical.currency
    doc.po_number = canonical.po_number
    doc.subtotal = _dec(canonical.subtotal)
    doc.total_tax = _dec(canonical.total_tax)
    doc.grand_total = _dec(canonical.grand_total)
    doc.canonical_data = canonical.model_dump()
    doc.validation_errors = errors
    if validation_report is not None:
        doc.validation_report = validation_report
        doc.validation_passed = bool(validation_report.get("passed"))
    doc.source_file_url = source_file_url or extraction_log.source_file_url
    doc.original_filename = extraction_log.original_filename
    doc.status = "READY"
    db.flush()
    _add_document_lines(db, doc, canonical)
    db.commit()
    db.refresh(doc)
    _audit(
        db,
        "invoice_document",
        doc.id,
        "CANONICALIZED",
        "ocr",
        None,
        {"invoice_number": doc.invoice_number, "errors": errors},
    )
    db.commit()
    return doc


def _add_document_lines(db: Session, doc: InvoiceDocument, canonical: CanonicalInvoice) -> None:
    for line in canonical.lines:
        db.add(
            InvoiceDocumentLine(
                invoice_document_id=doc.id,
                line_no=line.line_no,
                item_code=line.item_code,
                description=line.description,
                hsn_sac=line.hsn_sac,
                quantity=_dec(line.quantity),
                unit=line.unit,
                rate=_dec(line.rate),
                tax_rate=_dec(line.tax_rate),
                tax_amount=_dec(line.tax_amount),
                amount=_dec(line.amount),
                field_confidence=line.field_confidence,
                field_source=line.field_source,
            )
        )


def create_invoice_document(
    db: Session,
    canonical: CanonicalInvoice,
    extraction_log_id: Optional[int] = None,
    original_filename: Optional[str] = None,
    source_file_url: Optional[str] = None,
    validation_report: Optional[Dict[str, Any]] = None,
) -> InvoiceDocument:
    """
    Stores an already-canonical invoice (e.g. pushed by an ERP integration).

    `validation_report` is the gate verdict when one was produced; without it the
    document has no verification and the decision layer will treat it as unproven.
    """
    vendors = vendor_refs(db)
    canonical, resolution = normalize_invoice(canonical, vendors)
    errors = validate_canonical_invoice(canonical)

    doc = InvoiceDocument(
        extraction_log_id=extraction_log_id,
        vendor_id=resolution.vendor.id if resolution.vendor else None,
        invoice_number=canonical.invoice_number,
        invoice_date=_dt(canonical.invoice_date),
        currency=canonical.currency,
        po_number=canonical.po_number,
        subtotal=_dec(canonical.subtotal),
        total_tax=_dec(canonical.total_tax),
        grand_total=_dec(canonical.grand_total),
        canonical_data=canonical.model_dump(),
        validation_errors=errors,
        original_filename=original_filename,
        source_file_url=source_file_url,
        validation_report=validation_report,
        validation_passed=bool(validation_report.get("passed")) if validation_report else None,
        status="READY",
    )
    db.add(doc)
    db.flush()
    _add_document_lines(db, doc, canonical)
    db.commit()
    db.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _build_po_state(db: Session, po: Optional[PurchaseOrder]) -> Optional[POState]:
    if po is None:
        return None

    invoiced_rows = (
        db.query(InvoiceDocument.grand_total)
        .join(MatchResult, MatchResult.invoice_document_id == InvoiceDocument.id)
        .filter(MatchResult.po_id == po.id)
        .filter(MatchResult.status.in_([STATUS_MATCHED, STATUS_EXCEPTION]))
        .all()
    )
    total_invoiced = sum((_dec(row[0]) for row in invoiced_rows), Decimal("0"))

    lines: List[POLineState] = []
    for po_line in po.lines:
        balance = _ensure_balance(db, po_line)
        lines.append(
            POLineState(
                line_no=po_line.line_no,
                ordered_qty=_dec(po_line.ordered_qty),
                received_qty=_dec(balance.received_qty),
                invoiced_qty=_dec(balance.invoiced_qty),
                credited_qty=_dec(balance.credited_qty),
                rate=_dec(po_line.rate),
                tax_rate=_dec(po_line.tax_rate),
                item_code=normalize_item_code(po_line.item_code),
                description=po_line.description,
                unit=normalize_uom(po_line.unit),
                line_amount=_dec(po_line.line_amount),
            )
        )

    db.commit()
    return POState(
        po_number=po.po_number,
        status=po.status or "OPEN",
        vendor_name=po.vendor.name if po.vendor else None,
        vendor_gstin=po.vendor.gstin if po.vendor else None,
        currency=po.currency or "INR",
        lines=lines,
        grand_total=_dec(po.grand_total),
        invoiced_value=total_invoiced,
    )


def find_duplicate_invoice(db: Session, doc: InvoiceDocument) -> Optional[int]:
    """Same vendor + invoice number + amount + date within the duplicate window."""
    if not doc.invoice_number or not doc.grand_total:
        return None
    cutoff = datetime.utcnow() - timedelta(days=MATCH.duplicate_window_days)
    query = db.query(InvoiceDocument).filter(
        InvoiceDocument.id != doc.id,
        InvoiceDocument.invoice_number == doc.invoice_number,
        InvoiceDocument.grand_total == doc.grand_total,
        InvoiceDocument.created_at >= cutoff,
    )
    if doc.invoice_date:
        query = query.filter(InvoiceDocument.invoice_date == doc.invoice_date)
    if doc.vendor_id:
        query = query.filter(InvoiceDocument.vendor_id == doc.vendor_id)
        existing = query.order_by(InvoiceDocument.created_at.asc()).first()
        return existing.id if existing else None

    canonical = doc.canonical_data or {}
    gstin = normalize_gstin(canonical.get("vendor_gstin"))
    if gstin:
        for candidate in query.all():
            other = normalize_gstin((candidate.canonical_data or {}).get("vendor_gstin"))
            if other == gstin:
                return candidate.id
    return None


def run_match_for_invoice(db: Session, invoice_document_id: int) -> MatchResult:
    started = time.time()
    doc = db.query(InvoiceDocument).filter(InvoiceDocument.id == invoice_document_id).first()
    if doc is None:
        raise ValueError(f"Invoice document #{invoice_document_id} not found")

    canonical = CanonicalInvoice(**(doc.canonical_data or {}))
    vendors = vendor_refs(db)
    canonical, resolution = normalize_invoice(canonical, vendors)

    po = None
    if canonical.po_number:
        po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == canonical.po_number).first()

    vendor = None
    if doc.vendor_id:
        vendor = db.query(Vendor).filter(Vendor.id == doc.vendor_id).first()
    elif resolution.vendor and resolution.vendor.id:
        vendor = db.query(Vendor).filter(Vendor.id == resolution.vendor.id).first()

    tolerances = resolve_tolerances(
        vendor_name=vendor.name if vendor else canonical.vendor_name,
        category=(po.category if po and po.category else (vendor.category if vendor else None)),
        vendor_override=(
            {
                k: v
                for k, v in {
                    "price_pct": vendor.tolerance_price_pct,
                    "quantity": vendor.tolerance_qty,
                }.items()
                if v
            }
            if vendor
            else None
        ),
    )

    duplicate_of = find_duplicate_invoice(db, doc)

    ctx = MatchContext(
        invoice=canonical,
        po=_build_po_state(db, po),
        vendor_resolved=resolution.resolved or bool(doc.vendor_id),
        vendor_resolution_method=resolution.method,
        vendor_id=vendor.id if vendor else None,
        duplicate_of_invoice_id=duplicate_of,
        tolerances=tolerances,
        require_grn=MATCH.require_grn,
        today=datetime.utcnow().date().isoformat(),
    )
    outcome = run_three_way_match(ctx)
    duration_ms = int((time.time() - started) * 1000)

    result = MatchResult(
        invoice_document_id=doc.id,
        po_id=po.id if po else None,
        status=outcome.status,
        reason_codes=outcome.reason_codes,
        checks=[c.to_dict() for c in outcome.checks],
        line_findings=[f.to_dict() for f in outcome.line_findings],
        computed_tax=_dec(outcome.computed_tax),
        computed_total=_dec(outcome.computed_total),
        variance_total=_dec(outcome.variance_total),
        duplicate_of_invoice_id=duplicate_of,
        match_duration_ms=duration_ms,
        matched_at=datetime.utcnow(),
    )
    route_approval(result, _dec(doc.grand_total), vendor.risk_tier if vendor else "LOW", outcome.reason_codes)
    db.add(result)
    db.flush()

    doc.vendor_id = doc.vendor_id or (vendor.id if vendor else None)
    doc.po_id = po.id if po else None
    doc.status = {
        STATUS_MATCHED: "MATCHED",
        STATUS_EXCEPTION: "EXCEPTION",
        STATUS_REJECT: "REJECT",
    }[outcome.status]

    if outcome.status == STATUS_MATCHED:
        _apply_invoice_to_balances(db, result, doc)

    # Validation gate + approve/review/reject. This runs after the approval
    # routing because the decision can override it (auto-approve or hold).
    apply_decision(db, result, doc)

    db.commit()
    db.refresh(result)
    _audit(
        db,
        "match_result",
        result.id,
        "MATCH_RUN",
        "engine",
        None,
        {"status": result.status, "reason_codes": result.reason_codes, "duration_ms": duration_ms},
    )
    db.commit()
    return result


def _apply_invoice_to_balances(db: Session, result: MatchResult, doc: InvoiceDocument) -> None:
    """Increments invoiced_qty on each mapped PO line exactly once per match result."""
    if result.balances_applied:
        return
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first() if result.po_id else None
    if po is None:
        result.balances_applied = True
        return

    lines_by_no = {line.line_no: line for line in po.lines}
    for finding in result.line_findings or []:
        po_line_no = finding.get("po_line_no")
        if po_line_no is None:
            continue
        po_line = lines_by_no.get(int(po_line_no))
        if po_line is None:
            continue
        balance = _ensure_balance(db, po_line)
        qty = _dec(finding.get("invoice_qty"))
        balance.invoiced_qty = (_dec(balance.invoiced_qty) + qty).quantize(Decimal("0.0001"))

    result.balances_applied = True
    _audit(
        db,
        "purchase_order",
        po.id,
        "BALANCE_INVOICED",
        "engine",
        None,
        {"invoice_document_id": doc.id, "match_result_id": result.id},
    )


# ---------------------------------------------------------------------------
# Exception workflow
# ---------------------------------------------------------------------------
EXCEPTION_ACTIONS = {"ACCEPT_OVERRIDE", "CREDIT_NOTE", "HOLD", "REJECT"}


def apply_exception_action(
    db: Session,
    match_result_id: int,
    action: str,
    reason: Optional[str],
    actor: str,
) -> MatchResult:
    action = (action or "").upper()
    if action not in EXCEPTION_ACTIONS:
        raise ValueError(f"Unsupported action '{action}'. Use one of {sorted(EXCEPTION_ACTIONS)}")
    if action == "ACCEPT_OVERRIDE" and not (reason or "").strip():
        raise ValueError("An override reason is required")

    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise ValueError(f"Match result #{match_result_id} not found")
    if result.status == STATUS_MATCHED:
        raise ValueError("Matched invoices do not need exception actions")

    before = {
        "status": result.status,
        "resolution": result.resolution,
        "approval_status": result.approval_status,
    }
    doc = result.invoice_document
    if doc is None:
        raise ValueError("Match result has no invoice document")

    if action == "ACCEPT_OVERRIDE":
        result.resolution = "OVERRIDDEN"
        result.resolved_by = actor
        result.override_reason = reason
        result.resolved_at = datetime.utcnow()
        doc.status = "PENDING_APPROVAL"
        _apply_invoice_to_balances(db, result, doc)
    elif action == "CREDIT_NOTE":
        result.resolution = "CREDIT_NOTE"
        result.resolved_by = actor
        result.override_reason = reason
        result.resolved_at = datetime.utcnow()
        doc.status = "PENDING_APPROVAL"
    elif action == "HOLD":
        result.resolution = "HOLD"
        result.resolved_by = actor
        result.override_reason = reason
        doc.status = "EXCEPTION"
    else:  # REJECT
        result.resolution = "REJECTED"
        result.resolved_by = actor
        result.override_reason = reason
        result.resolved_at = datetime.utcnow()
        result.approval_status = "REJECTED"
        doc.status = "REJECT"

    db.add(
        ExceptionAction(
            match_result_id=result.id,
            action=action,
            reason=reason,
            actor=actor,
            payload={"reason_codes": result.reason_codes},
        )
    )
    db.commit()
    # Shadow ledger: the human's decision sits next to our recommendation.
    if result.decision_observation_id:
        record_human_decision(
            db,
            observation_id=result.decision_observation_id,
            human_decision="REJECT" if action == "REJECT" else "REVIEW",
            actor=actor,
            note=reason,
        )
        observation = (
            db.query(DecisionObservation)
            .filter(DecisionObservation.id == result.decision_observation_id)
            .first()
        )
        if observation is not None:
            observation.decided_at = datetime.utcnow()
    _audit(
        db,
        "match_result",
        result.id,
        f"EXCEPTION_{action}",
        actor,
        before,
        {"resolution": result.resolution, "approval_status": result.approval_status, "reason": reason},
    )
    db.commit()
    db.refresh(result)
    return result


# ---------------------------------------------------------------------------
# Validation gate + approve/review/reject decision
# ---------------------------------------------------------------------------
def apply_decision(
    db: Session,
    result: MatchResult,
    doc: InvoiceDocument,
    validation_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Records the gate verdict and the engine's recommendation for one document,
    then sets the approval state accordingly.

    The recommendation is always stored, even when a human will decide — that is
    what makes shadow-mode agreement measurable.
    """
    report = validation_report or doc.validation_report
    if not report:
        # No gate was run for this document (ERP push, re-match, demo seed). Judge
        # the canonical numbers rather than assuming the extraction is unverified.
        from .validation_gate import gate_for_canonical

        report = gate_for_canonical(CanonicalInvoice(**(doc.canonical_data or {}))).to_dict()
        doc.validation_report = report
        doc.validation_passed = bool(report.get("passed"))

    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first() if result.po_id else None
    tenant = doc.tenant_id or DEFAULT_TENANT

    agreement = vendor_agreement(db, tenant, doc.vendor_id)
    outcome = decide(
        validation_passed=bool(report.get("passed")),
        validation_errors=report.get("errors") or [],
        validation_warnings=report.get("warnings") or [],
        match_status=result.status,
        reason_codes=result.reason_codes or [],
        vendor_known=doc.vendor_id is not None,
        po_exists=po is not None,
        po_open=((po.status or "OPEN").upper() == "OPEN") if po else False,
        vendor_agreement=agreement,
    )

    result.validation_passed = bool(report.get("passed"))
    result.validation_report = report or None
    result.decision = outcome.decision
    result.decision_reasons = outcome.reason_codes
    result.decision_explanation = outcome.explanation
    result.auto_approve_allowed = outcome.auto_approve_allowed

    if outcome.decision == APPROVE:
        result.approval_status = "APPROVED"
        result.approvals_required = 0
        result.approvals_received = 0
        result.approved_at = datetime.utcnow()
        doc.status = "APPROVED"
    elif outcome.decision == REJECT:
        result.approval_status = "NOT_REQUIRED"
        if result.resolution != "REJECTED":
            doc.status = "REJECT"
    else:
        if result.approval_status == "NOT_REQUIRED":
            result.approval_status = "PENDING"
        # Keep the gate's own status visible: "held at validation" is different
        # from "matched but awaiting a human".
        if doc.status not in {"PENDING_APPROVAL", "REJECT", "VALIDATION_FAILED"}:
            doc.status = "PENDING_APPROVAL"

    observation = record_recommendation(
        db,
        tenant_id=tenant,
        document_id=doc.id,
        vendor_id=doc.vendor_id,
        recommendation=outcome.decision,
        reason_codes=outcome.reason_codes,
        confidence=float(result.validation_report.get("confidence") or 0) or None if result.validation_report else None,
        match_result_id=result.id,
        extraction_log_id=doc.extraction_log_id,
    )
    result.decision_observation_id = observation.id
    db.flush()
    return outcome.to_dict()


def record_validation_failure(
    db: Session,
    doc: InvoiceDocument,
    validation_report: Dict[str, Any],
    po_number: Optional[str] = None,
    actor: str = "validation_gate",
) -> MatchResult:
    """
    A document that fails the gate never reaches the match engine.

    It still gets a match result so it appears in one review queue with one
    side-by-side view; the reason codes are the failed checks themselves.
    """
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).first() if po_number else None
    report = validation_report or {}
    result = MatchResult(
        invoice_document_id=doc.id,
        po_id=po.id if po else None,
        status=STATUS_EXCEPTION,
        reason_codes=list(report.get("errors") or ["VALIDATION_FAILED"]),
        checks=[
            {
                "name": "validation_gate",
                "passed": False,
                "detail": "; ".join(report.get("errors") or []),
            }
        ],
        line_findings=[],
        match_duration_ms=0,
        matched_at=datetime.utcnow(),
    )
    result.approval_status = "PENDING"
    result.approvals_required = 0
    db.add(result)
    db.flush()

    doc.po_id = po.id if po else doc.po_id
    doc.validation_passed = False
    doc.validation_report = report
    doc.status = "VALIDATION_FAILED"

    apply_decision(db, result, doc, validation_report=report)
    # The gate is a hard hold: it can never auto-approve.
    if result.decision == APPROVE:
        result.decision = REVIEW
        result.decision_reasons = list(report.get("errors") or ["VALIDATION_FAILED"])
        result.approval_status = "PENDING"
        result.auto_approve_allowed = False
    db.commit()
    db.refresh(result)
    _audit(
        db,
        "match_result",
        result.id,
        "VALIDATION_GATE_BLOCKED",
        actor,
        None,
        {"errors": report.get("errors"), "warnings": report.get("warnings"), "document_id": doc.id},
    )
    db.commit()
    return result


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------
def route_approval(result: MatchResult, amount: Decimal, vendor_risk: str, reason_codes: Sequence[str]) -> None:
    """
    Picks the amount band, then adds the roles required by vendor risk and by
    escalated exception codes. Pure function of policy + data.
    """
    if result.status == STATUS_REJECT:
        result.required_roles = []
        result.approvals_required = 0
        result.approvals_received = 0
        result.approval_status = "NOT_REQUIRED"
        return

    rule = next((r for r in APPROVAL_RULES if r.covers(amount)), APPROVAL_RULES[-1])
    roles: List[str] = list(rule.roles)

    if any(code in APPROVAL_ESCALATED_REASON_CODES for code in reason_codes):
        roles.append("manager")
    risk_role = VENDOR_RISK_APPROVERS.get((vendor_risk or "LOW").upper())
    if risk_role:
        roles.append(risk_role)

    deduped: List[str] = []
    for role in roles:
        if role not in deduped:
            deduped.append(role)

    result.required_roles = deduped
    result.approvals_required = len(deduped)
    result.approvals_received = 0
    result.approval_status = "PENDING" if deduped else "NOT_REQUIRED"


def submit_approval(
    db: Session,
    match_result_id: int,
    approver: str,
    role: Optional[str],
    decision: str,
    note: Optional[str] = None,
) -> MatchResult:
    decision = (decision or "").upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValueError("Decision must be APPROVED or REJECTED")
    if not (approver or "").strip():
        raise ValueError("Approver is required")

    result = db.query(MatchResult).filter(MatchResult.id == match_result_id).first()
    if result is None:
        raise ValueError(f"Match result #{match_result_id} not found")

    doc = result.invoice_document
    if doc is None:
        raise ValueError("Match result has no invoice document")
    if result.status == STATUS_EXCEPTION and result.resolution not in {"OVERRIDDEN", "CREDIT_NOTE"}:
        raise ValueError("Resolve the exception before approving it")

    if SEGREGATION_OF_DUTIES and result.resolved_by and result.resolved_by.lower() == approver.lower():
        raise ValueError("Segregation of duties: the person who overrode the exception cannot approve it")

    if any(a.approver.lower() == approver.lower() for a in result.approvals):
        raise ValueError("This approver has already acted on this invoice")

    before = {"approval_status": result.approval_status, "approvals_received": result.approvals_received}
    db.add(
        ApprovalDecision(
            match_result_id=result.id,
            approver=approver,
            role=role,
            decision=decision,
            note=note,
        )
    )

    if decision == "REJECTED":
        result.approval_status = "REJECTED"
        doc.status = "REJECT"
    else:
        result.approvals_received = (result.approvals_received or 0) + 1
        if result.approvals_received >= (result.approvals_required or 0):
            result.approval_status = "APPROVED"
            result.approved_at = datetime.utcnow()
            doc.status = "APPROVED"

    # Shadow ledger: record what the human actually did with this document.
    if result.decision_observation_id:
        record_human_decision(
            db,
            observation_id=result.decision_observation_id,
            human_decision="APPROVE" if decision == "APPROVED" else "REJECT",
            actor=approver,
            note=note,
        )
        observation = (
            db.query(DecisionObservation)
            .filter(DecisionObservation.id == result.decision_observation_id)
            .first()
        )
        if observation is not None:
            observation.decided_at = datetime.utcnow()

    db.commit()
    _audit(
        db,
        "match_result",
        result.id,
        f"APPROVAL_{decision}",
        approver,
        before,
        {"approval_status": result.approval_status, "approvals_received": result.approvals_received, "note": note},
    )
    db.commit()
    db.refresh(result)
    return result


# ---------------------------------------------------------------------------
# Side-by-side payload for the exception UI
# ---------------------------------------------------------------------------
def build_side_by_side(db: Session, result: MatchResult) -> Dict[str, Any]:
    doc = result.invoice_document
    if doc is None:
        raise ValueError("Match result has no invoice document")
    canonical = doc.canonical_data or {}
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first() if result.po_id else None

    po_lines_by_no = {line.line_no: line for line in (po.lines if po else [])}
    grn_qty_by_line: Dict[int, Decimal] = {}
    grn_documents: List[Dict[str, Any]] = []
    if po:
        for grn in db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all():
            grn_documents.append(
                {
                    "grn_number": grn.grn_number,
                    "receipt_date": grn.receipt_date.isoformat() if grn.receipt_date else None,
                    "lines": [
                        {
                            "po_line_no": line.line_no,
                            "received_qty": str(line.received_qty),
                            "item_code": line.item_code,
                            "description": line.description,
                        }
                        for line in grn.lines
                    ],
                }
            )
        balances = (
            db.query(POLineBalance, PurchaseOrderLine)
            .join(PurchaseOrderLine, POLineBalance.po_line_id == PurchaseOrderLine.id)
            .filter(PurchaseOrderLine.po_id == po.id)
            .all()
        )
        for balance, po_line in balances:
            grn_qty_by_line[po_line.line_no] = _dec(balance.received_qty)

    invoice_lines = doc.lines
    lines: List[Dict[str, Any]] = []
    for finding in result.line_findings or []:
        line_no = int(finding.get("invoice_line_no") or 0)
        invoice_line = next((l for l in invoice_lines if l.line_no == line_no), None)
        po_line_no = finding.get("po_line_no")
        po_line = po_lines_by_no.get(int(po_line_no)) if po_line_no is not None else None
        mismatched = []
        for code in finding.get("reason_codes") or []:
            if code in {"QTY_EXCEEDS_GRN", "GRN_MISSING", "QTY_EXCEEDS_PO"}:
                mismatched.append("quantity")
            elif code == "RATE_TOLERANCE_EXCEEDED":
                mismatched.append("rate")
            elif code == "TAX_RATE_MISMATCH":
                mismatched.append("tax_rate")
            elif code == "LINE_AMOUNT_MISMATCH":
                mismatched.append("amount")
            elif code == "LINE_UNMAPPED":
                mismatched.append("item")

        lines.append(
            {
                "invoice_line": {
                    "line_no": line_no,
                    "item_code": invoice_line.item_code if invoice_line else finding.get("item_code"),
                    "description": invoice_line.description if invoice_line else finding.get("description"),
                    "quantity": str(invoice_line.quantity) if invoice_line else finding.get("invoice_qty"),
                    "unit": invoice_line.unit if invoice_line else None,
                    "rate": str(invoice_line.rate) if invoice_line else finding.get("invoice_rate"),
                    "tax_rate": str(invoice_line.tax_rate) if invoice_line else None,
                    "amount": str(invoice_line.amount) if invoice_line else finding.get("invoice_amount"),
                    "field_confidence": invoice_line.field_confidence if invoice_line else None,
                    "field_source": invoice_line.field_source if invoice_line else None,
                },
                "po_line": (
                    {
                        "line_no": po_line.line_no,
                        "item_code": po_line.item_code,
                        "description": po_line.description,
                        "quantity": str(po_line.ordered_qty),
                        "unit": po_line.unit,
                        "rate": str(po_line.rate),
                        "tax_rate": str(po_line.tax_rate),
                    }
                    if po_line
                    else None
                ),
                "grn": {
                    "po_line_no": po_line_no,
                    "received_qty": str(grn_qty_by_line.get(int(po_line_no), Decimal("0")))
                    if po_line_no is not None
                    else "0",
                    "already_invoiced_qty": finding.get("already_invoiced_qty"),
                    "billable_qty": finding.get("billable_qty"),
                    "mapping_method": finding.get("mapping_method"),
                    "mapping_score": finding.get("mapping_score"),
                },
                "mismatched_fields": sorted(set(mismatched)),
                "reason_codes": finding.get("reason_codes") or [],
                "status": finding.get("status"),
            }
        )

    header_fields = [
        {
            "field": "vendor",
            "invoice": canonical.get("vendor_name"),
            "po": po.vendor.name if po and po.vendor else None,
            "match": not any(c in (result.reason_codes or []) for c in ("VENDOR_MISMATCH", "VENDOR_PO_MISMATCH")),
        },
        {
            "field": "po_number",
            "invoice": canonical.get("po_number"),
            "po": po.po_number if po else None,
            "match": po is not None,
        },
        {
            "field": "currency",
            "invoice": canonical.get("currency"),
            "po": po.currency if po else None,
            "match": "CURRENCY_MISMATCH" not in (result.reason_codes or []),
        },
        {
            "field": "grand_total",
            "invoice": canonical.get("grand_total"),
            "po": str(po.grand_total) if po else None,
            "match": "TOTAL_MISMATCH" not in (result.reason_codes or []),
        },
    ]

    return {
        "match_result_id": result.id,
        "status": result.status,
        "reason_codes": result.reason_codes or [],
        "reason_descriptions": [REASON_DESCRIPTIONS.get(c, c) for c in (result.reason_codes or [])],
        "header_fields": header_fields,
        "lines": lines,
        "po": (
            {
                "po_number": po.po_number,
                "status": po.status,
                "currency": po.currency,
                "vendor": po.vendor.name if po.vendor else None,
                "grand_total": str(po.grand_total),
                "po_date": po.po_date.isoformat() if po.po_date else None,
            }
            if po
            else None
        ),
        "grns": grn_documents,
        "invoice": {
            "id": doc.id,
            "invoice_number": doc.invoice_number,
            "invoice_date": doc.invoice_date.isoformat() if doc.invoice_date else None,
            "vendor": canonical.get("vendor_name"),
            "vendor_gstin": canonical.get("vendor_gstin"),
            "currency": doc.currency,
            "subtotal": str(doc.subtotal),
            "total_tax": str(doc.total_tax),
            "grand_total": str(doc.grand_total),
            "source_file_url": doc.source_file_url,
            "status": doc.status,
            "validation_errors": doc.validation_errors or [],
            "field_confidence": canonical.get("field_confidence"),
            "field_source": canonical.get("field_source"),
        },
        "checks": result.checks or [],
        "workflow": {
            "resolution": result.resolution,
            "resolved_by": result.resolved_by,
            "override_reason": result.override_reason,
            "approval_status": result.approval_status,
            "approvals_required": result.approvals_required,
            "approvals_received": result.approvals_received,
            "required_roles": result.required_roles or [],
            "approvals": [
                {
                    "approver": a.approver,
                    "role": a.role,
                    "decision": a.decision,
                    "note": a.note,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in result.approvals
            ],
            "actions": [
                {
                    "action": a.action,
                    "reason": a.reason,
                    "actor": a.actor,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in result.actions
            ],
        },
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(db: Session) -> Dict[str, Any]:
    results: List[MatchResult] = db.query(MatchResult).all()
    total = len(results)
    matched = [r for r in results if r.status == STATUS_MATCHED]
    exceptions = [r for r in results if r.status == STATUS_EXCEPTION]
    rejects = [r for r in results if r.status == STATUS_REJECT]

    reason_counts: Dict[str, int] = {}
    duplicates = 0
    for result in results:
        for code in result.reason_codes or []:
            reason_counts[code] = reason_counts.get(code, 0) + 1
            if code == "DUPLICATE_INVOICE":
                duplicates += 1

    def _cycle_hours(result: MatchResult) -> Optional[float]:
        doc = result.invoice_document
        if not doc or not doc.created_at:
            return None
        end = result.approved_at or result.resolved_at or result.matched_at
        if not end:
            return None
        return round((end - doc.created_at).total_seconds() / 3600.0, 2)

    cycle_times = [h for h in (_cycle_hours(r) for r in results) if h is not None]
    # "Touchless" = matched by the engine with no human override and no reject.
    touchless = [r for r in matched if not r.resolution]
    total_value = sum((_dec(r.invoice_document.grand_total) for r in results if r.invoice_document), Decimal("0"))

    # ---- pipeline health: where documents come from and how they fail ----
    from ..database import ExtractionLog, InvoiceTemplate

    decisions = {"APPROVE": 0, "REVIEW": 0, "REJECT": 0}
    for result in results:
        if result.decision in decisions:
            decisions[result.decision] += 1

    logs = db.query(ExtractionLog).all()
    total_logs = len(logs)
    template_hits = sum(1 for log in logs if log.template_id is not None)
    ai_fallbacks = sum(
        1 for log in logs if (log.match_strategy or "").upper() == "AI_FALLBACK"
    )
    validation_failures = sum(1 for log in logs if log.validation_passed is False)
    templates = db.query(InvoiceTemplate).all()
    trusted = sum(1 for t in templates if (t.status or "").upper() in {"TRUSTED", "ACTIVE"})
    candidates = sum(1 for t in templates if (t.status or "").upper() in {"CANDIDATE", "DRAFT"})
    quarantined = sum(1 for t in templates if (t.status or "").upper() == "QUARANTINED")

    return {
        "total_invoices": total,
        "matched": len(matched),
        "exceptions": len(exceptions),
        "rejects": len(rejects),
        "decisions": decisions,
        "review_rate": round(decisions["REVIEW"] / total, 4) if total else 0.0,
        "pipeline": {
            "documents_extracted": total_logs,
            "template_hit_rate": round(template_hits / total_logs, 4) if total_logs else 0.0,
            "template_hits": template_hits,
            "ai_fallback_share": round(ai_fallbacks / total_logs, 4) if total_logs else 0.0,
            "ai_fallbacks": ai_fallbacks,
            "validation_failure_rate": round(validation_failures / total_logs, 4) if total_logs else 0.0,
            "validation_failures": validation_failures,
            "templates": {"trusted": trusted, "candidates": candidates, "quarantined": quarantined},
        },
        "straight_through_rate": round(len(touchless) / total, 4) if total else 0.0,
        "exception_rate": round(len(exceptions) / total, 4) if total else 0.0,
        "reject_rate": round(len(rejects) / total, 4) if total else 0.0,
        "touchless_invoices": len(touchless),
        "duplicates_caught": duplicates,
        "exception_rate_by_reason": {
            code: round(count / total, 4) if total else 0.0
            for code, count in sorted(reason_counts.items(), key=lambda kv: -kv[1])
        },
        "exception_counts_by_reason": dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])),
        "avg_cycle_time_hours": round(sum(cycle_times) / len(cycle_times), 2) if cycle_times else 0.0,
        "max_cycle_time_hours": max(cycle_times) if cycle_times else 0.0,
        "total_value_processed": str(total_value.quantize(Decimal("0.01"))),
        "avg_match_duration_ms": round(sum((r.match_duration_ms or 0) for r in results) / total, 1) if total else 0.0,
        "approvals_pending": sum(1 for r in results if r.approval_status == "PENDING"),
        "approved": sum(1 for r in results if r.approval_status == "APPROVED"),
    }


# ---------------------------------------------------------------------------
# Payment export
# ---------------------------------------------------------------------------
def payment_ready_rows(db: Session) -> List[Dict[str, Any]]:
    """Approved invoices that have not been marked paid yet."""
    results = (
        db.query(MatchResult)
        .filter(MatchResult.approval_status == "APPROVED")
        .filter(MatchResult.status.in_([STATUS_MATCHED, STATUS_EXCEPTION]))
        # A credit note that has not been received yet must not be paid out.
        .filter(or_(MatchResult.resolution.is_(None), MatchResult.resolution.notin_(["CREDIT_NOTE"])))
        .all()
    )
    rows: List[Dict[str, Any]] = []
    for result in results:
        doc = result.invoice_document
        if doc is None or doc.status == "PAID":
            continue
        canonical = doc.canonical_data or {}
        po = db.query(PurchaseOrder).filter(PurchaseOrder.id == result.po_id).first() if result.po_id else None
        rows.append(
            {
                "invoice_number": doc.invoice_number,
                "invoice_date": doc.invoice_date.strftime("%Y-%m-%d") if doc.invoice_date else "",
                "vendor": canonical.get("vendor_name") or "",
                "vendor_gstin": canonical.get("vendor_gstin") or "",
                "po_number": po.po_number if po else "",
                "currency": doc.currency or "INR",
                "subtotal": str(doc.subtotal or "0.00"),
                "tax": str(doc.total_tax or "0.00"),
                "amount": str(doc.grand_total or "0.00"),
                "match_status": result.status,
                "resolution": result.resolution or "AUTO",
                "approved_at": result.approved_at.strftime("%Y-%m-%d %H:%M") if result.approved_at else "",
                "source": doc.original_filename or "",
            }
        )
    return rows


def payment_csv(db: Session) -> str:
    rows = payment_ready_rows(db)
    fieldnames = [
        "invoice_number",
        "invoice_date",
        "vendor",
        "vendor_gstin",
        "po_number",
        "currency",
        "subtotal",
        "tax",
        "amount",
        "match_status",
        "resolution",
        "approved_at",
        "source",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def _xml_escape(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def payment_tally_xml(db: Session) -> str:
    """
    Minimal Tally-importable voucher XML for the approved invoices.
    (SAP / Zoho have their own shapes; CSV is the common starting point.)
    """
    rows = payment_ready_rows(db)
    vouchers = []
    for row in rows:
        vouchers.append(
            '    <VOUCHER VCHTYPE="Purchase" ACTION="Create">\n'
            f"      <DATE>{_xml_escape((row['invoice_date'] or '').replace('-', ''))}</DATE>\n"
            f"      <VOUCHERNUMBER>{_xml_escape(row['invoice_number'])}</VOUCHERNUMBER>\n"
            f"      <PARTYLEDGERNAME>{_xml_escape(row['vendor'])}</PARTYLEDGERNAME>\n"
            f"      <REFERENCE>{_xml_escape(row['po_number'])}</REFERENCE>\n"
            f"      <AMOUNT>{_xml_escape(row['amount'])}</AMOUNT>\n"
            "    </VOUCHER>"
        )
    body = "\n".join(vouchers)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<ENVELOPE>\n  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>\n"
        "  <BODY><IMPORTDATA>\n    <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>\n"
        "    <REQUESTDATA>\n<TALLYMESSAGE>\n"
        f"{body}\n"
        "</TALLYMESSAGE>\n    </REQUESTDATA>\n  </IMPORTDATA></BODY>\n</ENVELOPE>\n"
    )
