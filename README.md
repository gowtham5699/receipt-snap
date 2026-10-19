# Receipt Snap

Photograph a receipt. Gemini reads every line and sorts it into one of 69 spending categories. Each line becomes a row in a Tiger Data hypertable, and the dashboard updates instantly from continuous aggregates. Then ask questions in plain English ("which weekday do I spend the most on dining out?") and Gemini writes the SQL.

## How it works

```
 phone camera ──► FastAPI ──► Gemini (image ► structured JSON, 16 categories / 69 subcategories)
                     │
                     ▼  you review and fix any line, then save
             Tiger Data (TimescaleDB)
               receipts             relational table
               line_items           hypertable, 28-day chunks
                 └─ columnstore     after 30 days, merged into 12-week chunks, segmented by category, ordered by user
               spend_daily          continuous aggregate (real-time)
                 └─ spend_monthly   hierarchical continuous aggregate
               merchant_monthly     continuous aggregate
                     ▲
 "Ask" ── Gemini writes SQL ── runs as a read-only role that can only see my_* views
```

**Gemini**
- Reads photos and PDFs into a strict schema. Categories are an enum in the schema, so Gemini can't invent one.
- Expands abbreviations (`ORG BNNA` becomes "Organic bananas"), handles discounts and bag fees, and flags anything it couldn't read.
- Turns questions into SQL, retries once with the database error if the query fails, and then explains the result in a sentence.

**Tiger Data**
- One database for both kinds of data: relational receipts and time-series line items.
- The dashboard runs five queries, all against continuous aggregates. They are real-time, so a receipt you just saved shows up immediately.
- Columnstore compression on old chunks.
- The **Engine** tab shows row counts, compression savings, and a live benchmark of the same query on raw rows versus the continuous aggregate.

**Safety and correctness**
- The review screen checks that items + tax + tip add up to the total.
- Duplicates are caught by image hash and by matching merchant, date and total.
- Gemini-written SQL runs inside a `READ ONLY` transaction, as a role with `SELECT` on per-user views only, with a 5-second timeout, wrapped as a subquery so stacked statements fail.
- Optional HTTP basic auth (`APP_PASSWORD`) for a public deploy.

**Sentry:** errors, plus a span on every Gemini call with model and token counts.

## Run it locally

You need Python 3.11+ and Postgres with TimescaleDB 2.18 or later. The simplest option is a free Tiger Cloud service.

```bash
cp .env.example .env            # add DATABASE_URL and GEMINI_API_KEY
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The schema is applied automatically on startup. To try it from your phone, open `http://<your-laptop-ip>:8000` on the same Wi-Fi.

### Load demo data (for the dashboard and benchmark)

```bash
python -m scripts.seed --users 2000 --months 24     # about 5M line items
```

Demo users get ids 1000 and up, and your own receipts (user 1) are never touched. Switch to them with the **Showing** menu in the header. On a small free-tier instance, start with `--users 300`.

### Tests

```bash
pip install pytest
DATABASE_URL=postgresql://.../scratch_db python -m pytest -q tests
```

The tests stub Gemini and run against a real TimescaleDB. They cover the full flow (extract, save, dashboard, ask, delete), duplicate detection, and five attempts to break out of the SQL sandbox.

## Deploy on DigitalOcean App Platform

1. Push this repo to GitHub and set `repo:` in `.do/app.yaml`.
2. Run `doctl apps create --spec .do/app.yaml`, or create the app in the dashboard from the Dockerfile.
3. Set `DATABASE_URL` (from Tiger Cloud), `GEMINI_API_KEY`, `APP_PASSWORD` and `SENTRY_DSN` as secrets.

## License

MIT
