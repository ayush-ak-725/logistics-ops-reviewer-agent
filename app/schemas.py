from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Decision = Literal["auto_approve", "flag_for_review", "dispute"]
ReviewDecision = Literal["approve", "dispute", "modify"]


class FreightBillIn(BaseModel):
    id: str
    carrier_id: str | None = None
    carrier_name: str | None = None
    bill_number: str | None = None
    bill_date: date | None = None
    shipment_reference: str | None = None
    lane: str | None = None
    billed_weight_kg: float | None = None
    billing_unit: str | None = None
    rate_per_kg: float | None = None
    base_charge: float | None = None
    fuel_surcharge: float | None = None
    gst_amount: float | None = None
    total_amount: float | None = None

    @model_validator(mode="after")
    def allow_seed_id_or_full_bill(self) -> "FreightBillIn":
        supplied_values = [
            self.carrier_name,
            self.bill_number,
            self.bill_date,
            self.lane,
            self.billed_weight_kg,
            self.base_charge,
            self.fuel_surcharge,
            self.gst_amount,
            self.total_amount,
        ]
        if any(value is not None for value in supplied_values) and not all(value is not None for value in supplied_values):
            raise ValueError("Provide either only a seed freight bill id, or all required freight bill fields.")
        return self


class EvidenceResponse(BaseModel):
    matched_carrier: dict[str, Any] | None = None
    candidate_contracts: list[dict[str, Any]] = Field(default_factory=list)
    selected_contract: dict[str, Any] | None = None
    matched_shipment: dict[str, Any] | None = None
    matched_bols: list[dict[str, Any]] = Field(default_factory=list)
    validations: list[dict[str, Any]] = Field(default_factory=list)
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    graph_path: list[str] = Field(default_factory=list)


class FreightBillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    carrier_id: str | None
    carrier_name: str
    bill_number: str
    bill_date: date
    shipment_reference: str | None
    lane: str
    billed_weight_kg: float
    billing_unit: str | None
    rate_per_kg: float | None
    base_charge: float
    fuel_surcharge: float
    gst_amount: float
    total_amount: float
    status: str
    decision: str | None
    confidence: float | None
    evidence: dict[str, Any]
    explanation: str | None
    reviewer_decision: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ReviewItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    carrier_name: str
    bill_number: str
    bill_date: date
    lane: str
    total_amount: float
    status: str
    decision: str | None
    confidence: float | None
    explanation: str | None


class ReviewRequest(BaseModel):
    decision: ReviewDecision
    notes: str | None = None
    modifications: dict[str, Any] | None = None


class MetricsResponse(BaseModel):
    total_bills: int
    auto_approved: int
    in_review: int
    disputed: int
    reviewed: int
    average_confidence: float | None
