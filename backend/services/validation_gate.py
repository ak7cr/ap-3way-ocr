"""
Validation gate: arithmetic and format checks that decide whether an extraction
is trustworthy enough to reach the match engine.

OCR/AI confidence is a poor accuracy signal — a layout can be read with high
confidence and still be arithmetically wrong, and arithmetic errors are exactly
what turn into wrong payments. Everything here is cheap, deterministic and
independent of which extraction path produced the document.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..config import VALIDATION
from ..schemas import InvoiceData
from .validator import GSTIN_RE, normalize_gstin, to_decimal

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# GSTIN check-digit alphabet: 0-9 then A-Z, each with a value in base 36.
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PO_NUMBER_RE = re.compile(r"^[A-Za-z]{1,10}(?:[\-/][A-Za-z0-9]{1,12}){1,4}$")
_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y",
    "%d.%m.%y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y",
)


@dataclass
class Check:
    """One validation check and its outcome."""

    code: str
    status: str
    message: str
    severity: str = "ERROR"          # ERROR blocks matching, WARNING does not
    expected: Optional[str] = None
    actual: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "code": self.code,
            "status": self.status,
            "severity": self.severity,
            "message": self.message,
        }
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        return payload


@dataclass
class DocValidation:
    """The gate's verdict for one extracted document."""

    passed: bool = False
    checks: List[Check] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)     # block matching
    warnings: List[str] = field(default_factory=list)
    gstin: Dict[str, Any] = field(default_factory=dict)

    def add(self, check: Check) -> None:
        self.checks.append(check)
        if check.status == FAIL:
            self.errors.append(check.code)
        elif check.status == WARN:
            self.warnings.append(check.code)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "gstin": self.gstin,
            "checks": [c.to_dict() for c in self.checks],
        }

    def summary(self) -> str:
        if self.passed:
            return "validated" if not self.warnings else f"validated with {len(self.warnings)} warning(s)"
        return "failed: " + ", ".join(self.errors)


# ---------------------------------------------------------------------------
# GSTIN checksum
# ---------------------------------------------------------------------------
def gstin_check_digit(gstin: str) -> Optional[str]:
    """
    Computes the 15th (check) character of a GSTIN using the standard base-36
    mod-36 algorithm used by the GST portal.
    """
    value = normalize_gstin(gstin)
    if len(value) != 15 or any(ch not in _GSTIN_CHARS for ch in value):
        return None
    total = 0
    for index, char in enumerate(value[:14]):
        code = _GSTIN_CHARS.index(char)
        factor = 1 if index % 2 == 0 else 2
        product = code * factor
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin: Optional[str]) -> bool:
    """Format AND checksum. Returns False for empty values."""
    value = normalize_gstin(gstin)
    if not value or not GSTIN_RE.fullmatch(value):
        return False
    return gstin_check_digit(value) == value[14]


def _gstin_state(gstin: str) -> str:
    return normalize_gstin(gstin)[:2]


# ---------------------------------------------------------------------------
# Format checks
# ---------------------------------------------------------------------------
def parse_document_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def po_number_is_valid(value: Optional[str]) -> bool:
    """True when the reference looks like a real PO number rather than a stray word."""
    text = (value or "").strip()
    if len(text) < 4 or len(text) > 60:
        return False
    if not re.search(r"\d", text):
        return False
    return bool(_PO_NUMBER_RE.match(text.replace(" ", "")))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def _line_view(line, canonical_line: Any = None) -> Dict[str, Decimal]:
    """Normalises one line from either an InvoiceData item or a canonical line."""
    def pick(*names):
        for name in names:
            source = canonical_line if canonical_line is not None else line
            value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
            if value not in (None, ""):
                return to_decimal(value)
            value = getattr(line, name, None) if not isinstance(line, dict) else line.get(name)
            if value not in (None, ""):
                return to_decimal(value)
        return Decimal("0.00")

    return {
        "quantity": pick("quantity"),
        "rate": pick("rate", "unit_price"),
        "amount": pick("amount", "total_amount"),
        "tax_rate": pick("tax_rate"),
        "tax_amount": pick("tax_amount"),
    }


def _arithmetic_checks(
    result: DocValidation,
    lines: List[Dict[str, Decimal]],
    subtotal: Decimal,
    total_tax: Decimal,
    grand_total: Decimal,
    cgst: Decimal,
    sgst: Decimal,
    igst: Decimal,
    policy,
    require_totals: bool = True,
) -> None:
    """Line-sum, tax-recompute, breakdown and grand-total reconciliation."""
    # A PO/GRN is used for its lines, not its printed totals: a misread header is
    # worth flagging, but it must not block an otherwise usable goods document.
    def header(status: str) -> str:
        return status if require_totals else WARN

    printed_sum = sum((l["amount"] for l in lines), Decimal("0.00"))
    net_sum = sum(
        ((l["quantity"] * l["rate"]).quantize(Decimal("0.01")) if l["quantity"] > 0 and l["rate"] > 0 else l["amount"])
        for l in lines
    )

    # Which basis do the printed line amounts use? Indian invoices print either
    # the taxable (net) or the tax-inclusive (gross) line amount.
    basis = None
    if subtotal > 0:
        if abs(printed_sum - subtotal) <= policy.line_sum_abs:
            basis = "printed"
        elif abs(net_sum - subtotal) <= policy.line_sum_abs:
            basis = "net"

    if subtotal > 0:
        if basis:
            basis_value = printed_sum if basis == "printed" else net_sum
            note = "" if basis == "printed" else " (printed line amounts are gross of tax)"
            result.add(
                Check(
                    "LINE_SUM_MATCHES_TAXABLE",
                    PASS,
                    f"Line items total {basis_value} and match the taxable value {subtotal}{note}.",
                    expected=str(subtotal),
                    actual=str(basis_value),
                )
            )
        else:
            result.add(
                Check(
                    "LINE_SUM_MISMATCH",
                    header(FAIL),
                    f"Line items sum to {printed_sum} but the taxable total says {subtotal}.",
                    expected=str(subtotal),
                    actual=str(printed_sum),
                )
            )
    elif require_totals:
        result.add(Check("MISSING_TAXABLE_TOTAL", WARN, "Taxable value (subtotal) is missing.", severity="WARNING"))

    if grand_total <= 0 and require_totals:
        result.add(Check("MISSING_GRAND_TOTAL", FAIL, "Grand total is missing or zero."))

    # ---- tax recompute ----------------------------------------------------
    base = printed_sum if basis in (None, "printed") else net_sum
    explicit_tax = sum((l["tax_amount"] for l in lines), Decimal("0.00"))
    rate_tax = sum(
        ((l["amount"] if basis != "net" else (l["quantity"] * l["rate"]).quantize(Decimal("0.01"))) * l["tax_rate"] / Decimal("100")).quantize(Decimal("0.01"))
        for l in lines
        if l["tax_rate"] > 0
    )

    candidates: List[tuple] = []
    if explicit_tax > 0:
        candidates.append(("line tax amounts", explicit_tax))
    if rate_tax > 0:
        candidates.append(("line tax rates", rate_tax))
        # Many Indian invoices print the per-component rate (CGST 9% + SGST 9%).
        # If the stated tax is exactly the sum of both components, that is what
        # happened: the extracted rate is one half of the effective rate.
        if any(l["tax_rate"] > 0 for l in lines):
            candidates.append(("per-component tax rates", (rate_tax * 2).quantize(Decimal("0.01"))))

    if total_tax > 0:
        matched = next((name for name, value in candidates if abs(value - total_tax) <= policy.tax_recompute_abs), None)
        if matched:
            result.add(Check("TAX_RECOMPUTES", PASS, f"Tax recomputes to {total_tax} ({matched})."))
        elif candidates:
            best_name, best_value = min(candidates, key=lambda c: abs(c[1] - total_tax))
            result.add(
                Check(
                    "TAX_RECOMPUTE_MISMATCH",
                    header(FAIL),
                    f"Recomputed tax from {best_name} is {best_value} but the document states {total_tax}.",
                    expected=str(best_value),
                    actual=str(total_tax),
                )
            )
        else:
            result.add(Check("TAX_NOT_RECOMPUTABLE", WARN,
                             "Neither line tax amounts nor line tax rates were read, so tax cannot be recomputed.",
                             severity="WARNING"))
    elif lines and require_totals:
        result.add(Check("MISSING_TAX_TOTAL", WARN, "No tax total was read.", severity="WARNING"))

    # ---- tax breakdown ----------------------------------------------------
    components = cgst + sgst + igst
    if components > 0 and total_tax > 0:
        if abs(components - total_tax) > policy.tax_recompute_abs:
            result.add(
                Check(
                    "TAX_BREAKDOWN_MISMATCH",
                    header(FAIL),
                    f"CGST {cgst} + SGST {sgst} + IGST {igst} = {components}, but total tax is {total_tax}.",
                    expected=str(total_tax),
                    actual=str(components),
                )
            )
        elif igst > 0 and (cgst > 0 or sgst > 0):
            result.add(Check("TAX_COMPONENT_CONFLICT", WARN,
                             "Both IGST and CGST/SGST appear on the same invoice (inter-state vs intra-state).",
                             severity="WARNING"))
        else:
            result.add(Check("TAX_BREAKDOWN_MATCHES", PASS, f"Tax components add up to {components}."))

    # ---- grand total ------------------------------------------------------
    if subtotal > 0 and grand_total > 0:
        expected_total = (subtotal + total_tax).quantize(Decimal("0.01"))
        delta = abs(expected_total - grand_total)
        if delta <= policy.grand_total_abs:
            result.add(Check("GRAND_TOTAL_MATCHES", PASS, f"Taxable + tax equals the grand total {grand_total}."))
        elif delta > max(policy.grand_total_abs * 5, expected_total * Decimal("0.005")):
            result.add(
                Check(
                    "GRAND_TOTAL_MISMATCH",
                    header(FAIL),
                    f"Taxable {subtotal} + tax {total_tax} = {expected_total}, but the grand total is {grand_total}.",
                    expected=str(expected_total),
                    actual=str(grand_total),
                )
            )
        else:
            result.add(
                Check(
                    "GRAND_TOTAL_ROUNDING",
                    WARN,
                    f"Grand total {grand_total} differs from {expected_total} by a rounding amount.",
                    severity="WARNING",
                    expected=str(expected_total),
                    actual=str(grand_total),
                )
            )

    # ---- implied rate sanity ---------------------------------------------
    if subtotal > 0 and total_tax > 0:
        implied_rate = (total_tax / subtotal * Decimal("100")).quantize(Decimal("0.01"))
        if implied_rate > policy.max_tax_rate_pct + Decimal("2"):
            result.add(
                Check(
                    "IMPLIED_TAX_RATE_IMPLAUSIBLE",
                    header(FAIL),
                    f"Tax is {implied_rate}% of the taxable value, above the {policy.max_tax_rate_pct}% GST ceiling.",
                    actual=f"{implied_rate}%",
                )
            )

    # ---- per-line quantity x rate ----------------------------------------
    bad_rows = []
    for index, line in enumerate(lines, start=1):
        qty, rate, amount, rate_pct = line["quantity"], line["rate"], line["amount"], line["tax_rate"]
        if qty <= 0 or rate <= 0 or amount <= 0:
            continue
        net = (qty * rate).quantize(Decimal("0.01"))
        if abs(amount - net) <= policy.line_sum_abs:
            continue
        if abs(amount - (net * (Decimal("1") + rate_pct / Decimal("100"))).quantize(Decimal("0.01"))) <= policy.line_sum_abs:
            continue
        # tax-inclusive at twice the stated (per-component) rate
        if abs(amount - (net * (Decimal("1") + rate_pct * 2 / Decimal("100"))).quantize(Decimal("0.01"))) <= policy.line_sum_abs:
            continue
        bad_rows.append(index)
    if bad_rows:
        result.add(Check("LINE_AMOUNT_INCONSISTENT", WARN,
                         f"Row(s) {bad_rows}: quantity x rate does not explain the line amount.",
                         severity="WARNING", actual=",".join(str(r) for r in bad_rows)))
    elif lines:
        result.add(Check("LINE_AMOUNTS_CONSISTENT", PASS, "Every line amount agrees with quantity x rate."))


def validate_document(
    invoice: Optional[InvoiceData],
    policy=None,
    document_type: str = "INVOICE",
    po_reference: Optional[str] = None,
    canonical: Any = None,
) -> DocValidation:
    """
    Runs the checks that decide whether an extraction can be trusted.

    `document_type` matters: a GRN has no invoice number or grand total, so the
    invoice-only checks would report failures that mean nothing. Pass `canonical`
    (the harmonised view) to judge repaired arithmetic rather than raw OCR noise.
    """
    policy = policy or VALIDATION
    doc_type = (document_type or "INVOICE").upper()
    result = DocValidation()

    if invoice is None:
        result.add(Check("NO_EXTRACTION", FAIL, "No extraction result was produced."))
        return result

    is_invoice = doc_type == "INVOICE"
    is_goods_doc = doc_type in {"GRN", "PURCHASE_ORDER"}

    items = invoice.line_items or []
    meta = invoice.metadata

    # ---- structure -------------------------------------------------------
    if not items:
        result.add(Check("NO_LINE_ITEMS", FAIL, "No line items were extracted, so the document cannot be verified."))
    else:
        result.add(Check("LINE_ITEMS_PRESENT", PASS, f"{len(items)} line item(s) read."))

    if is_invoice:
        invoice_number = (getattr(meta, "invoice_number", None) or "").strip() if meta else ""
        if not invoice_number:
            status = FAIL if policy.require_invoice_number else WARN
            result.add(
                Check("MISSING_INVOICE_NUMBER", status, "Invoice number could not be read.",
                      severity="ERROR" if status == FAIL else "WARNING")
            )
        elif len(invoice_number) < 3:
            result.add(Check("IMPLAUSIBLE_INVOICE_NUMBER", WARN,
                             f"Invoice number '{invoice_number}' looks too short.",
                             severity="WARNING", actual=invoice_number))
        else:
            result.add(Check("INVOICE_NUMBER_PRESENT", PASS, f"Invoice number {invoice_number}."))

    # ---- date ------------------------------------------------------------
    invoice_date = getattr(meta, "invoice_date", None) if meta else None
    parsed_date = parse_document_date(invoice_date)
    if not str(invoice_date or "").strip():
        required = policy.require_invoice_date if is_invoice else False
        status = FAIL if required else WARN
        result.add(
            Check("MISSING_DOCUMENT_DATE", status, "Document date could not be read.",
                  severity="ERROR" if status == FAIL else "WARNING")
        )
    elif parsed_date is None:
        result.add(Check("DOCUMENT_DATE_FORMAT_INVALID", WARN,
                         f"Date '{invoice_date}' is not a recognisable date.",
                         severity="WARNING", actual=str(invoice_date)))
    else:
        result.add(Check("DOCUMENT_DATE_VALID", PASS, f"Date {parsed_date.date().isoformat()}."))

    # ---- PO reference ----------------------------------------------------
    po_number = (po_reference or (getattr(meta, "po_number", None) if meta else None) or "").strip() or None
    if not po_number:
        # A goods document without a PO cannot be applied to any order.
        status = FAIL if (is_goods_doc or policy.require_po_reference) else WARN
        result.add(
            Check("MISSING_PO_REFERENCE", status, "No purchase-order reference on the document.",
                  severity="ERROR" if status == FAIL else "WARNING")
        )
    elif not po_number_is_valid(po_number):
        result.add(Check("PO_NUMBER_FORMAT_INVALID", WARN,
                         f"PO reference '{po_number}' does not look like a PO number.",
                         severity="WARNING", actual=po_number))
    else:
        result.add(Check("PO_REFERENCE_VALID", PASS, f"PO reference {po_number}."))

    # ---- GSTIN -----------------------------------------------------------
    if is_invoice:
        seller_gstin = normalize_gstin(getattr(invoice.seller, "gstin", None) if invoice.seller else None)
        buyer_gstin = normalize_gstin(getattr(invoice.buyer, "gstin", None) if invoice.buyer else None)
        result.gstin = {
            "seller": seller_gstin or None,
            "buyer": buyer_gstin or None,
            "seller_valid": gstin_is_valid(seller_gstin) if seller_gstin else None,
            "buyer_valid": gstin_is_valid(buyer_gstin) if buyer_gstin else None,
        }

        if not seller_gstin:
            result.add(Check("MISSING_SELLER_GSTIN", WARN,
                             "Seller GSTIN is missing; the vendor will have to be matched by name.",
                             severity="WARNING"))
        elif not GSTIN_RE.fullmatch(seller_gstin):
            result.add(Check("INVALID_SELLER_GSTIN_FORMAT", FAIL,
                             f"Seller GSTIN '{seller_gstin}' is not a valid GSTIN format.", actual=seller_gstin))
        elif policy.gstin_checksum and not gstin_is_valid(seller_gstin):
            expected = f"{seller_gstin[:14]}{gstin_check_digit(seller_gstin)}"
            result.add(Check("GSTIN_CHECKSUM_INVALID", FAIL,
                             f"Seller GSTIN '{seller_gstin}' fails the GST check digit (expected '{expected}').",
                             expected=expected, actual=seller_gstin))
        else:
            result.add(Check("SELLER_GSTIN_VALID", PASS, f"Seller GSTIN {seller_gstin} verified."))

        if buyer_gstin and not gstin_is_valid(buyer_gstin):
            result.add(Check("BUYER_GSTIN_CHECKSUM_INVALID", WARN,
                             f"Buyer GSTIN '{buyer_gstin}' fails the GST check digit.",
                             severity="WARNING", actual=buyer_gstin))

    # ---- arithmetic ------------------------------------------------------
    if items:
        canonical_lines = list(getattr(canonical, "lines", None) or []) if canonical is not None else []
        lines: List[Dict[str, Decimal]] = []
        for index, item in enumerate(items):
            lines.append(_line_view(item, canonical_lines[index] if index < len(canonical_lines) else None))

        if canonical is not None:
            subtotal = to_decimal(getattr(canonical, "subtotal", None))
            total_tax = to_decimal(getattr(canonical, "total_tax", None))
            grand_total = to_decimal(getattr(canonical, "grand_total", None))
            cgst = to_decimal(getattr(canonical, "cgst_total", None))
            sgst = to_decimal(getattr(canonical, "sgst_total", None))
            igst = to_decimal(getattr(canonical, "igst_total", None))
        else:
            summary = invoice.summary
            subtotal = to_decimal(getattr(summary, "subtotal", None)) if summary else Decimal("0.00")
            total_tax = to_decimal(getattr(summary, "total_gst", None)) if summary else Decimal("0.00")
            grand_total = to_decimal(getattr(summary, "grand_total", None)) if summary else Decimal("0.00")
            cgst = to_decimal(getattr(summary, "cgst_total", None)) if summary else Decimal("0.00")
            sgst = to_decimal(getattr(summary, "sgst_total", None)) if summary else Decimal("0.00")
            igst = to_decimal(getattr(summary, "igst_total", None)) if summary else Decimal("0.00")

        _arithmetic_checks(
            result,
            lines,
            subtotal,
            total_tax,
            grand_total,
            cgst,
            sgst,
            igst,
            policy,
            require_totals=is_invoice,
        )

    result.passed = not result.errors
    return result


def gate_for_canonical(canonical, validation: Optional[DocValidation] = None) -> DocValidation:
    """
    Arithmetic-only gate for documents that arrive already canonicalised
    (CSV/ERP imports), which have no OCR text to check formats against.
    """
    result = validation or DocValidation()
    lines = getattr(canonical, "lines", None) or []
    if not lines:
        result.add(Check("NO_LINE_ITEMS", FAIL, "No lines on the canonical document."))
        result.passed = False
        return result

    _arithmetic_checks(
        result,
        [_line_view(line) for line in lines],
        to_decimal(getattr(canonical, "subtotal", None)),
        to_decimal(getattr(canonical, "total_tax", None)),
        to_decimal(getattr(canonical, "grand_total", None)),
        to_decimal(getattr(canonical, "cgst_total", None)),
        to_decimal(getattr(canonical, "sgst_total", None)),
        to_decimal(getattr(canonical, "igst_total", None)),
        VALIDATION,
    )
    result.passed = not result.errors
    return result
