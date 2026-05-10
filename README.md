# Logistics Ops Reviewer Agent

Local freight audit system for matching carrier invoices to carrier contracts, shipments, and BOLs. It uses FastAPI for the API, Postgres for durable workflow state, SQLAlchemy for the relational model, NetworkX as the graph read model, and LangGraph for the stateful agent with interrupt/resume human review.

## Quick Start

```bash
docker compose up --build
```

The API will be available at:

- API: <http://localhost:8000>
- Swagger docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

The app auto-loads reference seed data from `data/seed_data_logistics.json`. Freight bills are not pre-ingested; submit them one at a time so duplicate and prior-billing logic behaves like an incoming invoice stream.

Example flow:

```bash
curl -X POST http://localhost:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"id":"FB-2025-101"}'

curl -X POST http://localhost:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"id":"FB-2025-102"}'

curl http://localhost:8000/review-queue

curl -X POST http://localhost:8000/review/FB-2025-102 \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approve","notes":"Accepted renewed FY rate after ops review."}'
```

For local development, use a normal Postgres running on your machine. Docker is not required locally.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Install and start Postgres locally, for example with Homebrew:

```bash
brew install postgresql@16
brew services start postgresql@16
```

Create the local `freight` role and database:

```bash
chmod +x scripts/setup_local_postgres.sh
./scripts/setup_local_postgres.sh
```

Run the API:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --port 8001
```

The included `.env` points local Uvicorn to:

```text
postgresql+psycopg://freight:freight@localhost:5432/freight
```

If `DATABASE_URL` is not set, the app can fall back to SQLite, but this repo is configured to use local Postgres through `.env`.

Docker Compose remains available for remote-style packaging and deployment testing:

```bash
docker compose up --build
```

### Render Database

If deploying the API on Render, set `DATABASE_URL` in the Render web service environment to the Render Postgres **Internal Database URL**. If running the API outside Render, use the External Database URL.

Render often provides URLs in this form:

```text
postgresql://USER:PASSWORD@HOST/DB_NAME
```

The app automatically rewrites that to SQLAlchemy's `postgresql+psycopg://` driver internally, so the Render URL can be pasted directly into the environment variable.

Recommended Render variables:

```text
DATABASE_URL=<your Render Postgres internal database URL>
SEED_DATA_PATH=data/seed_data_logistics.json
AUTO_SEED=true
LOG_LEVEL=INFO
LLM_PROVIDER=ollama
ENABLE_LLM_EXPLANATIONS=false
ENABLE_LLM_CARRIER_NORMALIZATION=false
```

For LLM-enabled remote deployment, host Ollama on a separate VM and set `OLLAMA_BASE_URL` to that VM. Do not expose Ollama publicly without authentication.

## Architecture

```mermaid
flowchart LR
    Client["Ops user / integration"] --> API["FastAPI"]
    API --> Service["FreightBillService"]
    Service --> Agent["LangGraph agent"]
    Agent --> Rules["Deterministic decision engine"]
    Rules --> Graph["NetworkX graph read model"]
    Graph --> Edges["graph_edges table"]
    Rules --> DB["Postgres relational tables"]
    Agent --> Interrupt["interrupt() when review needed"]
    Interrupt --> Queue["GET /review-queue"]
    Queue --> Reviewer["Human reviewer"]
    Reviewer --> Resume["POST /review/{id}"]
    Resume --> Agent
    Agent --> Audit["audit_events"]
```

Request path:

1. `POST /freight-bills` accepts either a seed bill id or a full freight bill payload.
2. The bill is persisted as `received`.
3. LangGraph runs `analyze`.
4. The decision engine traverses the graph and validates carrier, contract, route, shipment, BOL, weight, and charge evidence.
5. High-confidence clean bills are finalized as `approved`.
6. Warnings or lower confidence bills enter `in_review`; the graph calls `interrupt()`.
7. `POST /review/{id}` resumes the same LangGraph thread with the reviewer decision.

## API

Required endpoints:

- `POST /freight-bills` - ingest a freight bill. Body can be `{"id":"FB-2025-101"}` for seed bills, or a complete freight bill payload.
- `GET /freight-bills/{id}` - retrieve current state, decision, confidence, explanation, and evidence.
- `GET /review-queue` - list bills waiting for ops review.
- `POST /review/{id}` - resume a paused bill with `approve`, `dispute`, or `modify`; requires `approver_name` for the audit trail.

Useful extra endpoint:

- `GET /metrics` - basic operational counters.
- `GET /graph` - graph nodes and edges as JSON for debugging.
- `GET /graph/mermaid` - Mermaid diagram text for visualizing the graph.

## Schema Design

The relational model keeps operational state durable and queryable:

- `carriers`: onboarded carrier master data.
- `carrier_contracts`: contract header plus `rate_card` JSON. I kept rate card lines embedded because this seed data has small, contract-local pricing structures. In production I would split rate lines into a first-class table once rate versioning, lane-level approvals, and index-heavy search matter.
- `shipments`: shipment records with carrier, contract, lane, date, status, and total weight.
- `bills_of_lading`: delivery evidence tied to shipments.
- `freight_bills`: incoming invoice state, final decision, confidence, evidence chain, and reviewer decision.
- `audit_events`: append-only workflow and decision trail.
- `graph_edges`: persisted relationship table used to build the NetworkX graph read model.

Postgres is the intended local database because it is boring, reliable, and production-shaped for this workflow: transactions, JSON support for evidence payloads, and easy deployment to Cloud SQL. SQLite is supported only as a convenience fallback for quick tests.

## Graph Model

The graph model is persisted as edges, then loaded into NetworkX:

- `carrier -> contract` via `HAS_CONTRACT`
- `contract -> lane` via `COVERS_LANE`
- `carrier -> shipment` via `MOVED_SHIPMENT`
- `shipment -> contract` via `BOOKED_UNDER`
- `shipment -> lane` via `ON_LANE`
- `shipment -> bol` via `HAS_BOL`

I chose NetworkX instead of Neo4j for this assignment because the graph is small and local. The important design point is explicit graph traversal and evidence paths, not running a graph cluster. The `graph_edges` table keeps the model portable: swapping NetworkX for Neo4j later would mainly affect `app/graph_store.py`, not the API or decision engine.

## Agent Design

LangGraph nodes:

- `analyze`: loads the persisted bill, builds candidate matches, runs deterministic validations, and computes a preliminary decision.
- `human_review`: persists `in_review` state and calls `interrupt()` with the evidence payload.
- `finalize`: persists the auto decision or applies the reviewer decision after resume.

The graph is compiled with `MemorySaver` checkpointer. The bill state, decision, evidence, and audit trail are persisted in Postgres; the in-process checkpointer holds the LangGraph resume point. In production I would replace `MemorySaver` with a Postgres-backed LangGraph checkpointer so paused graph frames survive API restarts.

## Deterministic Rules

The LLM is deliberately not trusted for math or approval decisions.

The decision engine validates:

- Carrier match: exact `carrier_id`, then fuzzy carrier-name match.
- Shipment match: explicit `shipment_reference`; otherwise lane/carrier/date candidates.
- Contract match: carrier + lane + effective date; if overlaps exist, prefer shipment contract, then best rate/charge fit.
- Rates: per-kg rate, FTL rate, or allowed FTL alternate kg rate.
- Fuel surcharge: applies `revised_fuel_surcharge_percent` when bill date is after `revised_on`.
- Charges: base, fuel, GST at 18%, and total within currency tolerance.
- Weight: exact BOL weight where possible; otherwise cumulative shipment weight, including prior ingested freight bills.
- Duplicate invoices: same carrier and bill number dispute immediately.

## Confidence Score

Confidence is a weighted score:

- 15% carrier match quality
- 20% shipment match quality
- 20% selected valid contract
- 30% charge/rate reconciliation
- 15% weight/BOL reconciliation

Then penalties are applied:

- overlapping valid contracts
- warnings such as inferred shipment, no exact BOL match, or contract ambiguity
- critical failures such as duplicate bill, rate mismatch, overbilling, or route mismatch

Decision thresholds:

- `>= 0.88` with no warnings: `auto_approve`
- critical dispute conditions: `dispute`
- everything else: `flag_for_review` and pause via LangGraph `interrupt()`

When a reviewer resumes a paused bill, the decision is intentionally marked as a manual outcome:

- reviewer `approve`: `manual_approve` with status `approved`
- reviewer `dispute`: `manual_dispute` with status `disputed`
- reviewer `modify`: `manual_modify` with status `reviewed`

The submitted review payload is stored on `freight_bills.reviewer_decision`, including `approver_name`, notes, and any modifications.

## LLM Usage

LLM calls are optional and off by default. Set:

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
ENABLE_LLM_EXPLANATIONS=true
ENABLE_LLM_CARRIER_NORMALIZATION=true
```

The LLM is used in two bounded places:

- Carrier-name normalization: if `carrier_id` is missing and deterministic fuzzy matching cannot confidently resolve a messy carrier name, the LLM may choose from the known carrier list. It cannot invent a carrier.
- Explanation writing: after deterministic matching, scoring, and decisioning are complete, the LLM can rewrite the evidence into a concise ops-facing explanation.

The LLM does not choose contracts, compute charges, score confidence, or approve/dispute bills. If Ollama is unavailable or LLM flags are disabled, deterministic fuzzy matching and deterministic explanations are used.

Local Ollama setup:

```bash
ollama pull qwen3:8b
ollama serve
```

Then run the API:

```bash
LLM_PROVIDER=ollama \
OLLAMA_MODEL=qwen3:8b \
ENABLE_LLM_EXPLANATIONS=true \
ENABLE_LLM_CARRIER_NORMALIZATION=true \
uvicorn app.main:app --reload --port 8001
```

If your local Ollama model tag is literally `qwen3:8B`, set `OLLAMA_MODEL=qwen3:8B` to match `ollama list`.

Ollama Cloud style:

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=<your Ollama API key>
OLLAMA_MODEL=qwen3:8b
ENABLE_LLM_EXPLANATIONS=true
ENABLE_LLM_CARRIER_NORMALIZATION=true
```

Free remote option:

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=<your OpenRouter key>
OPENAI_MODEL=qwen/qwen3-8b:free
ENABLE_LLM_EXPLANATIONS=true
ENABLE_LLM_CARRIER_NORMALIZATION=true
```

OpenRouter free models are useful for demos, but they may have tighter rate limits and availability can change.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covered examples:

- clean match auto-approves
- duplicate invoice disputes
- FTL alternate kg billing reconciles
- cumulative overbilling disputes after a prior bill

## Logs And Traceability

The app emits structured JSON logs to stdout. A single freight bill can be followed end-to-end by filtering on `bill_id`.

Run locally and save logs:

```bash
LOG_LEVEL=INFO uvicorn app.main:app --reload --port 8001 2>&1 | tee run.log
```

Useful events:

- `seed_reference_data_started` / `seed_reference_data_completed`
- `graph_edge_rebuild_started` / `graph_edge_rebuild_completed`
- `http_request_started` / `http_request_completed`
- `freight_bill_ingest_started` / `freight_bill_ingest_completed`
- `agent_process_started`, `agent_node_analyze_started`, `agent_paused_for_review`, `agent_resume_started`
- `graph_carrier_matched`, `graph_shipment_matched`, `graph_contract_candidates_found`
- `decision_match_summary`, `decision_charge_validation_completed`, `decision_weight_validation_completed`
- `decision_engine_completed`
- `llm_carrier_normalization_started`, `llm_carrier_normalization_completed`
- `llm_explanation_started`, `llm_explanation_completed`
- `review_submission_started` / `review_submission_completed`

Example:

```bash
grep 'FB-2025-102' run.log
```

That shows carrier matching, shipment inference, overlapping contract candidates, selected contract, charge math, weight reconciliation, confidence, pause reason, and reviewer resume.

## Trade-offs

- NetworkX is enough for the assignment-scale graph; Neo4j would be better if relationship queries became deep, multi-hop, or operationally important across millions of records.
- Rate cards are JSON on contracts for speed of implementation and readability. A normalized `contract_rate_lines` table would be my next step.
- LangGraph uses in-memory checkpoints while durable business state lives in Postgres. A production version should use a durable LangGraph checkpointer.
- There is no Alembic migration setup. `Base.metadata.create_all()` is fine for a take-home, but migrations are required for production.
- The confidence model is transparent and tunable, not learned. That is intentional because ops teams need to understand why a bill paused.

## What I Would Do Next

- Add Alembic migrations and indexes for bill number, carrier, lane, status, and bill date access paths.
- Add a Postgres LangGraph checkpointer.
- Split rate card lines into a normalized table with effective sub-periods and fuel surcharge revisions.
- Add idempotency keys for external ingestion.
- Add a lightweight reviewer UI.
- Add OpenTelemetry traces around each agent node and validation rule.
- Deploy API to Cloud Run, Postgres to Cloud SQL, and secrets to GCP Secret Manager.
