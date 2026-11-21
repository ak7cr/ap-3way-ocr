import os
import re
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, Numeric, DateTime, JSON, Boolean, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/invoice_ocr")

# Normalize the legacy "postgres://" scheme — SQLAlchemy 2.x only loads the
# dialect for "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

Base = declarative_base()


class InvoiceTemplate(Base):
    """
    Stores learned invoice layout formats and dynamic extraction rules (not hardcoded values).
    """
    __tablename__ = "invoice_templates"

    id = Column(Integer, primary_key=True, index=True)
    template_name = Column(String(255), nullable=False)           # e.g. "Two-Column European VAT Template"
    format_type = Column(String(50), default="STANDARD_INVOICE")  # e.g. "STANDARD_INVOICE", "GST_TAX_INVOICE"
    status = Column(String(50), default="DRAFT", index=True)      # DRAFT, ACTIVE, DEPRECATED, QUARANTINED
    
    # Versioning & Lineage
    version = Column(Integer, default=1)
    vendor_key = Column(String(255), nullable=True, index=True)
    template_family_key = Column(String(255), nullable=True, index=True)
    supersedes_id = Column(Integer, nullable=True)

    # Structural Fingerprint
    layout_hash = Column(String(255), nullable=True, index=True)
    anchor_keywords = Column(JSON, nullable=False)                # ["TAX INVOICE", "DESCRIPTION OF GOODS", ...]
    header_geometry = Column(JSON, nullable=True)                 # { headerZone, itemsZone, summaryZone }
    column_boundaries = Column(JSON, nullable=True)               # { description: [0.07, 0.42], ... }
    row_detection_rules = Column(JSON, nullable=True)             # { strategy: "sno_and_amount", ... }
    extraction_rules = Column(JSON, nullable=False)               # { "mid_x_ratio": 0.48, "table_start": 0.32 }
    table_columns = Column(JSON, nullable=False)                  # ["sno", "description", "qty", "rate", "amount"]
    known_vendors = Column(JSON, default=list)                    # ["Hall-Boyd", "Acme Corp"]
    sample_preview_url = Column(Text, nullable=True)

    # Reliability & Confidence Tracking
    hit_count = Column(Integer, default=1)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    avg_confidence = Column(Float, nullable=True)
    last_matched_at = Column(DateTime, nullable=True)
    last_failed_at = Column(DateTime, nullable=True)

    is_verified = Column(Boolean, default=False)
    verified_by = Column(String(255), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    # CANDIDATE -> TRUSTED only after repeated clean human confirmations.
    confirmations = Column(Integer, default=0)
    last_confirmed_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    extraction_logs = relationship("ExtractionLog", back_populates="template", foreign_keys="[ExtractionLog.template_id]")


class ExtractionLog(Base):
    """
    Audit log & invoice document record of extractions in PostgreSQL.
    """
    __tablename__ = "extraction_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("invoice_templates.id"), nullable=True, index=True)
    match_strategy = Column(String(50), default="AI_FALLBACK")    # EXACT_HASH, ANCHOR_KEYWORDS, AI_FALLBACK
    layout_match_score = Column(Float, nullable=True)
    status = Column(String(50), default="NEEDS_REVIEW", index=True) # SUCCESS, PARTIAL, FAILED, NEEDS_REVIEW

    invoice_number = Column(String(100), nullable=True, index=True)
    invoice_date = Column(DateTime, nullable=True)
    format_type = Column(String(50), nullable=True)
    vendor_name = Column(String(255), nullable=True)
    vendor_gstin = Column(String(50), nullable=True, index=True)
    buyer_name = Column(String(255), nullable=True)
    buyer_gstin = Column(String(50), nullable=True, index=True)

    engine_used = Column(String(100), nullable=False)
    engine_version = Column(String(50), nullable=True)
    processing_time_ms = Column(Integer, default=0)

    overall_confidence = Column(Float, nullable=True)
    field_confidence = Column(JSON, nullable=True)
    low_confidence_fields = Column(JSON, nullable=True)

    extracted_data = Column(JSON, nullable=True)
    normalized_data = Column(JSON, nullable=True)
    raw_ocr_text = Column(Text, nullable=True)

    grand_total = Column(Numeric(18, 2), nullable=True)
    taxable_value = Column(Numeric(18, 2), nullable=True)
    total_tax = Column(Numeric(18, 2), nullable=True)
    currency = Column(String(10), default="INR")

    validation_passed = Column(Boolean, nullable=True)
    validation_errors = Column(JSON, nullable=True)

    is_duplicate = Column(Boolean, default=False)
    duplicate_of_id = Column(Integer, ForeignKey("extraction_logs.id"), nullable=True, index=True)

    # Which escalation threshold decided this document's fate (for tuning).
    threshold_decision = Column(JSON, nullable=True)
    document_ocr_attempts = Column(JSON, nullable=True)

    # Geometry proposed by an AI extraction. It is NOT a template until a human
    # confirms it (see template_engine.confirm_proposed_template).
    proposed_template = Column(JSON, nullable=True)
    proposed_template_status = Column(String(30), default="AWAITING_CONFIRMATION")

    source_file_url = Column(Text, nullable=True)
    source_file_hash = Column(String(255), nullable=True, index=True)
    original_filename = Column(String(255), nullable=True)
    source_mime_type = Column(String(100), nullable=True)
    source_page_count = Column(Integer, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    template = relationship("InvoiceTemplate", back_populates="extraction_logs")
    duplicate_of = relationship("ExtractionLog", remote_side=[id], backref="duplicates")


class ExtractionReview(Base):
    """
    Human-in-the-loop audit and correction log.
    """
    __tablename__ = "extraction_reviews"

    id = Column(Integer, primary_key=True, index=True)
    extraction_log_id = Column(Integer, nullable=False, index=True)
    action = Column(String(50), nullable=False)                    # APPROVED, CORRECTED, REJECTED
    corrected_fields = Column(JSON, nullable=True)
    reviewer_note = Column(Text, nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ===========================================================================
# 3-WAY MATCH DOMAIN
# ===========================================================================
class Vendor(Base):
    """Vendor master. GSTIN is the authoritative key, name is the fallback."""
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    code = Column(String(50), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    gstin = Column(String(50), nullable=True, index=True)
    risk_tier = Column(String(20), default="LOW")                  # LOW, MEDIUM, HIGH
    category = Column(String(100), nullable=True)
    payment_terms = Column(String(100), nullable=True)
    # Per-vendor match tolerances (kept as strings so Decimal precision survives)
    tolerance_price_pct = Column(String(20), nullable=True)
    tolerance_qty = Column(String(20), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PurchaseOrder(Base):
    """Purchase order header. Usually imported from the ERP, not OCR'd."""
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    po_number = Column(String(100), nullable=False, unique=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True, index=True)
    status = Column(String(30), default="OPEN", index=True)       # OPEN, CLOSED, CANCELLED
    currency = Column(String(10), default="INR")
    po_date = Column(DateTime, nullable=True)
    subtotal = Column(Numeric(18, 2), nullable=True)
    total_tax = Column(Numeric(18, 2), nullable=True)
    grand_total = Column(Numeric(18, 2), nullable=True)
    category = Column(String(100), nullable=True)
    source = Column(String(20), default="ERP")                    # ERP, OCR, CSV
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lines = relationship("PurchaseOrderLine", back_populates="purchase_order", cascade="all, delete-orphan")
    vendor = relationship("Vendor")


class PurchaseOrderLine(Base):
    """A single ordered line on a PO."""
    __tablename__ = "purchase_order_lines"

    id = Column(Integer, primary_key=True, index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False, index=True)
    line_no = Column(Integer, nullable=False)
    item_code = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    hsn_sac = Column(String(20), nullable=True)
    unit = Column(String(20), nullable=True)
    ordered_qty = Column(Numeric(18, 4), default=0)
    rate = Column(Numeric(18, 2), default=0)
    tax_rate = Column(Numeric(6, 2), default=0)
    line_amount = Column(Numeric(18, 2), default=0)

    purchase_order = relationship("PurchaseOrder", back_populates="lines")
    balance = relationship("POLineBalance", back_populates="po_line", uselist=False, cascade="all, delete-orphan")


class POLineBalance(Base):
    """
    Running balance per PO line: what has been received, invoiced and credited.
    This is what makes partial deliveries correct: an invoice can only bill
    (cumulative GRN - already invoiced - credited).
    """
    __tablename__ = "po_line_balances"

    id = Column(Integer, primary_key=True, index=True)
    po_line_id = Column(Integer, ForeignKey("purchase_order_lines.id"), nullable=False, unique=True, index=True)
    received_qty = Column(Numeric(18, 4), default=0)
    invoiced_qty = Column(Numeric(18, 4), default=0)
    credited_qty = Column(Numeric(18, 4), default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    po_line = relationship("PurchaseOrderLine", back_populates="balance")


class GoodsReceipt(Base):
    """Goods Receipt Note header."""
    __tablename__ = "goods_receipts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    grn_number = Column(String(100), nullable=True, index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False, index=True)
    receipt_date = Column(DateTime, nullable=True)
    currency = Column(String(10), default="INR")
    source = Column(String(20), default="ERP")
    created_at = Column(DateTime, default=datetime.utcnow)

    lines = relationship("GoodsReceiptLine", back_populates="goods_receipt", cascade="all, delete-orphan")
    purchase_order = relationship("PurchaseOrder")


class GoodsReceiptLine(Base):
    """Received quantity for one PO line on one GRN."""
    __tablename__ = "goods_receipt_lines"

    id = Column(Integer, primary_key=True, index=True)
    grn_id = Column(Integer, ForeignKey("goods_receipts.id"), nullable=False, index=True)
    po_line_id = Column(Integer, ForeignKey("purchase_order_lines.id"), nullable=True, index=True)
    line_no = Column(Integer, nullable=True)
    item_code = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    unit = Column(String(20), nullable=True)
    received_qty = Column(Numeric(18, 4), default=0)

    goods_receipt = relationship("GoodsReceipt", back_populates="lines")
    po_line = relationship("PurchaseOrderLine")


class InvoiceDocument(Base):
    """Canonical invoice stored after OCR, ready to be matched."""
    __tablename__ = "invoice_documents"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    extraction_log_id = Column(Integer, ForeignKey("extraction_logs.id"), nullable=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True, index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=True, index=True)
    invoice_number = Column(String(100), nullable=True, index=True)
    invoice_date = Column(DateTime, nullable=True)
    currency = Column(String(10), default="INR")
    po_number = Column(String(100), nullable=True, index=True)
    subtotal = Column(Numeric(18, 2), nullable=True)
    total_tax = Column(Numeric(18, 2), nullable=True)
    grand_total = Column(Numeric(18, 2), nullable=True)
    canonical_data = Column(JSON, nullable=True)
    validation_errors = Column(JSON, nullable=True)
    source_file_url = Column(Text, nullable=True)
    original_filename = Column(String(255), nullable=True)
    validation_passed = Column(Boolean, nullable=True)
    validation_report = Column(JSON, nullable=True)
    status = Column(String(30), default="READY", index=True)      # READY, VALIDATION_FAILED, MATCHED, EXCEPTION, REJECT, PENDING_APPROVAL, APPROVED, PAID
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lines = relationship("InvoiceDocumentLine", back_populates="invoice_document", cascade="all, delete-orphan")


class InvoiceDocumentLine(Base):
    __tablename__ = "invoice_document_lines"

    id = Column(Integer, primary_key=True, index=True)
    invoice_document_id = Column(Integer, ForeignKey("invoice_documents.id"), nullable=False, index=True)
    line_no = Column(Integer, nullable=False)
    item_code = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    hsn_sac = Column(String(20), nullable=True)
    quantity = Column(Numeric(18, 4), default=0)
    unit = Column(String(20), nullable=True)
    rate = Column(Numeric(18, 2), default=0)
    tax_rate = Column(Numeric(6, 2), default=0)
    tax_amount = Column(Numeric(18, 2), default=0)
    amount = Column(Numeric(18, 2), default=0)
    field_confidence = Column(JSON, nullable=True)
    field_source = Column(JSON, nullable=True)

    invoice_document = relationship("InvoiceDocument", back_populates="lines")


class MatchResult(Base):
    """The outcome of one 3-way match run for one invoice."""
    __tablename__ = "match_results"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    invoice_document_id = Column(Integer, ForeignKey("invoice_documents.id"), nullable=False, index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=True, index=True)
    status = Column(String(20), nullable=False, index=True)        # MATCHED, EXCEPTION, REJECT
    reason_codes = Column(JSON, default=list)
    checks = Column(JSON, default=list)
    line_findings = Column(JSON, default=list)
    computed_tax = Column(Numeric(18, 2), nullable=True)
    computed_total = Column(Numeric(18, 2), nullable=True)
    variance_total = Column(Numeric(18, 2), nullable=True)
    duplicate_of_invoice_id = Column(Integer, nullable=True)
    match_duration_ms = Column(Integer, default=0)
    balances_applied = Column(Boolean, default=False)

    # Exception workflow
    resolution = Column(String(30), nullable=True, index=True)      # OVERRIDDEN, CREDIT_NOTE, HOLD, REJECTED
    resolved_by = Column(String(255), nullable=True)
    override_reason = Column(Text, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    # Validation gate + approve/review/reject decision
    validation_passed = Column(Boolean, nullable=True)
    validation_report = Column(JSON, nullable=True)
    decision = Column(String(20), nullable=True, index=True)        # APPROVE, REVIEW, REJECT
    decision_reasons = Column(JSON, default=list)
    decision_explanation = Column(Text, nullable=True)
    auto_approve_allowed = Column(Boolean, default=False)
    decision_observation_id = Column(Integer, nullable=True, index=True)
    human_decision = Column(String(20), nullable=True, index=True)   # what the reviewer actually did
    human_decision_by = Column(String(255), nullable=True)
    human_decision_at = Column(DateTime, nullable=True)

    # Approval workflow
    approval_status = Column(String(30), default="PENDING", index=True)  # NOT_REQUIRED, PENDING, APPROVED, REJECTED
    approvals_required = Column(Integer, default=0)
    approvals_received = Column(Integer, default=0)
    required_roles = Column(JSON, default=list)
    approved_at = Column(DateTime, nullable=True)

    matched_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    invoice_document = relationship("InvoiceDocument")
    actions = relationship("ExceptionAction", back_populates="match_result", cascade="all, delete-orphan")
    approvals = relationship("ApprovalDecision", back_populates="match_result", cascade="all, delete-orphan")


class ExceptionAction(Base):
    """Every human decision taken on an exception."""
    __tablename__ = "exception_actions"

    id = Column(Integer, primary_key=True, index=True)
    match_result_id = Column(Integer, ForeignKey("match_results.id"), nullable=False, index=True)
    action = Column(String(30), nullable=False)                    # ACCEPT_OVERRIDE, CREDIT_NOTE, HOLD, REJECT
    reason = Column(Text, nullable=True)
    actor = Column(String(255), nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    match_result = relationship("MatchResult", back_populates="actions")


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"

    id = Column(Integer, primary_key=True, index=True)
    match_result_id = Column(Integer, ForeignKey("match_results.id"), nullable=False, index=True)
    approver = Column(String(255), nullable=False)
    role = Column(String(50), nullable=True)
    decision = Column(String(20), nullable=False)                  # APPROVED, REJECTED
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    match_result = relationship("MatchResult", back_populates="approvals")


class AuditLog(Base):
    """Immutable who/what/when/before/after trail."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=True, index=True)
    entity_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(Integer, nullable=True, index=True)
    action = Column(String(50), nullable=False)
    actor = Column(String(255), nullable=True)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ===========================================================================
# OPERATIONAL TABLES: shadow ledger, job queue
# ===========================================================================
class DecisionObservation(Base):
    """
    The engine's recommendation recorded beside the human's decision.

    This is what shadow mode exists to produce: evidence, per vendor, that the
    recommendation agrees with what an accountant would have done.
    """
    __tablename__ = "decision_observations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    document_id = Column(Integer, ForeignKey("invoice_documents.id"), nullable=True, index=True)
    match_result_id = Column(Integer, ForeignKey("match_results.id"), nullable=True, index=True)
    extraction_log_id = Column(Integer, nullable=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True, index=True)
    recommendation = Column(String(20), nullable=False, index=True)   # APPROVE, REVIEW, REJECT
    reason_codes = Column(JSON, default=list)
    confidence = Column(Float, nullable=True)
    human_decision = Column(String(20), nullable=True)                # APPROVE, REVIEW, REJECT
    agreement = Column(String(20), default="PENDING", index=True)     # PENDING, AGREED, DISAGREED
    actor = Column(String(255), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decided_at = Column(DateTime, nullable=True)

    vendor = relationship("Vendor")


class Job(Base):
    """
    Durable work item for the stateless workers.

    Intake writes a row and returns; workers claim it. That keeps the HTTP
    request cheap and makes the heavy OCR work restartable.
    """
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True, default="default")
    kind = Column(String(50), nullable=False, index=True)             # INGEST_DOCUMENT, RUN_MATCH, ...
    payload = Column(JSON, default=dict)
    status = Column(String(20), default="QUEUED", index=True)         # QUEUED, RUNNING, DONE, FAILED
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    worker_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)



engine = None
SessionLocal = None


def auto_create_pg_database():
    """
    Connects to default 'postgres' database and creates 'invoice_ocr' database if it doesn't exist yet.
    """
    try:
        match = re.match(r"(postgresql://[^/]+/)([^?]+)", DATABASE_URL)
        if match:
            base_url, db_name = match.group(1), match.group(2)
            default_pg_url = base_url + "postgres"
            
            temp_engine = create_engine(default_pg_url, isolation_level="AUTOCOMMIT")
            with temp_engine.connect() as conn:
                res = conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname='{db_name}'"))
                if not res.scalar():
                    conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                    print(f"✨ [PostgreSQL] Automatically created database: '{db_name}'")
            temp_engine.dispose()
    except Exception as e:
        print(f"Auto DB create check: {e}")


def run_schema_migrations(target_engine):
    """
    Ensures newly added columns exist in PostgreSQL without requiring manual migrations.
    """
    migration_statements = [
        # invoice_templates columns
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'DRAFT';",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS vendor_key VARCHAR(255);",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS template_family_key VARCHAR(255);",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS supersedes_id INTEGER;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS layout_hash VARCHAR(255);",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS header_geometry JSONB;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS column_boundaries JSONB;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS row_detection_rules JSONB;",
        "ALTER TABLE extraction_logs ALTER COLUMN match_strategy TYPE VARCHAR(50) USING match_strategy::text;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS success_count INTEGER DEFAULT 0;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS failure_count INTEGER DEFAULT 0;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS avg_confidence DOUBLE PRECISION;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS last_matched_at TIMESTAMP;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS last_failed_at TIMESTAMP;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS verified_by VARCHAR(255);",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();",

        # extraction_logs columns
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS buyer_name VARCHAR(255);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS buyer_gstin VARCHAR(50);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS field_confidence JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS low_confidence_fields JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS normalized_data JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS validation_passed BOOLEAN;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS validation_errors JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS duplicate_of_id INTEGER;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS source_file_hash VARCHAR(255);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS original_filename VARCHAR(255);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS source_mime_type VARCHAR(100);",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS source_page_count INTEGER;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS threshold_decision JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS document_ocr_attempts JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS proposed_template JSONB;",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS proposed_template_status VARCHAR(30) DEFAULT 'AWAITING_CONFIRMATION';",
        "ALTER TABLE extraction_logs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();",

        # template trust
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS confirmations INTEGER DEFAULT 0;",
        "ALTER TABLE invoice_templates ADD COLUMN IF NOT EXISTS last_confirmed_at TIMESTAMP;",

        # tenancy on the 3-way domain tables
        "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",
        "ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",
        "ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",
        "ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",
        "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) DEFAULT 'default';",

        # validation gate + decision on the match result
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS validation_passed BOOLEAN;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS validation_report JSONB;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS decision VARCHAR(20);",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS decision_reasons JSONB;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS decision_explanation TEXT;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS auto_approve_allowed BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS decision_observation_id INTEGER;",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS human_decision VARCHAR(20);",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS human_decision_by VARCHAR(255);",
        "ALTER TABLE match_results ADD COLUMN IF NOT EXISTS human_decision_at TIMESTAMP;",

        # validation on the invoice document
        "ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS validation_passed BOOLEAN;",
        "ALTER TABLE invoice_documents ADD COLUMN IF NOT EXISTS validation_report JSONB;",
        "ALTER TABLE invoice_documents ALTER COLUMN status TYPE VARCHAR(30) USING status::text;",

        # Rows from before the proposal column existed carry only the default.
        "UPDATE extraction_logs SET proposed_template_status = NULL WHERE proposed_template IS NULL;",

        # A human "Save as template" counts as exactly one confirmation in the new
        # model (three are needed before a layout is trusted).
        "UPDATE invoice_templates SET status = 'CANDIDATE', confirmations = 1 "
        "WHERE status = 'ACTIVE' AND verified_by = 'frontend_save';",

        # Legacy templates were auto-trusted by the old code. Anything a human
        # never verified goes back to CANDIDATE so it has to earn trust again.
        "UPDATE invoice_templates SET status = 'CANDIDATE', is_verified = FALSE "
        "WHERE status = 'ACTIVE' AND (verified_by IS NULL OR verified_by IN ('ai_validated_learning', 'auto_promoted_consecutive_passes'));",
    ]
    # Each statement gets its own transaction: a single failing ALTER (for
    # example on a column that already has the target type) must not abort the
    # rest, which is what a single shared transaction would do on Postgres.
    applied = 0
    for stmt in migration_statements:
        try:
            with target_engine.begin() as conn:
                conn.execute(text(stmt))
            applied += 1
        except Exception as exc:
            print(f"Migration skipped ({str(exc).splitlines()[0][:120]}): {stmt[:70]}")
    if applied:
        print(f"🧱 [Schema] {applied}/{len(migration_statements)} migration statement(s) applied")


def init_db():
    global engine, SessionLocal
    auto_create_pg_database()
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20
        )
        Base.metadata.create_all(bind=engine)
        run_schema_migrations(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        print(f"✅ Successfully connected to PostgreSQL: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")
        return True, "Connected to PostgreSQL"
    except Exception as e:
        print(f"⚠️ PostgreSQL connection warning: {e}")
        sqlite_url = "sqlite:///./templates.db"
        engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        return False, f"PostgreSQL unavailable ({str(e)}). Running on local backup DB."


def get_db():
    global SessionLocal
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

