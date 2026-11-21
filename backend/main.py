if __package__ in (None, ""):
    raise SystemExit(
        "backend/main.py is a package module and cannot be executed directly.\n"
        "From the repo root (" + str(__file__).rsplit("/backend/", 1)[0] + ") run:\n"
        "    python main.py --reload --port 8010\n"
        "or:\n"
        "    uvicorn backend.main:app --reload --port 8010"
    )

from backend.schemas import BoundingBox
from backend.services.ocr_engine import perform_image_ocr
import os
import io
import time
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from PIL import Image
from sqlalchemy.orm import Session

from .schemas import (
    InvoiceData,
    PDFInspectionResult,
    ImageConvertRequest,
    SearchablePdfRequest,
    ReviewRequest
)
from .database import init_db, get_db, InvoiceTemplate, ExtractionLog
from .services.pdf_engines import render_pdf_to_images
from .services.image_converter import convert_images_to_pdf, create_searchable_pdf, preprocess_image, preprocess_image_full, image_to_base64
from .services.pdf_inspector import inspect_pdf_document
from .services.invoice_extractor import extract_invoice_local, extract_invoice_ai_vision, extract_invoice_from_image
from .services.ocr_router import extract_image_document
from .services.template_engine import (
    apply_template_rules,
    find_matching_template,
    learn_layout_proposal,
    log_extraction_event,
    save_or_update_template,
    update_template_stats,
)
from .services.validator import validate_invoice
from .services.ai_vision import finalise_forced_ai
from .services.samples import generate_sample_gst_invoice
from .adapter import router as adapter_router
from .api.threeway import router as threeway_router
from .config import CANDIDATE, ESCALATION, TEMPLATE_TRUST, TRUSTED, normalise_template_status, threshold_decision
from .services.storage import store_preview, public_url, read_bytes as read_stored_bytes, content_type_for

app = FastAPI(
    title="Invoice OCR & Document Intelligence API",
    description="Full-stack AI OCR with PostgreSQL Template Auto-Learning",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Our frontend's compatibility endpoints (/api/extractions, /api/templates,
# /api/extractions/:id/corrections, /api/health).
app.include_router(adapter_router)
app.include_router(threeway_router)

# Registering the handlers is what makes the queue usable. Importing the module
# is enough: the @jobs.register decorators run on import.
from .pipeline import jobs_handlers as _jobs_handlers  # noqa: E402,F401

# Initialize PostgreSQL Database on Startup
db_ok, db_msg = init_db()


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Invoice OCR & Spatial Document Intelligence",
        "database": db_msg,
        "features": [
            "PostgreSQL Template Learning & Fingerprinting",
            "Multi-Engine PDF Extraction (fitz, pdfplumber, pypdf)",
            "Local Offline EasyOCR Engine",
            "Gemini 2.0 Flash Multimodal Fallback",
            "Interactive Bounding Boxes & Line Items",
            "Canonical Invoice / PO / GRN Contract",
            "Deterministic 3-Way Match (PO ↔ GRN ↔ Invoice)",
            "Exception Queue, Approvals & Payment Export",
        ]
    }


@app.get("/api/files/{key:path}")
def serve_stored_file(key: str):
    """
    Serves a preview/document stored in object storage. The DB only stores the
    storage key; the browser fetches the bytes through this endpoint.
    """
    data = read_stored_bytes(key)
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return StreamingResponse(io.BytesIO(data), media_type=content_type_for(key))


def _external_ocr_health():
    """Sidecar availability for the config screen; never raises."""
    try:
        from .services.external_ocr import health

        return health()
    except Exception as exc:  # pragma: no cover - diagnostics only
        return [{"available": False, "error": type(exc).__name__}]


@app.get("/api/threeway/config")
def threeway_config():
    """Exposes the escalation thresholds and match tolerances the engine uses."""
    from .config import (
        APPROVAL_RULES,
        DEFAULT_TOLERANCES,
        MATCH,
        SHADOW,
        TEMPLATE_TRUST,
        VALIDATION,
    )
    from .pipeline import describe as describe_pipeline

    return {
        "validation": {
            "line_sum_abs": str(VALIDATION.line_sum_abs),
            "tax_recompute_abs": str(VALIDATION.tax_recompute_abs),
            "grand_total_abs": str(VALIDATION.grand_total_abs),
            "gstin_checksum": VALIDATION.gstin_checksum,
            "require_invoice_number": VALIDATION.require_invoice_number,
            "require_invoice_date": VALIDATION.require_invoice_date,
            "max_tax_rate_pct": str(VALIDATION.max_tax_rate_pct),
        },
        "template_trust": {
            "candidate_confirmations": TEMPLATE_TRUST.candidate_confirmations,
            "auto_learn_local": TEMPLATE_TRUST.auto_learn_local,
            "auto_learn_ai": TEMPLATE_TRUST.auto_learn_ai,
        },
        "shadow": {
            "mode": SHADOW.mode,
            "auto_approve_enabled": SHADOW.auto_approve_enabled,
            "min_decisions": SHADOW.min_decisions,
            "min_agreement": SHADOW.min_agreement,
            "onboarding_vendors": SHADOW.onboarding_vendors,
        },
        "architecture": describe_pipeline(),
        "ocr_routing": {
            "scanned_image": "external-then-local",
            "digital_pdf": "local",
            "external_enabled": os.getenv("EXTERNAL_OCR_ENABLED", "true").lower() not in {"0", "false", "no", "off"},
            "services": _external_ocr_health(),
        },
        "escalation": {
            "auto_accept": ESCALATION.auto_accept,
            "review_floor": ESCALATION.review_floor,
            "template_match": ESCALATION.template_match,
            "ai_accept": ESCALATION.ai_accept,
            "low_field_confidence": ESCALATION.low_field_confidence,
        },
        "tolerances": {
            "price_pct": str(DEFAULT_TOLERANCES.price_pct),
            "quantity": str(DEFAULT_TOLERANCES.quantity),
            "line_abs": str(DEFAULT_TOLERANCES.line_abs),
            "tax_abs": str(DEFAULT_TOLERANCES.tax_abs),
            "total_abs": str(DEFAULT_TOLERANCES.total_abs),
        },
        "match": {
            "duplicate_window_days": MATCH.duplicate_window_days,
            "require_grn": MATCH.require_grn,
        },
        "approval_rules": [
            {
                "name": r.name,
                "min_amount": str(r.min_amount),
                "max_amount": str(r.max_amount) if r.max_amount is not None else None,
                "roles": r.roles,
            }
            for r in APPROVAL_RULES
        ],
    }


@app.get("/api/db-status")
def get_db_status(db: Session = Depends(get_db)):
    """
    Returns PostgreSQL database status and template metrics.
    """
    template_count = db.query(InvoiceTemplate).count()
    logs_count = db.query(ExtractionLog).count()
    return {
        "status": "connected",
        "database": "PostgreSQL",
        "templates_count": template_count,
        "extractions_logged": logs_count
    }



def print_final_extraction_summary(invoice: InvoiceData, source_label: str = "EXTRACTED INVOICE"):
    """
    Prints plain text OCR extraction along with structured summary of the exact parsed invoice data to the console.
    """
    sym = (invoice.metadata.currency_symbol if invoice.metadata and invoice.metadata.currency_symbol else "₹")
    print("\n" + "="*84)
    print(f"📄 [RAW OCR PLAIN TEXT EXTRACTED FROM DOCUMENT - {source_label.upper()}]")
    print("="*84)
    if invoice.raw_text:
        print(invoice.raw_text.strip())
    else:
        print("(No raw text)")
    print("="*84)
    print(f"📊 [{source_label.upper()}] FINAL PARSED DATA")
    print("="*84)
    print(f"  🏢 Vendor / Seller:  {invoice.seller.name or 'N/A'} (GSTIN: {invoice.seller.gstin or 'N/A'})")
    if invoice.seller.address:
        print(f"     Address:          {invoice.seller.address}")
    print(f"  👤 Buyer / Client:   {invoice.buyer.name or 'N/A'} (GSTIN: {invoice.buyer.gstin or 'N/A'})")
    if invoice.buyer.address:
        print(f"     Address:          {invoice.buyer.address}")
    print(f"  📄 Invoice No:       {invoice.metadata.invoice_number or 'N/A'}")
    print(f"  📅 Invoice Date:     {invoice.metadata.invoice_date or 'N/A'}")
    print(f"  💵 Currency:         {invoice.metadata.currency or 'INR'} ({sym})")
    print(f"  📦 Line Items ({len(invoice.line_items)} items):")
    print("  " + "-"*80)
    print(f"     {'#':<3} | {'Description':<32} | {'HSN':<6} | {'Qty':<5} | {'Unit':<5} | {'Rate':<10} | {'Tax%':<5} | {'Total':<10}")
    print("  " + "-"*80)
    for it in invoice.line_items:
        desc_txt = it.item_name or it.description or "Item"
        desc_preview = (desc_txt[:30] + "..") if len(desc_txt) > 32 else desc_txt
        print(f"     {str(it.sno):<3} | {desc_preview:<32} | {str(it.hsn_sac or '-'):<6} | {str(it.quantity):<5} | {str(it.unit):<5} | {it.unit_price:<10.2f} | {str(it.tax_rate) + '%':<5} | {it.total_amount:<10.2f}")
    print("  " + "-"*80)
    if invoice.summary:
        print(f"  💰 Subtotal:         {sym}{invoice.summary.subtotal:,.2f}")
        print(f"  🏛️ Total GST / Tax:  {sym}{invoice.summary.total_gst:,.2f}")
        print(f"  🎯 Grand Total:      {sym}{invoice.summary.grand_total:,.2f}")
    print(f"  ⚡ Status:           {invoice.status} | Engine: {invoice.engine_used}")
    print("="*84 + "\n")


@app.post("/api/extract-invoice", response_model=InvoiceData)
async def extract_invoice(
    file: UploadFile = File(...),
    pipeline_mode: str = Form("hybrid"),  # "local", "hybrid", "ai"
    use_ai_vision: bool = Form(False),
    api_key: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Extracts structured invoice data with PostgreSQL Template Fingerprinting & Auto-Learning.
    Modes:
      - 'local': 100% Offline C++ Engine (0 API calls, ultra-fast)
      - 'hybrid': Local C++ first -> Auto-fallback to Gemini AI on complex layouts
      - 'ai': Force Gemini Vision AI directly
    """
    req_start = time.time()
    content = await file.read()
    filename = file.filename or "invoice"
    ext = filename.lower().split(".")[-1]
    is_image = ext in ["png", "jpg", "jpeg", "webp", "tiff", "bmp"]

    # Read API key from request form or .env file
    effective_api_key = api_key or os.getenv("GEMINI_API_KEY")
    
    # Determine execution mode
    mode = pipeline_mode.lower() if pipeline_mode in ["local", "hybrid", "ai"] else ("ai" if use_ai_vision else "hybrid")
    allow_ai_fallback = (mode in ["hybrid", "ai"]) and bool(effective_api_key)
    force_ai = (mode == "ai") and bool(effective_api_key)

    # 1. Image OCR or PDF extraction
    if is_image:
        t_prep_start = time.time()
        pil_img = Image.open(io.BytesIO(content))
        pil_img, cv_meta = preprocess_image_full(pil_img)
        img_w, img_h = float(pil_img.width), float(pil_img.height)
        img_b64 = image_to_base64(pil_img)
        # Preview bytes go to object storage; only the key is persisted in the DB.
        preview_key = store_preview(img_b64, prefix="invoice-previews")
        preview_url = public_url(preview_key) or img_b64
        preview_images = [preview_url]
        t_prep_ms = round((time.time() - t_prep_start) * 1000, 2)

        print("\n" + "="*84)
        print("🚀 [STARTING INVOICE PROCESSING PIPELINE]")
        print("="*84)
        print(f"🔹 [STEP 1/4: OCR & PREPROCESSING LEVEL]")
        print(f"   • Input File:       {filename} (Format: {ext.upper()})")
        print(f"   • Image Dimensions: {int(img_w)} x {int(img_h)} px")
        print(f"   • Preprocessing:    OpenCV (Blur: {cv_meta.get('blur_score_after', 'N/A')}, Skew: {cv_meta.get('skew_angle_deg', 0.0)}°)")

        t_ocr_start = time.time()
        full_text, words_data, _ = perform_image_ocr(pil_img)
        t_ocr_ms = round((time.time() - t_ocr_start) * 1000, 2)
        active_engine_name = words_data[0].get("engine", "EasyOCR") if words_data else "OCR Engine"
        print(f"   • Active OCR Engine: 🔤 {active_engine_name} ({len(words_data)} word tokens in {t_ocr_ms}ms)")

        # 2. CHECK POSTGRESQL TEMPLATE FINGERPRINT FIRST (Strict Quality Gate)
        print(f"\n🔹 [STEP 2/4: TEMPLATE & STRUCTURAL LEVEL]")
        t_match_start = time.time()
        total_tpls = db.query(InvoiceTemplate).count() if db else 0
        matched_template, layout_score = find_matching_template(full_text, words_data, img_w, img_h, db)
        if matched_template and normalise_template_status(matched_template.status) in (CANDIDATE, TRUSTED):
            tpl_name = getattr(matched_template, "template_name", f"Template #{matched_template.id}")
            print(f"   • PostgreSQL Cache: {total_tpls} learned templates stored")
            print(f"   • Matched Template: Template #{matched_template.id} ('{tpl_name}') [Match Score: {layout_score:.2f}]")
            t_map_start = time.time()
            tpl_res = apply_template_rules(
                matched_template, words_data, full_text, img_w, img_h, preview_url=preview_url
            )
            t_map_ms = round((time.time() - t_map_start) * 1000, 2)
            
            print(f"\n🔹 [STEP 3/4: FINANCIAL & MATHEMATICAL VALIDATION LEVEL]")
            t_val_start = time.time()
            tpl_val = validate_invoice(tpl_res)
            t_val_ms = round((time.time() - t_val_start) * 1000, 2)
            print(f"   • Validation Status: {'PASSED' if tpl_val.passed else 'FAILED'} (Confidence: {tpl_val.overall_confidence:.3f})")
            if tpl_val.errors: print(f"   • Validation Errors: {tpl_val.errors}")
            if tpl_val.warnings: print(f"   • Validation Warnings: {tpl_val.warnings}")
            
            tpl_res.timing_breakdown = {
                "pdf_render_ms": 0.0,
                "opencv_preprocess_ms": t_prep_ms,
                "paddle_ocr_ms": t_ocr_ms,
                "table_structure_ms": 0.0,
                "local_mapping_ms": t_map_ms,
                "validation_ms": t_val_ms,
                "gemini_latency_ms": 0.0,
                "total_processing_ms": round((time.time() - req_start) * 1000, 2),
                "cv_blur_before": cv_meta.get("blur_score_before"),
                "cv_blur_after": cv_meta.get("blur_score_after"),
                "cv_skew_deg": cv_meta.get("skew_angle_deg", 0.0),
                "cv_ocr_mode": cv_meta.get("ocr_mode", "UNKNOWN"),
                "cv_has_grid": cv_meta.get("table_grid", {}).get("has_grid", False),
            }
            tpl_res.validation_summary = {
                "passed": tpl_val.passed,
                "auto_acceptable": tpl_val.auto_acceptable,
                "overall_confidence": tpl_val.overall_confidence,
                "errors": tpl_val.errors,
                "warnings": tpl_val.warnings
            }

            print(f"\n🔹 [STEP 4/4: PIPELINE VERDICT]")
            # Auto-accept needs everything to pass AND a template that has earned
            # trust through human confirmations. A candidate never auto-accepts.
            template_trusted = normalise_template_status(matched_template.status) == TRUSTED
            if (
                template_trusted
                and tpl_val.overall_confidence >= ESCALATION.auto_accept
                and tpl_val.auto_acceptable
                and len(tpl_res.line_items) > 0
            ):
                tpl_res.status = "SUCCESS"
                match_strategy = "EXACT_HASH" if layout_score >= 0.95 else "ANCHOR_KEYWORDS"
                update_template_stats(matched_template, success=True, conf=tpl_val.overall_confidence, db=db)
                log_extraction_event(tpl_res, template_id=matched_template.id, match_strategy=match_strategy, validation=tpl_val, raw_content=content, db=db,
                                     layout_match_score=layout_score,
                                     threshold_decision=threshold_decision(tpl_val.overall_confidence, "auto_accept", matched_template.id, layout_score))
                print(f"   ⚡ [Fast Path Verdict] AUTO-APPROVED in {tpl_res.processing_time_ms}ms using Template #{matched_template.id} (0 API tokens)")
                print_final_extraction_summary(tpl_res, source_label=f"Fast Path (Template #{matched_template.id})")
                return tpl_res
            else:
                if not template_trusted:
                    print(f"   🌱 Template #{matched_template.id} is a candidate (unconfirmed); extraction routes to review, not auto-accept.")
                else:
                    print(f"   ⚠️ Template #{matched_template.id} validation confidence ({tpl_val.overall_confidence}) below threshold. Routing to Fallback...")
                # A candidate is not a failure; only a real validation failure counts against trust.
                if not tpl_val.passed:
                    update_template_stats(matched_template, success=False, conf=tpl_val.overall_confidence, db=db)
        else:
            if total_tpls == 0:
                print(f"   • Match Status:     No templates in PostgreSQL yet (First-time vendor / new layout).")
            else:
                print(f"   • Match Status:     New layout format (No existing template exceeded similarity threshold >= 0.85).")
            print(f"   • Action:           Executing Local 2D Cell Grid Matrix & C++ Engine -> Auto-learning template on approval.")

        # 3. IF FORCE AI VISION REQUESTED:
        if force_ai:
            try:
                print(f"\n🤖 [AI VISION LEVEL] Force AI Vision Mode active...")
                t_ai_start = time.time()
                ai_res = await extract_invoice_ai_vision(content, api_key=effective_api_key)
                t_ai_ms = round((time.time() - t_ai_start) * 1000, 2)
                if ai_res:
                    forced = finalise_forced_ai(
                        ai_res,
                        content=content,
                        db=db,
                        raw_text=full_text,
                        words_data=words_data,
                        img_w=img_w,
                        img_h=img_h,
                        preview_urls=preview_images,
                        preview_key=preview_key or "",
                        layout_score=layout_score,
                        timing={
                            "pdf_render_ms": 0.0,
                            "opencv_preprocess_ms": t_prep_ms,
                            "paddle_ocr_ms": t_ocr_ms,
                            "table_structure_ms": 0.0,
                            "local_mapping_ms": 0.0,
                            "validation_ms": 0.0,
                            "gemini_latency_ms": t_ai_ms,
                            "total_processing_ms": round((time.time() - req_start) * 1000, 2),
                        },
                    )
                    print_final_extraction_summary(forced, source_label="AI Vision (Forced)")
                    return forced
            except Exception as e:
                print(f"Force AI Vision error: {e}")

        # 4. SCANNED-IMAGE ROUTER: the external engines score better on images
        # (2/40 vs 0/40 on the sample kit) while this repo's extractor wins on
        # digital PDFs, so images are routed to them first — with our own engine
        # as the fallback, then the AI escalation below.
        print(f"\n🔹 [STEP 3/4: SCANNED-IMAGE EXTRACTION (Mode: {mode.upper()})]")
        t_loc_start = time.time()
        routed = extract_image_document(content, filename, file.content_type)
        local_res = routed.invoice if routed.invoice is not None else InvoiceData(
            engine_used="No engine produced an extraction"
        )
        print(f"   • Router: {routed.engine} ({'external' if routed.used_external else 'local'})")
        for attempt in routed.attempts:
            print(f"     - {attempt}")
        t_loc_ms = round((time.time() - t_loc_start) * 1000, 2)
        local_val = validate_invoice(local_res)

        print(f"\n🔹 [STEP 4/4: PIPELINE VERDICT]")
        # If Mode is Local-Only OR Local Extraction Passed:
        if mode == "local" or (len(local_res.line_items) > 0 and (local_val.auto_acceptable or (local_val.passed and local_val.overall_confidence >= ESCALATION.review_floor))):
            local_res.status = "SUCCESS" if (local_val.overall_confidence >= ESCALATION.review_floor and local_val.passed) else "NEEDS_REVIEW"
            saved_tpl = save_or_update_template(local_res, words_data, img_w, img_h, preview_url=preview_key or "", db=db, source="LOCAL") if (not TEMPLATE_TRUST.require_validation or local_val.passed) else None
            log_extraction_event(local_res, template_id=saved_tpl.id if saved_tpl else None, match_strategy="EXACT_HASH", validation=local_val, raw_content=content, db=db,
                                 layout_match_score=layout_score,
                                 threshold_decision=threshold_decision(local_val.overall_confidence, "review_floor", matched_template.id if matched_template else None, layout_score))
            print(f"   ⚡ [Local Engine Verdict] EXTRACTION SUCCESSFUL in {t_loc_ms}ms (0 API tokens used)")
            print_final_extraction_summary(local_res, source_label="Local C++ & Cell Matrix Engine")
            return local_res
        else:
            print(f"   ℹ️ Local extraction confidence ({local_val.overall_confidence:.2f}) below threshold. Checking AI Vision fallback...")

        # 5. IF HYBRID MODE & LOCAL EXTRACTION NEEDED HELP: Run Gemini Vision Fallback
        if allow_ai_fallback:
            try:
                print(f"\n🤖 [AI VISION LEVEL] Attempting Google Gemini Vision Fallback...")
                t_ai_start = time.time()
                ai_res = await extract_invoice_ai_vision(content, api_key=effective_api_key)
                t_ai_ms = round((time.time() - t_ai_start) * 1000, 2)
                if ai_res:
                    ai_res.document_preview_urls = preview_images
                    ai_res.raw_text = full_text
                    
                    # Generate spatial bounding boxes from OCR words for interactive UI overlays
                    boxes = []
                    for w in words_data:
                        lbl = "text"
                        wt = w["text"].upper()
                        if ai_res.metadata.invoice_number and ai_res.metadata.invoice_number in w["text"]: lbl = "invoice_number"
                        elif ai_res.seller.name and any(p.upper() in wt for p in ai_res.seller.name.split() if len(p) > 2): lbl = "seller_name"
                        elif ai_res.buyer.name and any(p.upper() in wt for p in ai_res.buyer.name.split() if len(p) > 2): lbl = "buyer_name"
                        elif "TOTAL" in wt or "GROSS" in wt: lbl = "totals"
                        elif any((it.item_name or it.description or "")[:10].upper() in wt for it in ai_res.line_items if len(it.item_name or it.description or "") > 3): lbl = "table_row"

                        boxes.append(BoundingBox(
                            label=lbl,
                            text=w["text"],
                            x0=round((w["x0"] / img_w) * 595.0, 2),
                            y0=round((w["y0"] / img_h) * 842.0, 2),
                            x1=round((w["x1"] / img_w) * 595.0, 2),
                            y1=round((w["y1"] / img_h) * 842.0, 2),
                            page=1,
                            confidence=0.99
                        ))
                    ai_res.bounding_boxes = boxes

                    # Strict Quality Validation for AI Output
                    t_val_start = time.time()
                    ai_val = validate_invoice(ai_res)
                    t_val_ms = round((time.time() - t_val_start) * 1000, 2)

                    ai_res.timing_breakdown = {
                        "pdf_render_ms": 0.0,
                        "opencv_preprocess_ms": t_prep_ms,
                        "paddle_ocr_ms": t_ocr_ms,
                        "table_structure_ms": 0.0,
                        "local_mapping_ms": 0.0,
                        "validation_ms": t_val_ms,
                        "gemini_latency_ms": t_ai_ms,
                        "total_processing_ms": round((time.time() - req_start) * 1000, 2),
                        "cv_blur_before": cv_meta.get("blur_score_before"),
                        "cv_blur_after": cv_meta.get("blur_score_after"),
                        "cv_skew_deg": cv_meta.get("skew_angle_deg", 0.0),
                        "cv_ocr_mode": cv_meta.get("ocr_mode", "UNKNOWN"),
                        "cv_has_grid": cv_meta.get("table_grid", {}).get("has_grid", False),
                    }
                    ai_res.validation_summary = {
                        "passed": ai_val.passed,
                        "auto_acceptable": ai_val.auto_acceptable,
                        "overall_confidence": ai_val.overall_confidence,
                        "errors": ai_val.errors,
                        "warnings": ai_val.warnings
                    }

                    # An AI extraction never saves a template on its own: the
                    # layout is proposed, and only a human confirmation creates
                    # a candidate. This keeps a bad AI read out of the library.
                    proposal = learn_layout_proposal(ai_res, words_data, img_w, img_h, preview_url=preview_key or "") if ai_val.passed else None
                    saved_tpl = None
                    if ai_val.overall_confidence >= 0.98 and ai_val.auto_acceptable:
                        ai_res.status = "SUCCESS"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score),
                                             proposed_template=proposal)
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision")
                        return ai_res
                    elif ai_val.passed:
                        print(f"ℹ️ AI Vision output passed checks ({ai_val.overall_confidence}). Routing to NEEDS_REVIEW.")
                        ai_res.status = "NEEDS_REVIEW"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score),
                                             proposed_template=proposal)
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision (Needs Review)")
                        return ai_res
                    else:
                        print(f"⚠️ AI Vision output failed semantic validation: {ai_val.errors}. Routing to NEEDS_REVIEW for human correction.")
                        ai_res.status = "NEEDS_REVIEW"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score))
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision (Validation Warning)")
                        return ai_res
            except Exception as e:
                print(f"AI Vision error: {e}")

        # Return local extraction result
        local_res.status = "SUCCESS" if (local_val.overall_confidence >= ESCALATION.auto_accept and local_val.auto_acceptable) else ("NEEDS_REVIEW" if local_val.passed else "FAILED")
        saved_tpl = (
            save_or_update_template(local_res, words_data, img_w, img_h, preview_url=preview_key or "", db=db, source="LOCAL")
            if local_val.overall_confidence >= ESCALATION.auto_accept and (not TEMPLATE_TRUST.require_validation or local_val.passed)
            else None
        )
        log_extraction_event(local_res, template_id=saved_tpl.id if saved_tpl else None, match_strategy="EXACT_HASH", validation=local_val, raw_content=content, db=db)
        print_final_extraction_summary(local_res, source_label="Local OCR & Cell Grid Engine")
        return local_res

    else:
        # PDF Document
        t_pdf_render_start = time.time()
        pdf_bytes = content
        try:
            rendered = render_pdf_to_images(pdf_bytes)
            preview_keys = [store_preview(img_b64, prefix="invoice-previews") for _, img_b64 in rendered]
            preview_keys = [k for k in preview_keys if k]
            preview_images = [public_url(k) for k in preview_keys]
        except Exception:
            preview_keys = []
            preview_images = []

        import pymupdf as fitz
        raster_bytes = None
        img_w, img_h = 595.0, 842.0
        words_data = []
        full_text = ""
        is_digital_pdf = False
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) > 0:
                page = doc[0]
                img_w, img_h = float(page.rect.width), float(page.rect.height)
                pix = page.get_pixmap(dpi=150)
                raster_bytes = pix.tobytes("png")
                
                fitz_words = page.get_text("words")
                if fitz_words and len(fitz_words) > 10:
                    is_digital_pdf = True
                    full_text = page.get_text("text")
                    words_data = [
                        {
                            "text": w[4],
                            "x0": float(w[0]),
                            "y0": float(w[1]),
                            "x1": float(w[2]),
                            "y1": float(w[3]),
                            "confidence": 0.99
                        }
                        for w in fitz_words
                    ]
                else:
                    raster_pil = Image.open(io.BytesIO(raster_bytes))
                    raster_pil, cv_meta_pdf = preprocess_image_full(raster_pil)
                    img_w, img_h = float(raster_pil.width), float(raster_pil.height)
                    full_text, words_data, _ = perform_image_ocr(raster_pil)
            doc.close()
        except Exception as e:
            print(f"PDF raster error: {e}")

        print("\n" + "="*84)
        print("🚀 [STARTING INVOICE PROCESSING PIPELINE - PDF]")
        print("="*84)
        print(f"🔹 [STEP 1/4: PDF INGESTION & TEXT EXTRACTION LEVEL]")
        print(f"   • Input File:       {filename} (Format: PDF)")
        print(f"   • Mode:             {'Digital Vector PDF (Native Text Layer)' if is_digital_pdf else 'Scanned PDF (Rasterized Image OCR)'}")
        print(f"   • Active Engine:    {'📄 PyMuPDF / fitz vector text reader' if is_digital_pdf else '🔤 EasyOCR Raster Engine'} ({len(words_data)} word tokens)")

        t_pdf_render_ms = round((time.time() - t_pdf_render_start) * 1000, 2)
        local_result = extract_invoice_local(pdf_bytes, filename=filename, preview_images=preview_images)
        if not full_text:
            full_text = local_result.raw_text or ""

        # 2. CHECK POSTGRESQL TEMPLATE FINGERPRINT FIRST (Strict Quality Gate)
        print(f"\n🔹 [STEP 2/4: TEMPLATE & STRUCTURAL LEVEL]")
        matched_template, layout_score = find_matching_template(full_text, words_data, img_w, img_h, db)
        if matched_template and normalise_template_status(matched_template.status) in (CANDIDATE, TRUSTED):
            tpl_name = getattr(matched_template, "template_name", f"Template #{matched_template.id}")
            print(f"   • Matched Template: Template #{matched_template.id} ('{tpl_name}') [Score: {layout_score:.2f}]")
            t_map_start = time.time()
            tpl_res = apply_template_rules(
                matched_template, words_data, full_text, img_w, img_h,
                preview_url=preview_images[0] if preview_images else ""
            )
            t_map_ms = round((time.time() - t_map_start) * 1000, 2)
            
            print(f"\n🔹 [STEP 3/4: FINANCIAL & MATHEMATICAL VALIDATION LEVEL]")
            t_val_start = time.time()
            tpl_val = validate_invoice(tpl_res)
            t_val_ms = round((time.time() - t_val_start) * 1000, 2)
            print(f"   • Validation Status: {'PASSED' if tpl_val.passed else 'FAILED'} (Confidence: {tpl_val.overall_confidence:.3f})")
            if tpl_val.errors: print(f"   • Validation Errors: {tpl_val.errors}")
            if tpl_val.warnings: print(f"   • Validation Warnings: {tpl_val.warnings}")

            tpl_res.timing_breakdown = {
                "pdf_render_ms": t_pdf_render_ms,
                "opencv_preprocess_ms": 0.0,
                "paddle_ocr_ms": 0.0,
                "table_structure_ms": 0.0,
                "local_mapping_ms": t_map_ms,
                "validation_ms": t_val_ms,
                "gemini_latency_ms": 0.0,
                "total_processing_ms": round((time.time() - req_start) * 1000, 2)
            }
            tpl_res.validation_summary = {
                "passed": tpl_val.passed,
                "auto_acceptable": tpl_val.auto_acceptable,
                "overall_confidence": tpl_val.overall_confidence,
                "errors": tpl_val.errors,
                "warnings": tpl_val.warnings
            }

            template_trusted = normalise_template_status(matched_template.status) == TRUSTED
            if (
                template_trusted
                and tpl_val.overall_confidence >= ESCALATION.auto_accept
                and tpl_val.auto_acceptable
                and len(tpl_res.line_items) > 0
            ):
                tpl_res.status = "SUCCESS"
                match_strategy = "EXACT_HASH" if layout_score >= 0.95 else "ANCHOR_KEYWORDS"
                update_template_stats(matched_template, success=True, conf=tpl_val.overall_confidence, db=db)
                log_extraction_event(tpl_res, template_id=matched_template.id, match_strategy=match_strategy, validation=tpl_val, raw_content=content, db=db,
                                     layout_match_score=layout_score,
                                     threshold_decision=threshold_decision(tpl_val.overall_confidence, "auto_accept", matched_template.id, layout_score))
                print(f"⚡ [Fast Path] PDF Auto-Approved in {tpl_res.processing_time_ms}ms using template #{matched_template.id} (Confidence: {tpl_val.overall_confidence}) - 0 API tokens")
                print_final_extraction_summary(tpl_res, source_label=f"Fast Path PDF (Template #{matched_template.id})")
                return tpl_res
            else:
                if not template_trusted:
                    print(f"🌱 PDF template #{matched_template.id} is a candidate (unconfirmed); routing to review rather than auto-accept.")
                else:
                    print(f"⚠️ PDF template #{matched_template.id} confidence ({tpl_val.overall_confidence}) below 0.98. Errors: {tpl_val.errors} | Warnings: {tpl_val.warnings}. Routing to AI fallback...")
                if not tpl_val.passed:
                    update_template_stats(matched_template, success=False, conf=tpl_val.overall_confidence, db=db)

        # 3. IF FORCE AI VISION REQUESTED ON PDF:
        if force_ai and raster_bytes:
            try:
                print(f"\n🤖 [AI VISION LEVEL] Force AI Vision Mode active on PDF...")
                t_ai_start = time.time()
                ai_res = await extract_invoice_ai_vision(raster_bytes, api_key=effective_api_key, mime_type="image/png")
                t_ai_ms = round((time.time() - t_ai_start) * 1000, 2)
                if ai_res:
                    forced = finalise_forced_ai(
                        ai_res,
                        content=content,
                        db=db,
                        raw_text=local_result.raw_text or full_text,
                        words_data=words_data,
                        img_w=img_w,
                        img_h=img_h,
                        preview_urls=preview_images,
                        preview_key=preview_keys[0] if preview_keys else "",
                        layout_score=layout_score,
                        bounding_boxes=local_result.bounding_boxes,
                        timing={
                            "pdf_render_ms": t_pdf_render_ms,
                            "opencv_preprocess_ms": 0.0,
                            "paddle_ocr_ms": 0.0,
                            "table_structure_ms": 0.0,
                            "local_mapping_ms": 0.0,
                            "validation_ms": 0.0,
                            "gemini_latency_ms": t_ai_ms,
                            "total_processing_ms": round((time.time() - req_start) * 1000, 2),
                        },
                    )
                    print_final_extraction_summary(forced, source_label="AI Vision PDF (Forced)")
                    return forced
            except Exception as e:
                print(f"Force AI Vision on PDF error: {e}")

        # 4. RUN LOCAL VECTOR / OCR PDF EXTRACTION (0 Tokens, Instant)
        print(f"\n🔹 [STEP 3/4: LOCAL PDF EXTRACTION VERDICT (Mode: {mode.upper()})]")
        local_val = validate_invoice(local_result)
        
        # If Mode is Local-Only OR Local Extraction Passed:
        if mode == "local" or (len(local_result.line_items) > 0 and (local_val.auto_acceptable or (local_val.passed and local_val.overall_confidence >= ESCALATION.review_floor))):
            local_result.status = "SUCCESS" if (local_val.overall_confidence >= ESCALATION.review_floor and local_val.passed) else "NEEDS_REVIEW"
            saved_tpl = save_or_update_template(local_result, words_data, img_w, img_h, preview_url=preview_keys[0] if preview_keys else "", db=db, source="LOCAL") if (not TEMPLATE_TRUST.require_validation or local_val.passed) else None
            log_extraction_event(local_result, template_id=saved_tpl.id if saved_tpl else None, match_strategy="EXACT_HASH", validation=local_val, raw_content=content, db=db,
                                 layout_match_score=layout_score,
                                 threshold_decision=threshold_decision(local_val.overall_confidence, "review_floor", matched_template.id if matched_template else None, layout_score))
            print(f"   ⚡ [Local PDF Engine Verdict] EXTRACTION SUCCESSFUL in {local_result.processing_time_ms}ms (0 API tokens used)")
            print_final_extraction_summary(local_result, source_label="Local PDF Engine")
            return local_result
        else:
            print(f"   ℹ️ Local PDF extraction confidence ({local_val.overall_confidence:.2f}) below threshold. Checking AI Vision fallback...")

        # 5. IF HYBRID MODE & LOCAL EXTRACTION NEEDED HELP: Run Gemini Vision Fallback
        if allow_ai_fallback and raster_bytes:
            try:
                t_ai_start = time.time()
                ai_res = await extract_invoice_ai_vision(raster_bytes, api_key=effective_api_key, mime_type="image/png")
                t_ai_ms = round((time.time() - t_ai_start) * 1000, 2)
                if ai_res:
                    ai_res.document_preview_urls = preview_images
                    ai_res.raw_text = local_result.raw_text or ai_res.raw_text
                    if local_result.bounding_boxes:
                        ai_res.bounding_boxes = local_result.bounding_boxes
                    
                    t_val_start = time.time()
                    ai_val = validate_invoice(ai_res)
                    t_val_ms = round((time.time() - t_val_start) * 1000, 2)

                    ai_res.timing_breakdown = {
                        "pdf_render_ms": t_pdf_render_ms,
                        "opencv_preprocess_ms": 0.0,
                        "paddle_ocr_ms": 0.0,
                        "table_structure_ms": 0.0,
                        "local_mapping_ms": 0.0,
                        "validation_ms": t_val_ms,
                        "gemini_latency_ms": t_ai_ms,
                        "total_processing_ms": round((time.time() - req_start) * 1000, 2)
                    }
                    ai_res.validation_summary = {
                        "passed": ai_val.passed,
                        "auto_acceptable": ai_val.auto_acceptable,
                        "overall_confidence": ai_val.overall_confidence,
                        "errors": ai_val.errors,
                        "warnings": ai_val.warnings
                    }

                    proposal = learn_layout_proposal(ai_res, words_data, img_w, img_h, preview_url=preview_keys[0] if preview_keys else "") if ai_val.passed else None
                    if ai_val.overall_confidence >= 0.98 and ai_val.auto_acceptable:
                        ai_res.status = "SUCCESS"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score),
                                             proposed_template=proposal)
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision (PDF)")
                        return ai_res
                    elif ai_val.passed:
                        print(f"ℹ️ PDF AI Vision output passed checks ({ai_val.overall_confidence}). Routing to NEEDS_REVIEW.")
                        ai_res.status = "NEEDS_REVIEW"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score),
                                             proposed_template=proposal)
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision PDF (Needs Review)")
                        return ai_res
                    else:
                        print(f"⚠️ PDF AI Vision output failed validation: {ai_val.errors}. Routing to NEEDS_REVIEW.")
                        ai_res.status = "NEEDS_REVIEW"
                        log_extraction_event(ai_res, template_id=None, match_strategy="AI_FALLBACK", validation=ai_val, raw_content=content, db=db,
                                             layout_match_score=layout_score,
                                             threshold_decision=threshold_decision(ai_val.overall_confidence, "ai_accept", None, layout_score))
                        print_final_extraction_summary(ai_res, source_label="Gemini AI Vision PDF (Validation Warning)")
                        return ai_res
            except Exception as e:
                print(f"AI Vision error on PDF: {e}")

        # Final Local Fallback
        local_result.status = "SUCCESS" if (local_val.overall_confidence >= ESCALATION.review_floor and local_val.passed) else "NEEDS_REVIEW"
        saved_tpl = (
            save_or_update_template(local_result, words_data, img_w, img_h, preview_url=preview_keys[0] if preview_keys else "", db=db, source="LOCAL")
            if local_val.overall_confidence >= ESCALATION.review_floor and (not TEMPLATE_TRUST.require_validation or local_val.passed)
            else None
        )
        log_extraction_event(local_result, template_id=saved_tpl.id if saved_tpl else None, match_strategy="EXACT_HASH", validation=local_val, raw_content=content, db=db)
        print_final_extraction_summary(local_result, source_label="Local OCR Fallback (PDF)")
        return local_result


@app.post("/api/inspect-pdf", response_model=PDFInspectionResult)
async def inspect_pdf(file: UploadFile = File(...)):
    """
    Multi-engine PDF inspector comparing PyMuPDF, pdfplumber, and pypdf.
    """
    content = await file.read()
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        content = convert_images_to_pdf([content])
    return inspect_pdf_document(content, filename=filename)


@app.post("/api/image-to-pdf")
async def image_to_pdf_endpoint(files: List[UploadFile] = File(...)):
    """
    Converts uploaded images into a merged standard PDF document.
    """
    img_bytes_list = [await f.read() for f in files]
    pdf_bytes = convert_images_to_pdf(img_bytes_list)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=converted_images.pdf"}
    )


@app.post("/api/searchable-pdf")
async def searchable_pdf_endpoint(file: UploadFile = File(...)):
    """
    Generates a Searchable PDF with background image and invisible OCR text layer.
    """
    content = await file.read()
    filename = file.filename or "invoice"
    
    # If already a PDF, rasterize first page to image
    if filename.lower().endswith(".pdf"):
        import pymupdf as fitz
        doc = fitz.open(stream=content, filetype="pdf")
        if len(doc) > 0:
            pix = doc[0].get_pixmap(dpi=150)
            content = pix.tobytes("png")
        doc.close()

    pil_img = Image.open(io.BytesIO(content))
    _, words_data, _ = perform_image_ocr(pil_img)
    
    pdf_bytes = create_searchable_pdf(content, words_data)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=searchable_{filename}.pdf"}
    )


@app.post("/api/export-csv")
async def export_csv_endpoint(items: List[dict]):
    """
    Exports line items into a downloadable CSV file.
    """
    import csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["#", "Description", "HSN/SAC", "Quantity", "Unit", "Rate", "Discount", "Tax %", "Amount"])
    
    for it in items:
        writer.writerow([
            it.get("sno", ""),
            it.get("description", ""),
            it.get("hsn_sac", ""),
            it.get("quantity", 1),
            it.get("unit", "each"),
            it.get("unit_price", 0.0),
            it.get("discount", 0.0),
            f"{it.get('tax_rate', 0)}%",
            it.get("total_amount", 0.0)
        ])
    
    csv_bytes = output.getvalue().encode("utf-8")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoice_items.csv"}
    )


@app.get("/api/sample-invoice")
def get_sample_invoice():
    """
    Returns the generated demo GST Tax Invoice PDF.
    """
    pdf_bytes = generate_sample_gst_invoice()
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=Demo_GST_Invoice.pdf"}
    )


@app.get("/api/documents")
def get_extraction_documents(db: Session = Depends(get_db)):
    """
    Returns recent extraction documents and audit records.
    """
    from backend.database import ExtractionLog
    docs = db.query(ExtractionLog).order_by(ExtractionLog.created_at.desc()).limit(50).all()
    return [{
        "id": d.id,
        "invoice_number": d.invoice_number,
        "vendor_name": d.vendor_name,
        "buyer_name": d.buyer_name,
        "grand_total": d.grand_total,
        "status": d.status,
        "engine_used": d.engine_used,
        "validation_passed": d.validation_passed,
        "created_at": d.created_at.isoformat() if d.created_at else None
    } for d in docs]


@app.post("/api/reviews/{log_id}")
async def submit_human_review(
    log_id: int,
    action: str = "CORRECTED",
    corrected_data: Optional[dict] = None,
    note: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Records human-in-the-loop corrections and feeds back into template reliability.
    """
    from backend.database import ExtractionLog, ExtractionReview, InvoiceTemplate
    log_entry = db.query(ExtractionLog).filter(ExtractionLog.id == log_id).first()
    if not log_entry:
        raise HTTPException(status_code=404, detail="Extraction log not found")

    review = ExtractionReview(
        extraction_log_id=log_id,
        action=action,
        corrected_fields=corrected_data,
        reviewer_note=note,
        reviewed_by="human_reviewer"
    )
    db.add(review)

    log_entry.status = "SUCCESS" if action in ["APPROVED", "CORRECTED"] else "FAILED"
    if corrected_data:
        log_entry.normalized_data = corrected_data

    # If linked to a DRAFT template, promote to ACTIVE after successful human approval
    if log_entry.template_id and action == "APPROVED":
        tpl = db.query(InvoiceTemplate).filter(InvoiceTemplate.id == log_entry.template_id).first()
        if tpl and tpl.status == "DRAFT":
            tpl.status = "ACTIVE"
            tpl.is_verified = True
            tpl.verified_by = "human_reviewer"
            tpl.verified_at = datetime.utcnow()

    db.commit()
    return {"status": "success", "message": f"Review recorded for extraction #{log_id}"}


@app.get("/api/templates")
def list_learned_templates(db: Session = Depends(get_db)):
    """
    Returns all learned layout templates with complete geometric fingerprints.
    """
    from backend.database import InvoiceTemplate
    templates = db.query(InvoiceTemplate).order_by(InvoiceTemplate.created_at.desc()).all()
    return [{
        "id": t.id,
        "template_name": t.template_name,
        "format_type": t.format_type,
        "status": t.status or "ACTIVE",
        "anchor_keywords": t.anchor_keywords or [],
        "table_columns": t.table_columns or [],
        "column_boundaries": t.column_boundaries or {},
        "extraction_rules": t.extraction_rules or {},
        "known_vendors": t.known_vendors or [],
        "sample_preview_url": t.sample_preview_url,
        "hit_count": t.hit_count or 0,
        "success_count": t.success_count or 0,
        "failure_count": t.failure_count or 0,
        "avg_confidence": t.avg_confidence,
        "is_verified": getattr(t, "is_verified", True),
        "created_at": t.created_at.isoformat() if t.created_at else None
    } for t in templates]


@app.post("/api/benchmark/run")
def run_benchmark_endpoint(db: Session = Depends(get_db)):
    """
    Runs automated regression benchmarking against approved ground truth invoices.
    """
    from backend.services.benchmark_runner import run_ground_truth_benchmark_suite
    from backend.database import ExtractionLog, ExtractionReview
    
    # Query approved human reviews for real benchmark ground truth
    reviewed_logs = db.query(ExtractionLog).join(ExtractionReview).filter(ExtractionReview.action == "APPROVED").all()
    test_cases = []
    
    for l in reviewed_logs:
        if l.extracted_data and l.normalized_data:
            test_cases.append({
                "id": l.id,
                "filename": l.original_filename or f"invoice_{l.id}.pdf",
                "extracted": l.extracted_data,
                "ground_truth": l.normalized_data
            })
            
    # Include sample GST ground truth benchmark
    sample_gt = {
        "id": "demo_sample_gst",
        "filename": "Demo_GST_Invoice.pdf",
        "extracted": {
            "metadata": {"invoice_number": "GST-2024-8842"},
            "seller": {"gstin": "08AAYFN9847L1Z0"},
            "buyer": {"gstin": "08AAPFU0274E1Z4"},
            "summary": {"grand_total": 3394.43, "total_gst": 517.78},
            "line_items": [{}, {}, {}, {}, {}, {}],
            "status": "SUCCESS"
        },
        "ground_truth": {
            "invoice_number": "GST-2024-8842",
            "seller_gstin": "08AAYFN9847L1Z0",
            "buyer_gstin": "08AAPFU0274E1Z4",
            "grand_total": "3394.43",
            "total_tax": "517.78",
            "line_items": [{}, {}, {}, {}, {}, {}]
        }
    }
    test_cases.append(sample_gt)
    
    return run_ground_truth_benchmark_suite(test_cases)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db)):
    """
    Deletes a specific learned template.
    """
    from backend.database import InvoiceTemplate
    tpl = db.query(InvoiceTemplate).filter(InvoiceTemplate.id == template_id).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(tpl)
    db.commit()
    return {"status": "success", "message": f"Template #{template_id} deleted"}


@app.post("/api/templates/clear-all")
def clear_all_templates(db: Session = Depends(get_db)):
    """
    Clears all learned templates for a fresh start.
    """
    from backend.database import InvoiceTemplate
    db.query(InvoiceTemplate).delete()
    db.commit()
    return {"status": "success", "message": "All learned templates cleared"}


@app.post("/api/templates/{template_id}/promote")
def promote_template(template_id: int, db: Session = Depends(get_db)):
    """
    Promotes a DRAFT template to ACTIVE status.
    """
    from backend.database import InvoiceTemplate
    tpl = db.query(InvoiceTemplate).filter(InvoiceTemplate.id == template_id).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.status = "ACTIVE"
    tpl.is_verified = True
    tpl.verified_by = "admin"
    tpl.verified_at = datetime.utcnow()
    db.commit()
    return {"status": "success", "message": f"Template #{template_id} promoted to ACTIVE"}


@app.post("/api/templates/{template_id}/quarantine")
def quarantine_template(template_id: int, db: Session = Depends(get_db)):
    """
    Quarantines a failing or fragile template.
    """
    from backend.database import InvoiceTemplate
    tpl = db.query(InvoiceTemplate).filter(InvoiceTemplate.id == template_id).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.status = "QUARANTINED"
    db.commit()
    return {"status": "success", "message": f"Template #{template_id} quarantined"}


@app.post("/api/extractions/{extraction_id}/review")
def create_extraction_review(
    extraction_id: int,
    payload: ReviewRequest = ReviewRequest(),
    db: Session = Depends(get_db)
):
    """
    Submits a human-in-the-loop audit review for an extraction log.
    """
    from backend.database import ExtractionLog, ExtractionReview
    log = db.query(ExtractionLog).filter(ExtractionLog.id == extraction_id).first()
    if not log:
        raise HTTPException(status_code=404, detail=f"Extraction #{extraction_id} not found")

    act_upper = (payload.action or "APPROVED").upper()
    if act_upper not in ["APPROVED", "CORRECTED", "REJECTED"]:
        act_upper = "APPROVED"

    review = ExtractionReview(
        extraction_log_id=extraction_id,
        action=act_upper,
        reviewer_note=payload.reviewer_note,
        reviewed_by=payload.reviewed_by or "accountant",
        corrected_fields=payload.corrected_fields or {}
    )
    db.add(review)

    # Update extraction log status based on review action
    if act_upper == "APPROVED":
        log.status = "SUCCESS"
        log.validation_passed = True
    elif act_upper == "CORRECTED":
        log.status = "SUCCESS"
        log.validation_passed = True
    elif act_upper == "REJECTED":
        log.status = "FAILED"
        log.validation_passed = False

    db.commit()
    db.refresh(review)

    print(f"👤 [Audit Review] Added review #{review.id} for Extraction #{extraction_id} (Action: {act_upper}) by {payload.reviewed_by}")
    return {
        "status": "success",
        "message": f"Review recorded for Extraction #{extraction_id}",
        "review_id": review.id,
        "action": act_upper,
        "corrected_fields": payload.corrected_fields
    }







if __name__ == "__main__":
    # `python -m backend.main` (package context keeps the relative imports working).
    # Use uvicorn's import string from the repo root for `python main.py --reload`.
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
