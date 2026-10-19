"""All Gemini calls: receipt extraction, question -> SQL, and answer phrasing."""
from __future__ import annotations

import json
import random
import time
from datetime import date
from typing import Literal, Optional

import sentry_sdk
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from . import categories
from .config import GEMINI_API_KEY, GEMINI_MODEL

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _generate(contents, schema, *, temperature=0.0, op="extract"):
    with sentry_sdk.start_span(op="gen_ai.generate_content", name=f"gemini {op}") as span:
        span.set_data("gen_ai.request.model", GEMINI_MODEL)
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = client().models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
                break
            except genai_errors.APIError as e:
                last_exc = e
                if getattr(e, "code", None) not in _RETRY_STATUSES or attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, 8) + random.random())
        else:  # pragma: no cover
            raise last_exc  # type: ignore[misc]
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            span.set_data("gen_ai.usage.input_tokens", usage.prompt_token_count)
            span.set_data("gen_ai.usage.output_tokens", usage.candidates_token_count)
    if resp.parsed is not None:
        return resp.parsed
    return schema.model_validate(json.loads(resp.text))


# ---------------------------------------------------------------------------
# 1. Receipt extraction
# ---------------------------------------------------------------------------

Category = Literal[tuple(categories.CATEGORY_NAMES)]  # type: ignore[valid-type]
Subcategory = Literal[tuple(categories.SUBCATEGORY_NAMES)]  # type: ignore[valid-type]


class Item(BaseModel):
    description: str = Field(description="Readable product name; expand receipt abbreviations, e.g. 'ORG BNNA' -> 'Organic bananas'")
    quantity: float = Field(1, description="Count or weight; 1 if not shown")
    amount: float = Field(description="Line total actually charged, after any per-item discount. Negative for discounts/coupons/returns.")
    category: Category
    subcategory: Subcategory


class Extraction(BaseModel):
    is_receipt: bool = Field(description="False if the image is not a purchase receipt or invoice")
    merchant: str = Field(description="Store or business name, cleaned up (e.g. 'Harris Teeter', not 'HARRIS TEETER #123')")
    purchase_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    purchase_time: Optional[str] = Field(None, description="HH:MM 24h, if printed")
    currency: str = Field("USD", description="ISO 4217 code")
    items: list[Item]
    subtotal: Optional[float] = None
    tax: Optional[float] = Field(None, description="Total sales tax, 0 if none")
    tip: Optional[float] = Field(None, description="Tip/gratuity if present")
    total: float = Field(description="Grand total paid")
    payment_method: Optional[str] = Field(None, description="e.g. 'Visa ••4417', 'Cash', 'Apple Pay'. Never the full card number.")
    warnings: list[str] = Field(default_factory=list, description="Anything unreadable or uncertain the user should double-check")


EXTRACT_PROMPT = """You are reading a photo or PDF of a purchase receipt. Extract it exactly.

Rules:
- One entry per purchased line. Put tax and tip in their fields, NOT in items.
- Apply discounts/coupons: either fold them into the item's amount or add a negative item
  with category "Fees & Taxes" / subcategory "Discount". Never double-count.
- Bag fees, service charges, delivery fees -> "Fees & Taxes" / "Service & Bag Fees".
- Pick the single best category + subcategory pair from this list. The subcategory MUST belong
  to the chosen category:
{taxonomy}
- Restaurant/cafe receipts: food lines are "Dining Out", not "Groceries".
- If the year is missing or ambiguous, assume the most recent date not after {today}.
- If something is unreadable, make your best guess and add a short warning.
"""


def extract_receipt(data: bytes, mime_type: str) -> Extraction:
    prompt = EXTRACT_PROMPT.format(taxonomy=categories.prompt_listing(), today=date.today().isoformat())
    result: Extraction = _generate(
        [types.Part.from_bytes(data=data, mime_type=mime_type), prompt], Extraction, op="extract")
    for it in result.items:  # guard against a subcategory from another category
        if not categories.is_valid(it.category, it.subcategory):
            it.subcategory = categories.TAXONOMY[it.category]["subs"][0]
    return result


# ---------------------------------------------------------------------------
# 2. Question -> SQL
# ---------------------------------------------------------------------------

class SqlPlan(BaseModel):
    sql: str = Field(description="One PostgreSQL SELECT statement, no trailing semicolon")
    chart: Literal["bar", "line", "pie", "number", "table"] = Field(description="Best way to show the result")
    x: Optional[str] = Field(None, description="Result column for the x axis / labels")
    y: Optional[str] = Field(None, description="Numeric result column for the y axis / values")
    title: str = Field(description="Short chart title")


SQL_PROMPT = """You write PostgreSQL (TimescaleDB) queries over ONE person's receipts.
You may ONLY use these views. They are already filtered to this person.

my_line_items(time timestamptz, receipt_id bigint, merchant text, category text,
              subcategory text, description text, quantity numeric, amount numeric)
    -- one row per purchased item; tax/tip are rows with category 'Fees & Taxes'
my_receipts(receipt_id bigint, merchant text, purchased_at timestamptz, currency text,
            subtotal numeric, tax numeric, tip numeric, total numeric, payment_method text)
my_spend_daily(day timestamptz, category text, subcategory text, total numeric, items bigint, receipts bigint)
my_spend_monthly(month timestamptz, category text, total numeric, items bigint, receipts bigint)
categories(category text, subcategory text, color text)

Valid categories and subcategories:
{taxonomy}

Guidance:
- Prefer my_spend_daily / my_spend_monthly for totals over time (pre-aggregated, fast).
- Use my_line_items for specific products, e.g. description ILIKE '%avocado%'.
- Use time_bucket('1 week', time) for weekly series. Today is {today} ({tz}).
- "this month" = date_trunc('month', now()). Round money with round(x, 2).
- Give result columns friendly snake_case names. Order time series by time ascending.
- Return a single SELECT (CTEs allowed). Never modify data.
{retry}
Question: {question}
"""


def plan_sql(question: str, tz: str, error: str | None = None, previous_sql: str | None = None) -> SqlPlan:
    retry = ""
    if error:
        retry = f"\nYour previous query failed.\nQuery: {previous_sql}\nError: {error}\nFix it.\n"
    prompt = SQL_PROMPT.format(taxonomy=categories.prompt_listing(), today=date.today().isoformat(),
                               tz=tz, retry=retry, question=question)
    return _generate(prompt, SqlPlan, op="plan_sql")


class Answer(BaseModel):
    answer: str = Field(description="1-2 sentence plain-English answer with the key numbers")


def phrase_answer(question: str, sql: str, rows: list[dict]) -> str:
    sample = json.dumps(rows[:40], default=str)
    prompt = (f"Question: {question}\nSQL used: {sql}\nResult rows (JSON, maybe truncated): {sample}\n"
              f"Total rows: {len(rows)}\nAnswer the question directly from these rows. "
              f"Use $ for USD amounts. If there are no rows, say no matching purchases were found.")
    return _generate(prompt, Answer, op="phrase_answer", temperature=0.2).answer
