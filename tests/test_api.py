"""End-to-end API test against a real TimescaleDB, with Gemini stubbed out.

    python -m pytest -q tests     (needs DATABASE_URL pointing at a scratch database)
"""
import io

import pytest
from fastapi.testclient import TestClient

from app import gemini, main


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


FAKE = gemini.Extraction(
    is_receipt=True, merchant="Test Mart", purchase_date="2026-10-18", purchase_time="14:05",
    currency="USD", total=12.84, tax=0.84, tip=None, subtotal=12.00, payment_method="Visa ••4417",
    items=[
        gemini.Item(description="Avocados", quantity=3, amount=4.50, category="Groceries", subcategory="Produce"),
        gemini.Item(description="Oat milk", quantity=1, amount=4.25, category="Groceries", subcategory="Dairy & Eggs"),
        gemini.Item(description="Dish soap", quantity=1, amount=3.25, category="Household", subcategory="Cleaning Supplies"),
    ],
)


def test_extract_save_and_query(client, monkeypatch):
    monkeypatch.setattr(gemini, "extract_receipt", lambda data, mime: FAKE.model_copy(deep=True))
    img = io.BytesIO(b"\x89PNG fake image bytes " + str(id(client)).encode())
    r = client.post("/api/extract", files={"file": ("r.png", img, "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checks"] == []  # items + tax == total
    x = body["extraction"]

    saved = client.post("/api/receipts", json={**x, "image_sha256": body["image_sha256"], "raw": x})
    assert saved.status_code == 200, saved.text
    rid = saved.json()["id"]
    assert saved.json()["items"] == 4  # three lines + tax row

    # same photo again is caught before Gemini is called
    img.seek(0)
    again = client.post("/api/extract", files={"file": ("r.png", img, "image/png")})
    assert again.json().get("duplicate", {}).get("id") == rid

    detail = client.get(f"/api/receipts/{rid}").json()
    assert round(sum(i["amount"] for i in detail["items"]), 2) == 12.84

    dash = client.get("/api/dashboard?month=2026-10").json()
    groceries = sum(r["total"] for r in dash["by_subcategory"] if r["category"] == "Groceries")
    assert groceries >= 8.75

    monkeypatch.setattr(gemini, "plan_sql", lambda q, tz, e=None, p=None: gemini.SqlPlan(
        sql="SELECT subcategory, sum(amount) AS spent FROM my_line_items "
            "WHERE category = 'Groceries' GROUP BY 1 ORDER BY 2 DESC",
        chart="bar", x="subcategory", y="spent", title="Groceries"))
    monkeypatch.setattr(gemini, "phrase_answer", lambda q, s, rows: "ok")
    ans = client.post("/api/ask", json={"question": "where did grocery money go?"})
    assert ans.status_code == 200, ans.text
    assert {r["subcategory"] for r in ans.json()["rows"]} >= {"Produce", "Dairy & Eggs"}

    assert client.delete(f"/api/receipts/{rid}").status_code == 200


@pytest.mark.parametrize("sql", [
    "SELECT * FROM line_items",                      # raw table: other people's data
    "SELECT * FROM receipts",
    "DELETE FROM receipts",                          # writes
    "SELECT 1; DROP TABLE receipts",                 # stacked statements
    "SELECT pg_sleep(10)",                           # runaway queries hit the timeout
])
def test_generated_sql_is_sandboxed(client, monkeypatch, sql):
    monkeypatch.setattr(gemini, "plan_sql", lambda q, tz, e=None, p=None: gemini.SqlPlan(
        sql=sql, chart="table", title="x"))
    r = client.post("/api/ask", json={"question": "try something sneaky"})
    assert r.status_code == 422, r.text


def test_demo_users_are_read_only_and_isolated(client):
    assert client.get("/api/dashboard?user=5").status_code == 403
    assert client.get("/api/receipts?user=1000").status_code == 200
