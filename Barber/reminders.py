"""
Visit reminders in Telegram, 24 h and 2 h before the appointment.

Altegio has no webhooks, so a JobQueue job polls every REMINDER_INTERVAL seconds
(default 10 min): it lists the next day's appointments per location, finds the
client's chat, and sends each reminder once ("✅ Буду" / "🔁 Перенести").

A chat is found by, in order:
  1. the appointment itself, if it was booked (or already reminded) in this bot;
  2. the client's phone (from the appointment, or the Altegio client card,
     cached for a week) matched against phones shared with the contact button.
Clients who never shared their number here get no Telegram reminder; Altegio's
own SMS reminders are unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import admin_notify
import texts as T
from altegio import AltegioError, Appointment

logger = logging.getLogger(__name__)

# kind -> (lower bound exclusive, upper bound inclusive) of time left until the visit.
# The 24 h reminder is skipped once it is too close to the 2 h one.
WINDOWS = {
    "24h": (timedelta(hours=3), timedelta(hours=24)),
    "2h": (timedelta(minutes=0), timedelta(hours=2)),
}
LOOKAHEAD = timedelta(hours=24, minutes=15)

_run_lock = asyncio.Lock()


def due_kind(appt: Appointment, now: datetime) -> Optional[str]:
    if not appt.active:
        return None
    left = appt.start - now
    for kind, (lo, hi) in WINDOWS.items():
        if lo < left <= hi:
            return kind
    return None


def reminder_message(appt: Appointment, kind: str, now: datetime, staff_name: str, location: str):
    service = " (%s)" % appt.service_titles if appt.service_titles else ""
    if kind == "24h":
        text = T.REMINDER_24H.format(rel=T.relative_day(appt.start, now), staff=staff_name,
                                     service=service, location=location)
    else:
        text = T.REMINDER_2H.format(time=appt.start.strftime("%H:%M"), staff=staff_name, location=location)
    return text, move_confirm_buttons(appt)


def visit_buttons(loc: int, appt_id: int, waiting: bool = True) -> InlineKeyboardMarkup:
    """"✅ Буду" (only while unconfirmed), "🔁 Перенести" and "✖️ Скасувати" for one visit."""
    row = []
    if waiting:
        row.append(InlineKeyboardButton(T.REMINDER_CONFIRM, callback_data="rm:ok:%d:%d" % (loc, appt_id)))
    row.append(InlineKeyboardButton(T.REMINDER_MOVE, callback_data="rm:mv:%d:%d" % (loc, appt_id)))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton(T.REMINDER_CANCEL, callback_data="rm:cx:%d:%d" % (loc, appt_id))]])


def move_confirm_buttons(appt: Appointment) -> InlineKeyboardMarkup:
    return visit_buttons(appt.location_id, appt.id, waiting=appt.status == "waiting")


async def find_chat(appt: Appointment, client, links) -> Optional[int]:
    chat = await links.chat_for_appointment(appt.location_id, appt.id)
    if chat:
        return chat
    phone = appt.client_phone
    if not phone and appt.client_id:
        cached = await links.cached_client_phone(appt.location_id, appt.client_id)
        if cached is None:
            try:
                phone = await client.get_client_phone(appt.location_id, appt.client_id)
            except AltegioError as e:
                if e.status != 404:
                    raise
                phone = ""
            await links.cache_client_phone(appt.location_id, appt.client_id, phone)
        else:
            phone = "" if cached == "-" else cached
    chat = await links.chat_for_phone(phone) if phone else None
    if chat and appt.client_id:
        await links.remember_client_id(appt.location_id, phone, appt.client_id)
    return chat


SendFn = Callable[..., Awaitable[object]]


async def run_reminders(client, links, locations: Dict[int, str], send: SendFn,
                        now: Optional[datetime] = None) -> int:
    """One polling pass. Returns how many reminders were sent."""
    now = now or datetime.now(client.tz)
    sent = 0
    for loc, address in locations.items():
        try:
            appts = await client.list_appointments(loc, now, now + LOOKAHEAD)
        except AltegioError as e:
            logger.warning("reminders: cannot list appointments for %s: %s", loc, e)
            continue
        staff_names = None
        for appt in appts:
            kind = due_kind(appt, now)
            if not kind:
                continue
            key = "%s:%d" % (kind, int(appt.start.timestamp()))  # a moved visit is reminded again
            if await links.was_sent(appt.id, key):
                continue
            try:
                chat = await find_chat(appt, client, links)
            except AltegioError as e:
                logger.warning("reminders: client lookup failed for %s: %s", appt.id, e)
                continue
            if not chat or await links.opted_out(chat):
                continue
            name = appt.staff_name
            if not name:
                if staff_names is None:
                    try:
                        staff_names = {s.id: s.name for s in await client.list_staff(loc)}
                    except AltegioError:
                        staff_names = {}
                name = staff_names.get(appt.staff_id, "")
            text, markup = reminder_message(appt, kind, now, name, address)
            try:
                await send(chat_id=chat, text=text, reply_markup=markup)
                sent += 1
            except (Forbidden, BadRequest) as e:
                # Permanent for this chat: mark it so a blocked chat is not retried every 10 min.
                logger.info("reminders: chat %s refused the reminder: %s", chat, e)
            except TelegramError as e:
                # Timeouts, flood control, network: leave it unmarked, the next pass retries.
                logger.warning("reminders: send to %s failed, will retry: %s", chat, e)
                continue
            await links.mark_sent(appt.id, key)
            await links.remember_appointment(loc, appt.id, chat)
    return sent


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    client = bd.get("altegio")
    if client is None or _run_lock.locked():
        return
    async with _run_lock:
        try:
            n = await run_reminders(client, bd["links"], bd.get("locations") or {}, context.bot.send_message)
            if n:
                logger.info("reminders: sent %d", n)
        except Exception:
            logger.exception("reminders: pass failed")


async def on_confirm_visit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button rm:ok:{loc}:{appointment_id} -> attendance 2 (client confirmed)."""
    q = update.callback_query
    bd = context.application.bot_data
    client, links = bd.get("altegio"), bd.get("links")
    try:
        _, _, loc, appt_id = q.data.split(":")
        loc, appt_id = int(loc), int(appt_id)
    except ValueError:
        await q.answer()
        return
    if client is None:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return
    if await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer(T.NOT_YOURS, show_alert=True)
        return
    try:
        appt = await client.get_appointment(loc, appt_id)
        if appt.status == "confirmed":
            await q.answer(T.CONFIRMED)
            return
        # Never overwrite arrived / no-show, and never confirm a cancelled or past visit.
        if not appt.active or appt.start <= datetime.now(client.tz):
            await q.answer(T.CANT_CHANGE, show_alert=True)
            try:
                await q.edit_message_reply_markup(None)
            except BadRequest:
                pass
            return
        await client.confirm_appointment(loc, appt_id)
    except AltegioError as e:
        logger.warning("confirm %s/%s failed: %s", loc, appt_id, e)
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return
    await q.answer(T.CONFIRMED)
    move = visit_buttons(loc, appt_id, waiting=False)  # 🔁 Перенести · ✖️ Скасувати
    try:
        await q.edit_message_text((q.message.text or "") + "\n\n" + T.CONFIRMED, reply_markup=move)
    except BadRequest:
        pass


async def unlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop_reminders: forget this chat's phone."""
    links = context.application.bot_data.get("links")
    done = links is not None and await links.unlink(update.effective_chat.id)
    await update.effective_chat.send_message(T.UNLINKED if done else T.NOT_LINKED)


CANCEL_MIN_HOURS = float(os.getenv("CANCEL_MIN_HOURS", "2"))


def _parse(data):
    try:
        _, action, loc, appt_id = data.split(":")
        return action, int(loc), int(appt_id)
    except ValueError:
        return None, None, None


async def on_cancel_visit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """rm:cx = ask "cancel this visit?"; rm:cxn = keep it (buttons back); rm:cxy = delete it in Altegio."""
    q = update.callback_query
    bd = context.application.bot_data
    client, links = bd.get("altegio"), bd.get("links")
    action, loc, appt_id = _parse(q.data)
    if action is None:
        await q.answer()
        return
    if client is None:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return
    if await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer(T.NOT_YOURS, show_alert=True)
        return
    if action == "cx":
        await q.answer()
        ask = InlineKeyboardMarkup([[
            InlineKeyboardButton(T.CANCEL_YES, callback_data="rm:cxy:%d:%d" % (loc, appt_id)),
            InlineKeyboardButton(T.CANCEL_NO, callback_data="rm:cxn:%d:%d" % (loc, appt_id)),
        ]])
        try:
            await q.edit_message_text((q.message.text or "") + "\n\n" + T.CANCEL_ASK, reply_markup=ask)
        except BadRequest:
            pass
        return
    try:
        appt = await client.get_appointment(loc, appt_id)
    except AltegioError as e:
        logger.warning("cancel: cannot load %s/%s: %s", loc, appt_id, e)
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return
    text = (q.message.text or "").replace("\n\n" + T.CANCEL_ASK, "")
    if action == "cxn":
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=move_confirm_buttons(appt) if appt.active else None)
        except BadRequest:
            pass
        return
    now = datetime.now(client.tz)
    if not appt.active or appt.start <= now:
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return
    if appt.start - now < timedelta(hours=CANCEL_MIN_HOURS):
        phone = (bd.get("shop") or {}).get("phone_display", "")
        await q.answer(T.CANCEL_TOO_LATE.format(hours="%g" % CANCEL_MIN_HOURS, phone=phone), show_alert=True)
        return
    try:
        await client.cancel_appointment(loc, appt_id)
    except AltegioError as e:
        logger.warning("cancel %s/%s failed: %s", loc, appt_id, e)
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return
    logger.info("visit %s/%s cancelled by chat %s", loc, appt_id, update.effective_chat.id)
    try:
        user = update.effective_user
        if not admin_notify.admin_chat_ids():
            raise StopIteration  # no admins configured: skip the extra Altegio lookup
        staff = appt.staff_name
        if not staff:
            try:
                staff = {s.id: s.name for s in await client.list_staff(loc)}.get(appt.staff_id, "")
            except AltegioError:
                staff = ""
        phone = appt.client_phone or await links.phone_for_chat(update.effective_chat.id) or ""
        if not phone and appt.client_id:
            try:
                phone = await client.get_client_phone(loc, appt.client_id)
            except AltegioError:
                phone = ""
        await admin_notify.notify_admins(context, admin_notify.cancelled_text(
            appt.client_name or admin_notify.user_name(user), phone,
            getattr(user, "username", None), staff, appt.service_titles, T.when(appt.start)))
    except StopIteration:
        pass
    except Exception:
        logger.exception("cancel %s/%s: admin notification failed", loc, appt_id)
    await q.answer(T.VISIT_CANCELLED)
    try:
        await q.edit_message_text(text + "\n\n" + T.VISIT_CANCELLED, reply_markup=None)
    except BadRequest:
        pass


def handlers():
    return [
        CallbackQueryHandler(on_confirm_visit, pattern=r"^rm:ok:\d+:\d+$"),
        CallbackQueryHandler(on_cancel_visit, pattern=r"^rm:cx[yn]?:\d+:\d+$"),
        CommandHandler("stop_reminders", unlink_command),
    ]
