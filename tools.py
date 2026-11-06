"""
tools.py
========
The business logic of the shop, written as PLAIN PYTHON FUNCTIONS.

No LLM and no LangChain in this file. That is deliberate (Phase 2 of the brief:
"implement and test the product search, filtering, order lookup and policy
functions independently"). agent.py wraps these functions so the LLM can call
them, and tests/test_tools.py tests them directly.

Every function:
  * takes simple arguments (strings / numbers / lists),
  * is forgiving about messy input, because small LLMs often send things like
    "50,000", "None", "laptops" or "5G, AMOLED",
  * returns a JSON-friendly dict. Problems come back as
        {"error": "<CODE>", "message": "<human readable text>"}
    instead of raising, so the agent can read the error and try again.

Product discovery tools
    search_products, get_product_details, compare_products, list_categories
Customer service tools
    list_my_orders, get_order_status, get_return_policy,
    check_return_eligibility, get_refund_details, initiate_return
"""
from __future__ import annotations

import csv
import difflib
import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import config
import persistence

# =============================================================================
# 1. DATA LOADING
# =============================================================================


def _read_csv(name: str) -> List[Dict[str, str]]:
    path = config.CUSTOMERS_FILE if name == "customers.csv" else config.DATA_DIR / name
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_date(text: Optional[str]) -> Optional[date]:
    text = (text or "").strip()
    return date.fromisoformat(text) if text else None


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


@lru_cache(maxsize=1)
def load_products() -> Tuple[Dict[str, Any], ...]:
    """products.csv as a tuple of dicts with proper number types."""
    rows = []
    for r in _read_csv("products.csv"):
        rows.append({
            "product_id": r["product_id"].strip(),
            "product_name": r["product_name"].strip(),
            "category": r["category"].strip(),
            "price": int(r["price"]),
            "brand": r["brand"].strip(),
            "rating": float(r["rating"]),
            "stock": int(r["stock"]),
            "specifications": r["specifications"].strip(),
        })
    return tuple(rows)


@lru_cache(maxsize=1)
def load_orders() -> Tuple[Dict[str, Any], ...]:
    """Load orders using only fields present in the assignment CSV."""
    rows = []
    for r in _read_csv("orders.csv"):
        order_id = r["order_id"].strip()
        rows.append({
            "order_id": order_id,
            "customer_id": r["customer_id"].strip(),
            "product_id": r["product_id"].strip(),
            "order_date": _to_date(r["order_date"]),
            "order_status": r["order_status"].strip(),
            "expected": _to_date(r["expected_delivery_date"]),
            "payment_status": r["payment_status"].strip(),
            "quantity": None,
            "unit_price": None,
            "delivered": None,
            "payment_method": None,
            "return_request_date": None,
        })
    return tuple(rows)


@lru_cache(maxsize=1)
def load_policy() -> Dict[str, Dict[str, Any]]:
    """Load explicit policies and synthesize a clearly generic fallback per new category."""
    categories = set(_valid_categories())
    out = {}
    for r in _read_csv("returns_policy.csv"):
        category = r["category"].strip()
        if category not in categories:
            continue
        out[category] = {
            "category": category,
            "return_window_days": int(r["return_window_days"]),
            "defect_window_days": int(r["defect_window_days"]),
            "refund_method": r["refund_method"].strip(),
            "refund_processing_days": int(r["refund_processing_days"]),
            "conditions": r["conditions"].strip(),
        }
    for category in categories:
        out.setdefault(category, {
            "category": category,
            "return_window_days": 7,
            "defect_window_days": 30,
            "refund_method": "Original payment method",
            "refund_processing_days": 5,
            "conditions": "Synthetic demo policy: original packaging and accessories must be included",
        })
    return out


@lru_cache(maxsize=1)
def load_customers() -> Dict[str, Dict[str, str]]:
    """Load every customer profile, plus ID-only entries for order customers."""
    order_customer_ids = {row["customer_id"] for row in load_orders()}
    customers = {
        r["customer_id"].strip(): {
            "customer_id": r["customer_id"].strip(),
            "first_name": r.get("first_name", "").strip(),
            "last_name": r.get("last_name", "").strip(),
            "name": " ".join(filter(None, (
                r.get("first_name", "").strip(), r.get("last_name", "").strip()
            ))) or r.get("name", "").strip() or r["customer_id"].strip(),
            "city": r["city"].strip(),
        }
        for r in _read_csv("customers.csv")
        if r["customer_id"].strip()
    }
    for customer_id in order_customer_ids:
        customers.setdefault(customer_id, {"customer_id": customer_id, "name": customer_id})
    if persistence.enabled():
        customers.update(persistence.customers())
    return customers


def reload_data() -> None:
    """Forget cached CSV data (used by tests)."""
    for fn in (load_products, load_orders, load_policy, load_customers):
        fn.cache_clear()


# Return requests created while the program runs (initiate_return). They live
# in memory only, so the CSV files are never modified and tests stay repeatable.
_RETURNS_CREATED: Dict[str, Dict[str, Any]] = {}


def reset_runtime_state(customer_id: Any = None) -> None:
    cid = _norm_customer(customer_id)
    if cid is None:
        _RETURNS_CREATED.clear()
        if persistence.enabled():
            # Demo reset is intentionally scoped to this app's test return table.
            persistence.initialize()
            from persistence import _connect
            with _connect() as conn:
                conn.execute("DELETE FROM return_requests")
        return
    for order_id in [
        order_id for order_id, request in _RETURNS_CREATED.items()
        if request["customer_id"] == cid
    ]:
        del _RETURNS_CREATED[order_id]
    if persistence.enabled():
        persistence.initialize()
        from persistence import _connect
        with _connect() as conn:
            conn.execute("DELETE FROM return_requests WHERE customer_id=%s", (cid,))


def get_created_returns() -> Dict[str, Dict[str, Any]]:
    return persistence.returns() if persistence.enabled() else dict(_RETURNS_CREATED)


def list_customers() -> List[Dict[str, str]]:
    customers = load_customers()
    return [customers[customer_id] for customer_id in sorted(customers)]


def get_customer(customer_id: Optional[str]) -> Optional[Dict[str, str]]:
    return load_customers().get(_norm_customer(customer_id) or "")


# General policy rules that apply to every category (the per-category numbers
# are in data/returns_policy.csv).
GENERAL_POLICY = {
    "how_the_window_is_counted": "The return window is counted from the delivered date and includes the last day.",
    "change_of_mind": "Change-of-mind returns must be raised within the category's return window.",
    "defective_damaged_wrong_item": "Defective, damaged or wrong items can be returned within the longer defect window of the category.",
    "not_yet_delivered": "An order that has not been delivered cannot be returned. Returns open after delivery.",
    "refund_destination": ("Refunds go to the original payment method. For Cash on Delivery orders the refund is "
                           "sent by bank transfer after the customer shares bank details."),
    "refund_timing": ("The refund is issued within the category's refund processing days (business days) after the "
                      "returned item is received and passes the quality check."),
    "pickup": "After a return request is created, pickup is arranged within 2 business days.",
    "cancellations": "Orders cancelled before shipping are refunded in full within 3 business days.",
    "failed_or_pending_payments": ("If a payment failed or is pending, nothing is charged. If the bank shows a debit "
                                   "anyway, it is reversed automatically within 5 to 7 business days."),
    "unsupported_actions": ("This assistant cannot cancel orders, change delivery addresses or edit payments. "
                            "Customers must contact human customer support for those."),
}

# =============================================================================
# 2. SMALL HELPERS
# =============================================================================

_NULLS = {"", "none", "null", "nil", "n/a", "na", "any", "all", "undefined", "unspecified"}


class ArgError(ValueError):
    """Raised internally for a bad argument; turned into an INVALID_ARGUMENT result."""


def _err(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": code, "message": message, **extra}


def _clean_str(value: Any) -> Optional[str]:
    """None for empty / 'null' / 'none' style values, else the stripped text."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NULLS else text


def _num(value: Any, name: str) -> Optional[float]:
    """Parse numbers the way people write them: 50000, '50,000', '₹50k', '1 lakh'."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ArgError(f"'{name}' must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_str(value)
    if text is None:
        return None
    t = text.lower()
    t = re.sub(r"^\s*(?:under|below|less than|up to|at most|maximum|max)\s*", "", t)
    for junk in ("₹", "inr", "rupees", "rupee", "rs.", "rs", ",", "_"):
        t = t.replace(junk, "")
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(k|lakhs?|lacs?|crores?)?\s*", t)
    if not m:
        raise ArgError(f"'{name}' must be a number, got {value!r}")
    amount = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "k":
        amount *= 1_000
    elif unit.startswith(("lakh", "lac")):
        amount *= 100_000
    elif unit.startswith("crore"):
        amount *= 10_000_000
    return amount


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "yes", "y", "1", "t"}


def _str_list(value: Any) -> List[str]:
    """Accept a list or a comma separated string; drop empty items."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;|]", str(value))
    out = []
    for item in items:
        s = _clean_str(item)
        if s:
            out.append(s)
    return out


def inr(amount: float) -> str:
    """Format rupees the Indian way: 119999 -> ₹1,19,999."""
    n = int(round(amount))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + "₹" + s


def add_business_days(start: date, days: int) -> date:
    """Add working days (Mon to Fri). Public holidays are ignored."""
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


# =============================================================================
# 3. PRODUCT DISCOVERY TOOLS
# =============================================================================

_CATEGORY_ALIASES = {
    "laptop": "Laptop", "laptops": "Laptop", "notebook": "Laptop", "notebooks": "Laptop",
    "ultrabook": "Laptop", "computer": "Laptop", "computers": "Laptop",
    "smartphone": "Smartphone", "smartphones": "Smartphone", "phone": "Smartphone", "phones": "Smartphone",
    "mobile": "Smartphone", "mobiles": "Smartphone", "mobilephone": "Smartphone", "mobilephones": "Smartphone",
    "cellphone": "Smartphone", "cellphones": "Smartphone",
    "headphone": "Headphones", "headphones": "Headphones", "earphone": "Headphones", "earphones": "Headphones",
    "earbud": "Headphones", "earbuds": "Headphones", "headset": "Headphones", "headsets": "Headphones",
    "neckband": "Headphones", "neckbands": "Headphones", "audio": "Headphones",
    "smartwatch": "Smartwatch", "smartwatches": "Smartwatch", "watch": "Smartwatch", "watches": "Smartwatch",
    "wearable": "Smartwatch", "wearables": "Smartwatch", "fitnessband": "Smartwatch", "band": "Smartwatch",
    "tablet": "Tablet", "tablets": "Tablet", "tab": "Tablet", "tabs": "Tablet", "ipad": "Tablet",
}

_CATEGORY_EQUIVALENTS = {
    "headphone": {"audio"},
    "smartwatch": {"wearable"},
}

_SORT_ALIASES = {
    "price_asc": "price_asc", "price_desc": "price_desc", "rating_desc": "rating_desc",
    "price": "price_asc", "cheapest": "price_asc", "lowest": "price_asc", "low": "price_asc",
    "asc": "price_asc", "ascending": "price_asc", "price_low_to_high": "price_asc",
    "most_expensive": "price_desc", "highest_price": "price_desc", "desc": "price_desc",
    "price_high_to_low": "price_desc",
    "rating": "rating_desc", "best": "rating_desc", "top_rated": "rating_desc", "highest_rated": "rating_desc",
    "relevance": "rating_desc", "popularity": "rating_desc",
}

_KEYWORD_SYNONYMS = {
    "anc": "noise cancellation", "noise cancelling": "noise cancellation", "noise canceling": "noise cancellation",
    "wifi": "wi-fi", "fast charge": "fast charging", "water proof": "water resistance",
}

_QUERY_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "without", "me", "my", "i",
    "show", "find", "want", "need", "looking", "look", "buy", "get", "give", "recommend", "suggest", "please",
    "best", "good", "great", "cheap", "cheapest", "top", "under", "below", "above", "over", "than", "less", "more",
    "least", "most", "price", "priced", "budget", "rs", "inr", "rupees", "any", "some", "new", "latest", "is",
    "are", "do", "you", "have", "which", "what", "that", "this", "it", "one", "ones", "product", "products",
    "item", "items", "available", "stock", "instock", "rated", "rating", "ratings", "high", "low", "range",
}


def _canon_category(value: Any) -> Optional[str]:
    s = _clean_str(value)
    if s is None:
        return None
    key = re.sub(r"[^a-z]", "", s.lower())
    categories = _valid_categories()
    for category in categories:
        category_key = re.sub(r"[^a-z]", "", category.lower())
        if key == category_key or key.rstrip("s") == category_key.rstrip("s"):
            return category
    alias = _CATEGORY_ALIASES.get(key) or _CATEGORY_ALIASES.get(key.rstrip("s"))
    if alias:
        alias_key = re.sub(r"[^a-z]", "", alias.lower())
        equivalent_keys = _CATEGORY_EQUIVALENTS.get(alias_key.rstrip("s"), set())
        target_keys = {alias_key.rstrip("s"), *equivalent_keys}
        return next((category for category in categories
                     if re.sub(r"[^a-z]", "", category.lower()).rstrip("s") in target_keys), None)
    return None


def _valid_categories() -> List[str]:
    return sorted({p["category"] for p in load_products()})


def _canon_brand(value: Any) -> Optional[str]:
    s = _clean_str(value)
    if s is None:
        return None
    low = s.lower()
    brands = {p["brand"] for p in load_products()}
    for b in brands:
        if b.lower() == low:
            return b
    for b in brands:
        if b.lower().startswith(low) or low.startswith(b.lower()):
            return b
    return None


def _keyword_pattern(keyword: str) -> Optional["re.Pattern[str]"]:
    """
    Build a safe pattern for a spec keyword.

    '8 GB' must match '8 GB RAM' but NOT '128 GB SSD', so the match cannot start
    in the middle of a number or word. Spaces and hyphens are optional, so
    '16GB', '16 GB' and 'wifi' / 'Wi-Fi' all work.
    """
    kw = keyword.lower().strip()
    kw = _KEYWORD_SYNONYMS.get(kw, kw)
    tokens = re.findall(r"\d+(?:\.\d+)?|[a-z]+", kw)
    if not tokens:
        return None
    return re.compile(r"(?<![\w.])" + r"[\s\-]*".join(re.escape(t) for t in tokens) + r"(?!\w)")


def _spec_has(spec_text: str, keyword: str) -> bool:
    pattern = _keyword_pattern(keyword)
    return bool(pattern and pattern.search(spec_text.lower()))


def _ram_gb(spec_text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*GB\s*RAM", spec_text, re.I)
    return int(m.group(1)) if m else None


def _storage_gb(spec_text: str) -> Optional[int]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|TB)\s*(?:SSD|storage)", spec_text, re.I)
    if not m:
        return None
    size = float(m.group(1))
    return int(size * 1024) if m.group(2).upper() == "TB" else int(size)


def _public_product(p: Dict[str, Any]) -> Dict[str, Any]:
    """The view of a product that is shown to the LLM and the customer."""
    return {
        "product_id": p["product_id"],
        "product_name": p["product_name"],
        "category": p["category"],
        "brand": p["brand"],
        "price_inr": p["price"],
        "price": inr(p["price"]),
        "rating": p["rating"],
        "stock": p["stock"],
        "availability": f"In stock ({p['stock']} units)" if p["stock"] > 0 else "Out of stock",
        "specifications": p["specifications"],
    }


def search_products(
    query: Optional[str] = None,
    category: Optional[str] = None,
    brand: Optional[str] = None,
    min_price: Any = None,
    max_price: Any = None,
    min_rating: Any = None,
    in_stock_only: Any = False,
    spec_keywords: Any = None,
    min_ram_gb: Any = None,
    min_storage_gb: Any = None,
    sort_by: Optional[str] = None,
    limit: Any = 10,
) -> Dict[str, Any]:
    """Filter the catalogue. Every filter is optional; all given filters must match (AND)."""
    try:
        cat_in = _clean_str(category)
        cat = _canon_category(cat_in) if cat_in else None
        if cat_in and not cat:
            return _err("UNKNOWN_CATEGORY", f"'{cat_in}' is not a category we sell.",
                        valid_categories=_valid_categories())
        brand_in = _clean_str(brand)
        br = _canon_brand(brand_in) if brand_in else None
        if brand_in and not br:
            return _err("UNKNOWN_BRAND", f"'{brand_in}' is not a brand we sell.",
                        valid_brands=sorted({p["brand"] for p in load_products()}))
        lo, hi = _num(min_price, "min_price"), _num(max_price, "max_price")
        rating_floor = _num(min_rating, "min_rating")
        ram_floor, storage_floor = _num(min_ram_gb, "min_ram_gb"), _num(min_storage_gb, "min_storage_gb")
        lim = _num(limit, "limit")
        lim = 10 if lim is None else int(lim)
        lim = max(1, min(lim, 20))
        if rating_floor is not None and not (0 <= rating_floor <= 5):
            raise ArgError("'min_rating' must be between 0 and 5")
    except ArgError as exc:
        return _err("INVALID_ARGUMENT", str(exc))

    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    only_in_stock = _bool(in_stock_only)
    keywords = _str_list(spec_keywords)
    sort_key = re.sub(r"[^a-z_]", "", (_clean_str(sort_by) or "rating_desc").lower().replace(" ", "_"))
    sort_key = _SORT_ALIASES.get(sort_key, "rating_desc")

    query_text = _clean_str(query) or ""
    query_tokens = []
    for tok in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", query_text.lower()):
        if tok in _QUERY_STOPWORDS or (tok.isdigit() and len(tok) >= 4):
            continue  # filler words and prices are not product words
        query_tokens.append(tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok)

    matches = []
    for p in load_products():
        if cat and p["category"] != cat:
            continue
        if br and p["brand"] != br:
            continue
        if lo is not None and p["price"] < lo:
            continue
        if hi is not None and p["price"] > hi:
            continue
        if rating_floor is not None and p["rating"] < rating_floor:
            continue
        if only_in_stock and p["stock"] <= 0:
            continue
        if ram_floor is not None:
            ram = _ram_gb(p["specifications"])
            if ram is None or ram < ram_floor:
                continue
        if storage_floor is not None:
            storage = _storage_gb(p["specifications"])
            if storage is None or storage < storage_floor:
                continue
        if keywords and not all(_spec_has(p["specifications"], k) for k in keywords):
            continue
        if query_tokens:
            haystack = " ".join([p["product_name"], p["brand"], p["category"], p["specifications"]]).lower()
            if not all(tok in haystack for tok in query_tokens):
                continue
        matches.append(p)

    if sort_key == "price_asc":
        matches.sort(key=lambda p: (p["price"], -p["rating"]))
    elif sort_key == "price_desc":
        matches.sort(key=lambda p: (-p["price"], -p["rating"]))
    else:
        matches.sort(key=lambda p: (-p["rating"], p["price"]))

    applied = {k: v for k, v in {
        "query": query_text or None, "category": cat, "brand": br, "min_price": lo, "max_price": hi,
        "min_rating": rating_floor, "in_stock_only": only_in_stock or None,
        "spec_keywords": keywords or None, "min_ram_gb": ram_floor, "min_storage_gb": storage_floor,
        "sort_by": sort_key,
    }.items() if v is not None}

    result: Dict[str, Any] = {
        "total_matches": len(matches),
        "showing": min(len(matches), lim),
        "filters_applied": applied,
        "products": [_public_product(p) for p in matches[:lim]],
    }
    if not matches:
        result["message"] = "No products match the given filters."
        result["suggestion"] = "Try a higher budget, fewer spec keywords, or a different category or brand."
        if cat:
            prices = [p["price"] for p in load_products() if p["category"] == cat]
            result["category_price_range"] = {
                "category": cat, "cheapest": inr(min(prices)), "most_expensive": inr(max(prices)),
                "products_in_category": len(prices)}
    elif query_text and not query_tokens:
        result["note"] = "The query had only filler words, so it was ignored. Results use the other filters."
    return result


def _resolve_product(ref: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Find one product by id ('P111') or by (part of) its name. Returns (product, error)."""
    text = _clean_str(ref)
    if text is None:
        return None, _err("INVALID_ARGUMENT", "A product id or product name is required.")
    products = load_products()
    low = text.lower()

    for p in products:                       # 1. exact id
        if p["product_id"].lower() == low:
            return p, None
    for p in products:                       # 2. exact name
        if p["product_name"].lower() == low:
            return p, None
    inside = [p for p in products if p["product_name"].lower() in low]   # 3. a full name appears in the text
    if inside:
        best = max(inside, key=lambda p: len(p["product_name"]))
        return best, None
    partial = [p for p in products if low in p["product_name"].lower()]  # 4. text is part of a name
    if not partial:
        tokens = re.findall(r"[a-z0-9.]+", low)                          # 5. all words appear in a name
        if tokens:
            partial = [p for p in products if all(t in p["product_name"].lower() for t in tokens)]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, _err("AMBIGUOUS_PRODUCT", f"'{text}' matches several products. Ask the customer which one.",
                          matches=[{"product_id": p["product_id"], "product_name": p["product_name"]} for p in partial[:8]])
    close = difflib.get_close_matches(low, [p["product_name"].lower() for p in products], n=3, cutoff=0.5)
    suggestions = [p["product_name"] for p in products if p["product_name"].lower() in close]
    return None, _err("PRODUCT_NOT_FOUND", f"No product found for '{text}'.", did_you_mean=suggestions)


def get_product_details(product: Any = None) -> Dict[str, Any]:
    """Full details of one product, found by product id (P111) or by name."""
    p, error = _resolve_product(product)
    if error:
        return error
    return _public_product(p)


def compare_products(products: Any = None) -> Dict[str, Any]:
    """Side by side data for 2 to 4 products (ids or names), plus simple highlights."""
    refs = _str_list(products)
    if len(refs) < 2:
        return _err("INVALID_ARGUMENT", "Give at least two product ids or names to compare.")
    if len(refs) > 4:
        return _err("INVALID_ARGUMENT", "Compare at most four products at a time.")
    found, problems = [], []
    for ref in refs:
        p, error = _resolve_product(ref)
        if error:
            problems.append({"requested": ref, **error})
        elif p not in found:
            found.append(p)
    if problems:
        return _err("PRODUCT_NOT_FOUND", "Some products could not be identified.", problems=problems)
    if len(found) < 2:
        return _err("INVALID_ARGUMENT", "These names point to the same product. Give two different products.")
    if len({p["category"] for p in found}) != 1:
        return _err(
            "MIXED_CATEGORIES",
            "Choose products from the same category to compare their price, rating, availability, and specifications.",
        )
    cheapest = min(found, key=lambda p: p["price"])
    best = max(found, key=lambda p: p["rating"])
    return {
        "products": [_public_product(p) for p in found],
        "highlights": {
            "cheapest": f"{cheapest['product_name']} ({inr(cheapest['price'])})",
            "highest_rated": f"{best['product_name']} ({best['rating']})",
            "out_of_stock": [p["product_name"] for p in found if p["stock"] <= 0],
        },
    }


def list_categories() -> Dict[str, Any]:
    """What the store sells: categories with counts, price range and brands."""
    out = []
    for cat in _valid_categories():
        items = [p for p in load_products() if p["category"] == cat]
        prices = [p["price"] for p in items]
        out.append({
            "category": cat,
            "products": len(items),
            "in_stock": sum(1 for p in items if p["stock"] > 0),
            "cheapest": inr(min(prices)),
            "most_expensive": inr(max(prices)),
            "brands": sorted({p["brand"] for p in items}),
        })
    return {"store": config.STORE_NAME, "categories": out}


# =============================================================================
# 4. CUSTOMER SERVICE TOOLS
# =============================================================================

_RETURN_OK_STATES = ("Delivered",)


def _norm_customer(customer_id: Any) -> Optional[str]:
    s = _clean_str(customer_id)
    return s.upper() if s else None


def _norm_order_id(order_id: Any) -> Optional[str]:
    s = _clean_str(order_id)
    if not s:
        return None
    s = s.upper().replace("#", "").replace(" ", "")
    digits = s[1:] if s.startswith("O") else s
    return "O" + digits if digits.isdigit() else s


def _product_by_id(product_id: str) -> Dict[str, Any]:
    return next(p for p in load_products() if p["product_id"] == product_id)


def _owned_order(order_id: Any, customer_id: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Look up an order for the signed-in customer.

    Security rule: a customer can only see their OWN orders. Someone else's order
    and a non-existent order give the SAME answer, so order ids cannot be probed.
    """
    cid = _norm_customer(customer_id)
    if not cid:
        return None, _err("NOT_SIGNED_IN",
                          "No customer is signed in. Ask the customer to sign in with their customer account "
                          "before looking at orders.")
    if cid not in load_customers():
        return None, _err("NOT_SIGNED_IN", f"Customer account '{cid}' is not valid. Ask the customer to sign in again.")
    oid = _norm_order_id(order_id)
    if not oid:
        return None, _err("INVALID_ARGUMENT", "An order id is required, for example O0001.")
    order = next((o for o in load_orders() if o["order_id"] == oid), None)
    if order is None and oid.startswith("O") and oid[1:].isdigit():
        number = oid[1:].lstrip("0")
        order = next((o for o in load_orders()
                      if o["order_id"].startswith("O")
                      and o["order_id"][1:].isdigit()
                      and o["order_id"][1:].lstrip("0") == number), None)
    if order is None or order["customer_id"] != cid:
        return None, _err("ORDER_NOT_FOUND",
                          f"We could not find order {oid} on your account. Please check the order id.")
    return order, None


def _effective_return_status(order: Dict[str, Any]) -> str:
    if order["order_id"] in get_created_returns():
        return "Return Requested"
    if order["order_status"] in ("Return Requested", "Returned"):
        return order["order_status"]
    return "None"


def _delivery_message(o: Dict[str, Any]) -> Tuple[str, Optional[bool], Optional[int]]:
    """Plain English delivery summary, plus (is_delayed, days_late)."""
    today, status = config.TODAY, o["order_status"]
    exp, delivered = o["expected"], o["delivered"]

    if status == "Delivered":
        if not delivered:
            return "This order is marked as delivered; the actual delivery date is not available.", None, None
        msg = f"Delivered on {_iso(delivered)} ({(today - delivered).days} day(s) ago)."
        if exp and delivered > exp:
            msg += f" It arrived {(delivered - exp).days} day(s) after the expected date of {_iso(exp)}."
        return msg, False, 0
    if status == "Cancelled":
        return "This order was cancelled, so nothing will be delivered.", None, None
    if status == "Returned":
        if not delivered:
            return "This order is marked as returned; the delivery date is not available.", None, None
        return f"The item was delivered on {_iso(delivered)} and has since been returned.", None, None
    if status == "Return Requested":
        return "A return has been requested for this order.", None, None

    base = {
        "Processing": "The order is being prepared and has not shipped yet.",
        "Shipped": "The order has shipped and is in transit.",
        "Out for Delivery": "The order is out for delivery.",
    }.get(status, f"Order status: {status}.")
    if exp and today > exp:
        late = (today - exp).days
        return f"{base} It is running {late} day(s) late. The expected delivery date was {_iso(exp)}.", True, late
    if exp:
        left = (exp - today).days
        when = "today" if left == 0 else f"in {left} day(s)"
        return f"{base} Expected delivery: {_iso(exp)} ({when}).", False, 0
    return base, False, 0


def _order_view(o: Dict[str, Any]) -> Dict[str, Any]:
    p = _product_by_id(o["product_id"])
    message, delayed, late = _delivery_message(o)
    view = {
        "order_id": o["order_id"],
        "product_id": p["product_id"],
        "product_name": p["product_name"],
        "category": p["category"],
        "order_date": _iso(o["order_date"]),
        "order_status": o["order_status"],
        "payment_status": o["payment_status"],
        "expected_delivery_date": _iso(o["expected"]),
        "delivery_message": message,
    }
    if delayed is not None:
        view.update(is_delayed=delayed, days_late=late)
    return_status = _effective_return_status(o)
    if return_status != "None":
        view["return_status"] = return_status
    if o["quantity"] is not None and o["unit_price"] is not None:
        total = o["quantity"] * o["unit_price"]
        view.update(quantity=o["quantity"], unit_price=inr(o["unit_price"]),
                    total_amount_inr=total, total_amount=inr(total))
    if o["payment_method"]:
        view["payment_method"] = o["payment_method"]
    if o["delivered"]:
        view["delivered_date"] = _iso(o["delivered"])
    if o["return_request_date"]:
        view["return_request_date"] = _iso(o["return_request_date"])
    return view


def list_my_orders(customer_id: Any = None) -> Dict[str, Any]:
    """All orders of the signed-in customer, newest first."""
    cid = _norm_customer(customer_id)
    customer = load_customers().get(cid or "")
    if not customer:
        return _err("NOT_SIGNED_IN", "No customer is signed in. Ask the customer to sign in with their customer "
                                     "account before looking at orders.")
    mine = sorted((o for o in load_orders() if o["customer_id"] == cid),
                  key=lambda o: o["order_date"], reverse=True)
    items = []
    for o in mine:
        p = _product_by_id(o["product_id"])
        message, delayed, late = _delivery_message(o)
        item = {
            "order_id": o["order_id"], "product_id": p["product_id"], "category": p["category"],
            "product_name": p["product_name"], "order_date": _iso(o["order_date"]),
            "order_status": o["order_status"], "payment_status": o["payment_status"],
            "expected_delivery_date": _iso(o["expected"]), "delivery_message": message,
        }
        returned = _effective_return_status(o)
        item["return_status"] = returned
        if o["delivered"]:
            item["delivered_date"] = _iso(o["delivered"])
        if o["quantity"] is not None and o["unit_price"] is not None:
            item["quantity"] = o["quantity"]
            item["total_amount"] = inr(o["quantity"] * o["unit_price"])
        if o["payment_method"]:
            item["payment_method"] = o["payment_method"]
        if o["return_request_date"]:
            item["return_request_date"] = _iso(o["return_request_date"])
        if delayed is not None:
            item["is_delayed"] = delayed
            item["days_late"] = late
        items.append(item)
    return {"customer_id": cid, "customer_name": customer["name"], "order_count": len(items), "orders": items}


def get_order_status(order_id: Any = None, customer_id: Any = None) -> Dict[str, Any]:
    """Status, payment and delivery details of one order of the signed-in customer."""
    order, error = _owned_order(order_id, customer_id)
    if error:
        return error
    return _order_view(order)


def get_return_policy(category: Optional[str] = None) -> Dict[str, Any]:
    """Return and refund rules, for one category or for all categories."""
    cat_in = _clean_str(category)
    cat = _canon_category(cat_in) if cat_in else None
    if cat_in and not cat:
        return _err("UNKNOWN_CATEGORY", f"'{cat_in}' is not a category we sell.", valid_categories=_valid_categories())

    def describe(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "category": row["category"],
            "change_of_mind_window_days": row["return_window_days"],
            "defective_or_damaged_window_days": row["defect_window_days"],
            "refund_method": row["refund_method"],
            "refund_processing_business_days": row["refund_processing_days"],
            "conditions": row["conditions"],
            "summary": (f"{row['category']}: change-of-mind returns within {row['return_window_days']} days of "
                        f"delivery; defective, damaged or wrong items within {row['defect_window_days']} days of "
                        f"delivery. Refund within {row['refund_processing_days']} business days after the item "
                        f"passes the quality check."),
        }

    policy = load_policy()
    if cat:
        return {"category_policy": describe(policy[cat]), "general_rules": GENERAL_POLICY}
    return {"all_categories": [describe(policy[c]) for c in sorted(policy)], "general_rules": GENERAL_POLICY}


_DEFECT_WORDS = ("defect", "fault", "damag", "broken", "break", "crack", "not work", "stopped work", "doesn't work",
                 "doesnt work", "dead", "wrong", "missing", "malfunction", "scratch", "leak", "burn", "faulty")


def _reason_type(reason: Any) -> str:
    """'defective' for faulty / damaged / wrong item reasons, else 'changed_mind'."""
    text = (_clean_str(reason) or "").lower()
    return "defective" if any(w in text for w in _DEFECT_WORDS) else "changed_mind"


def _eligibility(o: Dict[str, Any], reason: Any) -> Dict[str, Any]:
    """The return rules in one place. Used by check_return_eligibility and initiate_return."""
    today = config.TODAY
    p = _product_by_id(o["product_id"])
    pol = load_policy()[p["category"]]
    rtype = _reason_type(reason)
    window = pol["defect_window_days"] if rtype == "defective" else pol["return_window_days"]
    label = "defect, damage or wrong-item" if rtype == "defective" else "change-of-mind"

    out: Dict[str, Any] = {
        "order_id": o["order_id"], "product_name": p["product_name"], "category": p["category"],
        "order_status": o["order_status"], "reason_type": rtype, "window_days": window,
        "eligible": False, "decision": "NOT_ELIGIBLE",
    }
    if o["order_status"] == "Delivered" and not o["delivered"]:
        out.update(
            error="DATA_UNAVAILABLE",
            decision="UNKNOWN",
            message=("The order is marked as delivered, but its actual delivery date is not in the supplied "
                     "orders.csv, so the return window cannot be verified."),
        )
        return out

    status = _effective_return_status(o)
    if status != "None":
        out["message"] = f"Not eligible: a return already exists for this order (status: {status})."
        return out
    st = o["order_status"]
    if st == "Cancelled":
        out["message"] = ("Not eligible: this order was cancelled and never delivered, so there is nothing to return. "
                          "If it was paid, the refund follows the cancellation refund rules.")
        return out
    if st == "Returned":
        out["message"] = "Not eligible: this order has already been returned."
        return out
    if st not in _RETURN_OK_STATES:
        out["message"] = (f"Not eligible yet: the order has not been delivered (status: {st}), so it cannot be "
                          f"returned. Returns open after delivery.")
        return out

    delivered = o["delivered"]
    days_since = (today - delivered).days
    deadline = delivered + timedelta(days=window)
    days_left = (deadline - today).days
    out.update(delivered_date=_iso(delivered), days_since_delivery=days_since, return_deadline=_iso(deadline),
               days_left=max(days_left, 0))

    if days_since <= window:
        method = (f"Original payment method ({o['payment_method']})" if o["payment_method"]
                  else "Original payment method")
        left_text = "today is the last day" if days_left == 0 else f"{days_left} day(s) left"
        out.update(
            eligible=True, decision="ELIGIBLE",
            refund_method=method, refund_processing_business_days=pol["refund_processing_days"],
            conditions=pol["conditions"],
            message=(f"Eligible: delivered on {_iso(delivered)} ({days_since} day(s) ago), inside the {window}-day "
                     f"{label} window that ends on {_iso(deadline)} ({left_text}). Refund timing and amount depend "
                     f"on order details not present in the supplied CSV. Item condition required: {pol['conditions']}."))
        return out

    msg = (f"Not eligible: delivered on {_iso(delivered)} ({days_since} day(s) ago), which is outside the "
           f"{window}-day {label} window that ended on {_iso(deadline)}.")
    if rtype == "changed_mind":
        defect_deadline = delivered + timedelta(days=pol["defect_window_days"])
        if today <= defect_deadline:
            out["defect_claim_possible_until"] = _iso(defect_deadline)
            msg += (f" If the product is defective, damaged or wrong, a claim is still possible until "
                    f"{_iso(defect_deadline)} ({(defect_deadline - today).days} day(s) left).")
    out["message"] = msg
    return out


def check_return_eligibility(order_id: Any = None, customer_id: Any = None, reason: Any = None) -> Dict[str, Any]:
    """Can this order be returned today? Read-only: nothing is created."""
    order, error = _owned_order(order_id, customer_id)
    if error:
        return error
    return _eligibility(order, reason)


def get_refund_details(order_id: Any = None, customer_id: Any = None) -> Dict[str, Any]:
    """Report the refund facts available for an order without inferring missing amounts or dates."""
    order, error = _owned_order(order_id, customer_id)
    if error:
        return error
    p = _product_by_id(order["product_id"])
    pay, status = order["payment_status"], order["order_status"]
    ret = _effective_return_status(order)
    out: Dict[str, Any] = {
        "order_id": order["order_id"], "product_name": p["product_name"], "order_status": status,
        "payment_status": pay,
    }
    if ret != "None":
        out["return_status"] = ret
    if pay == "Refunded":
        out.update(refund_state="COMPLETED",
                   message="The payment status in the order data is Refunded. The refund amount, method, and date are not provided.")
    elif ret == "Return Requested":
        out.update(refund_state="UNKNOWN",
                   message=("A return is marked as requested. The supplied CSV does not include pickup, refund "
                            "progress, amount, or timing details."))
    else:
        out.update(refund_state="UNKNOWN",
                   message=(f"The supplied order data lists payment status {pay!r}, but does not include enough "
                            "refund details to confirm whether a refund is due or when it will arrive."))
    return out


def initiate_return(order_id: Any = None, customer_id: Any = None, reason: Any = None) -> Dict[str, Any]:
    """
    Create a return request. This is the one tool that CHANGES something (in
    memory only). It runs the same rules as check_return_eligibility first and
    refuses when the order is not eligible.
    """
    order, error = _owned_order(order_id, customer_id)
    if error:
        return error
    if not _clean_str(reason):
        return _err("INVALID_ARGUMENT", "A reason is required to create a return request. Ask the customer why.")
    result = _eligibility(order, reason)
    if result.get("error"):
        result["created"] = False
        result["message"] += " No return request was created."
        return result
    if not result["eligible"]:
        result["created"] = False
        result["message"] += " No return request was created."
        return result

    if persistence.enabled():
        request_id = persistence.create_return(order["order_id"], order["customer_id"], str(reason).strip(), result["reason_type"], _iso(config.TODAY))
    else:
        request_id = f"RR-{len(_RETURNS_CREATED) + 1:04d}"
        _RETURNS_CREATED[order["order_id"]] = {
            "return_request_id": request_id, "order_id": order["order_id"], "customer_id": order["customer_id"],
            "reason": str(reason).strip(), "reason_type": result["reason_type"], "created_on": _iso(config.TODAY),
        }
    result["created"] = True
    result["return_request_id"] = request_id
    result["message"] = (f"Return request {request_id} was created for order {order['order_id']}. Pickup will be "
                         f"arranged within 2 business days. {result['message']}")
    return result
