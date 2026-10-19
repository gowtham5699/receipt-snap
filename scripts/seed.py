"""Generate realistic synthetic purchase history for demo users, then compress it.

    python -m scripts.seed --users 1000 --months 24

Your own receipts (user 1) are never touched. Demo users get ids 1000+.
Everything is generated inside Postgres with generate_series, so millions of rows
take seconds to minutes rather than hours.
"""
from __future__ import annotations

import argparse
import time

import psycopg

from app import categories
from app.config import DATABASE_URL

# merchant, category, typical subcategories, avg item price, min items, max items, relative frequency
MERCHANTS = [
    ("Harris Teeter", "Groceries", ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Pantry & Dry Goods", "Frozen", "Snacks & Sweets", "Beverages"], 4.5, 4, 22, 14),
    ("Food Lion", "Groceries", ["Produce", "Meat & Seafood", "Dairy & Eggs", "Pantry & Dry Goods", "Frozen", "Beverages"], 3.8, 3, 18, 10),
    ("Trader Joe's", "Groceries", ["Produce", "Prepared Foods", "Snacks & Sweets", "Frozen", "Dairy & Eggs"], 4.2, 4, 16, 9),
    ("Patel Brothers", "Groceries", ["Spices & Ethnic Foods", "Produce", "Pantry & Dry Goods", "Snacks & Sweets"], 4.8, 3, 14, 4),
    ("Costco", "Groceries", ["Meat & Seafood", "Pantry & Dry Goods", "Beverages", "Snacks & Sweets", "Produce"], 13.0, 3, 12, 4),
    ("Starbucks", "Dining Out", ["Coffee & Tea"], 5.6, 1, 3, 12),
    ("Chipotle", "Dining Out", ["Fast Food"], 11.5, 1, 3, 6),
    ("Chick-fil-A", "Dining Out", ["Fast Food"], 8.5, 1, 4, 6),
    ("Local Restaurant", "Dining Out", ["Restaurants"], 18.0, 2, 6, 6),
    ("DoorDash", "Dining Out", ["Food Delivery"], 16.0, 1, 4, 5),
    ("Shell", "Transport", ["Gas & Fuel"], 42.0, 1, 1, 7),
    ("Uber", "Transport", ["Rideshare & Taxi"], 19.0, 1, 1, 3),
    ("CVS Pharmacy", "Health", ["Pharmacy", "Vitamins & Supplements"], 12.0, 1, 4, 4),
    ("Target", "Household", ["Cleaning Supplies", "Paper Goods", "Kitchenware", "Laundry"], 9.0, 2, 10, 6),
    ("Home Depot", "Household", ["Home Improvement"], 24.0, 1, 6, 2),
    ("Amazon", "Shopping", ["Electronics", "Books", "Gifts", "Hobbies & Crafts", "Office Supplies", "Clothing & Shoes"], 26.0, 1, 4, 7),
    ("Old Navy", "Shopping", ["Clothing & Shoes"], 22.0, 1, 5, 2),
    ("Ulta Beauty", "Personal Care", ["Beauty & Cosmetics", "Toiletries"], 17.0, 1, 4, 2),
    ("Great Clips", "Personal Care", ["Haircut & Salon"], 24.0, 1, 1, 1),
    ("Regal Cinemas", "Entertainment", ["Movies & Events"], 15.0, 1, 4, 1),
    ("Total Wine", "Alcohol", ["Beer", "Wine", "Spirits"], 17.0, 1, 5, 2),
    ("PetSmart", "Pets", ["Pet Food", "Pet Supplies"], 21.0, 1, 4, 2),
]

PRODUCTS = {
    "Produce": ["Bananas", "Avocados", "Baby spinach", "Roma tomatoes", "Red onions", "Strawberries", "Cilantro", "Lemons", "Broccoli crowns", "Gala apples"],
    "Meat & Seafood": ["Chicken breast", "Ground beef 85/15", "Atlantic salmon", "Chicken thighs", "Shrimp 1lb", "Pork chops"],
    "Dairy & Eggs": ["Large eggs dozen", "2% milk gallon", "Greek yogurt", "Cheddar block", "Paneer", "Butter"],
    "Bakery": ["Sourdough loaf", "Bagels 6ct", "Croissants", "Naan 4pk"],
    "Pantry & Dry Goods": ["Basmati rice 10lb", "Pasta", "Olive oil", "Peanut butter", "Canned chickpeas", "Toor dal", "Oats"],
    "Frozen": ["Frozen peas", "Frozen pizza", "Ice cream", "Frozen dumplings"],
    "Snacks & Sweets": ["Tortilla chips", "Dark chocolate", "Trail mix", "Cookies", "Popcorn"],
    "Beverages": ["Sparkling water 12pk", "Orange juice", "Cold brew", "Soda 12pk"],
    "Prepared Foods": ["Rotisserie chicken", "Hummus", "Salad kit", "Sushi roll"],
    "Spices & Ethnic Foods": ["Garam masala", "Turmeric", "Basmati rice", "Curry leaves", "Mango pickle", "Atta 20lb"],
    "Coffee & Tea": ["Latte", "Cold brew", "Chai latte", "Drip coffee", "Croissant"],
    "Fast Food": ["Burrito bowl", "Chicken sandwich", "Nuggets", "Fries", "Drink"],
    "Restaurants": ["Entree", "Appetizer", "Dessert", "Drinks"],
    "Food Delivery": ["Delivery order", "Delivery fee", "Service fee"],
    "Gas & Fuel": ["Regular unleaded"],
    "Rideshare & Taxi": ["UberX trip"],
    "Pharmacy": ["Allergy tablets", "Pain reliever", "Bandages", "Cough drops"],
    "Vitamins & Supplements": ["Multivitamin", "Vitamin D3", "Fish oil"],
    "Cleaning Supplies": ["Dish soap", "All-purpose cleaner", "Sponges", "Dishwasher pods"],
    "Paper Goods": ["Paper towels", "Toilet paper", "Napkins"],
    "Kitchenware": ["Storage containers", "Spatula", "Mixing bowl"],
    "Laundry": ["Laundry detergent", "Dryer sheets"],
    "Home Improvement": ["LED bulbs", "Paint", "Extension cord", "Screws", "Air filter"],
    "Electronics": ["USB-C cable", "Wireless earbuds", "Phone case", "Power bank"],
    "Books": ["Paperback novel", "Technical book"],
    "Gifts": ["Gift card", "Candle set", "Gift wrap"],
    "Hobbies & Crafts": ["Sketchbook", "Yarn", "Puzzle"],
    "Office Supplies": ["Notebooks", "Pens", "Printer paper"],
    "Clothing & Shoes": ["T-shirt", "Jeans", "Socks 6pk", "Running shoes", "Jacket"],
    "Beauty & Cosmetics": ["Moisturizer", "Sunscreen", "Lip balm"],
    "Toiletries": ["Toothpaste", "Shampoo", "Body wash", "Deodorant"],
    "Haircut & Salon": ["Haircut"],
    "Movies & Events": ["Movie ticket", "Popcorn combo"],
    "Beer": ["IPA 6pk", "Lager 12pk"],
    "Wine": ["Cabernet", "Pinot grigio"],
    "Spirits": ["Bourbon 750ml"],
    "Pet Food": ["Dry dog food", "Cat food cans"],
    "Pet Supplies": ["Cat litter", "Chew toys"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=1000)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--receipts-per-week", type=float, default=4.0)
    ap.add_argument("--reset", action="store_true", help="delete existing demo users first")
    args = ap.parse_args()

    for m in MERCHANTS:
        for s in m[2]:
            assert categories.is_valid(m[1], s), (m[0], s)
            assert s in PRODUCTS, s

    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        if args.reset:
            print("Removing old demo data…")
            c.execute("DELETE FROM line_items WHERE user_id >= 1000")
            c.execute("DELETE FROM receipts WHERE user_id >= 1000")
            c.execute("DELETE FROM users WHERE id >= 1000")

        start_id = c.execute("SELECT coalesce(max(id), 999) + 1 FROM users WHERE id >= 1000").fetchone()[0]
        t0 = time.time()
        with c.transaction():
            c.execute("CREATE TEMP TABLE seed_merchants (idx int, merchant text, category text, subs text[],"
                      " price float8, min_items int, max_items int, weight float8) ON COMMIT DROP")
            with c.cursor() as cur:
                cur.executemany("INSERT INTO seed_merchants VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                [(i, *m) for i, m in enumerate(MERCHANTS)])
            c.execute("CREATE TEMP TABLE seed_products (subcategory text, names text[]) ON COMMIT DROP")
            with c.cursor() as cur:
                cur.executemany("INSERT INTO seed_products VALUES (%s,%s)", list(PRODUCTS.items()))
            # cumulative weights for weighted merchant choice
            c.execute("""CREATE TEMP TABLE seed_mw ON COMMIT DROP AS
                SELECT idx, sum(weight) OVER (ORDER BY idx) / sum(weight) OVER () AS cum FROM seed_merchants""")

            c.execute("INSERT INTO users (id, name, is_demo) SELECT g, 'Demo user ' || g, TRUE "
                      "FROM generate_series(%s::int, %s::int) g", (start_id, start_id + args.users - 1))

            print(f"Generating receipts for {args.users} users over {args.months} months…")
            c.execute("""
            CREATE TEMP TABLE seed_receipts ON COMMIT DROP AS
            WITH u AS (
                SELECT id AS user_id, 0.6 + random() * 0.9 AS spend_factor
                FROM users WHERE id BETWEEN %(lo)s AND %(hi)s
            ), days AS (
                SELECT d::date AS day FROM generate_series(
                    date_trunc('day', now()) - make_interval(months => %(months)s),
                    date_trunc('day', now()) - INTERVAL '1 day', INTERVAL '1 day') d
            ), events AS (
                SELECT u.user_id, u.spend_factor, days.day, random() AS pick, random() AS when_r
                FROM u CROSS JOIN days
                CROSS JOIN LATERAL generate_series(1, 3) k
                WHERE random() < (%(rpw)s / 7.0 / 3.0)
                    * CASE WHEN extract(dow FROM days.day) IN (0, 6) THEN 1.4 ELSE 0.85 END
                    * CASE WHEN extract(month FROM days.day) IN (11, 12) THEN 1.25 ELSE 1 END
            )
            SELECT nextval('receipts_id_seq') AS id, e.user_id, e.spend_factor,
                   (e.day + make_interval(mins => (420 + floor(e.when_r * 840))::int)) AT TIME ZONE 'America/New_York' AS ts,
                   m.idx AS merchant_idx
            FROM events e
            JOIN LATERAL (SELECT idx FROM seed_mw WHERE cum >= e.pick ORDER BY cum LIMIT 1) m ON TRUE
            """, {"lo": start_id, "hi": start_id + args.users - 1, "months": args.months,
                  "rpw": args.receipts_per_week})

            print("Generating line items…")
            c.execute("""
            CREATE TEMP TABLE seed_items ON COMMIT DROP AS
            SELECT r.id AS receipt_id, r.user_id, r.ts, m.merchant, m.category, s.sub AS subcategory,
                   p.names[1 + floor(random() * array_length(p.names, 1))::int] AS description,
                   1 + (random() < 0.2)::int AS quantity,
                   round((m.price * r.spend_factor * exp((random() - 0.5) * 1.2))::numeric, 2) AS amount
            FROM seed_receipts r
            JOIN seed_merchants m ON m.idx = r.merchant_idx
            CROSS JOIN LATERAL generate_series(1, m.min_items + floor(random() * (m.max_items - m.min_items + 1))::int) n
            CROSS JOIN LATERAL (SELECT m.subs[1 + floor(random() * array_length(m.subs, 1))::int] AS sub
                                WHERE n > 0) s
            JOIN seed_products p ON p.subcategory = s.sub
            """)

            c.execute("""
            INSERT INTO receipts (id, user_id, merchant, purchased_at, subtotal, tax, total, payment_method)
            SELECT i.receipt_id, i.user_id, min(i.merchant), min(i.ts), sum(i.amount),
                   round(sum(i.amount) * 0.0725, 2), sum(i.amount) + round(sum(i.amount) * 0.0725, 2), 'Card'
            FROM seed_items i GROUP BY i.receipt_id, i.user_id
            """)
            c.execute("""
            INSERT INTO line_items (time, user_id, receipt_id, merchant, category, subcategory, description, quantity, amount)
            SELECT ts, user_id, receipt_id, merchant, category, subcategory, description, quantity, amount FROM seed_items
            UNION ALL
            SELECT min(ts), user_id, receipt_id, min(merchant), 'Fees & Taxes', 'Sales Tax', 'Sales tax', 1,
                   round(sum(amount) * 0.0725, 2)
            FROM seed_items GROUP BY receipt_id, user_id
            """)
            n_items = c.execute("SELECT count(*) FROM seed_items").fetchone()[0]
            n_receipts = c.execute("SELECT count(*) FROM seed_receipts").fetchone()[0]
        print(f"  {n_receipts:,} receipts, {n_items:,} items (+ tax rows) in {time.time() - t0:.0f}s")

        before = c.execute("SELECT hypertable_size('line_items')").fetchone()[0]
        print("Compressing chunks older than 30 days into the columnstore…")
        t1 = time.time()
        c.execute("SELECT count(compress_chunk(ch, if_not_compressed => true)) FROM "
                  "show_chunks('line_items', older_than => INTERVAL '30 days') ch")
        after = c.execute("SELECT hypertable_size('line_items')").fetchone()[0]
        print(f"  {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB in {time.time() - t1:.0f}s")

        print("Refreshing continuous aggregates…")
        for view in ("spend_daily", "spend_monthly", "merchant_monthly"):
            c.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
        c.execute("ANALYZE line_items")
        print(f"Done in {time.time() - t0:.0f}s total.")


if __name__ == "__main__":
    main()
