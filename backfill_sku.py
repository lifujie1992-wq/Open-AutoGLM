#!/usr/bin/env python3
"""Backfill missing SKU data for previously-scraped records.

For each record in the input that lacks `skus`, open the product detail page
via `adb am start` (using its share short link), dump the SKU sheet, and
merge skus/sizes/selected_sku into the record. Writes a new jsonl.

Uses the same humanization / popup-dismissal helpers as u2_shoes.py.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uiautomator2 as u2  # noqa: E402

from u2_shoes import (  # noqa: E402
    dismiss_popups,
    extract_sku_table,
    has_captcha,
    hback,
    hsleep,
)


def open_product(url: str) -> None:
    """Launch livelite directly at the product detail via deep link."""
    subprocess.run(
        ["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW",
         "-d", url, "-p", "com.ss.android.ugc.livelite"],
        check=False, capture_output=True,
    )


def main(in_path: Path, out_path: Path) -> int:
    records = [json.loads(l) for l in in_path.read_text().splitlines() if l.strip()]
    todo = [r for r in records if not r.get("skus")]
    print(f"total: {len(records)} | needs backfill: {len(todo)}")
    if not todo:
        print("nothing to do.")
        return 0
    d = u2.connect()
    print(f"connected: {d.app_current()}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    captured = 0
    for i, rec in enumerate(records):
        if rec.get("skus"):
            continue
        title = rec.get("title", "")[:30]
        url = rec.get("url", "")
        print(f"\n[{captured + 1}/{len(todo)}] {title}")
        if not url:
            print("  skip: no url")
            continue
        if has_captcha(d):
            print("!!! captcha detected — stopping; complete it then re-run")
            break
        open_product(url)
        # SPA cold load: wait longer for the detail page to fully render
        time.sleep(random.uniform(7.5, 11.0))
        dismiss_popups(d)
        sku_data = extract_sku_table(d)
        skus = sku_data.get("skus", [])
        print(f"  → {len(skus)} SKUs, "
              f"{len(sku_data.get('sizes', []))} sizes, "
              f"selected={sku_data.get('selected_sku', '')!r}")
        if skus:
            rec["skus"] = skus
            rec["sizes"] = sku_data.get("sizes", [])
            rec["selected_sku"] = sku_data.get("selected_sku", "")
            captured += 1
        # head back to home / close popups before next loop
        hback(d)
        hsleep(0.8)
        # paced cooldown to avoid risk-control
        rest = random.uniform(6.0, 12.0)
        if captured and captured % 5 == 0:
            rest = random.uniform(25.0, 50.0)
        print(f"  rest {rest:.1f}s")
        time.sleep(rest)
    # Write all records (backfilled + already-complete)
    with out_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\ndone → {out_path} | backfilled {captured}/{len(todo)}")
    return 0


if __name__ == "__main__":
    in_p = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results" / "u2_links.full.jsonl"
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "results" / "u2_links.full.jsonl"
    sys.exit(main(in_p, out_p))
