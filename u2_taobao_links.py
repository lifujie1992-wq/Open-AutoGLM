#!/usr/bin/env python3
"""淘宝搜索结果页抓取：链接 + 销量 + 评价数量 + 最新评价日期。

设计原则(从拼多多版 u2_pdd_scrape.py 移植的「轻手法」)：
不是「滑得更像人」，而是【尽量不碰 uiautomator2 的工具层暴露面】——
那才是上次淘宝把账号登出的主因。

具体：
1. 不用 send_keys/自定义输入法   -> 启动即 set_fastinput_ime(False) 还原系统键盘；
                                   搜索默认假设你已手动搜好，停在结果页。
2. 不重启 App(无 app_start stop) 。
3. 链接走被动 dumpsys 读 Intent 的 id(App 感知不到)，不靠点「分享」。
4. dump_hierarchy 降到最少：列表 1 次 / 单品 1 次。
5. 全程人类化动作：随机抖动的点击/滑动、详情页上下反复滑、随机停留、商品间隔与分批。
6. 任意时刻命中登录页/验证码 -> 立即停，绝不自动登录。

用法：
  python u2_taobao_links.py cleanup    # 还原系统输入法、检查环境(被登出后先跑这个)
  python u2_taobao_links.py recon      # 登录后跑1个：dump详情文案，校准字段选择器
  python u2_taobao_links.py 5          # 小批量抓 5 个
  python u2_taobao_links.py 20         # 抓 20 个
"""
import os
import re
import sys
import time
import random
import subprocess
from urllib.parse import unquote

import uiautomator2 as u2

SERIAL = "R28M219S2NA"
PKG = "com.taobao.taobao"
OUT = "/Users/lifujie/Projects/Open-AutoGLM/results/taobao_shoukeji_links.txt"
RECON_DUMP = "/Users/lifujie/Projects/Open-AutoGLM/results/taobao_recon_dump.txt"

STOP_ACTIVITIES = ("UserLoginActivity", "LoginActivity", "MiniLoginActivity")
CAPTCHA_TOKENS = ("滑动验证", "拖动滑块", "人机验证", "安全验证", "完成验证",
                  "请按住", "异常访问", "验证码", "人脸")

SALES_PATS = [
    re.compile(r"((?:已售|月销|销量|成交)\s*[\d.]+\s*万?\+?)"),
    re.compile(r"([\d.]+\s*万?\+?\s*人?(?:付款|收货|已售|购买))"),
    re.compile(r"(全网\s*[\d.]+\s*万?\+?\s*人付款)"),
]
REVIEW_CNT_PATS = [
    re.compile(r"(?:宝贝评价|累计评价|全部评价|评价|评论)\s*[\(（]?\s*([\d.]+\s*万?\+?)\s*[\)）]?\s*条?"),
    re.compile(r"([\d.]+\s*万?\+?)\s*条\s*(?:评价|评论)"),
]
DATE_PATS = [
    re.compile(r"(20\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)"),
    re.compile(r"(\d{1,2}[-./月]\d{1,2}日?)"),
    re.compile(r"(\d+\s*(?:天|个月|月|年)前)"),
]


# ---------------- 人类化动作 ----------------
def hsleep(base: float, jitter: float = 0.35) -> None:
    lo = max(0.05, base * (1 - jitter))
    time.sleep(random.uniform(lo, base * (1 + jitter)))


def hclick(d, x: int, y: int, dx: int = 14, dy: int = 14) -> None:
    time.sleep(random.uniform(0.08, 0.22))
    d.click(x + random.randint(-dx, dx), y + random.randint(-dy, dy))


def hswipe(d, x1, y1, x2, y2, duration: float = 0.45) -> None:
    j = 22
    dur = duration * random.uniform(0.75, 1.45)
    d.swipe(x1 + random.randint(-j, j), y1 + random.randint(-j, j),
            x2 + random.randint(-j, j), y2 + random.randint(-j, j), duration=dur)


def hback(d) -> None:
    time.sleep(random.uniform(0.18, 0.45))
    d.press("back")


def take_a_break(reason: str, low: float, high: float) -> None:
    pause = random.uniform(low, high)
    print(f"    [pace] {reason}: 歇 {pause:.1f}s", flush=True)
    time.sleep(pause)


# ---------------- 链接(被动 dumpsys) ----------------
def get_detail_url() -> str | None:
    out = subprocess.run(
        ["adb", "-s", SERIAL, "shell", "dumpsys", "activity", "activities"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    for line in out.splitlines():
        if "dat=http" in line and "item" in line and "id=" in line:
            m = re.search(r"dat=(\S+)", line)
            if m:
                return m.group(1)
    return None


def parse_link(url: str) -> tuple[str | None, str]:
    idm = re.search(r"[?&]id=(\d+)", url)
    tm = re.search(r"[?&]title=([^&]+)", url)
    return (idm.group(1) if idm else None), (unquote(tm.group(1)) if tm else "")


# ---------------- 状态/风控检测 ----------------
def block_reason(d) -> str:
    act = d.app_current().get("activity", "")
    if any(s in act for s in STOP_ACTIVITIES):
        return f"登录页 {act}"
    for tok in CAPTCHA_TOKENS:
        if d.xpath(f'//*[contains(@text,"{tok}")]').exists:
            return f"验证页 {tok}"
    return ""


def abort(msg: str) -> None:
    print(f"\n!!! 停止：{msg}")
    print("    账号已掉登录态/触发风控。请手动处理，绝不自动登录。")
    sys.exit(2)


def guard(d) -> None:
    why = block_reason(d)
    if why:
        abort(why)


def on_detail(d) -> bool:
    return "Detail" in d.app_current().get("activity", "")


# ---------------- 详情页人类化浏览 + 采集 ----------------
def collect_texts(d) -> set[str]:
    xml = d.dump_hierarchy()
    return {m.group(1).strip()
            for m in re.finditer(r'(?:content-desc|text)="([^"]+)"', xml)
            if m.group(1).strip()}


def browse_and_collect(d) -> set[str]:
    """像真人一样上下反复滑详情页，全程只 dump 1~2 次采集文案。"""
    bag: set[str] = set()
    hsleep(2.2)                       # 进页先看主图
    bag |= collect_texts(d)           # dump #1：拿销量/主信息

    n_down = random.randint(4, 7)
    i = 0
    while i < n_down:
        guard(d)
        hswipe(d, 540, 1650, 540, 800, duration=random.uniform(0.45, 0.8))
        hsleep(random.uniform(1.0, 3.0))
        i += 1
        if random.random() < 0.35 and i < n_down:   # 回滑看一眼
            hswipe(d, 540, 800, 540, 1300, duration=0.5)
            hsleep(random.uniform(0.8, 1.8))

    bag |= collect_texts(d)           # dump #2：此时多半已滑到评价区，拿评价数/日期

    if random.random() < 0.5:         # 半数概率点开评价列表，看最新评价日期
        bag |= try_open_reviews(d)
    return bag


def try_open_reviews(d) -> set[str]:
    bag: set[str] = set()
    for kw in ("宝贝评价", "全部评价", "查看全部评价", "累计评价", "评价"):
        el = d.xpath(f'//*[contains(@content-desc,"{kw}")]')
        if not el.exists:
            continue
        try:
            el.click()
            hsleep(2.2)
            guard(d)
            for _ in range(random.randint(1, 2)):
                bag |= collect_texts(d)
                hswipe(d, 540, 1600, 540, 800, duration=0.6)
                hsleep(random.uniform(1.2, 2.5))
            bag |= collect_texts(d)
            hback(d)
            hsleep(1.6)
        except Exception:
            pass
        break
    return bag


# ---------------- 字段抽取 ----------------
def first_match(texts, pats) -> str:
    for t in texts:
        for p in pats:
            m = p.search(t)
            if m:
                return m.group(1).strip()
    return ""


def extract_fields(texts, card_desc: str = "") -> tuple[str, str, str]:
    pool = set(texts)
    if card_desc:
        pool.add(card_desc)
    sales = first_match(pool, SALES_PATS)
    reviews = first_match(pool, REVIEW_CNT_PATS)
    ctx = [t for t in pool if ("评价" in t or "评论" in t)]
    date = first_match(ctx, DATE_PATS) or first_match(pool, DATE_PATS)
    return sales, reviews, date


# ---------------- 搜索结果页卡片(1 次 dump) ----------------
def find_cards(d) -> list[tuple[int, int, str]]:
    """返回 (点击x, 点击y, 标题) 列表。点图片区，避开进店/更多按钮。"""
    xml = d.dump_hierarchy()
    cards = []
    for m in re.finditer(
        r'<node[^>]*clickable="true"[^>]*content-desc="([^"]{8,})"[^>]*'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        desc = m.group(1)
        x0, y0, x1, y1 = map(int, m.groups()[1:])
        if x0 <= 5 and x1 >= 1060 and y0 > 800 and (y1 - y0) > 300:
            cards.append((y0, x0 + 230, y0 + 200, desc))
    cards.sort(key=lambda c: c[0])
    return [(cx, cy, desc) for _, cx, cy, desc in cards]


def on_results(d) -> bool:
    if d.app_current().get("package") != PKG:
        return False
    return len(find_cards(d)) >= 2


# ---------------- 环境清理 ----------------
def cleanup(d) -> None:
    """被登出后先跑：还原系统输入法，去掉 uiautomator 的暴露面。"""
    try:
        d.set_fastinput_ime(False)
        print("已还原系统输入法 (set_fastinput_ime False)")
    except Exception as e:
        print("还原输入法失败:", e)
    cur = subprocess.run(["adb", "-s", SERIAL, "shell", "settings", "get",
                          "secure", "default_input_method"],
                         capture_output=True, text=True).stdout.strip()
    print("当前默认输入法:", cur)
    if "uiautomator" in cur or "FastInput" in cur:
        print("⚠️ 默认输入法仍是自动化键盘，手动到 设置>输入法 切回 三星键盘")
    print("当前前台:", d.app_current().get("activity", ""))


# ---------------- 主流程 ----------------
def recon(d) -> None:
    guard(d)
    if not on_results(d):
        abort("不在搜索结果页，请先手动搜「手机壳」停在结果页")
    cx, cy, desc = find_cards(d)[0]
    print(f"recon 点进：{desc[:30]}")
    hclick(d, cx, cy)
    hsleep(3.6)
    guard(d)
    if not on_detail(d):
        abort(f"未进入详情页：{d.app_current().get('activity','')}")
    bag = browse_and_collect(d)
    url = get_detail_url()
    os.makedirs(os.path.dirname(RECON_DUMP), exist_ok=True)
    with open(RECON_DUMP, "w") as f:
        f.write(f"URL: {url}\n卡片desc: {desc}\n\n=== 全部文案 ===\n")
        for t in sorted(bag):
            f.write(t + "\n")
    sales, reviews, date = extract_fields(bag, desc)
    print(f"落盘 {RECON_DUMP}（{len(bag)} 条）")
    print(f"启发式 -> 销量:{sales!r} 评价数:{reviews!r} 最新日期:{date!r}")
    hback(d)


def scrape(d, target: int) -> None:
    guard(d)
    if not on_results(d):
        abort("不在搜索结果页，请先手动搜「手机壳」停在结果页")
    seen: dict[str, bool] = {}
    rows = []
    loops = 0
    since_break = 0
    while len(rows) < target and loops < 30:
        loops += 1
        guard(d)
        for cx, cy, desc in find_cards(d):
            if len(rows) >= target:
                break
            hclick(d, cx, cy)
            hsleep(3.4)
            guard(d)
            if not on_detail(d):
                hback(d); hsleep(1.4)
                continue
            bag = browse_and_collect(d)
            url = get_detail_url()
            hback(d)
            hsleep(1.8)
            if not url:
                continue
            iid, title = parse_link(url)
            if not iid or iid in seen:
                continue
            seen[iid] = True
            sales, reviews, date = extract_fields(bag, desc)
            rows.append((iid, f"https://item.taobao.com/item.htm?id={iid}",
                         title, sales, reviews, date))
            since_break += 1
            print(f"[{len(rows):2d}] {iid} 销:{sales or '-'} 评:{reviews or '-'} "
                  f"日期:{date or '-'}  {title[:20]}", flush=True)
            take_a_break("商品间隔", 8, 15)
            if since_break >= random.randint(5, 6):   # 每 5~6 个歇久一点
                since_break = 0
                take_a_break("分批休息", 40, 80)
        hswipe(d, 540, 1650, 540, 750, duration=0.6)
        hsleep(random.uniform(1.5, 2.6))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("序号\t链接\t销量\t评价数量\t最新评价日期\t标题\n")
        for i, (iid, link, title, sales, reviews, date) in enumerate(rows, 1):
            f.write(f"{i}\t{link}\t{sales}\t{reviews}\t{date}\t{title}\n")
    print(f"\n共抓 {len(rows)} 个，已保存 {OUT}", flush=True)


def main() -> None:
    d = u2.connect(SERIAL)
    try:                       # 一连上就还原系统输入法，去掉自定义 IME 暴露
        d.set_fastinput_ime(False)
    except Exception:
        pass
    arg = sys.argv[1] if len(sys.argv) > 1 else "5"
    if arg == "cleanup":
        cleanup(d)
    elif arg == "recon":
        recon(d)
    else:
        scrape(d, int(arg))


if __name__ == "__main__":
    main()
