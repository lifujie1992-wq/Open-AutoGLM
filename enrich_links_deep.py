#!/usr/bin/env python3
"""Deep enrich: render each short link in headless Chrome to extract the
full image bundle (main images + detail long-images) — beyond what the
short-link redirect's goods_detail JSON can give (which is only 1 thumb).

SKU list (color × size × per-SKU price) is NOT in the initial DOM — it's
lazy-loaded behind the "选规格" tap. That needs a CDP click loop; we
don't do it here.

Categories of image URLs:
- main_images:   800x800 product photos (ecom-shop-material .._www800-800)
- detail_images: 790xNNN long detail strips (ecom-shop-material .._www790-)
- banner_images: 1080-wide promo/banner
- decoration:    system PNGs (bangdan / tplv decorations) — filtered out
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_IN = ROOT / "results" / "u2_links.enriched.jsonl"
DEFAULT_OUT = ROOT / "results" / "u2_links.full.jsonl"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/17.0 Mobile/15E148 Safari/604.1")

# Match ecombdimg.com image URLs (skip svg / decoration tos-cn-i-* / generic icons)
IMG_RE = re.compile(r"https?://[a-zA-Z0-9_./~?=&-]*ecombdimg\.com[a-zA-Z0-9_./~?=&-]*")
SHOP_MAT_RE = re.compile(r"ecom-shop-material/(?P<fmt>\w+)_m_(?P<hash>[0-9a-f]+)_sx_\d+_www(?P<w>\d+)-(?P<h>\d+)")


def render_dom(url: str, profile_dir: str, time_budget_ms: int = 40000) -> str:
    """Headless render the URL and return the post-JS DOM HTML."""
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={profile_dir}",
        f"--user-agent={UA}",
        f"--virtual-time-budget={time_budget_ms}",
        "--dump-dom",
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=time_budget_ms / 1000 + 20)
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""


def classify_image(url: str) -> str | None:
    """Return 'main' / 'detail' / 'banner' or None to skip."""
    if "tos-cn-i-" in url and "ecom-shop-material" not in url:
        return None  # system decoration
    m = SHOP_MAT_RE.search(url)
    if not m:
        # other ecom-shop-material variants (resize / origin) — still useful
        if "ecom-shop-material" in url and "_www" in url:
            return "detail"
        return None
    w = int(m.group("w"))
    h = int(m.group("h"))
    if w == 800 and h == 800:
        return "main"
    if w == 790:
        return "detail"
    if w >= 1000 or h >= 1000:
        return "banner"
    return "detail"


def deep_enrich(short_url: str, profile_dir: str) -> dict:
    """Render the short link page and return categorized image lists."""
    html = render_dom(short_url, profile_dir)
    if not html:
        return {"main_images": [], "detail_images": [], "banner_images": []}
    seen = set()
    main, detail, banner = [], [], []
    for m in IMG_RE.finditer(html):
        u = m.group(0).rstrip(",.;\"'")
        # de-dup by underlying hash (same image served via diff CDN nodes)
        sm = SHOP_MAT_RE.search(u)
        key = sm.group("hash") if sm else u
        if key in seen:
            continue
        seen.add(key)
        # Normalize CDN suffix: the rendered DOM truncates "~tplv-…-resize_q"
        # which 404s. Replace any tplv suffix with the stable -image.png form.
        u = re.sub(r"~tplv-[a-z0-9-]+(?:-[a-z_]+)?(\.[a-z]+)?$",
                   "~tplv-5mmsx3fupr-image.png", u)
        kind = classify_image(u)
        if kind == "main":
            main.append(u)
        elif kind == "banner":
            banner.append(u)
        elif kind == "detail":
            detail.append(u)
    return {
        "main_images": main,
        "detail_images": detail,
        "banner_images": banner,
    }


def main(in_path: Path, out_path: Path, limit: int | None = None) -> int:
    if not in_path.exists():
        print(f"input not found: {in_path}", file=sys.stderr)
        return 1
    records = [json.loads(l) for l in in_path.read_text().splitlines() if l.strip()]
    if limit is not None:
        records = records[:limit]
    print(f"deep-enriching {len(records)} records → {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cprof_") as profile_dir, \
         out_path.open("w") as f:
        for i, rec in enumerate(records, 1):
            short = rec.get("url", "")
            print(f"[{i:>2}/{len(records)}] {rec.get('title','')[:38]}")
            t0 = time.time()
            deep = deep_enrich(short, profile_dir) if short else {}
            dt = time.time() - t0
            merged = {**rec, **deep}
            f.write(json.dumps(merged, ensure_ascii=False) + "\n")
            print(f"    main={len(deep.get('main_images', []))} "
                  f"detail={len(deep.get('detail_images', []))} "
                  f"banner={len(deep.get('banner_images', []))} "
                  f"({dt:.1f}s)")
    print(f"done → {out_path}")
    return 0


if __name__ == "__main__":
    in_p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_IN
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else None
    sys.exit(main(in_p, out_p, lim))
