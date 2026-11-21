"""
Mixed-document ingest: one upload box for POs, GRNs and invoices.

For each file we run the existing OCR pipeline (local first, AI fallback when the
confidence is low), classify what the document is, then either import it into the
PO/GRN ledger or match it as an invoice.
"""
import io
import copy
import os
import re
import asyncio
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..database import ExtractionLog, GoodsReceipt, PurchaseOrder
from . import matching_service as svc
from .doc_classifier import (
    GRN,
    INVOICE,
    PURCHASE_ORDER,
    UNKNOWN,
    DOC_TYPE_LABELS,
    classify_document,
)
from .canonical import canonical_invoice_from_extraction
from .document_text_ocr import extract_document_text
from .decision import APPROVE as DECISION_APPROVE
from .external_ocr import is_image
from .grn_intake import po_lines_for_entry
from .grn_quantities import extract_accepted_quantities
from .grn_table_reader import map_lines_to_po, payload_lines, read_grn_table, sanity_problems
from .storage import save_bytes
from .validation_gate import validate_document

MODE_MAP = {"ai": "ai", "local": "local", "pdfplumber": "local"}

_CODE_RE = re.compile(r"Code\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]{1,40})", re.I)


class IngestError(Exception):
    pass


def _looks_like_code(value: Optional[str]) -> bool:
    if not value:
        return False
    text = value.strip()
    return bool(text) and len(text) <= 30 and " " not in text and re.search(r"\d", text) is not None


def _item_code(line) -> Optional[str]:
    for candidate in (getattr(line, "item_code", None), getattr(line, "part_number", None)):
        # An HSN code (e.g. "7318") is not an item code; several lines share it.
        if candidate and not str(candidate).strip().isdigit():
            return candidate
    if _looks_like_code(getattr(line, "description", None)):
        return line.description
    if _looks_like_code(getattr(line, "item_name", None)):
        return line.item_name
    return None


def _line_description(line) -> Optional[str]:
    parts: List[str] = []
    for candidate in (getattr(line, "item_name", None), getattr(line, "description", None)):
        text = (candidate or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts) or None


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_usable_text(text: str) -> bool:
    return len(re.findall(r"[A-Za-z0-9]{2,}", text or "")) >= 3


def _counterparty_gstin(inv, doc_type: str, gstins: List[str]) -> Optional[str]:
    # On a PO/GRN the first GSTIN belongs to the issuer (buyer) and the second to
    # the supplier. Prefer the raw-text order; AI extraction sometimes swaps them.
    if doc_type in (PURCHASE_ORDER, GRN):
        if len(gstins) > 1:
            return gstins[1]
        return inv.buyer.gstin or (gstins[0] if gstins else None)
    return inv.seller.gstin or (gstins[0] if gstins else None)


def _local_line_codes(pdf_bytes: bytes, expected: int) -> List[str]:
    """
    Re-reads the document with the deterministic table parser, which keeps the
    item-code column that AI extraction tends to drop. Only used positionally
    when the row count matches.
    """
    if expected <= 0:
        return []
    try:
        from .invoice_extractor import extract_invoice_local

        local = extract_invoice_local(pdf_bytes, filename="document.pdf")
    except Exception:
        return []
    codes: List[str] = []
    for line in local.line_items or []:
        candidate = getattr(line, "item_code", None) or getattr(line, "part_number", None)
        if not candidate and _looks_like_code(getattr(line, "description", None)):
            candidate = line.description
        codes.append(candidate)
    if len(codes) == expected and all(codes):
        return codes
    return []


def _issuer_name(inv, raw_text: str) -> Optional[str]:
    for candidate in (inv.seller.name, (raw_text or "").splitlines()[0] if raw_text else None):
        if candidate and not re.match(r"^(?:plot|no\.|gstin|ph\b)", candidate.strip(), re.I):
            return candidate.strip()
    return None


def _enrich_line_codes(extracted_data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """
    Attach item codes that the table parser missed. Some layouts print the code
    on its own line ("Code: CTN-5P-450"); if the count matches the extracted
    lines we can pair them positionally, which makes PO↔invoice mapping by code
    work instead of relying on fuzzy text.
    """
    codes = [c.strip().upper() for c in _CODE_RE.findall(raw_text or "")]
    lines = extracted_data.get("line_items") or []
    if not lines:
        return extracted_data

    if len(codes) == len(lines):
        pairs = list(zip(lines, codes))
    else:
        # The AI often folds the code into the description ("Code: CTN-5P-450").
        pairs = []
        for line in lines:
            found = _CODE_RE.search(str(line.get("description") or ""))
            if found:
                pairs.append((line, found.group(1).strip().upper()))

    for line, code in pairs:
        existing = str(line.get("item_code") or "").strip()
        # AI extraction sometimes stores the HSN in the item-code field; an
        # explicit "Code: X" in the document is a better key for matching.
        if not existing or existing.isdigit():
            line["item_code"] = code
    return extracted_data


def _po_payload(inv, cls, raw_text: str, filename: str, codes: List[str]) -> Dict[str, Any]:
    lines = []
    for idx, line in enumerate(inv.line_items or [], start=1):
        code = codes[idx - 1] if idx <= len(codes) else _item_code(line)
        lines.append(
            {
                "line_no": idx,
                "item_code": code,
                "description": _line_description(line),
                "hsn_sac": getattr(line, "hsn_sac", None),
                "quantity": _money(line.quantity),
                "unit": line.unit,
                "rate": _money(line.unit_price),
                "tax_rate": _money(line.tax_rate),
                "amount": _money(line.total_amount),
            }
        )

    subtotal = _money(inv.summary.subtotal) if inv.summary else 0.0
    total_tax = _money(inv.summary.total_gst) if inv.summary else 0.0
    if subtotal == 0 and lines:
        subtotal = sum((l["amount"] or (l["quantity"] * l["rate"])) for l in lines)
    grand_total = round(subtotal + total_tax, 2)
    return {
        "po_number": cls.document_number or f"PO-{filename.rsplit('.', 1)[0]}",
        "vendor_name": cls.vendor_name or _issuer_name(inv, raw_text),
        "vendor_gstin": _counterparty_gstin(inv, PURCHASE_ORDER, cls.gstins),
        "po_date": cls.document_date,
        "currency": (inv.metadata.currency if inv.metadata else None) or "INR",
        "status": "OPEN",
        "lines": lines,
        "subtotal": subtotal,
        "total_tax": total_tax,
        "grand_total": grand_total,
    }


def _grn_number_key(value: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _grn_po_lines(db, po_number: Optional[str], grn_number: Optional[str] = None) -> List[dict]:
    """
    PO lines for mapping a receipt; empty when the PO is not imported yet.

    A re-uploaded GRN replaces the previous one, so its quantities are added
    back before the open-quantity check; otherwise the same receipt looks like
    it is over-receiving.
    """
    if not po_number:
        return []
    try:
        rows = po_lines_for_entry(db, po_number)
    except Exception:
        return []
    if not grn_number:
        return rows
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number.strip()).first()
    if po is None:
        return rows
    key = _grn_number_key(grn_number)
    by_line = {row["po_line_no"]: row for row in rows}
    for previous in db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all():
        if _grn_number_key(previous.grn_number) != key:
            continue
        for line in previous.lines:
            row = by_line.get(line.line_no)
            if row is not None:
                row["remaining_qty"] = str(float(row["remaining_qty"]) + float(line.received_qty or 0))
    return rows


def _grn_lines_from_extraction(inv, content: bytes) -> List[Dict[str, Any]]:
    accepted = extract_accepted_quantities(content)
    items = inv.line_items or []
    use_accepted = bool(accepted) and len(accepted) == len(items)
    lines = []
    for idx, line in enumerate(items, start=1):
        lines.append(
            {
                "line_no": idx,
                "item_code": _item_code(line),
                "description": _line_description(line),
                "uom": getattr(line, "unit", None),
                "received_qty": accepted[idx - 1] if use_accepted else _money(line.quantity),
            }
        )
    return lines


def _grn_payload_from_lines(cls, lines: List[Dict[str, Any]], filename: str) -> Dict[str, Any]:
    return {
        "grn_number": cls.document_number or f"GRN-{filename.rsplit('.', 1)[0]}",
        "po_number": cls.po_reference,
        "receipt_date": cls.document_date,
        "currency": "INR",
        "lines": payload_lines(lines),
    }


def _reconcile_invoice_number(inv, cls, raw_text: str) -> None:
    """
    Prefer a document number that actually appears in the OCR text.

    The vision model sometimes returns a plausible-looking number that is not on
    the page ("6T/26-27/0891" for a printed "KFT/26-27/0891").
    """
    meta = getattr(inv, "metadata", None)
    if meta is None or cls.doc_type != INVOICE:
        return
    candidate = (cls.document_number or "").strip()
    current = (meta.invoice_number or "").strip()
    if not candidate:
        return
    if not current or current.upper() not in (raw_text or "").upper():
        meta.invoice_number = candidate


def _reconcile_extracted_number(extracted: Dict[str, Any], cls, raw_text: str) -> Dict[str, Any]:
    """Apply _reconcile_invoice_number to the JSON that canonicalisation reads."""
    meta = extracted.get("metadata") if isinstance(extracted, dict) else None
    if not isinstance(meta, dict):
        return extracted
    candidate = (cls.document_number or "").strip()
    current = (meta.get("invoice_number") or "").strip()
    if candidate and (not current or current.upper() not in (raw_text or "").upper()):
        meta["invoice_number"] = candidate
    return extracted


def _ai_vision_configured(api_key: Optional[str]) -> bool:
    return bool(api_key or os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))


async def _ai_grn_fallback(upload: StarletteUploadFile, content: bytes, api_key: Optional[str], db):
    """Ask the vision model for the table only; the caller re-runs the checks."""
    try:
        ai_inv = await _extract(upload, content, "ai", api_key, db)
    except Exception as exc:  # a failed fallback must not lose the reader's work
        print(f"[Ingest] GRN AI fallback skipped: {exc}")
        return None
    if not (getattr(ai_inv, "line_items", None) or []):
        return None
    return _grn_lines_from_extraction(ai_inv, content)


async def _extract(upload: StarletteUploadFile, content: bytes, mode: str, api_key: Optional[str], db):
    from backend.main import extract_invoice

    file_proxy = StarletteUploadFile(filename=upload.filename or "document", file=io.BytesIO(content))
    return await extract_invoice(
        file=file_proxy,
        pipeline_mode=mode,
        use_ai_vision=(mode == "ai"),
        api_key=api_key,
        db=db,
    )


async def ingest_upload(
    db,
    upload: StarletteUploadFile,
    extraction_method: str = "auto",
    api_key: Optional[str] = None,
    po_number: Optional[str] = None,
) -> Dict[str, Any]:
    """Processes one uploaded file and returns the UI summary for it."""
    filename = upload.filename or "document"
    content = await upload.read()
    result: Dict[str, Any] = {
        "file_name": filename,
        "doc_type": UNKNOWN,
        "doc_type_label": DOC_TYPE_LABELS[UNKNOWN],
        "type_confidence": 0.0,
        "action": "error",
        "message": "",
        "extraction": {},
        "classification": {},
        "match": None,
        "record": None,
        "preview": {},
        "ocr_attempts": [],
        "validation": None,
        "validation_passed": None,
        "review_required": False,
    }

    mode = MODE_MAP.get(extraction_method, "hybrid")
    text_ocr, text_attempt = await asyncio.to_thread(
        extract_document_text,
        content,
        filename,
        upload.content_type,
    )
    if text_attempt.get("reason") != "not_an_image":
        result["ocr_attempts"].append(text_attempt)
    text_classification = classify_document(text_ocr) if text_ocr else None

    try:
        inv = await _extract(upload, content, mode, api_key, db)
    except Exception as exc:
        result["ocr_attempts"].append(
            {
                "engine": "structured_extraction",
                "ok": False,
                "reason": "exception",
                "detail": str(exc),
            }
        )
        result["message"] = (
            "OCR engine returned no text. " if not _has_usable_text(text_ocr) else ""
        ) + f"Extraction failed: {exc}"
        return result

    extracted_raw_text = inv.raw_text or ""
    raw_text = text_ocr if _has_usable_text(text_ocr) else (extracted_raw_text or text_ocr)
    if inv.engine_used:
        result["ocr_attempts"].append(
            {
                "engine": inv.engine_used,
                "ok": _has_usable_text(extracted_raw_text),
                "text_characters": len(extracted_raw_text),
                "role": "structured_extraction",
            }
        )
    if _has_usable_text(text_ocr):
        inv.raw_text = text_ocr
    validation = inv.validation_summary or {}
    result["extraction"] = {
        "engine": inv.engine_used,
        "confidence": validation.get("overall_confidence"),
        "extraction_log_id": inv.extraction_log_id,
        "ai_fallback": "Gemini" in (inv.engine_used or "") or "Claude" in (inv.engine_used or ""),
        "line_items": len(inv.line_items or []),
    }

    cls = text_classification or classify_document(raw_text, inv)
    if cls.doc_type == UNKNOWN:
        cls = classify_document(raw_text, inv)
    if cls.needs_llm and raw_text:
        try:
            from .llm_assist import classify_document_type

            llm_type = classify_document_type(raw_text, api_key)
            if llm_type and llm_type != UNKNOWN:
                cls.doc_type = llm_type
                cls.confidence = max(cls.confidence, 0.6)
                cls.needs_llm = False
                cls.signals = list(cls.signals) + ["llm"]
        except Exception as exc:  # classification is best-effort
            print(f"[Ingest] LLM classification skipped: {exc}")

    _reconcile_invoice_number(inv, cls, raw_text)

    # Judge the extraction by arithmetic and format, not by OCR confidence.
    # This runs for every document type; only invoices are hard-blocked by it.
    doc_validation = None
    validation_payload: Dict[str, Any] = {}

    result["classification"] = cls.to_dict()
    result["doc_type"] = cls.doc_type
    result["doc_type_label"] = DOC_TYPE_LABELS.get(cls.doc_type, DOC_TYPE_LABELS[UNKNOWN])
    result["type_confidence"] = round(cls.confidence, 3)
    preview_vendor = cls.vendor_name
    if not preview_vendor and cls.doc_type == INVOICE:
        preview_vendor = getattr(getattr(inv, "seller", None), "name", None)
    result["preview"] = {
        "vendor": preview_vendor,
        "document_number": cls.document_number,
        "document_date": cls.document_date,
        "total": (
            _money(inv.summary.grand_total)
            if cls.doc_type == INVOICE and inv.summary
            else None
        ),
        "currency": (inv.metadata.currency if inv.metadata else None) or "INR",
    }

    # Persist the original document (DB stores only the storage key).
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    try:
        source_key = save_bytes(content, ext if len(ext) <= 5 else "bin", prefix="documents")
    except Exception:
        source_key = None
    if inv.extraction_log_id:
        log = db.query(ExtractionLog).filter(ExtractionLog.id == inv.extraction_log_id).first()
        if log is not None and source_key:
            log.source_file_url = source_key
            db.commit()

    def _gate(document_type: str, canonical=None, reference: Optional[str] = None):
        """Runs the gate for one document type and mirrors it onto the result."""
        nonlocal doc_validation, validation_payload
        doc_validation = validate_document(
            inv,
            document_type=document_type,
            po_reference=reference,
            canonical=canonical,
        )
        validation_payload = doc_validation.to_dict()
        result["validation"] = validation_payload
        result["validation_passed"] = doc_validation.passed
        # The gate is the authority on whether an extraction is usable, so its
        # verdict replaces the OCR-level flag on the audit record.
        log_row = (
            db.query(ExtractionLog).filter(ExtractionLog.id == inv.extraction_log_id).first()
            if getattr(inv, "extraction_log_id", None)
            else None
        )
        if log_row is not None:
            log_row.validation_passed = doc_validation.passed
            log_row.validation_errors = doc_validation.errors + doc_validation.warnings
            db.commit()
        return doc_validation

    try:
        log_row = (
            db.query(ExtractionLog).filter(ExtractionLog.id == inv.extraction_log_id).first()
            if inv.extraction_log_id
            else None
        )
        if log_row is not None:
            log_row.raw_ocr_text = raw_text
            log_row.document_ocr_attempts = result["ocr_attempts"]
            db.commit()

        if cls.doc_type == PURCHASE_ORDER:
            if not (inv.line_items or []):
                result["action"] = "error"
                result["message"] = "Recognised a purchase order but could not read its line items."
                return result
            _gate(PURCHASE_ORDER, reference=cls.document_number)
            # _local_line_codes misreads images (it expects a PDF) and returns
            # HSN numbers as item codes; images use the extracted fields only.
            codes = (
                []
                if is_image(filename, upload.content_type)
                else _local_line_codes(content, len(inv.line_items or []))
            )
            po = svc.import_purchase_order(db, _po_payload(inv, cls, raw_text, filename, codes), source="OCR")
            result["action"] = "purchase_order_imported"
            result["record"] = {"po_number": po.po_number, "lines": len(po.lines), "vendor": po.vendor.name if po.vendor else None}
            if po.vendor is not None:
                # The header label can be misread as the vendor name; the
                # resolved vendor master is authoritative.
                result["preview"]["vendor"] = po.vendor.name
            result["message"] = f"Purchase order {po.po_number} imported with {len(po.lines)} line(s)."
            if not doc_validation.passed:
                result["review_required"] = True
                result["message"] += (
                    " Check the numbers: " + ", ".join(e.replace("_", " ").lower() for e in doc_validation.errors) + "."
                )

        elif cls.doc_type == GRN:
            if not cls.po_reference:
                result["action"] = "error"
                result["message"] = "Recognised a goods receipt but could not find the PO reference on it."
                return result

            po_lines = _grn_po_lines(db, cls.po_reference, cls.document_number)
            lines: List[Dict[str, Any]] = []
            problems: List[str] = []

            is_pdf = filename.lower().endswith(".pdf") or (upload.content_type or "").lower() == "application/pdf"
            if is_image(filename, upload.content_type) or is_pdf:
                table = read_grn_table(content)
                result["ocr_attempts"].append(table.to_attempt())
                if table.ok:
                    mapped, mapping_problems = map_lines_to_po(table.lines, po_lines)
                    problems = mapping_problems + sanity_problems(mapped, po_lines)
                    lines = mapped
                else:
                    problems = [table.reason or "no lines found"]

                if problems and mode != "ai" and _ai_vision_configured(api_key):
                    ai_lines = await _ai_grn_fallback(upload, content, api_key, db)
                    if ai_lines:
                        ai_mapped, ai_mapping = map_lines_to_po(ai_lines, po_lines)
                        ai_problems = ai_mapping + sanity_problems(ai_mapped, po_lines)
                        result["ocr_attempts"].append(
                            {
                                "engine": "ai_vision",
                                "ok": True,
                                "role": "grn_table_fallback",
                                "lines": len(ai_lines),
                                "problems": ai_problems,
                            }
                        )
                        # Keep the reader's answer unless the model's is better.
                        if ai_mapped and len(ai_problems) < len(problems):
                            lines, problems = ai_mapped, ai_problems

            if not lines and (inv.line_items or []):
                extracted_lines = _grn_lines_from_extraction(inv, content)
                mapped, mapping_problems = map_lines_to_po(extracted_lines, po_lines)
                problems = mapping_problems + sanity_problems(mapped, po_lines)
                lines = mapped

            lines = [line for line in lines if line.get("received_qty") is not None]
            if not lines:
                result["action"] = "error"
                result["message"] = "Recognised a goods receipt but could not read its line items."
                return result

            if inv.line_items:
                _gate(GRN, reference=cls.po_reference)
            result["extraction"]["line_items"] = len(lines)
            grn = svc.import_grn(db, _grn_payload_from_lines(cls, lines, filename), source="OCR")
            result["action"] = "grn_imported"
            result["record"] = {
                "grn_number": grn.grn_number,
                "po_number": cls.po_reference,
                "lines": len(grn.lines),
                "line_items": [
                    {
                        "line_no": line.get("po_line_no") or line.get("line_no"),
                        "item_code": line.get("item_code"),
                        "description": line.get("description"),
                        "uom": line.get("uom"),
                        "received_qty": line.get("received_qty"),
                    }
                    for line in lines
                ],
            }
            result["message"] = f"Goods receipt {grn.grn_number or ''} recorded against {cls.po_reference}."
            if problems:
                result["review_required"] = True
                result["message"] += " Check the quantities: " + "; ".join(problems) + "."
            elif doc_validation is not None and not doc_validation.passed:
                result["review_required"] = True
                result["message"] += (
                    " Check the quantities: " + ", ".join(e.replace("_", " ").lower() for e in doc_validation.errors) + "."
                )

        elif cls.doc_type == INVOICE:
            if not inv.extraction_log_id:
                result["action"] = "error"
                result["message"] = "Invoice extracted but no extraction record was stored."
                return result
            log = db.query(ExtractionLog).filter(ExtractionLog.id == inv.extraction_log_id).first()
            if log is None:
                result["action"] = "error"
                result["message"] = "Invoice extraction record not found."
                return result
            if log.extracted_data:
                # The AI path does not always hand back raw_text on the model.
                text_for_codes = raw_text or (log.extracted_data.get("raw_text") or "")
                # deepcopy: a shallow copy shares the line list, so the original
                # JSON value would be mutated in place and SQLAlchemy would see
                # no change to persist.
                enriched = _enrich_line_codes(copy.deepcopy(log.extracted_data), text_for_codes)
                enriched = _reconcile_extracted_number(enriched, cls, text_for_codes)
                log.extracted_data = enriched
                flag_modified(log, "extracted_data")
                db.commit()
            reference = po_number or cls.po_reference or (inv.metadata.po_number if inv.metadata else None)
            # Repair obvious extraction artefacts (gross vs net line amounts,
            # per-component tax rates) before judging the arithmetic, so the gate
            # reports real inconsistency rather than OCR noise.
            harmonised = canonical_invoice_from_extraction(inv, po_number=reference)
            _gate(INVOICE, canonical=harmonised, reference=reference)
            doc = svc.canonicalize_extraction(
                db,
                log,
                po_number=reference,
                source_file_url=source_key,
                validation_report=validation_payload,
            )
            if doc_validation.passed:
                match = svc.run_match_for_invoice(db, doc.id)
            else:
                # Failed the gate: no arithmetic-verified data, so no match.
                match = svc.record_validation_failure(db, doc, validation_payload, po_number=doc.po_number)

            result["action"] = {
                ("MATCHED", DECISION_APPROVE): "invoice_matched",
                ("MATCHED", None): "invoice_review",
                ("EXCEPTION", None): "invoice_exception",
                ("REJECT", None): "invoice_rejected",
            }.get((match.status, match.decision), "invoice_exception")
            if match.status == "MATCHED" and match.decision != DECISION_APPROVE:
                result["action"] = "invoice_review"
            if match.status == "REJECT" or match.decision == "REJECT":
                result["action"] = "invoice_rejected"
            result["review_required"] = match.decision != DECISION_APPROVE
            result["match"] = {
                "match_result_id": match.id,
                "status": match.status,
                "reason_codes": match.reason_codes or [],
                "decision": match.decision,
                "decision_reasons": match.decision_reasons or [],
                "decision_explanation": match.decision_explanation,
                "validation_passed": match.validation_passed,
                "approval_status": match.approval_status,
                "approvals_required": match.approvals_required,
                "invoice_document_id": doc.id,
                "po_number": doc.po_number,
            }
            result["record"] = {"invoice_number": doc.invoice_number, "po_number": doc.po_number}
            reasons = ", ".join(c.replace("_", " ").lower() for c in (match.decision_reasons or match.reason_codes or []))
            result["message"] = f"Invoice {doc.invoice_number or ''}: {match.decision or match.status}" + (
                f" ({reasons})" if reasons else ""
            )
            if not doc_validation.passed:
                result["message"] = (
                    f"Invoice {doc.invoice_number or ''}: held at validation — "
                    + ", ".join(e.replace("_", " ").lower() for e in doc_validation.errors)
                    + ". Nothing reaches matching until the numbers reconcile."
                )
            if not doc.po_number and doc_validation.passed:
                result["message"] += " — no PO reference found, add one to match."

        else:
            result["action"] = "unclassified"
            if not _has_usable_text(raw_text):
                result["message"] = "OCR engine returned no usable text. Check OCR service availability and retry."
            else:
                result["message"] = (
                    "OCR returned text, but the document type could not be identified. "
                    "Check the detected text and classification signals before retrying."
                )
    except ValueError as exc:
        result["action"] = "error"
        result["message"] = str(exc)
    except Exception as exc:
        result["action"] = "error"
        result["message"] = f"{type(exc).__name__}: {exc}"

    return result


def summarise(results: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {
        "files": len(results),
        "purchase_orders": 0,
        "goods_receipts": 0,
        "invoices": 0,
        "matched": 0,
        "exceptions": 0,
        "review": 0,
        "rejected": 0,
        "unclassified": 0,
        "validation_failed": 0,
        "errors": 0,
    }
    for result in results:
        action = result.get("action")
        if action == "purchase_order_imported":
            summary["purchase_orders"] += 1
        elif action == "grn_imported":
            summary["goods_receipts"] += 1
        elif action in ("invoice_matched", "invoice_review", "invoice_exception", "invoice_rejected"):
            summary["invoices"] += 1
            if action == "invoice_matched":
                summary["matched"] += 1
            elif action == "invoice_review":
                summary["review"] += 1
            elif action == "invoice_exception":
                summary["exceptions"] += 1
            else:
                summary["rejected"] += 1
        elif action == "unclassified":
            summary["unclassified"] += 1
        else:
            summary["errors"] += 1
        if result.get("validation_passed") is False:
            summary["validation_failed"] += 1
    return summary
