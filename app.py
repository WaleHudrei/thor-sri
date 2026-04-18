"""
thor-sri — Flask API implementing the Thor scraper contract.
See CONTRIBUTING.md for endpoint specifications.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

# Make src/ importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import db
from src.core.queue import JobQueue, Job
from src.core.scraper import SRIScraper, ScrapeParams, proxy_from_env

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("thor-sri.app")

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

SERVICE_START = time.time()
SCRAPER_NAME = "sri"
SCRAPER_VERSION = "1.0.0"

# ── Init ──────────────────────────────────────────────────────────────────────
db_ready = db.init()
proxy_config, proxy_type = proxy_from_env()
log.info("Proxy type: %s", proxy_type)


# ── Job worker: bridges the JobQueue to the async scraper ────────────────────
def scrape_worker(job: Job) -> None:
    """
    Called by JobQueue in a worker thread. Runs the async scraper under
    a dedicated event loop, wiring progress back to the job.
    """
    class Sink:
        def log(self, level, msg): queue._log(job, level, msg)
        def set_progress(self, cur, tot):
            job.progress_current, job.progress_total = cur, tot
            queue._persist(job)
        def add_results(self, n): job.result_count += n
        def add_errors(self, n): job.error_count += n
        def should_cancel(self): return job.cancel_flag.is_set()

    sink = Sink()
    try:
        params = ScrapeParams.from_dict(job.params)
    except Exception as e:
        sink.log("ERROR", f"Invalid params: {e}")
        raise

    scraper = SRIScraper(proxy=proxy_config)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        records = loop.run_until_complete(scraper.run(params, sink))
    finally:
        loop.close()

    # Persist results
    if records and db.is_available():
        try:
            n = db.upsert_listings(records, source_job_id=job.job_id)
            sink.log("INFO", f"Persisted {n} listings to Postgres")
        except Exception as e:
            sink.log("ERROR", f"DB upsert failed: {e}")
            job.error_count += 1
    elif records:
        sink.log("WARN", f"DB unavailable — {len(records)} records not persisted")


queue = JobQueue(worker=scrape_worker)


# ══════════════════════════════════════════════════════════════════════════════
# CONTRACT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    cooldown = queue.cooldown_status()
    qs = queue.queue_stats()
    return jsonify({
        "status": "ok",
        "scraper": SCRAPER_NAME,
        "version": SCRAPER_VERSION,
        "uptime_seconds": int(time.time() - SERVICE_START),
        "proxy": {"type": proxy_type, "active": proxy_config is not None},
        "db": {"connected": db.is_available()},
        "queue": qs,
        "cooldown": cooldown,
    })


@app.post("/api/scrape")
def start_scrape():
    body = request.get_json(silent=True) or {}
    params = body.get("params") or body  # accept either shape

    # Validate minimally
    try:
        ScrapeParams.from_dict(params)
    except Exception as e:
        return jsonify({"error": "invalid_params", "detail": str(e)}), 400

    job_id, err = queue.submit(params)
    if err:
        return jsonify(err), 429

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "queue_position": queue.queue_stats()["length"],
    }), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = queue.get(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify(_serialize_job(job))


@app.get("/api/jobs/<job_id>/logs")
def get_logs(job_id):
    job = queue.get(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404

    since = int(request.args.get("since", "0"))
    logs = [l for l in getattr(job, "logs", []) if l["index"] >= since]
    complete = job.status in ("done", "error", "cancelled")
    return jsonify({"job_id": job_id, "logs": logs, "complete": complete})


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    if not queue.cancel(job_id):
        return jsonify({"error": "job_not_found_or_already_finished"}), 404
    return jsonify({"job_id": job_id, "status": "cancelled"})


@app.get("/api/jobs/history")
def job_history():
    limit = min(int(request.args.get("limit", "50")), 200)
    jobs = queue.list_recent(limit=limit)
    # Normalize timestamps (DB returns datetime objects)
    for j in jobs:
        for k in ("started_at", "finished_at", "created_at"):
            v = j.get(k)
            if hasattr(v, "isoformat"):
                j[k] = v.isoformat()
    return jsonify({"jobs": jobs})


@app.get("/api/leads")
def leads():
    if not db.is_available():
        return jsonify({"total": 0, "leads": [], "columns": [],
                        "error": "db_unavailable"}), 503

    total, rows = db.query_listings(
        sale_type=request.args.get("sale_type"),
        county=request.args.get("county"),
        state=request.args.get("state"),
        since=request.args.get("since"),
        limit=min(int(request.args.get("limit", "1000")), 5000),
        offset=int(request.args.get("offset", "0")),
    )
    # Normalize datetimes for JSON
    for r in rows:
        if hasattr(r.get("scraped_at"), "isoformat"):
            r["scraped_at"] = r["scraped_at"].isoformat()

    columns = [
        "sale_type", "county", "state", "address", "city", "zip_code",
        "case_number", "parcel", "sale_date", "minimum_bid", "judgment",
        "status", "defendant", "plaintiff", "scraped_at",
    ]
    return jsonify({"total": total, "leads": rows, "columns": columns})


# ══════════════════════════════════════════════════════════════════════════════
# CSV export (nice-to-have, not required by contract)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/leads.csv")
def leads_csv():
    if not db.is_available():
        return jsonify({"error": "db_unavailable"}), 503

    total, rows = db.query_listings(
        sale_type=request.args.get("sale_type"),
        county=request.args.get("county"),
        state=request.args.get("state"),
        since=request.args.get("since"),
        limit=min(int(request.args.get("limit", "10000")), 50000),
    )

    def gen():
        import csv, io
        buf = io.StringIO()
        cols = [
            "sale_type", "county", "state", "address", "city", "zip_code",
            "case_number", "parcel", "sale_date", "minimum_bid", "judgment",
            "status", "defendant", "plaintiff", "attorney", "scraped_at",
        ]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for r in rows:
            if hasattr(r.get("scraped_at"), "isoformat"):
                r["scraped_at"] = r["scraped_at"].isoformat()
            w.writerow(r)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return Response(
        gen(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=sri_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _serialize_job(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": {
            "current": job.progress_current,
            "total": job.progress_total,
            "unit": "counties",
        },
        "result_count": job.result_count,
        "error_count": job.error_count,
        "params": job.params,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error_message": job.error_message,
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
