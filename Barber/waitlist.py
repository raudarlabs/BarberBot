"""
Waitlist: "tell me if a time frees up".

- In the bot's booking, the time step has a "🔔 Повідомити, якщо звільниться" button (new bookings
  with a chosen barber). It stores the client's wish: barber, service, day — and the start times
  that were free at that moment.
- Every 10 minutes (api/cron_reminders.py), and right after a client cancels in the bot, each
  wish is checked against Altegio's online-booking times. Times that were not free before are
  "freed": the client gets them with a button that opens booking straight on that day.
- A wish lives until its day has passed, the client removes it (/waitlist), or it was notified
  MAX_NOTIFY times. A chat keeps at most MAX_PER_CHAT wishes.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import texts as T
from altegio import AltegioError

logger = logging.getLogger(__name__)

WAITLIST = "bb:wl"     # hash: "<chat>:<staff>:<day>" -> entry json
MAX_PER_CHAT = 5
MAX_NOTIFY = 3
LEAD = timedelta(minutes=30)  # today: only times at least this far ahead are worth a message


def entry_id(chat: int, staff: int, day: str) -> str:
    return "%d:%d:%s" % (chat, staff, day)


async def _all(links) -> dict:
    raw = (await links._pipe(["HGETALL", WAITLIST]))[0] or []
    out = {}
    for i in range(0, len(raw) - 1, 2):
        try:
            out[str(raw[i])] = json.loads(raw[i + 1])
        except (TypeError, ValueError):
            continue
    return out


async def _save(links, eid: str, e: dict) -> None:
    await links._pipe(["HSET", WAITLIST, eid, json.dumps(e, ensure_ascii=False)])


async def _drop(links, *eids: str) -> None:
    if eids:
        await links._pipe(*[["HDEL", WAITLIST, e] for e in eids])


def _times(starts, now: datetime, day: date) -> list:
    return [s.strftime("%H:%M") for s in starts if day > now.date() or s >= now + LEAD]


async def add(links, chat: int, bk: dict, free_now: list, now: datetime) -> str:
    """Store a wish from the booking flow's state `bk`. Returns "added" / "updated" / "full"."""
    eid = entry_id(chat, bk["staff_id"], bk["day"])
    entries = await _all(links)
    mine = [k for k in entries if k.startswith("%d:" % chat)]
    if eid not in entries and len(mine) >= MAX_PER_CHAT:
        return "full"
    svc = bk.get("services", {}).get(bk.get("service_id"))
    await _save(links, eid, {
        "chat": chat, "loc": bk["loc"], "staff": bk["staff_id"], "staff_name": bk.get("staff_name", ""),
        "svc": bk.get("service_id"), "svc_title": getattr(svc, "title", "") if svc else "",
        "day": bk["day"], "seen": free_now, "sent": 0, "at": int(time.time()),
    })
    return "updated" if eid in entries else "added"


def freed_markup(e: dict, eid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Записатися на %s" % T.day_button(date.fromisoformat(e["day"])),
                              callback_data="bk:wd:%d:%d:%d:%s" % (e["loc"], e["staff"], e.get("svc") or 0,
                                                                   e["day"].replace("-", "")))],
        [InlineKeyboardButton("🔕 Більше не чекаю", callback_data="wl:rm:" + eid)],
    ])


async def run(client, links, bot, now: Optional[datetime] = None, only: Optional[tuple] = None) -> int:
    """Check the wishes (only those for `only` = (staff_id, "YYYY-MM-DD") when given).
    Returns how many clients were told about freed times."""
    now = now or datetime.now(client.tz)
    entries = await _all(links)
    stale, told = [], 0
    for eid, e in entries.items():
        day = date.fromisoformat(e["day"])
        if day < now.date() or e.get("sent", 0) >= MAX_NOTIFY:
            stale.append(eid)
            continue
        if only and (e["staff"], e["day"]) != (only[0], only[1]):
            continue
        try:
            starts = await client.booking_times(e["loc"], e["staff"], day, [e["svc"]] if e.get("svc") else [])
        except AltegioError as err:
            logger.warning("waitlist %s: %s", eid, err)
            continue
        free = _times(starts, now, day)
        if not free and day == now.date() and now.hour >= 21:
            stale.append(eid)
            continue
        new = [t for t in free if t not in set(e.get("seen") or [])]
        e["seen"] = free
        if new:
            text = "🔔 Звільнився час у %s на %s%s:\n%s\n\nВстигніть записатися 👇" % (
                e.get("staff_name") or "майстра", T.day_button(day),
                (" («%s»)" % e["svc_title"]) if e.get("svc_title") else "", ", ".join(new[:8]))
            try:
                await bot.send_message(chat_id=e["chat"], text=text, reply_markup=freed_markup(e, eid))
                told += 1
                e["sent"] = e.get("sent", 0) + 1
            except (Forbidden, BadRequest) as err:
                logger.info("waitlist: chat %s refused: %s", e["chat"], err)
                stale.append(eid)
                continue
            except TelegramError as err:
                logger.warning("waitlist: send to %s failed: %s", e["chat"], err)
                e["seen"] = [t for t in e["seen"] if t not in new]  # retry next pass
        await _save(links, eid, e)
    await _drop(links, *stale)
    return told


# ------------------------------------------------------------------ chat side

async def on_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    links = context.application.bot_data.get("links")
    eid = q.data[len("wl:rm:"):]
    if links is None or not eid.startswith("%d:" % update.effective_chat.id):
        await q.answer()
        return
    await _drop(links, eid)
    await q.answer("Прибрав")
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    if q.message is not None and "Ваш лист очікування" in (q.message.text or ""):
        await show(update, context)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/waitlist: the chat's wishes, each with a remove button."""
    links = context.application.bot_data.get("links")
    chat = update.effective_chat
    mine = {k: v for k, v in (await _all(links)).items() if k.startswith("%d:" % chat.id)} if links else {}
    if not mine:
        await chat.send_message("Ви ні на що не чекаєте. Під час запису, на кроці вибору часу, натисніть "
                                "«🔔 Повідомити, якщо звільниться» — і я напишу, щойно зʼявиться вільний час.")
        return
    lines, rows = ["🔔 Ваш лист очікування:"], []
    for eid, e in sorted(mine.items(), key=lambda kv: kv[1]["day"]):
        d = T.day_button(date.fromisoformat(e["day"]))
        lines.append("• %s — %s%s" % (d, e.get("staff_name") or "майстер", (", " + e["svc_title"]) if e.get("svc_title") else ""))
        rows.append([InlineKeyboardButton("🔕 Прибрати: %s, %s" % (d, (e.get("staff_name") or "").split()[0] if e.get("staff_name") else ""),
                                          callback_data="wl:rm:" + eid)])
    await chat.send_message("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


def handlers():
    return [CommandHandler("waitlist", show), CallbackQueryHandler(on_remove, pattern=r"^wl:rm:\d+:\d+:[\d-]+$")]
