from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Carrier(Base):
    __tablename__ = "carriers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    carrier_code: Mapped[str | None] = mapped_column(String(40))
    gstin: Mapped[str | None] = mapped_column(String(40))
    bank_account: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    onboarded_on: Mapped[date | None] = mapped_column(Date)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CarrierContract(Base):
    __tablename__ = "carrier_contracts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    carrier_id: Mapped[str] = mapped_column(ForeignKey("carriers.id"), nullable=False, index=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    rate_card: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    carrier_id: Mapped[str] = mapped_column(ForeignKey("carriers.id"), nullable=False, index=True)
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("carrier_contracts.id"), index=True)
    lane: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    shipment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BillOfLading(Base):
    __tablename__ = "bills_of_lading"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), nullable=False, index=True)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    actual_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "relation", "target_type", "target_id", name="uq_graph_edge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FreightBill(Base):
    __tablename__ = "freight_bills"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    carrier_id: Mapped[str | None] = mapped_column(String(40), index=True)
    carrier_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    bill_number: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    bill_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shipment_reference: Mapped[str | None] = mapped_column(String(80), index=True)
    lane: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    billed_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    billing_unit: Mapped[str | None] = mapped_column(String(40))
    rate_per_kg: Mapped[float | None] = mapped_column(Float)
    base_charge: Mapped[float] = mapped_column(Float, nullable=False)
    fuel_surcharge: Mapped[float] = mapped_column(Float, nullable=False)
    gst_amount: Mapped[float] = mapped_column(Float, nullable=False)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(40), nullable=False, default="received", index=True)
    decision: Mapped[str | None] = mapped_column(String(40), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation: Mapped[str | None] = mapped_column(Text)
    reviewer_decision: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id: Mapped[str | None] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
