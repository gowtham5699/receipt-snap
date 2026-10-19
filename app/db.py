from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import categories
from .config import DATABASE_URL, TIMEZONE

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": False,
                              "options": f"-c TimeZone={TIMEZONE}"})

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


@contextmanager
def conn():
    with pool.connection() as c:
        yield c


def init_schema() -> None:
    """Apply schema.sql and sync the category taxonomy. Idempotent."""
    with pool.connection() as c:
        c.autocommit = True  # continuous aggregates can't be created in a transaction
        c.execute(SCHEMA.read_text().replace("{{TIMEZONE}}", TIMEZONE))
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO categories (category, subcategory, color) VALUES (%s, %s, %s) "
                "ON CONFLICT (category, subcategory) DO UPDATE SET color = EXCLUDED.color",
                list(categories.rows()),
            )
        c.autocommit = False
