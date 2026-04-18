"""
Postgres access layer for thor-sri.

Uses a single connection pool that the Flask app and job workers share.
All queries are parametrized. All writes are idempotent via ON CONFLICT.
"""

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

log = logging.getLogger("thor-sri.db")

_pool: Optional[ThreadedConnectionPool] = None


# ── Lifecycle ────────────────────────────────────────────────────────────────
def init(database_url: Optional[str] = None,
         minconn: int = 1, maxconn: int = 10) -> bool:
    """
    Initialize the connection pool and apply schema. Returns True if the DB is
    usable. If DATABASE_URL is missing or connection fails, returns False —
    the app will run with no persistence and log warnings.
    """
    global _pool
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        log.warning("DATABASE_URL not set — persistence disabled")
        return False

    try:
        _pool = ThreadedConnectionPool(minconn, maxconn, url)
        _apply_schema()
        recovered = recover_orphan_jobs()
        if recovered:
            log.info("Recovered %d orphan jobs from prior restart", recovered)
        log.info("Postgres pool initialized")
        return True
    except Exception as e:
        log.error("DB init failed: %s", e)
        _pool = None
        return False


def close() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


def is_available() -> bool:
    return _pool is not None


@contextmanager
def cursor(commit: bool = True):
    """Context manager yielding a cursor; commits or rolls back automatically."""
    if not _pool:
        raise RuntimeError("DB not initialized")
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _apply_schema() -> None:
    schema_path = Path(__file__).parent / "schema.sql"
    with cursor() as cur:
        cur.execute(schema_path.read_text())


def recover_orphan_jobs() -> int:
    with cursor() as cur:
        cur.execute("SELECT sri_recover_jobs() AS n")
        return cur.fetchone()["n"]


# ── Jobs ─────────────────────────────────────────────────────────────────────
def create_job(job_id: str, params: dict) -> None:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO sri_jobs (job_id, status, params)
            VALUES (%s, 'queued', %s)
            """,
            (job_id, psycopg2.extras.Json(params)),
        )


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(psycopg2.extras.Json(v) if isinstance(v, (dict, list)) else v)
    vals.append(job_id)
    with cursor() as cur:
        cur.execute(
            f"UPDATE sri_jobs SET {', '.join(sets)} WHERE job_id = %s",
            tuple(vals),
        )


def get_job(job_id: str) -> Optional[dict]:
    with cursor(commit=False) as cur:
        cur.execute("SELECT * FROM sri_jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_jobs(limit: int = 50) -> list[dict]:
    with cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM sri_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


# ── Listings ─────────────────────────────────────────────────────────────────
LISTING_COLS = [
    "sale_type", "state", "county", "scraped_at", "source_job_id",
    "case_number", "parcel", "item_number",
    "address", "city", "zip_code",
    "sale_date", "minimum_bid", "judgment", "status",
    "plaintiff", "defendant", "attorney",
    "tax_years", "delinquent_amount",
    "raw_text", "extras",
]


def upsert_listings(records: Iterable[dict], source_job_id: str) -> int:
    rows = []
    for r in records:
        row = []
        for c in LISTING_COLS:
            if c == "source_job_id":
                row.append(source_job_id)
            elif c == "extras":
                v = r.get("extras")
                row.append(psycopg2.extras.Json(v) if v else None)
            else:
                row.append(r.get(c))
        rows.append(tuple(row))

    if not rows:
        return 0

    cols = ", ".join(LISTING_COLS)
    sql = f"""
        INSERT INTO sri_listings ({cols})
        VALUES %s
        ON CONFLICT (sale_type, state, county, case_number, parcel)
        DO UPDATE SET
            sale_date         = EXCLUDED.sale_date,
            minimum_bid       = EXCLUDED.minimum_bid,
            judgment          = EXCLUDED.judgment,
            status            = EXCLUDED.status,
            scraped_at        = EXCLUDED.scraped_at,
            source_job_id     = EXCLUDED.source_job_id,
            delinquent_amount = EXCLUDED.delinquent_amount,
            raw_text          = EXCLUDED.raw_text,
            extras            = EXCLUDED.extras
    """
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
        return len(rows)


def query_listings(
    sale_type: Optional[str] = None,
    county: Optional[str] = None,
    state: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    clauses, params = [], []
    if sale_type:
        clauses.append("sale_type = %s")
        params.append(sale_type)
    if county:
        clauses.append("LOWER(county) = LOWER(%s)")
        params.append(county)
    if state:
        clauses.append("state = %s")
        params.append(state)
    if since:
        clauses.append("scraped_at >= %s")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with cursor(commit=False) as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM sri_listings {where}", tuple(params))
        total = cur.fetchone()["n"]

        cur.execute(
            f"""
            SELECT * FROM sri_listings {where}
            ORDER BY scraped_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit, offset]),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return total, rows
