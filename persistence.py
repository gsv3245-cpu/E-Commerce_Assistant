"""Small PostgreSQL adapter for state that must survive Vercel invocations.

Product, order and policy catalogues remain the assignment CSV files. This
module stores user identities and mutable application state in Neon.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(DATABASE_URL)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def initialize() -> None:
    if not enabled():
        return
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_customers (
              customer_id TEXT PRIMARY KEY, first_name TEXT NOT NULL DEFAULT '',
              last_name TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS customer_accounts (
              email TEXT PRIMARY KEY, customer_id TEXT NOT NULL UNIQUE REFERENCES app_customers(customer_id)
            );
            CREATE TABLE IF NOT EXISTS app_state (
              state_key TEXT PRIMARY KEY, state_value JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS return_requests (
              request_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
              customer_id TEXT NOT NULL REFERENCES app_customers(customer_id), reason TEXT NOT NULL,
              reason_type TEXT NOT NULL, created_on DATE NOT NULL
            );
        """)
        _seed_csv(conn)


def _seed_csv(conn) -> None:
    root = Path(__file__).resolve().parent / "data"
    customer_path, account_path = root / "customers.csv", root / "customer_accounts.csv"
    if customer_path.is_file():
        with customer_path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cid = (r.get("customer_id") or "").strip()
                if cid:
                    conn.execute("""INSERT INTO app_customers(customer_id,first_name,last_name,city)
                      VALUES (%s,%s,%s,%s) ON CONFLICT(customer_id) DO NOTHING""",
                      (cid, (r.get("first_name") or "").strip(), (r.get("last_name") or "").strip(), (r.get("city") or "").strip()))
    if account_path.is_file():
        with account_path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                email, cid = (r.get("email") or "").strip().casefold(), (r.get("customer_id") or "").strip()
                if email and cid:
                    conn.execute("INSERT INTO customer_accounts(email,customer_id) VALUES (%s,%s) ON CONFLICT(email) DO NOTHING", (email, cid))


def accounts() -> dict[str, str]:
    initialize()
    with _connect() as conn:
        return {r["email"]: r["customer_id"] for r in conn.execute("SELECT email,customer_id FROM customer_accounts").fetchall()}


def register(email: str, first_name: str, last_name: str) -> str:
    initialize()
    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(84523901)")
        if conn.execute("SELECT 1 FROM customer_accounts WHERE email=%s", (email,)).fetchone():
            raise FileExistsError("This email already has an account")
        # Preserve the existing C#### ID convention while avoiding IDs in the order CSV.
        root = Path(__file__).resolve().parent / "data" / "orders.csv"
        ids = []
        if root.is_file():
            with root.open(newline="", encoding="utf-8") as f:
                ids = [r.get("customer_id", "") for r in csv.DictReader(f)]
        rows = conn.execute("SELECT customer_id FROM app_customers").fetchall()
        used = {r["customer_id"].upper() for r in rows} | {str(x).upper() for x in ids}
        nums = [int(x[1:]) for x in used if len(x) > 1 and x[0] == "C" and x[1:].isdigit()]
        n = max(nums, default=0) + 1
        while f"C{n:04d}" in used:
            n += 1
        cid = f"C{n:04d}"
        conn.execute("INSERT INTO app_customers(customer_id,first_name,last_name,city) VALUES (%s,%s,%s,'')", (cid, first_name, last_name))
        conn.execute("INSERT INTO customer_accounts(email,customer_id) VALUES (%s,%s)", (email, cid))
        return cid


def customers() -> dict[str, dict[str, str]]:
    initialize()
    with _connect() as conn:
        rows = conn.execute("SELECT customer_id,first_name,last_name,city FROM app_customers").fetchall()
    return {r["customer_id"]: {**r, "name": " ".join(x for x in (r["first_name"], r["last_name"]) if x) or r["customer_id"]} for r in rows}


def state_get(key: str, default: Any = None) -> Any:
    initialize()
    with _connect() as conn:
        row = conn.execute("SELECT state_value FROM app_state WHERE state_key=%s", (key,)).fetchone()
    return row["state_value"] if row else default


def state_set(key: str, value: Any) -> None:
    initialize()
    with _connect() as conn:
        conn.execute("""INSERT INTO app_state(state_key,state_value) VALUES (%s,%s)
          ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value""", (key, json.dumps(value)))


def state_delete(key: str) -> None:
    initialize()
    with _connect() as conn:
        conn.execute("DELETE FROM app_state WHERE state_key=%s", (key,))


def state_update(key: str, updater, default: Any = None):
    """Run a read/update/write under an advisory lock across server instances."""
    initialize()
    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        row = conn.execute("SELECT state_value FROM app_state WHERE state_key=%s FOR UPDATE", (key,)).fetchone()
        value = updater(row["state_value"] if row else default)
        if value is None:
            conn.execute("DELETE FROM app_state WHERE state_key=%s", (key,))
        else:
            conn.execute("""INSERT INTO app_state(state_key,state_value) VALUES (%s,%s)
              ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value""", (key, json.dumps(value)))
        return value


def returns() -> dict[str, dict[str, Any]]:
    initialize()
    with _connect() as conn:
        rows = conn.execute("SELECT request_id,order_id,customer_id,reason,reason_type,created_on FROM return_requests").fetchall()
    return {r["order_id"]: {"return_request_id": r["request_id"], "order_id": r["order_id"], "customer_id": r["customer_id"], "reason": r["reason"], "reason_type": r["reason_type"], "created_on": r["created_on"].isoformat()} for r in rows}


def create_return(order_id: str, customer_id: str, reason: str, reason_type: str, created_on: str) -> str:
    initialize()
    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(84523902)")
        row = conn.execute("SELECT request_id FROM return_requests WHERE order_id=%s", (order_id,)).fetchone()
        if row:
            return row["request_id"]
        count = conn.execute("SELECT count(*) AS n FROM return_requests").fetchone()["n"]
        request_id = f"RR-{count + 1:04d}"
        conn.execute("INSERT INTO return_requests(request_id,order_id,customer_id,reason,reason_type,created_on) VALUES (%s,%s,%s,%s,%s,%s)", (request_id, order_id, customer_id, reason, reason_type, created_on))
        return request_id


def delete_customer(email: str) -> None:
    """Test helper to remove a temporary account and its profile."""
    initialize()
    with _connect() as conn:
        row = conn.execute("DELETE FROM customer_accounts WHERE email=%s RETURNING customer_id", (email,)).fetchone()
        if row:
            conn.execute("DELETE FROM app_customers WHERE customer_id=%s", (row["customer_id"],))
