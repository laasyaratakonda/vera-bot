#!/usr/bin/env python3
"""
Builds submission.jsonl (challenge-brief.md §7.2) from the 30 canonical
test_pairs.json entries produced by dataset/generate_dataset.py, calling
composer.py directly (same logic your live bot uses at /v1/tick).

Usage:
    python generate_submission.py --dataset /path/to/expanded --out submission.jsonl
"""
import argparse
import json
from pathlib import Path

from composer import compose


def load_json(p: Path):
    return json.loads(p.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="./expanded", help="Path to the expanded dataset dir")
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    ds = Path(args.dataset)
    test_pairs = load_json(ds / "test_pairs.json")["pairs"]

    categories = {f.stem: load_json(f) for f in (ds / "categories").glob("*.json")}
    merchants = {f.stem: load_json(f) for f in (ds / "merchants").glob("*.json")}
    customers = {f.stem: load_json(f) for f in (ds / "customers").glob("*.json")}
    triggers = {f.stem: load_json(f) for f in (ds / "triggers").glob("*.json")}

    lines = []
    for pair in test_pairs:
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None

        composed = compose(category, merchant, trigger, customer)
        lines.append({
            "test_id": pair["test_id"],
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    with open(args.out, "w") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {args.out}")


if __name__ == "__main__":
    main()
