"""Account pool for rotating Douyin Mall accounts to dodge risk control.

State persisted to results/accounts.json. Configure pool via env:
  ACC_IDS="acc1,acc2,acc3"     # short labels you give each logged-in account
  ACC_DAILY_CAP=80              # max product details / account / day
  ACC_COOLDOWN_HRS=24           # how long a risk-flagged account stays parked

An account is "fatigued" if today's ops >= ACC_DAILY_CAP, or it's still
inside its cooling window. pick_next() returns the freshest healthy one.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "results" / "accounts.json"

DAILY_CAP = int(os.environ.get("ACC_DAILY_CAP", "80"))
COOL_DOWN_HOURS = int(os.environ.get("ACC_COOLDOWN_HRS", "24"))


@dataclass
class Account:
    id: str
    ops: int = 0
    day: str = ""
    last_used: str = ""
    cooling_until: str = ""
    risk_hits: int = 0


@dataclass
class Pool:
    accounts: list[Account] = field(default_factory=list)
    current: str = ""

    def to_dict(self) -> dict:
        return {
            "accounts": [asdict(a) for a in self.accounts],
            "current": self.current,
        }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_cooling(a: Account) -> bool:
    if not a.cooling_until:
        return False
    try:
        return datetime.fromisoformat(a.cooling_until) > datetime.now()
    except ValueError:
        return False


def _roll_day(accs: list[Account]) -> None:
    today = _today()
    for a in accs:
        if a.day != today:
            a.ops = 0
            a.day = today


def load_pool() -> Pool:
    if not STATE_FILE.exists():
        ids = [x.strip() for x in os.environ.get("ACC_IDS", "").split(",") if x.strip()]
        accs = [Account(id=i, day=_today()) for i in ids]
        pool = Pool(accounts=accs, current=ids[0] if ids else "")
        if accs:
            save_pool(pool)
        return pool
    data = json.loads(STATE_FILE.read_text())
    accs = [Account(**a) for a in data.get("accounts", [])]
    _roll_day(accs)
    current = data.get("current", "")
    if not current and accs:
        current = accs[0].id
    return Pool(accounts=accs, current=current)


def save_pool(p: Pool) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))


def healthy(p: Pool) -> list[Account]:
    return [a for a in p.accounts if a.ops < DAILY_CAP and not _is_cooling(a)]


def current_account(p: Pool) -> Account | None:
    for a in p.accounts:
        if a.id == p.current:
            return a
    return None


def mark_used(p: Pool, acc_id: str) -> None:
    for a in p.accounts:
        if a.id == acc_id:
            a.ops += 1
            a.last_used = _now_iso()
            break
    save_pool(p)


def mark_risk(p: Pool, acc_id: str) -> None:
    until = (datetime.now() + timedelta(hours=COOL_DOWN_HOURS)).isoformat(timespec="seconds")
    for a in p.accounts:
        if a.id == acc_id:
            a.cooling_until = until
            a.risk_hits += 1
            break
    save_pool(p)


def pick_next(p: Pool, exclude: str = "") -> Account | None:
    candidates = [a for a in healthy(p) if a.id != exclude]
    if not candidates:
        return None
    return min(candidates, key=lambda a: (a.ops, a.last_used or ""))


def set_current(p: Pool, acc_id: str) -> None:
    p.current = acc_id
    save_pool(p)


def summary(p: Pool) -> str:
    parts = []
    for a in p.accounts:
        flag = "*" if a.id == p.current else " "
        cool = " [cooling]" if _is_cooling(a) else ""
        parts.append(f"{flag}{a.id}: ops={a.ops}/{DAILY_CAP} risk={a.risk_hits}{cool}")
    return " | ".join(parts) if parts else "(empty pool)"
