from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import Settings
from app.db import SessionLocal
from app.decision_engine import DecisionEngine, DecisionResult
from app.models import AuditEvent, FreightBill


logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    bill_id: str
    analysis: dict[str, Any]
    review: dict[str, Any]


class FreightBillAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        builder = StateGraph(AgentState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("human_review", self._human_review)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "analyze")
        builder.add_conditional_edges(
            "analyze",
            self._route_after_analysis,
            {"human_review": "human_review", "finalize": "finalize"},
        )
        builder.add_edge("human_review", "finalize")
        builder.add_edge("finalize", END)
        self.graph = builder.compile(checkpointer=MemorySaver())

    def process(self, bill_id: str) -> dict[str, Any]:
        logger.info("agent_process_started", extra={"bill_id": bill_id, "thread_id": bill_id})
        config = {"configurable": {"thread_id": bill_id}}
        result = self.graph.invoke({"bill_id": bill_id}, config=config)
        logger.info("agent_process_completed", extra={"bill_id": bill_id, "thread_id": bill_id})
        return result

    def resume(self, bill_id: str, review: dict[str, Any]) -> dict[str, Any]:
        logger.info(
            "agent_resume_started",
            extra={"bill_id": bill_id, "thread_id": bill_id, "review_decision": review.get("decision")},
        )
        config = {"configurable": {"thread_id": bill_id}}
        result = self.graph.invoke(Command(resume=review), config=config)
        logger.info(
            "agent_resume_completed",
            extra={"bill_id": bill_id, "thread_id": bill_id, "review_decision": review.get("decision")},
        )
        return result

    def _analyze(self, state: AgentState) -> AgentState:
        logger.info("agent_node_analyze_started", extra={"bill_id": state["bill_id"]})
        with SessionLocal() as db:
            bill = db.get(FreightBill, state["bill_id"])
            if not bill:
                raise ValueError(f"Freight bill {state['bill_id']} not found")
            result = DecisionEngine(db, self.settings).evaluate(bill)
            logger.info(
                "agent_node_analyze_completed",
                extra={
                    "bill_id": state["bill_id"],
                    "decision": result.decision,
                    "status": result.status,
                    "confidence": result.confidence,
                },
            )
            return {"analysis": self._result_payload(result)}

    def _route_after_analysis(self, state: AgentState) -> str:
        return "human_review" if state["analysis"]["status"] == "in_review" else "finalize"

    def _human_review(self, state: AgentState) -> AgentState:
        if not self._already_waiting_for_review(state["bill_id"]):
            self._persist_analysis(state["bill_id"], state["analysis"], "agent_paused_for_review")
            logger.info(
                "agent_paused_for_review",
                extra={
                    "bill_id": state["bill_id"],
                    "decision": state["analysis"]["decision"],
                    "confidence": state["analysis"]["confidence"],
                    "validation_codes": [
                        item["code"] for item in state["analysis"].get("evidence", {}).get("validations", [])
                    ],
                },
            )
        review_payload = interrupt(
            {
                "freight_bill_id": state["bill_id"],
                "reason": "confidence_below_auto_approval_threshold_or_warning_present",
                "decision": state["analysis"]["decision"],
                "confidence": state["analysis"]["confidence"],
                "evidence": state["analysis"]["evidence"],
                "message": "Submit POST /review/{id} with approve, dispute, or modify to resume.",
            }
        )
        logger.info(
            "agent_review_interrupt_resumed",
            extra={"bill_id": state["bill_id"], "review_decision": review_payload.get("decision")},
        )
        return {"review": review_payload}

    def _finalize(self, state: AgentState) -> AgentState:
        if "review" in state:
            logger.info(
                "agent_node_finalize_review_started",
                extra={"bill_id": state["bill_id"], "review_decision": state["review"].get("decision")},
            )
            with SessionLocal() as db:
                bill = db.get(FreightBill, state["bill_id"])
                if not bill:
                    raise ValueError(f"Freight bill {state['bill_id']} not found")
                result = DecisionEngine(db, self.settings).apply_review(bill, state["review"])
                payload = self._result_payload(result)
                self._persist_analysis(state["bill_id"], payload, "review_applied", state["review"])
                logger.info(
                    "agent_node_finalize_review_completed",
                    extra={
                        "bill_id": state["bill_id"],
                        "decision": payload["decision"],
                        "status": payload["status"],
                        "confidence": payload["confidence"],
                    },
                )
                return {"analysis": payload}

        self._persist_analysis(state["bill_id"], state["analysis"], "agent_finalized")
        logger.info(
            "agent_node_finalize_completed",
            extra={
                "bill_id": state["bill_id"],
                "decision": state["analysis"]["decision"],
                "status": state["analysis"]["status"],
                "confidence": state["analysis"]["confidence"],
            },
        )
        return state

    def _persist_analysis(
        self,
        bill_id: str,
        analysis: dict[str, Any],
        event_type: str,
        review: dict[str, Any] | None = None,
    ) -> None:
        with SessionLocal() as db:
            bill = db.get(FreightBill, bill_id)
            if not bill:
                raise ValueError(f"Freight bill {bill_id} not found")
            bill.decision = analysis["decision"]
            bill.status = analysis["status"]
            bill.confidence = analysis["confidence"]
            bill.evidence = analysis["evidence"]
            bill.explanation = analysis["explanation"]
            if review is not None:
                bill.reviewer_decision = review
            logger.info(
                "agent_persisting_analysis",
                extra={
                    "bill_id": bill_id,
                    "event_type": event_type,
                    "decision": analysis["decision"],
                    "status": analysis["status"],
                    "confidence": analysis["confidence"],
                    "has_review": review is not None,
                },
            )
            db.add(
                AuditEvent(
                    freight_bill_id=bill_id,
                    event_type=event_type,
                    payload={"analysis": analysis, "review": review},
                )
            )
            db.commit()

    def _already_waiting_for_review(self, bill_id: str) -> bool:
        with SessionLocal() as db:
            bill = db.get(FreightBill, bill_id)
            return bool(bill and bill.status == "in_review")

    def _result_payload(self, result: DecisionResult) -> dict[str, Any]:
        return {
            "decision": result.decision,
            "status": result.status,
            "confidence": result.confidence,
            "evidence": result.evidence,
            "explanation": result.explanation,
        }
