"""
After a visit: the bot asks the client how it went.

- The 10-minute cron (api/cron_reminders.py) finds visits marked "client arrived" in Altegio that
  ended at least REVIEW_DELAY ago, and asks the client (if their Telegram is linked to the phone and
  they did not /stop_reminders) to rate the visit 1–5. Only between 10:00 and 21:00 — a late visit
  is asked about the next morning. Once per visit.
- Every rating gets the same thanks and the same "leave a Google review" button: Google does not
  allow asking only happy clients for public reviews, so the button is offered to everyone.
- A rating of 4 or lower also asks what to improve; the client's next text message (within
  FEEDBACK_WINDOW) is forwarded to the admins together with the rating.
- Ratings are kept per visit in bb:ratings (for the dashboard: average per period and per barber).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

import admin_notify
from altegio import AltegioError
from reminders import find_chat

logger = logging.getLogger(__name__)

REVIEW_DELAY = timedelta(hours=2)
LOOKBACK = timedelta(hours=20)
SEND_HOURS = (10, 21)            # [from, to) in the shop's time
FEEDBACK_WINDOW = 3600           # seconds to write what to improve
RATINGS = "bb:ratings"           # hash: appointment id -> {"s", "n", "r", "d", "c"}
SENT_KIND = "review"


def review_url() -> str:
    """Where the Google button leads: GOOGLE_REVIEW_URL (a direct 'write a review' link), else the
    shop on Google Maps, where 'Write a review' is one tap away."""
    from config import SHOP
    return os.getenv("GOOGLE_REVIEW_URL") or SHOP.get("maps") or ""


def ask_markup(loc: int, appt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("%d ⭐" % n,
                                                       callback_data="rv:%d:%d:%d" % (loc, appt_id, n))
                                  for n in range(1, 6)]])


def ask_text(staff: str, start: datetime, now: datetime) -> str:
    when = "сьогодні" if start.date() == now.date() else "вчора" if (now.date() - start.date()).days == 1 \
        else start.strftime("%d.%m")
    who = " до %s" % staff if staff else ""
    return ("Дякуємо, що завітали до Custom True! 💈\n\n"
            "Як вам візит%s %s? Оцініть, будь ласка, від 1 до 5:" % (who, when))


async def run(client, links, locations: Dict[int, str], bot, now: Optional[datetime] = None) -> int:
    """One pass: ask about finished visits. Returns how many were asked."""
    now = now or datetime.now(client.tz)
    if not (SEND_HOURS[0] <= now.hour < SEND_HOURS[1]):
        return 0
    sent = 0
    for loc in locations:
        try:
            appts = await client.list_appointments(loc, now - LOOKBACK - REVIEW_DELAY, now)
        except AltegioError as e:
            logger.warning("reviews: cannot list appointments for %s: %s", loc, e)
            continue
        names = None
        for a in appts:
            if a.status != "arrived" or a.end + REVIEW_DELAY > now or a.end < now - LOOKBACK - REVIEW_DELAY:
                continue
            if await links.was_sent(a.id, SENT_KIND):
                continue
            try:
                chat = await find_chat(a, client, links)
            except AltegioError as e:
                logger.warning("reviews: client lookup failed for %s: %s", a.id, e)
                continue
            if not chat or await links.opted_out(chat):
                continue
            staff = a.staff_name
            if not staff:
                if names is None:
                    try:
                        names = {s.id: s.name for s in await client.list_staff(loc)}
                    except AltegioError:
                        names = {}
                staff = names.get(a.staff_id, "")
            try:
                await bot.send_message(chat_id=chat, text=ask_text(staff, a.start, now), reply_markup=ask_markup(loc, a.id))
                sent += 1
            except (Forbidden, BadRequest) as e:
                logger.info("reviews: chat %s refused: %s", chat, e)
            except TelegramError as e:
                logger.warning("reviews: send to %s failed, will retry: %s", chat, e)
                continue
            await links.mark_sent(a.id, SENT_KIND)
            await links.remember_appointment(loc, a.id, chat)
    return sent


async def _store(links, appt_id: int, rec: Optional[dict] = None):
    if rec is None:
        v = (await links._pipe(["HMGET", RATINGS, str(appt_id)]))[0][0]
        return json.loads(v) if v else None
    await links._pipe(["HSET", RATINGS, str(appt_id), json.dumps(rec, ensure_ascii=False)])
    return rec


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """rv:{loc}:{appointment}:{stars}"""
    q = update.callback_query
    bd = context.application.bot_data
    links, client = bd.get("links"), bd.get("altegio")
    try:
        _, loc, appt_id, stars = q.data.split(":")
        loc, appt_id, stars = int(loc), int(appt_id), int(stars)
    except ValueError:
        await q.answer()
        return
    if links is None or not 1 <= stars <= 5 or await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer()
        return
    if await _store(links, appt_id):
        await q.answer("Оцінку вже збережено. Дякуємо!")
        return
    staff_id, staff, day = 0, "", ""
    if client is not None:
        try:
            a = await client.get_appointment(loc, appt_id)
            staff_id, staff, day = a.staff_id, a.staff_name, a.start.date().isoformat()
            if not staff:
                staff = {s.id: s.name for s in await client.list_staff(loc)}.get(a.staff_id, "")
        except AltegioError as e:
            logger.warning("reviews: appointment %s: %s", appt_id, e)
    await _store(links, appt_id, {"s": staff_id, "n": staff, "r": stars, "d": day or datetime.now().date().isoformat(),
                                  "c": update.effective_chat.id, "at": int(time.time())})
    await q.answer("Дякуємо!")
    url = review_url()
    google = [[InlineKeyboardButton("✍️ Відгук у Google", url=url)]] if url else []
    if stars >= 5:
        text = "Дякуємо за %s! 🙏 Якщо маєте хвилинку — відгук у Google дуже допомагає іншим знайти нас." % ("⭐" * stars)
    else:
        context.user_data["rv_wait"] = {"appt": appt_id, "loc": loc, "r": stars, "n": staff, "until": time.time() + FEEDBACK_WINDOW}
        text = ("Дякуємо за чесну оцінку (%d ⭐). Що нам зробити краще? Напишіть одним повідомленням — "
                "я передам власнику.\n\nА якщо хочете — можна залишити і відгук у Google." % stars)
    try:
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(google) if google else None)
    except BadRequest:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(google) if google else None)
    await admin_notify.count_event("rated")
    if stars <= 3:  # a low rating: tell the admins right away; the text, if any, follows
        await admin_notify.notify_admins(context, "⚠️ Оцінка %d/5 · %s · %s\n👤 %s\nЧекаю коментар клієнта." % (
            stars, staff or "майстер ?", day, admin_notify.user_name(update.effective_user)))


async def take_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """A text message right after a low rating is the client's comment. True when it was."""
    w = context.user_data.get("rv_wait") if context.user_data is not None else None
    if not w or not update.message or not update.message.text:
        return False
    context.user_data.pop("rv_wait", None)
    if time.time() > w.get("until", 0):
        return False
    links = context.application.bot_data.get("links")
    text = update.message.text.strip()[:1500]
    if links is not None:
        rec = await _store(links, w["appt"]) or {}
        rec["t"] = text
        await _store(links, w["appt"], rec)
    phone = await links.phone_for_chat(update.effective_chat.id) if links is not None else None
    await admin_notify.notify_admins(context, "💬 Відгук клієнта · %d/5 · %s\n👤 %s%s\n\n«%s»" % (
        w["r"], w.get("n") or "майстер ?", admin_notify.user_name(update.effective_user),
        (" +" + str(phone).lstrip("+")) if phone else "", text))
    await update.message.reply_text("Дякуємо! Передав власнику — обовʼязково розберемося. 🙏")
    return True


def ratings(links_pipe, first: str, last: str) -> list:
    """[{"s", "n", "r", "d"}] for visits from `first` to `last` (YYYY-MM-DD), for the dashboard."""
    raw = links_pipe(["HGETALL", RATINGS])[0] or []
    out = []
    for i in range(1, len(raw), 2):
        try:
            r = json.loads(raw[i])
        except (TypeError, ValueError):
            continue
        if first <= r.get("d", "") <= last:
            out.append(r)
    return out


def handlers():
    return [CallbackQueryHandler(on_rate, pattern=r"^rv:\d+:\d+:[1-5]$")]
