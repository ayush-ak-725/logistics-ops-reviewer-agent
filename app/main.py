from contextlib import asynccontextmanager
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from pythonjsonlogger import jsonlogger
from sqlalchemy.orm import Session

from app.agent import FreightBillAgent
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db, init_db
from app.schemas import FreightBillIn, FreightBillResponse, MetricsResponse, ReviewItem, ReviewRequest
from app.seed_loader import seed_reference_data
from app.service import FreightBillService


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    init_db()
    if settings.auto_seed:
        with SessionLocal() as db:
            seed_reference_data(db, settings.seed_data_path)
    app.state.agent = FreightBillAgent(settings)
    logging.getLogger(__name__).info("freight_bill_api_started", extra={"seed_path": str(settings.seed_data_path)})
    yield


app = FastAPI(
    title="Logistics Ops Reviewer Agent",
    version="0.1.0",
    description="FastAPI + LangGraph service for matching, validating, and reviewing carrier freight bills.",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid4()))
    start = perf_counter()
    logger = logging.getLogger(__name__)
    logger.info(
        "http_request_started",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
        },
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "http_request_failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round((perf_counter() - start) * 1000, 2),
            },
        )
        raise
    response.headers["x-request-id"] = request_id
    logger.info(
        "http_request_completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((perf_counter() - start) * 1000, 2),
        },
    )
    return response


def get_service(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> FreightBillService:
    return FreightBillService(db=db, settings=settings, agent=app.state.agent)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/freight-bills", response_model=FreightBillResponse, status_code=201)
def ingest_freight_bill(payload: FreightBillIn, service: FreightBillService = Depends(get_service)):
    return service.ingest(payload)


@app.get("/freight-bills/{bill_id}", response_model=FreightBillResponse)
def get_freight_bill(bill_id: str, service: FreightBillService = Depends(get_service)):
    return service.get(bill_id)


@app.get("/review-queue", response_model=list[ReviewItem])
def review_queue(service: FreightBillService = Depends(get_service)):
    return service.review_queue()


@app.post("/review/{bill_id}", response_model=FreightBillResponse)
def submit_review(bill_id: str, payload: ReviewRequest, service: FreightBillService = Depends(get_service)):
    return service.submit_review(bill_id, payload)


@app.get("/metrics", response_model=MetricsResponse)
def metrics(service: FreightBillService = Depends(get_service)):
    return service.metrics()


@app.get("/graph")
def graph_snapshot(service: FreightBillService = Depends(get_service)):
    return service.graph_snapshot()


@app.get("/graph/mermaid")
def graph_mermaid(service: FreightBillService = Depends(get_service)):
    return Response(content=service.graph_mermaid(), media_type="text/plain")
