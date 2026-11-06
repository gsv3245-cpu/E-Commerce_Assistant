"""Flask application for the authenticated CircuitCart dashboard."""
from __future__ import annotations

import csv
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import tempfile
import time
import uuid
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for

import config
import llm as llm_module
import mailer
import persistence
import tools as shop
from agent import ShopAgent
from llm import LLMUnavailable

app = Flask(__name__, static_folder=str(config.BASE_DIR / "static"), static_url_path="/static")
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
)
agent = ShopAgent()
_histories: Dict[str, list] = {}
_pending_otps: Dict[str, Dict[str, Any]] = {}
_otp_ip_requests: Dict[str, list] = {}
_admin_otp_failures: Dict[str, Dict[str, float]] = {}
_lock = threading.Lock()
_auth_lock = threading.Lock()


def _request_data() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalize_email(value: Any) -> Optional[str]:
    email = str(value or "").strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None
    return email


def _masked_email(email: str) -> str:
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _customer_accounts() -> Dict[str, str]:
    if persistence.enabled():
        return persistence.accounts()
    if not config.CUSTOMER_ACCOUNTS_FILE.is_file():
        raise FileNotFoundError(
            f"Customer mapping file is missing: {config.CUSTOMER_ACCOUNTS_FILE.name}"
        )
    accounts: Dict[str, str] = {}
    with open(config.CUSTOMER_ACCOUNTS_FILE, newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if not {"email", "customer_id"}.issubset(reader.fieldnames or []):
            raise ValueError("customer_accounts.csv must contain email and customer_id columns")
        for row in reader:
            email = _normalize_email(row.get("email"))
            customer_id = shop._norm_customer(row.get("customer_id"))
            if not email or not customer_id:
                raise ValueError("customer_accounts.csv contains an invalid mapping row")
            if customer_id not in shop.load_customers():
                raise ValueError(f"Unknown customer_id in customer_accounts.csv: {customer_id}")
            previous = accounts.get(email)
            if previous and previous != customer_id:
                raise ValueError(f"Email is mapped to multiple customers: {_masked_email(email)}")
            accounts[email] = customer_id
    return accounts


def _clean_name(value: Any) -> Optional[str]:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 50 or not all(ch.isalpha() or ch in " '-" for ch in name):
        return None
    return name


def _write_csv_atomic(path, fieldnames, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent,
                                    delete=False) as target:
        temp_path = target.name
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def _register_customer_account(email: str, first_name: str, last_name: str) -> str:
    """Persist a verified signup as a customer profile plus email mapping."""
    if persistence.enabled():
        customer_id = persistence.register(email, first_name, last_name)
        shop.reload_data()
        return customer_id
    customer_path = config.CUSTOMERS_FILE
    accounts_path = config.CUSTOMER_ACCOUNTS_FILE
    with open(customer_path, newline="", encoding="utf-8") as source:
        customer_reader = csv.DictReader(source)
        customer_fields = customer_reader.fieldnames or ["customer_id", "first_name", "last_name", "city"]
        customers = list(customer_reader)
    with open(accounts_path, newline="", encoding="utf-8") as source:
        account_reader = csv.DictReader(source)
        account_fields = account_reader.fieldnames or ["email", "customer_id"]
        accounts = list(account_reader)

    for row in accounts:
        if _normalize_email(row.get("email")) == email:
            raise FileExistsError("This email already has an account")

    used_ids = {row.get("customer_id", "").strip().upper() for row in customers}
    used_ids.update(row.get("customer_id", "").strip().upper() for row in shop.load_orders())
    numeric_ids = [int(match.group(1)) for value in used_ids
                   if (match := re.fullmatch(r"C(\d+)", value))]
    next_number = max(numeric_ids, default=0) + 1
    customer_id = f"C{next_number:04d}"
    while customer_id in used_ids:
        next_number += 1
        customer_id = f"C{next_number:04d}"

    customers.append({
        "customer_id": customer_id,
        "first_name": first_name,
        "last_name": last_name,
        "city": "",
    })
    accounts.append({"email": email, "customer_id": customer_id})
    old_customers, old_accounts = customer_path.read_bytes(), accounts_path.read_bytes()
    try:
        _write_csv_atomic(customer_path, customer_fields, customers)
        _write_csv_atomic(accounts_path, account_fields, accounts)
    except OSError:
        _write_csv_atomic(customer_path, customer_fields,
                          list(csv.DictReader(old_customers.decode("utf-8").splitlines())))
        _write_csv_atomic(accounts_path, account_fields,
                          list(csv.DictReader(old_accounts.decode("utf-8").splitlines())))
        raise
    shop.reload_data()
    return customer_id


def _authenticated_customer() -> Optional[Tuple[str, str]]:
    email = _normalize_email(session.get("email"))
    customer_id = shop._norm_customer(session.get("customer_id"))
    if not email or not customer_id:
        return None
    try:
        if _customer_accounts().get(email) == customer_id:
            return email, customer_id
    except (OSError, ValueError):
        app.logger.exception("Unable to validate the customer account mapping")
        return None
    session.clear()
    return None


def _login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _authenticated_customer() is None:
            return jsonify({"error": "Please sign in with a linked customer account."}), 401
        return view(*args, **kwargs)

    return wrapped


def _otp_digest(email: str, code: str) -> str:
    return hmac.new(config.SECRET_KEY.encode("utf-8"), f"{email}:{code}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _allow_otp_request(email: str, client_ip: str, now: float) -> Tuple[bool, int]:
    if persistence.enabled():
        record = persistence.state_get(f"otp:{email}")
        if record:
            elapsed = now - record["last_sent"]
            if elapsed < config.OTP_RESEND_SECONDS:
                return False, max(1, int(config.OTP_RESEND_SECONDS - elapsed))
            if now - record["window_started"] < 3600 and record["request_count"] >= 5:
                return False, max(1, int(3600 - (now - record["window_started"])))
        accepted = {"value": False}
        def prune(values):
            values = [v for v in (values or []) if now - v < 600]
            if len(values) >= 20:
                return values
            values.append(now)
            accepted["value"] = True
            return values
        stamps = persistence.state_update(f"otp-ip:{client_ip}", prune, [])
        if not accepted["value"]:
            retry = max(1, int(600 - (now - stamps[0])))
            return False, retry
        return True, 0
    record = _pending_otps.get(email)
    if record:
        elapsed = now - record["last_sent"]
        if elapsed < config.OTP_RESEND_SECONDS:
            return False, max(1, int(config.OTP_RESEND_SECONDS - elapsed))
        if now - record["window_started"] < 3600 and record["request_count"] >= 5:
            return False, max(1, int(3600 - (now - record["window_started"])))

    requests = [stamp for stamp in _otp_ip_requests.get(client_ip, []) if now - stamp < 600]
    if len(requests) >= 20:
        _otp_ip_requests[client_ip] = requests
        return False, max(1, int(600 - (now - requests[0])))
    requests.append(now)
    _otp_ip_requests[client_ip] = requests
    return True, 0


def _create_otp(email: str, now: float, signup: Optional[Dict[str, str]] = None) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    previous = persistence.state_get(f"otp:{email}") if persistence.enabled() else _pending_otps.get(email)
    started = now
    count = 1
    if previous and now - previous["window_started"] < 3600:
        started = previous["window_started"]
        count = previous["request_count"] + 1
    record = {
        "digest": _otp_digest(email, code),
        "expires_at": now + config.OTP_TTL_SECONDS,
        "attempts": 0,
        "last_sent": now,
        "window_started": started,
        "request_count": count,
        "signup": signup,
    }
    if persistence.enabled():
        persistence.state_set(f"otp:{email}", record)
    else:
        _pending_otps[email] = record
    return code


def _authenticated_identity() -> Tuple[str, str]:
    identity = _authenticated_customer()
    if identity is None:
        raise RuntimeError("The request has no authenticated customer")
    return identity


@app.get("/")
def index():
    if _authenticated_customer() is None:
        return redirect(url_for("login_page"))
    return send_from_directory(config.BASE_DIR, "dashboard.html")


@app.get("/login")
def login_page():
    if _authenticated_customer() is not None:
        return redirect(url_for("index"))
    response = send_from_directory(config.BASE_DIR, "login.html")
    # Login markup and its JavaScript must not remain cached after an update.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/status")
def status():
    return jsonify({**llm_module.groq_status(), "store": config.STORE_NAME, "today": config.TODAY.isoformat()})


@app.post("/api/auth/request-otp")
def request_otp():
    data = _request_data()
    email = _normalize_email(data.get("email"))
    if email is None:
        return jsonify({"error": "Enter a valid email address."}), 400
    intent = str(data.get("intent") or "login").strip().lower()
    if intent not in {"login", "signup"}:
        return jsonify({"error": "Choose sign in or create account."}), 400
    signup = None
    if intent == "signup":
        first_name, last_name = _clean_name(data.get("first_name")), _clean_name(data.get("last_name"))
        if not first_name or not last_name:
            return jsonify({"error": "Enter a valid first and last name."}), 400
        signup = {"first_name": first_name, "last_name": last_name}
    try:
        known_account = email in _customer_accounts()
    except (OSError, ValueError):
        app.logger.exception("Unable to load customer account mappings")
        return jsonify({"error": "Customer accounts are unavailable. Check customer_accounts.csv."}), 500
    if config.OTP_DEVELOPMENT_MODE and intent == "login" and not known_account:
        return jsonify({"error": "No account is registered with this email. Create an account first."}), 404
    if config.OTP_DEVELOPMENT_MODE and intent == "signup" and known_account:
        return jsonify({"error": "This email already has an account. Try signing in instead."}), 409
    admin_email = _normalize_email(config.ADMIN_LOGIN_EMAIL)
    admin_shortcut = bool(
        intent == "login" and known_account and admin_email == email
        and re.fullmatch(r"\d{6}", config.ADMIN_LOGIN_OTP)
    )
    client_ip = request.remote_addr or ""
    if config.OTP_DEVELOPMENT_MODE:
        try:
            is_loopback = ipaddress.ip_address(client_ip).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            return jsonify({"error": "Development sign-in codes are available only from this computer."}), 403
    else:
        if not admin_shortcut and not mailer.smtp_is_configured():
            return jsonify({"error": "Email delivery is not configured on this server."}), 503

    now = time.time() if persistence.enabled() else time.monotonic()
    with _auth_lock:
        allowed, retry_after = _allow_otp_request(email, client_ip, now)
        if not allowed:
            return jsonify({"error": f"Please wait {retry_after} seconds before requesting another code.",
                            "resend_in": retry_after}), 429
        if admin_shortcut and not config.OTP_DEVELOPMENT_MODE:
            return jsonify({
                "dev_mode": False,
                "message": "Enter the administrator sign-in code.",
                "resend_in": config.OTP_RESEND_SECONDS,
            })
        # Do not reveal whether an email is linked to an account in SMTP mode.
        should_send = (intent == "signup") != known_account
        if not config.OTP_DEVELOPMENT_MODE and not should_send:
            return jsonify({
                "dev_mode": False,
                "message": "If the request is eligible, a sign-in code has been sent.",
                "resend_in": config.OTP_RESEND_SECONDS,
            })
        code = _create_otp(email, now, signup=signup)

    if not config.OTP_DEVELOPMENT_MODE:
        try:
            mailer.send_otp_email(email, code)
        except Exception:
            with _auth_lock:
                if persistence.enabled():
                    persistence.state_delete(f"otp:{email}")
                else:
                    _pending_otps.pop(email, None)
            app.logger.exception("OTP email delivery failed for %s", _masked_email(email))
            return jsonify({"error": "Could not send the sign-in code. Check SMTP settings and try again."}), 503
        app.logger.info("Sent a sign-in code to %s", _masked_email(email))
        return jsonify({
            "dev_mode": False,
            "message": "If the request is eligible, a sign-in code has been sent.",
            "resend_in": config.OTP_RESEND_SECONDS,
        })

    app.logger.info("Issued a local development OTP for %s", _masked_email(email))
    return jsonify({
        "dev_mode": True,
        "dev_otp": code,
        "resend_in": config.OTP_RESEND_SECONDS,
    })


@app.post("/api/auth/verify-otp")
def verify_otp():
    data = _request_data()
    email = _normalize_email(data.get("email"))
    code = str(data.get("code") or "").strip()
    if email is None or not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "Enter the email address and its 6 digit code."}), 400

    admin_email = _normalize_email(config.ADMIN_LOGIN_EMAIL)
    is_admin_address = bool(admin_email and email == admin_email and re.fullmatch(r"\d{6}", config.ADMIN_LOGIN_OTP))
    admin_login = False
    signup = None
    now = time.time() if persistence.enabled() else time.monotonic()
    with _auth_lock:
        admin_state_key = f"admin-otp-failures:{email}"
        if is_admin_address:
            failures = (persistence.state_get(admin_state_key, {}) if persistence.enabled()
                        else _admin_otp_failures.get(email, {}))
            if failures.get("blocked_until", 0) > now:
                return jsonify({"error": "Too many administrator code attempts. Try again later."}), 429
            if hmac.compare_digest(code, config.ADMIN_LOGIN_OTP):
                admin_login = True
                if persistence.enabled():
                    persistence.state_delete(admin_state_key)
                else:
                    _admin_otp_failures.pop(email, None)

        if admin_login:
            if persistence.enabled():
                persistence.state_delete(f"otp:{email}")
            else:
                _pending_otps.pop(email, None)
        else:
            record = persistence.state_get(f"otp:{email}") if persistence.enabled() else _pending_otps.get(email)
            if not record or now > record["expires_at"]:
                if persistence.enabled():
                    persistence.state_delete(f"otp:{email}")
                else:
                    _pending_otps.pop(email, None)
                if is_admin_address:
                    failures = failures if failures.get("window_started", now) + 900 > now else {"attempts": 0, "window_started": now}
                    failures["attempts"] = failures.get("attempts", 0) + 1
                    if failures["attempts"] >= 5:
                        failures["blocked_until"] = now + 900
                    if persistence.enabled():
                        persistence.state_set(admin_state_key, failures)
                    else:
                        _admin_otp_failures[email] = failures
                return jsonify({"error": "That code has expired. Request a new one."}), 401
            record["attempts"] += 1
            if record["attempts"] > config.OTP_MAX_ATTEMPTS:
                if persistence.enabled():
                    persistence.state_delete(f"otp:{email}")
                else:
                    _pending_otps.pop(email, None)
                return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429
            if not hmac.compare_digest(record["digest"], _otp_digest(email, code)):
                if is_admin_address:
                    failures = (persistence.state_get(admin_state_key, {}) if persistence.enabled()
                                else _admin_otp_failures.get(email, {}))
                    failures = failures if failures.get("window_started", now) + 900 > now else {"attempts": 0, "window_started": now}
                    failures["attempts"] = failures.get("attempts", 0) + 1
                    if failures["attempts"] >= 5:
                        failures["blocked_until"] = now + 900
                        if persistence.enabled():
                            persistence.state_delete(f"otp:{email}")
                        else:
                            _pending_otps.pop(email, None)
                    if persistence.enabled():
                        persistence.state_set(admin_state_key, failures)
                    else:
                        _admin_otp_failures[email] = failures
                    if failures.get("blocked_until", 0) > now:
                        return jsonify({"error": "Too many administrator code attempts. Try again in 15 minutes."}), 429
                if record["attempts"] >= config.OTP_MAX_ATTEMPTS:
                    if persistence.enabled():
                        persistence.state_delete(f"otp:{email}")
                    else:
                        _pending_otps.pop(email, None)
                    return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429
                if persistence.enabled():
                    persistence.state_set(f"otp:{email}", record)
                return jsonify({"error": "That code is not correct. Try again."}), 401
            if persistence.enabled():
                persistence.state_delete(f"otp:{email}")
            else:
                _pending_otps.pop(email, None)
            signup = record.get("signup")

    try:
        customer_id = _customer_accounts().get(email)
        if admin_login and customer_id is None:
            return jsonify({"error": "Administrator email must have a registered account before using the admin code."}), 403
        if signup and customer_id is None:
            customer_id = _register_customer_account(email, signup["first_name"], signup["last_name"])
        elif signup and customer_id is not None:
            return jsonify({"error": "This email may already be registered. Try signing in."}), 409
    except (OSError, ValueError):
        app.logger.exception("Unable to load customer account mappings")
        return jsonify({"error": "Customer accounts are unavailable. Check customer_accounts.csv."}), 500
    except FileExistsError:
        return jsonify({"error": "This email may already be registered. Try signing in."}), 409
    if customer_id is None:
        return jsonify({
            "error": "Email verified, but it is not linked to a customer ID. Add the mapping to "
                     "data/customer_accounts.csv and request a new code."
        }), 403

    session.clear()
    session["email"] = email
    session["customer_id"] = customer_id
    session["chat_id"] = uuid.uuid4().hex
    return jsonify({"ok": True})


@app.post("/api/auth/logout")
def logout():
    chat_id = session.get("chat_id")
    if chat_id:
        _histories.pop(chat_id, None)
        if persistence.enabled():
            persistence.state_delete(f"chat:{chat_id}")
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@_login_required
def me():
    email, customer_id = _authenticated_identity()
    customer = shop.get_customer(customer_id)
    if customer is None:
        return jsonify({"error": "The linked customer record is unavailable."}), 500
    return jsonify({
        "customer": {
            "customer_id": customer_id,
            "name": customer["name"],
            "first_name": customer.get("first_name", ""),
            "last_name": customer.get("last_name", ""),
            "city": customer.get("city", ""),
        },
        "llm": llm_module.groq_status(),
    })


@app.get("/api/orders")
@_login_required
def orders():
    _, customer_id = _authenticated_identity()
    result = shop.list_my_orders(customer_id)
    if "error" in result:
        return jsonify(result), 500
    return jsonify({"orders": result["orders"], "order_count": result["order_count"]})


@app.get("/api/products")
@_login_required
def products():
    try:
        result = shop.search_products(
            query=request.args.get("q"),
            category=request.args.get("category"),
            brand=request.args.get("brand"),
            max_price=request.args.get("max_price"),
            min_rating=request.args.get("min_rating"),
            in_stock_only=request.args.get("in_stock", "false"),
            spec_keywords=request.args.get("keywords"),
            sort_by=request.args.get("sort", "rating_desc"),
            limit=20,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if "error" in result:
        return jsonify(result), 400
    return jsonify({**result, "catalogue": shop.list_categories()["categories"]})


@app.get("/api/compare")
@_login_required
def compare():
    refs = [value.strip() for value in request.args.get("ids", "").split(",") if value.strip()]
    result = shop.compare_products(refs)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/policy")
@_login_required
def policy():
    result = shop.get_return_policy()
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.get("/api/returns")
@_login_required
def returns():
    _, customer_id = _authenticated_identity()
    listed = shop.list_my_orders(customer_id)
    if "error" in listed:
        return jsonify(listed), 500
    matching = [
        order for order in listed["orders"]
        if order["return_status"] != "None" or order["payment_status"] == "Refunded"
    ]
    items = []
    for order in matching:
        refund = shop.get_refund_details(order["order_id"], customer_id)
        items.append({
            **order,
            "refund": refund,
        })
    return jsonify({"returns": items})


@app.get("/api/orders/<order_id>/eligibility")
@_login_required
def order_eligibility(order_id: str):
    _, customer_id = _authenticated_identity()
    result = shop.check_return_eligibility(order_id, customer_id, request.args.get("reason"))
    return jsonify(result)


@app.post("/api/orders/<order_id>/return")
@_login_required
def create_return(order_id: str):
    _, customer_id = _authenticated_identity()
    reason = _request_data().get("reason")
    result = shop.initiate_return(order_id, customer_id, reason)
    if result.get("error"):
        status_code = 409 if result["error"] in {"DATA_UNAVAILABLE", "ORDER_NOT_FOUND"} else 400
        return jsonify(result), status_code
    return jsonify(result)


@app.post("/api/demo/reset-returns")
@_login_required
def reset_demo_returns():
    _, customer_id = _authenticated_identity()
    shop.reset_runtime_state(customer_id)
    return jsonify({"ok": True})


@app.post("/api/chat")
@_login_required
def chat():
    data = _request_data()
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "Please type a message."}), 400
    if len(message) > 1000:
        return jsonify({"error": "Message is too long (max 1000 characters)."}), 400

    _, customer_id = _authenticated_identity()
    chat_id = session.setdefault("chat_id", uuid.uuid4().hex)
    with _lock:
        if persistence.enabled():
            history = persistence.state_get(f"chat:{chat_id}", [])
        else:
            history = _histories.setdefault(chat_id, [])
        try:
            result = agent.chat(message, customer_id=customer_id, history=history)
        except LLMUnavailable as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:
            app.logger.exception("Unexpected assistant error")
            return jsonify({"error": f"Unexpected error: {type(exc).__name__}: {exc}"}), 500
        history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": result.reply}])
        del history[:-2 * config.HISTORY_TURNS]
        if persistence.enabled():
            persistence.state_set(f"chat:{chat_id}", history)
    return jsonify(result.to_dict())


@app.post("/api/chat/reset")
@_login_required
def reset_chat():
    chat_id = session.get("chat_id")
    if chat_id:
        _histories.pop(chat_id, None)
        if persistence.enabled():
            persistence.state_delete(f"chat:{chat_id}")
    session["chat_id"] = uuid.uuid4().hex
    return jsonify({"ok": True})


@app.get("/api/evaluation")
@_login_required
def evaluation():
    runs = [
        path for path in config.RESULTS_DIR.iterdir()
        if path.is_dir() and (path / "metrics.json").is_file()
    ] if config.RESULTS_DIR.exists() else []
    if not runs:
        return jsonify({"run": None, "metrics": None, "failures": []})
    latest = max(runs, key=lambda path: (path / "metrics.json").stat().st_mtime)
    try:
        import json

        metrics = json.loads((latest / "metrics.json").read_text(encoding="utf-8"))
        failures = []
        results_file = latest / "test_results.csv"
        if results_file.is_file():
            with open(results_file, newline="", encoding="utf-8") as source:
                for row in csv.DictReader(source):
                    if row.get("task_completed") == "FAIL" or row.get("tool_selection") == "FAIL" \
                            or row.get("constraint_policy_compliance") == "FAIL":
                        failures.append({
                            "id": row.get("test_id", ""),
                            "request": row.get("request", ""),
                            "notes": row.get("notes", ""),
                        })
        return jsonify({"run": latest.name, "metrics": metrics, "failures": failures})
    except (OSError, ValueError, csv.Error):
        app.logger.exception("Unable to read evaluation results")
        return jsonify({"error": "Evaluation results could not be read."}), 500


if __name__ == "__main__":
    info = llm_module.groq_status()
    print(f"\n{config.STORE_NAME} assistant | simulated date {config.TODAY}")
    print(info["message"])
    print(f"Open http://{config.HOST}:{config.PORT}/login in your browser\n")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
