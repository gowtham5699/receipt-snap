import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/receipts")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
# Your own receipts live under this user. Seeded demo users get ids >= 1000.
USER_ID = int(os.environ.get("USER_ID", "1"))
# If set, the whole app sits behind HTTP basic auth (username: anything).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "15"))
# Receipts print local times; this is the zone they're interpreted in.
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
