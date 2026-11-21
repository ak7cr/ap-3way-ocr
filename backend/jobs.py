"""
Durable job queue for the stateless workers.

The HTTP request writes a row and returns; a worker claims it and does the heavy
work (OCR, matching). That is what makes the "modular monolith" operative: one
codebase, clear modules, and OCR work that can be scaled out or restarted
without touching the API process.

Claiming uses a conditional UPDATE so two workers can never take the same job.
"""
import os
import socket
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .config import DEFAULT_TENANT, QUEUE
from .database import Job

QUEUED = "QUEUED"
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"

# Registered handlers, keyed by job kind. Modules register their own.
_HANDLERS: Dict[str, Callable[[Session, Dict[str, Any]], Any]] = {}


def register(kind: str):
    """Decorator: `@register("INGEST_DOCUMENT")` registers a job handler."""

    def decorator(handler: Callable[[Session, Dict[str, Any]], Any]):
        _HANDLERS[kind] = handler
        return handler

    return decorator


def handlers() -> Dict[str, Callable[[Session, Dict[str, Any]], Any]]:
    return dict(_HANDLERS)


def worker_name() -> str:
    return QUEUE.worker_id or f"{socket.gethostname()}:{os.getpid()}"


def enqueue(
    db: Session,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    tenant_id: str = DEFAULT_TENANT,
    max_attempts: Optional[int] = None,
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload or {},
        status=QUEUED,
        attempts=0,
        max_attempts=max_attempts or QUEUE.max_attempts,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def claim_next(db: Session, kinds: Optional[List[str]] = None) -> Optional[Job]:
    """Atomically takes one queued job, if there is one."""
    me = worker_name()
    while True:
        query = db.query(Job).filter(Job.status == QUEUED)
        if kinds:
            query = query.filter(Job.kind.in_(kinds))
        candidate = query.order_by(Job.created_at.asc()).first()
        if candidate is None:
            return None

        # Only one worker can flip QUEUED -> RUNNING for a given row.
        updated = (
            db.query(Job)
            .filter(Job.id == candidate.id, Job.status == QUEUED)
            .update(
                {
                    Job.status: RUNNING,
                    Job.worker_id: me,
                    Job.started_at: datetime.utcnow(),
                    Job.attempts: (candidate.attempts or 0) + 1,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if updated:
            db.refresh(candidate)
            return candidate


def complete(db: Session, job: Job, result: Any = None) -> Job:
    job.status = DONE
    job.result = result if isinstance(result, (dict, list)) else {"value": str(result)}
    job.finished_at = datetime.utcnow()
    db.commit()
    return job


def fail(db: Session, job: Job, error: str) -> Job:
    job.error = error[:4000]
    job.finished_at = datetime.utcnow()
    if (job.attempts or 0) >= (job.max_attempts or QUEUE.max_attempts):
        job.status = FAILED
    else:
        # Back to the queue; another worker can retry it.
        job.status = QUEUED
    db.commit()
    return job


def run_once(db: Session, kinds: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Claims and runs a single job. Returns a summary, or None when idle."""
    job = claim_next(db, kinds)
    if job is None:
        return None

    handler = _HANDLERS.get(job.kind)
    if handler is None:
        fail(db, job, f"No handler registered for job kind '{job.kind}'")
        return {"job_id": job.id, "kind": job.kind, "status": FAILED, "error": "no handler"}

    try:
        result = handler(db, job.payload or {})
    except Exception as exc:  # a failing job must not kill the worker
        fail(db, job, f"{type(exc).__name__}: {exc}")
        return {"job_id": job.id, "kind": job.kind, "status": job.status, "error": str(exc)}

    complete(db, job, result)
    return {"job_id": job.id, "kind": job.kind, "status": DONE}


def run_worker(db: Session, max_jobs: Optional[int] = None, kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Drains the queue (bounded by `max_jobs` when given)."""
    processed: List[Dict[str, Any]] = []
    while max_jobs is None or len(processed) < max_jobs:
        summary = run_once(db, kinds)
        if summary is None:
            break
        processed.append(summary)
    return processed


def queue_stats(db: Session, tenant_id: Optional[str] = None) -> Dict[str, Any]:
    query = db.query(Job.status, Job.kind)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)
    counts: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for status, kind in query.all():
        counts[status] = counts.get(status, 0) + 1
        if status in (QUEUED, RUNNING):
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "queued": counts.get(QUEUED, 0),
        "running": counts.get(RUNNING, 0),
        "done": counts.get(DONE, 0),
        "failed": counts.get(FAILED, 0),
        "pending_by_kind": by_kind,
        "handler_kinds": sorted(_HANDLERS),
        "worker": worker_name(),
    }
