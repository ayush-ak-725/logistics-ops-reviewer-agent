from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from math import isclose
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.graph_store import FreightGraph, RateMatch
from app.llm import ExplanationService
from app.models import BillOfLading, CarrierContract, FreightBill, Shipment


logger = logging.getLogger(__name__)


@dataclass
class DecisionResult:
    decision: str
    status: str
    confidence: float
    evidence: dict[str, Any]
    explanation: str


class DecisionEngine:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.graph = FreightGraph(db, settings)
        self.explainer = ExplanationService(settings)

    def evaluate(self, bill: FreightBill) -> DecisionResult:
        logger.info(
            "decision_engine_started",
            extra={
                "bill_id": bill.id,
                "bill_number": bill.bill_number,
                "carrier_id": bill.carrier_id,
                "carrier_name": bill.carrier_name,
                "lane": bill.lane,
                "bill_date": bill.bill_date.isoformat(),
                "shipment_reference": bill.shipment_reference,
                "billed_weight_kg": bill.billed_weight_kg,
                "total_amount": bill.total_amount,
            },
        )
        validations: list[dict[str, Any]] = []

        duplicate = self._find_duplicate(bill)
        if duplicate:
            logger.warning(
                "decision_duplicate_invoice_detected",
                extra={
                    "bill_id": bill.id,
                    "duplicate_bill_id": duplicate.id,
                    "bill_number": bill.bill_number,
                    "carrier_id": bill.carrier_id,
                    "carrier_name": bill.carrier_name,
                },
            )
            validations.append(
                self._validation(
                    "duplicate_invoice",
                    "critical",
                    f"Bill number {bill.bill_number} already exists for this carrier as {duplicate.id}.",
                    expected=duplicate.id,
                    actual=bill.id,
                )
            )

        carrier, carrier_score, carrier_match_type = self.graph.find_carrier(bill.carrier_id, bill.carrier_name)
        if not carrier:
            validations.append(
                self._validation(
                    "carrier_match",
                    "warning",
                    f"No onboarded carrier matched {bill.carrier_name}.",
                    expected="known carrier",
                    actual=bill.carrier_name,
                )
            )

        shipment, shipment_candidates, shipment_match_type = self.graph.find_shipment(
            carrier.id if carrier else bill.carrier_id,
            bill.lane,
            bill.shipment_reference,
            bill.bill_date,
        )
        self._validate_shipment(bill, shipment, shipment_candidates, shipment_match_type, validations)

        contract_candidates = (
            self.graph.contract_candidates(carrier.id, bill.lane, bill.bill_date, shipment) if carrier else []
        )
        selected = self._select_contract(bill, contract_candidates)
        logger.info(
            "decision_match_summary",
            extra={
                "bill_id": bill.id,
                "carrier_id": carrier.id if carrier else None,
                "carrier_match_type": carrier_match_type,
                "carrier_score": round(carrier_score, 3),
                "shipment_id": shipment.id if shipment else None,
                "shipment_match_type": shipment_match_type,
                "shipment_candidate_ids": [candidate.id for candidate in shipment_candidates],
                "contract_candidate_ids": [candidate.contract.id for candidate in contract_candidates],
                "valid_contract_candidate_ids": [candidate.contract.id for candidate in contract_candidates if candidate.date_valid],
                "selected_contract_id": selected.contract.id if selected else None,
            },
        )
        if not contract_candidates:
            validations.append(
                self._validation(
                    "contract_match",
                    "warning" if not carrier else "critical",
                    f"No contract candidate covers lane {bill.lane} for the matched carrier.",
                    expected="contract with lane and bill date",
                    actual=None,
                )
            )
        elif not selected:
            validations.append(
                self._validation(
                    "contract_match",
                    "critical",
                    "Contract candidates exist, but none are effective on the bill date.",
                    expected=bill.bill_date.isoformat(),
                    actual=[candidate.contract.id for candidate in contract_candidates],
                )
            )
        elif len([candidate for candidate in contract_candidates if candidate.date_valid]) > 1:
            validations.append(
                self._validation(
                    "contract_ambiguity",
                    "warning",
                    "Multiple effective contracts cover this carrier/lane/date; selected by shipment contract or best charge fit.",
                    expected=selected.contract.id,
                    actual=[candidate.contract.id for candidate in contract_candidates if candidate.date_valid],
                )
            )

        charge_score = 0.0
        if selected:
            charge_score = self._validate_charges(bill, selected, validations)

        bols = self.graph.shipment_bols(shipment.id if shipment else None)
        weight_score = self._validate_weight(bill, shipment, bols, validations)

        confidence = self._confidence(
            carrier_score=carrier_score if carrier else 0,
            shipment_match_type=shipment_match_type,
            selected=selected,
            contract_candidates=contract_candidates,
            charge_score=charge_score,
            weight_score=weight_score,
            validations=validations,
        )
        decision, status = self._decision(confidence, validations)
        logger.info(
            "decision_engine_completed",
            extra={
                "bill_id": bill.id,
                "decision": decision,
                "status": status,
                "confidence": confidence,
                "charge_score": round(charge_score, 3),
                "weight_score": round(weight_score, 3),
                "selected_contract_id": selected.contract.id if selected else None,
                "shipment_id": shipment.id if shipment else None,
                "bol_ids": [bol.id for bol in bols],
                "validation_codes": [item["code"] for item in validations],
                "critical_validation_codes": [item["code"] for item in validations if item.get("severity") == "critical"],
                "warning_validation_codes": [item["code"] for item in validations if item.get("severity") == "warning"],
            },
        )

        evidence = {
            "matched_carrier": (
                {"id": carrier.id, "name": carrier.name, "match_score": carrier_score, "match_type": carrier_match_type}
                if carrier
                else None
            ),
            "candidate_contracts": [self._contract_candidate_payload(candidate, bill.bill_date) for candidate in contract_candidates],
            "selected_contract": self._contract_candidate_payload(selected, bill.bill_date) if selected else None,
            "matched_shipment": self._shipment_payload(shipment, shipment_match_type) if shipment else None,
            "shipment_candidates": [self._shipment_payload(candidate, "candidate") for candidate in shipment_candidates],
            "matched_bols": [self._bol_payload(bol) for bol in bols],
            "validations": validations,
            "confidence_breakdown": {
                "carrier_score": carrier_score if carrier else 0,
                "shipment_match_type": shipment_match_type,
                "charge_score": charge_score,
                "weight_score": weight_score,
                "penalties": self._penalties(validations, contract_candidates),
            },
            "graph_path": self.graph.graph_path(carrier, selected.contract if selected else None, shipment),
        }
        explanation = self.explainer.explain(self._bill_payload(bill), decision, confidence, evidence)
        return DecisionResult(decision=decision, status=status, confidence=confidence, evidence=evidence, explanation=explanation)

    def apply_review(self, bill: FreightBill, review: dict[str, Any]) -> DecisionResult:
        logger.info(
            "decision_review_apply_started",
            extra={
                "bill_id": bill.id,
                "previous_status": bill.status,
                "previous_decision": bill.decision,
                "previous_confidence": bill.confidence,
                "review_decision": review.get("decision"),
            },
        )
        prior_evidence = bill.evidence or {}
        validations = list(prior_evidence.get("validations", []))
        validations.append(
            self._validation(
                "human_review",
                "info",
                f"Reviewer submitted {review.get('decision')}.",
                expected="human decision",
                actual=review,
            )
        )
        decision_map = {
            "approve": ("auto_approve", "approved"),
            "dispute": ("dispute", "disputed"),
            "modify": ("flag_for_review", "reviewed"),
        }
        decision, status = decision_map.get(review.get("decision"), ("flag_for_review", "reviewed"))
        evidence = {**prior_evidence, "validations": validations, "review": review}
        confidence = max(float(bill.confidence or 0), 0.95)
        explanation = self.explainer.explain(self._bill_payload(bill), decision, confidence, evidence)
        logger.info(
            "decision_review_apply_completed",
            extra={
                "bill_id": bill.id,
                "decision": decision,
                "status": status,
                "confidence": confidence,
                "review_decision": review.get("decision"),
            },
        )
        return DecisionResult(decision=decision, status=status, confidence=confidence, evidence=evidence, explanation=explanation)

    def _find_duplicate(self, bill: FreightBill) -> FreightBill | None:
        query = select(FreightBill).where(
            FreightBill.id != bill.id,
            FreightBill.bill_number == bill.bill_number,
            FreightBill.status != "void",
        )
        if bill.carrier_id:
            query = query.where(FreightBill.carrier_id == bill.carrier_id)
        else:
            query = query.where(FreightBill.carrier_name == bill.carrier_name)
        return self.db.scalars(query).first()

    def _validate_shipment(
        self,
        bill: FreightBill,
        shipment: Shipment | None,
        shipment_candidates: list[Shipment],
        match_type: str,
        validations: list[dict[str, Any]],
    ) -> None:
        if not shipment:
            severity = "warning" if shipment_candidates else "critical"
            validations.append(
                self._validation(
                    "shipment_match",
                    severity,
                    "No shipment reference was provided and graph traversal could not choose a single shipment."
                    if shipment_candidates
                    else "No shipment matched the freight bill route and carrier.",
                    expected="single shipment",
                    actual=[candidate.id for candidate in shipment_candidates],
                )
            )
            return

        if shipment.lane != bill.lane:
            validations.append(
                self._validation("route_match", "critical", "Shipment lane differs from bill lane.", shipment.lane, bill.lane)
            )
        if bill.carrier_id and shipment.carrier_id != bill.carrier_id:
            validations.append(
                self._validation(
                    "shipment_carrier_match",
                    "critical",
                    "Shipment carrier differs from bill carrier.",
                    shipment.carrier_id,
                    bill.carrier_id,
                )
            )
        if match_type != "shipment_reference":
            validations.append(
                self._validation(
                    "shipment_inference",
                    "warning",
                    "Shipment was inferred because the bill did not include a shipment reference.",
                    "explicit shipment reference",
                    match_type,
                )
            )

    def _select_contract(self, bill: FreightBill, candidates: list[RateMatch]) -> RateMatch | None:
        valid = [candidate for candidate in candidates if candidate.date_valid]
        if not valid:
            return None

        def score(candidate: RateMatch) -> tuple[float, float]:
            charge_score = self._charge_fit_score(bill, candidate)
            shipment_score = 1.0 if candidate.shipment_contract_match else 0.0
            rate_score = 1.0 if self._rate_matches(bill, candidate.rate) else 0.0
            # Prefer the explicit shipment contract, then the contract whose charges fit best.
            return (shipment_score * 2 + charge_score + rate_score, candidate.contract.effective_date.toordinal())

        return sorted(valid, key=score, reverse=True)[0]

    def _validate_charges(self, bill: FreightBill, selected: RateMatch, validations: list[dict[str, Any]]) -> float:
        expected = self._expected_charges(bill, selected.rate, bill.bill_date)
        score_parts = []

        rate_ok = self._rate_matches(bill, selected.rate)
        score_parts.append(1 if rate_ok else 0)
        if not rate_ok:
            validations.append(
                self._validation(
                    "rate_match",
                    "critical",
                    "Billed rate does not match the selected contract rate or allowed alternate rate.",
                    expected.get("expected_rate"),
                    bill.rate_per_kg,
                )
            )

        for key in ["base_charge", "fuel_surcharge", "gst_amount", "total_amount"]:
            actual = float(getattr(bill, key))
            expected_value = float(expected[key])
            ok = abs(actual - expected_value) <= self.settings.currency_tolerance
            score_parts.append(1 if ok else 0)
            if not ok:
                severity = "critical" if key in {"base_charge", "total_amount"} else "warning"
                validations.append(
                    self._validation(
                        key,
                        severity,
                        f"{key.replace('_', ' ').title()} differs from contract calculation.",
                        round(expected_value, 2),
                        actual,
                    )
                )

        if selected.contract.status != "active":
            validations.append(
                self._validation(
                    "contract_status",
                    "warning",
                    f"Selected contract status is {selected.contract.status}.",
                    "active",
                    selected.contract.status,
                )
            )

        charge_score = sum(score_parts) / len(score_parts)
        logger.info(
            "decision_charge_validation_completed",
            extra={
                "bill_id": bill.id,
                "contract_id": selected.contract.id,
                "charge_score": round(charge_score, 3),
                "expected_rate": expected["expected_rate"],
                "actual_rate": bill.rate_per_kg,
                "expected_base_charge": round(expected["base_charge"], 2),
                "actual_base_charge": bill.base_charge,
                "expected_fuel_surcharge": round(expected["fuel_surcharge"], 2),
                "actual_fuel_surcharge": bill.fuel_surcharge,
                "expected_gst_amount": round(expected["gst_amount"], 2),
                "actual_gst_amount": bill.gst_amount,
                "expected_total_amount": round(expected["total_amount"], 2),
                "actual_total_amount": bill.total_amount,
                "validation_codes_after_charge": [item["code"] for item in validations],
            },
        )
        return charge_score

    def _validate_weight(
        self,
        bill: FreightBill,
        shipment: Shipment | None,
        bols: list[BillOfLading],
        validations: list[dict[str, Any]],
    ) -> float:
        if not shipment:
            logger.info("decision_weight_validation_skipped", extra={"bill_id": bill.id, "reason": "no_matched_shipment"})
            return 0.0

        prior_billed_weight = self._prior_billed_weight(bill, shipment.id)
        total_billed_weight = prior_billed_weight + bill.billed_weight_kg
        shipment_ok = total_billed_weight <= shipment.total_weight_kg + self.settings.weight_tolerance_kg
        bol_exact = any(
            abs(bol.actual_weight_kg - bill.billed_weight_kg) <= self.settings.weight_tolerance_kg for bol in bols
        )

        if not shipment_ok:
            logger.warning(
                "decision_weight_validation_failed",
                extra={
                    "bill_id": bill.id,
                    "shipment_id": shipment.id,
                    "prior_billed_weight_kg": prior_billed_weight,
                    "current_billed_weight_kg": bill.billed_weight_kg,
                    "total_billed_weight_kg": total_billed_weight,
                    "shipment_total_weight_kg": shipment.total_weight_kg,
                },
            )
            validations.append(
                self._validation(
                    "shipment_weight",
                    "critical",
                    "Cumulative billed weight exceeds the shipment total weight.",
                    shipment.total_weight_kg,
                    total_billed_weight,
                    {"prior_billed_weight_kg": prior_billed_weight},
                )
            )
            return 0.0

        if bol_exact:
            logger.info(
                "decision_weight_validation_completed",
                extra={
                    "bill_id": bill.id,
                    "shipment_id": shipment.id,
                    "weight_score": 1.0,
                    "match_type": "exact_bol_weight",
                    "bol_ids": [bol.id for bol in bols],
                    "bol_weights": [bol.actual_weight_kg for bol in bols],
                },
            )
            return 1.0

        delivered_weight = sum(bol.actual_weight_kg for bol in bols)
        if bill.billed_weight_kg <= shipment.total_weight_kg and total_billed_weight <= shipment.total_weight_kg:
            validations.append(
                self._validation(
                    "bol_weight",
                    "warning",
                    "No individual BOL exactly matches the billed weight, but cumulative billing stays within shipment weight.",
                    {"bol_weights": [bol.actual_weight_kg for bol in bols], "shipment_total": shipment.total_weight_kg},
                    bill.billed_weight_kg,
                    {"prior_billed_weight_kg": prior_billed_weight, "delivered_weight_kg": delivered_weight},
                )
            )
            logger.info(
                "decision_weight_validation_completed",
                extra={
                    "bill_id": bill.id,
                    "shipment_id": shipment.id,
                    "weight_score": 0.65,
                    "match_type": "shipment_cumulative_weight",
                    "prior_billed_weight_kg": prior_billed_weight,
                    "current_billed_weight_kg": bill.billed_weight_kg,
                    "delivered_weight_kg": delivered_weight,
                    "shipment_total_weight_kg": shipment.total_weight_kg,
                },
            )
            return 0.65

        logger.warning(
            "decision_weight_validation_failed",
            extra={
                "bill_id": bill.id,
                "shipment_id": shipment.id,
                "current_billed_weight_kg": bill.billed_weight_kg,
                "bol_weights": [bol.actual_weight_kg for bol in bols],
                "shipment_total_weight_kg": shipment.total_weight_kg,
            },
        )
        validations.append(
            self._validation(
                "bol_weight",
                "critical",
                "Billed weight does not reconcile to BOL or shipment totals.",
                {"bol_weights": [bol.actual_weight_kg for bol in bols], "shipment_total": shipment.total_weight_kg},
                bill.billed_weight_kg,
            )
        )
        return 0.0

    def _prior_billed_weight(self, bill: FreightBill, shipment_id: str) -> float:
        prior_bills = self.db.scalars(
            select(FreightBill).where(
                and_(
                    FreightBill.id != bill.id,
                    FreightBill.shipment_reference == shipment_id,
                    FreightBill.status.in_(["approved", "reviewed", "in_review", "disputed"]),
                )
            )
        ).all()
        return sum(float(item.billed_weight_kg) for item in prior_bills)

    def _expected_charges(self, bill: FreightBill, rate: dict[str, Any], bill_date: date) -> dict[str, float]:
        base = self._expected_base_charge(bill, rate)
        fuel_percent = self._fuel_percent(rate, bill_date)
        fuel = base * fuel_percent / 100
        gst = (base + fuel) * 0.18
        total = base + fuel + gst
        return {
            "expected_rate": self._expected_rate_for_bill(bill, rate),
            "base_charge": base,
            "fuel_surcharge": fuel,
            "gst_amount": gst,
            "total_amount": total,
        }

    def _expected_base_charge(self, bill: FreightBill, rate: dict[str, Any]) -> float:
        if self._uses_alternate_kg_rate(bill, rate):
            return max(float(rate.get("alternate_rate_per_kg", 0)) * bill.billed_weight_kg, float(rate.get("min_charge", 0)))
        if "rate_per_unit" in rate and rate.get("unit") == "FTL":
            return float(rate["rate_per_unit"])
        return max(float(rate.get("rate_per_kg", 0)) * bill.billed_weight_kg, float(rate.get("min_charge", 0)))

    def _expected_rate_for_bill(self, bill: FreightBill, rate: dict[str, Any]) -> float | None:
        if self._uses_alternate_kg_rate(bill, rate):
            return float(rate["alternate_rate_per_kg"])
        if "rate_per_kg" in rate:
            return float(rate["rate_per_kg"])
        if "alternate_rate_per_kg" in rate and bill.rate_per_kg is not None:
            return float(rate["alternate_rate_per_kg"])
        return None

    def _uses_alternate_kg_rate(self, bill: FreightBill, rate: dict[str, Any]) -> bool:
        return (
            rate.get("unit") == "FTL"
            and bill.billing_unit == "kg"
            and bill.rate_per_kg is not None
            and "alternate_rate_per_kg" in rate
        )

    def _fuel_percent(self, rate: dict[str, Any], bill_date: date) -> float:
        revised_on = rate.get("revised_on")
        if revised_on and bill_date >= date.fromisoformat(revised_on):
            return float(rate.get("revised_fuel_surcharge_percent", rate.get("fuel_surcharge_percent", 0)))
        return float(rate.get("fuel_surcharge_percent", 0))

    def _rate_matches(self, bill: FreightBill, rate: dict[str, Any]) -> bool:
        expected = self._expected_rate_for_bill(bill, rate)
        if expected is None or bill.rate_per_kg is None:
            return "rate_per_unit" in rate and bill.billing_unit == rate.get("unit")
        tolerance = max(expected * self.settings.rate_tolerance_percent, 0.01)
        return isclose(float(bill.rate_per_kg), expected, abs_tol=tolerance)

    def _charge_fit_score(self, bill: FreightBill, candidate: RateMatch) -> float:
        expected = self._expected_charges(bill, candidate.rate, bill.bill_date)
        keys = ["base_charge", "fuel_surcharge", "gst_amount", "total_amount"]
        return sum(
            1
            for key in keys
            if abs(float(getattr(bill, key)) - float(expected[key])) <= self.settings.currency_tolerance
        ) / len(keys)

    def _confidence(
        self,
        carrier_score: float,
        shipment_match_type: str,
        selected: RateMatch | None,
        contract_candidates: list[RateMatch],
        charge_score: float,
        weight_score: float,
        validations: list[dict[str, Any]],
    ) -> float:
        shipment_score = {
            "shipment_reference": 1.0,
            "single_lane_candidate": 0.65,
            "no_reference_ambiguous": 0.25,
            "no_reference_no_candidate": 0.0,
            "shipment_reference_not_found": 0.0,
        }.get(shipment_match_type, 0.0)
        contract_score = 1.0 if selected else 0.0
        valid_contract_count = len([candidate for candidate in contract_candidates if candidate.date_valid])
        ambiguity_penalty = 0.07 * max(valid_contract_count - 1, 0)
        severity_penalty = sum({"info": 0, "warning": 0.04, "critical": 0.25}.get(v["severity"], 0) for v in validations)
        score = (
            0.15 * carrier_score
            + 0.20 * shipment_score
            + 0.20 * contract_score
            + 0.30 * charge_score
            + 0.15 * weight_score
            - ambiguity_penalty
            - severity_penalty
        )
        return round(min(max(score, 0), 1), 3)

    def _decision(self, confidence: float, validations: list[dict[str, Any]]) -> tuple[str, str]:
        critical_codes = {item["code"] for item in validations if item.get("severity") == "critical"}
        if "duplicate_invoice" in critical_codes:
            return "dispute", "disputed"
        dispute_codes = {
            "rate_match",
            "base_charge",
            "total_amount",
            "shipment_weight",
            "route_match",
            "shipment_carrier_match",
        }
        if critical_codes & dispute_codes:
            return "dispute", "disputed"
        if confidence >= self.settings.auto_approve_threshold and not any(v["severity"] == "warning" for v in validations):
            return "auto_approve", "approved"
        return "flag_for_review", "in_review"

    def _penalties(self, validations: list[dict[str, Any]], contract_candidates: list[RateMatch]) -> dict[str, Any]:
        valid_contract_count = len([candidate for candidate in contract_candidates if candidate.date_valid])
        return {
            "valid_contract_count": valid_contract_count,
            "validation_penalties": [
                {"code": validation["code"], "severity": validation["severity"]} for validation in validations
            ],
        }

    def _validation(
        self,
        code: str,
        severity: str,
        message: str,
        expected: Any = None,
        actual: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "expected": expected,
            "actual": actual,
            "metadata": metadata or {},
        }

    def _contract_candidate_payload(self, candidate: RateMatch | None, bill_date: date) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {
            "id": candidate.contract.id,
            "carrier_id": candidate.contract.carrier_id,
            "effective_date": candidate.contract.effective_date.isoformat(),
            "expiry_date": candidate.contract.expiry_date.isoformat(),
            "status": candidate.contract.status,
            "date_valid": candidate.date_valid,
            "shipment_contract_match": candidate.shipment_contract_match,
            "rate": candidate.rate,
            "bill_date": bill_date.isoformat(),
        }

    def _shipment_payload(self, shipment: Shipment, match_type: str) -> dict[str, Any]:
        return {
            "id": shipment.id,
            "carrier_id": shipment.carrier_id,
            "contract_id": shipment.contract_id,
            "lane": shipment.lane,
            "shipment_date": shipment.shipment_date.isoformat(),
            "status": shipment.status,
            "total_weight_kg": shipment.total_weight_kg,
            "match_type": match_type,
        }

    def _bol_payload(self, bol: BillOfLading) -> dict[str, Any]:
        return {
            "id": bol.id,
            "shipment_id": bol.shipment_id,
            "delivery_date": bol.delivery_date.isoformat(),
            "actual_weight_kg": bol.actual_weight_kg,
            "notes": bol.notes,
        }

    def _bill_payload(self, bill: FreightBill) -> dict[str, Any]:
        return {
            "id": bill.id,
            "carrier_id": bill.carrier_id,
            "carrier_name": bill.carrier_name,
            "bill_number": bill.bill_number,
            "bill_date": bill.bill_date.isoformat(),
            "shipment_reference": bill.shipment_reference,
            "lane": bill.lane,
            "billed_weight_kg": bill.billed_weight_kg,
            "rate_per_kg": bill.rate_per_kg,
            "base_charge": bill.base_charge,
            "fuel_surcharge": bill.fuel_surcharge,
            "gst_amount": bill.gst_amount,
            "total_amount": bill.total_amount,
        }
