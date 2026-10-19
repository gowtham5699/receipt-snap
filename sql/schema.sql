-- Receipt Snap schema for Tiger Data (TimescaleDB >= 2.18).
-- Safe to re-run: every statement is idempotent.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Relational side: people, categories, receipts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id          INT PRIMARY KEY,
    name        TEXT NOT NULL,
    is_demo     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (id, name, is_demo) VALUES (1, 'Me', FALSE)
ON CONFLICT (id) DO NOTHING;

-- Two-level taxonomy. Gemini must choose from this list (enforced by the
-- structured-output schema), so the charts never fill up with near-duplicates.
CREATE TABLE IF NOT EXISTS categories (
    category     TEXT NOT NULL,
    subcategory  TEXT NOT NULL,
    color        TEXT NOT NULL,
    PRIMARY KEY (category, subcategory)
);

CREATE TABLE IF NOT EXISTS receipts (
    id              BIGSERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(id),
    merchant        TEXT NOT NULL,
    purchased_at    TIMESTAMPTZ NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    subtotal        NUMERIC(12,2),
    tax             NUMERIC(12,2),
    tip             NUMERIC(12,2),
    total           NUMERIC(12,2) NOT NULL,
    payment_method  TEXT,
    image_sha256    TEXT,
    raw_extraction  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS receipts_user_time ON receipts (user_id, purchased_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS receipts_user_image
    ON receipts (user_id, image_sha256) WHERE image_sha256 IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Time-series side: every line item is an event in a hypertable
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS line_items (
    time         TIMESTAMPTZ NOT NULL,
    user_id      INT NOT NULL,
    receipt_id   BIGINT NOT NULL,
    merchant     TEXT NOT NULL,
    category     TEXT NOT NULL,
    subcategory  TEXT NOT NULL,
    description  TEXT NOT NULL,
    quantity     NUMERIC(10,3) NOT NULL DEFAULT 1,
    amount       NUMERIC(12,2) NOT NULL
);

SELECT create_hypertable('line_items', by_range('time', INTERVAL '28 days'),
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS line_items_user_time ON line_items (user_id, time DESC);
CREATE INDEX IF NOT EXISTS line_items_receipt   ON line_items (receipt_id);

-- Columnstore (compression). Segmenting by category and ordering by user keeps
-- similar rows together (one person's coffee runs compress very well), and the
-- per-batch min/max on user_id lets "my spending" queries skip other people's batches.
-- Compressed chunks are merged into 12-week chunks so each batch is large.
ALTER TABLE line_items SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'category',
    timescaledb.orderby   = 'user_id, time DESC',
    timescaledb.compress_chunk_time_interval = '84 days'
);

DO $$
BEGIN
    CALL add_columnstore_policy('line_items', after => INTERVAL '30 days', if_not_exists => TRUE);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- Continuous aggregates: the dashboard reads only from these.
-- Buckets follow local days and months ({{TIMEZONE}} is filled in from config).
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS spend_daily
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 day', time, '{{TIMEZONE}}') AS day,
       user_id,
       category,
       subcategory,
       sum(amount)              AS total,
       count(*)                 AS items,
       count(DISTINCT receipt_id) AS receipts
FROM line_items
GROUP BY day, user_id, category, subcategory
WITH NO DATA;

-- Hierarchical aggregate built on top of the daily one.
CREATE MATERIALIZED VIEW IF NOT EXISTS spend_monthly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 month', day, '{{TIMEZONE}}') AS month,
       user_id,
       category,
       sum(total)    AS total,
       sum(items)    AS items,
       sum(receipts) AS receipts
FROM spend_daily
GROUP BY month, user_id, category
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS merchant_monthly
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 month', time, '{{TIMEZONE}}') AS month,
       user_id,
       merchant,
       sum(amount)                AS total,
       count(DISTINCT receipt_id) AS visits
FROM line_items
GROUP BY month, user_id, merchant
WITH NO DATA;

DO $$
DECLARE
    p RECORD;
BEGIN
    FOR p IN SELECT * FROM (VALUES
        ('spend_daily',      INTERVAL '3 months', INTERVAL '1 hour', INTERVAL '15 minutes'),
        ('spend_monthly',    INTERVAL '6 months', INTERVAL '1 day',  INTERVAL '1 hour'),
        ('merchant_monthly', INTERVAL '6 months', INTERVAL '1 hour', INTERVAL '1 hour')
    ) AS t(view_name, start_off, end_off, every)
    LOOP
        BEGIN
            PERFORM add_continuous_aggregate_policy(p.view_name::regclass,
                start_offset => p.start_off, end_offset => p.end_off,
                schedule_interval => p.every, if_not_exists => TRUE);
        EXCEPTION
            -- Re-running the schema: the policy is already there.
            WHEN invalid_parameter_value OR feature_not_supported THEN NULL;
        END;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- "Ask your receipts": Gemini-written SQL runs as a read-only role that can
-- only see the current user's rows through these views.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW my_line_items AS
SELECT time, receipt_id, merchant, category, subcategory, description, quantity, amount
FROM line_items
WHERE user_id = current_setting('app.user_id')::int;

CREATE OR REPLACE VIEW my_receipts AS
SELECT id AS receipt_id, merchant, purchased_at, currency, subtotal, tax, tip, total, payment_method
FROM receipts
WHERE user_id = current_setting('app.user_id')::int;

CREATE OR REPLACE VIEW my_spend_daily AS
SELECT day, category, subcategory, total, items, receipts
FROM spend_daily
WHERE user_id = current_setting('app.user_id')::int;

CREATE OR REPLACE VIEW my_spend_monthly AS
SELECT month, category, total, items, receipts
FROM spend_monthly
WHERE user_id = current_setting('app.user_id')::int;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receipt_reader') THEN
        CREATE ROLE receipt_reader NOLOGIN;
    END IF;
    EXECUTE format('GRANT receipt_reader TO %I', current_user);
    GRANT USAGE ON SCHEMA public TO receipt_reader;
    GRANT SELECT ON my_line_items, my_receipts, my_spend_daily, my_spend_monthly, categories
        TO receipt_reader;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'Could not create receipt_reader role; NL queries will rely on read-only transactions only.';
END $$;
