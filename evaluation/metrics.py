"""
metrics.py
==========
Grades one agent run against one test case and computes the three metrics
from the brief:

  1. Tool-selection accuracy
        share of test requests where the right tool(s) were called, no wrong
        tool was called, and order tools were used with the right order id.
  2. Constraint satisfaction / return-policy compliance
        discovery cases : every product named in the reply meets ALL the
                          requirements of the request
        service cases   : the return decision matches the policy, a return
                          request was (not) created as it should be, and nothing
                          forbidden (leaked data, false promises) was said.
        Only cases that have such a rule count in the denominator.
  3. Task completion rate
        share of requests completed successfully: the agent produced an answer,
        the key facts are in it, nothing forbidden is in it, and the compliance
        check (when it applies) passed.

The grader reads the raw CSV files itself and does not call tools.py to decide
what is right, so it is an independent check on the tools as well.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from typing import Any, Dict, List, Optional

import config

ARG_ERROR_CODES = {"INVALID_ARGUMENT", "UNKNOWN_CATEGORY", "UNKNOWN_BRAND", "TOOL_EXCEPTION", "UNKNOWN_TOOL"}
ORDER_TOOLS = {"get_order_status", "check_return_eligibility", "get_refund_details", "initiate_return"}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def _products() -> List[Dict[str, Any]]:
    with open(config.DATA_DIR / "products.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["price"], r["stock"], r["rating"] = int(r["price"]), int(r["stock"]), float(r["rating"])
    return rows


# ---------------------------------------------------------------- text checks
def _norm(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").lower()


def _has_date(reply: str, iso: str) -> bool:
    d = date.fromisoformat(iso)
    low = _norm(reply)
    if iso in low:
        return True
    full = MONTHS[d.month - 1]
    for mon in {full, full[:3]}:
        if re.search(rf"\b0?{d.day}(?:st|nd|rd|th)?\s+(?:of\s+)?{mon}\b", low):
            return True
        if re.search(rf"\b{mon}\.?\s+0?{d.day}(?:st|nd|rd|th)?\b", low):
            return True
    return bool(re.search(rf"\b0?{d.day}[/-]0?{d.month}[/-](?:{d.year}|{d.year % 100})\b", low))


def _has_number(reply: str, number: str) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(number)}(?!\d)", reply.replace(",", "")))


def has_token(reply: str, token: str) -> bool:
    """True if ANY of the '/'-separated alternatives is present in the reply."""
    for alt in token.split("/"):
        alt = alt.strip()
        if alt.startswith("date:"):
            ok = _has_date(reply, alt[5:])
        elif alt.startswith("num:"):
            ok = _has_number(reply, alt[4:])
        else:
            ok = bool(alt) and _norm(alt) in _norm(reply)
        if ok:
            return True
    return False


_NOT_ELIGIBLE = [
    r"ineligible", r"not (?:be )?eligible", r"(?:isn't|is not|aren't|are not|wasn't|was not) (?:eligible|returnable)",
    r"(?:can't|cannot|can not|unable to|not able to) (?:be )?(?:return|process|initiate|start)",
    r"(?:window|period) (?:has |have )?(?:already )?(?:closed|expired|ended|passed)",
    r"outside (?:of )?the (?:\d+-day )?(?:return |change-of-mind )?(?:window|period)", r"past the return window",
    r"no longer eligible",
]
_ELIGIBLE = [
    r"\beligible\b", r"can (?:be )?return", r"you can (?:still )?(?:return|initiate|start)",
    r"return request[^.]{0,40}(?:created|raised|initiated|placed)", r"has been (?:created|initiated|raised)",
]


def decision_from_text(reply: str) -> str:
    """Heuristic: which verdict does the reply give? Earliest verdict phrase wins."""
    low = _norm(reply)

    def first(patterns):
        hits = [m.start() for p in patterns for m in [re.search(p, low)] if m]
        return min(hits) if hits else None

    neg, pos = first(_NOT_ELIGIBLE), first(_ELIGIBLE)
    if neg is None and pos is None:
        return "UNKNOWN"
    if pos is None or (neg is not None and neg <= pos):
        return "NOT_ELIGIBLE"
    return "ELIGIBLE"


def decision_from_tools(calls: List[Dict[str, Any]], order: Optional[str]) -> Optional[str]:
    for c in reversed(calls):
        r = c.get("result") or {}
        if c["name"] in ("check_return_eligibility", "initiate_return") and "decision" in r:
            if order is None or r.get("order_id") == order:
                return r["decision"]
    return None


# --------------------------------------------------------- product constraints
def _ram(spec: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*GB\s*RAM", spec, re.I)
    return int(m.group(1)) if m else None


def _storage(spec: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(GB|TB)\s*(?:SSD|storage)", spec, re.I)
    return None if not m else int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)


def violations(p: Dict[str, Any], c: Dict[str, Any]) -> List[str]:
    out = []
    if "category" in c and p["category"] != c["category"]:
        out.append(f"category {p['category']} != {c['category']}")
    if "brand" in c and p["brand"] != c["brand"]:
        out.append(f"brand {p['brand']} != {c['brand']}")
    if "min_price" in c and p["price"] < c["min_price"]:
        out.append(f"price {p['price']} < {c['min_price']}")
    if "max_price" in c and p["price"] > c["max_price"]:
        out.append(f"price {p['price']} > {c['max_price']}")
    if "min_rating" in c and p["rating"] < c["min_rating"]:
        out.append(f"rating {p['rating']} < {c['min_rating']}")
    if c.get("in_stock") and p["stock"] <= 0:
        out.append("out of stock")
    if "min_ram_gb" in c and (_ram(p["specifications"]) or 0) < c["min_ram_gb"]:
        out.append(f"RAM below {c['min_ram_gb']} GB")
    if "min_storage_gb" in c and (_storage(p["specifications"]) or 0) < c["min_storage_gb"]:
        out.append(f"storage below {c['min_storage_gb']} GB")
    spec = re.sub(r"[\s\-]+", "", p["specifications"].lower())
    for kw in c.get("keywords", []):
        if re.sub(r"[\s\-]+", "", kw.lower()) not in spec:
            out.append(f"missing '{kw}'")
    return out


def mentioned_products(reply: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Catalogue products named in the reply: by id, full name, or name without the brand."""
    low = _norm(reply)
    found = []
    for p in products:
        name = p["product_name"].lower()
        brand = p["brand"].lower()
        rest = name[len(brand):].strip() if name.startswith(brand) else name
        if (re.search(rf"\b{p['product_id'].lower()}\b", low) or name in low or
                (len(rest) >= 6 and re.search(rf"(?<!\w){re.escape(rest)}(?!\w)", low))):
            found.append(p)
    return found


def _norm_order(value: Any) -> str:
    s = str(value or "").upper().replace("#", "").replace(" ", "")
    return "O" + s if s.isdigit() else s


# ------------------------------------------------------------------- grading
def grade(case, run: Dict[str, Any], created_returns: Dict[str, Any]) -> Dict[str, Any]:
    """run = {'reply', 'tool_calls', 'error'}. Returns the verdicts for one case."""
    calls, reply = run.get("tool_calls", []), run.get("reply", "") or ""
    names = [c["name"] for c in calls]
    notes: List[str] = []

    # 1. tool selection
    missing = [r for r in case.expected_tools if not any(a in names for a in r.split("|"))]
    allowed = {a for r in case.expected_tools for a in r.split("|")} | set(case.optional_tools)
    extra = sorted({n for n in names if n not in allowed})
    wrong_order = []
    if case.order:
        wrong_order = [c["name"] for c in calls if c["name"] in ORDER_TOOLS
                       and _norm_order((c.get("args") or {}).get("order_id")) != case.order]
    tool_ok = not missing and not extra and not wrong_order
    tool_notes = []
    if missing:
        tool_notes.append(f"missing tool: {', '.join(missing)}")
    if extra:
        tool_notes.append(f"unexpected tool: {', '.join(extra)}")
    if wrong_order:
        tool_notes.append(f"wrong order id in: {', '.join(wrong_order)}")

    # 2. constraint satisfaction / policy compliance
    applicable, comp_ok, comp_notes = False, True, []
    if case.constraints is not None:
        applicable = True
        if case.expect_empty:
            if not any(c["name"] == "search_products" and (c.get("result") or {}).get("total_matches") == 0
                       for c in calls):
                comp_ok = False
                comp_notes.append("no search returned an empty result")
        else:
            shown = mentioned_products(reply, _products())
            if not shown:
                comp_ok = False
                comp_notes.append("no catalogue product named in the reply")
            for p in shown:
                bad = violations(p, case.constraints)
                if bad:
                    comp_ok = False
                    comp_notes.append(f"{p['product_id']}: {'; '.join(bad)}")
    if case.decision is not None:
        applicable = True
        by_tool, by_text = decision_from_tools(calls, case.order), decision_from_text(reply)
        if by_tool != case.decision:
            comp_ok = False
            comp_notes.append(f"policy tool said {by_tool}, expected {case.decision}")
        elif by_text not in (case.decision, "UNKNOWN"):
            comp_ok = False
            comp_notes.append(f"reply says {by_text}, expected {case.decision}")
    if case.expect_return is not None:
        applicable = True
        if (case.order in created_returns) != case.expect_return:
            comp_ok = False
            comp_notes.append("return request created" if case.order in created_returns else "return request missing")
    for token in case.must_not_contain:
        applicable = True
        if has_token(reply, token):
            comp_ok = False
            comp_notes.append(f"forbidden content: {token.split('/')[0]}")

    # 3. task completion
    missing_facts = [t.split("/")[0] for t in case.must_contain if not has_token(reply, t)]
    completed = bool(reply.strip()) and not run.get("error") and not missing_facts and (comp_ok or not applicable)
    done_notes = []
    if run.get("error"):
        done_notes.append(f"agent error: {run['error']}")
    if missing_facts:
        done_notes.append(f"missing in reply: {', '.join(missing_facts)}")

    arg_errors = sum(1 for c in calls if (c.get("result") or {}).get("error") in ARG_ERROR_CODES)
    return {
        "tool_ok": tool_ok, "tool_notes": "; ".join(tool_notes),
        "compliance": (comp_ok if applicable else None), "compliance_notes": "; ".join(comp_notes),
        "completed": completed, "completion_notes": "; ".join(done_notes),
        "tools_called": names, "arg_calls": len(calls), "arg_errors": arg_errors,
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def pct(a, b):
        return round(100.0 * a / b, 1) if b else None

    def block(items):
        comp = [r for r in items if r["compliance"] is not None]
        return {
            "tests": len(items),
            "tool_selection_accuracy_pct": pct(sum(r["tool_ok"] for r in items), len(items)),
            "constraint_or_policy_compliance_pct": pct(sum(1 for r in comp if r["compliance"]), len(comp)),
            "compliance_tests": len(comp),
            "task_completion_rate_pct": pct(sum(r["completed"] for r in items), len(items)),
            "valid_argument_rate_pct": pct(sum(r["arg_calls"] - r["arg_errors"] for r in items),
                                           sum(r["arg_calls"] for r in items)),
            "avg_latency_s": round(sum(r["latency_s"] for r in items) / len(items), 2) if items else None,
        }

    return {"overall": block(rows),
            "product_discovery": block([r for r in rows if r["kind"] == "discovery"]),
            "customer_service": block([r for r in rows if r["kind"] == "service"])}
