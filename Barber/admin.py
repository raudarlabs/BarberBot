"""
Admin panel inside the bot — only for chats listed in ADMIN_CHAT_IDS (others never see it).

    /admin → 📋 Записи сьогодні / завтра · 🕳 Вільні вікна сьогодні / завтра · 📊 Статистика

Plus the text of the 9:00 morning digest (sent by api/cron_digest.py).
Reads Altegio through the bot's own client and the shared store for statistics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import admin_notify
import texts as T
from altegio import AltegioError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _games  # noqa: E402
import _kv  # noqa: E402

logger = logging.getLogger(__name__)

GAME_NAMES = {"dryer": "Фен-пілот", "fade": "Ідеальний фейд", "beard": "Борода-ніндзя",
              "snake": "Змійка", "blocks": "Укладка"}
MIN_WINDOW_MINUTES = 30
MAX_MESSAGE = 4000  # Telegram's limit is 4096


def is_admin(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id in admin_notify.admin_chat_ids()


def panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Записи сьогодні", callback_data="adm:appts:0"),
         InlineKeyboardButton("📋 Завтра", callback_data="adm:appts:1")],
        [InlineKeyboardButton("🕳 Вікна сьогодні", callback_data="adm:free:0"),
         InlineKeyboardButton("🕳 Завтра", callback_data="adm:free:1")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton("☀️ Дайджест", callback_data="adm:digest")],
    ])


def _day_title(d: date) -> str:
    return "%s, %s" % (T.WEEKDAYS[d.weekday()], d.strftime("%d.%m"))


def _via_bot(a) -> bool:
    return (a.api_id or "").startswith("tg-") or "Telegram" in (a.comment or "")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 20] + "\n… (обрізано)"


# ------------------------------------------------------------------ appointments

async def _staff_names(client, loc) -> Dict[int, str]:
    try:
        return {s.id: s.name for s in await client.list_staff(loc)}
    except AltegioError:
        return {}


async def day_appointments(client, loc, day: date):
    start = datetime(day.year, day.month, day.day, tzinfo=client.tz)
    appts = await client.list_appointments(loc, start, start + timedelta(days=1))
    return sorted((a for a in appts if a.status != "cancelled" and a.start.date() == day), key=lambda a: a.start)


async def appointments_text(client, locations: Dict[int, str], day: date) -> str:
    lines, total, via_bot = [], 0, 0
    for loc, address in locations.items():
        appts = await day_appointments(client, loc, day)
        names = await _staff_names(client, loc) if any(not a.staff_name for a in appts) else {}
        by_staff: Dict[str, list] = {}
        for a in appts:
            by_staff.setdefault(a.staff_name or names.get(a.staff_id, "#%d" % a.staff_id), []).append(a)
        if len(locations) > 1:
            lines.append("\n📍 " + address)
        for staff, items in by_staff.items():
            lines.append("\n💈 %s — %d" % (staff, len(items)))
            for a in items:
                total += 1
                bot = _via_bot(a)
                via_bot += bot
                phone = " +" + a.client_phone if a.client_phone else ""
                lines.append("  %s · %s%s · %s%s%s" % (
                    a.start.strftime("%H:%M"), a.client_name or "—", phone, a.service_titles or "—",
                    " 🤖" if bot else "", " ✅" if a.status == "confirmed" else ""))
    head = "📋 Записи — %s: %d (через бот: %d)" % (_day_title(day), total, via_bot)
    if not total:
        return head + "\n\nЗаписів немає."
    return _clip(head + "\n" + "\n".join(lines) + "\n\n🤖 через бот · ✅ клієнт підтвердив")


# ------------------------------------------------------------------ free windows

def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def free_ranges(grid, day: date, now: Optional[datetime] = None,
                min_minutes: int = MIN_WINDOW_MINUTES) -> List[Tuple[str, str]]:
    """Altegio's 5-minute grid [{time, is_free}] → free stretches of at least `min_minutes`.
    For today, the past is cut off at `now`."""
    cells = sorted((_minutes(c["time"]), bool(c.get("is_free"))) for c in grid if isinstance(c, dict) and c.get("time"))
    if not cells:
        return []
    step = min((b[0] - a[0] for a, b in zip(cells, cells[1:]) if b[0] > a[0]), default=5)
    floor = None
    if now is not None and now.date() == day:
        floor = now.hour * 60 + now.minute
    out, start, prev = [], None, None
    for m, free in cells + [(10 ** 6, False)]:
        if free and floor is not None and m < floor:
            free = False
        if free and start is None:
            start = m
        if start is not None and (not free or (prev is not None and m - prev > step)):
            end = prev + step
            if end - start >= min_minutes:
                out.append(("%02d:%02d" % divmod(start, 60), "%02d:%02d" % divmod(end, 60)))
            start = m if free else None
        prev = m
    return out


async def free_windows(client, loc, day: date, staff=None, now=None):
    """[(barber name, [(from, to), ...] or None for a day off)]."""
    staff = staff if staff is not None else await client.list_staff(loc)
    out = []
    for s in staff:
        grid = await client.day_grid(loc, s.id, day)
        out.append((s.name, free_ranges(grid, day, now) if grid else None))
    return out


async def free_windows_text(client, locations: Dict[int, str], day: date) -> str:
    now = datetime.now(client.tz)
    lines = ["🕳 Вільні вікна — %s" % _day_title(day)]
    for loc, address in locations.items():
        if len(locations) > 1:
            lines.append("\n📍 " + address)
        for name, ranges in await free_windows(client, loc, day, now=now):
            if ranges is None:
                lines.append("💈 %s: вихідний" % name)
            else:
                lines.append("💈 %s: %s" % (name, ", ".join("%s–%s" % r for r in ranges) if ranges else "все зайнято"))
    return _clip("\n".join(lines) + "\n\nВікна від %d хв за розкладом Altegio." % MIN_WINDOW_MINUTES)


# ------------------------------------------------------------------ statistics

def _event_counts(ts=None) -> Dict[str, Dict[str, int]]:
    """{event: {"today", "week", "month", "all"}} for the bot's own activity."""
    days = _games.last_days(7, ts)
    cmds, slots = [], []
    for ev in admin_notify.BOT_EVENTS:
        base = "stats:bot:%s" % ev
        for period, key in (("all", base), ("month", "%s:%s" % (base, _games.month_id(ts))),
                            ("today", "%s:%s" % (base, _games.day_id(ts)))):
            slots.append((ev, period)); cmds.append(["GET", key])
        for d in days:
            slots.append((ev, "week")); cmds.append(["GET", "%s:%s" % (base, d)])
    out = {ev: {"today": 0, "week": 0, "month": 0, "all": 0} for ev in admin_notify.BOT_EVENTS}
    for (ev, period), v in zip(slots, _kv.pipeline(*cmds)):
        out[ev][period] += int(float(v)) if v is not None else 0
    return out


def _chat_counts(now=None) -> Dict[str, int]:
    raw = _kv.pipeline(["HGETALL", "bb:chats"])[0] or []
    now = now or time.time()
    total = today = week = 0
    for i in range(1, len(raw), 2):
        last = (json.loads(raw[i]) or {}).get("last", 0)
        total += 1
        today += now - last < 86400
        week += now - last < 7 * 86400
    return {"total": total, "day": today, "week": week}


def stats_text() -> str:
    import stats as arcade_stats  # api/stats.py
    ev = _event_counts()
    chats = _chat_counts()
    games = arcade_stats.collect()
    labels = (("booked", "записи"), ("moved", "переноси"), ("cancelled", "скасування"), ("reminded", "нагадування"))
    lines = ["📊 Статистика · %s" % games["today"], "", "🤖 Бот — сьогодні · 7 днів · місяць · всього"]
    for key, label in labels:
        c = ev[key]
        lines.append("  %s: %d · %d · %d · %d" % (label, c["today"], c["week"], c["month"], c["all"]))
    lines += ["", "👥 Клієнти бота: %d (активні сьогодні: %d, за 7 днів: %d)" % (chats["total"], chats["day"], chats["week"])]
    g = games["games"]
    lines += ["", "🎮 Ігри — партії сьогодні · місяць · всього"]
    for gid, name in GAME_NAMES.items():
        x = g.get(gid) or {}
        p = x.get("plays", {})
        best = x.get("best_month")
        lines.append("  %s: %d · %d · %d | гравців міс.: %s%s" % (
            name, p.get("today", 0), p.get("month", 0), p.get("all", 0), x.get("players", {}).get("month", 0),
            " | рекорд: %d" % best if best else ""))
    guests = sum((g.get(gid) or {}).get("guest", {}).get("month", 0) for gid in GAME_NAMES)
    hub = (g.get("hub") or {}).get("opens", {})
    lines.append("  без рейтингу (стара кнопка) за місяць: %d" % guests)
    lines.append("  хаб відкривали: сьогодні %d · місяць %d" % (hub.get("today", 0), hub.get("month", 0)))
    lines += ["", "Детальніше по днях: /stats.html (ключ персоналу)"]
    return "\n".join(lines)


# ------------------------------------------------------------------ morning digest

async def digest_text(client, locations: Dict[int, str], now: datetime) -> str:
    today = now.date()
    yesterday_ts = now.timestamp() - 86400
    total, via_bot = 0, 0
    for loc in locations:
        appts = await day_appointments(client, loc, today)
        total += len(appts)
        via_bot += sum(1 for a in appts if _via_bot(a))
    y = {}
    try:
        keys = {ev: "stats:bot:%s:%s" % (ev, _games.day_id(yesterday_ts)) for ev in ("booked", "moved", "cancelled")}
        vals = await asyncio.to_thread(_kv.pipeline, *[["GET", k] for k in keys.values()])
        y = {ev: int(float(v)) if v else 0 for ev, v in zip(keys, vals)}
    except Exception as e:  # noqa: BLE001
        logger.warning("digest counters: %s", e)
    title = _day_title(today)
    lines = ["☀️ Доброго ранку! %s" % (title[:1].upper() + title[1:]),
             "📋 Записів сьогодні: %d (через бот: %d)" % (total, via_bot)]
    if y:
        lines.append("🤖 Вчора через бот: записів %d · переносів %d · скасувань %d"
                     % (y["booked"], y["moved"], y["cancelled"]))
    lines.append("")
    lines.append("🕳 Вільні вікна сьогодні:")
    working = False
    for loc in locations:
        for name, ranges in await free_windows(client, loc, today, now=now):
            working = working or ranges is not None
            if ranges:
                lines.append("💈 %s: %s" % (name, ", ".join("%s–%s" % r for r in ranges)))
    if lines[-1] == "🕳 Вільні вікна сьогодні:":
        lines[-1] = "🕳 Вільних вікон сьогодні немає 💪" if working else "🌙 Сьогодні вихідний"
    lines += ["", "/admin — записи, вікна, статистика"]
    return _clip("\n".join(lines))


# ------------------------------------------------------------------ handlers

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.effective_chat.send_message(T.FALLBACK, reply_markup=context.application.bot_data.get("main_menu"))
        return
    await update.effective_chat.send_message("🛠 Адмін-панель Custom True", reply_markup=panel())


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(update):
        await q.answer()
        return
    bd = context.application.bot_data
    client, locations = bd.get("altegio"), bd.get("locations") or {}
    parts = q.data.split(":")
    await q.answer("⏳")
    try:
        if parts[1] == "stats":
            text = await asyncio.to_thread(stats_text)
        elif parts[1] == "digest" and client is not None:
            text = await digest_text(client, locations, datetime.now(client.tz))
        elif client is None:
            text = T.UNAVAILABLE
        else:
            day = datetime.now(client.tz).date() + timedelta(days=int(parts[2]))
            text = await (appointments_text if parts[1] == "appts" else free_windows_text)(client, locations, day)
    except AltegioError as e:
        logger.warning("admin %s: %s", q.data, e)
        text = T.API_ERROR
    try:
        await q.edit_message_text(text, reply_markup=panel())
    except BadRequest:  # same text as before, or the message is too old to edit
        await update.effective_chat.send_message(text, reply_markup=panel())


def handlers():
    return [
        CommandHandler("admin", admin_command),
        CallbackQueryHandler(on_admin_button, pattern=r"^adm:(appts:[01]|free:[01]|stats|digest)$"),
    ]
