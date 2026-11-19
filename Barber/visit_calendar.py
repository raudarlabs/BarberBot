"""
"📅 Додати в календар": a visit as a calendar event.

One button (ics:{loc}:{appointment}) under the booking summary and under each visit in «Мій запис».
Pressing it sends a .ics file (iPhone and Android open it with the phone's calendar: "Add to
Calendar") with a 2-hour alert, and on the same message a Google Calendar link for those who use
it on the web. The visit is read from Altegio at that moment, so a moved visit gets its new time.
Only the chat the visit is linked to (bb:appt) can get it.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

import texts as T
from altegio import AltegioError, Appointment

logger = logging.getLogger(__name__)

ALERT_MINUTES = 120
BOT_LINK = "https://t.me/custom_true_bot"


def button(loc: int, appt_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(T.ICS_BUTTON, callback_data="ics:%d:%d" % (loc, appt_id))


def markup(loc: int, appt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button(loc, appt_id)]])


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    """RFC 5545 TEXT escaping."""
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after CRLF + space (UTF-8 safe)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def event_fields(appt: Appointment, staff: str, location: str):
    service = appt.service_titles or "Візит"
    title = "Custom True — %s" % service
    details = "Майстер: %s\nПеренести або скасувати: %s" % (staff or "—", BOT_LINK)
    return title, details, location


def build_ics(appt: Appointment, staff: str, location: str, now: datetime = None) -> bytes:
    title, details, location = event_fields(appt, staff, location)
    now = now or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Custom True//Telegram bot//UK",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        "UID:altegio-%d-%d@custom-true-bot" % (appt.location_id, appt.id),
        "DTSTAMP:" + _utc(now),
        "DTSTART:" + _utc(appt.start),
        "DTEND:" + _utc(appt.end),
        "SUMMARY:" + _esc(title),
        "LOCATION:" + _esc(location),
        "DESCRIPTION:" + _esc(details),
        "BEGIN:VALARM",
        "TRIGGER:-PT%dM" % ALERT_MINUTES,
        "ACTION:DISPLAY",
        "DESCRIPTION:" + _esc(title),
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(_fold(x) for x in lines) + "\r\n").encode("utf-8")


def google_url(appt: Appointment, staff: str, location: str) -> str:
    title, details, location = event_fields(appt, staff, location)
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE",
        "text": title,
        "dates": "%s/%s" % (_utc(appt.start), _utc(appt.end)),
        "details": details,
        "location": location,
    })


def location_text(bot_data: dict, loc: int) -> str:
    """'Custom True Barbershop, Черкаси, вул. …' from the shop facts / the location's address."""
    shop = (bot_data.get("shop") or {}).get("address", "")
    addr = (bot_data.get("locations") or {}).get(loc, "")
    full = shop if addr and addr in shop else (addr or shop)
    return "Custom True Barbershop, " + full if full else "Custom True Barbershop"


async def on_ics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    bd = context.application.bot_data
    client, links = bd.get("altegio"), bd.get("links")
    try:
        _, loc, appt_id = q.data.split(":")
        loc, appt_id = int(loc), int(appt_id)
    except ValueError:
        await q.answer()
        return
    if client is None or links is None:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return
    if await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer(T.NOT_YOURS, show_alert=True)
        return
    try:
        appt = await client.get_appointment(loc, appt_id)
        staff = appt.staff_name or {s.id: s.name for s in await client.list_staff(loc)}.get(appt.staff_id, "")
    except AltegioError as e:
        logger.warning("ics %s/%s: %s", loc, appt_id, e)
        await q.answer(T.API_ERROR, show_alert=True)
        return
    if not appt.active:
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return
    await q.answer()
    where = location_text(bd, loc)
    await update.effective_chat.send_document(
        document=InputFile(io.BytesIO(build_ics(appt, staff, where)), filename="custom-true-%s.ics" % appt.start.strftime("%d-%m")),
        caption=T.ICS_CAPTION.format(when=T.when(appt.start)),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(T.ICS_GOOGLE, url=google_url(appt, staff, where))]]),
    )


def handlers():
    return [CallbackQueryHandler(on_ics, pattern=r"^ics:\d+:\d+$")]
