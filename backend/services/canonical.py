"""
Canonical document contract shared by OCR output, ERP imports and the match engine.

OCR output is mapped into these models and validated before anything downstream
touches it. Every field carries a confidence and a source so the exception UI can
show *why* a value is trusted (and where it came from).

PO and GRN documents normally arrive from the ERP/CSV import, not OCR. The same
models are used either way; only `source` changes.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..schemas import InvoiceData
from .validator import to_decimal


SOURCE_OCR = "OCR"
SOURCE_ERP = "ERP"
SOURCE_MANUAL = "MANUAL"


class CanonicalLine(BaseModel):
    """One line of an invoice, a PO or a GRN."""

    line_no: int
    item_code: Optional[str] = None
    description: Optional[str] = None
    quantity: float = 0.0
    unit: Optional[str] = None
    rate: float = 0.0
    tax_rate: float = 0.0
    tax_amount: float = 0.0
    amount: float = 0.0
    hsn_sac: Optional[str] = None
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    field_source: Dict[str, str] = Field(default_factory=dict)

    def as_money(self) -> Dict[str, Decimal]:
        return {
            "quantity": to_decimal(self.quantity),
            "rate": to_decimal(self.rate),
            "tax_rate": to_decimal(self.tax_rate),
            "tax_amount": to_decimal(self.tax_amount),
            "amount": to_decimal(self.amount),
        }


class CanonicalTaxComponent(BaseModel):
    """A tax row such as CGST 9% or IGST 18%."""

    name: str
    rate: float = 0.0
    taxable_value: float = 0.0
    amount: float = 0.0


class CanonicalInvoice(BaseModel):
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    po_number: Optional[str] = None
    currency: str = "INR"
    lines: List[CanonicalLine] = Field(default_factory=list)
    subtotal: float = 0.0
    cgst_total: float = 0.0
    sgst_total: float = 0.0
    igst_total: float = 0.0
    total_tax: float = 0.0
    grand_total: float = 0.0
    tax_breakdown: List[CanonicalTaxComponent] = Field(default_factory=list)
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    field_source: Dict[str, str] = Field(default_factory=dict)


class CanonicalPurchaseOrder(BaseModel):
    po_number: str
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    po_date: Optional[str] = None
    currency: str = "INR"
    status: str = "OPEN"
    category: Optional[str] = None
    lines: List[CanonicalLine] = Field(default_factory=list)
    subtotal: float = 0.0
    total_tax: float = 0.0
    grand_total: float = 0.0
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    field_source: Dict[str, str] = Field(default_factory=dict)


class CanonicalGRNLine(BaseModel):
    line_no: int
    po_line_no: Optional[int] = None
    item_code: Optional[str] = None
    description: Optional[str] = None
    received_quantity: float = 0.0
    unit: Optional[str] = None
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    field_source: Dict[str, str] = Field(default_factory=dict)


class CanonicalGRN(BaseModel):
    grn_number: Optional[str] = None
    po_number: str
    receipt_date: Optional[str] = None
    currency: str = "INR"
    lines: List[CanonicalGRNLine] = Field(default_factory=list)
    field_confidence: Dict[str, float] = Field(default_factory=dict)
    field_source: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------
_CANONICAL_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d/%b/%Y",
    "%d.%b.%Y",
    "%d %b, %Y",
    "%d %B, %Y",
)


def parse_canonical_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _CANONICAL_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # last resort: pull the first numeric date out of a noisy string
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text)
    if m:
        d, mo, y = m.groups()
        y = y if len(y) == 4 else f"20{y}"
        try:
            return date(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None


def _money(value: Any) -> Decimal:
    return to_decimal(value)


def validate_canonical_invoice(invoice: CanonicalInvoice, tolerance: Decimal = Decimal("1.00")) -> List[str]:
    """
    Returns contract violations. An empty list means the OCR output is valid
    against the canonical schema and internally consistent.
    """
    errors: List[str] = []

    if not invoice.invoice_number:
        errors.append("MISSING_INVOICE_NUMBER")
    if not (invoice.vendor_gstin or invoice.vendor_name):
        errors.append("MISSING_VENDOR_IDENTITY")
    if not invoice.invoice_date:
        errors.append("MISSING_INVOICE_DATE")
    elif parse_canonical_date(invoice.invoice_date) is None:
        errors.append("UNPARSEABLE_INVOICE_DATE")
    if not invoice.lines:
        errors.append("MISSING_LINES")

    for idx, line in enumerate(invoice.lines, start=1):
        qty = _money(line.quantity)
        rate = _money(line.rate)
        amount = _money(line.amount)
        if qty <= 0:
            errors.append(f"LINE_{idx}_NON_POSITIVE_QTY")
        if rate < 0:
            errors.append(f"LINE_{idx}_NEGATIVE_RATE")
        if amount <= 0:
            errors.append(f"LINE_{idx}_NON_POSITIVE_AMOUNT")
        elif qty > 0 and rate > 0:
            expected_net = (qty * rate).quantize(Decimal("0.01"))
            gross = (expected_net * (Decimal("1") + _money(line.tax_rate) / Decimal("100"))).quantize(Decimal("0.01"))
            if abs(amount - expected_net) > tolerance and abs(amount - gross) > tolerance:
                errors.append(f"LINE_{idx}_AMOUNT_INCONSISTENT")

    if invoice.lines:
        lines_total = sum((_money(l.amount) for l in invoice.lines), Decimal("0"))
        subtotal = _money(invoice.subtotal)
        grand_total = _money(invoice.grand_total)
        total_tax = _money(invoice.total_tax)
        if subtotal > 0 and abs(lines_total - subtotal) > tolerance:
            errors.append("SUBTOTAL_MISMATCH")
        if grand_total > 0:
            if (
                abs(subtotal + total_tax - grand_total) > tolerance
                and abs(lines_total + total_tax - grand_total) > tolerance
            ):
                errors.append("GRAND_TOTAL_MISMATCH")
        else:
            errors.append("MISSING_GRAND_TOTAL")

    return errors


def field_confidence_block(invoice: CanonicalInvoice) -> Dict[str, Any]:
    """Compact view used by the exception UI and audit log."""
    return {
        "invoice_number": invoice.field_confidence.get("invoice_number"),
        "invoice_date": invoice.field_confidence.get("invoice_date"),
        "vendor": invoice.field_confidence.get("vendor_name"),
        "po_number": invoice.field_confidence.get("po_number"),
        "grand_total": invoice.field_confidence.get("grand_total"),
        "lines": [l.field_confidence for l in invoice.lines],
    }


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
def canonical_invoice_from_extraction(invoice: InvoiceData, po_number: Optional[str] = None) -> CanonicalInvoice:
    """
    Maps the OCR `InvoiceData` model onto the canonical contract.
    Confidence comes from the validator's per-field scores; source is OCR.
    """
    from .validator import validate_invoice

    validation = validate_invoice(invoice)
    field_conf = {k: v for k, v in (validation.field_confidence or {}).items() if isinstance(v, (int, float))}

    meta = invoice.metadata
    summary = invoice.summary
    lines: List[CanonicalLine] = []
    for idx, item in enumerate(invoice.line_items or [], start=1):
        item_code = getattr(item, "item_code", None) or getattr(item, "part_number", None) or item.hsn_sac
        line_conf = {
            "description": 0.95 if (item.description or item.item_name) else 0.2,
            "quantity": 0.95 if (item.quantity or 0) > 0 else 0.2,
            "rate": 0.95 if (item.unit_price or 0) > 0 else 0.2,
            "amount": 0.95 if (item.total_amount or 0) > 0 else 0.2,
        }
        lines.append(
            CanonicalLine(
                line_no=idx,
                item_code=item_code,
                description=item.description or item.item_name,
                quantity=float(item.quantity or 0.0),
                unit=item.unit,
                rate=float(item.unit_price or 0.0),
                tax_rate=float(item.tax_rate or 0.0),
                tax_amount=float(item.tax_amount or 0.0),
                amount=float(item.total_amount or 0.0),
                hsn_sac=item.hsn_sac,
                field_confidence=line_conf,
                field_source={k: SOURCE_OCR for k in line_conf},
            )
        )

    # Some layouts print the per-component rate (CGST 9% + SGST 9%) while the PO
    # and our checks work with the combined rate (18%). If every line rate is
    # exactly half the effective rate implied by the stated tax, combine them.
    if lines and summary.total_gst and (summary.subtotal or 0) > 0:
        effective_rate = float(summary.total_gst) / float(summary.subtotal) * 100.0
        positive_rates = [l.tax_rate for l in lines if l.amount and l.tax_rate > 0]
        if positive_rates and not any(l.tax_rate == 0 for l in lines if l.amount):
            if all(abs(rate * 2 - effective_rate) <= 0.75 for rate in positive_rates):
                for line in lines:
                    line.tax_rate = round(line.tax_rate * 2, 2)

    tax_components: List[CanonicalTaxComponent] = []
    for name, value in (
        ("CGST", summary.cgst_total),
        ("SGST", summary.sgst_total),
        ("IGST", summary.igst_total),
    ):
        if (value or 0) > 0:
            tax_components.append(
                CanonicalTaxComponent(
                    name=name,
                    rate=float(value),
                    taxable_value=float(summary.subtotal or 0.0),
                    amount=float(value),
                )
            )

    canonical = CanonicalInvoice(
        vendor_name=invoice.seller.name if invoice.seller else None,
        vendor_gstin=invoice.seller.gstin if invoice.seller else None,
        invoice_number=meta.invoice_number if meta else None,
        invoice_date=meta.invoice_date if meta else None,
        po_number=po_number or (meta.po_number if meta else None),
        currency=(meta.currency if meta and meta.currency else "INR"),
        lines=lines,
        subtotal=float(summary.subtotal or 0.0),
        cgst_total=float(summary.cgst_total or 0.0),
        sgst_total=float(summary.sgst_total or 0.0),
        igst_total=float(summary.igst_total or 0.0),
        total_tax=float(summary.total_gst or 0.0),
        grand_total=float(summary.grand_total or 0.0),
        tax_breakdown=tax_components,
        field_confidence={
            "invoice_number": field_conf.get("invoice_number", 0.9),
            "invoice_date": 0.95 if (meta and meta.invoice_date) else 0.2,
            "vendor_name": field_conf.get("seller_name", 0.9),
            "vendor_gstin": field_conf.get("seller_gstin", 0.8),
            "po_number": 0.95 if (po_number or (meta and meta.po_number)) else 0.2,
            "grand_total": field_conf.get("grand_total", 0.9),
            "subtotal": field_conf.get("line_items_sum", 0.9),
        },
        field_source={
            "invoice_number": SOURCE_OCR,
            "invoice_date": SOURCE_OCR,
            "vendor_name": SOURCE_OCR,
            "vendor_gstin": SOURCE_OCR,
            "po_number": SOURCE_OCR,
            "grand_total": SOURCE_OCR,
            "subtotal": SOURCE_OCR,
        },
    )
    return harmonise_extracted_amounts(canonical)


def harmonise_extracted_amounts(invoice: CanonicalInvoice) -> CanonicalInvoice:
    """
    Repairs the arithmetic OCR/AI extraction frequently gets wrong.

    Line quantity and rate are the fields the match actually compares, so when
    a line amount disagrees with quantity x rate (gross-of-tax, a dropped or
    added zero, a merged cell) the amount is rebuilt from them. The document
    totals are then recomputed when they no longer reconcile with the lines.
    """
    lines = invoice.lines or []
    if not lines:
        return invoice

    for line in lines:
        net = None
        if (line.quantity or 0) > 0 and (line.rate or 0) > 0:
            net = round(float(line.quantity) * float(line.rate), 2)
        if net:
            if not line.amount or abs(float(line.amount) - net) > max(1.0, net * 0.01):
                line.amount = net
            if (line.tax_rate or 0) > 0:
                expected_tax = round(net * float(line.tax_rate) / 100.0, 2)
                if not line.tax_amount or abs(float(line.tax_amount) - expected_tax) > max(1.0, expected_tax * 0.01):
                    line.tax_amount = expected_tax
        elif line.tax_rate and line.amount and not line.tax_amount:
            line.tax_amount = round(float(line.amount) * float(line.tax_rate) / 100.0, 2)

    lines_total = round(sum(float(l.amount or 0) for l in lines), 2)
    if not invoice.subtotal or abs(float(invoice.subtotal) - lines_total) > max(1.0, lines_total * 0.01):
        invoice.subtotal = lines_total

    taxed_lines = [l for l in lines if (l.tax_rate or 0) > 0 and (l.amount or 0) > 0]
    lines_tax = round(sum(float(l.tax_amount or 0) for l in lines), 2)
    if taxed_lines and lines_tax:
        stated_tax = float(invoice.total_tax or 0)
        if not stated_tax or abs(stated_tax - lines_tax) > max(1.0, lines_tax * 0.01):
            invoice.total_tax = lines_tax

    computed_grand = round(lines_total + float(invoice.total_tax or 0), 2)
    stated_grand = float(invoice.grand_total or 0)
    if not stated_grand or abs(stated_grand - computed_grand) > max(1.0, computed_grand * 0.01):
        invoice.grand_total = computed_grand

    return invoice


def _to_float(value: Any) -> float:
    try:
        return float(to_decimal(value))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


def canonical_invoice_from_dict(payload: Dict[str, Any], source: str = SOURCE_ERP) -> CanonicalInvoice:
    """
    Maps an ERP / ground-truth invoice payload onto the canonical contract.
    Accepts both our native field names and common aliases (number, po_ref,
    qty, uom, gst_rate, taxable_value, total, tax_breakup, vendor object).
    """
    vendor = payload.get("vendor") or {}
    vendor_name = payload.get("vendor_name") or (vendor.get("name") if isinstance(vendor, dict) else vendor)
    vendor_gstin = payload.get("vendor_gstin") or (vendor.get("gstin") if isinstance(vendor, dict) else None)

    raw_lines = payload.get("lines") or []
    lines: List[CanonicalLine] = []
    computed_subtotal = Decimal("0")
    for idx, raw in enumerate(raw_lines, start=1):
        qty = _to_float(raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty", 0))
        rate = _to_float(raw.get("rate") or raw.get("unit_price"))
        amount = _to_float(raw.get("amount") or raw.get("total_amount") or raw.get("taxable_value"))
        if amount == 0 and qty and rate:
            amount = float((Decimal(str(qty)) * Decimal(str(rate))).quantize(Decimal("0.01")))
        computed_subtotal += Decimal(str(amount))
        lines.append(
            CanonicalLine(
                line_no=int(raw.get("line_no") or idx),
                item_code=raw.get("item_code") or raw.get("part_number"),
                description=raw.get("description") or raw.get("item_name"),
                quantity=qty,
                unit=raw.get("unit") or raw.get("uom"),
                rate=rate,
                tax_rate=_to_float(raw.get("tax_rate") if raw.get("tax_rate") is not None else raw.get("gst_rate")),
                tax_amount=_to_float(raw.get("tax_amount")),
                amount=amount,
                hsn_sac=raw.get("hsn_sac") or raw.get("hsn"),
                field_confidence={"quantity": 1.0, "rate": 1.0, "amount": 1.0},
                field_source={"quantity": source, "rate": source, "amount": source},
            )
        )

    # Tax breakup may be a list of {type, rate, amount} (CGST/SGST/IGST rows).
    cgst = sgst = igst = Decimal("0")
    for component in payload.get("tax_breakup") or payload.get("tax_breakdown") or []:
        kind = str(component.get("type") or component.get("name") or "").upper()
        amount = _to_float(component.get("amount"))
        if "CGST" in kind:
            cgst += Decimal(str(amount))
        elif "SGST" in kind:
            sgst += Decimal(str(amount))
        elif "IGST" in kind:
            igst += Decimal(str(amount))

    subtotal = _to_float(payload.get("subtotal"))
    if subtotal == 0:
        subtotal = float(computed_subtotal)
    total_tax = _to_float(payload.get("total_tax"))
    breakup_total = float(cgst + sgst + igst)
    if total_tax == 0:
        total_tax = breakup_total
    grand_total = _to_float(
        payload.get("grand_total")
        if payload.get("grand_total") is not None
        else payload.get("total", 0)
    )
    if grand_total == 0:
        grand_total = float(Decimal(str(subtotal)) + Decimal(str(total_tax)))

    return CanonicalInvoice(
        vendor_name=vendor_name,
        vendor_gstin=vendor_gstin,
        invoice_number=payload.get("invoice_number") or payload.get("number"),
        invoice_date=payload.get("invoice_date") or payload.get("date"),
        po_number=payload.get("po_number") or payload.get("po_ref"),
        currency=(payload.get("currency") or "INR").upper(),
        lines=lines,
        subtotal=subtotal,
        cgst_total=_to_float(payload.get("cgst_total")) or float(cgst),
        sgst_total=_to_float(payload.get("sgst_total")) or float(sgst),
        igst_total=_to_float(payload.get("igst_total")) or float(igst),
        total_tax=total_tax,
        grand_total=grand_total,
        field_confidence={k: 1.0 for k in ("invoice_number", "invoice_date", "vendor_name", "grand_total")},
        field_source={k: source for k in ("invoice_number", "invoice_date", "vendor_name", "grand_total")},
    )


def canonical_po_from_dict(payload: Dict[str, Any], source: str = SOURCE_ERP) -> CanonicalPurchaseOrder:
    lines: List[CanonicalLine] = []
    for idx, raw in enumerate(payload.get("lines") or [], start=1):
        lines.append(
            CanonicalLine(
                line_no=int(raw.get("line_no") or idx),
                item_code=raw.get("item_code") or raw.get("part_number") or raw.get("hsn_sac"),
                description=raw.get("description") or raw.get("item_name"),
                quantity=_to_float(
                    raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty", raw.get("ordered_qty"))
                ),
                unit=raw.get("unit") or raw.get("uom"),
                rate=_to_float(raw.get("rate") or raw.get("unit_price")),
                tax_rate=_to_float(raw.get("tax_rate") if raw.get("tax_rate") is not None else raw.get("gst_rate")),
                tax_amount=_to_float(raw.get("tax_amount")),
                amount=_to_float(raw.get("amount") or raw.get("total_amount") or raw.get("taxable_value")),
                hsn_sac=raw.get("hsn_sac") or raw.get("hsn"),
                field_confidence={k: 1.0 for k in ("quantity", "rate", "amount")},
                field_source={k: source for k in ("quantity", "rate", "amount")},
            )
        )

    vendor = payload.get("vendor") or {}
    vendor_name = payload.get("vendor_name") or (vendor.get("name") if isinstance(vendor, dict) else vendor)
    vendor_gstin = payload.get("vendor_gstin") or (vendor.get("gstin") if isinstance(vendor, dict) else None)

    breakup_total = sum(
        (_to_float(c.get("amount")) for c in payload.get("tax_breakup") or payload.get("tax_breakdown") or []),
        0.0,
    )
    total_tax = _to_float(payload.get("total_tax")) or breakup_total
    subtotal = _to_float(payload.get("subtotal"))
    grand_total = _to_float(
        payload.get("grand_total") if payload.get("grand_total") is not None else payload.get("total", 0)
    )

    return CanonicalPurchaseOrder(
        po_number=str(payload.get("po_number") or payload.get("po") or payload.get("number") or "").strip(),
        vendor_name=vendor_name,
        vendor_gstin=vendor_gstin,
        po_date=payload.get("po_date") or payload.get("date"),
        currency=(payload.get("currency") or "INR").upper(),
        status=(payload.get("status") or "OPEN").upper(),
        category=payload.get("category"),
        lines=lines,
        subtotal=subtotal,
        total_tax=total_tax,
        grand_total=grand_total,
        field_confidence={"po_number": 1.0, "vendor_name": 1.0, "currency": 1.0},
        field_source={"po_number": source, "vendor_name": source, "currency": source},
    )


def _grn_accepted_qty(raw: Dict[str, Any]) -> float:
    """
    The billable GRN quantity is the *accepted* quantity: goods rejected at the
    gate must never become payable. Falls back through received/quantity.
    """
    for key in ("accepted_qty", "received_qty", "received_quantity", "quantity"):
        if raw.get(key) is not None:
            return _to_float(raw.get(key))
    return 0.0


def canonical_grn_from_dict(payload: Dict[str, Any], source: str = SOURCE_ERP) -> CanonicalGRN:
    lines: List[CanonicalGRNLine] = []
    for idx, raw in enumerate(payload.get("lines") or [], start=1):
        lines.append(
            CanonicalGRNLine(
                line_no=int(raw.get("line_no") or idx),
                po_line_no=(int(raw["po_line_no"]) if raw.get("po_line_no") else None),
                item_code=raw.get("item_code") or raw.get("part_number"),
                description=raw.get("description") or raw.get("item_name"),
                received_quantity=_grn_accepted_qty(raw),
                unit=raw.get("unit") or raw.get("uom"),
                field_confidence={"received_quantity": 1.0},
                field_source={"received_quantity": source},
            )
        )

    return CanonicalGRN(
        grn_number=payload.get("grn_number") or payload.get("grn"),
        po_number=str(payload.get("po_number") or payload.get("po") or payload.get("po_ref") or "").strip(),
        receipt_date=payload.get("receipt_date") or payload.get("date"),
        currency=(payload.get("currency") or "INR").upper(),
        lines=lines,
        field_confidence={"po_number": 1.0, "receipt_date": 1.0},
        field_source={"po_number": source, "receipt_date": source},
    )
