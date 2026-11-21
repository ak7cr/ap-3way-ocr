"""
Goods-receipt intake that does not depend on OCR.

GRNs are the weakest OCR input in Indian AP: they are often handwritten, stamped,
or not issued at all. For customers without an ERP the right answer is not better
OCR, it is a small form (or a spreadsheet) where the store person enters the
received quantity against the PO they already have.

Everything here produces the same canonical GRN payload the OCR path produces,
so the match engine cannot tell the difference.
"""
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from ..database import GoodsReceipt, PurchaseOrder, PurchaseOrderLine
from .validator import to_decimal

QUANTITY_KEYS = ("received_qty", "accepted_qty", "quantity", "qty", "received_quantity")


class GrnIntakeError(ValueError):
    """Raised when the manual receipt cannot be understood."""


def po_lines_for_entry(db: Session, po_number: str) -> List[Dict[str, Any]]:
    """The rows a store person sees when recording a receipt against a PO."""
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == (po_number or "").strip()).first()
    if po is None:
        raise GrnIntakeError(f"Purchase order {po_number} not found")

    already = {}
    for grn in db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).all():
        for line in grn.lines:
            if line.po_line_id:
                already[line.po_line_id] = already.get(line.po_line_id, Decimal("0")) + to_decimal(line.received_qty)

    rows = []
    for line in sorted(po.lines, key=lambda l: l.line_no or 0):
        received = already.get(line.id, Decimal("0"))
        rows.append(
            {
                "po_line_no": line.line_no,
                "item_code": line.item_code,
                "description": line.description,
                "unit": line.unit,
                "ordered_qty": str(line.ordered_qty or 0),
                "already_received_qty": str(received),
                "remaining_qty": str(max(Decimal("0"), to_decimal(line.ordered_qty) - received)),
            }
        )
    return rows


def _generated_grn_number(po_number: str, receipt_date: Optional[str], existing: int) -> str:
    stamp = (receipt_date or date.today().isoformat()).replace("-", "")
    return f"MANUAL/{po_number}/{stamp}/{existing + 1}"


def build_manual_grn_payload(
    db: Session,
    *,
    po_number: str,
    rows: Sequence[Dict[str, Any]],
    grn_number: Optional[str] = None,
    receipt_date: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validates a hand-entered receipt and returns the canonical GRN payload.

    Rejects the mistakes a form makes easy: no PO, no rows, a row for an item
    that is not on the PO, or negative/zero quantities.
    """
    po_number = (po_number or "").strip()
    if not po_number:
        raise GrnIntakeError("A PO number is required")
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_number == po_number).first()
    if po is None:
        raise GrnIntakeError(f"Purchase order {po_number} not found")

    lines: List[Dict[str, Any]] = []
    seen: set = set()
    for index, row in enumerate(rows or [], start=1):
        qty_raw = next((row.get(key) for key in QUANTITY_KEYS if row.get(key) not in (None, "")), None)
        if qty_raw in (None, ""):
            continue
        qty = to_decimal(qty_raw)
        if qty <= 0:
            raise GrnIntakeError(f"Row {index}: received quantity must be greater than zero")
        if qty > Decimal("1000000000"):
            raise GrnIntakeError(f"Row {index}: received quantity {qty} is implausible")

        po_line_no = row.get("po_line_no") or row.get("line_no")
        item_code = (row.get("item_code") or "").strip() or None
        description = (row.get("description") or "").strip() or None

        target = None
        if po_line_no not in (None, ""):
            try:
                wanted = int(float(po_line_no))
            except (TypeError, ValueError):
                raise GrnIntakeError(f"Row {index}: '{po_line_no}' is not a PO line number")
            target = next((l for l in po.lines if l.line_no == wanted), None)
            if target is None:
                raise GrnIntakeError(f"Row {index}: PO {po_number} has no line {wanted}")
        elif item_code:
            target = next(
                (l for l in po.lines if (l.item_code or "").strip().upper() == item_code.upper()),
                None,
            )
            if target is None:
                raise GrnIntakeError(f"Row {index}: item code {item_code} is not on PO {po_number}")
        else:
            raise GrnIntakeError(f"Row {index}: give a PO line number or an item code")

        if target.line_no in seen:
            raise GrnIntakeError(f"Row {index}: PO line {target.line_no} is listed twice")
        seen.add(target.line_no)

        lines.append(
            {
                "po_line_no": target.line_no,
                "item_code": target.item_code,
                "description": target.description or description,
                "unit": target.unit,
                "received_quantity": str(qty),
            }
        )

    if not lines:
        raise GrnIntakeError("Enter at least one received quantity")

    existing = db.query(GoodsReceipt).filter(GoodsReceipt.po_id == po.id).count()
    return {
        "grn_number": (grn_number or "").strip() or _generated_grn_number(po_number, receipt_date, existing),
        "po_number": po_number,
        "receipt_date": receipt_date or date.today().isoformat(),
        "currency": po.currency or "INR",
        "note": note,
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Spreadsheet / CSV intake
# ---------------------------------------------------------------------------
def table_to_csv_text(filename: str, content: bytes) -> str:
    """
    Normalises an uploaded receipt sheet to CSV text.

    CSV/TSV needs no dependency; XLSX needs openpyxl, which is optional so the
    core install stays lean.
    """
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise GrnIntakeError(
                "Reading .xlsx needs the 'openpyxl' package. Install it, or upload the sheet as CSV."
            ) from exc
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in sheet.iter_rows(values_only=True):
            if row is None:
                continue
            writer.writerow(["" if cell is None else cell for cell in row])
        workbook.close()
        return buffer.getvalue()

    text = content.decode("utf-8-sig", errors="replace")
    if name.endswith(".tsv") or ("\t" in text and "," not in text.splitlines()[0:1][0] if text else False):
        text = re.sub("\t", ",", text)
    return text


def group_receipt_rows(text: str) -> List[Dict[str, Any]]:
    """
    Groups a flat sheet into one receipt per GRN number.

    Recognised columns: grn_number, po_number, receipt_date, po_line_no,
    item_code, description, received_qty (or accepted_qty / quantity).
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise GrnIntakeError("The uploaded sheet has no rows")
    if not any((row.get("po_number") or row.get("po") or "").strip() for row in rows):
        raise GrnIntakeError("The sheet needs a 'po_number' column")

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        normalised = {(k or "").strip().lower(): v for k, v in row.items()}
        po_number = (normalised.get("po_number") or normalised.get("po") or "").strip()
        if not po_number:
            continue
        key = (normalised.get("grn_number") or normalised.get("grn") or "").strip() or f"{po_number}"
        entry = grouped.setdefault(
            key,
            {
                "po_number": po_number,
                "grn_number": key if (normalised.get("grn_number") or normalised.get("grn")) else None,
                "receipt_date": (normalised.get("receipt_date") or normalised.get("date") or "").strip() or None,
                "note": "Imported from spreadsheet",
                "rows": [],
            },
        )
        entry["rows"].append(normalised)
    return list(grouped.values())
