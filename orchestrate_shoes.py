#!/usr/bin/env python3
"""Collect Douyin Mall women's-shoes share links matching sales/recency criteria.

Architecture: scrcpy syncs phone clipboard to Mac clipboard. Agent only
needs to copy the share link on the phone; orchestrator reads it via
pbpaste. No phone-side paste/UI extraction needed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = str(ROOT / "run.sh")
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LINKS_FILE = RESULTS_DIR / "shoes_links.jsonl"
LOG_FILE = RESULTS_DIR / "orchestrate.log"

TARGET = int(os.environ.get("TARGET", "20"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", str(TARGET * 3)))
TODAY = date.today().isoformat()

URL_RE = re.compile(r"https?://v\.douyin\.com/\S+|https?://[\w.-]*douyin[\w.-]*/\S+")
FINISH_RE = re.compile(r'finish\(message=(["\'])(.*?)\1\)', re.DOTALL)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_existing() -> list[dict]:
    if not LINKS_FILE.exists():
        return []
    return [json.loads(l) for l in LINKS_FILE.read_text().splitlines() if l.strip()]


def pbpaste() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def pbcopy(s: str) -> None:
    subprocess.run(["pbcopy"], input=s, text=True)


def run_autoglm(task: str, max_steps: int = 40, timeout: int = 900) -> str:
    step_log = RESULTS_DIR / f"step_{int(time.time())}.log"
    try:
        with step_log.open("w") as out:
            r = subprocess.run(
                [RUN, "--max-steps", str(max_steps), task],
                stdout=out, stderr=subprocess.STDOUT, text=True, timeout=timeout,
            )
        return step_log.read_text()
    except subprocess.TimeoutExpired:
        return step_log.read_text() + "\n[TIMEOUT]"


def last_finish_message(output: str) -> str | None:
    matches = FINISH_RE.findall(output)
    return matches[-1][1] if matches else None


def extract_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(",.!?。，)] ") if m else None


def bootstrap() -> None:
    log("bootstrap: open 抖音商城 → search 女鞋")
    task = (
        "打开抖音 App，进入'商城'tab，点击顶部搜索框，输入'女鞋'并点击搜索。"
        "等待结果列表出现后 finish(message=\"ready\")。"
    )
    out = run_autoglm(task, max_steps=25, timeout=480)
    log(f"bootstrap finish: {last_finish_message(out)!r}")


def reset_to_results(seen_count: int) -> None:
    log(f"reset: back to 女鞋 search results, skip {seen_count} seen")
    task = (
        f"你现在在抖音 App 里。回到'商城'tab 的女鞋搜索结果列表页（按返回键多次直到看到搜索结果，"
        "或在搜索框输入'女鞋'重搜）。"
        f"然后向下滑动 {min(seen_count + 1, 8)} 屏跳过已看过的商品，让新商品出现在屏幕中央。"
        "完成后 finish(message=\"reset_done\")。"
    )
    out = run_autoglm(task, max_steps=25, timeout=480)
    log(f"reset finish: {last_finish_message(out)!r}")


def collect_one(idx: int) -> dict | None:
    log(f"collect #{idx}: hunt one product + copy link")
    sentinel = f"SENTINEL_{int(time.time())}_{idx}"
    pbcopy(sentinel)

    task = (
        f"今天是 {TODAY}。你在抖音 App 里。目标：找一款符合条件的女鞋，复制它的分享链接。\n"
        "UI 提示：商品详情页右上角 4 个图标：放大镜搜索、星收藏、向右上箭头分享、三点更多。"
        "分享按钮是向右上箭头那个，不是三点更多。\n"
        "流程：\n"
        "1. 当前可能在商城女鞋搜索结果列表，也可能已在某个商品详情页。"
        "如果在列表，挑可见的一款新鞋点进去；如果在商品页，直接用这款。\n"
        "2. 在商品页找已售或销量的数字，必须大于 40。不满足按返回键回列表选下一款（最多再试 2 次）。\n"
        f"3. 进入评价 tab 或滚动到评价区，看最新一条评价的日期是否在最近 3 天内（今天 {TODAY}）。"
        "不满足返回换下一款（最多再试 2 次）。\n"
        "4. 满足后回到商品页顶部，点右上角向右上箭头的分享图标。\n"
        "5. 在弹出的分享面板里点复制链接按钮，注意不要点其他按钮。\n"
        "6. 看到复制链接成功的提示后，按返回键关闭分享面板。\n"
        "7. 这时任务完成，调用 finish 动作，message 内容就是 COPIED 这六个字母。\n"
        "如果某一步实在做不到，调用 finish 动作，message 以 FAIL 开头加上简短原因。"
    )
    out = run_autoglm(task, max_steps=40, timeout=1200)
    msg = last_finish_message(out)
    log(f"  agent finish: {msg!r}")
    if not msg or msg.startswith("FAIL"):
        return None

    time.sleep(2)
    clip = pbpaste()
    if clip == sentinel or sentinel in clip:
        log(f"  clipboard not updated (still sentinel) — copy didn't sync")
        return None

    url = extract_url(clip)
    if not url:
        log(f"  no douyin URL in clipboard: {clip[:120]!r}")
        return None

    return {
        "idx": idx,
        "url": url,
        "clip": clip.strip(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    log(f"=== start TARGET={TARGET} MAX_ATTEMPTS={MAX_ATTEMPTS} ===")
    collected = load_existing()
    seen_urls = {c["url"] for c in collected}
    log(f"resume: {len(collected)} already collected")

    if not collected and not os.environ.get("SKIP_BOOTSTRAP"):
        bootstrap()

    attempt = 0
    while len(collected) < TARGET and attempt < MAX_ATTEMPTS:
        attempt += 1
        log(f"--- attempt {attempt}/{MAX_ATTEMPTS}  have {len(collected)}/{TARGET} ---")
        result = collect_one(len(collected) + 1)
        if result and result["url"] not in seen_urls:
            with LINKS_FILE.open("a") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            collected.append(result)
            seen_urls.add(result["url"])
            log(f"+1 url={result['url']}  total={len(collected)}/{TARGET}")
        elif result:
            log(f"duplicate url={result['url']}, skipping")
        if len(collected) < TARGET:
            reset_to_results(len(collected))

    log(f"=== done collected={len(collected)} attempts={attempt} ===")
    return 0 if len(collected) >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main())
