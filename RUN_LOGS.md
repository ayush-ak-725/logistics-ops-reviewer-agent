# Run Logs

These are the meaningful logs and command outputs from the implementation and verification runs.

## Initial Pytest Attempt With System Python

Command:

```bash
python3 -m pytest -q
```

Result:

```text
Fatal Python error: Segmentation fault

Current thread ...:
  File "/opt/homebrew/anaconda3/lib/python3.13/rlcompleter.py", line 212 in <module>
  File "/opt/homebrew/anaconda3/lib/python3.13/pdb.py", line 93 in <module>
  File "/opt/homebrew/anaconda3/lib/python3.13/site-packages/_pytest/debugging.py", line 66 in pytest_configure
```

Cause: the default Anaconda Python 3.13 crashed before importing the app. I switched to Python 3.11.

## Virtualenv Setup

Command:

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Result:

```text
Successfully installed fastapi uvicorn sqlalchemy psycopg pydantic pydantic-settings
langgraph langchain-openai networkx rapidfuzz python-json-logger httpx pytest
pytest-asyncio and transitive dependencies
```

## First Test Failures

Command:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
FFFF
TypeError: SQLite Date type only accepts Python date objects as input.
```

Fix: normalized freight bill `bill_date` from string to Python `date` in `app/seed_loader.py`.

Second failure:

```text
TypeError: Object of type date is not JSON serializable
```

Fix: added `json_safe()` for `raw_payload` and audit payload JSON fields.

## Passing Unit Tests

Command:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
....                                                                     [100%]
4 passed in 0.21s
```

Covered:

- clean freight bill auto-approves
- duplicate invoice disputes
- FTL alternate kg billing reconciles
- cumulative shipment overbilling disputes

## API Smoke Test

Command:

```bash
rm -f freight_bill_agent.db
.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    r1 = client.post('/freight-bills', json={'id': 'FB-2025-101'})
    r2 = client.post('/freight-bills', json={'id': 'FB-2025-102'})
    q = client.get('/review-queue')
    rr = client.post('/review/FB-2025-102', json={
        'decision': 'approve',
        'notes': 'Accepted renewed FY rate after ops review.'
    })
    print(r1.status_code, r1.json()['status'], r1.json()['decision'], r1.json()['confidence'])
    print(r2.status_code, r2.json()['status'], r2.json()['decision'], r2.json()['confidence'])
    print(q.status_code, [x['id'] for x in q.json()])
    print(rr.status_code, rr.json()['status'], rr.json()['decision'])
PY
```

Result:

```text
201 approved auto_approve 1.0
201 in_review flag_for_review 0.618
200 ['FB-2025-102']
200 approved auto_approve
```

Relevant structured logs:

```text
{"levelname": "INFO", "name": "app.main", "message": "freight_bill_api_started", "seed_path": "data/seed_data_logistics.json"}
{"levelname": "INFO", "name": "httpx", "message": "HTTP Request: POST http://testserver/freight-bills \"HTTP/1.1 201 Created\""}
{"levelname": "INFO", "name": "httpx", "message": "HTTP Request: GET http://testserver/review-queue \"HTTP/1.1 200 OK\""}
{"levelname": "INFO", "name": "httpx", "message": "HTTP Request: POST http://testserver/review/FB-2025-102 \"HTTP/1.1 200 OK\""}
```

## Docker Compose Config Check

Command:

```bash
docker compose config
```

Result:

```text
services:
  api:
    environment:
      AUTO_SEED: "true"
      DATABASE_URL: postgresql+psycopg://freight:freight@postgres:5432/freight
      ENABLE_LLM_EXPLANATIONS: "false"
      LOG_LEVEL: INFO
      SEED_DATA_PATH: /app/data/seed_data_logistics.json
    ports:
      - target: 8000
        published: "8000"
  postgres:
    image: postgres:16-alpine
```

## Compile Check

Command:

```bash
.venv/bin/python -m compileall app tests
```

Result:

```text
Listing 'app'...
Listing 'tests'...
Compiling 'tests/test_decision_engine.py'...
```

## Local Uvicorn Run

Command:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Result:

```text
INFO:     Started server process [14569]
INFO:     Waiting for application startup.
{"levelname": "INFO", "name": "app.main", "message": "freight_bill_api_started", "seed_path": "data/seed_data_logistics.json"}
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8001 (Press CTRL+C to quit)
```

Health and ingest check:

```bash
curl -s http://127.0.0.1:8001/health
curl -s -X POST http://127.0.0.1:8001/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"id":"FB-2025-101"}'
```

Result:

```text
{"status":"ok"}

{
  "id": "FB-2025-101",
  "status": "approved",
  "decision": "auto_approve",
  "confidence": 1.0
}
```

## Stopping The Server

Command:

```bash
lsof -ti :8001 | xargs -r kill
```

Result:

```text
stopped
```

## Current Port Error You Saw

Command:

```bash
uvicorn app.main:app --reload
```

Result:

```text
INFO:     Will watch for changes in these directories: ['/Users/aka725/Documents/Codex/2026-05-09/logistics-ops-reviewer-agent']
ERROR:    [Errno 48] Address already in use
```

Meaning: something is already listening on the default Uvicorn port `8000`.

Fix options:

```bash
uvicorn app.main:app --reload --port 8001
```

or:

```bash
lsof -ti :8000 | xargs kill
uvicorn app.main:app --reload
```

## How To Capture Fresh Logs Yourself

Run API and save logs:

```bash
uvicorn app.main:app --reload --port 8001 2>&1 | tee run.log
```

Run Docker logs:

```bash
docker compose up --build
docker compose logs -f api postgres
```

Enable more app logs:

```bash
LOG_LEVEL=DEBUG uvicorn app.main:app --reload --port 8001
```
