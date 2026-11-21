"""
Hand-built 3-way match scenarios.

These cases define correct matching behaviour: partial deliveries, tax mismatch,
duplicates, over-billing, tolerance edges, and the exception-vs-reject split.
"""
from decimal import Decimal

from backend.config import MatchTolerances
from backend.services.canonical import CanonicalInvoice, CanonicalLine
from backend.services.matching_engine import (
    STATUS_EXCEPTION,
    STATUS_MATCHED,
    STATUS_REJECT,
    MatchContext,
    POLineState,
    POState,
    run_three_way_match,
)


def po_line(
    line_no=1,
    ordered=100,
    received=100,
    invoiced=0,
    credited=0,
    rate=62.50,
    tax=18,
    code="STL-100",
    description="MS Angle 50x50x6",
    unit="KG",
):
    return POLineState(
        line_no=line_no,
        ordered_qty=Decimal(str(ordered)),
        received_qty=Decimal(str(received)),
        invoiced_qty=Decimal(str(invoiced)),
        credited_qty=Decimal(str(credited)),
        rate=Decimal(str(rate)),
        tax_rate=Decimal(str(tax)),
        item_code=code,
        description=description,
        unit=unit,
        line_amount=Decimal(str(ordered)) * Decimal(str(rate)),
    )


def po_state(lines=None, po_number="PO-1", status="OPEN", currency="INR", grand_total=None, invoiced_value=0):
    lines = lines or [po_line()]
    computed = sum((l.ordered_qty * l.rate for l in lines), Decimal("0"))
    computed_tax = sum((l.ordered_qty * l.rate * l.tax_rate / Decimal("100") for l in lines), Decimal("0"))
    return POState(
        po_number=po_number,
        status=status,
        vendor_name="Acme Steel Pvt Ltd",
        vendor_gstin="27AABCU9603R1ZM",
        currency=currency,
        lines=lines,
        grand_total=Decimal(str(grand_total)) if grand_total is not None else (computed + computed_tax),
        invoiced_value=Decimal(str(invoiced_value)),
    )


def inv_line(
    line_no=1,
    qty=100,
    rate=62.50,
    tax=18,
    code="STL-100",
    description="MS Angle 50x50x6",
    unit="KG",
    amount=None,
):
    qty_d = Decimal(str(qty))
    rate_d = Decimal(str(rate))
    amount_d = Decimal(str(amount)) if amount is not None else (qty_d * rate_d)
    return CanonicalLine(
        line_no=line_no,
        item_code=code,
        description=description,
        quantity=float(qty_d),
        unit=unit,
        rate=float(rate_d),
        tax_rate=float(tax),
        amount=float(amount_d),
    )


def invoice(
    lines=None,
    number="INV-001",
    date="2026-11-08",
    po="PO-1",
    vendor="Acme Steel Pvt Ltd",
    gstin="27AABCU9603R1ZM",
    currency="INR",
    total_tax=None,
    grand_total=None,
    cgst=None,
    sgst=None,
    igst=None,
):
    lines = lines or [inv_line()]
    subtotal = sum((Decimal(str(l.amount)) for l in lines), Decimal("0"))
    computed_tax = sum(
        (Decimal(str(l.amount)) * Decimal(str(l.tax_rate)) / Decimal("100") for l in lines),
        Decimal("0"),
    )
    tax = Decimal(str(total_tax)) if total_tax is not None else computed_tax
    half = (tax / 2).quantize(Decimal("0.01"))
    return CanonicalInvoice(
        vendor_name=vendor,
        vendor_gstin=gstin,
        invoice_number=number,
        invoice_date=date,
        po_number=po,
        currency=currency,
        lines=lines,
        subtotal=float(subtotal),
        cgst_total=float(Decimal(str(cgst)) if cgst is not None else half),
        sgst_total=float(Decimal(str(sgst)) if sgst is not None else half),
        igst_total=float(Decimal(str(igst)) if igst is not None else 0),
        total_tax=float(tax),
        grand_total=float(Decimal(str(grand_total)) if grand_total is not None else (subtotal + tax)),
    )


_UNSET = object()


def ctx(inv=None, po=_UNSET, **kwargs):
    defaults = dict(
        invoice=inv or invoice(),
        po=po_state() if po is _UNSET else po,
        vendor_resolved=True,
        vendor_resolution_method="GSTIN",
        tolerances=MatchTolerances(),
        require_grn=True,
        today="2027-01-01",
    )
    defaults.update(kwargs)
    return MatchContext(**defaults)


def codes(outcome):
    return set(outcome.reason_codes)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_clean_invoice_matches():
    outcome = run_three_way_match(ctx())
    assert outcome.status == STATUS_MATCHED
    assert outcome.reason_codes == []
    assert outcome.matched_po_number == "PO-1"


def test_rate_within_tolerance_matches():
    # +1.9% is inside the ±2% default
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(rate=63.60)])))
    assert outcome.status == STATUS_MATCHED
    assert "RATE_TOLERANCE_EXCEEDED" not in codes(outcome)


def test_rate_above_tolerance_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(rate=66.00)])))
    assert outcome.status == STATUS_EXCEPTION
    assert "RATE_TOLERANCE_EXCEEDED" in codes(outcome)


def test_rate_below_tolerance_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(rate=59.00)])))
    assert outcome.status == STATUS_EXCEPTION
    assert "RATE_TOLERANCE_EXCEEDED" in codes(outcome)


def test_gross_line_amount_is_accepted():
    gross = Decimal("100") * Decimal("62.50") * Decimal("1.18")
    outcome = run_three_way_match(
        ctx(
            inv=invoice(
                lines=[inv_line(amount=gross)],
                total_tax=0,
                grand_total=gross,
                cgst=0,
                sgst=0,
            )
        )
    )
    assert "LINE_AMOUNT_MISMATCH" not in codes(outcome)


# ---------------------------------------------------------------------------
# Quantities, GRN and partial deliveries
# ---------------------------------------------------------------------------
def test_invoice_qty_above_grn_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=120)])))
    assert outcome.status == STATUS_EXCEPTION
    assert "QTY_EXCEEDS_GRN" in codes(outcome)


def test_invoice_qty_above_po_is_exception():
    po = po_state(lines=[po_line(ordered=100, received=100)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=110)]), po=po))
    assert "QTY_EXCEEDS_PO" in codes(outcome)


def test_grn_missing_is_exception():
    po = po_state(lines=[po_line(received=0)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=10)]), po=po))
    assert "GRN_MISSING" in codes(outcome)


def test_partial_delivery_bills_only_received_qty():
    po = po_state(lines=[po_line(ordered=100, received=60)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=60)]), po=po))
    assert outcome.status == STATUS_MATCHED


def test_partial_delivery_over_billed_is_exception():
    po = po_state(lines=[po_line(ordered=100, received=60)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=70)]), po=po))
    assert outcome.status == STATUS_EXCEPTION
    assert "QTY_EXCEEDS_GRN" in codes(outcome)


def test_second_partial_invoice_bills_remaining_received_qty():
    po = po_state(lines=[po_line(ordered=100, received=100, invoiced=60)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=40)]), po=po))
    assert outcome.status == STATUS_MATCHED


def test_second_partial_invoice_cannot_bill_more_than_received_balance():
    po = po_state(lines=[po_line(ordered=100, received=100, invoiced=80)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=40)]), po=po))
    assert "QTY_EXCEEDS_GRN" in codes(outcome)
    assert "QTY_EXCEEDS_PO" in codes(outcome)


def test_credit_note_reduces_billable_quantity():
    po = po_state(lines=[po_line(ordered=100, received=100, invoiced=60, credited=30)])
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=40)]), po=po))
    assert "QTY_EXCEEDS_GRN" in codes(outcome)


# ---------------------------------------------------------------------------
# Header-level rejections
# ---------------------------------------------------------------------------
def test_missing_po_number_is_reject():
    outcome = run_three_way_match(ctx(inv=invoice(po=None), po=None))
    assert outcome.status == STATUS_REJECT
    assert "PO_NUMBER_MISSING" in codes(outcome)


def test_po_not_found_is_reject():
    outcome = run_three_way_match(ctx(po=None))
    assert outcome.status == STATUS_REJECT
    assert "PO_NOT_FOUND" in codes(outcome)


def test_closed_po_is_reject():
    outcome = run_three_way_match(ctx(po=po_state(status="CLOSED")))
    assert outcome.status == STATUS_REJECT
    assert "PO_NOT_OPEN" in codes(outcome)


def test_currency_mismatch_is_reject():
    outcome = run_three_way_match(ctx(po=po_state(currency="USD")))
    assert outcome.status == STATUS_REJECT
    assert "CURRENCY_MISMATCH" in codes(outcome)


def test_duplicate_invoice_is_reject():
    outcome = run_three_way_match(ctx(duplicate_of_invoice_id=42))
    assert outcome.status == STATUS_REJECT
    assert "DUPLICATE_INVOICE" in codes(outcome)


def test_unknown_vendor_is_reject():
    outcome = run_three_way_match(ctx(vendor_resolved=False))
    assert outcome.status == STATUS_REJECT
    assert "UNKNOWN_VENDOR" in codes(outcome)


def test_vendor_on_po_must_match_invoice_vendor():
    po = po_state()
    po.vendor_gstin = "29ZZZZZ1234Z1Z5"
    outcome = run_three_way_match(ctx(po=po))
    assert outcome.status == STATUS_REJECT
    assert "VENDOR_PO_MISMATCH" in codes(outcome)


def test_missing_invoice_number_is_reject():
    outcome = run_three_way_match(ctx(inv=invoice(number=None)))
    assert outcome.status == STATUS_REJECT
    assert "MISSING_INVOICE_NUMBER" in codes(outcome)


def test_no_line_items_is_reject():
    inv = invoice()
    inv.lines = []
    outcome = run_three_way_match(ctx(inv=inv))
    assert outcome.status == STATUS_REJECT
    assert "MISSING_INVOICE_LINES" in codes(outcome)


def test_invoice_date_in_future_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(date="2027-06-01"), today="2027-01-01"))
    assert outcome.status == STATUS_EXCEPTION
    assert "INVOICE_DATE_IN_FUTURE" in codes(outcome)


# ---------------------------------------------------------------------------
# Totals and tax
# ---------------------------------------------------------------------------
def test_tax_mismatch_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(total_tax=100)))
    assert outcome.status == STATUS_EXCEPTION
    assert "TAX_MISMATCH" in codes(outcome)


def test_tax_breakdown_mismatch_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(cgst=500, sgst=500)))
    assert "TAX_BREAKDOWN_MISMATCH" in codes(outcome)


def test_grand_total_mismatch_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(grand_total=1)))
    assert outcome.status == STATUS_EXCEPTION
    assert "TOTAL_MISMATCH" in codes(outcome)


def test_line_amount_mismatch_is_exception():
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(amount=Decimal("100") * Decimal("62.50") + 100)]))
    )
    assert "LINE_AMOUNT_MISMATCH" in codes(outcome)


def test_tax_rate_mismatch_is_exception():
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(tax=12)])))
    assert "TAX_RATE_MISMATCH" in codes(outcome)


def test_cumulative_value_above_po_is_exception():
    po = po_state(lines=[po_line(ordered=100, rate=62.50)], invoiced_value=6000)
    outcome = run_three_way_match(ctx(inv=invoice(lines=[inv_line(qty=100)]), po=po))
    assert "TOTAL_EXCEEDS_PO" in codes(outcome)


# ---------------------------------------------------------------------------
# Line mapping ladder
# ---------------------------------------------------------------------------
def test_line_mapping_by_item_code_ignores_description_drift():
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(code="STL-100", description="angle section 50 mm")]))
    )
    finding = outcome.line_findings[0]
    assert finding.mapping_method == "ITEM_CODE"
    assert outcome.status == STATUS_MATCHED


def test_line_mapping_falls_back_to_fuzzy_description():
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(code=None, description="MS Angle 50 x 50 x 6")]))
    )
    finding = outcome.line_findings[0]
    assert finding.mapping_method == "DESCRIPTION_FUZZY"
    assert finding.po_line_no == 1


def test_line_mapping_falls_back_to_rate_and_quantity():
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(code=None, description="misc fabricated item xyz")]))
    )
    finding = outcome.line_findings[0]
    assert finding.mapping_method == "RATE_QTY"
    assert finding.po_line_no == 1


def test_unmapped_line_is_exception():
    po = po_state(lines=[po_line(ordered=10, received=10, rate=999.00, code="OTHER", description="Widget")])
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(code=None, description="completely different service", rate=5, qty=1)]), po=po)
    )
    assert outcome.status == STATUS_EXCEPTION
    assert "LINE_UNMAPPED" in codes(outcome)


# ---------------------------------------------------------------------------
# Tolerances are configuration
# ---------------------------------------------------------------------------
def test_zero_price_tolerance_flags_small_drift():
    outcome = run_three_way_match(
        ctx(inv=invoice(lines=[inv_line(rate=63.60)]), tolerances=MatchTolerances(price_pct=Decimal("0")))
    )
    assert "RATE_TOLERANCE_EXCEEDED" in codes(outcome)


def test_quantity_tolerance_allows_small_overage():
    po = po_state(lines=[po_line(ordered=100, received=100)])
    outcome = run_three_way_match(
        ctx(
            inv=invoice(lines=[inv_line(qty=100.5)]),
            po=po,
            tolerances=MatchTolerances(quantity=Decimal("1")),
        )
    )
    assert "QTY_EXCEEDS_GRN" not in codes(outcome)
