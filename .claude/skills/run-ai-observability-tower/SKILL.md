---
name: run-ai-observability-tower
description: Build, run, and drive the AI Observability Tower (Nexus KPI Pipeline) FastAPI server. Use when asked to start the API, verify a backend change works end-to-end, exercise /kpis, /chat, /jobs, or /documents endpoints, or smoke-test the service after a code change.
---

FastAPI service (no browser UI) — start it as a background process, then drive it with
`curl` via the committed smoke script at `.claude/skills/run-ai-observability-tower/smoke.sh`.
All paths below are relative to the repo root (`d:\ai observability tower`).

## Prerequisites

Dependencies are already declared in `pyproject.toml`/`uv.lock` and a `.venv` exists at repo
root. If it's missing or stale:

```bash
uv sync
```

Requires `.env` at repo root (see `.env.example`) with an LLM provider key
(`GEMINI_API_KEY` or Azure equivalent) and a reachable Postgres (`POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PWD`) — schema migrations to
Postgres already happened; the app is **not** on SQLite despite what `README.md` says.

**Corporate HTTP proxy gotcha**: this machine sets `HTTP_PROXY`/`HTTPS_PROXY` to a local
MITM proxy (`127.0.0.1:3636`), which intercepts loopback `curl` calls to the server and
returns a `502 [WinError 1225]`. Always set `NO_PROXY=localhost,127.0.0.1` before curling
the app (the smoke script does this for you).

## Run (agent path)

Launch in the background (uses uvicorn's `--reload`; startup does a hard-fail env-var
check, a Postgres ping, and applies the schema — takes ~5s):

```bash
./.venv/Scripts/python main.py > /tmp/server.log 2>&1 &
sleep 6
tail -n 20 /tmp/server.log   # look for "Uvicorn running on http://0.0.0.0:8005"
```

Then drive it with the smoke script — hits `/health`, `/health/db`, `/kpis/`, `/jobs/`, and
a live `/chat/ask` call that round-trips through Postgres + the LLM:

```bash
bash .claude/skills/run-ai-observability-tower/smoke.sh
```

Expected output:

```
PASS  [200] liveness
PASS  [200] db connectivity
PASS  [200] list all KPIs
PASS  [200] list jobs
PASS  [200] agentic chat ask (sql route)

Results: 5 passed, 0 failed
```

Override the base URL with `HOST=http://127.0.0.1:8005 bash .claude/skills/run-ai-observability-tower/smoke.sh`
if the server is on a different port.

### Direct invocation (for internal-logic changes)

Most PRs touch KPI calculation, fact extraction, or chatbot routing logic below the API
layer — no need to boot the whole server for those. Import and call directly, e.g.:

```bash
./.venv/Scripts/python -c "
from backend.db.db_client import get_session
from backend.kpi_extractor.app.registry.registry_service import RegistryService
r = RegistryService()
print(r.is_populated())
"
```

This still runs `backend/config.py` module-level code but skips FastAPI startup/lifespan.

### Stop

```bash
# Find and kill the uvicorn/reloader process:
powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort 8005).OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }"
```

`--reload` spawns a child watcher process — if the server keeps responding after killing
the PID you found, re-check `Get-NetTCPConnection -LocalPort 8005` for a new owning PID.
On this environment, `Get-NetTCPConnection`'s reported `OwningProcess` for a server started
from the Bash tool can point at a PID that `Get-Process`/`tasklist` can no longer see (a
sandbox/session boundary between the Bash and PowerShell tools), even though the server is
still demonstrably serving requests. If `Stop-Process` on that PID silently does nothing and
the port keeps responding, it's harmless to leave it running — re-launching on the same port
will simply fail with "address already in use" rather than starting a duplicate.

## Run (human path)

```bash
uvicorn main:app --reload --port 8005   # or: docker compose up --build
```

Same server, foreground. `Ctrl-C` to stop. Docs UI at `http://localhost:8005/docs`.

## Test

No automated test suite exists in this repo (no `tests/` dir, no pytest config) — the
smoke script above is the verification path.

## Gotchas

- **`README.md` describes SQLite; the app now runs on Postgres.** `backend/config.py`'s
  docstring still says `nexus_schema.sql applied to Postgres on startup` but the prose
  elsewhere (and `.env.example`) hasn't caught up. Trust `backend/db/db_client.py` /
  `.env`'s `POSTGRES_*` vars over the README.
- **Loopback proxy interception** — see Prerequisites. Forgetting `NO_PROXY` produces a
  502 with `[WinError 1225]`, which looks like the server isn't listening at all when it
  actually is.
- **`/chat/ask` request field is `message`, not `question`** — `AgenticAskRequest` in
  `backend/chatbot/orchestrator.py` requires `message` (min length 1); `company_id` and
  `session_id` are optional but recommended for routing/history.
- **Startup hard-fails** if `NEXUS_LLM_PROVIDER`'s required key is missing or Postgres is
  unreachable (`backend/api/deps.py::init_shared_resources`) — check `/tmp/server.log` for
  `RuntimeError: Missing required environment variables` or `Database unreachable` if the
  background launch produces no listener.

## Troubleshooting

- **`curl` returns a `502 Bad Gateway` / `[WinError 1225] The remote computer refused the
  network connection`**: the corporate `HTTP_PROXY` is intercepting the loopback request.
  Run `export NO_PROXY=localhost,127.0.0.1` first.
- **`422 Unprocessable Entity` on `POST /chat/ask` with `{"question": ...}`**: wrong field
  name — use `{"message": ...}`.