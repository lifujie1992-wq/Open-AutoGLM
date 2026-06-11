#!/usr/bin/env python3
"""Enrich u2_links.jsonl with main_image, product_id, exact price/sales by
following each short link's redirect chain and parsing goods_detail JSON
from the final URL's query string.

Zero reverse-engineering: the redirect target itself carries the payload.
For detail-image lists / SKUs, use the future headless-Chrome enricher
(needs JS execution to populate the SPA).

Usage:
    python enrich_links.py [input.jsonl] [output.jsonl]
    # defaults: results/u2_links.jsonl -> results/u2_links.enriched.jsonl
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

import requests

import dedupe

ROOT = Path(__file__).resolve().parent
DEFAULT_IN = ROOT / "results" / "u2_links.jsonl"
DEFAULT_OUT = ROOT / "results" / "u2_links.enriched.jsonl"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/17.0 Mobile/15E148 Safari/604.1")


def follow_short_link(url: str, timeout: float = 12.0) -> str | None:
    """Return the final URL after all redirects (or None on error)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         allow_redirects=True, timeout=timeout)
        return r.url
    except Exception as e:
        print(f"  ! follow failed: {e}", file=sys.stderr)
        return None


def parse_goods_payload(final_url: str) -> dict:
    """Extract product_id + goods_detail JSON from the final URL's query."""
    out: dict = {}
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
    except Exception:
        return out
    # product_id may live in top-level qs or only inside nested detail_schema.
    # 'id' is a reliable top-level fallback (same numeric value).
    pid = qs.get("product_id", [None])[0] or qs.get("id", [None])[0]
    if pid:
        out["product_id"] = pid
    promo = qs.get("promotion_id", [None])[0]
    if promo:
        out["promotion_id"] = promo
    elif pid:
        out["promotion_id"] = pid  # promotion == product for non-promo items
    if "goods_detail" in qs:
        try:
            gd = json.loads(qs["goods_detail"][0])
            out["title_exact"] = gd.get("title", "")
            out["sales_exact"] = gd.get("sales")
            mn, mx = gd.get("min_price"), gd.get("max_price")
            if mn is not None:
                out["price_min_cny"] = mn / 100  # API in fen
            if mx is not None:
                out["price_max_cny"] = mx / 100
            img = gd.get("img") or {}
            urls = img.get("url_list") or []
            if urls:
                out["main_image"] = urls[0]
                out["main_image_alt"] = urls[1] if len(urls) > 1 else ""
        except Exception as e:
            print(f"  ! goods_detail parse failed: {e}", file=sys.stderr)
    return out


def enrich(record: dict) -> dict:
    short = record.get("url", "")
    if not short:
        return record
    final = follow_short_link(short)
    if not final:
        return record
    payload = parse_goods_payload(final)
    record = dict(record)
    record.update(payload)  # final_url omitted (4KB+ noise); product_id is enough
    return record


def main(in_path: Path, out_path: Path) -> int:
    if not in_path.exists():
        print(f"input not found: {in_path}", file=sys.stderr)
        return 1
    records = [json.loads(l) for l in in_path.read_text().splitlines() if l.strip()]
    print(f"enriching {len(records)} records → {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    enriched_records: list[dict] = []
    with out_path.open("w") as f:
        for i, rec in enumerate(records, 1):
            print(f"[{i:>2}/{len(records)}] {rec.get('title','')[:38]}")
            enriched = enrich(rec)
            mi = enriched.get("main_image")
            if mi:
                print(f"    img: {mi[:100]}")
            pid = enriched.get("product_id")
            if pid:
                print(f"    product_id: {pid}")
            sales = enriched.get("sales_exact")
            if sales is not None:
                print(f"    sales_exact: {sales}")
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")
            enriched_records.append(enriched)
            time.sleep(0.5)  # polite delay
    print(f"done → {out_path}")
    # update dedupe index so tomorrow's u2_shoes.py run can skip these at the card level
    indexed = sum(1 for r in enriched_records if r.get("product_id"))
    dedupe.update_index_from_records(enriched_records)
    print(f"dedupe index: merged {indexed} product_ids → {dedupe.INDEX_FILE}")
    return 0


if __name__ == "__main__":
    in_p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_IN
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    sys.exit(main(in_p, out_p))
