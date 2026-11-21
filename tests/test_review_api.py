"""
API-level tests for the review and manual-receipt endpoints.

These exercise the HTTP surface the reviewer and the store person actually use:
the review queue must show the recommendation, the decision endpoint must respect
segregation of duties, and a hand-entered receipt must land in the ledger.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.threeway import router
from backend.database import Base, get_db
from backend.services import matching_service as svc

VALID_GSTIN = "27AABCU9603R1ZN"


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    app = FastAPI()
    app.include_router(router)

    def override_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        test_client.session = session  # type: ignore[attr-defined]
        yield test_client

    session.close()
    engine.dispose()


def seed_po(db, po_number="PO-2026-001", qty=100, rate=62.5):
    svc.upsert_vendor(db, {"code": "V-ACME", "name": "Acme Steel", "gstin": VALID_GSTIN})
    svc.import_purchase_order(
        db,
        {
            "po_number": po_number,
            "vendor_name": "Acme Steel",
            "vendor_gstin": VALID_GSTIN,
            "status": "OPEN",
            "lines": [
                {"line_no": 1, "item_code": "STL-100", "description": "MS Angle", "quantity": qty, "rate": rate, "tax_rate": 18},
                {"line_no": 2, "item_code": "STL-200", "description": "MS Plate", "quantity": 50, "rate": 71.0, "tax_rate": 18},
            ],
        },
    )


def test_receipt_entry_rows_expose_remaining_quantity(client):
    seed_po(client.session)
    response = client.get("/api/grns/entry/PO-2026-001")
    assert response.status_code == 200
    lines = response.json()["lines"]

    assert [line["po_line_no"] for line in lines] == [1, 2]
    assert lines[0]["remaining_qty"] == "100.00"


def test_manual_receipt_updates_the_remaining_quantity(client):
    seed_po(client.session)
    response = client.post(
        "/api/grns/manual",
        json={"po_number": "PO-2026-001", "receipt_date": "2026-11-12", "lines": [{"po_line_no": 1, "received_qty": 40}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["grn_number"].startswith("MANUAL/PO-2026-001")

    lines = client.get("/api/grns/entry/PO-2026-001").json()["lines"]
    assert lines[0]["already_received_qty"] == "40.00"
    assert lines[0]["remaining_qty"] == "60.00"


def test_manual_receipt_rejects_an_unknown_po(client):
    seed_po(client.session)
    response = client.post(
        "/api/grns/manual",
        json={"po_number": "PO-NOPE", "lines": [{"po_line_no": 1, "received_qty": 5}]},
    )
    assert response.status_code == 404


def _match_a_clean_invoice(db):
    from backend.services.canonical import CanonicalInvoice

    canonical = CanonicalInvoice(
        vendor_name="Acme Steel",
        vendor_gstin=VALID_GSTIN,
        invoice_number="ACME/1",
        invoice_date="2026-11-08",
        po_number="PO-2026-001",
        subtotal=6250.0,
        total_tax=1125.0,
        cgst_total=562.5,
        sgst_total=562.5,
        grand_total=7375.0,
        lines=[
            {"line_no": 1, "item_code": "STL100", "description": "MS Angle", "quantity": 100, "rate": 62.5, "tax_rate": 18, "amount": 6250.0}
        ],
    )
    svc.import_grn(
        db,
        {
            "grn_number": "GRN-1",
            "po_number": "PO-2026-001",
            "lines": [{"po_line_no": 1, "item_code": "STL-100", "received_quantity": 100}],
        },
    )
    doc = svc.create_invoice_document(db, canonical, original_filename="clean.pdf")
    return svc.run_match_for_invoice(db, doc.id)


def test_review_queue_shows_the_recommendation_and_decision_is_recorded(client):
    seed_po(client.session)
    result = _match_a_clean_invoice(client.session)

    queue = client.get("/api/review/queue").json()
    entry = next(item for item in queue if item["id"] == result.id)
    assert entry["status"] == "MATCHED"
    # Shadow mode: the system would approve, but a human still decides.
    assert entry["decision"] == "REVIEW"
    assert "SHADOW_MODE" in entry["decision_reasons"]

    decision = client.post(
        f"/api/review/queue/{result.id}/decision",
        json={"decision": "APPROVE", "actor": "finance@acme", "note": "checked"},
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["human_decision"] == "APPROVE"
    assert body["approval_status"] == "APPROVED"

    agreement = client.get("/api/shadow/agreement").json()
    assert agreement["total_decisions"] == 1
    assert agreement["overall_agreement"] == 1.0


def test_review_decision_requires_an_actor(client):
    seed_po(client.session)
    result = _match_a_clean_invoice(client.session)
    response = client.post(f"/api/review/queue/{result.id}/decision", json={"decision": "APPROVE"})
    assert response.status_code == 400


def test_validation_endpoint_lists_the_checks(client):
    seed_po(client.session)
    result = _match_a_clean_invoice(client.session)

    payload = client.get(f"/api/review/queue/{result.id}/validation").json()
    codes = [check["code"] for check in payload["validation"]["checks"]]
    assert "LINE_SUM_MATCHES_TAXABLE" in codes
    assert "TAX_RECOMPUTES" in codes
    assert payload["validation"]["passed"] is True
