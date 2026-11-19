"""
"Time for a haircut?": a month after a client's last visit the bot invites them back, with one
button that books the same barber and the same service again — only the day and time are left
to pick.

- Runs inside the 10-minute cron (api/cron_reminders.py), once a day between SEND_HOURS in the
  shop's time (a per-day mark), and only with COMEBACK_ENABLED=1.
- Candidates: visits COMEBACK_DAYS ago (with CATCH_UP_DAYS of slack, so a missed day is caught up)
  that happened ("arrived", or "confirmed" that nobody marked), whose client's Telegram is linked
  (reminders.find_chat) and who did not /stop_reminders or press "🔕 Не нагадувати".
- Skipped when the client has any later visit: already back, or already booked for the next
  LOOKAHEAD_DAYS. At most one invitation per chat per COOLDOWN, once per visit.
- The barber left / is hidden: the invitation offers any barber instead of the repeat button.

Buttons: cb:rp:{loc}:{appointment} -> booking.start_repeat (same barber + service -> day picker),
bk:new -> the normal booking, cb:off -> no more invitations to this chat.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

import texts as T
from altegio import AltegioError, Appointment
from reminders import find_chat

logger = logging.getLogger(__name__)

COMEBACK_DAYS = int(os.getenv("COMEBACK_DAYS", "30"))
CATCH_UP_DAYS = 3
LOOKAHEAD_DAYS = 60
COOLDOWN = 21 * 86400
SEND_HOURS = (12, 20)  # [from, to) in the shop's time
SENT_KIND = "comeback"
CAME_BACK = ("waiting", "confirmed", "arrived")  # a later visit with one of these: no invitation


def enabled() -> bool:
    return os.environ.get("COMEBACK_ENABLED") == "1"


def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def message(appt: Appointment, staff_name: str, staff_active: bool):
    loc = appt.location_id
    service = appt.service_titles if len(appt.services) == 1 else ""
    if staff_active and staff_name:
        text = T.COMEBACK.format(staff=staff_name)
        label = T.COMEBACK_REPEAT_SERVICE.format(name=_first(staff_name), service=service) if service \
            else T.COMEBACK_REPEAT.format(name=_first(staff_name))
        rows = [[InlineKeyboardButton(label, callback_data="cb:rp:%d:%d" % (loc, appt.id))],
                [InlineKeyboardButton(T.COMEBACK_OTHER, callback_data="bk:new")]]
    else:
        text = T.COMEBACK_NO_STAFF
        rows = [[InlineKeyboardButton(T.VISITS_BOOK, callback_data="bk:new")]]
    rows.append([InlineKeyboardButton(T.COMEBACK_OFF, callback_data="cb:off")])
    return text, InlineKeyboardMarkup(rows)


async def _off(links, chat_id: int) -> bool:
    return bool((await links._pipe(["GET", "bb:cbout:%d" % chat_id]))[0])


async def _cooling(links, chat_id: int) -> bool:
    return bool((await links._pipe(["GET", "bb:cbchat:%d" % chat_id]))[0])


async def run(client, links, locations: Dict[int, str], bot, now: Optional[datetime] = None,
              force: bool = False) -> int:
    """One daily pass. Returns how many invitations were sent. `force` skips the switch,
    the hour and the once-a-day mark (tests, manual runs)."""
    now = now or datetime.now(client.tz)
    if not force:
        if not enabled() or not (SEND_HOURS[0] <= now.hour < SEND_HOURS[1]):
            return 0
        mark = (await links._pipe(["SET", "bb:cbday:%s" % now.date().isoformat(), "1", "EX", 2 * 86400, "NX"]))[0]
        if mark is None:
            return 0  # already ran today
    first_day = now.date() - timedelta(days=COMEBACK_DAYS + CATCH_UP_DAYS - 1)
    last_day = now.date() - timedelta(days=COMEBACK_DAYS)
    lo = datetime(first_day.year, first_day.month, first_day.day, tzinfo=client.tz)
    hi = datetime(last_day.year, last_day.month, last_day.day, tzinfo=client.tz) + timedelta(days=1)
    sent, chats_done = 0, set()
    for loc in locations:
        try:
            past = await client.list_appointments(loc, lo, hi)
        except AltegioError as e:
            logger.warning("comeback: cannot list visits for %s: %s", loc, e)
            continue
        candidates = []
        for a in sorted(past, key=lambda x: x.start, reverse=True):  # newest visit per client first
            if a.status not in ("arrived", "confirmed") or not (lo <= a.start < hi):
                continue
            if await links.was_sent(a.id, SENT_KIND):
                continue
            try:
                chat = await find_chat(a, client, links)
            except AltegioError as e:
                logger.warning("comeback: client lookup failed for %s: %s", a.id, e)
                continue
            if not chat or chat in chats_done:
                continue
            chats_done.add(chat)
            if await links.opted_out(chat) or await _off(links, chat) or await _cooling(links, chat):
                continue
            candidates.append((a, chat))
        if not candidates:
            continue
        try:
            later = await client.list_appointments(loc, min(a.start for a, _ in candidates),
                                                   now + timedelta(days=LOOKAHEAD_DAYS))
            staff = {s.id: s.name for s in await client.list_staff(loc)}
        except AltegioError as e:
            logger.warning("comeback: cannot check later visits for %s: %s", loc, e)
            continue
        for a, chat in candidates:
            phone = a.client_phone or await links.phone_for_chat(chat) or ""
            again = any(b.id != a.id and b.start > a.start and b.status in CAME_BACK and (
                (a.client_id and b.client_id == a.client_id) or (phone and b.client_phone == phone))
                for b in later)
            if again:
                await links.mark_sent(a.id, SENT_KIND)  # came back already: never ask about this visit
                continue
            name = a.staff_name or staff.get(a.staff_id, "")
            text, markup = message(a, name, a.staff_id in staff)
            try:
                await bot.send_message(chat_id=chat, text=text, reply_markup=markup)
                sent += 1
            except (Forbidden, BadRequest) as e:
                logger.info("comeback: chat %s refused: %s", chat, e)
            except TelegramError as e:
                logger.warning("comeback: send to %s failed, will retry: %s", chat, e)
                continue
            await links.mark_sent(a.id, SENT_KIND)
            await links.remember_appointment(loc, a.id, chat)  # the repeat button checks this
            await links._pipe(["SET", "bb:cbchat:%d" % chat, "1", "EX", COOLDOWN])
    return sent


async def on_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cb:off — no more "time for a haircut" invitations to this chat."""
    q = update.callback_query
    links = context.application.bot_data.get("links")
    if links is not None:
        await links._pipe(["SET", "bb:cbout:%d" % update.effective_chat.id, "1"])
    await q.answer(T.COMEBACK_OFF_DONE)
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass


def handlers():
    return [CallbackQueryHandler(on_off, pattern=r"^cb:off$")]
