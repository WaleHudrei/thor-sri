-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║  thor-sri — Postgres schema (shared Thor database)                      ║
-- ║                                                                          ║
-- ║  Lives alongside thor-mycase's `cases`, `parties`, `addresses`, `jobs`. ║
-- ║  Never touches those tables. Fully idempotent — safe to re-run.         ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- ── sri_jobs ───────────────────────────────────────────────────────────────
-- Persistent job history. Survives service restarts.
CREATE TABLE IF NOT EXISTS sri_jobs (
    job_id            TEXT        PRIMARY KEY,
    status            TEXT        NOT NULL,              -- queued|running|done|error|cancelled
    params            JSONB       NOT NULL,
    result_count      INTEGER     NOT NULL DEFAULT 0,
    error_count       INTEGER     NOT NULL DEFAULT 0,
    progress_current  INTEGER     NOT NULL DEFAULT 0,
    progress_total    INTEGER     NOT NULL DEFAULT 0,
    error_message     TEXT,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sri_jobs_status     ON sri_jobs (status);
CREATE INDEX IF NOT EXISTS idx_sri_jobs_created_at ON sri_jobs (created_at DESC);

-- ── sri_listings ───────────────────────────────────────────────────────────
-- One row per unique (sale_type, state, county, case_number, parcel) tuple.
-- Re-running a scrape upserts — status/dates refresh, duplicates collapse.
CREATE TABLE IF NOT EXISTS sri_listings (
    id                SERIAL      PRIMARY KEY,
    -- Provenance
    sale_type         TEXT        NOT NULL,              -- tax_sale | commissioner_sale | sheriff_sale
    state             TEXT        NOT NULL,
    county            TEXT        NOT NULL,
    scraped_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_job_id     TEXT        REFERENCES sri_jobs(job_id) ON DELETE SET NULL,
    -- Identity
    case_number       TEXT,
    parcel            TEXT,
    item_number       TEXT,
    -- Property
    address           TEXT,
    city              TEXT,
    zip_code          TEXT,
    -- Sale
    sale_date         TEXT,
    minimum_bid       TEXT,
    judgment          TEXT,
    status            TEXT,
    -- Parties (sheriff sale)
    plaintiff         TEXT,
    defendant         TEXT,
    attorney          TEXT,
    -- Tax sale specific
    tax_years         TEXT,
    delinquent_amount TEXT,
    -- Fallbacks
    raw_text          TEXT,
    extras            JSONB,
    -- Dedupe key
    CONSTRAINT sri_listings_unique UNIQUE
        (sale_type, state, county, case_number, parcel)
);
CREATE INDEX IF NOT EXISTS idx_sri_sale_type   ON sri_listings (sale_type);
CREATE INDEX IF NOT EXISTS idx_sri_county      ON sri_listings (county);
CREATE INDEX IF NOT EXISTS idx_sri_sale_date   ON sri_listings (sale_date);
CREATE INDEX IF NOT EXISTS idx_sri_scraped_at  ON sri_listings (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_sri_source_job  ON sri_listings (source_job_id);

-- ── Crash recovery ─────────────────────────────────────────────────────────
-- Called once on service startup to reconcile jobs that were running when
-- the last process died.
CREATE OR REPLACE FUNCTION sri_recover_jobs() RETURNS INTEGER AS $$
DECLARE
    recovered INTEGER;
BEGIN
    UPDATE sri_jobs
       SET status = 'error',
           error_message = 'service_restarted',
           finished_at = NOW()
     WHERE status IN ('running', 'queued');
    GET DIAGNOSTICS recovered = ROW_COUNT;
    RETURN recovered;
END;
$$ LANGUAGE plpgsql;
