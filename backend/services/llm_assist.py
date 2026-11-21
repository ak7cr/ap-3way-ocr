"""
LLM assistance — edges only.

The rules layer decides matches and approvals. The LLM is used for three narrow,
advisory jobs:
  1. explaining a mismatch in plain language,
  2. drafting the vendor query email,
  3. proposing fuzzy line mappings that the deterministic ladder could not resolve.

Every function has a deterministic fallback so the product works with no API key.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from ..config import LLM_ASSIST_MODEL
from .matching_engine import REASON_DESCRIPTIONS
from .normalization import LineMapping, map_invoice_lines_to_po, similarity


def _client(api_key: Optional[str]):
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai

        return genai.Client(api_key=key)
    except Exception as exc:  # google-genai not installed or bad key
        print(f"[LLM Assist] unavailable: {exc}")
        return None


def _generate(prompt: str, api_key: Optional[str]) -> Optional[str]:
    client = _client(api_key)
    if client is None:
        return None
    try:
        response = client.models.generate_content(model=LLM_ASSIST_MODEL, contents=prompt)
        text = getattr(response, "text", None)
        return text.strip() if text else None
    except Exception as exc:
        print(f"[LLM Assist] generation failed: {exc}")
        return None


def _fmt(value: Any) -> str:
    try:
        from decimal import Decimal

        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
# Deterministic fallbacks
# ---------------------------------------------------------------------------
def fallback_explanation(payload: Dict[str, Any]) -> str:
    """Plain-language summary built purely from the reason codes."""
    invoice = payload.get("invoice") or {}
    po = payload.get("po") or {}
    codes = payload.get("reason_codes") or []
    header = f"Invoice {invoice.get('invoice_number') or 'unknown'} from {invoice.get('vendor') or 'unknown vendor'}"
    if po.get("po_number"):
        header += f" does not reconcile against PO {po['po_number']}."
    else:
        header += " could not be matched to a purchase order."

    bullets = [f"- {REASON_DESCRIPTIONS.get(code, code)}" for code in codes]

    line_notes = []
    for line in payload.get("lines") or []:
        if not line.get("reason_codes"):
            continue
        inv = line.get("invoice_line") or {}
        po_line = line.get("po_line") or {}
        grn = line.get("grn") or {}
        line_notes.append(
            f"- Line {inv.get('line_no')} ({inv.get('item_code') or inv.get('description') or 'item'}): "
            f"invoiced {inv.get('quantity')} @ {_fmt(inv.get('rate'))}, "
            f"ordered {po_line.get('quantity', 'n/a')} @ {_fmt(po_line.get('rate'))}, "
            f"received {grn.get('received_qty', 'n/a')}."
        )

    parts = [header]
    if bullets:
        parts.append("\nIssues found:\n" + "\n".join(bullets))
    if line_notes:
        parts.append("\nLine detail:\n" + "\n".join(line_notes))
    parts.append(
        "\nSuggested next step: confirm the goods receipt quantity and agreed rate with the vendor, "
        "then either request a corrected invoice / credit note, or accept with an override and a reason."
    )
    return "\n".join(parts)


def fallback_vendor_email(payload: Dict[str, Any]) -> Dict[str, str]:
    invoice = payload.get("invoice") or {}
    po = payload.get("po") or {}
    number = invoice.get("invoice_number") or "your invoice"
    vendor = invoice.get("vendor") or "Sir/Madam"

    issues = []
    for line in payload.get("lines") or []:
        if not line.get("reason_codes"):
            continue
        inv = line.get("invoice_line") or {}
        po_line = line.get("po_line") or {}
        grn = line.get("grn") or {}
        issues.append(
            f"  - Line {inv.get('line_no')}: invoiced quantity {inv.get('quantity')} at rate "
            f"{_fmt(inv.get('rate'))}; our PO says {po_line.get('quantity', 'n/a')} at "
            f"{_fmt(po_line.get('rate'))}, and GRN shows {grn.get('received_qty', 'n/a')} received."
        )
    for code in payload.get("reason_codes") or []:
        if code.startswith("LINE_") or code.startswith("QTY_"):
            continue
        issues.append(f"  - {REASON_DESCRIPTIONS.get(code, code)}")

    body = (
        f"Dear {vendor},\n\n"
        f"We are unable to process invoice {number} as submitted"
        + (f" against purchase order {po.get('po_number')}" if po.get("po_number") else "")
        + ".\n\nThe following points need your confirmation or correction:\n"
        + ("\n".join(issues) if issues else "  - The invoice does not match our purchase order records.")
        + "\n\nCould you please issue a revised invoice or a credit note for the difference? "
        "We will release payment as soon as the corrected document is received.\n\n"
        "Thank you,\nAccounts Payable"
    )
    return {"subject": f"Query on invoice {number}", "body": body}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def explain_exception(payload: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
    fallback = fallback_explanation(payload)
    prompt = (
        "You are an accounts-payable assistant. Explain the following 3-way match exception in plain "
        "business language for a finance reviewer. Be specific, cite the numbers, and suggest the next "
        "action. Do not invent data. Keep it under 150 words.\n\n"
        + json.dumps(payload, default=str)[:6000]
    )
    generated = _generate(prompt, api_key)
    return {"explanation": generated or fallback, "generated_by": "llm" if generated else "rules"}


def draft_vendor_email(payload: Dict[str, Any], api_key: Optional[str] = None) -> Dict[str, Any]:
    fallback = fallback_vendor_email(payload)
    prompt = (
        "Write a short, polite accounts-payable email to the vendor about the following invoice exceptions. "
        "State the specific quantities/rates that differ, ask for a corrected invoice or credit note, and do "
        "not promise a payment date. Return JSON with keys 'subject' and 'body' only.\n\n"
        + json.dumps(payload, default=str)[:6000]
    )
    raw = _generate(prompt, api_key)
    if not raw:
        return {**fallback, "generated_by": "rules"}
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return {
            "subject": parsed.get("subject") or fallback["subject"],
            "body": parsed.get("body") or fallback["body"],
            "generated_by": "llm",
        }
    except json.JSONDecodeError:
        return {"subject": fallback["subject"], "body": raw, "generated_by": "llm"}


def suggest_line_mappings(invoice_lines: List[Any], po_lines: List[Any]) -> List[Dict[str, Any]]:
    """
    Advisory only. Returns fuzzy suggestions for lines the deterministic ladder
    left unmapped, so a human can confirm them. Never auto-applies a mapping.
    """
    mappings: List[LineMapping] = map_invoice_lines_to_po(invoice_lines, po_lines)
    suggestions: List[Dict[str, Any]] = []
    for mapping in mappings:
        if mapping.mapped:
            continue
        best, best_score = None, 0.0
        for po_line in po_lines:
            score = max(
                similarity(mapping.invoice_line.description, po_line.description),
                similarity(mapping.invoice_line.item_code, po_line.item_code),
            )
            if score > best_score:
                best, best_score = po_line, score
        if best is not None:
            suggestions.append(
                {
                    "invoice_line_no": mapping.invoice_line.line_no,
                    "invoice_description": mapping.invoice_line.description,
                    "suggested_po_line_no": best.line_no,
                    "suggested_po_description": best.description,
                    "confidence": round(best_score, 4),
                    "advisory": True,
                }
            )
    return suggestions


def classify_document_type(raw_text: str, api_key: Optional[str] = None) -> Optional[str]:
    """
    Tie-breaker when keyword scoring is ambiguous. Returns PURCHASE_ORDER, GRN,
    INVOICE or UNKNOWN. Advisory only: the caller keeps the deterministic result
    if this returns None.
    """
    sample = (raw_text or "").strip()[:4000]
    if not sample:
        return None
    prompt = (
        "Classify this business document. Answer with exactly one word: "
        "PURCHASE_ORDER, GRN (goods receipt note / material receipt), INVOICE, or UNKNOWN.\n\n"
        + sample
    )
    generated = _generate(prompt, api_key)
    if not generated:
        return None
    token = re.sub(r"[^A-Z_ ]", "", generated.upper()).strip().split()
    for candidate in token:
        if candidate in {"PURCHASE_ORDER", "PO", "GRN", "INVOICE", "UNKNOWN"}:
            return {"PO": "PURCHASE_ORDER"}.get(candidate, candidate)
    return None
