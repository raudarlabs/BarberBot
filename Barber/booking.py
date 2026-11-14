"""
In-chat booking: location -> barber -> service -> day -> time -> phone -> confirm.

Also the "move my visit" flow started from a reminder button (rm:mv:{loc}:{id}):
it jumps straight to day/time for the same barber and service and updates the
existing appointment instead of creating a new one.

Voice: a voice note at any step (or to start) is transcribed, the barber /
service / day / time it names are filled in, and the flow jumps to the first
step still missing. Booking is still confirmed with a button.

Needs in application.bot_data:
    "altegio"   HttpAltegioClient (or None -> booking is off)
    "links"     linkstore.LinkStore
    "locations" {location_id: "address"} in display order
    "main_menu" ReplyKeyboardMarkup restored when the flow ends
    "voice"     voice.OpenAIVoiceAssistant (or None -> voice notes are declined politely)
"""
from __future__ import annotations

import logging
import time
import warnings
from datetime import date, datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import admin_notify
import texts as T
import voice as V
from altegio import AltegioError, normalize_phone

logger = logging.getLogger(__name__)

LOC, STAFF, SERVICE, DAY, TIME, PHONE, CONFIRM, ANY = range(8)
ANY_OPTIONS = 6  # "any barber": how many earliest options to offer
DAYS_AHEAD = 14
MAX_TIMES = 88  # 22 rows of 4 + nav row stays under Telegram's 100-button limit


# ------------------------------------------------------------------ plumbing

def _deps(context):
    bd = context.application.bot_data
    return bd.get("altegio"), bd.get("links"), bd.get("locations") or {}


STALE_AFTER = 30 * 60  # same as conversation_timeout


def _bk(context) -> dict:
    bk = context.user_data.setdefault("bk", {})
    bk["ts"] = time.time()
    return bk


def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _nav(back=None):
    row = []
    if back:
        row.append(InlineKeyboardButton(T.BACK_BUTTON, callback_data="bk:back:" + back))
    row.append(InlineKeyboardButton(T.CANCEL_BUTTON, callback_data="bk:x"))
    return row


async def _show(update: Update, text: str, markup=None):
    """Edit the flow's message when we came from a button, otherwise send a new one."""
    q = update.callback_query
    if q is not None:
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except Exception:  # message too old / identical: fall back to a new message
            pass
    await update.effective_chat.send_message(text, reply_markup=markup)


async def _api_failed(update, context, e):
    logger.warning("Altegio call failed: %s", e)
    await _show(update, T.API_ERROR)
    return await _finish(update, context, None)


async def _finish(update, context, text):
    context.user_data.pop("bk", None)
    if text:
        menu = context.application.bot_data.get("main_menu")
        await update.effective_chat.send_message(text, reply_markup=menu)
    return ConversationHandler.END


# ------------------------------------------------------------------ steps (render)

def _hint(context) -> str:
    """Voice tip, shown once on the first screen of a booking."""
    if _bk(context).pop("hint", False) and context.application.bot_data.get("voice"):
        return "\n\n" + T.VOICE_HINT
    return ""


async def show_locations(update, context):
    client, _, locations = _deps(context)
    if len(locations) == 1:
        _bk(context)["loc"] = next(iter(locations))
        return await show_staff(update, context, back=None)
    buttons = [[InlineKeyboardButton("📍 " + name, callback_data="bk:loc:%d" % loc)]
               for loc, name in locations.items()]
    await _show(update, T.PICK_LOCATION + _hint(context), InlineKeyboardMarkup(buttons + [_nav()]))
    return LOC


async def show_staff(update, context, back="loc"):
    client, _, locations = _deps(context)
    bk = _bk(context)
    back = back if len(locations) > 1 else None
    try:
        staff = await client.list_staff(bk["loc"])
    except AltegioError as e:
        return await _api_failed(update, context, e)
    bk["staff_names"] = {s.id: s.name for s in staff}
    if not staff:
        await _show(update, T.NO_STAFF, InlineKeyboardMarkup([_nav(back)]))
        return STAFF
    buttons = [InlineKeyboardButton("💈 " + s.name, callback_data="bk:stf:%d" % s.id) for s in staff]
    rows = _rows(buttons, 2)
    if len(staff) > 1:
        rows.insert(0, [InlineKeyboardButton(T.ANY_BUTTON, callback_data="bk:any")])
    await _show(update, T.PICK_STAFF + _hint(context), InlineKeyboardMarkup(rows + [_nav(back)]))
    return STAFF


# ------------------------------------------------------------------ any barber, earliest time

def _norm_title(title: str) -> str:
    return " ".join((title or "").lower().split())


async def on_any(update, context):
    """'⚡ Найближчий час': pick a service by name; the same name groups each barber's own service."""
    client, _, _ = _deps(context)
    bk = _bk(context)
    if "loc" not in bk:
        return await _expired(update, context)
    try:
        names = bk.get("staff_names") or {s.id: s.name for s in await client.list_staff(bk["loc"])}
        bk["staff_names"] = names
        groups = {}
        for sid in names:
            for svc in await client.list_services(bk["loc"], sid):
                g = groups.setdefault(_norm_title(svc.title), {"title": svc.title, "by_staff": {}, "order": len(groups)})
                g["by_staff"].setdefault(sid, svc)
    except AltegioError as e:
        return await _api_failed(update, context, e)
    # The services most barbers offer first; ties keep the shop's own order from Altegio.
    ordered = sorted(groups.values(), key=lambda g: (-len(g["by_staff"]), g["order"]))
    bk["any"] = ordered
    buttons = []
    for i, g in enumerate(ordered):
        prices = [s.price_min for s in g["by_staff"].values() if s.price_min]
        label = g["title"] + (" · від %d ₴" % min(prices) if prices else "")
        buttons.append([InlineKeyboardButton(label, callback_data="bk:asv:%d" % i)])
    await _show(update, T.ANY_PICK_SERVICE, InlineKeyboardMarkup(buttons + [_nav("stf")]))
    return ANY


async def on_any_service(update, context):
    client, _, _ = _deps(context)
    bk = _bk(context)
    q = update.callback_query
    try:
        group = bk["any"][int(q.data.split(":")[2])]
    except (KeyError, IndexError, ValueError):
        return await _expired(update, context)
    await q.answer()
    try:  # searching across every barber takes a few seconds: say so first
        await q.edit_message_text(T.ANY_SEARCHING)
    except Exception:
        pass
    now = datetime.now(client.tz)
    options = []
    try:
        for sid, svc in group["by_staff"].items():
            days = await client.booking_dates(bk["loc"], sid, [svc.id], now.date(), DAYS_AHEAD)
            for day in days[:2]:  # today's slots may all be gone already
                starts = await client.booking_times(bk["loc"], sid, day, [svc.id])
                if starts:
                    options.append((starts[0], sid, svc))
                    break
    except AltegioError as e:
        return await _api_failed(update, context, e)
    options.sort(key=lambda o: o[0])
    nav = _nav("stf")
    if not options:
        await _edit(update, T.ANY_NONE.format(service=group["title"]), InlineKeyboardMarkup([nav]))
        return ANY
    names = bk.get("staff_names") or {}
    rows = []
    for start, sid, svc in options[:ANY_OPTIONS]:
        label = "%s · %s%s" % (T.slot_button(start, now), names.get(sid, ""),
                               (" · " + svc.price_label) if svc.price_label else "")
        rows.append([InlineKeyboardButton(label, callback_data="bk:ago:%d:%d:%s" % (sid, svc.id, start.strftime("%Y%m%d%H%M")))])
    await _edit(update, T.ANY_PICK_TIME.format(service=group["title"]), InlineKeyboardMarkup(rows + [nav]))
    return ANY


async def on_any_go(update, context):
    """An option picked: continue exactly like a manual booking at the phone / summary step."""
    bk = _bk(context)
    try:
        _, _, sid, svc_id, stamp = update.callback_query.data.split(":")
        sid, svc_id = int(sid), int(svc_id)
        svc = next(g["by_staff"][sid] for g in bk.get("any", []) if g["by_staff"].get(sid) and g["by_staff"][sid].id == svc_id)
    except (ValueError, StopIteration, KeyError):
        return await _expired(update, context)
    bk.update(staff_id=sid, staff_name=(bk.get("staff_names") or {}).get(sid, ""), service_id=svc.id,
              duration=svc.duration, services={svc.id: svc},
              day="%s-%s-%s" % (stamp[:4], stamp[4:6], stamp[6:8]), time=stamp[8:12])
    await update.callback_query.answer()
    return await _after_time(update, context)


async def _edit(update, text, markup):
    """Edit the flow message after the callback was already answered (no second answer())."""
    try:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    except Exception:
        await update.effective_chat.send_message(text, reply_markup=markup)


async def show_services(update, context):
    client, _, _ = _deps(context)
    bk = _bk(context)
    try:
        services = await client.list_services(bk["loc"], bk["staff_id"])
    except AltegioError as e:
        return await _api_failed(update, context, e)
    bk["services"] = {s.id: s for s in services}
    if not services:
        await _show(update, T.NO_SERVICES.format(staff=bk["staff_name"]), InlineKeyboardMarkup([_nav("stf")]))
        return SERVICE
    buttons = [[InlineKeyboardButton(
        s.title + (" · " + s.price_label if s.price_label else ""), callback_data="bk:svc:%d" % s.id)]
        for s in services]
    await _show(update, T.PICK_SERVICE.format(staff=bk["staff_name"]),
                InlineKeyboardMarkup(buttons + [_nav("stf")]))
    return SERVICE


async def show_days(update, context):
    client, _, _ = _deps(context)
    bk = _bk(context)
    today = datetime.now(client.tz).date()
    try:
        days = await client.booking_dates(bk["loc"], bk["staff_id"], _service_ids(bk), today, DAYS_AHEAD)
        appt = bk.get("appt") if bk.get("reschedule") else None
        if appt is not None and appt.start.date() >= today and appt.start.date() not in days:
            days = sorted(days + [appt.start.date()])  # moving within the visit's own day is allowed
    except AltegioError as e:
        return await _api_failed(update, context, e)
    back = None if bk.get("reschedule") else "svc"
    if not days:
        await _show(update, T.NO_DAYS, InlineKeyboardMarkup([_nav(None if bk.get("reschedule") else "stf")]))
        return DAY
    buttons = [InlineKeyboardButton(T.day_button(d), callback_data="bk:day:" + d.isoformat()) for d in days]
    await _show(update, T.PICK_DAY, InlineKeyboardMarkup(_rows(buttons, 3) + [_nav(back)]))
    return DAY


async def show_times(update, context, prefix=None, window=None, starts=None):
    """Free start times for bk["day"]; `window` = ("HH:MM" or None, "HH:MM" or None) narrows them."""
    client, _, _ = _deps(context)
    bk = _bk(context)
    day = date.fromisoformat(bk["day"])
    if starts is None:
        try:
            starts = await _free_starts(client, bk, day)
        except AltegioError as e:
            return await _api_failed(update, context, e)
    if not starts:
        text = (prefix + "\n\n" if prefix else "") + T.NO_TIME
        await _show(update, text, InlineKeyboardMarkup([_nav("day")]))
        return TIME
    if window and any(window):
        lo, hi = window[0] or "00:00", window[1] or "23:59"
        narrowed = [s for s in starts if lo <= s.strftime("%H:%M") <= hi]
        if narrowed:
            starts = narrowed
        else:
            prefix = (prefix + "\n" if prefix else "") + T.VOICE_WINDOW_EMPTY
    buttons = [InlineKeyboardButton(s.strftime("%H:%M"), callback_data="bk:t:" + s.strftime("%H%M"))
               for s in starts[:MAX_TIMES]]
    text = (prefix + "\n\n" if prefix else "") + T.PICK_TIME.format(day=T.day_button(day))
    await _show(update, text, InlineKeyboardMarkup(_rows(buttons, 4) + [_nav("day")]))
    return TIME


def _service_ids(bk):
    """The chosen service, or (when moving a visit) the visit's own services."""
    if bk.get("service_id"):
        return [bk["service_id"]]
    appt = bk.get("appt")
    return [int(s["id"]) for s in (getattr(appt, "services", None) or []) if isinstance(s, dict) and s.get("id")]


async def _free_starts(client, bk, day):
    """New booking: start times exactly as Altegio's online booking offers them (busy time, service
    length and the location's online-booking rules included). Moving a visit: the barber's free grid
    with the visit's own slot counted as free, so it can shift by less than its length."""
    appt = bk.get("appt") if bk.get("reschedule") else None
    if appt is not None:
        exclude = [(appt.start, appt.end)] if appt.staff_id == bk["staff_id"] else []
        return await client.free_starts(bk["loc"], bk["staff_id"], day, bk["duration"], exclude=exclude)
    return await client.booking_times(bk["loc"], bk["staff_id"], day, _service_ids(bk))


def _start_dt(context) -> datetime:
    client, _, _ = _deps(context)
    bk = _bk(context)
    d = date.fromisoformat(bk["day"])
    return datetime(d.year, d.month, d.day, int(bk["time"][:2]), int(bk["time"][2:]), tzinfo=client.tz)


async def show_summary(update, context):
    client, _, locations = _deps(context)
    bk = _bk(context)
    start = _start_dt(context)
    # One id per booking attempt, reused on retries so a duplicate can be recognised.
    bk.setdefault("api_id", "tg-%d-%d" % (update.effective_chat.id, int(time.time() * 1000)))
    if bk.get("reschedule"):
        text = T.RESCHEDULE_SUMMARY.format(staff=bk["staff_name"], when=T.when(start))
        rows = [[InlineKeyboardButton(T.CONFIRM_BUTTON, callback_data="bk:ok")], _nav("day")]
    else:
        svc = bk["services"][bk["service_id"]]
        text = T.SUMMARY.format(
            location=locations.get(bk["loc"], ""), staff=bk["staff_name"], service=svc.title,
            price=" · " + svc.price_label if svc.price_label else "", when=T.when(start), phone=bk["phone"],
        )
        rows = [[InlineKeyboardButton(T.CONFIRM_BUTTON, callback_data="bk:ok")],
                [InlineKeyboardButton(T.OTHER_PHONE_BUTTON, callback_data="bk:phone")], _nav("day")]
    if client.is_mock:
        text += T.MOCK_NOTE
    await _show(update, text, InlineKeyboardMarkup(rows))
    return CONFIRM


async def ask_phone(update, context):
    q = update.callback_query
    if q is not None:
        await q.answer()
    kb = ReplyKeyboardMarkup([[KeyboardButton(T.SHARE_PHONE_BUTTON, request_contact=True)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await update.effective_chat.send_message(T.ASK_PHONE, reply_markup=kb)
    return PHONE


# ------------------------------------------------------------------ entry points

async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client, _, locations = _deps(context)
    if update.callback_query is not None:  # "📅 Записатися" under "Мій запис"
        await update.callback_query.answer()
    if client is None or not locations:
        await update.effective_chat.send_message(T.UNAVAILABLE)
        return ConversationHandler.END
    context.user_data["bk"] = {"hint": True}
    return await show_locations(update, context)


async def start_with_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """bk:go:{loc}:{staff} from the prices screen: straight to that barber's services."""
    client, _, locations = _deps(context)
    q = update.callback_query
    try:
        _, _, loc, staff_id = q.data.split(":")
        loc, staff_id = int(loc), int(staff_id)
    except ValueError:
        await q.answer()
        return None
    if client is None or loc not in locations:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return None
    try:
        names = {s.id: s.name for s in await client.list_staff(loc)}
    except AltegioError as e:
        return await _api_failed(update, context, e)
    if staff_id not in names:
        await q.answer(T.SESSION_EXPIRED, show_alert=True)
        return None
    context.user_data["bk"] = {"loc": loc, "staff_id": staff_id, "staff_name": names[staff_id],
                               "staff_names": names}
    return await show_services(update, context)


async def start_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reminder button rm:mv:{loc}:{appointment_id}."""
    client, links, _ = _deps(context)
    q = update.callback_query
    try:
        _, _, loc, appt_id = q.data.split(":")
        loc, appt_id = int(loc), int(appt_id)
    except ValueError:
        await q.answer()
        return None
    if client is None:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return None
    if await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer(T.NOT_YOURS, show_alert=True)
        return None
    try:
        appt = await client.get_appointment(loc, appt_id)
    except AltegioError as e:
        logger.warning("reschedule: cannot load %s/%s: %s", loc, appt_id, e)
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return None  # a booking already in progress (if any) carries on
    if not appt.active or appt.start <= datetime.now(client.tz):
        await q.answer(T.CANT_CHANGE, show_alert=True)
        return None
    context.user_data["bk"] = {
        "loc": loc, "staff_id": appt.staff_id, "duration": appt.duration,
        "staff_name": appt.staff_name or "", "reschedule": appt_id, "appt": appt,
    }
    if not appt.staff_name:
        try:
            names = {s.id: s.name for s in await client.list_staff(loc)}
            context.user_data["bk"]["staff_name"] = names.get(appt.staff_id, "")
        except AltegioError:
            pass
    # Keep the reminder text; open the picker as a new message under it.
    await q.answer()
    await q.edit_message_reply_markup(None)
    return await show_days(_NoCallback(update), context)


class _NoCallback:
    """Update proxy without callback_query, so _show sends a new message."""

    def __init__(self, update):
        self.effective_chat = update.effective_chat
        self.effective_user = update.effective_user
        self.callback_query = None


# ------------------------------------------------------------------ button handlers

async def on_location(update, context):
    bk = _bk(context)
    bk["loc"] = int(update.callback_query.data.split(":")[2])
    return await show_staff(update, context)


async def on_staff(update, context):
    bk = _bk(context)
    if "loc" not in bk:
        return await _expired(update, context)
    staff_id = int(update.callback_query.data.split(":")[2])
    bk["staff_id"] = staff_id
    bk["staff_name"] = bk.get("staff_names", {}).get(staff_id, "")
    return await show_services(update, context)


async def on_service(update, context):
    bk = _bk(context)
    svc = bk.get("services", {}).get(int(update.callback_query.data.split(":")[2]))
    if svc is None:
        return await _expired(update, context)
    bk["service_id"] = svc.id
    bk["duration"] = svc.duration
    return await show_days(update, context)


async def on_day(update, context):
    bk = _bk(context)
    if "staff_id" not in bk:
        return await _expired(update, context)
    bk["day"] = update.callback_query.data.split(":")[2]
    return await show_times(update, context)


async def on_time(update, context):
    _, links, _ = _deps(context)
    bk = _bk(context)
    if "day" not in bk:
        return await _expired(update, context)
    bk["time"] = update.callback_query.data.split(":")[2]
    return await _after_time(update, context)


async def _after_time(update, context):
    _, links, _ = _deps(context)
    bk = _bk(context)
    if bk.get("reschedule"):
        return await show_summary(update, context)
    phone = await links.phone_for_chat(update.effective_chat.id)
    if phone:
        bk["phone"] = phone
        return await show_summary(update, context)
    return await ask_phone(update, context)


async def on_contact(update, context):
    _, links, _ = _deps(context)
    bk = _bk(context)
    contact = update.message.contact
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(T.NOT_YOUR_CONTACT)
        return PHONE
    if "time" not in bk:
        return await _expired(update, context)
    phone = normalize_phone(contact.phone_number)
    bk["phone"] = phone
    bk["name"] = " ".join(x for x in (contact.first_name, contact.last_name) if x)
    await links.link(update.effective_chat.id, phone)
    await update.message.reply_text(T.PHONE_SAVED, reply_markup=ReplyKeyboardRemove())
    return await show_summary(_NoCallback(update), context)


async def on_phone_text(update, context):
    await update.message.reply_text(T.NOT_YOUR_CONTACT)
    return PHONE


async def on_confirm(update, context):
    client, links, locations = _deps(context)
    bk = _bk(context)
    if "time" not in bk or ("phone" not in bk and not bk.get("reschedule")):
        return await _expired(update, context)
    q = update.callback_query
    chat_id = update.effective_chat.id
    start = _start_dt(context)
    try:
        if bk.get("reschedule"):
            appt = await client.reschedule_appointment(bk["appt"], start)
            text = T.RESCHEDULED.format(when=T.when(start))
        else:
            svc = bk["services"][bk["service_id"]]
            user = update.effective_user
            name = bk.get("name") or " ".join(x for x in (user.first_name, user.last_name) if x)
            api_id = bk.setdefault("api_id", "tg-%d-%d" % (chat_id, int(time.time() * 1000)))
            try:
                appt = await client.create_appointment(
                    bk["loc"], bk["staff_id"], [svc], start, bk["phone"], name,
                    comment="Запис через Telegram-бот", api_id=api_id,
                )
            except AltegioError as e:
                # A retry after a timeout, or after the first attempt already went through,
                # lands here as "busy": look for our own appointment before giving up.
                appt = await _find_own(client, bk, start) if e.status in (None, 409, 422) else None
                if appt is None:
                    raise
            text = T.BOOKED.format(staff=bk["staff_name"], service=svc.title, when=T.when(start),
                                   location=locations.get(bk["loc"], ""))
    except AltegioError as e:
        if e.status in (409, 422):  # slot taken between listing and booking
            return await show_times(update, context, prefix=T.SLOT_TAKEN)
        return await _api_failed(update, context, e)

    # The visit exists now: bookkeeping failures must not hide that from the client.
    try:
        await links.remember_appointment(bk["loc"], appt.id, chat_id)
        if appt.client_id and bk.get("phone"):
            await links.remember_client_id(bk["loc"], bk["phone"], appt.client_id)
        # A reminder for a window we are already inside would just repeat this message.
        hours = (start - datetime.now(client.tz)).total_seconds() / 3600
        stamp = int(start.timestamp())
        if hours <= 24:
            await links.mark_sent(appt.id, "24h:%d" % stamp)
        if hours <= 2:
            await links.mark_sent(appt.id, "2h:%d" % stamp)
    except Exception:
        logger.exception("booking %s: saving reminder links failed", appt.id)
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    await admin_notify.count_event("moved" if bk.get("reschedule") else "booked")
    try:
        user = update.effective_user
        who = (bk.get("name") or admin_notify.user_name(user), bk.get("phone") or appt.client_phone,
               getattr(user, "username", None))
        if bk.get("reschedule"):
            note = admin_notify.moved_text(*who, bk.get("staff_name", ""), T.when(bk["appt"].start), T.when(start))
        else:
            note = admin_notify.booked_text(*who, bk.get("staff_name", ""), svc.title, T.when(start),
                                            locations.get(bk["loc"], ""))
        if admin_notify.admin_chat_ids():
            await admin_notify.notify_admins(context, note + (T.MOCK_NOTE if client.is_mock else ""))
    except Exception:
        logger.exception("booking %s: admin notification failed", appt.id)
    if client.is_mock:
        text += T.MOCK_NOTE
    return await _finish(update, context, text)


async def _find_own(client, bk, start):
    """Our appointment at `start` (same barber, our api_id or phone), if Altegio already has it."""
    try:
        appts = await client.list_appointments(bk["loc"], start, start + timedelta(minutes=1))
    except AltegioError:
        return None
    for a in appts:
        if a.active and a.staff_id == bk["staff_id"] and a.start == start and (
                (a.api_id and a.api_id == bk.get("api_id")) or (a.client_phone and a.client_phone == bk.get("phone"))):
            return a
    return None


# ------------------------------------------------------------------ voice

async def on_voice(update, context):
    """A voice note: transcribe, fill in what was said, jump to the first missing step."""
    client, _, locations = _deps(context)
    assistant = context.application.bot_data.get("voice")
    msg = update.message
    in_flow = "bk" in context.user_data
    if client is None or not locations:
        await msg.reply_text(T.UNAVAILABLE)
        return ConversationHandler.END
    if assistant is None:
        await msg.reply_text(T.VOICE_OFF)
        return None  # stay where we are
    media = msg.voice or msg.audio
    if media is None:
        return None
    pending = context.user_data.get("bk") or {}
    if in_flow and "time" in pending and "phone" not in pending and not pending.get("reschedule"):
        await msg.reply_text(T.PHONE_BY_BUTTON)  # the phone step: only the contact button works
        return None
    if (media.duration or 0) > V.MAX_VOICE_SECONDS:
        await msg.reply_text(T.VOICE_TOO_LONG)
        return None
    await update.effective_chat.send_action(ChatAction.TYPING)

    old = context.user_data.get("bk")
    if old is not None and time.time() - old.get("ts", 0) > STALE_AFTER:
        context.user_data.pop("bk")  # left over from a booking that timed out
        in_flow = False
    bk = _bk(context)
    today = datetime.now(client.tz).date()
    try:
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        text = await assistant.transcribe(audio)
        if not text:
            await msg.reply_text(T.VOICE_NOT_UNDERSTOOD)
            return None if in_flow else ConversationHandler.END
        if bk.get("reschedule"):
            catalog = {bk["loc"]: []}
        else:
            catalog = {loc: await client.list_staff(loc) for loc in locations}
        current = {k: bk[k] for k in ("loc", "staff_name", "day", "time") if k in bk}
        intent = await assistant.extract(text, catalog, locations, today, current=current or None)
        intent.said = text
    except AltegioError as e:
        return await _api_failed(update, context, e)
    except Exception:
        logger.exception("voice: transcription/extraction failed")
        await msg.reply_text(T.VOICE_NOT_UNDERSTOOD)
        return None if in_flow else ConversationHandler.END

    await msg.reply_text(T.VOICE_HEARD.format(text=text[:300]))
    if intent.cancel_visit:
        phone = (context.application.bot_data.get("shop") or {}).get("phone_display", "")
        await update.effective_chat.send_message(T.CANCEL_VISIT_INFO.format(phone=phone))
        return None if in_flow else ConversationHandler.END
    if intent.cancel:
        return await _finish(update, context, _left_text(context))
    return await apply_intent(update, context, intent, catalog)


async def _pick_service(context, intent, services):
    """Exact title -> that; else let the assistant pick from this barber's list; else the matcher."""
    svc = V.exact_service(intent.service, services)
    if svc is not None:
        return svc
    assistant = context.application.bot_data.get("voice")
    if assistant is not None and hasattr(assistant, "pick_service") and intent.said:
        try:
            sid = await assistant.pick_service(intent.said, services)
            return next((s for s in services if s.id == sid), None)
        except Exception:
            logger.exception("voice: service pick failed")
    return V.match_service(intent.service, services)


async def apply_intent(update, context, intent, catalog):
    """Merge a voice intent into the booking and render the first step that is still open."""
    client, _, locations = _deps(context)
    bk = _bk(context)
    bk.pop("hint", None)
    if not bk.get("reschedule"):
        if intent.location_id and intent.location_id != bk.get("loc"):
            for k in ("staff_id", "staff_name", "service_id", "duration", "services", "time"):
                bk.pop(k, None)
            bk["loc"] = intent.location_id
        if "loc" not in bk and len(locations) == 1:
            bk["loc"] = next(iter(locations))
        if intent.staff_id and intent.staff_id != bk.get("staff_id"):
            names = {s.id: s.name for s in catalog.get(bk.get("loc"), [])}
            bk.update(staff_id=intent.staff_id, staff_name=names.get(intent.staff_id, ""), staff_names=names)
            for k in ("service_id", "duration", "services", "time"):
                bk.pop(k, None)
        if intent.service and "staff_id" in bk:
            try:
                services = await client.list_services(bk["loc"], bk["staff_id"])
            except AltegioError as e:
                return await _api_failed(update, context, e)
            bk["services"] = {s.id: s for s in services}
            svc = await _pick_service(context, intent, services)
            if svc is not None:
                bk.update(service_id=svc.id, duration=svc.duration)
    if intent.day:
        if bk.get("day") != intent.day.isoformat():
            bk.pop("time", None)
        bk["day"] = intent.day.isoformat()

    if "loc" not in bk:
        return await show_locations(update, context)
    if "staff_id" not in bk:
        return await show_staff(update, context)
    if "duration" not in bk:
        return await show_services(update, context)
    if "day" not in bk:
        return await show_days(update, context)
    if intent.time:
        day = date.fromisoformat(bk["day"])
        try:
            starts = await _free_starts(client, bk, day)
        except AltegioError as e:
            return await _api_failed(update, context, e)
        wanted = intent.time.replace(":", "")
        if any(s.strftime("%H%M") == wanted for s in starts):
            bk["time"] = wanted
            return await _after_time(update, context)
        return await show_times(update, context, prefix=T.VOICE_TIME_BUSY.format(time=intent.time),
                                starts=starts)
    return await show_times(update, context, window=(intent.time_from, intent.time_to))


async def on_other_phone(update, context):
    return await ask_phone(update, context)


async def on_back(update, context):
    target = update.callback_query.data.split(":")[2]
    bk = _bk(context)
    needs = {"loc": (), "stf": ("loc",), "svc": ("loc", "staff_id"), "day": ("loc", "staff_id", "duration")}
    if any(k not in bk for k in needs.get(target, ("_",))):
        return await _expired(update, context)
    if target == "loc":
        return await show_locations(update, context)
    if target == "stf":
        return await show_staff(update, context)
    if target == "svc":
        return await show_services(update, context)
    return await show_days(update, context)


def _left_text(context) -> str:
    """What leaving the flow means: nothing booked, or the visit being moved stays as it was."""
    bk = context.user_data.get("bk") or {}
    appt = bk.get("appt") if bk.get("reschedule") else None
    if appt is not None:
        return T.RESCHEDULE_KEPT.format(when=T.when(appt.start))
    return T.CANCELLED


async def on_cancel(update, context):
    if update.callback_query is not None:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except Exception:
            pass
    return await _finish(update, context, _left_text(context))


async def on_menu_exit(update, context):
    """A main-menu button pressed mid-booking: drop the booking and do what the button says."""
    context.user_data.pop("bk", None)
    dispatch = context.application.bot_data.get("menu_dispatch")
    if dispatch is not None:
        await dispatch(update, context)
    return ConversationHandler.END


async def _expired(update, context):
    if update.callback_query is not None:
        await update.callback_query.answer(T.SESSION_EXPIRED, show_alert=True)
    context.user_data.pop("bk", None)
    return ConversationHandler.END


async def stale_button(update, context):
    """A booking button pressed outside a running conversation (e.g. after a restart)."""
    await update.callback_query.answer(T.SESSION_EXPIRED, show_alert=True)


def build_handler(persistent: bool = False) -> ConversationHandler:
    voice = MessageHandler(filters.VOICE | filters.AUDIO, on_voice)
    nav = [
        CallbackQueryHandler(on_back, pattern=r"^bk:back:"),
        CallbackQueryHandler(on_cancel, pattern=r"^bk:x$"),
        voice,
    ]
    # per_message=False is intended: the flow mixes buttons with contact and voice messages.
    warnings.filterwarnings("ignore", message=".*per_message=False.*")
    return ConversationHandler(
        entry_points=[
            CommandHandler("book", start_booking),
            MessageHandler(filters.Regex("^%s$" % T.BOOK_BUTTON), start_booking),
            CallbackQueryHandler(start_reschedule, pattern=r"^rm:mv:"),
            CallbackQueryHandler(start_booking, pattern=r"^bk:new$"),
            CallbackQueryHandler(start_with_staff, pattern=r"^bk:go:\d+:\d+$"),
            voice,
        ],
        states={
            LOC: [CallbackQueryHandler(on_location, pattern=r"^bk:loc:\d+$")] + nav,
            STAFF: [CallbackQueryHandler(on_staff, pattern=r"^bk:stf:\d+$"),
                    CallbackQueryHandler(on_any, pattern=r"^bk:any$")] + nav,
            ANY: [CallbackQueryHandler(on_any_service, pattern=r"^bk:asv:\d+$"),
                  CallbackQueryHandler(on_any_go, pattern=r"^bk:ago:\d+:\d+:\d{12}$")] + nav,
            SERVICE: [CallbackQueryHandler(on_service, pattern=r"^bk:svc:\d+$")] + nav,
            DAY: [CallbackQueryHandler(on_day, pattern=r"^bk:day:[\d-]+$")] + nav,
            TIME: [CallbackQueryHandler(on_time, pattern=r"^bk:t:\d{4}$")] + nav,
            PHONE: [
                MessageHandler(filters.CONTACT, on_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(T.MENU_REGEX), on_phone_text),
            ] + nav,
            CONFIRM: [
                CallbackQueryHandler(on_confirm, pattern=r"^bk:ok$"),
                CallbackQueryHandler(on_other_phone, pattern=r"^bk:phone$"),
            ] + nav,
        },
        fallbacks=[
            CommandHandler("cancel", on_cancel),
            MessageHandler(filters.Regex(T.MENU_REGEX), on_menu_exit),
        ],
        allow_reentry=True,
        # Polling: a JobQueue ends idle flows. Webhook (persistent, no JobQueue): steps are stored in the
        # database and a stale flow is detected by its age (STALE_AFTER) instead.
        conversation_timeout=None if persistent else 30 * 60,
        name="booking",
        persistent=persistent,
    )
