"""
run_evaluation.py
=================
Runs the test cases and writes the testing evidence into results/<label>/.

    python -m evaluation.run_evaluation                 # real run with your Groq model
    python -m evaluation.run_evaluation --oracle        # self-test, no LLM needed (should be 100%)
    python -m evaluation.run_evaluation --only service  # only customer-service cases
    python -m evaluation.run_evaluation --ids D01,S05   # only these cases
    python -m evaluation.run_evaluation --model openai/gpt-oss-20b

Output files:
    test_case_sheet.csv   the test cases and their pass criteria
    test_results.csv      one row per case: tools called, verdicts, reply
    metrics.json          the three metrics (overall and per type)
    summary.md            readable summary with the list of failures
    agent_logs.jsonl      full trace of every case (tool calls, arguments, results)
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import tools  # noqa: E402
from agent import ShopAgent  # noqa: E402
from evaluation.metrics import grade, summarize  # noqa: E402
from evaluation.oracle_llm import OracleLLM  # noqa: E402
from evaluation.test_cases import ALL_CASES  # noqa: E402
from llm import LLMUnavailable, get_chat_model  # noqa: E402


def write_sheet(cases, out: Path):
    with open(out / "test_case_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "type", "signed_in_customer", "user_request", "expected_tools", "optional_tools",
                    "order", "constraints", "expected_decision", "return_request_expected", "must_contain",
                    "must_not_contain", "tags"])
        for c in cases:
            w.writerow([c.id, c.kind, c.customer or "(not signed in)", c.query, " + ".join(c.expected_tools) or "(none)",
                        ", ".join(c.optional_tools), c.order or "", json.dumps(c.constraints) if c.constraints else "",
                        c.decision or ("no result expected" if c.expect_empty else ""),
                        "" if c.expect_return is None else c.expect_return, " | ".join(c.must_contain),
                        " | ".join(c.must_not_contain), ", ".join(c.tags)])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle", action="store_true", help="self-test without any LLM")
    ap.add_argument("--model", default="", help="Groq model name (default: GROQ_MODEL)")
    ap.add_argument("--only", choices=["discovery", "service"])
    ap.add_argument("--ids", default="", help="comma separated test ids, e.g. D01,S05")
    args = ap.parse_args()

    cases = [c for c in ALL_CASES if (not args.only or c.kind == args.only)]
    if args.ids:
        wanted = {i.strip().upper() for i in args.ids.split(",")}
        cases = [c for c in cases if c.id in wanted]

    shared_agent, label = None, "oracle"
    if not args.oracle:
        try:
            llm = get_chat_model(args.model)
        except LLMUnavailable as exc:
            sys.exit(f"\n{exc}\n")
        shared_agent = ShopAgent(llm=llm)
        label = re.sub(r"[^A-Za-z0-9._-]", "-", str(llm.model))
    out = config.RESULTS_DIR / label
    out.mkdir(parents=True, exist_ok=True)
    write_sheet(cases, out)
    print(f"Running {len(cases)} test cases ({'oracle self-test' if args.oracle else label}) ...\n")

    rows, logs = [], []
    for case in cases:
        tools.reset_runtime_state()
        agent = ShopAgent(llm=OracleLLM(case)) if args.oracle else shared_agent
        try:
            res = agent.chat(case.query, customer_id=case.customer, history=[], log=False).to_dict()
        except LLMUnavailable as exc:
            sys.exit(f"\n{exc}\n")
        except Exception as exc:  # one broken case must not stop the run
            res = {"reply": "", "tool_calls": [], "steps": 0, "latency_s": 0, "error": f"{type(exc).__name__}: {exc}"}
        verdict = grade(case, res, tools.get_created_returns())
        row = {"id": case.id, "kind": case.kind, "query": case.query, "customer": case.customer or "",
               "expected": " + ".join(case.expected_tools) or "(none)", "reply": res["reply"],
               "latency_s": res["latency_s"], "steps": res["steps"], **verdict}
        rows.append(row)
        logs.append({"id": case.id, "query": case.query, "customer": case.customer, **res, "verdict": verdict})
        flag = lambda v: "n/a " if v is None else ("PASS" if v else "FAIL")  # noqa: E731
        print(f"{case.id}  tool:{flag(verdict['tool_ok'])}  policy:{flag(verdict['compliance'])}  "
              f"done:{flag(verdict['completed'])}  {case.query[:60]}")

    metrics = summarize(rows)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    with open(out / "agent_logs.jsonl", "w", encoding="utf-8") as f:
        for entry in logs:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    with open(out / "test_results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "type", "customer", "request", "expected_tools", "tools_called", "tool_selection",
                    "constraint_policy_compliance", "task_completed", "notes", "latency_s", "reply"])
        for r in rows:
            fl = lambda v: "n/a" if v is None else ("PASS" if v else "FAIL")  # noqa: E731
            notes = " | ".join(n for n in (r["tool_notes"], r["compliance_notes"], r["completion_notes"]) if n)
            w.writerow([r["id"], r["kind"], r["customer"], r["query"], r["expected"], " > ".join(r["tools_called"]),
                        fl(r["tool_ok"]), fl(r["compliance"]), fl(r["completed"]), notes, r["latency_s"],
                        r["reply"].replace("\n", " ")])

    lines = [f"# Evaluation summary ({label})", "", "| Metric | Overall | Product discovery | Customer service |",
             "|---|---|---|---|"]
    for key, title in [("tool_selection_accuracy_pct", "Tool-selection accuracy"),
                       ("constraint_or_policy_compliance_pct", "Constraint satisfaction / policy compliance"),
                       ("task_completion_rate_pct", "Task completion rate"),
                       ("valid_argument_rate_pct", "Valid tool arguments"), ("avg_latency_s", "Average time (s)")]:
        lines.append(f"| {title} | " + " | ".join(
            str(metrics[b][key]) for b in ("overall", "product_discovery", "customer_service")) + " |")
    lines += ["", f"Tests run: {metrics['overall']['tests']} "
                  f"(discovery {metrics['product_discovery']['tests']}, service {metrics['customer_service']['tests']}).", "",
              "## Failures", ""]
    failed = [r for r in rows if not (r["tool_ok"] and r["compliance"] is not False and r["completed"])]
    for r in failed:
        notes = " | ".join(n for n in (r["tool_notes"], r["compliance_notes"], r["completion_notes"]) if n)
        lines.append(f"- **{r['id']}** {r['query']}  \n  {notes}")
    if not failed:
        lines.append("None.")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    o = metrics["overall"]
    print(f"\nTool-selection accuracy : {o['tool_selection_accuracy_pct']}%")
    print(f"Constraint/policy comp. : {o['constraint_or_policy_compliance_pct']}% (over {o['compliance_tests']} tests)")
    print(f"Task completion rate    : {o['task_completion_rate_pct']}%")
    print(f"\nFiles written to {out}")


if __name__ == "__main__":
    main()
