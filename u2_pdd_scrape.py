#!/usr/bin/env python3
"""Collect Pinduoduo hot-pinmo products via uiautomator2 — any search keyword.

Usage:
  SEARCH=拖鞋  TARGET=50 MIN_BUYERS=100 python u2_pdd_scrape.py
  SEARCH=杯子  TARGET=30                    python u2_pdd_scrape.py
  SEARCH=拖鞋  KEEP="拖鞋,凉拖,凉鞋,洞洞鞋,半拖,沙滩鞋" python u2_pdd_scrape.py

The script starts from the PDD home, types SEARCH into the search bar, hits
搜索, and verifies the results page (综合/销量/价格 chips) before scraping.

Pre-conditions:
  - 拼多多 (com.xunmeng.pinduoduo) installed and logged in
  - scrcpy clipboard bridge running (`pgrep -fl scrcpy`)
  - Samsung S10e USB connected (serial R28M219S2NA)
  - ADB Keyboard set as default IME

Selection rule:
  - On product detail page: read "1天内 N+ 人买过" AND "全店 M 人在拼"
  - Qualify if max(N, M) > MIN_BUYERS (default 100)

Per-query outputs (SLUG = SEARCH, with "拖鞋" → "slippers" for back-compat):
  results/pdd_<slug>.jsonl       — one record per qualifying product
  results/pdd_<slug>_imgs/       — main-image screenshots
  results/pdd_<slug>.log         — full run log
  results/feishu_pdd_<slug>.json — Feishu sheet config (auto-created on first run)
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
from lxml import etree
from PIL import Image

PKG = "com.xunmeng.pinduoduo"
TARGET = int(os.environ.get("TARGET", "50"))
MIN_BUYERS = int(os.environ.get("MIN_BUYERS", "100"))
SEARCH_QUERY = os.environ.get("SEARCH", "拖鞋").strip()
if not SEARCH_QUERY:
    print("FATAL: SEARCH env var is empty", file=sys.stderr)
    sys.exit(2)

# Per-keyword data isolation. "拖鞋" maps to the legacy `pdd_slippers` base
# so the 50 records + Feishu sheet "多多拖鞋" we already populated keep working.
_LEGACY_SLUG = {"拖鞋": "slippers"}
SLUG = _LEGACY_SLUG.get(SEARCH_QUERY) or re.sub(
    r"[^\w一-鿿]+", "_", SEARCH_QUERY
).strip("_") or "default"
BASE = f"pdd_{SLUG}"

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
IMG_DIR = RESULTS_DIR / f"{BASE}_imgs"
IMG_DIR.mkdir(exist_ok=True)
LINKS_FILE = RESULTS_DIR / f"{BASE}.jsonl"
LOG_FILE = RESULTS_DIR / f"{BASE}.log"
FEISHU_CONFIG = RESULTS_DIR / f"feishu_{BASE}.json"

# Category whitelist: each comma-separated word must appear in the card title
# to pass. PDD's search results tail off into a mixed "猜你喜欢" feed; this
# filter blocks that drift. Default = SEARCH itself; pass KEEP to expand.
_DEFAULT_KEEP = {
    "拖鞋": "拖鞋,凉拖,凉鞋,人字拖,一字拖,洞洞鞋,半拖,沙滩鞋",
}
CATEGORY_KEEP = os.environ.get("KEEP") or _DEFAULT_KEEP.get(SEARCH_QUERY, SEARCH_QUERY)
_keep_words = [w.strip() for w in CATEGORY_KEEP.split(",") if w.strip()]
CATEGORY_RE = re.compile("|".join(re.escape(w) for w in _keep_words))

# Negative filter: a title hitting BAN is rejected even if it also hit KEEP.
# Use for words that PDD might legitimately put on competing categories
# (e.g. 男士 / 男款 in a 大码女装 search → those are 大码男装, not 大码女装).
CATEGORY_BAN = os.environ.get("BAN", "").strip()
_ban_words = [w.strip() for w in CATEGORY_BAN.split(",") if w.strip()]
BAN_RE = (
    re.compile("|".join(re.escape(w) for w in _ban_words)) if _ban_words else None
)


def _legacy_feishu_path() -> Path | None:
    """One-time alias for the original "多多拖鞋" config that was created
    before per-slug naming existed."""
    if SLUG != "slippers":
        return None
    old = RESULTS_DIR / "feishu_pdd_config.json"
    return old if old.exists() else None


def _load_feishu_cfg() -> dict | None:
    path = FEISHU_CONFIG if FEISHU_CONFIG.exists() else _legacy_feishu_path()
    if not path:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def ensure_feishu_table() -> dict | None:
    """Return the Feishu sheet config for this SEARCH. Auto-create the sheet
    on the first run for a new keyword. Best-effort: returns None on failure
    so the rest of the scrape can still proceed (jsonl + images always work).
    """
    cfg = _load_feishu_cfg()
    if cfg and cfg.get("spreadsheet_token") and cfg.get("sheet_id"):
        return cfg
    title = f"多多{SEARCH_QUERY}"
    log(f"creating new Feishu sheet '{title}'")
    create_cmd = [
        "lark-cli", "sheets", "+create", "--as", "bot",
        "--title", title,
        "--headers", json.dumps(
            ["ts", "title", "today_buyers", "pinmo_count", "url", "image"],
            ensure_ascii=False,
        ),
    ]
    try:
        r = subprocess.run(create_cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"  lark-cli create failed: {r.stderr[:200]}")
            return None
        data = json.loads(r.stdout).get("data", {})
        token = data.get("spreadsheet_token")
        if not token:
            log(f"  no spreadsheet_token in create response")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        log(f"  lark-cli create raised: {e}")
        return None
    # fetch the default sheet_id via +info
    info_cmd = [
        "lark-cli", "sheets", "+info", "--as", "bot",
        "--spreadsheet-token", token,
    ]
    try:
        r = subprocess.run(info_cmd, capture_output=True, text=True, timeout=30)
        sheets = (
            json.loads(r.stdout).get("data", {})
            .get("sheets", {}).get("sheets", [])
        )
        if not sheets:
            return None
        sheet_id = sheets[0]["sheet_id"]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None
    cfg = {
        "spreadsheet_token": token,
        "sheet_id": sheet_id,
        "title": title,
        "url": f"https://my.feishu.cn/sheets/{token}",
        "headers": ["ts", "title", "today_buyers", "pinmo_count", "url", "image"],
    }
    FEISHU_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    log(f"  created: {cfg['url']}")
    return cfg


def push_to_feishu(rec: dict, cfg: dict | None) -> bool:
    """Append one captured row. Best-effort."""
    if not cfg:
        return False
    row = [
        rec.get("ts", ""), rec.get("title", ""),
        rec.get("today_buyers", 0), rec.get("pinmo_count", 0),
        rec.get("url", ""), rec.get("image", ""),
    ]
    cmd = [
        "lark-cli", "sheets", "+append", "--as", "bot",
        "--spreadsheet-token", cfg["spreadsheet_token"],
        "--sheet-id", cfg["sheet_id"],
        "--values", json.dumps([row], ensure_ascii=False),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

# Title-fingerprint dedupe: PDD share URLs use one-shot `ps` codes that change
# every share, so URL-level dedupe is useless across runs. Match on a
# normalized title fingerprint instead — strip brackets, promo noise, punct,
# keep first 25 chars.
_NOISE_WORDS = [
    "2026新款", "2025新款", "2024新款", "新款上市", "新款", "新品",
    "618大促", "618提前抢", "618", "百亿补贴", "店铺热销", "限时秒杀",
    "限时直降", "限时", "买一送一", "买1送1", "正品", "包邮",
    "官方旗舰", "旗舰店", "热销", "爆款", "情侣款", "情侣",
    "春夏", "秋冬", "夏季", "夏天", "冬季",
]
_PUNCT_RE = re.compile(
    r"[【】\[\]\(\)（）{}<>《》\s\-—_·•、，,。.!！?？:：;；'\"​/\\|*~`]+"
)


def normalize_title(t: str) -> str:
    """Reduce a PDD title to a comparable fingerprint."""
    if not t:
        return ""
    s = t
    for w in _NOISE_WORDS:
        s = s.replace(w, "")
    s = _PUNCT_RE.sub("", s)
    return s.lower()[:25]

# --- regex helpers -----------------------------------------------------------

# "1天内 300+ 人买过" / "1天内300人买过"
TODAY_BUYERS_RE = re.compile(r"1天内\s*(\d+)\+?\s*人买过")
# "全店 821 人在拼" or "全店821人在拼，参与可直接成团"
PINMO_RE = re.compile(r"全店\s*(\d+)\s*人在拼")
# pdd share URL
URL_RE = re.compile(r"https?://mobile\.yangkeduo\.com/\S+")


# --- humanization helpers ----------------------------------------------------

def hsleep(base: float, jitter: float = 0.35) -> None:
    lo = max(0.05, base * (1 - jitter))
    hi = base * (1 + jitter)
    time.sleep(random.uniform(lo, hi))


def hclick(d: u2.Device, x: int, y: int, dx: int = 14, dy: int = 14) -> None:
    ox = x + random.randint(-dx, dx)
    oy = y + random.randint(-dy, dy)
    time.sleep(random.uniform(0.08, 0.22))
    d.click(ox, oy)


def hswipe(
    d: u2.Device, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4
) -> None:
    j = 22
    x1o = x1 + random.randint(-j, j)
    y1o = y1 + random.randint(-j, j)
    x2o = x2 + random.randint(-j, j)
    y2o = y2 + random.randint(-j, j)
    dur = duration * random.uniform(0.75, 1.45)
    d.swipe(x1o, y1o, x2o, y2o, duration=dur)


def hback(d: u2.Device) -> None:
    time.sleep(random.uniform(0.18, 0.45))
    d.press("back")


def take_a_break(reason: str, low: float, high: float) -> None:
    pause = random.uniform(low, high)
    log(f"    [pace] {reason}: rest {pause:.1f}s")
    time.sleep(pause)


def humanize_detail_browse(d: u2.Device) -> None:
    """Inside a product detail page: scroll like a real shopper would before
    we head off to share the link. Reduces the 'tap → share' robot signature."""
    n_swipes = random.randint(2, 4)
    for _ in range(n_swipes):
        hswipe(d, 540, 1700, 540, 800, duration=random.uniform(0.45, 0.8))
        hsleep(random.uniform(1.0, 3.0))
        if random.random() < 0.25:
            hswipe(d, 540, 700, 540, 1300, duration=0.5)
            hsleep(random.uniform(0.6, 1.4))


def pre_action_scroll(d: u2.Device) -> None:
    if random.random() < 0.5:
        if random.random() < 0.3:
            hswipe(d, 540, 900, 540, 1500, duration=0.5)
        else:
            hswipe(d, 540, 1500, 540, 1000, duration=0.5)
        hsleep(0.5)


# --- io ---------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def pbpaste() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def pbcopy(s: str) -> None:
    subprocess.run(["pbcopy"], input=s, text=True)


def load_seen() -> tuple[set[str], set[str], set[str]]:
    """Return (urls, exact_titles, fingerprints) from history jsonl."""
    if not LINKS_FILE.exists():
        return set(), set(), set()
    urls, titles, fps = set(), set(), set()
    for line in LINKS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        urls.add(rec.get("url", ""))
        t = rec.get("title", "")
        titles.add(t)
        fp = normalize_title(t)
        if fp:
            fps.add(fp)
    return urls, titles, fps


# --- search results page ----------------------------------------------------

def _bounds(el) -> tuple[int, int, int, int] | None:
    b = el.attrib.get("bounds", "")
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
    if not m:
        return None
    return tuple(map(int, m.groups()))  # type: ignore[return-value]


def in_home_feed(d: u2.Device) -> bool:
    """True iff the home-tab bottom nav is visible.

    PDD's home recommendation feed and the search results list both show
    dual-column product cards, so card presence alone is not enough. The
    bottom nav (首页 / 多多视频 / 聊天 / 个人中心) is only rendered on the
    home tab — its presence is the surest sign we drifted off the results
    list. Per user rule: scraping MUST stay on the search results list.
    """
    if d.app_current().get("package") != PKG:
        return False
    home_nav = ("首页", "多多视频", "聊天", "个人中心")
    return sum(1 for w in home_nav if d(text=w).exists) >= 3


def on_search_results(d: u2.Device) -> bool:
    """We're on the search results list iff:
      - we have >=2 product-title TextViews, AND
      - we are NOT in the home recommendation feed (home bottom-nav absent).

    Filter chips (综合/销量/价格/筛选) are at the very top only — once the user
    scrolls down they disappear, so chip presence is a fresh-results signal,
    not a general "on results page" check.
    """
    if d.app_current().get("package") != PKG:
        return False
    if in_home_feed(d):
        return False
    return len(list_cards(d)) >= 2


def on_search_results_topfresh(d: u2.Device) -> bool:
    """Stricter check: we just landed on the results page (filter chips visible)."""
    if d.app_current().get("package") != PKG:
        return False
    chips = sum(1 for c in ("综合", "销量", "价格", "筛选") if d(text=c).exists)
    return chips >= 3


def go_home(d: u2.Device) -> bool:
    """Back-out repeatedly to reach the PDD home tab. Relaunch app if we
    accidentally exited PDD."""
    for _ in range(8):
        cur = d.app_current()
        if cur.get("package") != PKG:
            subprocess.run(
                ["adb", "shell", "monkey", "-p", PKG,
                 "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True,
            )
            hsleep(4.0)
            continue
        # home tab signal: 首页 + 个人中心 bottom-nav both visible
        if d(text="首页").exists and d(text="个人中心").exists:
            return True
        d.press("back")
        hsleep(0.7)
        dismiss_popups(d)
    return d(text="首页").exists and d(text="个人中心").exists


def do_search(d: u2.Device, query: str) -> bool:
    """Drive: home → tap search bar → type → 搜索. Verify chips appear.

    Returns True iff we land on a fresh results page for `query`.
    """
    # tap the search bar (y~190 on this device)
    hclick(d, 540, 190, dx=80, dy=20)
    hsleep(1.4)
    # ensure ADB Keyboard, clear, type
    subprocess.run(
        ["adb", "shell", "ime", "set", "com.android.adbkeyboard/.AdbIME"],
        capture_output=True,
    )
    subprocess.run(
        ["adb", "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
        capture_output=True,
    )
    hsleep(0.4)
    subprocess.run(
        ["adb", "shell", "am", "broadcast",
         "-a", "ADB_INPUT_TEXT", "--es", "msg", query],
        capture_output=True,
    )
    hsleep(0.9)
    if d(text="搜索").exists:
        d(text="搜索").click()
    hsleep(2.8)
    dismiss_popups(d)
    return on_search_results_topfresh(d)


def list_cards(d: u2.Device) -> list[dict]:
    """Visible product cards on the search-results page.

    PDD renders the product title as a single TextView with the full title in
    `content-desc`. The TextView is ~487x49 in the upper half of each card.
    Tapping it bubbles up to the card's click handler.
    """
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode())
    cards: list[dict] = []
    seen_titles: set[str] = set()
    for el in root.iter():
        if not el.attrib.get("class", "").endswith("TextView"):
            continue
        cd = (el.attrib.get("content-desc") or "").strip()
        if len(cd) < 10:
            continue
        if cd.startswith(("拍照搜索", "侧屏幕面板", "搜索")):
            continue
        if cd in ("最近使用", "主屏幕", "返回"):
            continue
        bnd = _bounds(el)
        if not bnd:
            continue
        x1, y1, x2, y2 = bnd
        w, h = x2 - x1, y2 - y1
        # Title TextView shape: ~400-540 wide, <80 tall, below the filter row
        if w < 400 or w > 560:
            continue
        if h > 90:
            continue
        if y1 < 280:  # below filter chips (y~302) and category chips (y~406)
            continue
        if cd in seen_titles:
            continue
        seen_titles.add(cd)
        cards.append(
            {
                "title": cd,
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                "bounds": (x1, y1, x2, y2),
            }
        )
    cards.sort(key=lambda c: (c["bounds"][1], c["bounds"][0]))
    return cards


# --- detail page ------------------------------------------------------------

def on_detail_page(d: u2.Device) -> bool:
    """Detail page signals: 分享 button content-desc, plus a price ¥ marker."""
    if not d(description="分享").exists:
        return False
    return True


def extract_signals(d: u2.Device) -> tuple[int, int]:
    """Read (today_buyers, pinmo_count) from the open detail page.

    today_buyers: number in '1天内 N+ 人买过' (0 if not present)
    pinmo_count : number in '全店 M 人在拼'   (0 if not present)
    """
    xml = d.dump_hierarchy()
    root = etree.fromstring(xml.encode())
    today = 0
    pinmo = 0
    for el in root.iter():
        t = (el.attrib.get("text") or "").strip()
        if not t:
            continue
        m = TODAY_BUYERS_RE.search(t)
        if m and today == 0:
            today = int(m.group(1))
        m = PINMO_RE.search(t)
        if m and pinmo == 0:
            pinmo = int(m.group(1))
    return today, pinmo


def screenshot_main_image(slug: str) -> Path | None:
    """Screenshot the detail page and crop the carousel area as the main image.
    PDD carousel sits roughly y=110-1080 (1080x2280 device). We crop a square
    so the result is a clean product photo with minimal chrome.
    """
    raw_path = IMG_DIR / f"{slug}_raw.png"
    out_path = IMG_DIR / f"{slug}.png"
    try:
        subprocess.run(
            ["adb", "shell", "screencap", "-p", "/sdcard/_pdd_main.png"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["adb", "pull", "/sdcard/_pdd_main.png", str(raw_path)],
            check=True, capture_output=True,
        )
        img = Image.open(raw_path)
        # crop carousel: skip status bar (top ~110) and keep a square of width
        w, h = img.size
        crop_top = 110 if h >= 1200 else int(h * 0.05)
        crop_bottom = min(h, crop_top + w)  # square crop = w x w
        cropped = img.crop((0, crop_top, w, crop_bottom))
        cropped.save(out_path)
        raw_path.unlink(missing_ok=True)
        return out_path
    except Exception as e:
        log(f"    main-image capture failed: {e}")
        return None


# --- share & copy link ------------------------------------------------------

def share_and_copy(d: u2.Device) -> str | None:
    """Tap 分享 (top-right) → swipe share row → 复制链接 → read clipboard."""
    sentinel = f"SENTINEL_{int(time.time() * 1000)}"
    pbcopy(sentinel)
    if not d(description="分享").exists:
        log("    no 分享 button")
        return None
    hsleep(0.4)
    d(description="分享").click()
    hsleep(1.4)

    # Reveal 复制链接 — it sits off-screen-right by default. Swipe left until
    # found (up to 3 swipes).
    for _ in range(3):
        if d(text="复制链接").exists:
            break
        hswipe(d, 900, 1880, 200, 1880, duration=0.4)
        hsleep(0.5)
    if not d(text="复制链接").exists:
        log("    no 复制链接 in share panel")
        hback(d)
        return None
    hsleep(0.3)
    d(text="复制链接").click()
    hsleep(1.8)
    # share panel auto-closes on PDD after copy; if not, back out
    if d(text="复制链接").exists:
        hback(d)
        hsleep(0.5)
    clip = pbpaste().strip()
    if not clip or clip == sentinel:
        log("    clipboard not updated")
        return None
    return clip


# --- risk control & popups --------------------------------------------------

CAPTCHA_TOKENS = (
    "为了你的账号安全", "请完成验证", "拖动滑块", "人机验证",
    "验证码", "识别图中", "请按住按钮", "异常访问",
)

ERROR_TOKENS = (
    "网络异常", "加载失败", "服务器繁忙", "请稍后再试",
    "暂无数据", "访问受限", "操作过于频繁", "今日浏览次数已达上限",
)


def has_captcha(d: u2.Device) -> str:
    for token in CAPTCHA_TOKENS:
        if d.xpath(f'//*[contains(@text, "{token}")]').exists:
            return token
    return ""


def has_error_overlay(d: u2.Device) -> str:
    for token in ERROR_TOKENS:
        if d.xpath(f'//*[contains(@text, "{token}")]').exists:
            return token
    return ""


def dismiss_popups(d: u2.Device) -> int:
    closed = 0
    for desc in ("关闭", "关闭按钮", "close"):
        node = d.xpath(f'//*[@content-desc="{desc}"]')
        if node.exists:
            node.click()
            hsleep(0.4)
            closed += 1
    for text in ("以后再说", "暂不升级", "暂不更新", "不升级", "不更新",
                 "下次再说", "取消", "我知道了"):
        node = d.xpath(f'//*[@text="{text}"]')
        if node.exists:
            node.click()
            hsleep(0.4)
            closed += 1
    return closed


# --- per-card collection -----------------------------------------------------

def slugify(s: str) -> str:
    """Filename-safe slug from a product title."""
    s = re.sub(r"[^\w一-鿿]+", "_", s)
    return s.strip("_")[:40] or f"item_{int(time.time())}"


def collect_from_card(
    d: u2.Device,
    card: dict,
    seen_urls: set[str],
    seen_titles: set[str],
    seen_fps: set[str],
) -> dict | None:
    title = card["title"]
    log(f"  > '{title[:50]}'")
    if title in seen_titles:
        log("    skip: title already seen (exact)")
        return None
    fp = normalize_title(title)
    if fp and fp in seen_fps:
        log(f"    skip: fingerprint seen ({fp!r})")
        return None
    if BAN_RE and BAN_RE.search(title):
        log(f"    skip: title hits BAN list")
        return None
    if not CATEGORY_RE.search(title):
        log(f"    skip: off-category (none of KEEP matched)")
        return None
    hclick(d, *card["center"])
    hsleep(2.5)

    # if a popup intercepted the tap, try to dismiss
    dismiss_popups(d)

    if not on_detail_page(d):
        log("    failed to land on detail page — banning title from this run")
        # mark seen so we don't keep retrying the same dead card next screen
        seen_titles.add(title)
        if fp:
            seen_fps.add(fp)
        hback(d)
        hsleep(0.8)
        return None

    err = has_error_overlay(d)
    if err:
        log(f"    !!! error overlay on detail: {err}")
        hback(d)
        return None

    # human dwell + scroll before extracting (less robotic)
    humanize_detail_browse(d)

    # scroll back to top before reading signals (humanize may have scrolled)
    for _ in range(3):
        hswipe(d, 540, 700, 540, 1900, duration=0.4)
        hsleep(0.5)

    today, pinmo = extract_signals(d)
    log(f"    signals: 1天内={today}+ 人买过 | 全店={pinmo} 人在拼")

    if max(today, pinmo) < MIN_BUYERS:
        log(f"    skip: max({today},{pinmo}) < {MIN_BUYERS}")
        hback(d)
        return None

    # capture main image BEFORE sharing (we're on the carousel/top section)
    slug = slugify(title)
    img_path = screenshot_main_image(slug)

    # pre-share micro-pause + 1 peek scroll (more humanlike)
    pre_action_scroll(d)

    clip = share_and_copy(d)
    if not clip:
        hback(d)
        return None
    m = URL_RE.search(clip)
    if not m:
        log(f"    no PDD url in clip: {clip[:80]!r}")
        hback(d)
        return None
    url = m.group(0).rstrip(",.!?。， )]")
    if url in seen_urls:
        log("    skip: url already seen")
        hback(d)
        return None

    hback(d)
    return {
        "title": title,
        "today_buyers": today,
        "pinmo_count": pinmo,
        "url": url,
        "image": str(img_path) if img_path else "",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- main loop --------------------------------------------------------------

def main() -> int:
    log(
        f"=== start SEARCH={SEARCH_QUERY!r} SLUG={SLUG!r} "
        f"TARGET={TARGET} MIN_BUYERS={MIN_BUYERS} "
        f"KEEP={CATEGORY_KEEP!r} ==="
    )
    d = u2.connect()
    log(f"connected: {d.app_current()}")

    feishu_cfg = ensure_feishu_table()
    if feishu_cfg:
        log(f"feishu: {feishu_cfg.get('title')} → {feishu_cfg.get('url')}")
    else:
        log("feishu: no config — Feishu push disabled (jsonl + images still recorded)")

    # Always re-search at startup unless the chip row is right there. The
    # weaker on_search_results() check can be fooled by the search-suggestion
    # page (the same TextView shapes get picked up by list_cards). A fresh
    # search guarantees we're at the top of the real results list.
    if on_search_results_topfresh(d) and d(text=SEARCH_QUERY).exists:
        log(f"already on FRESH results page for '{SEARCH_QUERY}', skipping do_search")
    else:
        log(f"forcing a fresh search for '{SEARCH_QUERY}'")
        if not go_home(d):
            log("FATAL: could not reach PDD home tab")
            return 2
        if not do_search(d, SEARCH_QUERY):
            log(f"FATAL: search for '{SEARCH_QUERY}' did not land on a results page")
            return 2
        log("on fresh search results page ✓")

    # warm-up
    warmup = random.uniform(2.0, 5.0)
    log(f"warmup pause {warmup:.1f}s")
    time.sleep(warmup)

    seen_urls, seen_titles, seen_fps = load_seen()
    log(f"history: {len(seen_urls)} urls / {len(seen_fps)} title-fingerprints")
    collected = 0
    scrolls = 0
    no_progress_scrolls = 0
    consecutive_all_seen = 0  # screens in a row where every slipper card is dup
    captured_in_session = 0
    last_long_rest_at = 0

    home_drift_recoveries = 0
    MAX_HOME_DRIFTS = 2
    tail_recoveries = 0
    MAX_TAIL_RECOVERIES = 2
    consecutive_offtopic_screens = 0
    OFFTOPIC_THRESHOLD = 0.8  # ≥80% of cards off-category → likely tail feed

    while collected < TARGET and no_progress_scrolls < 6 and scrolls < 80:
        # captcha guard — STOP, don't try to bypass (per feedback_pause_dont_charge)
        ct = has_captcha(d)
        if ct:
            log(f"!!! CAPTCHA detected: {ct!r}")
            log("    stopping — manual intervention required")
            return 3

        # Hard rule: scraping MUST stay on the search results list. If we drift
        # into the PDD home recommendation feed (bottom-nav appears), the cards
        # are off-topic regardless of KEEP. Re-run the search to recover; bail
        # if it happens repeatedly (don't fight it).
        if in_home_feed(d):
            log("!!! drifted into PDD home recommendation feed (not search list)")
            home_drift_recoveries += 1
            if home_drift_recoveries > MAX_HOME_DRIFTS:
                log(f"    {home_drift_recoveries} drifts — bailing rather than fight feed")
                return 5
            log(f"    re-searching '{SEARCH_QUERY}' (recovery {home_drift_recoveries}/{MAX_HOME_DRIFTS})")
            if not do_search(d, SEARCH_QUERY):
                log("    re-search failed — bailing")
                return 5
            log("    ✓ back on fresh search results")
            continue

        cards = list_cards(d)
        log(f"visible cards: {len(cards)}")
        if not cards:
            log("  no cards visible; scrolling")
            hswipe(d, 540, 1700, 540, 700, duration=0.5)
            hsleep(1.0)
            no_progress_scrolls += 1
            scrolls += 1
            continue

        # Tail-feed detection: PDD inserts a long "猜你喜欢" recommendation
        # block at the bottom of the real search results, with mostly off-
        # category cards. Same activity, no bottom-nav — invisible to
        # in_home_feed(). If ≥OFFTOPIC_THRESHOLD of cards fail KEEP (or hit BAN)
        # for several screens in a row, we're scraping junk — re-search to jump
        # back to the top of the actual results list.
        on_topic = [
            c for c in cards
            if CATEGORY_RE.search(c["title"])
            and not (BAN_RE and BAN_RE.search(c["title"]))
        ]
        offtopic_ratio = 1.0 - (len(on_topic) / len(cards))
        if len(cards) >= 3 and offtopic_ratio >= OFFTOPIC_THRESHOLD:
            consecutive_offtopic_screens += 1
            log(f"  [tail-check] {len(cards) - len(on_topic)}/{len(cards)} cards "
                f"off-category ({consecutive_offtopic_screens}/3)")
            if consecutive_offtopic_screens >= 3:
                log("!!! drifted into mixed-category tail feed "
                    "(≥80% off-topic for 3 screens)")
                tail_recoveries += 1
                if tail_recoveries > MAX_TAIL_RECOVERIES:
                    log(f"    {tail_recoveries} tail recoveries — bailing")
                    return 5
                log(f"    re-searching '{SEARCH_QUERY}' to jump back to top "
                    f"(recovery {tail_recoveries}/{MAX_TAIL_RECOVERIES})")
                if not do_search(d, SEARCH_QUERY):
                    log("    re-search failed — bailing")
                    return 5
                consecutive_offtopic_screens = 0
                consecutive_all_seen = 0
                no_progress_scrolls = 0
                continue
        else:
            consecutive_offtopic_screens = 0

        # exit signal: a screen where every CATEGORY-passing card is already
        # a known fingerprint. Off-category cards don't count (they'd reset
        # the counter wrongly during the recommendation feed tail).
        slipper_cards = [c for c in cards if CATEGORY_RE.search(c["title"])]
        if slipper_cards and all(
            normalize_title(c["title"]) in seen_fps for c in slipper_cards
        ):
            consecutive_all_seen += 1
            log(f"  [exit-check] all {len(slipper_cards)} slipper cards on "
                f"this screen already seen ({consecutive_all_seen}/3)")
            if consecutive_all_seen >= 3:
                log("=== exit: 3 consecutive screens fully deduped — "
                    "feed exhausted relative to history ===")
                break
        elif slipper_cards:
            consecutive_all_seen = 0

        progress_before = collected
        for card in cards:
            if collected >= TARGET:
                break
            if card["title"] in seen_titles:
                continue
            fp = normalize_title(card["title"])
            if fp and fp in seen_fps:
                continue
            result = collect_from_card(d, card, seen_urls, seen_titles, seen_fps)
            if result is None:
                # back up to results if we drifted
                if not on_search_results(d):
                    hback(d); hsleep(0.7)
                    if not on_search_results(d):
                        log("    [recover] tried back, not on results — bail")
                        return 4
                continue
            with LINKS_FILE.open("a") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            seen_urls.add(result["url"])
            seen_titles.add(result["title"])
            fp = normalize_title(result["title"])
            if fp:
                seen_fps.add(fp)
            collected += 1
            captured_in_session += 1
            log(f"+1 ({collected}/{TARGET}) {result['title'][:50]} {result['url']}")
            if push_to_feishu(result, feishu_cfg):
                log(f"    [feishu] pushed to {feishu_cfg.get('title','?')}")
            elif feishu_cfg:
                log("    [feishu] push failed (continuing)")
            # back to results in case PDD didn't auto-return
            if not on_search_results(d):
                hback(d); hsleep(0.7)
            # rests: short / medium / long cadence
            take_a_break("after capture", 3.0, 8.0)
            if captured_in_session % 3 == 0:
                take_a_break("3-pack cooldown", 12.0, 22.0)
            if captured_in_session - last_long_rest_at >= 6:
                take_a_break("long cooldown", 40.0, 75.0)
                last_long_rest_at = captured_in_session

        if collected == progress_before:
            no_progress_scrolls += 1
        else:
            no_progress_scrolls = 0
        if collected < TARGET:
            # occasional reverse peek-scroll, then main scroll down
            if random.random() < 0.15:
                hswipe(d, 540, 800, 540, 1700, duration=0.5)
                hsleep(0.7)
            hswipe(d, 540, 1900, 540, 600, duration=0.4)
            hsleep(1.2)
            scrolls += 1

    log(f"=== done collected={collected}/{TARGET} scrolls={scrolls} ===")
    return 0 if collected >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
