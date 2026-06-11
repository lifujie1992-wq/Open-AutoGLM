#!/usr/bin/env python3
"""Collect Douyin Mall women's-shoes share links via uiautomator2 (a11y tree).

10-100x faster than the VLM-based orchestrator: tap by element description,
read text directly, no per-step inference.

Pre-conditions: phone on 抖音商城 → 搜索"女鞋" → 结果列表页(ECSearchActivity).
scrcpy running for clipboard sync. Mac clipboard reachable via pbpaste.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import uiautomator2 as u2

import account_pool as ap
import dedupe
import feishu_push
from brand_match import match_brand, match_brand_in_tags

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LINKS_FILE = RESULTS_DIR / "u2_links.jsonl"
LOG_FILE = RESULTS_DIR / "u2.log"

TARGET = int(os.environ.get("TARGET", "20"))
MIN_SALES = int(os.environ.get("MIN_SALES", "40"))
MAX_REVIEW_AGE_DAYS = int(os.environ.get("MAX_REVIEW_AGE_DAYS", "3"))
SEARCH_QUERY = os.environ.get("SEARCH_QUERY", "女鞋")

URL_RE = re.compile(r"https?://v\.douyin\.com/\S+")
SALES_RE = re.compile(r"已售\s*(\d+)\+?件?")
ZW = "​"


# --- humanization helpers: jittered click/swipe/sleep + paced rests ----------

def hsleep(base: float, jitter: float = 0.35) -> None:
    """Sleep base seconds * uniform(1-jitter, 1+jitter)."""
    lo = max(0.05, base * (1 - jitter))
    hi = base * (1 + jitter)
    time.sleep(random.uniform(lo, hi))


def hclick(d: u2.Device, x: int, y: int, dx: int = 14, dy: int = 14) -> None:
    """Click with up-to-(dx,dy) pixel jitter and a brief pre-click pause."""
    ox = x + random.randint(-dx, dx)
    oy = y + random.randint(-dy, dy)
    time.sleep(random.uniform(0.08, 0.22))
    d.click(ox, oy)


def hback(d: u2.Device) -> None:
    time.sleep(random.uniform(0.18, 0.45))
    d.press("back")


def hswipe(d: u2.Device, x1: int, y1: int, x2: int, y2: int,
           duration: float = 0.4) -> None:
    """Swipe with random end-point offset and randomized duration."""
    j = 22
    x1o = x1 + random.randint(-j, j)
    y1o = y1 + random.randint(-j, j)
    x2o = x2 + random.randint(-j, j)
    y2o = y2 + random.randint(-j, j)
    dur = duration * random.uniform(0.75, 1.45)
    d.swipe(x1o, y1o, x2o, y2o, duration=dur)


def take_a_break(reason: str, low: float, high: float) -> None:
    """Pause to look more human."""
    pause = random.uniform(low, high)
    log(f"    [pace] {reason}: rest {pause:.1f}s")
    time.sleep(pause)


def pre_action_scroll(d: u2.Device) -> None:
    """Sometimes peek-scroll the page before the next decision (more humanlike)."""
    if random.random() < 0.55:
        # small scroll, sometimes upward (going back to peek)
        if random.random() < 0.3:
            hswipe(d, 540, 900, 540, 1500, duration=0.5)  # scroll up a bit
        else:
            hswipe(d, 540, 1500, 540, 1000, duration=0.5)  # gentle scroll down
        hsleep(0.7)


def humanize_detail_browse(d: u2.Device) -> None:
    """Right after entering a product detail page: browse images/long-detail/specs
    like a real shopper would. Lower the 'tap → reviews → share' robot signature
    that triggers risk control."""
    n_swipes = random.randint(2, 5)
    for i in range(n_swipes):
        hswipe(d, 540, 1700, 540, 700, duration=random.uniform(0.45, 0.85))
        hsleep(random.uniform(1.4, 4.2))  # dwell — "reading" each section
        # occasional small back-scroll like re-checking a photo
        if random.random() < 0.25:
            hswipe(d, 540, 700, 540, 1300, duration=0.5)
            hsleep(random.uniform(0.8, 1.8))


def humanize_review_browse(d: u2.Device) -> None:
    """Inside the reviews sheet: scroll a couple of reviews before reading the
    top date. Real shoppers don't open reviews and immediately leave."""
    n_swipes = random.randint(1, 3)
    for _ in range(n_swipes):
        hswipe(d, 540, 1600, 540, 900, duration=random.uniform(0.4, 0.7))
        hsleep(random.uniform(1.0, 2.8))
    # scroll back to the top so top_review_date can find the latest
    for _ in range(n_swipes + 1):
        hswipe(d, 540, 900, 540, 1600, duration=0.4)
        hsleep(0.4)

RECENT_RE = re.compile(
    r"(\d+)\s*(?:分钟|小时|天|秒)前|刚刚|今日|今天|昨日|昨天|前天"
)
DATE_ONLY_RE = re.compile(
    r"^\s*(?:刚刚|今日|今天|昨日|昨天|前天|\d+\s*(?:分钟|小时|天|秒)前)\s*$"
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def pbpaste() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def pbcopy(s: str) -> None:
    subprocess.run(["pbcopy"], input=s, text=True)


def parse_sales(text: str) -> int:
    m = SALES_RE.search(text)
    return int(m.group(1)) if m else 0


def is_recent(text: str) -> bool:
    """Check if a date string indicates within MAX_REVIEW_AGE_DAYS days."""
    if not text:
        return False
    if "刚刚" in text or "今日" in text or "今天" in text:
        return True
    if "昨日" in text or "昨天" in text:
        return MAX_REVIEW_AGE_DAYS >= 1
    if "前天" in text:
        return MAX_REVIEW_AGE_DAYS >= 2
    m = re.search(r"(\d+)\s*(分钟|小时|天|秒)前", text)
    if not m:
        return False
    n, unit = int(m.group(1)), m.group(2)
    if unit in ("秒", "分钟", "小时"):
        return True
    if unit == "天":
        return n <= MAX_REVIEW_AGE_DAYS
    return False


def clean_title(s: str) -> str:
    """Strip the zero-width spaces that Douyin sprinkles between digits."""
    return s.replace(ZW, "").lstrip("I").strip()


def load_seen() -> tuple[set, set]:
    if not LINKS_FILE.exists():
        return set(), set()
    urls, titles = set(), set()
    for line in LINKS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        urls.add(rec.get("url", ""))
        titles.add(rec.get("title", ""))
    return urls, titles


def list_cards(d: u2.Device) -> list[dict]:
    """Return visible product cards with title, sales, and click center."""
    from lxml import etree
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode())
    cards = []
    for sold_el in root.xpath('//*[starts-with(@content-desc, "已售")]'):
        # Walk up to find the card-sized container (~520x800)
        parent = sold_el.getparent()
        card_bounds = None
        while parent is not None:
            b = parent.attrib.get("bounds", "")
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                w, h = x2 - x1, y2 - y1
                if 400 < w < 600 and 700 < h < 900:
                    card_bounds = (x1, y1, x2, y2)
                    break
            parent = parent.getparent()
        if not card_bounds:
            continue
        # Title element inside card
        title = ""
        for cand in root.iter():
            cb = cand.attrib.get("bounds", "")
            cm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", cb)
            if not cm:
                continue
            cx1, cy1, cx2, cy2 = map(int, cm.groups())
            if (cx1 >= card_bounds[0] and cy1 >= card_bounds[1]
                and cx2 <= card_bounds[2] and cy2 <= card_bounds[3]):
                desc = cand.attrib.get("content-desc", "")
                # Title is the longest desc that isn't 已售/月上新
                if desc and not desc.startswith(("已售", "月上新")) and len(desc) > len(title):
                    title = desc
        x1, y1, x2, y2 = card_bounds
        cards.append({
            "title": clean_title(title),
            "sales": parse_sales(sold_el.attrib.get("content-desc", "")),
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "bounds": card_bounds,
        })
    return cards


SKU_RE = re.compile(r"^(?:已选择|未选择)(.+?)¥(\d+(?:\.\d+)?)$")
SIZE_RE = re.compile(r"^\d{2,3}(?:\.\d)?$")  # shoe sizes 35-46, or numbers like 38.5


def _parse_sku_sheet(d: u2.Device) -> dict:
    """Parse one a11y snapshot of the currently-open SKU sheet."""
    from lxml import etree
    out: dict = {"skus": [], "sizes": [], "selected_sku": ""}
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode())
    sku_names_seen: set[str] = set()
    sizes_seen: set[str] = set()
    for el in root.iter():
        cd = (el.attrib.get("content-desc") or "").strip()
        if cd:
            m = SKU_RE.match(cd)
            if m:
                name = m.group(1).strip()
                price = float(m.group(2))
                if name not in sku_names_seen:
                    sku_names_seen.add(name)
                    out["skus"].append({
                        "name": name,
                        "price_cny": price,
                        "selected": cd.startswith("已选择"),
                    })
            if cd.startswith("已选 ") and not out["selected_sku"]:
                out["selected_sku"] = cd[len("已选 "):].strip()
        t = (el.attrib.get("text") or "").strip()
        if t and SIZE_RE.match(t):
            b = el.attrib.get("bounds", "")
            bm = re.match(r"\[(\d+),(\d+)\]", b)
            clickable = el.attrib.get("clickable") == "true"
            if bm and clickable and int(bm.group(2)) >= 1400:
                if t not in sizes_seen:
                    sizes_seen.add(t)
                    out["sizes"].append(t)
    return out


def extract_sku_table(d: u2.Device) -> dict:
    """Open the SKU sheet (共X款 button) and dump every spec option with price.
    Retries the dump up to 4 times so a slow-rendering sheet still gets caught.
    Side effect: leaves you back at the product detail page."""
    out: dict = {"skus": [], "sizes": [], "selected_sku": ""}
    if not d(textMatches=r"共\d+款").exists:
        return out
    d(textMatches=r"共\d+款").click()
    hsleep(2.0)
    # Dismiss the "服务授权 / 下次再说" dialog
    for txt in ("下次再说", "暂不授权", "取消"):
        if d(text=txt).exists:
            d(text=txt).click()
            hsleep(0.8)
            break
    # Re-tap if the dialog blocked the first open
    if not d(textContains="颜色分类").exists and not d(textContains="规格").exists:
        if d(textMatches=r"共\d+款").exists:
            d(textMatches=r"共\d+款").click()
            hsleep(1.8)
    # Retry up to 4 times — sheet may render slowly (SPA cold-load)
    for attempt in range(4):
        snapshot = _parse_sku_sheet(d)
        if snapshot["skus"]:
            out = snapshot
            break
        if snapshot["selected_sku"] and not out["selected_sku"]:
            out["selected_sku"] = snapshot["selected_sku"]
        hsleep(1.5)
    # Close the sheet
    hback(d)
    hsleep(0.8)
    return out


def extract_detail_info(d: u2.Device) -> dict:
    """Pull price/sales/tags/attrs from the open product detail page.
    Returns a dict suitable for merging into the result record.
    All fields are best-effort (missing → empty)."""
    from lxml import etree
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode())
    info = {
        "price_strike": "",
        "price_now": "",
        "sales_detail": "",
        "tags": [],
        "attrs": {},
        "sku_count": "",
        "long_title": "",
    }
    # Collect (y1, text) for nodes in livelite package
    nodes = []
    for el in root.iter():
        if el.attrib.get("package") != "com.ss.android.ugc.livelite":
            continue
        t = (el.attrib.get("text") or "").strip()
        if not t:
            continue
        b = el.attrib.get("bounds", "")
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if y1 > 2100:  # bottom bar
            continue
        nodes.append((y1, x1, t))
    nodes.sort()

    # Price: split price fragments by ¥ markers, group by proximity in x.
    price_frags = sorted(
        (x, t) for y, x, t in nodes
        if 1200 <= y <= 1290 and (t == "¥" or t.replace(".", "").isdigit())
    )
    prices: list[str] = []
    current = ""
    last_x = -10**6
    for x, t in price_frags:
        if t == "¥" or x - last_x > 120:
            if current.startswith("¥") and len(current) > 1:
                prices.append(current)
            current = t if t == "¥" else "¥" + t
        else:
            current += t
        last_x = x
    if current.startswith("¥") and len(current) > 1:
        prices.append(current)
    if prices:
        info["price_strike"] = prices[0]
        info["price_now"] = prices[-1] if len(prices) > 1 else prices[0]

    # sales_detail and 首单补贴
    for _, _, t in nodes:
        if t.startswith("已售") and not info["sales_detail"]:
            info["sales_detail"] = t
        if t.startswith("共") and t.endswith("款"):
            info["sku_count"] = t

    # long_title (the one starting with 'v' or matching shoe-title pattern, y~1575)
    for y, _, t in nodes:
        if 1530 <= y <= 1620 and len(t) > 15:
            info["long_title"] = t.lstrip("v").strip()
            break

    # tags row (y~1744): 运费险/热搜.../好评率X%
    tag_y_lo, tag_y_hi = 1700, 1800
    info["tags"] = [t for y, _, t in nodes if tag_y_lo <= y <= tag_y_hi]

    # Attribute table: pairs of (value, label) at y~1854 and y~1913
    val_row = [(x, t) for y, x, t in nodes if 1840 <= y <= 1870]
    lbl_row = [(x, t) for y, x, t in nodes if 1900 <= y <= 1930]
    val_row.sort(); lbl_row.sort()
    for (vx, val), (lx, lab) in zip(val_row, lbl_row):
        info["attrs"][lab] = val

    return info


def open_review_detail(d: u2.Device) -> bool:
    """From product detail page, scroll until the 商品评价 row appears, then tap
    it to open the reviews sheet. Returns False if 商品评价 never appears
    (product has no reviews)."""
    from lxml import etree
    for _ in range(8):
        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode())
        for el in root.iter():
            if (el.attrib.get("text") or "").strip() == "商品评价":
                b = el.attrib.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if not m:
                    continue
                x1, y1, x2, y2 = map(int, m.groups())
                hclick(d, 540, (y1 + y2) // 2)
                hsleep(2.0)
                return True
        hswipe(d, 540, 1700, 540, 700, duration=0.3)
        hsleep(0.6)
    return False


def sort_by_latest(d: u2.Device) -> bool:
    """In the open review sheet, tap the 最新 sort button."""
    try:
        el = d.xpath('//*[@text="最新" and @clickable="true"]').get(timeout=2)
    except Exception:
        return False
    if not el:
        return False
    hsleep(0.25)
    el.click()
    hsleep(1.4)
    return True


def top_review_date(d: u2.Device) -> str | None:
    """After sorting by latest, find the topmost (smallest y) date string in
    the review list area (below the sort row)."""
    from lxml import etree
    for _ in range(3):
        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode())
        dates = []
        for el in root.iter():
            t = (el.attrib.get("text") or "").strip()
            if not t or not DATE_ONLY_RE.match(t):
                continue
            b = el.attrib.get("bounds", "")
            m = re.match(r"\[(\d+),(\d+)\]", b)
            if not m:
                continue
            y = int(m.group(2))
            if y > 700:  # below the sort buttons row
                dates.append((y, t))
        if dates:
            dates.sort()
            return dates[0][1]
        hsleep(0.6)
    return None


def share_and_copy(d: u2.Device) -> str | None:
    """Tap 分享 → 复制链接, then read mac clipboard."""
    sentinel = f"SENTINEL_{int(time.time()*1000)}"
    pbcopy(sentinel)

    try:
        share = d.xpath('//*[@content-desc="分享"]').get(timeout=2)
    except Exception:
        share = None
    if not share:
        log("  no 分享 button found")
        return None
    hsleep(0.3)
    share.click()
    hsleep(1.4)

    try:
        copy = d.xpath('//*[@text="复制链接" or @content-desc="复制链接"]').get(timeout=3)
    except Exception:
        copy = None
    if not copy:
        log("  no 复制链接 button in share panel")
        hback(d)
        return None
    hsleep(0.25)
    copy.click()
    hsleep(1.8)
    # If the share panel didn't auto-dismiss (still see 复制链接 button), close it.
    if d.xpath('//*[@text="复制链接"]').exists:
        hback(d)
        hsleep(0.5)

    clip = pbpaste()
    if clip == sentinel or sentinel in clip:
        log("  clipboard not updated")
        return None
    return clip.strip()


RISK_SENTINEL = {"_risk": True}


NOISE_TITLES = {
    "", "券后价", "优惠价", "大促价", "原价", "现价", "降价", "立减",
    "到手价", "活动价", "限时价",
}


def _is_noise_title(t: str) -> bool:
    """A 'title' that's actually just a price-label leaking from the card UI.
    The new Douyin Mall search page hides the real product title from the
    a11y tree — card title is unreliable until we tap into detail."""
    if not t or t in NOISE_TITLES:
        return True
    return len(t.strip()) < 4


def card_skip_reason(
    card: dict, seen_titles: set, dedupe_index: dict | None
) -> str:
    """Cheap pre-tap filters. Return skip reason or '' if card is worth tapping.
    When card title is noise (price label), skip title-based filters and rely
    on detail-page brand + URL dedupe instead."""
    if card["sales"] < MIN_SALES:
        return f"low-sales {card['sales']}<{MIN_SALES}"
    if _is_noise_title(card["title"]):
        return ""  # tap and let detail-page filters decide
    if card["title"] in seen_titles:
        return "title seen this run"
    if dedupe_index is not None:
        norm = dedupe.normalize_title(card["title"])
        dup_pid = dedupe.card_likely_dupe(norm, dedupe_index)
        if dup_pid:
            return f"dedupe product_id={dup_pid}"
    brand = match_brand(card["title"])
    if brand:
        return f"brand={brand} (card title)"
    return ""


_CLIP_TITLE_RE = re.compile(
    r"【抖音商城】https?://v\.douyin\.com/\S+\s+(.+?)(?:\n|$)"
)


def title_from_clip(clip: str) -> str:
    """Pull the real product title out of the share-clip string Douyin generates.
    Format: '...【抖音商城】https://v.douyin.com/XXX/ REAL TITLE\\n长按复制...'"""
    if not clip:
        return ""
    m = _CLIP_TITLE_RE.search(clip)
    return m.group(1).strip() if m else ""


def collect_from_card(d: u2.Device, card: dict, seen_urls: set, seen_titles: set) -> dict | None:
    """Tap into detail and try to collect. Assumes card already passed
    card_skip_reason(). Returns dict on success, None on detail-side failure,
    RISK_SENTINEL on silent risk."""
    log(f"  > '{card['title'][:40]}' sales={card['sales']}")
    hclick(d, *card["center"])
    hsleep(2.4)
    if d.app_current().get("activity", "").endswith("ECSearchActivity"):
        log("    failed to enter product page")
        return None

    # Silent risk control: detail loaded but content blanked, or error overlay.
    # Bubble up a sentinel so main() can rotate accounts instead of just skipping.
    risk = is_detail_risk_blocked(d)
    if risk:
        log(f"    !!! silent risk on detail: {risk}")
        hback(d)
        return RISK_SENTINEL

    # Browse the page like a real shopper would (image scroll, dwell, occasional
    # back-scroll). Reduces the 'tap → reviews → share' robotic signature.
    humanize_detail_browse(d)

    # Detail-page brand filter (tags + long_title). Read-only; zero extra
    # interaction beyond one a11y dump. Many brands only put their name in the
    # '品牌授权' chip on the detail page, not in the card title.
    detail_info = extract_detail_info(d)
    detail_brand = (
        match_brand_in_tags(detail_info.get("tags", []))
        or match_brand(detail_info.get("long_title", ""))
    )
    if detail_brand:
        log(f"    skip (brand: {detail_brand} from detail)")
        hback(d)
        return None

    # human peek before deciding
    pre_action_scroll(d)

    if not open_review_detail(d):
        log("    no 商品评价 section on this product")
        hback(d)
        return None
    if not sort_by_latest(d):
        log("    no 最新 sort button")
        hback(d); hsleep(0.4); hback(d)
        return None
    # browse some reviews before checking the top date (less robotic)
    humanize_review_browse(d)
    top_date = top_review_date(d)
    log(f"    top review date: {top_date!r}")
    if not top_date or not is_recent(top_date):
        log("    latest review too old")
        hback(d); hsleep(0.4); hback(d)
        return None

    # Close the reviews sheet so 分享 in the page header is reachable
    hback(d)
    hsleep(0.9)
    clip = share_and_copy(d)
    if not clip:
        hback(d)
        return None
    m = URL_RE.search(clip)
    if not m:
        log(f"    no douyin URL in clip: {clip[:80]!r}")
        hback(d)
        return None
    url = m.group(0).rstrip(",.!?。， )]")
    if url in seen_urls:
        log("    dup url")
        hback(d)
        return None

    hback(d)
    # prefer the real title from share-clip over the card-level price-label noise
    real_title = title_from_clip(clip) or card["title"]
    return {
        "title": real_title,
        "sales": card["sales"],
        "url": url,
        "review_hint": top_date,
        "clip": clip,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


SEARCH_BOX_TAP = (int(os.environ.get("SEARCH_BOX_X", "280")), int(os.environ.get("SEARCH_BOX_Y", "200")))
SEARCH_BTN_TAP = (int(os.environ.get("SEARCH_BTN_X", "972")), int(os.environ.get("SEARCH_BTN_Y", "200")))


def has_captcha(d: u2.Device) -> bool:
    """Detect Douyin's risk-control / captcha overlay."""
    for token in ("开始检测", "为保障账号安全", "请进行验证", "完成验证", "拖动滑块"):
        if d.xpath(f'//*[contains(@text, "{token}")]').exists:
            return True
    return False


# --- silent risk control: detail page blanked or error overlay ---------------

ERROR_OVERLAY_TOKENS = (
    "网络异常", "加载失败", "服务器繁忙", "请稍后再试",
    "重新加载", "暂无数据", "访问受限", "操作过于频繁",
    "今日浏览次数已达上限", "出错了",
)


def has_error_overlay(d: u2.Device) -> str:
    """Return the first error-overlay token seen on screen, or ''."""
    for token in ERROR_OVERLAY_TOKENS:
        if d.xpath(f'//*[contains(@text, "{token}")]').exists:
            return token
    return ""


def is_detail_blank(d: u2.Device) -> bool:
    """Detail page opened but content didn't render.

    A real detail page has at least one of: '已售X' badge, '¥' price marker,
    '共X款' SKU button, or 分享 button. If after settle we see none of these,
    risk control most likely blanked the page.
    """
    if d.xpath('//*[starts-with(@content-desc, "已售")]').exists:
        return False
    if d.xpath('//*[@content-desc="分享"]').exists:
        return False
    if d.xpath('//*[contains(@text, "¥")]').exists:
        return False
    if d.xpath('//*[contains(@text, "共") and contains(@text, "款")]').exists:
        return False
    return True


def is_detail_risk_blocked(d: u2.Device, settle_s: float = 2.5) -> str:
    """After tapping into a detail page, decide if it was silently risk-blocked.

    Returns the reason string ('error:网络异常', 'blank', etc.) or '' if healthy.
    Waits up to settle_s for the page to render before deciding.
    """
    deadline = time.time() + settle_s
    while time.time() < deadline:
        token = has_error_overlay(d)
        if token:
            return f"error:{token}"
        if not is_detail_blank(d):
            return ""
        time.sleep(0.4)
    # final check: still no content after settle window
    token = has_error_overlay(d)
    if token:
        return f"error:{token}"
    if is_detail_blank(d):
        return "blank"
    return ""


# --- account rotation --------------------------------------------------------

def _back_to_results(d: u2.Device, max_back: int = 5) -> None:
    """Pop back until we're at the search-results page (>=2 sold tags)."""
    for _ in range(max_back):
        if _sold_count(d) >= 2:
            return
        hback(d)
        hsleep(0.6)
        dismiss_popups(d)


def wait_for_screen_quiet(d: u2.Device, timeout_s: float = 60.0) -> bool:
    """Block until the user has finished switching accounts.

    Heuristic: search-results cards are visible AND no captcha for 4s straight.
    Returns False on timeout.
    """
    stable_for = 0.0
    deadline = time.time() + timeout_s
    last = time.time()
    while time.time() < deadline:
        now = time.time()
        ok = (not has_captcha(d)) and _sold_count(d) >= 2
        stable_for = stable_for + (now - last) if ok else 0.0
        last = now
        if stable_for >= 4.0:
            return True
        time.sleep(0.8)
    return False


def trigger_account_rotation(d: u2.Device, pool: ap.Pool, reason: str) -> bool:
    """Park the current account, pick the next healthy one, pause for human
    to do the actual UI switch, then verify we're back on the results page.

    Returns True if a fresh account is active; False if the pool is exhausted.
    """
    current = ap.current_account(pool)
    cur_id = current.id if current else "(none)"
    log(f"!!! risk trigger on '{cur_id}': {reason}")
    if current:
        ap.mark_risk(pool, current.id)

    nxt = ap.pick_next(pool, exclude=cur_id)
    if not nxt:
        log("!!! account pool exhausted — no healthy accounts left")
        return False

    ap.set_current(pool, nxt.id)
    log(f"=== SWITCH ACCOUNT: '{cur_id}' -> '{nxt.id}' ===")
    log(f"    pool: {ap.summary(pool)}")
    log(f"    ACTION REQUIRED: switch the app to account '{nxt.id}' now")
    log(f"    waiting up to 60s for screen to settle on results page...")
    # try to get us back to a known surface so the user doesn't have to navigate
    _back_to_results(d)
    ok = wait_for_screen_quiet(d, timeout_s=60.0)
    if not ok:
        log("    timed out waiting for account switch; bailing")
        return False
    log(f"    [resumed on '{nxt.id}']")
    # warm-up pause so the new account doesn't get hit with a stampede
    take_a_break("post-switch warmup", 6.0, 14.0)
    return True


def is_livestream(d: u2.Device) -> bool:
    """Detect that we wandered into a livestream room from a card-tap."""
    # Activity-based hint (cheap)
    act = d.app_current().get("activity", "")
    if "Live" in act or "live" in act:
        return True
    # Element-based hints (only true inside live rooms)
    for token in ("送礼物", "进入直播间", "送出"):
        if d.xpath(f'//*[@text="{token}" or @content-desc="{token}"]').exists:
            return True
    if d.xpath('//*[@content-desc="礼物"]').exists and d.xpath('//*[@content-desc="说点什么..." or @text="说点什么..."]').exists:
        return True
    return False


def exit_livestream(d: u2.Device) -> bool:
    """Dwell briefly (look human) then close the live room and return to results."""
    take_a_break("livestream dwell", 2.5, 5.5)
    # 1) try explicit close button content-desc
    for cd in ("关闭", "关闭直播间", "退出直播间", "close"):
        try:
            el = d.xpath(f'//*[@content-desc="{cd}"]').get(timeout=1)
        except Exception:
            el = None
        if el:
            el.click()
            hsleep(1.2)
            if not is_livestream(d):
                return True
    # 2) fallback: tap top-right corner where Douyin's × usually sits
    hclick(d, 1010, 180, dx=22, dy=22)
    hsleep(1.0)
    if not is_livestream(d):
        return True
    # 3) last resort: back key
    hback(d)
    hsleep(0.8)
    return not is_livestream(d)


def dismiss_popups(d: u2.Device) -> int:
    """Close common Douyin promo/coupon/upgrade popups."""
    closed = 0
    for desc in ("关闭", "关闭按钮", "close"):
        if d.xpath(f'//*[@content-desc="{desc}"]').exists:
            d.xpath(f'//*[@content-desc="{desc}"]').click()
            hsleep(0.4)
            closed += 1
    for text in ("以后再说", "暂不升级", "暂不更新", "不升级", "不更新", "取消"):
        if d.xpath(f'//*[@text="{text}"]').exists:
            d.xpath(f'//*[@text="{text}"]').click()
            hsleep(0.4)
            closed += 1
    return closed


def _relaunch_app(d: u2.Device) -> None:
    subprocess.run(
        ["adb", "shell", "monkey", "-p", "com.ss.android.ugc.livelite",
         "-c", "android.intent.category.LAUNCHER", "1"],
        check=False, capture_output=True,
    )
    hsleep(5.0, jitter=0.15)
    dismiss_popups(d)


def _sold_count(d: u2.Device) -> int:
    """Count '已售X' nodes — search-results page has many; detail page has 1."""
    return len(d.xpath('//*[starts-with(@content-desc, "已售")]').all())


def _on_correct_search_results(d: u2.Device) -> bool:
    """True iff we're on the search results page for SEARCH_QUERY.

    Search results page signatures (both must hold):
      A. Filter chips '综合' AND '销量' are visible (search-only UI).
      B. The search bar's text == SEARCH_QUERY (rules out home recommendations
         where the bar shows a trending-suggestion placeholder via content-desc).
    """
    if not d.xpath('//*[@text="综合"]').exists:
        return False
    if not d.xpath('//*[@text="销量"]').exists:
        return False
    if SEARCH_QUERY and not d.xpath(f'//*[@text="{SEARCH_QUERY}"]').exists:
        return False
    return True


def _do_search(d: u2.Device) -> None:
    """Type SEARCH_QUERY into the search bar and submit."""
    hclick(d, *SEARCH_BOX_TAP)
    hsleep(0.9)
    subprocess.run(["adb", "shell", "ime", "set", "com.android.adbkeyboard/.AdbIME"],
                   check=False, capture_output=True)
    subprocess.run(["adb", "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
                   check=False, capture_output=True)
    hsleep(0.3)
    subprocess.run(
        ["adb", "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", SEARCH_QUERY],
        check=False, capture_output=True,
    )
    hsleep(0.8)
    hclick(d, *SEARCH_BTN_TAP)
    hsleep(3.0)
    dismiss_popups(d)


def ensure_search_results(d: u2.Device) -> bool:
    """Make sure we're on the search results page for SEARCH_QUERY.

    Distinguishes a real search-results page from the home recommendation feed
    (which also has 已售 badges and looks like results but isn't filtered by
    the search query). Re-searches if the query doesn't match.
    """
    # 1. Make sure the app is in foreground
    if d.app_current().get("package") != "com.ss.android.ugc.livelite":
        _relaunch_app(d)
    dismiss_popups(d)
    # 2. Already on the correct search results?
    if _on_correct_search_results(d):
        return True
    # 3. Back out toward home (or search-bar visible state)
    for _ in range(6):
        dismiss_popups(d)
        if _on_correct_search_results(d):
            return True
        # at home (search bar visible, no result cards) → stop backing out
        if d.xpath('//*[@content-desc="搜索"]').exists and _sold_count(d) <= 6:
            break
        hback(d)
        hsleep(0.8)
        if d.app_current().get("package") != "com.ss.android.ugc.livelite":
            _relaunch_app(d)
    # 4. Type the search and submit
    _do_search(d)
    if _on_correct_search_results(d):
        return True
    # 5. Last-ditch: maybe the query went into a different field; relaunch and retry
    log("search did not land on filter-chip page; relaunching app and retrying")
    _relaunch_app(d)
    _do_search(d)
    return _on_correct_search_results(d)


def _rotate_or_bail(d: u2.Device, pool: ap.Pool, reason: str) -> bool:
    """Wrapper that returns True if we successfully rotated, False to bail."""
    if not pool.accounts:
        log(f"!!! {reason} but no account pool configured (set ACC_IDS) — bailing")
        return False
    return trigger_account_rotation(d, pool, reason)


def main() -> int:
    log(f"=== start SEARCH={SEARCH_QUERY!r} TARGET={TARGET} MIN_SALES={MIN_SALES} MAX_REVIEW_AGE_DAYS={MAX_REVIEW_AGE_DAYS} ===")
    d = u2.connect()
    log(f"connected: {d.app_current()}")

    # warm-up: random delay before first action to avoid stampede signature
    warmup = random.uniform(2.0, 6.0)
    log(f"warmup pause {warmup:.1f}s")
    time.sleep(warmup)

    pool = ap.load_pool()
    if pool.accounts:
        cur = ap.current_account(pool)
        # if current is fatigued, pick a fresh one before we even start
        if cur and (cur.ops >= ap.DAILY_CAP or (cur.cooling_until and ap._is_cooling(cur))):
            nxt = ap.pick_next(pool, exclude=cur.id)
            if nxt:
                ap.set_current(pool, nxt.id)
        log(f"account pool: {ap.summary(pool)}")
    else:
        log("account pool: (none — set ACC_IDS to enable rotation)")

    seen_urls, seen_titles = load_seen()
    log(f"history: {len(seen_urls)} previously collected (used for dedupe)")
    # TARGET means "new items collected this run", not "total in jsonl"
    collected = 0

    dedupe_index = dedupe.load_index()
    n_dup_products = len(dedupe_index.get("by_product_id", {}))
    log(f"dedupe index: {n_dup_products} products (window={dedupe.DEDUPE_DAYS}d)")

    if has_captcha(d):
        log("!!! captcha already present at start")
        if not _rotate_or_bail(d, pool, "startup captcha"):
            return 3

    if os.environ.get("BYPASS_SEARCH_BOOTSTRAP") == "1":
        log("BYPASS_SEARCH_BOOTSTRAP=1 — skipping search-page check, trusting current page")
    elif not ensure_search_results(d):
        log("FATAL: cannot reach search results page")
        return 2

    scrolls = 0
    no_progress_scrolls = 0
    captured_in_session = 0
    last_long_rest_at = 0
    while collected < TARGET and no_progress_scrolls < 6 and scrolls < 50:
        # captcha guard: try to rotate accounts first
        if has_captcha(d):
            if not _rotate_or_bail(d, pool, "captcha overlay"):
                return 3
            ensure_search_results(d)
            continue
        # preemptive: current account hit its daily cap — switch before getting flagged
        cur = ap.current_account(pool)
        if cur and cur.ops >= ap.DAILY_CAP:
            log(f"    [pace] current account {cur.id} hit daily cap ({cur.ops})")
            if not _rotate_or_bail(d, pool, "daily cap reached"):
                log("    no fresh accounts; stopping")
                break
            ensure_search_results(d)
            continue
        # livestream guard: dwell then close
        if is_livestream(d):
            log("    [livestream] detected, dwelling then closing")
            exit_livestream(d)
            continue
        cards = list_cards(d)
        log(f"visible cards: {len(cards)}")
        if not cards:
            log("  no cards visible, re-bootstrapping search")
            if not ensure_search_results(d):
                log("  re-bootstrap failed, giving up")
                break
            no_progress_scrolls += 1
            continue
        progress_before = collected
        rotated = False
        for card in cards:
            if collected >= TARGET:
                break
            if has_captcha(d):
                if not _rotate_or_bail(d, pool, "mid-loop captcha"):
                    return 3
                ensure_search_results(d)
                rotated = True
                break
            skip = card_skip_reason(card, seen_titles, dedupe_index)
            if skip:
                log(f"  > '{card['title'][:40]}' skip ({skip})")
                continue
            cur = ap.current_account(pool)
            result = collect_from_card(d, card, seen_urls, seen_titles)
            # account-touched-the-detail counter (every tap costs budget)
            if cur:
                ap.mark_used(pool, cur.id)
            if result is RISK_SENTINEL:
                if not _rotate_or_bail(d, pool, "silent risk on detail"):
                    log("    no fresh accounts; stopping")
                    return 3
                ensure_search_results(d)
                rotated = True
                break
            if result:
                with LINKS_FILE.open("a") as f:
                    f.write(json.dumps(result, ensure_ascii=False) + "\n")
                seen_urls.add(result["url"])
                # only track real titles for in-run dedupe; noise titles are useless
                if not _is_noise_title(result["title"]):
                    seen_titles.add(result["title"])
                # best-effort: push to Feishu sheet (failures don't block scraping)
                if feishu_push.push_records([result]):
                    log("    [feishu] pushed")
                else:
                    log("    [feishu] push failed (continuing)")
                collected += 1
                captured_in_session += 1
                log(f"+1 ({collected}/{TARGET}) {result['title'][:50]} {result['url']}")
                # short rest after every successful capture (mimic reading)
                take_a_break("after capture", 3.0, 8.0)
                # medium rest every 3 captures
                if captured_in_session % 3 == 0:
                    take_a_break("3-pack cooldown", 12.0, 25.0)
                # long rest every 6 captures
                if captured_in_session - last_long_rest_at >= 6:
                    take_a_break("long cooldown", 40.0, 75.0)
                    last_long_rest_at = captured_in_session
        if rotated:
            continue
        if collected == progress_before:
            no_progress_scrolls += 1
        else:
            no_progress_scrolls = 0
        if collected < TARGET:
            # occasional reverse scroll to look more humanlike
            if random.random() < 0.18:
                hswipe(d, 540, 700, 540, 1700, duration=0.5)
                hsleep(0.8)
            hswipe(d, 540, 1800, 540, 600, duration=0.4)
            hsleep(1.2)
            scrolls += 1

    log(f"=== done collected={collected}/{TARGET} scrolls={scrolls} ===")
    return 0 if collected >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
