from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import FreightBillAgent
from app.config import Settings
from app.models import AuditEvent, BillOfLading, Carrier, CarrierContract, FreightBill, GraphEdge, Shipment
from app.schemas import FreightBillIn, ReviewRequest
from app.seed_loader import get_seed_freight_bill, json_safe


logger = logging.getLogger(__name__)


class FreightBillService:
    def __init__(self, db: Session, settings: Settings, agent: FreightBillAgent):
        self.db = db
        self.settings = settings
        self.agent = agent

    def ingest(self, payload: FreightBillIn) -> FreightBill:
        logger.info(
            "freight_bill_ingest_started",
            extra={"bill_id": payload.id, "payload_mode": "seed_id" if payload.carrier_name is None else "full_payload"},
        )
        if self.db.get(FreightBill, payload.id):
            logger.warning("freight_bill_ingest_rejected_duplicate_id", extra={"bill_id": payload.id})
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Freight bill {payload.id} already exists.")

        bill_data = self._resolve_bill_payload(payload)
        logger.info(
            "freight_bill_payload_resolved",
            extra={
                "bill_id": bill_data["id"],
                "carrier_id": bill_data.get("carrier_id"),
                "carrier_name": bill_data.get("carrier_name"),
                "bill_number": bill_data.get("bill_number"),
                "lane": bill_data.get("lane"),
                "shipment_reference": bill_data.get("shipment_reference"),
                "total_amount": bill_data.get("total_amount"),
            },
        )
        bill = FreightBill(**bill_data, raw_payload=json_safe(bill_data), status="received")
        self.db.add(bill)
        self.db.add(AuditEvent(freight_bill_id=bill.id, event_type="freight_bill_ingested", payload=json_safe(bill_data)))
        self.db.commit()

        self.agent.process(bill.id)
        self.db.refresh(bill)
        logger.info(
            "freight_bill_ingest_completed",
            extra={
                "bill_id": bill.id,
                "status": bill.status,
                "decision": bill.decision,
                "confidence": bill.confidence,
            },
        )
        return bill

    def get(self, bill_id: str) -> FreightBill:
        logger.info("freight_bill_get_requested", extra={"bill_id": bill_id})
        bill = self.db.get(FreightBill, bill_id)
        if not bill:
            logger.warning("freight_bill_get_not_found", extra={"bill_id": bill_id})
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Freight bill {bill_id} not found.")
        logger.info(
            "freight_bill_get_completed",
            extra={"bill_id": bill_id, "status": bill.status, "decision": bill.decision, "confidence": bill.confidence},
        )
        return bill

    def review_queue(self) -> list[FreightBill]:
        bills = list(
            self.db.scalars(
                select(FreightBill).where(FreightBill.status == "in_review").order_by(FreightBill.created_at.asc())
            ).all()
        )
        logger.info("review_queue_listed", extra={"count": len(bills), "bill_ids": [bill.id for bill in bills]})
        return bills

    def submit_review(self, bill_id: str, review: ReviewRequest) -> FreightBill:
        logger.info(
            "review_submission_started",
            extra={"bill_id": bill_id, "review_decision": review.decision, "has_modifications": bool(review.modifications)},
        )
        bill = self.get(bill_id)
        if bill.status != "in_review":
            logger.warning(
                "review_submission_rejected_not_waiting",
                extra={"bill_id": bill_id, "current_status": bill.status, "review_decision": review.decision},
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Freight bill {bill_id} is {bill.status}, not waiting for review.",
            )
        self.agent.resume(bill_id, review.model_dump(exclude_none=True))
        self.db.refresh(bill)
        logger.info(
            "review_submission_completed",
            extra={
                "bill_id": bill_id,
                "status": bill.status,
                "decision": bill.decision,
                "confidence": bill.confidence,
                "review_decision": review.decision,
            },
        )
        return bill

    def metrics(self) -> dict[str, Any]:
        rows = self.db.scalars(select(FreightBill)).all()
        if not rows:
            payload = {
                "total_bills": 0,
                "auto_approved": 0,
                "in_review": 0,
                "disputed": 0,
                "reviewed": 0,
                "average_confidence": None,
            }
            logger.info("metrics_computed", extra=payload)
            return payload
        avg_confidence = self.db.scalar(select(func.avg(FreightBill.confidence)))
        payload = {
            "total_bills": len(rows),
            "auto_approved": sum(1 for row in rows if row.status == "approved"),
            "in_review": sum(1 for row in rows if row.status == "in_review"),
            "disputed": sum(1 for row in rows if row.status == "disputed"),
            "reviewed": sum(1 for row in rows if row.status == "reviewed"),
            "average_confidence": round(float(avg_confidence), 3) if avg_confidence is not None else None,
        }
        logger.info("metrics_computed", extra=payload)
        return payload

    def graph_snapshot(self) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}

        for carrier in self.db.scalars(select(Carrier)).all():
            nodes[f"carrier:{carrier.id}"] = {"id": f"carrier:{carrier.id}", "type": "carrier", "label": carrier.name}
        for contract in self.db.scalars(select(CarrierContract)).all():
            nodes[f"contract:{contract.id}"] = {
                "id": f"contract:{contract.id}",
                "type": "contract",
                "label": contract.id,
                "status": contract.status,
            }
        for shipment in self.db.scalars(select(Shipment)).all():
            nodes[f"shipment:{shipment.id}"] = {
                "id": f"shipment:{shipment.id}",
                "type": "shipment",
                "label": shipment.id,
                "lane": shipment.lane,
            }
        for bol in self.db.scalars(select(BillOfLading)).all():
            nodes[f"bol:{bol.id}"] = {"id": f"bol:{bol.id}", "type": "bol", "label": bol.id}

        edges = []
        for edge in self.db.scalars(select(GraphEdge).order_by(GraphEdge.source_type, GraphEdge.source_id)).all():
            source = f"{edge.source_type}:{edge.source_id}"
            target = f"{edge.target_type}:{edge.target_id}"
            nodes.setdefault(source, {"id": source, "type": edge.source_type, "label": edge.source_id})
            nodes.setdefault(target, {"id": target, "type": edge.target_type, "label": edge.target_id})
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "relation": edge.relation,
                    "properties": edge.properties or {},
                }
            )

        payload = {"nodes": list(nodes.values()), "edges": edges}
        logger.info("graph_snapshot_generated", extra={"node_count": len(payload["nodes"]), "edge_count": len(edges)})
        return payload

    def graph_mermaid(self) -> str:
        snapshot = self.graph_snapshot()
        lines = ["flowchart LR"]
        for node in snapshot["nodes"]:
            node_id = self._mermaid_id(node["id"])
            label = f'{node["type"]}: {node["label"]}'
            lines.append(f'    {node_id}["{label}"]')
        for edge in snapshot["edges"]:
            source = self._mermaid_id(edge["source"])
            target = self._mermaid_id(edge["target"])
            relation = edge["relation"]
            lines.append(f"    {source} -- {relation} --> {target}")
        return "\n".join(lines)

    def _resolve_bill_payload(self, payload: FreightBillIn) -> dict[str, Any]:
        if payload.carrier_name is None:
            seed_bill = get_seed_freight_bill(self.settings.seed_data_path, payload.id)
            if not seed_bill:
                logger.warning("seed_freight_bill_not_found", extra={"bill_id": payload.id})
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No seed freight bill found for id {payload.id}.",
                )
            return seed_bill

        return payload.model_dump(exclude_none=False)

    def _mermaid_id(self, value: str) -> str:
        return value.replace(":", "_").replace("-", "_")
