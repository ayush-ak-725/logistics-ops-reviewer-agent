from dataclasses import dataclass
from datetime import date
import logging
from typing import Any

import networkx as nx
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.llm import CarrierNameNormalizer
from app.models import BillOfLading, Carrier, CarrierContract, GraphEdge, Shipment


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateMatch:
    contract: CarrierContract
    rate: dict[str, Any]
    date_valid: bool
    lane_match: bool
    shipment_contract_match: bool


class FreightGraph:
    """NetworkX read model over persisted graph edges.

    The database is the source of truth; NetworkX gives the agent graph traversal
    semantics without needing a Neo4j dependency for this assignment.
    """

    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings
        self.normalizer = CarrierNameNormalizer(settings) if settings else None
        self.graph = nx.MultiDiGraph()
        self._load()

    def _node(self, node_type: str, node_id: str) -> str:
        return f"{node_type}:{node_id}"

    def _load(self) -> None:
        edges = self.db.scalars(select(GraphEdge)).all()
        for edge in edges:
            source = self._node(edge.source_type, edge.source_id)
            target = self._node(edge.target_type, edge.target_id)
            self.graph.add_node(source, type=edge.source_type, id=edge.source_id)
            self.graph.add_node(target, type=edge.target_type, id=edge.target_id)
            self.graph.add_edge(source, target, relation=edge.relation, **(edge.properties or {}))
        logger.debug(
            "graph_loaded",
            extra={
                "node_count": self.graph.number_of_nodes(),
                "edge_count": len(edges),
            },
        )

    def find_carrier(self, carrier_id: str | None, carrier_name: str) -> tuple[Carrier | None, float, str]:
        if carrier_id:
            carrier = self.db.get(Carrier, carrier_id)
            if carrier:
                logger.info(
                    "graph_carrier_matched",
                    extra={
                        "carrier_id": carrier.id,
                        "carrier_name": carrier.name,
                        "match_type": "carrier_id",
                        "match_score": 1.0,
                    },
                )
                return carrier, 1.0, "carrier_id"

        carriers = self.db.scalars(select(Carrier)).all()
        best: tuple[Carrier | None, float] = (None, 0.0)
        for carrier in carriers:
            score = fuzz.token_set_ratio(carrier_name.lower(), carrier.name.lower()) / 100
            if score > best[1]:
                best = (carrier, score)
        if best[0] and best[1] >= 0.82:
            logger.info(
                "graph_carrier_matched",
                extra={
                    "carrier_id": best[0].id,
                    "carrier_name": best[0].name,
                    "input_carrier_name": carrier_name,
                    "match_type": "fuzzy_name",
                    "match_score": round(best[1], 3),
                },
            )
            return best[0], best[1], "fuzzy_name"

        if self.normalizer:
            normalized = self.normalizer.normalize(
                carrier_name,
                [{"id": carrier.id, "name": carrier.name, "carrier_code": carrier.carrier_code or ""} for carrier in carriers],
                best[1],
            )
            if normalized:
                carrier = self.db.get(Carrier, normalized["carrier_id"])
                if carrier:
                    logger.info(
                        "graph_carrier_matched",
                        extra={
                            "carrier_id": carrier.id,
                            "carrier_name": carrier.name,
                            "input_carrier_name": carrier_name,
                            "match_type": "llm_name_normalization",
                            "match_score": round(float(normalized["confidence"]), 3),
                            "normalization_reason": normalized.get("reason"),
                        },
                    )
                    return carrier, float(normalized["confidence"]), "llm_name_normalization"
        logger.info(
            "graph_carrier_unmatched",
            extra={"input_carrier_id": carrier_id, "input_carrier_name": carrier_name, "best_score": round(best[1], 3)},
        )
        return None, best[1], "unmatched"

    def contract_candidates(
        self,
        carrier_id: str,
        lane: str,
        bill_date: date,
        shipment: Shipment | None = None,
    ) -> list[RateMatch]:
        contracts = self.db.scalars(
            select(CarrierContract).where(CarrierContract.carrier_id == carrier_id)
        ).all()
        candidates: list[RateMatch] = []
        for contract in contracts:
            for rate in contract.rate_card:
                if rate.get("lane") != lane:
                    continue
                candidates.append(
                    RateMatch(
                        contract=contract,
                        rate=rate,
                        date_valid=contract.effective_date <= bill_date <= contract.expiry_date,
                        lane_match=True,
                        shipment_contract_match=bool(shipment and shipment.contract_id == contract.id),
                    )
                )
        logger.info(
            "graph_contract_candidates_found",
            extra={
                "carrier_id": carrier_id,
                "lane": lane,
                "bill_date": bill_date.isoformat(),
                "shipment_id": shipment.id if shipment else None,
                "candidate_count": len(candidates),
                "valid_candidate_ids": [candidate.contract.id for candidate in candidates if candidate.date_valid],
                "all_candidate_ids": [candidate.contract.id for candidate in candidates],
            },
        )
        return candidates

    def find_shipment(
        self,
        carrier_id: str | None,
        lane: str,
        shipment_reference: str | None,
        bill_date: date,
    ) -> tuple[Shipment | None, list[Shipment], str]:
        if shipment_reference:
            shipment = self.db.get(Shipment, shipment_reference)
            if shipment:
                logger.info(
                    "graph_shipment_matched",
                    extra={
                        "shipment_id": shipment.id,
                        "match_type": "shipment_reference",
                        "carrier_id": shipment.carrier_id,
                        "lane": shipment.lane,
                    },
                )
                return shipment, [shipment], "shipment_reference"
            logger.info(
                "graph_shipment_reference_not_found",
                extra={"shipment_reference": shipment_reference, "carrier_id": carrier_id, "lane": lane},
            )
            return None, [], "shipment_reference_not_found"

        query = select(Shipment).where(Shipment.lane == lane, Shipment.shipment_date <= bill_date)
        if carrier_id:
            query = query.where(Shipment.carrier_id == carrier_id)
        candidates = list(self.db.scalars(query).all())
        if len(candidates) == 1:
            logger.info(
                "graph_shipment_matched",
                extra={
                    "shipment_id": candidates[0].id,
                    "match_type": "single_lane_candidate",
                    "carrier_id": carrier_id,
                    "lane": lane,
                },
            )
            return candidates[0], candidates, "single_lane_candidate"
        logger.info(
            "graph_shipment_candidates_found",
            extra={
                "carrier_id": carrier_id,
                "lane": lane,
                "bill_date": bill_date.isoformat(),
                "candidate_count": len(candidates),
                "candidate_ids": [candidate.id for candidate in candidates],
            },
        )
        return None, candidates, "no_reference_ambiguous" if candidates else "no_reference_no_candidate"

    def shipment_bols(self, shipment_id: str | None) -> list[BillOfLading]:
        if not shipment_id:
            return []
        return list(self.db.scalars(select(BillOfLading).where(BillOfLading.shipment_id == shipment_id)).all())

    def graph_path(self, carrier: Carrier | None, contract: CarrierContract | None, shipment: Shipment | None) -> list[str]:
        path: list[str] = []
        if carrier:
            path.append(self._node("carrier", carrier.id))
        if contract:
            path.append(self._node("contract", contract.id))
        if shipment:
            path.append(self._node("shipment", shipment.id))
            for bol in self.shipment_bols(shipment.id):
                path.append(self._node("bol", bol.id))
        return path
