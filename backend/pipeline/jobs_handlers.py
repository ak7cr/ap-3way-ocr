"""
Job handlers: the worker-side entry points into the pipeline.

The API never runs OCR inline when a job is queued; it stores the file, enqueues
an INGEST_DOCUMENT job and returns. A worker then does the expensive part. This
keeps request latency flat and makes the OCR stage independently scalable.
"""
import asyncio
from typing import Any, Dict

from sqlalchemy.orm import Session

from .. import jobs
from ..config import DEFAULT_TENANT
from ..services import matching_service as svc
from ..services.storage import read_bytes


@jobs.register("INGEST_DOCUMENT")
def ingest_document_job(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs the full ingest for a file already written to object storage.

    Kept async-free: the OCR pipeline is async, so we drive it on a loop here
    rather than making every worker a coroutine.
    """
    import io

    from starlette.datastructures import Headers, UploadFile

    from ..services.ingest_service import ingest_upload

    storage_key = payload.get("storage_key")
    filename = payload.get("file_name") or "document"
    if not storage_key:
        raise ValueError("INGEST_DOCUMENT needs a storage_key")

    content = read_bytes(storage_key)
    upload = UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": payload.get("content_type") or "application/octet-stream"}),
    )

    result = asyncio.run(
        ingest_upload(
            db,
            upload,
            extraction_method=payload.get("extraction_method") or "auto",
            api_key=payload.get("api_key"),
            po_number=payload.get("po_number"),
        )
    )
    return result if isinstance(result, dict) else {"result": str(result)}


@jobs.register("RUN_MATCH")
def run_match_job(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Re-runs the deterministic match for an already-canonicalised invoice."""
    document_id = payload.get("invoice_document_id")
    if not document_id:
        raise ValueError("RUN_MATCH needs an invoice_document_id")
    result = svc.run_match_for_invoice(db, int(document_id))
    return {
        "match_result_id": result.id,
        "status": result.status,
        "decision": result.decision,
        "reason_codes": result.reason_codes,
    }
