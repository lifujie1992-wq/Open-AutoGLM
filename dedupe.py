"""Cross-day dedupe for Douyin Mall shoes pipeline.

Canonical dedupe key is product_id (the user's choice: same product_id different
SKUs/colors = one product; different product_ids with same display title = kept).

Three-layer filter, cheapest first:
  1. card-level   — normalized title in last DEDUPE_DAYS days → skip before tap
  2. URL-level    — exact short-link match (existing in u2_shoes.py)
  3. product_id   — canonical, written from enrich_links.py after redirect parse

Index file: results/dedupe_index.json
  {
    "by_product_id": {
      "<pid>": {
        "titles_normalized": [...],
        "raw_titles": [...],
        "first_seen": "YYYY-MM-DD",
        "last_seen":  "YYYY-MM-DD",
        "url":  "...",
        "sales_last": int
      }
    },
    "by_norm_title": { "<norm>": ["<pid>", ...] }
  }

CLI:
    python dedupe.py rebuild           # rebuild index from u2_links.enriched.jsonl
    python dedupe.py stats             # print index stats
    python dedupe.py check "<title>"   # check whether a title would be filtered
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "results" / "dedupe_index.json"
DEFAULT_ENRICHED = ROOT / "results" / "u2_links.enriched.jsonl"

DEDUPE_DAYS = int(os.environ.get("DEDUPE_DAYS", "7"))

_ZW_CHARS = "​‌‍﻿⁠"
_LEADING_NOISE_RE = re.compile(r"^[IvVlL•·\-—_~`'\"\s]+")
_BRACKET_TAG_RE = re.compile(r"[【\[\(（].*?[】\]\)）]")
_YEAR_RE = re.compile(r"20\d{2}")
_SEASON_RE = re.compile(
    r"(?:春夏|秋冬|夏季|冬季|春季|秋季|夏天|冬天|新款|爆款|热销|特价|限量|清仓)"
)
_PUNCT_RE = re.compile(r"[^\w一-鿿]+")  # keep alnum + CJK


def normalize_title(s: str) -> str:
    """Aggressive normalization for cross-day matching.

    Removes year, season words, marketing tags, brackets, punctuation,
    and the zero-width spaces Douyin sprinkles between digits. Keeps the
    surface word order (sorting characters causes too many false collisions).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for c in _ZW_CHARS:
        s = s.replace(c, "")
    s = _LEADING_NOISE_RE.sub("", s)
    s = _BRACKET_TAG_RE.sub("", s)
    s = _YEAR_RE.sub("", s)
    s = _SEASON_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return s.lower().strip()


# --- index I/O --------------------------------------------------------------

def _empty_index() -> dict:
    return {"by_product_id": {}, "by_norm_title": {}}


def load_index() -> dict:
    if not INDEX_FILE.exists():
        return _empty_index()
    try:
        data = json.loads(INDEX_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_index()
    data.setdefault("by_product_id", {})
    data.setdefault("by_norm_title", {})
    return data


def save_index(index: dict) -> None:
    INDEX_FILE.parent.mkdir(exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2))


# --- read side: card-level filter -------------------------------------------

MIN_NORM_LEN = 6  # below this, normalized title is too generic to trust


def card_likely_dupe(norm_title: str, index: dict, days: int = DEDUPE_DAYS) -> str:
    """Return matching product_id if this normalized title was seen recently,
    else empty string. Caller decides whether to skip."""
    if not norm_title or len(norm_title) < MIN_NORM_LEN:
        return ""
    pids = index.get("by_norm_title", {}).get(norm_title, [])
    if not pids:
        return ""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    by_pid = index.get("by_product_id", {})
    for pid in pids:
        entry = by_pid.get(pid, {})
        if entry.get("last_seen", "") >= cutoff:
            return pid
    return ""


# --- write side: enrich-time index update -----------------------------------

def _key_for(rec: dict) -> str:
    """Canonical key: prefer product_id, fall back to short URL.
    Records without either are skipped."""
    pid = rec.get("product_id")
    if pid:
        return str(pid)
    url = rec.get("url", "")
    return f"url:{url}" if url else ""


def update_index_from_records(records: list[dict], index: dict | None = None) -> dict:
    """Merge a batch of enriched records into the index. Records need
    title (or title_exact) + url, ideally product_id. Falls back to URL
    when product_id is absent. Returns the updated (and saved) index."""
    if index is None:
        index = load_index()
    today = datetime.now().strftime("%Y-%m-%d")
    for rec in records:
        pid = _key_for(rec)
        if not pid:
            continue
        raw = rec.get("title_exact") or rec.get("title") or ""
        norm = normalize_title(raw)
        entry = index["by_product_id"].setdefault(pid, {
            "titles_normalized": [],
            "raw_titles": [],
            "first_seen": today,
        })
        if norm and norm not in entry["titles_normalized"]:
            entry["titles_normalized"].append(norm)
            bucket = index["by_norm_title"].setdefault(norm, [])
            if pid not in bucket:
                bucket.append(pid)
        if raw and raw not in entry["raw_titles"]:
            entry["raw_titles"].append(raw)
        entry["last_seen"] = today
        if rec.get("url"):
            entry["url"] = rec["url"]
        if rec.get("sales_exact") is not None:
            entry["sales_last"] = rec["sales_exact"]
    save_index(index)
    return index


# --- CLI --------------------------------------------------------------------

def _cmd_rebuild() -> int:
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ENRICHED
    if not path.exists():
        print(f"input not found: {path}", file=sys.stderr)
        return 1
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    index = _empty_index()
    update_index_from_records(records, index)
    print(f"rebuilt index from {len(records)} records → {INDEX_FILE}")
    _print_stats(index)
    return 0


def _cmd_stats() -> int:
    index = load_index()
    _print_stats(index)
    return 0


def _print_stats(index: dict) -> None:
    pids = index.get("by_product_id", {})
    norms = index.get("by_norm_title", {})
    if not pids:
        print("(empty)")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=DEDUPE_DAYS)).strftime("%Y-%m-%d")
    recent = sum(1 for e in pids.values() if e.get("last_seen", "") >= cutoff)
    print(f"products:        {len(pids)}")
    print(f"normalized keys: {len(norms)}")
    print(f"recent ({DEDUPE_DAYS}d): {recent}")
    collisions = sum(1 for v in norms.values() if len(v) > 1)
    print(f"norm collisions: {collisions} (same norm → multiple product_ids)")


def _cmd_check() -> int:
    if len(sys.argv) < 3:
        print("usage: dedupe.py check <title>", file=sys.stderr)
        return 1
    raw = sys.argv[2]
    norm = normalize_title(raw)
    index = load_index()
    pid = card_likely_dupe(norm, index)
    print(f"raw:  {raw}")
    print(f"norm: {norm}")
    if pid:
        entry = index["by_product_id"].get(pid, {})
        print(f"DUPE  → product_id={pid} last_seen={entry.get('last_seen')}")
    else:
        print("not in index (would be collected)")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "rebuild":
        sys.exit(_cmd_rebuild())
    if cmd == "stats":
        sys.exit(_cmd_stats())
    if cmd == "check":
        sys.exit(_cmd_check())
    print(f"unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)
