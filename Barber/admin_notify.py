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


async def count_event(name: str, n: int = 1) -> None:
    """Best effort: +n to stats:bot:<name> for all time, this month and today."""
    if n <= 0:
        return
    try:
        await asyncio.to_thread(_kv.pipeline, *[["INCR", k] for k in _games.stat_keys("bot", name) for _ in range(n)])
    except Exception as e:  # noqa: BLE001 - analytics must never break the client's flow
        logger.warning("count_event %s failed: %s", name, e)


def admin_chat_ids(env=None) -> List[int]:
    env = os.environ if env is None else env
    out = []
    for part in (env.get("ADMIN_CHAT_IDS") or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


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
    """Send `text` to every admin chat. Returns how many got it."""
    bot = getattr(context, "bot", None)
    ids = admin_chat_ids()
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
