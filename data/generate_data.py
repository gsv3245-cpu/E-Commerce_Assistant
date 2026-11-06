"""Generate synthetic return-policy rows for the current product catalogue.

The supplied products.csv and orders.csv are source data and are never
rewritten by this script.

Run from the project root:

    python data/generate_data.py
"""
import csv
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
POLICY_FIELDS = [
    "category", "return_window_days", "defect_window_days", "refund_method",
    "refund_processing_days", "conditions",
]
POLICY_DEFAULTS = {
    "return_window_days": 7,
    "defect_window_days": 30,
    "refund_method": "Original payment method",
    "refund_processing_days": 5,
    "conditions": "Synthetic demo policy: original packaging and accessories must be included",
}


def main() -> None:
    with open(DATA_DIR / "products.csv", newline="", encoding="utf-8") as source:
        categories = sorted({row["category"].strip() for row in csv.DictReader(source)})
    if not categories:
        raise ValueError("products.csv contains no product categories")

    with open(DATA_DIR / "returns_policy.csv", "w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=POLICY_FIELDS)
        writer.writeheader()
        for category in categories:
            writer.writerow({"category": category, **POLICY_DEFAULTS})
    print(f"Wrote synthetic policies for {len(categories)} product categories")


if __name__ == "__main__":
    main()
