"""One-shot: backfill F-J columns (sales_exact, price_min, price_max,
main_image, product_id) of the Feishu sheet from an enriched JSONL.

Matches by URL in column B. Existing F-J cells are overwritten.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from feishu_push import load_config, _unwrap_cell

ROOT = Path(__file__).resolve().parent
ENRICHED = ROOT / "results" / "u2_links.enriched.jsonl"


def main() -> int:
    cfg = load_config()
    if not cfg:
        print("missing feishu_config.json", file=sys.stderr)
        return 1
    if not ENRICHED.exists():
        print(f"input not found: {ENRICHED}", file=sys.stderr)
        return 1

    records = [json.loads(line) for line in ENRICHED.read_text().splitlines() if line.strip()]
    by_url = {r.get("url", ""): r for r in records if r.get("url")}
    print(f"loaded {len(records)} enriched records ({len(by_url)} unique URLs)")

    # Read column B (URLs) from sheet to get the row order
    read_cmd = [
        "lark-cli", "sheets", "+read",
        "--as", "bot",
        "--spreadsheet-token", cfg["spreadsheet_token"],
        "--range", f"{cfg['sheet_id']}!B2:B5000",
    ]
    r = subprocess.run(read_cmd, capture_output=True, text=True, timeout=30)
    data = json.loads(r.stdout)
    values = data.get("data", {}).get("valueRange", {}).get("values", []) or []

    sheet_urls = []
    for i, row in enumerate(values):
        if not row:
            sheet_urls.append("")
            continue
        sheet_urls.append(_unwrap_cell(row[0]))

    # Trim trailing empty rows
    while sheet_urls and not sheet_urls[-1]:
        sheet_urls.pop()
    print(f"sheet has {len(sheet_urls)} data rows (B2:B{len(sheet_urls)+1})")

    # Build the F-J matrix in row order
    fj_matrix = []
    matched = 0
    unmatched = []
    for u in sheet_urls:
        rec = by_url.get(u)
        if rec:
            matched += 1
            fj_matrix.append([
                rec.get("sales_exact", ""),
                rec.get("price_min_cny", ""),
                rec.get("price_max_cny", ""),
                rec.get("main_image", ""),
                rec.get("product_id", ""),
            ])
        else:
            unmatched.append(u)
            fj_matrix.append(["", "", "", "", ""])

    print(f"matched {matched}/{len(sheet_urls)} rows; {len(unmatched)} URLs missing from enriched data")
    if unmatched:
        for u in unmatched[:3]:
            print(f"  unmatched: {u}")

    # Write F2:J<last> in one batch
    last_row = len(sheet_urls) + 1
    write_cmd = [
        "lark-cli", "sheets", "+write",
        "--as", "bot",
        "--spreadsheet-token", cfg["spreadsheet_token"],
        "--range", f"{cfg['sheet_id']}!F2:J{last_row}",
        "--values", json.dumps(fj_matrix, ensure_ascii=False),
    ]
    r = subprocess.run(write_cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"write FAILED: {r.stderr[:300]}", file=sys.stderr)
        return 2
    resp = json.loads(r.stdout)
    cells = resp.get("data", {}).get("updatedCells", "?")
    print(f"write OK — {cells} cells updated in F2:J{last_row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
