"""
Completion step for a forced-AI extraction.

When the user picks "Always use AI", the vision model runs directly and there is
no OCR pass or template path behind it. The model's fields are usable, but the
result must still carry the raw text, bounding boxes and an extraction-log row
or downstream ingest rejects it ("no text" / "no extraction record was stored").
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from ..config import ESCALATION, threshold_decision
from ..schemas import BoundingBox
from .template_engine import learn_layout_proposal, log_extraction_event
from .validator import validate_invoice


def _bounding_boxes(ai_res, words_data: Sequence[dict], img_w: float, img_h: float) -> list:
    boxes = []
    for word in words_data:
        label = "text"
        upper = word["text"].upper()
        if ai_res.metadata.invoice_number and ai_res.metadata.invoice_number in word["text"]:
            label = "invoice_number"
        elif ai_res.seller.name and any(p.upper() in upper for p in ai_res.seller.name.split() if len(p) > 2):
            label = "seller_name"
        elif ai_res.buyer.name and any(p.upper() in upper for p in ai_res.buyer.name.split() if len(p) > 2):
            label = "buyer_name"
        elif "TOTAL" in upper or "GROSS" in upper:
            label = "totals"
        elif any(
            (item.item_name or item.description or "")[:10].upper() in upper
            for item in ai_res.line_items
            if len(item.item_name or item.description or "") > 3
        ):
            label = "table_row"
        boxes.append(
            BoundingBox(
                label=label,
                text=word["text"],
                x0=round((word["x0"] / img_w) * 595.0, 2),
                y0=round((word["y0"] / img_h) * 842.0, 2),
                x1=round((word["x1"] / img_w) * 595.0, 2),
                y1=round((word["y1"] / img_h) * 842.0, 2),
                page=1,
                confidence=0.99,
            )
        )
    return boxes


def finalise_forced_ai(
    ai_res,
    *,
    content: bytes,
    db,
    raw_text: str,
    words_data: Optional[Sequence[dict]] = None,
    img_w: float = 0.0,
    img_h: float = 0.0,
    preview_urls: Optional[Sequence[str]] = None,
    preview_key: str = "",
    layout_score: float = 0.0,
    timing: Optional[dict] = None,
    bounding_boxes: Optional[list] = None,
    match_strategy: str = "AI_FORCED",
):
    """Attach the context and audit record a forced-AI result otherwise lacks."""
    ai_res.raw_text = raw_text or ai_res.raw_text
    if preview_urls:
        ai_res.document_preview_urls = list(preview_urls)
    if bounding_boxes is not None:
        ai_res.bounding_boxes = bounding_boxes
    elif words_data and img_w and img_h:
        ai_res.bounding_boxes = _bounding_boxes(ai_res, words_data, img_w, img_h)

    validation = validate_invoice(ai_res)
    ai_res.validation_summary = {
        "passed": validation.passed,
        "auto_acceptable": validation.auto_acceptable,
        "overall_confidence": validation.overall_confidence,
        "errors": validation.errors,
        "warnings": validation.warnings,
    }
    if timing is not None:
        ai_res.timing_breakdown = timing

    proposal = (
        learn_layout_proposal(ai_res, words_data, img_w, img_h, preview_url=preview_key or "")
        if validation.passed and words_data and img_w and img_h
        else None
    )
    ai_res.status = (
        "SUCCESS"
        if validation.overall_confidence >= ESCALATION.ai_accept and validation.passed
        else "NEEDS_REVIEW"
    )
    log_extraction_event(
        ai_res,
        template_id=None,
        match_strategy=match_strategy,
        validation=validation,
        raw_content=content,
        db=db,
        layout_match_score=layout_score,
        threshold_decision=threshold_decision(
            validation.overall_confidence, "ai_accept", None, layout_score
        ),
        proposed_template=proposal,
    )
    return ai_res
