# Thor Scraper API Contract

This document defines the standard interface every Thor scraper service must implement. Following this contract means `thor-ui` can add your scraper to the picker with a single config entry — no custom integration code.

**Every new scraper (thor-recorder, thor-obituaries, thor-permits, etc.) must implement this contract.**

---

## 1. Architecture principles

Every Thor scraper is:

1. **A standalone Railway service** with its own process, own deploy lifecycle, own logs.
2. **A Flask HTTP API** — no HTML. UI concerns belong to `thor-ui`.
3. **A writer to the shared Postgres DB** — its own tables, never touches other scrapers' tables.
4. **Isolated by default** — crashing, restarting, redeploying must not affect other scrapers.

This means no shared imports across scrapers. If two scrapers need the same helper, copy it. Isolation > DRY.

---

## 2. Required endpoints

Every scraper service MUST implement these 7 endpoints with identical request/response shapes. Scraper-specific parameters go in the `params` field of the request body.

### `GET /api/health`

Health check. Called by `thor-ui` to verify the service is up, and by Railway to decide restarts.

**Response — 200:**
```json
{
  "status": "ok",
  "scraper": "sri",
  "version": "1.0.0",
  "uptime_seconds": 12345,
  "proxy": { "type": "zyte", "active": true },
  "db": { "connected": true },
  "queue": { "length": 0, "running": 0 },
  "cooldown": { "active": false, "reason": null, "seconds_remaining": 0 }
}
```

### `POST /api/scrape`

Start a scrape job. Returns immediately with a `job_id`; the actual scrape runs in the background.

**Request body:**
```json
{
  "params": { /* scraper-specific — see your scraper's schema */ }
}
```

**Response — 202 Accepted:**
```json
{ "job_id": "abc123", "status": "queued", "queue_position": 0 }
```

**Response — 429 (rejected):**
```json
{ "error": "cooldown_active", "reason": "soft_block", "retry_after_seconds": 1800 }
```

### `GET /api/jobs/<job_id>`

Get the status of one job.

**Response — 200:**
```json
{
  "job_id": "abc123",
  "status": "running",                 // queued | running | done | error | cancelled
  "progress": { "current": 12, "total": 50, "unit": "counties" },
  "result_count": 847,
  "error_count": 2,
  "params": { /* original request params */ },
  "started_at": "2026-04-18T20:15:00Z",
  "finished_at": null,
  "duration_seconds": 142
}
```

### `GET /api/jobs/<job_id>/logs`

Stream logs for one job. Supports `?since=<log_index>` for incremental polling.

**Response — 200:**
```json
{
  "job_id": "abc123",
  "logs": [
    { "index": 0, "time": "2026-04-18T20:15:00Z", "level": "INFO",
      "msg": "Starting SRI tax sale scrape for 10 counties" }
  ],
  "complete": false
}
```

### `POST /api/jobs/<job_id>/cancel`

Cancel a running or queued job.

**Response — 200:** `{ "job_id": "abc123", "status": "cancelled" }`

### `GET /api/jobs/history`

List recent jobs for this scraper, newest first. Optional `?limit=50`.

**Response — 200:**
```json
{ "jobs": [ /* array of the same shape as GET /api/jobs/<id> */ ] }
```

### `GET /api/leads`

Query the records written by this scraper. Scraper-specific filters pass through as query string params.

**Response — 200:**
```json
{
  "total": 1234,
  "leads": [ /* flattened records ready for UI display */ ],
  "columns": [ "sale_type", "county", "address", "..." ]
}
```

---

## 3. Database rules

- Shared Postgres instance; each scraper writes to **its own tables**.
- Table prefix = scraper name: `sri_listings`, `sri_jobs`, `recorder_liens`, etc.
- NEVER write to another scraper's tables. If you need cross-scraper data, read-only.
- Schema migrations live in `src/db/schema.sql` per service and are idempotent (`CREATE TABLE IF NOT EXISTS`).
- The `<scraper>_jobs` table is the persistent source of truth for job history. The in-memory `jobs` dict is just a cache.

---

## 4. Job lifecycle (state machine)

```
queued ──► running ──► done
              │ 
              ├─► error         (exception, marked failed)
              └─► cancelled     (user requested stop)
```

- On service restart, jobs stuck in `running` are marked `error` with reason `service_restarted`.
- Cancelled jobs must stop their scrape within 10 seconds of the cancel request.

---

## 5. Queue rules

- Each scraper service runs its own queue. NEVER coordinate across services.
- Default: one job at a time per scraper. Override via `MAX_CONCURRENT_JOBS` env var.
- When a job is rejected due to queue limits or cooldown, return `429` with `retry_after_seconds`.

---

## 6. Environment variables

Standard names every service respects:

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Shared Postgres connection string |
| `PROXY_TYPE` | no | `zyte` / `webshare` / `direct` |
| `ZYTE_API_KEY` | conditional | Zyte Smart Proxy key if `PROXY_TYPE=zyte` |
| `PROXY_URL` | conditional | Full proxy URL if `PROXY_TYPE=webshare` |
| `MAX_CONCURRENT_JOBS` | no | Default 1 |
| `LOG_LEVEL` | no | Default `INFO` |
| `PORT` | auto | Railway injects this |

---

## 7. Registering a new scraper in thor-ui

Add one entry to `thor-ui/config.py`:

```python
SCRAPERS["sri"] = {
    "name": "SRI Services",
    "icon": "🏛️",
    "url": os.getenv("THOR_SRI_URL"),
    "description": "Tax, commissioner, and sheriff sale auctions",
    "param_schema": "sri_params_form.html",
}
```

That's the only integration work.

---

## 8. Reference implementation

`thor-sri` is the reference. When building a new scraper, copy its structure:

```
thor-<name>/
├── app.py                     Flask routes implementing this contract
├── src/core/scraper.py        The actual scraping logic
├── src/core/queue.py          Job queue + lifecycle
├── src/db/                    Postgres schema + queries
├── src/models/                Record dataclasses
├── Dockerfile                 Railway build
├── railway.toml               Railway config
├── requirements.txt
└── CONTRIBUTING.md            (this file, updated if contract evolves)
```

When you change the contract, bump the major version in `/api/health` and update `thor-ui`.
