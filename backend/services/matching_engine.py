"""
Deterministic 3-way match engine.

This module contains **no LLM and no I/O**. It takes an invoice, the PO state and
the received quantities and returns MATCHED / EXCEPTION / REJECT plus reason codes.
Keeping it pure is what makes it testable against hand-built scenarios.

Order of evaluation:
  1. header checks  (vendor, PO exists/open, currency)
  2. line checks    (invoice qty <= GRN qty <= PO qty, rate tolerance)
  3. totals         (tax recompute, grand total)
  4. partial delivery against the PO line balance (received minus already invoiced)
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Sequence

from ..config import MATCH, DEFAULT_TOLERANCES, MatchTolerances
from .canonical import CanonicalInvoice, CanonicalLine, parse_canonical_date
from .normalization import LineMapping, map_invoice_lines_to_po, normalize_item_code, similarity
from .validator import to_decimal

MONEY = Decimal("0.01")
QTY = Decimal("0.0001")

STATUS_MATCHED = "MATCHED"
STATUS_EXCEPTION = "EXCEPTION"
STATUS_REJECT = "REJECT"

# Reason codes that block the invoice outright (must be re-issued / escalated).
REJECT_REASON_CODES = {
    "VENDOR_MISMATCH",
    "VENDOR_PO_MISMATCH",
    "PO_NOT_FOUND",
    "PO_NOT_OPEN",
    "PO_NUMBER_MISSING",
    "CURRENCY_MISMATCH",
    "MISSING_INVOICE_NUMBER",
    "MISSING_INVOICE_LINES",
    "UNKNOWN_VENDOR",
}

REASON_DESCRIPTIONS = {
    "VENDOR_MISMATCH": "Invoice vendor does not match any entry in the vendor master.",
    "VENDOR_PO_MISMATCH": "Invoice vendor is different from the vendor on the purchase order.",
    "PO_NOT_FOUND": "The referenced purchase order does not exist.",
    "PO_NOT_OPEN": "The purchase order is closed or cancelled.",
    "PO_NUMBER_MISSING": "The invoice does not reference a purchase order.",
    "CURRENCY_MISMATCH": "Invoice currency differs from the purchase order currency.",
    "MISSING_INVOICE_NUMBER": "The invoice number could not be read.",
    "MISSING_INVOICE_LINES": "No line items were extracted from the invoice.",
    "UNKNOWN_VENDOR": "Vendor could not be matched to the vendor master.",
    "LINE_UNMAPPED": "An invoice line could not be matched to any PO line.",
    "QTY_EXCEEDS_GRN": "Invoiced quantity is higher than the quantity received on the GRN.",
    "QTY_EXCEEDS_PO": "Invoiced quantity (including previous invoices) exceeds the ordered quantity.",
    "GRN_MISSING": "No goods receipt exists for this PO line.",
    "RATE_TOLERANCE_EXCEEDED": "Unit rate differs from the PO rate beyond the allowed tolerance.",
    "TAX_RATE_MISMATCH": "Line tax rate differs from the tax rate on the purchase order.",
    "LINE_AMOUNT_MISMATCH": "Quantity x rate does not equal the line amount on the invoice.",
    "TAX_MISMATCH": "Recomputed tax does not match the tax stated on the invoice.",
    "TAX_BREAKDOWN_MISMATCH": "CGST + SGST + IGST does not equal the total tax on the invoice.",
    "TOTAL_MISMATCH": "Line totals plus tax do not equal the invoice grand total.",
    "TOTAL_EXCEEDS_PO": "Cumulative invoiced value exceeds the purchase order value.",
    "INVOICE_DATE_INVALID": "The invoice date could not be parsed.",
    "INVOICE_DATE_IN_FUTURE": "The invoice date is in the future.",
}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass
class POLineState:
    """A PO line plus its live balance from the GRN / invoice ledger."""

    line_no: int
    ordered_qty: Decimal = Decimal("0")
    received_qty: Decimal = Decimal("0")      # cumulative across all GRNs
    invoiced_qty: Decimal = Decimal("0")      # already invoiced (this PO line)
    credited_qty: Decimal = Decimal("0")      # credit notes issued
    rate: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    item_code: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    line_amount: Decimal = Decimal("0")

    @property
    def billable_qty(self) -> Decimal:
        """Cumulative received minus what has already been invoiced / credited."""
        return (self.received_qty - self.invoiced_qty - self.credited_qty).quantize(QTY)

    @property
    def remaining_order_qty(self) -> Decimal:
        return (self.ordered_qty - self.invoiced_qty - self.credited_qty).quantize(QTY)

    def as_canonical_line(self) -> CanonicalLine:
        return CanonicalLine(
            line_no=self.line_no,
            item_code=self.item_code,
            description=self.description,
            quantity=float(self.ordered_qty),
            unit=self.unit,
            rate=float(self.rate),
            tax_rate=float(self.tax_rate),
            amount=float(self.line_amount or (self.ordered_qty * self.rate)),
        )


@dataclass
class POState:
    po_number: str
    status: str = "OPEN"
    vendor_name: Optional[str] = None
    vendor_gstin: Optional[str] = None
    currency: str = "INR"
    lines: List[POLineState] = field(default_factory=list)
    grand_total: Decimal = Decimal("0")
    invoiced_value: Decimal = Decimal("0")    # value already invoiced against this PO


@dataclass
class MatchContext:
    invoice: CanonicalInvoice
    po: Optional[POState] = None
    vendor_resolved: bool = True
    vendor_resolution_method: str = "NONE"
    vendor_id: Optional[int] = None
    tolerances: MatchTolerances = DEFAULT_TOLERANCES
    require_grn: bool = MATCH.require_grn
    today: Optional[str] = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
@dataclass
class LineFinding:
    invoice_line_no: int
    item_code: Optional[str] = None
    description: Optional[str] = None
    po_line_no: Optional[int] = None
    mapping_method: str = "NONE"
    mapping_score: float = 0.0
    invoice_qty: Decimal = Decimal("0")
    po_qty: Decimal = Decimal("0")
    grn_qty: Decimal = Decimal("0")
    already_invoiced_qty: Decimal = Decimal("0")
    billable_qty: Decimal = Decimal("0")
    invoice_rate: Decimal = Decimal("0")
    po_rate: Decimal = Decimal("0")
    invoice_amount: Decimal = Decimal("0")
    reason_codes: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return STATUS_EXCEPTION if self.reason_codes else STATUS_MATCHED

    def to_dict(self) -> Dict:
        return {
            "invoice_line_no": self.invoice_line_no,
            "item_code": self.item_code,
            "description": self.description,
            "po_line_no": self.po_line_no,
            "mapping_method": self.mapping_method,
            "mapping_score": self.mapping_score,
            "invoice_qty": str(self.invoice_qty),
            "po_qty": str(self.po_qty),
            "grn_qty": str(self.grn_qty),
            "already_invoiced_qty": str(self.already_invoiced_qty),
            "billable_qty": str(self.billable_qty),
            "invoice_rate": str(self.invoice_rate),
            "po_rate": str(self.po_rate),
            "invoice_amount": str(self.invoice_amount),
            "reason_codes": self.reason_codes,
            "status": self.status,
        }


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    reason_code: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "reason_code": self.reason_code,
        }


@dataclass
class MatchOutcome:
    status: str
    reason_codes: List[str] = field(default_factory=list)
    checks: List[CheckResult] = field(default_factory=list)
    line_findings: List[LineFinding] = field(default_factory=list)
    matched_po_number: Optional[str] = None
    computed_tax: Decimal = Decimal("0")
    computed_total: Decimal = Decimal("0")
    variance_total: Decimal = Decimal("0")
    overridable: bool = False

    @property
    def is_matched(self) -> bool:
        return self.status == STATUS_MATCHED

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "reason_codes": self.reason_codes,
            "reason_descriptions": [REASON_DESCRIPTIONS.get(c, c) for c in self.reason_codes],
            "checks": [c.to_dict() for c in self.checks],
            "line_findings": [f.to_dict() for f in self.line_findings],
            "matched_po_number": self.matched_po_number,
            "computed_tax": str(self.computed_tax),
            "computed_total": str(self.computed_total),
            "variance_total": str(self.variance_total),
            "overridable": self.overridable,
        }


def _m(value) -> Decimal:
    return to_decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _q(value) -> Decimal:
    return to_decimal(value).quantize(QTY, rounding=ROUND_HALF_UP)


def _add_reason(outcome: MatchOutcome, code: str) -> None:
    if code not in outcome.reason_codes:
        outcome.reason_codes.append(code)


def compute_tax(invoice: CanonicalInvoice) -> Decimal:
    """Recomputes tax from the lines using the invoice's own stated tax rate."""
    total = Decimal("0")
    for line in invoice.lines:
        taxable = _m(line.amount)
        rate = to_decimal(line.tax_rate)
        if taxable > 0 and rate > 0:
            total += (taxable * rate / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)
    return total.quantize(MONEY, rounding=ROUND_HALF_UP)


def run_three_way_match(ctx: MatchContext) -> MatchOutcome:
    invoice = ctx.invoice
    tolerances = ctx.tolerances
    outcome = MatchOutcome(status=STATUS_MATCHED)
    invoice_lines: Sequence[CanonicalLine] = invoice.lines

    # ---------------- header checks ----------------
    if not invoice.invoice_number:
        outcome.checks.append(CheckResult("invoice_number", False, "missing", "MISSING_INVOICE_NUMBER"))
        _add_reason(outcome, "MISSING_INVOICE_NUMBER")
    else:
        outcome.checks.append(CheckResult("invoice_number", True, invoice.invoice_number))

    if not invoice_lines:
        outcome.checks.append(CheckResult("line_items", False, "no lines", "MISSING_INVOICE_LINES"))
        _add_reason(outcome, "MISSING_INVOICE_LINES")
    else:
        outcome.checks.append(CheckResult("line_items", True, f"{len(invoice_lines)} line(s)"))

    if not ctx.vendor_resolved:
        outcome.checks.append(CheckResult("vendor", False, "not in vendor master", "UNKNOWN_VENDOR"))
        _add_reason(outcome, "UNKNOWN_VENDOR")
        _add_reason(outcome, "VENDOR_MISMATCH")
    elif not (invoice.vendor_gstin or invoice.vendor_name):
        outcome.checks.append(CheckResult("vendor", False, "no vendor identity on invoice", "VENDOR_MISMATCH"))
        _add_reason(outcome, "VENDOR_MISMATCH")
    else:
        outcome.checks.append(CheckResult("vendor", True, f"resolved via {ctx.vendor_resolution_method}"))

    if not invoice.po_number:
        outcome.checks.append(CheckResult("po_reference", False, "no PO on invoice", "PO_NUMBER_MISSING"))
        _add_reason(outcome, "PO_NUMBER_MISSING")
    elif ctx.po is None:
        outcome.checks.append(CheckResult("po_reference", False, f"PO {invoice.po_number} not found", "PO_NOT_FOUND"))
        _add_reason(outcome, "PO_NOT_FOUND")
    elif (ctx.po.status or "").upper() != "OPEN":
        outcome.checks.append(
            CheckResult("po_reference", False, f"PO {ctx.po.po_number} is {ctx.po.status}", "PO_NOT_OPEN")
        )
        _add_reason(outcome, "PO_NOT_OPEN")
    else:
        outcome.matched_po_number = ctx.po.po_number
        outcome.checks.append(CheckResult("po_reference", True, ctx.po.po_number))

    if ctx.po is not None:
        inv_ccy = (invoice.currency or "INR").upper()
        po_ccy = (ctx.po.currency or "INR").upper()
        if inv_ccy != po_ccy:
            outcome.checks.append(
                CheckResult("currency", False, f"invoice {inv_ccy} vs PO {po_ccy}", "CURRENCY_MISMATCH")
            )
            _add_reason(outcome, "CURRENCY_MISMATCH")
        else:
            outcome.checks.append(CheckResult("currency", True, inv_ccy))

        # vendor on the PO must be the same party that billed us
        if ctx.vendor_resolved and (ctx.po.vendor_gstin or ctx.po.vendor_name):
            same = True
            if invoice.vendor_gstin and ctx.po.vendor_gstin:
                same = re_clean_gstin(invoice.vendor_gstin) == re_clean_gstin(ctx.po.vendor_gstin)
            elif invoice.vendor_name and ctx.po.vendor_name:
                same = similarity(invoice.vendor_name, ctx.po.vendor_name) >= 0.9
            if not same:
                outcome.checks.append(
                    CheckResult("vendor_po", False, "invoice vendor != PO vendor", "VENDOR_PO_MISMATCH")
                )
                _add_reason(outcome, "VENDOR_PO_MISMATCH")
            else:
                outcome.checks.append(CheckResult("vendor_po", True, "same vendor"))

    parsed_date = parse_canonical_date(invoice.invoice_date)
    if invoice.invoice_date and parsed_date is None:
        outcome.checks.append(CheckResult("invoice_date", False, "unparseable", "INVOICE_DATE_INVALID"))
        _add_reason(outcome, "INVOICE_DATE_INVALID")
    elif parsed_date and ctx.today:
        today = parse_canonical_date(ctx.today)
        if today and parsed_date > today:
            outcome.checks.append(CheckResult("invoice_date", False, "in the future", "INVOICE_DATE_IN_FUTURE"))
            _add_reason(outcome, "INVOICE_DATE_IN_FUTURE")
        else:
            outcome.checks.append(CheckResult("invoice_date", True, parsed_date.isoformat()))
    else:
        outcome.checks.append(CheckResult("invoice_date", True, str(invoice.invoice_date)))

    # ---------------- line checks ----------------
    if invoice_lines and ctx.po is not None:
        po_canonical_lines = [line.as_canonical_line() for line in ctx.po.lines]
        mappings = map_invoice_lines_to_po(invoice_lines, po_canonical_lines)
        state_by_line_no = {line.line_no: line for line in ctx.po.lines}

        for mapping in mappings:
            finding = LineFinding(
                invoice_line_no=mapping.invoice_line.line_no,
                item_code=normalize_item_code(mapping.invoice_line.item_code),
                description=mapping.invoice_line.description,
                mapping_method=mapping.method,
                mapping_score=mapping.score,
                invoice_qty=_q(mapping.invoice_line.quantity),
                invoice_rate=_m(mapping.invoice_line.rate),
                invoice_amount=_m(mapping.invoice_line.amount),
            )

            if not mapping.mapped:
                finding.reason_codes.append("LINE_UNMAPPED")
                _add_reason(outcome, "LINE_UNMAPPED")
                outcome.line_findings.append(finding)
                continue

            po_line_state = state_by_line_no.get(mapping.po_line.line_no)
            if po_line_state is None:
                finding.reason_codes.append("LINE_UNMAPPED")
                _add_reason(outcome, "LINE_UNMAPPED")
                outcome.line_findings.append(finding)
                continue

            finding.po_line_no = po_line_state.line_no
            finding.po_qty = po_line_state.ordered_qty
            finding.grn_qty = po_line_state.received_qty
            finding.already_invoiced_qty = po_line_state.invoiced_qty
            finding.billable_qty = po_line_state.billable_qty
            finding.po_rate = _m(po_line_state.rate)

            _check_line(finding, mapping, po_line_state, tolerances, ctx.require_grn)
            for code in finding.reason_codes:
                _add_reason(outcome, code)
            outcome.line_findings.append(finding)

    # ---------------- totals ----------------
    computed_tax = compute_tax(invoice)
    lines_total = sum((_m(l.amount) for l in invoice_lines), Decimal("0")).quantize(MONEY)
    stated_tax = _m(invoice.total_tax)
    stated_total = _m(invoice.grand_total)
    computed_total = (lines_total + (stated_tax if stated_tax > 0 else computed_tax)).quantize(MONEY)
    outcome.computed_tax = computed_tax
    outcome.computed_total = computed_total

    if invoice_lines:
        if stated_tax > 0 and abs(computed_tax - stated_tax) > tolerances.tax_abs:
            outcome.checks.append(
                CheckResult("tax_recompute", False, f"computed {computed_tax} vs stated {stated_tax}", "TAX_MISMATCH")
            )
            _add_reason(outcome, "TAX_MISMATCH")
        else:
            outcome.checks.append(CheckResult("tax_recompute", True, f"tax {stated_tax or computed_tax}"))

        breakdown = _m(invoice.cgst_total) + _m(invoice.sgst_total) + _m(invoice.igst_total)
        if breakdown > 0 and abs(breakdown - stated_tax) > tolerances.tax_abs:
            outcome.checks.append(
                CheckResult(
                    "tax_breakdown",
                    False,
                    f"CGST+SGST+IGST={breakdown} vs total tax {stated_tax}",
                    "TAX_BREAKDOWN_MISMATCH",
                )
            )
            _add_reason(outcome, "TAX_BREAKDOWN_MISMATCH")
        else:
            outcome.checks.append(CheckResult("tax_breakdown", True, "breakdown consistent"))

        if stated_total > 0:
            if abs(computed_total - stated_total) > tolerances.total_abs:
                outcome.variance_total = (stated_total - computed_total).quantize(MONEY)
                outcome.checks.append(
                    CheckResult("grand_total", False, f"computed {computed_total} vs stated {stated_total}", "TOTAL_MISMATCH")
                )
                _add_reason(outcome, "TOTAL_MISMATCH")
            else:
                outcome.checks.append(CheckResult("grand_total", True, f"total {stated_total}"))
        else:
            outcome.checks.append(CheckResult("grand_total", False, "missing", "TOTAL_MISMATCH"))
            _add_reason(outcome, "TOTAL_MISMATCH")

        # Cumulative PO value guard (over-billing the order as a whole). Skip it
        # when a quantity overrun already explains the excess, so the exception
        # queue shows one cause rather than two symptoms of the same problem.
        if ctx.po is not None and ctx.po.grand_total > 0 and "QTY_EXCEEDS_PO" not in outcome.reason_codes:
            cumulative = ctx.po.invoiced_value + stated_total
            ceiling = ctx.po.grand_total * (Decimal("1") + tolerances.price_pct)
            if cumulative > ceiling.quantize(MONEY, rounding=ROUND_HALF_UP):
                outcome.checks.append(
                    CheckResult(
                        "po_value",
                        False,
                        f"cumulative invoiced {cumulative} exceeds PO value {ctx.po.grand_total}",
                        "TOTAL_EXCEEDS_PO",
                    )
                )
                _add_reason(outcome, "TOTAL_EXCEEDS_PO")

    # ---------------- verdict ----------------
    reject_codes = [c for c in outcome.reason_codes if c in REJECT_REASON_CODES]
    if reject_codes:
        outcome.status = STATUS_REJECT
        outcome.overridable = False
    elif outcome.reason_codes:
        outcome.status = STATUS_EXCEPTION
        outcome.overridable = True
    else:
        outcome.status = STATUS_MATCHED
        outcome.overridable = False

    return outcome


def _check_line(
    finding: LineFinding,
    mapping: LineMapping,
    po_line: POLineState,
    tolerances: MatchTolerances,
    require_grn: bool,
) -> None:
    """Fills in the reason codes for one mapped invoice line."""
    inv_qty = finding.invoice_qty
    qty_tolerance = tolerances.quantity

    # 1. Invoice qty must not exceed what was received (GRN is the physical truth)
    if require_grn and po_line.received_qty <= 0:
        finding.reason_codes.append("GRN_MISSING")
    elif inv_qty > po_line.received_qty + qty_tolerance:
        finding.reason_codes.append("QTY_EXCEEDS_GRN")

    # 2. Invoice qty (plus what was already invoiced) must not exceed the order
    if inv_qty > 0 and po_line.ordered_qty > 0:
        if po_line.invoiced_qty + inv_qty > po_line.ordered_qty + qty_tolerance:
            finding.reason_codes.append("QTY_EXCEEDS_PO")

    # 3. Partial deliveries: bill only the un-invoiced portion of the received qty
    if inv_qty > 0 and po_line.billable_qty >= 0 and po_line.invoiced_qty > 0:
        if inv_qty > po_line.billable_qty + qty_tolerance:
            if "QTY_EXCEEDS_GRN" not in finding.reason_codes:
                finding.reason_codes.append("QTY_EXCEEDS_GRN")

    # 4. Rate tolerance
    po_rate = _m(po_line.rate)
    if po_rate > 0:
        upper = po_rate * (Decimal("1") + tolerances.price_pct)
        lower = po_rate * (Decimal("1") - tolerances.price_pct)
        if finding.invoice_rate > upper or finding.invoice_rate < lower:
            finding.reason_codes.append("RATE_TOLERANCE_EXCEEDED")

    # 5. Line arithmetic (qty * rate vs amount), allowing net or gross presentation
    if inv_qty > 0 and finding.invoice_rate > 0 and finding.invoice_amount > 0:
        net = (inv_qty * finding.invoice_rate).quantize(MONEY, rounding=ROUND_HALF_UP)
        gross = (net * (Decimal("1") + to_decimal(mapping.invoice_line.tax_rate) / Decimal("100"))).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
        if (
            abs(finding.invoice_amount - net) > tolerances.line_abs
            and abs(finding.invoice_amount - gross) > tolerances.line_abs
        ):
            finding.reason_codes.append("LINE_AMOUNT_MISMATCH")

    # 6. Tax rate must agree with the PO line (only when the PO actually states one)
    po_tax_rate = to_decimal(po_line.tax_rate)
    inv_tax_rate = to_decimal(mapping.invoice_line.tax_rate)
    if po_tax_rate > 0 and abs(inv_tax_rate - po_tax_rate) > Decimal("0.01"):
        finding.reason_codes.append("TAX_RATE_MISMATCH")


def re_clean_gstin(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def explain_reason_codes(codes: Sequence[str]) -> List[str]:
    return [REASON_DESCRIPTIONS.get(code, code) for code in codes]
