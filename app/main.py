from __future__ import annotations

import base64
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import sentry_sdk
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import categories, db, gemini
from .config import APP_PASSWORD, MAX_UPLOAD_MB, SENTRY_DSN, TIMEZONE, USER_ID

if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=1.0, send_default_pii=False)

STATIC = Path(__file__).resolve().parent / "static"
TZ = ZoneInfo(TIMEZONE)
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.pool.open()
    db.init_schema()
    yield
    db.pool.close()


app = FastAPI(title="Receipt Snap", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Receipt Snap"'})
    return await call_next(request)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def rows_json(rows):
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


def viewer(user: Optional[int]) -> int:
    """Your own data by default; seeded demo users (id >= 1000) can be viewed read-only."""
    if user is None or user == USER_ID:
        return USER_ID
    if user >= 1000:
        return user
    raise HTTPException(403, "Only your own data or demo users can be viewed")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/categories")
def get_categories():
    return {c: {"color": v["color"], "subs": v["subs"]} for c, v in categories.TAXONOMY.items()}


# ---------------------------------------------------------------------------
# Snap -> extract -> review -> save
# ---------------------------------------------------------------------------

@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB")
    mime = (file.content_type or "").lower()
    if mime not in ALLOWED_TYPES:
        raise HTTPException(415, "Upload a photo (JPEG/PNG/WebP/HEIC) or a PDF")
    sha = hashlib.sha256(data).hexdigest()

    with db.conn() as c:
        dup = c.execute("SELECT id, merchant, total, purchased_at FROM receipts "
                        "WHERE user_id = %s AND image_sha256 = %s", (USER_ID, sha)).fetchone()
    if dup:
        return {"duplicate": rows_json([dup])[0], "image_sha256": sha}

    try:
        result = await run_in_threadpool(gemini.extract_receipt, data, mime)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:  # surfaced to the user; full trace goes to Sentry
        sentry_sdk.capture_exception(e)
        raise HTTPException(502, f"Gemini couldn't read that receipt: {e}")

    extraction = result.model_dump()
    checks = _checks(extraction)
    possible_dup = None
    if extraction.get("purchase_date"):
        with db.conn() as c:
            possible_dup = c.execute(
                "SELECT id, merchant, total, purchased_at FROM receipts WHERE user_id = %s "
                "AND purchased_at::date = %s::date AND abs(total - %s) < 0.01 LIMIT 1",
                (USER_ID, extraction["purchase_date"], extraction["total"])).fetchone()
    return {"extraction": extraction, "checks": checks, "image_sha256": sha,
            "possible_duplicate": rows_json([possible_dup])[0] if possible_dup else None}


def _checks(x: dict) -> list[str]:
    out = []
    items_sum = round(sum(i["amount"] for i in x["items"]), 2)
    calc = round(items_sum + (x.get("tax") or 0) + (x.get("tip") or 0), 2)
    if abs(calc - x["total"]) > 0.02:
        out.append(f"Items + tax + tip = ${calc:.2f}, but the receipt total is ${x['total']:.2f}. "
                   f"Something may be misread.")
    if not x.get("purchase_date"):
        out.append("No purchase date found; today's date will be used.")
    if not x.get("is_receipt", True):
        out.append("This doesn't look like a receipt.")
    return out


class ItemIn(BaseModel):
    description: str
    quantity: float = 1
    amount: float
    category: str
    subcategory: str


class ReceiptIn(BaseModel):
    merchant: str = Field(min_length=1)
    purchase_date: Optional[str] = None
    purchase_time: Optional[str] = None
    currency: str = "USD"
    items: list[ItemIn]
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    tip: Optional[float] = None
    total: float
    payment_method: Optional[str] = None
    image_sha256: Optional[str] = None
    raw: Optional[dict] = None


def _purchased_at(d: Optional[str], t: Optional[str]) -> datetime:
    try:
        day = date.fromisoformat(d) if d else datetime.now(TZ).date()
    except ValueError:
        day = datetime.now(TZ).date()
    hh, mm = 12, 0
    if t:
        try:
            hh, mm = (int(p) for p in t.split(":")[:2])
        except ValueError:
            pass
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


@app.post("/api/receipts")
def save_receipt(r: ReceiptIn):
    for it in r.items:
        if not categories.is_valid(it.category, it.subcategory):
            raise HTTPException(422, f"Unknown category: {it.category} / {it.subcategory}")
    ts = _purchased_at(r.purchase_date, r.purchase_time)
    from psycopg.types.json import Jsonb

    with db.conn() as c:
        try:
            rid = c.execute(
                "INSERT INTO receipts (user_id, merchant, purchased_at, currency, subtotal, tax, tip, total,"
                " payment_method, image_sha256, raw_extraction) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING id",
                (USER_ID, r.merchant.strip(), ts, r.currency, r.subtotal, r.tax, r.tip, r.total,
                 r.payment_method, r.image_sha256, Jsonb(r.raw) if r.raw else None)).fetchone()["id"]
        except Exception as e:
            if "receipts_user_image" in str(e):
                raise HTTPException(409, "You've already saved this exact photo")
            raise
        rows = [(ts, USER_ID, rid, r.merchant.strip(), it.category, it.subcategory, it.description.strip(),
                 it.quantity, it.amount) for it in r.items]
        if r.tax:
            rows.append((ts, USER_ID, rid, r.merchant, "Fees & Taxes", "Sales Tax", "Sales tax", 1, r.tax))
        if r.tip:
            rows.append((ts, USER_ID, rid, r.merchant, "Fees & Taxes", "Tip", "Tip", 1, r.tip))
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO line_items (time, user_id, receipt_id, merchant, category, subcategory,"
                " description, quantity, amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        c.commit()
    refresh_aggregates(ts)
    return {"id": rid, "items": len(rows)}


def refresh_aggregates(ts: datetime) -> None:
    """Receipts are usually backdated (bought yesterday, snapped today), which lands
    them in a window the continuous aggregates have already materialized. Refresh just
    the affected day and month so the dashboard is correct immediately."""
    day0 = ts.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    m0 = day0.replace(day=1)
    m1 = (m0 + timedelta(days=32)).replace(day=1)
    day1 = (day0 + timedelta(days=1)).replace(hour=0)
    with db.conn() as c:
        c.autocommit = True  # refresh can't run inside a transaction
        try:
            c.execute("CALL refresh_continuous_aggregate('spend_daily', %s, %s)", (day0, day1))
            c.execute("CALL refresh_continuous_aggregate('spend_monthly', %s, %s)", (m0, m1))
            c.execute("CALL refresh_continuous_aggregate('merchant_monthly', %s, %s)", (m0, m1))
        finally:
            c.autocommit = False


@app.get("/api/receipts")
def list_receipts(limit: int = 30, user: Optional[int] = None):
    uid = viewer(user)
    with db.conn() as c:
        rows = c.execute(
            "SELECT r.id, r.merchant, r.purchased_at, r.total, r.currency,"
            " (SELECT count(*) FROM line_items li WHERE li.receipt_id = r.id"
            "    AND li.user_id = r.user_id AND li.time = r.purchased_at) AS items"
            " FROM receipts r WHERE r.user_id = %s ORDER BY r.purchased_at DESC LIMIT %s",
            (uid, min(limit, 200))).fetchall()
    return rows_json(rows)


@app.get("/api/receipts/{rid}")
def get_receipt(rid: int, user: Optional[int] = None):
    uid = viewer(user)
    with db.conn() as c:
        r = c.execute("SELECT id, merchant, purchased_at, total, tax, tip, subtotal, currency, payment_method"
                      " FROM receipts WHERE id = %s AND user_id = %s", (rid, uid)).fetchone()
        if not r:
            raise HTTPException(404)
        items = c.execute("SELECT description, quantity, amount, category, subcategory FROM line_items"
                          " WHERE receipt_id = %s AND user_id = %s AND time = %s ORDER BY amount DESC",
                          (rid, uid, r["purchased_at"])).fetchall()
    out = rows_json([r])[0]
    out["items"] = rows_json(items)
    return out


@app.delete("/api/receipts/{rid}")
def delete_receipt(rid: int):
    with db.conn() as c:
        r = c.execute("SELECT purchased_at FROM receipts WHERE id = %s AND user_id = %s",
                      (rid, USER_ID)).fetchone()
        if not r:
            raise HTTPException(404)
        c.execute("DELETE FROM line_items WHERE receipt_id = %s AND user_id = %s AND time = %s",
                  (rid, USER_ID, r["purchased_at"]))
        c.execute("DELETE FROM receipts WHERE id = %s", (rid,))
        c.commit()
    refresh_aggregates(r["purchased_at"])
    return {"deleted": rid}


# ---------------------------------------------------------------------------
# Dashboard: reads only from continuous aggregates
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(month: Optional[str] = None, user: Optional[int] = None):
    uid = viewer(user)
    with db.conn() as c:
        if month:
            m0 = date.fromisoformat(month + "-01")
        else:  # default to the latest month with data, so demo users show something
            latest = c.execute("SELECT max(month) AS m FROM spend_monthly WHERE user_id = %s",
                               (uid,)).fetchone()["m"]
            m0 = latest.astimezone(TZ).date().replace(day=1) if latest else datetime.now(TZ).date().replace(day=1)
        m1 = (m0 + timedelta(days=32)).replace(day=1)
        prev0 = (m0 - timedelta(days=1)).replace(day=1)
        start12 = (m0 - timedelta(days=330)).replace(day=1)

        t0 = time.perf_counter()
        monthly = c.execute(
            "SELECT month, category, round(total, 2) AS total FROM spend_monthly"
            " WHERE user_id = %s AND month >= %s AND month < %s ORDER BY month",
            (uid, start12, m1)).fetchall()
        by_sub = c.execute(
            "SELECT category, subcategory, round(sum(total), 2) AS total, sum(items) AS items"
            " FROM spend_daily WHERE user_id = %s AND day >= %s AND day < %s"
            " GROUP BY 1, 2 ORDER BY total DESC", (uid, m0, m1)).fetchall()
        daily = c.execute(
            "SELECT day, round(sum(total), 2) AS total FROM spend_daily"
            " WHERE user_id = %s AND day >= %s AND day < %s GROUP BY day ORDER BY day",
            (uid, prev0, m1)).fetchall()
        merchants = c.execute(
            "SELECT merchant, round(total, 2) AS total, visits FROM merchant_monthly"
            " WHERE user_id = %s AND month = %s ORDER BY total DESC LIMIT 8", (uid, m0)).fetchall()
        months_available = c.execute(
            "SELECT DISTINCT month FROM spend_monthly WHERE user_id = %s ORDER BY month DESC LIMIT 36",
            (uid,)).fetchall()
        query_ms = (time.perf_counter() - t0) * 1000

    def tot(rows, lo, hi):
        return round(sum(float(r["total"]) for r in rows if lo <= r["day"].astimezone(TZ).date() < hi), 2)

    return {
        "user": uid,
        "month": m0.isoformat()[:7],
        "months_available": [r["month"].astimezone(TZ).date().isoformat()[:7] for r in months_available],
        "this_month": tot(daily, m0, m1),
        "last_month": tot(daily, prev0, m0),
        # For the month in progress, compare against last month up to the same day.
        "last_month_to_date": (tot(daily, prev0, min(prev0 + (datetime.now(TZ).date() - m0) + timedelta(days=1), m0))
                               if m0 == datetime.now(TZ).date().replace(day=1) else None),
        "monthly": rows_json(monthly),
        "by_subcategory": rows_json(by_sub),
        "daily": rows_json(daily),
        "merchants": rows_json(merchants),
        "query_ms": round(query_ms, 1),
    }


# ---------------------------------------------------------------------------
# Ask your receipts
# ---------------------------------------------------------------------------

class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def _run_readonly(sql: str, uid: int) -> tuple[list[dict], float]:
    sql = sql.strip().rstrip(";")
    with db.conn() as c:
        try:
            c.execute("SET TRANSACTION READ ONLY")
            c.execute("SET LOCAL statement_timeout = '5s'")
            c.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
            try:
                c.execute("SET LOCAL ROLE receipt_reader")
            except Exception:
                c.rollback()
                raise RuntimeError("The read-only role is missing; re-run the schema as an admin.")
            t0 = time.perf_counter()
            # Wrapping as a subquery rejects multi-statement input and caps the result size.
            rows = c.execute(f"SELECT * FROM ({sql}) AS q LIMIT 500").fetchall()
            return rows, (time.perf_counter() - t0) * 1000
        finally:
            c.rollback()


@app.post("/api/ask")
async def ask(body: Ask, user: Optional[int] = None):
    uid = viewer(user)
    plan = error = None
    for _ in range(2):  # one retry, feeding the database error back to Gemini
        plan = await run_in_threadpool(gemini.plan_sql, body.question, TIMEZONE, error,
                                       plan.sql if plan else None)
        try:
            rows, ms = await run_in_threadpool(_run_readonly, plan.sql, uid)
            break
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        except Exception as e:
            error = str(e).splitlines()[0]
    else:
        raise HTTPException(422, f"Couldn't answer that one. Last error: {error}")
    rows = rows_json(rows)
    answer = await run_in_threadpool(gemini.phrase_answer, body.question, plan.sql, rows)
    return {"answer": answer, "sql": plan.sql, "chart": plan.chart, "x": plan.x, "y": plan.y,
            "title": plan.title, "rows": rows, "query_ms": round(ms, 1)}


# ---------------------------------------------------------------------------
# Performance panel: the Tiger Data story in numbers
# ---------------------------------------------------------------------------

BENCH_RAW = """SELECT time_bucket('1 month', time) AS month, category, sum(amount) AS total
FROM line_items GROUP BY 1, 2"""
BENCH_CAGG = """SELECT month, category, sum(total) AS total FROM spend_monthly GROUP BY 1, 2"""


def _timed(c, sql, runs=2):
    best = None
    for _ in range(runs):
        t0 = time.perf_counter()
        c.execute(sql).fetchall()
        ms = (time.perf_counter() - t0) * 1000
        best = ms if best is None else min(best, ms)
    return round(best, 1)


@app.get("/api/stats")
def stats(bench: bool = True):
    with db.conn() as c:
        counts = c.execute(
            "SELECT approximate_row_count('line_items') AS line_items,"
            " (SELECT count(*) FROM receipts) AS receipts,"
            " (SELECT count(*) FROM users) AS users,"
            " (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'line_items') AS chunks,"
            " (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'line_items'"
            "   AND is_compressed) AS compressed_chunks").fetchone()
        comp = c.execute(
            "SELECT sum(before_compression_total_bytes) AS before,"
            " sum(after_compression_total_bytes) AS after"
            " FROM hypertable_columnstore_stats('line_items')").fetchone()
        size = c.execute("SELECT hypertable_size('line_items') AS bytes").fetchone()["bytes"]
        out = {**counts, "hypertable_bytes": size,
               "compressed_before_bytes": comp["before"], "compressed_after_bytes": comp["after"]}
        if comp["before"]:
            out["compression_ratio"] = round(comp["before"] / max(comp["after"], 1), 1)
            out["space_saved_pct"] = round(100 * (1 - comp["after"] / comp["before"]), 1)
        if bench:
            out["bench"] = {
                "question": "Total spend by month and category, across every user and all time",
                "raw_ms": _timed(c, BENCH_RAW),
                "cagg_ms": _timed(c, BENCH_CAGG),
            }
            out["bench"]["speedup"] = round(out["bench"]["raw_ms"] / max(out["bench"]["cagg_ms"], 0.1), 1)
        demo = c.execute("SELECT id FROM users WHERE is_demo ORDER BY id LIMIT 1").fetchone()
        out["demo_user"] = demo["id"] if demo else None
        c.rollback()
    return out


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    sentry_sdk.capture_exception(exc)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on the server."})
