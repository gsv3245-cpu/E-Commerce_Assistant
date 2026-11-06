"""
config.py
=========
Central settings. Everything can be overridden with environment variables, so
you normally do not need to edit this file.

    GROQ_API_KEY      Groq API key (loaded from data/.env).
    GROQ_MODEL        Groq model name (default "openai/gpt-oss-20b").
    FLASK_SECRET_KEY  stable signing key for login sessions (recommended).
    SMTP_*            Optional SMTP account settings for real email OTP delivery.
    OTP_DEVELOPMENT_MODE  Show local-only OTPs when true; set false for SMTP.
    ADMIN_LOGIN_EMAIL / ADMIN_LOGIN_OTP  Optional reusable code for one existing admin account.
    SHOP_TODAY        the simulated "today" as YYYY-MM-DD (default 2026-11-01)
"""
import os
import secrets
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(DATA_DIR / ".env")

STORE_NAME = "CircuitCart"  # fictional store

# The shop pretends it is this day. Fixing it keeps return-window answers
# identical on every machine and on every day the project is run.
TODAY = date.fromisoformat(os.getenv("SHOP_TODAY", "2026-11-01"))

# ---- LLM (Groq API) ----
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
LLM_TEMPERATURE = 0.0  # deterministic answers make testing repeatable

# ---- Agent loop ----
MAX_AGENT_STEPS = 6      # max LLM calls for one customer message
HISTORY_TURNS = 6        # how many previous exchanges are remembered
LOG_INTERACTIONS = True  # append every chat to logs/agent_trace.jsonl

# ---- Web server ----
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
SESSION_COOKIE_SECURE = os.getenv(
    "SESSION_COOKIE_SECURE", "true" if os.getenv("VERCEL") else ""
).strip().lower() in {"1", "true", "yes"}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ---- Email OTP login ----
CUSTOMER_ACCOUNTS_FILE = DATA_DIR / "customer_accounts.csv"
CUSTOMERS_FILE = DATA_DIR / "customers.csv"
OTP_TTL_SECONDS = 300
OTP_RESEND_SECONDS = 30
OTP_MAX_ATTEMPTS = 5
OTP_DEVELOPMENT_MODE = os.getenv("OTP_DEVELOPMENT_MODE", "true").strip().lower() in {"1", "true", "yes"}
# Reusable local administrator code. Keep it in data/.env; it is only honored
# for this one already-registered email and is disabled when unset.
ADMIN_LOGIN_EMAIL = os.getenv("ADMIN_LOGIN_EMAIL", "").strip().casefold()
ADMIN_LOGIN_OTP = os.getenv("ADMIN_LOGIN_OTP", "").strip()

# ---- SMTP delivery for real sign-in codes ----
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME).strip()
SMTP_TIMEOUT_SECONDS = 15
