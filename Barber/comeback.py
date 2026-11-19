"""
"Time for a haircut?": when a client is due again, the bot invites them back, with one button that
books the same barber and the same service — only the day and time are left to pick.

- Runs inside the 10-minute cron (api/cron_reminders.py), once a day between SEND_HOURS in the
  shop's time (a per-day mark), and only with COMEBACK_ENABLED=1.
- Who: chats that wrote to the bot (bb:chats) and shared their phone. Their visits of the last
  HISTORY_DAYS are matched by phone in one Altegio pass per location.
- When: each client's own rhythm — the median gap between their recent visits (personal_interval),
  between MIN_INTERVAL and MAX_INTERVAL days; COMEBACK_DAYS (30) for a single known visit. Invited
  from that day for GRACE_DAYS, so clients gone for long are not all messaged at once when this is
  switched on.
- Not when: an upcoming booking, /stop_reminders, "🔕 Не нагадувати", already invited for this
  visit, or invited in the last COOLDOWN.
- Every pass also stores each linked client's latest visit (bb:last) for "🔁 Як минулого разу".
- The barber left / is hidden: the invitation offers any barber instead of the repeat button.

Buttons: cb:rp:{loc}:{appointment} -> booking.start_repeat (same barber + service -> calendar),
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

logger = logging.getLogger(__name__)

COMEBACK_DAYS = int(os.getenv("COMEBACK_DAYS", "30"))  # interval for a client with one known visit
MIN_INTERVAL, MAX_INTERVAL = 14, 60
GRACE_DAYS = 14         # invite on days [interval, interval + GRACE_DAYS] after the last visit
HISTORY_DAYS = 120      # visit history read per pass
LOOKAHEAD_DAYS = 60     # an upcoming booking in this window means: no invitation
COOLDOWN = 21 * 86400
SEND_HOURS = (12, 20)   # [from, to) in the shop's time
SENT_KIND = "comeback"
HAPPENED = ("arrived", "confirmed")  # a past visit that took place ("confirmed" = nobody marked it)


def enabled() -> bool:
    return os.environ.get("COMEBACK_ENABLED") == "1"


def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def message(appt: Appointment, staff_name: str, staff_active: bool):
    loc = appt.location_id
    service = appt.service_titles if len(appt.services) == 1 else ""
    when = "%d %s" % (appt.start.day, T.MONTHS_GEN[appt.start.month - 1])
    if staff_active and staff_name:
        text = T.COMEBACK.format(staff=staff_name, date=when)
        label = T.COMEBACK_REPEAT_SERVICE.format(name=_first(staff_name), service=service) if service \
            else T.COMEBACK_REPEAT.format(name=_first(staff_name))
        rows = [[InlineKeyboardButton(label, callback_data="cb:rp:%d:%d" % (loc, appt.id))],
                [InlineKeyboardButton(T.COMEBACK_OTHER, callback_data="bk:new")]]
    else:
        text = T.COMEBACK_NO_STAFF.format(date=when)
        rows = [[InlineKeyboardButton(T.VISITS_BOOK, callback_data="bk:new")]]
    rows.append([InlineKeyboardButton(T.COMEBACK_OFF, callback_data="cb:off")])
    return text, InlineKeyboardMarkup(rows)


async def _off(links, chat_id: int) -> bool:
    return bool((await links._pipe(["GET", "bb:cbout:%d" % chat_id]))[0])


async def _cooling(links, chat_id: int) -> bool:
    return bool((await links._pipe(["GET", "bb:cbchat:%d" % chat_id]))[0])


def personal_interval(starts) -> int:
    """Days between a client's visits: the median of the last (up to 4) gaps, clamped to
    [MIN_INTERVAL, MAX_INTERVAL]; COMEBACK_DAYS when there is only one visit."""
    days = sorted({s.date() for s in starts})
    gaps = [(b - a).days for a, b in zip(days, days[1:])][-4:]
    if not gaps:
        return COMEBACK_DAYS
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return int(min(MAX_INTERVAL, max(MIN_INTERVAL, round(median))))


async def linked_phones(links) -> Dict[str, int]:
    """{phone: chat} for every chat in bb:chats that shared its phone — barbers (bb:team_chat) excluded."""
    raw, team = await links._pipe(["HGETALL", "bb:chats"], ["HGETALL", "bb:team_chat"])
    staff_chats = {str(team[i]) for i in range(0, len(team or []), 2)}
    raw = raw or []
    chats = [int(raw[i]) for i in range(0, len(raw), 2)
             if str(raw[i]).lstrip("-").isdigit() and str(raw[i]) not in staff_chats]
    if not chats:
        return {}
    phones = await links._pipe(*[["GET", "bb:chat:%d" % c] for c in chats])
    return {p: c for c, p in zip(chats, phones) if p}


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
    phones = await linked_phones(links)
    if not phones:
        return 0
    sent, done = 0, set()
    for loc in locations:
        try:
            appts = await client.list_appointments(loc, now - timedelta(days=HISTORY_DAYS),
                                                   now + timedelta(days=LOOKAHEAD_DAYS))
        except AltegioError as e:
            logger.warning("comeback: cannot list visits for %s: %s", loc, e)
            continue
        by_phone: Dict[str, list] = {}
        for a in appts:
            if a.client_phone in phones:
                by_phone.setdefault(a.client_phone, []).append(a)
        staff = None
        for phone, items in by_phone.items():
            chat = phones[phone]
            past = sorted((a for a in items if a.start <= now and a.status in HAPPENED), key=lambda a: a.start)
            if not past or chat in done:
                continue
            done.add(chat)
            last = past[-1]
            await links.remember_last(chat, loc, last.id)  # "🔁 Як минулого разу"
            await links.remember_appointment(loc, last.id, chat)  # its buttons work in this chat
            if any(a.start > now and a.active for a in items):
                continue  # already booked
            interval = personal_interval([a.start for a in past])
            since = (now.date() - last.start.date()).days
            if not (interval <= since <= interval + GRACE_DAYS):
                continue
            if await links.was_sent(last.id, SENT_KIND) or await links.opted_out(chat) \
                    or await _off(links, chat) or await _cooling(links, chat):
                continue
            if staff is None:
                try:
                    staff = {s.id: s.name for s in await client.list_staff(loc)}
                except AltegioError:
                    staff = {}
            name = last.staff_name or staff.get(last.staff_id, "")
            text, markup = message(last, name, last.staff_id in staff)
            try:
                await bot.send_message(chat_id=chat, text=text, reply_markup=markup)
                sent += 1
            except (Forbidden, BadRequest) as e:
                logger.info("comeback: chat %s refused: %s", chat, e)
            except TelegramError as e:
                logger.warning("comeback: send to %s failed, will retry: %s", chat, e)
                continue
            await links.mark_sent(last.id, SENT_KIND)
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
