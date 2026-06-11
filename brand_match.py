#!/usr/bin/env python3
"""Brand detection for Douyin Mall product titles & detail-page text.

Only "well-known" brands (国民度 / 国际大牌 / 头部连锁) are listed here so
white-label / factory-direct products are not falsely filtered.

Curation principles:
  - prefer multi-character names (≥2 chars Chinese, ≥3 chars Latin) to avoid
    accidental substring hits
  - skip single-char or generic words that have non-brand meanings
    (e.g. "EVA" = material, "UR" = generic pronoun, 中文"保罗" = 人名歧义)
  - aliases ordered longest-first inside each brand for greedy match
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# (canonical_name, [aliases])
KNOWN_BRANDS: list[tuple[str, list[str]]] = [
    # ── 国际运动头部 ──────────────────────────────────────
    ("Nike",          ["Nike", "NIKE", "耐克"]),
    ("Adidas",        ["Adidas", "ADIDAS", "阿迪达斯"]),
    ("Puma",          ["Puma", "PUMA", "彪马"]),
    ("New Balance",   ["New Balance", "NewBalance", "NEW BALANCE", "新百伦"]),
    ("Vans",          ["Vans", "VANS", "万斯"]),
    ("Converse",      ["Converse", "CONVERSE", "匡威"]),
    ("FILA",          ["FILA", "Fila", "斐乐"]),
    ("Skechers",      ["Skechers", "SKECHERS", "斯凯奇"]),
    ("Asics",         ["Asics", "ASICS", "亚瑟士"]),
    ("Reebok",        ["Reebok", "REEBOK", "锐步"]),
    ("Under Armour",  ["Under Armour", "UNDER ARMOUR", "安德玛"]),

    # ── 国内运动 ──────────────────────────────────────
    ("安踏",          ["安踏", "ANTA"]),
    ("李宁",          ["李宁", "Li-Ning", "LI-NING", "LiNing"]),
    ("特步",          ["特步", "XTEP"]),
    ("361度",         ["361度", "361°"]),
    ("乔丹中国",      ["乔丹中国"]),  # 中文"乔丹"易和 Air Jordan 混

    # ── 户外 ──────────────────────────────────────
    ("The North Face",["The North Face", "TheNorthFace", "北面", "TNF"]),
    ("Columbia",      ["Columbia", "哥伦比亚"]),
    ("骆驼",          ["骆驼", "CAMEL"]),  # CAMEL/骆驼 为户外品牌

    # ── 快时尚连锁 ──────────────────────────────────────
    ("ZARA",          ["ZARA", "Zara"]),
    ("H&M",           ["H&M", "H & M"]),
    ("UNIQLO",        ["UNIQLO", "Uniqlo", "优衣库"]),
    ("MUJI",          ["MUJI", "无印良品"]),
    ("Urban Revivo",  ["URBAN REVIVO", "Urban Revivo"]),  # 不用 UR 缩写
    ("GAP",           ["GAP", "Gap"]),

    # ── 国内服饰 ──────────────────────────────────────
    ("真维斯",        ["真维斯", "JEANSWEST", "Jeanswest"]),
    ("森马",          ["森马", "SENMA", "Senma"]),
    ("美特斯邦威",    ["美特斯邦威", "Metersbonwe"]),
    ("七匹狼",        ["七匹狼", "SEPTWOLVES"]),
    ("以纯",          ["以纯", "YISHION"]),
    ("唐狮",          ["唐狮", "TONLION"]),
    ("太平鸟",        ["太平鸟", "PEACEBIRD"]),
    ("热风",          ["热风", "HOTWIND"]),
    ("海澜之家",      ["海澜之家", "HEILAN HOME", "HLA"]),
    ("雅戈尔",        ["雅戈尔", "YOUNGOR"]),
    ("波司登",        ["波司登", "Bosideng", "BOSIDENG"]),
    ("拉夏贝尔",      ["拉夏贝尔", "La Chapelle"]),
    ("江南布衣",      ["江南布衣", "JNBY"]),
    ("MO&Co.",        ["MO&Co", "MO & Co"]),
    ("ONLY",          ["ONLY"]),    # 注意：单独 ONLY 易误判，仅大写
    ("VeroModa",      ["VeroModa", "Vero Moda", "VERO MODA"]),

    # ── 鞋类知名 ──────────────────────────────────────
    ("百丽",          ["百丽", "BELLE", "Belle"]),
    ("达芙妮",        ["达芙妮", "DAPHNE"]),
    ("天美意",        ["天美意", "Teenmix"]),
    ("奥康",          ["奥康", "AOKANG"]),
    ("康奈",          ["康奈", "KANGNAI"]),
    ("红蜻蜓",        ["红蜻蜓"]),
    ("回力",          ["回力", "Warrior", "WARRIOR"]),
    ("飞跃",          ["FEIYUE", "feiyue"]),  # 中文'飞跃'歧义太大，只用拼音/英文

    # ── 抖音商城常见连锁 ──────────────────────────────────────
    ("SHOEBOX鞋柜",   ["SHOEBOX鞋柜", "SHOEBOX", "shoebox"]),
    ("Tagabe",        ["Tagabe", "TAGABE", "tagabe"]),
    ("啄木鸟",        ["啄木鸟"]),

    # ── 奢侈与国际时尚 ──────────────────────────────────────
    ("Polo Ralph Lauren", ["Polo Ralph Lauren", "POLO RALPH"]),  # 不匹配单独"保罗"
    ("Tommy Hilfiger",["Tommy Hilfiger", "TOMMY"]),
    ("Calvin Klein",  ["Calvin Klein", "CK"]),
    ("Levis",         ["Levi's", "LEVI'S", "Levis"]),
    ("Lee",           ["Lee Jeans", "LEE JEANS"]),  # 不匹配单独 "Lee"
]


# 显式排除（避免误判）：以下出现在 title 中即使匹配也不算品牌
_EXCLUDE_CONTEXTS = (
    "EVA",          # 乙烯-醋酸乙烯材质
    "PU",           # 聚氨酯
)


# 构建 (regex, canonical) — 按 alias 长度降序，长的优先
_PATTERNS: list[tuple[re.Pattern, str]] = []
for canonical, aliases in KNOWN_BRANDS:
    for alias in aliases:
        # \b 边界对中英文混合不可靠；用 lookaround 排除前后字母数字相邻
        # 但中文不存在 \b，直接 escape 就行
        _PATTERNS.append((re.compile(re.escape(alias), re.IGNORECASE), canonical))
_PATTERNS.sort(key=lambda p: -len(p[0].pattern))


def match_brand(text: str) -> str:
    """Return canonical brand name, or empty string if none matched."""
    if not text:
        return ""
    for pat, canonical in _PATTERNS:
        if pat.search(text):
            return canonical
    return ""


def match_brand_in_tags(tags: list[str]) -> str:
    """Detect a brand by scanning a list of detail-page tag chips.
    The first tag right before '品牌授权' is almost always the brand name."""
    if not tags:
        return ""
    for i, tag in enumerate(tags):
        if "品牌授权" in tag and i > 0:
            b = match_brand(tags[i - 1])
            if b:
                return b
    # Fallback: try each tag
    for tag in tags[:3]:
        b = match_brand(tag)
        if b:
            return b
    return ""


def tag_file(in_path: Path, out_path: Path | None = None) -> int:
    out_path = out_path or in_path
    records = [json.loads(l) for l in in_path.read_text().splitlines() if l.strip()]
    hits = 0
    for r in records:
        b = match_brand(r.get("title", ""))
        if not b:
            b = match_brand_in_tags(r.get("tags", []))
        r["brand"] = b
        if b:
            hits += 1
    with out_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"tagged {hits}/{len(records)} records → {out_path}")
    return 0


if __name__ == "__main__":
    in_p = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results" / "u2_links.jsonl"
    out_p = Path(sys.argv[2]) if len(sys.argv) > 2 else in_p
    sys.exit(tag_file(in_p, out_p))
