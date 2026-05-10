from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent import FreightBillAgent
from app.config import Settings
from app.db import Base
from app.decision_engine import DecisionEngine
from app.models import FreightBill
from app.seed_loader import get_seed_freight_bill, json_safe, seed_reference_data


SEED_PATH = Path("data/seed_data_logistics.json")


def make_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    seed_reference_data(db, SEED_PATH)
    return db


def make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        seed_reference_data(db, SEED_PATH)
    return Session


def add_bill(db, bill_id: str, status: str = "received") -> FreightBill:
    payload = get_seed_freight_bill(SEED_PATH, bill_id)
    bill = FreightBill(**payload, raw_payload=json_safe(payload), status=status)
    db.add(bill)
    db.commit()
    return bill


def engine(db) -> DecisionEngine:
    return DecisionEngine(db, Settings(seed_data_path=SEED_PATH, openai_api_key=None))


def test_clean_bill_auto_approves():
    db = make_session()
    bill = add_bill(db, "FB-2025-101")

    result = engine(db).evaluate(bill)

    assert result.decision == "auto_approve"
    assert result.status == "approved"
    assert result.confidence >= 0.88
    assert result.evidence["selected_contract"]["id"] == "CC-2024-SFX-001"


def test_duplicate_bill_disputes_against_existing_bill_number():
    db = make_session()
    existing = add_bill(db, "FB-2025-101", status="approved")
    existing.decision = "auto_approve"
    db.commit()
    duplicate = add_bill(db, "FB-2025-109")

    result = engine(db).evaluate(duplicate)

    assert result.decision == "dispute"
    assert any(item["code"] == "duplicate_invoice" for item in result.evidence["validations"])


def test_ftl_alternate_kg_billing_reconciles():
    db = make_session()
    bill = add_bill(db, "FB-2025-107")

    result = engine(db).evaluate(bill)

    assert result.decision == "auto_approve"
    assert result.evidence["selected_contract"]["id"] == "CC-2024-TCI-002"


def test_overbilling_disputes_when_prior_bill_consumes_remaining_weight():
    db = make_session()
    prior = add_bill(db, "FB-2025-103", status="approved")
    prior.decision = "auto_approve"
    db.commit()
    overbill = add_bill(db, "FB-2025-104")

    result = engine(db).evaluate(overbill)

    assert result.decision == "dispute"
    assert any(item["code"] == "shipment_weight" for item in result.evidence["validations"])


def test_messy_carrier_name_uses_deterministic_fuzzy_fallback_without_llm():
    db = make_session()
    payload = get_seed_freight_bill(SEED_PATH, "FB-2025-101")
    payload["id"] = "FB-TEST-MESSY-CARRIER"
    payload["carrier_id"] = None
    payload["carrier_name"] = "Safe Xpress Logistix"
    bill = FreightBill(**payload, raw_payload=json_safe(payload), status="received")
    db.add(bill)
    db.commit()

    result = engine(db).evaluate(bill)

    assert result.evidence["matched_carrier"]["id"] == "CAR001"
    assert result.evidence["matched_carrier"]["match_type"] == "fuzzy_name"


def test_agent_resume_applies_review_when_memory_checkpoint_is_missing(monkeypatch):
    Session = make_session_factory()
    monkeypatch.setattr("app.agent.SessionLocal", Session)

    payload = get_seed_freight_bill(SEED_PATH, "FB-2025-103")
    with Session() as db:
        bill = FreightBill(
            **payload,
            raw_payload=json_safe(payload),
            status="in_review",
            decision="flag_for_review",
            confidence=0.72,
            evidence={"validations": []},
            explanation="Waiting for reviewer decision.",
        )
        db.add(bill)
        db.commit()

    agent = FreightBillAgent(Settings(seed_data_path=SEED_PATH, openai_api_key=None))
    result = agent.resume(
        "FB-2025-103",
        {"decision": "approve", "approver_name": "Ayush", "notes": "Manual check passed."},
    )

    assert result["analysis"]["status"] == "approved"
    assert result["analysis"]["decision"] == "manual_approve"
    with Session() as db:
        reviewed = db.get(FreightBill, "FB-2025-103")
        assert reviewed.status == "approved"
        assert reviewed.reviewer_decision["decision"] == "approve"
        assert reviewed.reviewer_decision["approver_name"] == "Ayush"
