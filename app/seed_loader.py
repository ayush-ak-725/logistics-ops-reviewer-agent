import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import BillOfLading, Carrier, CarrierContract, GraphEdge, Shipment


logger = logging.getLogger(__name__)


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def load_seed_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def json_safe(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def get_seed_freight_bill(path: Path, freight_bill_id: str) -> dict[str, Any] | None:
    data = load_seed_file(path)
    for bill in data.get("freight_bills", []):
        if bill["id"] == freight_bill_id:
            normalized = {key: value for key, value in bill.items() if not key.startswith("_")}
            normalized["bill_date"] = parse_date(normalized["bill_date"])
            return normalized
    return None


def seed_reference_data(db: Session, path: Path) -> None:
    data = load_seed_file(path)
    logger.info(
        "seed_reference_data_started",
        extra={
            "seed_path": str(path),
            "carrier_count": len(data.get("carriers", [])),
            "contract_count": len(data.get("carrier_contracts", [])),
            "shipment_count": len(data.get("shipments", [])),
            "bol_count": len(data.get("bills_of_lading", [])),
        },
    )

    for carrier in data.get("carriers", []):
        db.merge(
            Carrier(
                id=carrier["id"],
                name=carrier["name"],
                carrier_code=carrier.get("carrier_code"),
                gstin=carrier.get("gstin"),
                bank_account=carrier.get("bank_account"),
                status=carrier.get("status", "active"),
                onboarded_on=parse_date(carrier.get("onboarded_on")),
                raw_payload=carrier,
            )
        )
    db.flush()

    for contract in data.get("carrier_contracts", []):
        db.merge(
            CarrierContract(
                id=contract["id"],
                carrier_id=contract["carrier_id"],
                effective_date=parse_date(contract["effective_date"]),
                expiry_date=parse_date(contract["expiry_date"]),
                status=contract.get("status", "active"),
                notes=contract.get("notes"),
                rate_card=contract.get("rate_card", []),
                raw_payload=contract,
            )
        )
    db.flush()

    for shipment in data.get("shipments", []):
        db.merge(
            Shipment(
                id=shipment["id"],
                carrier_id=shipment["carrier_id"],
                contract_id=shipment.get("contract_id"),
                lane=shipment["lane"],
                shipment_date=parse_date(shipment["shipment_date"]),
                status=shipment.get("status", "unknown"),
                total_weight_kg=float(shipment["total_weight_kg"]),
                notes=shipment.get("notes"),
                raw_payload=shipment,
            )
        )
    db.flush()

    for bol in data.get("bills_of_lading", []):
        db.merge(
            BillOfLading(
                id=bol["id"],
                shipment_id=bol["shipment_id"],
                delivery_date=parse_date(bol["delivery_date"]),
                actual_weight_kg=float(bol["actual_weight_kg"]),
                notes=bol.get("notes") or bol.get("_note"),
                raw_payload=bol,
            )
        )

    db.flush()
    rebuild_graph_edges(db)
    db.commit()
    logger.info("seed_reference_data_completed", extra={"seed_path": str(path)})


def rebuild_graph_edges(db: Session) -> None:
    logger.info("graph_edge_rebuild_started")
    db.execute(delete(GraphEdge))

    carriers = db.scalars(select(Carrier)).all()
    contracts = db.scalars(select(CarrierContract)).all()
    shipments = db.scalars(select(Shipment)).all()
    bols = db.scalars(select(BillOfLading)).all()

    for carrier in carriers:
        db.add(edge("carrier", carrier.id, "HAS_STATUS", "status", carrier.status))

    for contract in contracts:
        db.add(edge("carrier", contract.carrier_id, "HAS_CONTRACT", "contract", contract.id))
        for item in contract.rate_card:
            db.add(
                edge(
                    "contract",
                    contract.id,
                    "COVERS_LANE",
                    "lane",
                    item["lane"],
                    {
                        "effective_date": contract.effective_date.isoformat(),
                        "expiry_date": contract.expiry_date.isoformat(),
                        "status": contract.status,
                        "rate": item,
                    },
                )
            )

    for shipment in shipments:
        db.add(edge("carrier", shipment.carrier_id, "MOVED_SHIPMENT", "shipment", shipment.id))
        if shipment.contract_id:
            db.add(edge("shipment", shipment.id, "BOOKED_UNDER", "contract", shipment.contract_id))
        db.add(edge("shipment", shipment.id, "ON_LANE", "lane", shipment.lane))

    for bol in bols:
        db.add(edge("shipment", bol.shipment_id, "HAS_BOL", "bol", bol.id))

    edge_count = len(carriers) + len(contracts) + sum(len(contract.rate_card) for contract in contracts)
    edge_count += len(shipments) + len([shipment for shipment in shipments if shipment.contract_id]) + len(shipments)
    edge_count += len(bols)
    logger.info(
        "graph_edge_rebuild_completed",
        extra={
            "carrier_count": len(carriers),
            "contract_count": len(contracts),
            "shipment_count": len(shipments),
            "bol_count": len(bols),
            "edge_count": edge_count,
        },
    )


def edge(
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
    properties: dict[str, Any] | None = None,
) -> GraphEdge:
    return GraphEdge(
        source_type=source_type,
        source_id=source_id,
        relation=relation,
        target_type=target_type,
        target_id=target_id,
        properties=properties or {},
    )
