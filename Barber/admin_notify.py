"""
Telegram notifications to the shop's admins about what clients do through the bot:
a new booking, a moved visit, a cancelled visit.

    ADMIN_CHAT_IDS=123456789,987654321   (numeric Telegram ids; each must have pressed /start in the bot)

Best effort: a failed notification is logged and never affects the client's flow.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import List, Optional

from telegram.error import TelegramError

import texts as T

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _games  # noqa: E402
import _kv  # noqa: E402

# Bot activity counters (all time / Kyiv month / Kyiv day), shown in the admin panel and digest.
BOT_EVENTS = ("booked", "moved", "cancelled", "reminded")


# Booking funnel: how far each booking dialog got (booking._step), counted once per dialog.
FUNNEL = ("start", "barber", "service", "day", "time", "phone", "booked")


def funnel_counts(ts=None) -> dict:
    """{step: {"today", "week", "month", "all"}} from the stats:bot:funnel_<step> counters."""
    days = _games.last_days(7, ts)
    cmds, slots = [], []
    for step in FUNNEL:
        base = "stats:bot:funnel_%s" % step
        for period, key in (("all", base), ("month", "%s:%s" % (base, _games.month_id(ts))),
                            ("today", "%s:%s" % (base, _games.day_id(ts)))):
            slots.append((step, period))
            cmds.append(["GET", key])
        for d in days:
            slots.append((step, "week"))
            cmds.append(["GET", "%s:%s" % (base, d)])
    out = {s: {"today": 0, "week": 0, "month": 0, "all": 0} for s in FUNNEL}
    for (step, period), v in zip(slots, _kv.pipeline(*cmds)):
        out[step][period] += int(float(v)) if v is not None else 0
    return out


async def count_event(name: str, n: int = 1) -> None:
    """Best effort: +n to stats:bot:<name> for all time, this month and today."""
    if n <= 0:
        return
    try:
        await asyncio.to_thread(_kv.pipeline, *[["INCR", k] for k in _games.stat_keys("bot", name) for _ in range(n)])
    except Exception as e:  # noqa: BLE001 - analytics must never break the client's flow
        logger.warning("count_event %s failed: %s", name, e)


def _ids(raw: str) -> List[int]:
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def admin_chat_ids(env=None) -> List[int]:
    """Who may open /admin and the dashboard."""
    env = os.environ if env is None else env
    return _ids(env.get("ADMIN_CHAT_IDS"))


# The shop's admin account @Custom_true_barbershop: every notification goes there (owner's choice,
# Nov 2026) — new / moved / cancelled bookings, low ratings and comments, the 9:00 digest and the
# 21:00 unmarked visits. NOTIFY_CHAT_IDS overrides; "admins" sends to ADMIN_CHAT_IDS instead.
DEFAULT_NOTIFY = "1051263236"


def notify_chat_ids(env=None) -> List[int]:
    """Who receives the bot's notifications (not the same as who may open /admin)."""
    env = os.environ if env is None else env
    raw = env.get("NOTIFY_CHAT_IDS", DEFAULT_NOTIFY)
    return admin_chat_ids(env) if raw.strip().lower() == "admins" else _ids(raw)


def user_name(user) -> str:
    if user is None:
        return ""
    full = getattr(user, "full_name", None)
    if full:
        return full
    return " ".join(x for x in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if x)


def _who(name: str, phone: str, username: Optional[str] = None) -> str:
    bits = [name or "клієнт"]
    if phone:
        bits.append("+" + phone.lstrip("+"))
    if username:
        bits.append("@" + username)
    return " · ".join(bits)


def booked_text(name, phone, username, staff, service, when, location) -> str:
    return "🆕 Новий запис через бот\n👤 %s\n💈 %s · %s\n🕒 %s\n📍 %s" % (
        _who(name, phone, username), staff, service, when, location)


def moved_text(name, phone, username, staff, old_when, new_when) -> str:
    return "🔁 Перенесення через бот\n👤 %s\n💈 %s\n🕒 було %s → стало %s" % (
        _who(name, phone, username), staff, old_when, new_when)


def cancelled_text(name, phone, username, staff, service, when) -> str:
    return "❌ Скасування через бот\n👤 %s\n💈 %s%s\n🕒 %s" % (
        _who(name, phone, username), staff, (" · " + service) if service else "", when)


async def notify_admins(context, text: str) -> int:
    """Send `text` to the notification chats (notify_chat_ids). Returns how many got it."""
    bot = getattr(context, "bot", None)
    ids = notify_chat_ids()
    if bot is None or not ids:
        return 0
    sent = 0
    for chat_id in ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except TelegramError as e:
            logger.warning("admin notify to %s failed: %s", chat_id, e)
    return sent


__all__ = ["admin_chat_ids", "notify_admins", "booked_text", "moved_text", "cancelled_text", "T"]
