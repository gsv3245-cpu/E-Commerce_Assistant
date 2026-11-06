"""
cli.py
======
Command-line version of the assistant (the brief allows a CLI or a web page).

    python cli.py                 # not signed in
    python cli.py

Use /customers to list current customer IDs and /customer <id> to sign in.

Commands inside the chat:  /customer <id>   /customers   /trace   /reset   /quit
"""
import argparse
import json
import sys

import config
import tools as shop
from agent import ShopAgent
from llm import LLMUnavailable


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", default=None, help="customer ID from the current orders.csv")
    args = ap.parse_args()

    agent = ShopAgent()
    customer = shop._norm_customer(args.customer)
    history, show_trace = [], True
    print(f"\n{config.STORE_NAME} assistant | simulated date {config.TODAY}")
    print("Type your question. Commands: /customer <id>  /customers  /trace  /reset  /quit\n")

    while True:
        who = (shop.get_customer(customer) or {}).get("name", "not signed in")
        try:
            text = input(f"[{who}] you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/reset":
            history.clear()
            shop.reset_runtime_state()
            print("Conversation cleared.\n")
            continue
        if text == "/trace":
            show_trace = not show_trace
            print(f"Tool trace {'on' if show_trace else 'off'}.\n")
            continue
        if text == "/customers":
            for c in shop.list_customers():
                location = f" ({c['city']})" if c.get("city") else ""
                print(f"  {c['customer_id']}  {c['name']}{location}")
            print()
            continue
        if text.startswith("/customer"):
            wanted = shop._norm_customer(text.replace("/customer", "", 1))
            if wanted and shop.get_customer(wanted):
                customer = wanted
                history.clear()
                print(f"Signed in as {shop.get_customer(wanted)['name']}.\n")
            else:
                print("Unknown customer id. Use /customers to list them.\n")
            continue

        try:
            result = agent.chat(text, customer_id=customer, history=history)
        except LLMUnavailable as exc:
            print(f"\n{exc}\n")
            continue
        if show_trace:
            for c in result.tool_calls:
                print(f"   tool> {c['name']}({json.dumps(c['args'], ensure_ascii=False)})  -> "
                      f"{'ok' if c['ok'] else 'ERROR ' + str(c['result'].get('error'))}  [{c['ms']} ms]")
        print(f"\nassistant> {result.reply}\n")
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
        del history[:-2 * config.HISTORY_TURNS]


if __name__ == "__main__":
    main()
