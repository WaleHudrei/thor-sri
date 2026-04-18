# thor-sri

Standalone Railway service for scraping SRI Services (Indiana tax, commissioner's, and sheriff sale auctions). Implements the Thor scraper contract documented in `CONTRIBUTING.md`.

**Part of the Thor ecosystem:**
- `thor-mycase` — court filings (existing)
- **`thor-sri`** — SRI auctions (this repo)
- `thor-ui` — unified frontend (Phase 2)
- Shared Railway Postgres database

**Isolation guarantees:** this service has its own process, own queue, own deploy lifecycle. Crashing, restarting, or redeploying this service does not affect any other Thor scraper.

---

## Files

```
thor-sri/
├── app.py                   Flask API (7 contract endpoints + CSV bonus)
├── src/
│   ├── core/
│   │   ├── scraper.py       Playwright scraper, all 3 sale types
│   │   └── queue.py         Job queue + lifecycle manager
│   └── db/
│       ├── __init__.py      Postgres connection pool + queries
│       └── schema.sql       Schema for sri_jobs + sri_listings
├── Dockerfile
├── railway.toml
├── requirements.txt
├── .env.example
├── CONTRIBUTING.md          Thor scraper contract (the blueprint)
└── THOR_UI_PATCH.md         How to add the scraper picker to thor-mycase
```

---

## Deployment to Railway

### 1. Attach shared Postgres

If thor-mycase already has a Railway Postgres plugin, thor-sri uses the same database. Grab the `DATABASE_URL` from the Postgres plugin's Connect tab.

### 2. Create a new Railway service for thor-sri

```bash
cd thor-sri
git init && git add . && git commit -m "thor-sri v1.0"
git remote add origin git@github.com:WaleHudrei/thor-sri.git
git push -u origin main
```

In Railway dashboard:
- New Project → Deploy from GitHub → pick `thor-sri` repo
- Railway auto-detects the `Dockerfile`

### 3. Set environment variables

Service → Variables tab:

```
DATABASE_URL=<paste from shared Postgres plugin>
PROXY_TYPE=zyte
ZYTE_API_KEY=<your Zyte key>
MAX_CONCURRENT_JOBS=1
LOG_LEVEL=INFO
```

For Webshare instead of Zyte:
```
PROXY_TYPE=webshare
PROXY_URL=http://user:pass@proxy.webshare.io:PORT
```

### 4. Deploy

Railway deploys automatically. Verify:

```bash
curl https://thor-sri-production.up.railway.app/api/health
```

You should see:
```json
{
  "status": "ok",
  "scraper": "sri",
  "version": "1.0.0",
  "db": { "connected": true },
  "proxy": { "type": "zyte", "active": true },
  ...
}
```

### 5. Patch thor-mycase UI to surface the scraper picker

See `THOR_UI_PATCH.md`.

---

## API usage

### Start a scrape

```bash
curl -X POST https://thor-sri-production.up.railway.app/api/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "params": {
      "sale_types": ["tax_sale", "sheriff_sale"],
      "state": "IN",
      "counties": "priority"
    }
  }'
```

Response:
```json
{ "job_id": "a1b2c3d4e5f6", "status": "queued", "queue_position": 0 }
```

### Poll job status

```bash
curl https://thor-sri-production.up.railway.app/api/jobs/a1b2c3d4e5f6
```

### Stream logs

```bash
curl "https://thor-sri-production.up.railway.app/api/jobs/a1b2c3d4e5f6/logs?since=0"
```

### Query leads

```bash
# All tax sale leads in Marion
curl "https://thor-sri-production.up.railway.app/api/leads?sale_type=tax_sale&county=marion"

# CSV export
curl "https://thor-sri-production.up.railway.app/api/leads.csv?sale_type=sheriff_sale" \
  -o sri_sheriff.csv
```

---

## Request parameter schema

### `/api/scrape` params

| Field | Type | Default | Description |
|---|---|---|---|
| `sale_types` | string \| list | all 3 | `"tax_sale"`, `"commissioner_sale"`, `"sheriff_sale"`, or a list |
| `state` | string | `"IN"` | Two-letter state code |
| `counties` | string \| list | all | `"all"`, `"priority"` (marion/allen/lake/vanderburgh), comma-string, or list of slugs |
| `headless` | bool | `true` | Set `false` only for local debugging |
| `timeout_seconds` | int | `45` | Page load timeout |

---

## Local development

```bash
# 1. Clone and install
git clone <repo>
cd thor-sri
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. Copy env
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL (can point at Railway shared DB for dev)

# 3. Run
export $(grep -v '^#' .env | xargs)
python app.py

# 4. Test
curl http://localhost:5000/api/health
curl -X POST http://localhost:5000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"params":{"sale_types":["sheriff_sale"],"counties":["marion"]}}'
```

---

## Troubleshooting

| Symptom | Diagnosis | Fix |
|---|---|---|
| `/api/health` shows `db.connected: false` | Bad `DATABASE_URL` | Verify Postgres plugin is attached; copy URL fresh |
| `/api/health` shows `proxy.active: false` with `PROXY_TYPE=zyte` | Missing `ZYTE_API_KEY` | Set the env var |
| All jobs return 0 results | SRI layout changed OR proxy blocked | Run locally with `"headless": false` to inspect |
| `429 cooldown_active` | Reactive cooldown triggered | Wait `retry_after_seconds`; Thor will auto-recover |
| Jobs stuck in `running` after restart | Pre-restart jobs | DB migration auto-marks them `error: service_restarted` |

---

## Contract version

This service speaks Thor scraper contract **v1.0**. See `CONTRIBUTING.md` for spec.
