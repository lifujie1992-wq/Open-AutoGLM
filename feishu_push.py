"""Push captured records to a Feishu/Lark spreadsheet via lark-cli.

Config lives in results/feishu_config.json:
  { "spreadsheet_token": "...", "sheet_id": "...", "title": "抖音女鞋" }

Each row matches the sheet's header order:
  ts | url | title | sales | review_hint |
  sales_exact | price_min | price_max | main_image | product_id

CLI:
    python feishu_push.py                          # push results/u2_links.jsonl
    python feishu_push.py results/x.enriched.jsonl # push a specific file
    python feishu_push.py --dedup                  # skip URLs already in the sheet

Library:
    from feishu_push import push_records
    push_records([record_dict, ...])               # called by u2_shoes after each capture
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "results" / "feishu_config.json"

HEADERS = [
    "ts", "url", "title", "sales", "review_hint",
    "sales_exact", "price_min", "price_max", "main_image", "product_id",
]


def load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def record_to_row(r: dict) -> list:
    return [
        r.get("ts", ""),
        r.get("url", ""),
        r.get("title", ""),
        r.get("sales", ""),
        r.get("review_hint", ""),
        r.get("sales_exact", ""),
        r.get("price_min_cny", ""),
        r.get("price_max_cny", ""),
        r.get("main_image", ""),
        r.get("product_id", ""),
    ]


def push_records(records: list[dict], cfg: dict | None = None,
                 verbose: bool = False) -> bool:
    """Append records as rows to the Feishu sheet. Returns True on success.
    Best-effort: catches all subprocess errors and returns False without raising."""
    if not records:
        return True
    cfg = cfg or load_config()
    if not cfg or not cfg.get("spreadsheet_token") or not cfg.get("sheet_id"):
        if verbose:
            print("feishu: no config — skipping push", file=sys.stderr)
        return False
    rows = [record_to_row(r) for r in records]
    cmd = [
        "lark-cli", "sheets", "+append",
        "--as", "bot",
        "--spreadsheet-token", cfg["spreadsheet_token"],
        "--sheet-id", cfg["sheet_id"],
        "--values", json.dumps(rows, ensure_ascii=False),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if verbose:
            print(f"feishu: subprocess failed — {e}", file=sys.stderr)
        return False
    if r.returncode != 0:
        if verbose:
            print(f"feishu: lark-cli exit {r.returncode}: {r.stderr[:300]}", file=sys.stderr)
        return False
    return True


def _unwrap_cell(cell) -> str:
    """Feishu returns URLs as [{type:'url', link:'...', text:'...'}] segments.
    Plain text is just a string. Return canonical string."""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, list):
        for seg in cell:
            if isinstance(seg, dict):
                v = seg.get("link") or seg.get("text") or ""
                if v:
                    return v.strip()
        return ""
    if isinstance(cell, dict):
        return (cell.get("link") or cell.get("text") or "").strip()
    return str(cell)


def _read_existing_urls(cfg: dict) -> set[str]:
    """Read column B (url) from the sheet to dedupe before pushing."""
    cmd = [
        "lark-cli", "sheets", "+read",
        "--as", "bot",
        "--spreadsheet-token", cfg["spreadsheet_token"],
        "--range", f"{cfg['sheet_id']}!B2:B5000",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
        values = data.get("data", {}).get("valueRange", {}).get("values", []) or []
        urls = set()
        for row in values:
            if not row:
                continue
            u = _unwrap_cell(row[0])
            if u:
                urls.add(u)
        return urls
    except Exception:
        return set()


def _cmd_push(path: Path, dedup: bool) -> int:
    cfg = load_config()
    if not cfg:
        print(f"missing {CONFIG_FILE} — create the sheet first", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"input not found: {path}", file=sys.stderr)
        return 1
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    print(f"loaded {len(records)} records from {path}")
    if dedup:
        existing = _read_existing_urls(cfg)
        before = len(records)
        records = [r for r in records if r.get("url") and r["url"] not in existing]
        print(f"dedupe: {before - len(records)} already in sheet, pushing {len(records)}")
    if not records:
        print("nothing to push")
        return 0
    ok = push_records(records, cfg, verbose=True)
    print(f"push {'OK' if ok else 'FAILED'} ({len(records)} rows → {cfg.get('title')})")
    return 0 if ok else 2


if __name__ == "__main__":
    args = sys.argv[1:]
    dedup = "--dedup" in args
    args = [a for a in args if a != "--dedup"]
    path = Path(args[0]) if args else ROOT / "results" / "u2_links.jsonl"
    sys.exit(_cmd_push(path, dedup))
