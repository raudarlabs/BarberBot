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

import calendar
import logging
import os
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
import team
import visit_calendar
import texts as T
import voice as V
import waitlist
from altegio import AltegioError, normalize_phone

logger = logging.getLogger(__name__)

LOC, STAFF, SERVICE, DAY, TIME, PHONE, CONFIRM, ANY = range(8)
ANY_OPTIONS = 6  # "any barber": how many earliest options to offer
DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "60"))  # calendar horizon; Altegio's online-booking rules still apply
ANY_DAYS = 21  # "nearest time, any barber" looks this far: one Altegio request per barber per month
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
            days = await client.booking_dates(bk["loc"], sid, [svc.id], now.date(), ANY_DAYS)
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


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _next_month(m: date, step: int = 1) -> date:
    y, mo = divmod(m.year * 12 + m.month - 1 + step, 12)
    return date(y, mo + 1, 1)


async def _free_days(client, bk, month: date, today: date, last: date):
    """Bookable days of one calendar month, clipped to [today, last]."""
    lo, hi = max(today, month), min(last, _next_month(month) - timedelta(days=1))
    if lo > hi:
        return []
    days = await client.booking_dates(bk["loc"], bk["staff_id"], _service_ids(bk), lo, (hi - lo).days + 1)
    appt = bk.get("appt") if bk.get("reschedule") else None
    if appt is not None and lo <= appt.start.date() <= hi and appt.start.date() not in days:
        days = sorted(days + [appt.start.date()])  # moving within the visit's own day is allowed
    return days


def calendar_markup(month: date, days, today: date, last: date, back) -> InlineKeyboardMarkup:
    """Month grid: free days are numbers (bk:day:YYYY-MM-DD), the rest dots; ◀ ▶ switch months."""
    free = set(days)
    nop = lambda text: InlineKeyboardButton(text, callback_data="bk:nop")  # noqa: E731
    prev_m, next_m = _next_month(month, -1), _next_month(month)
    head = [
        InlineKeyboardButton("◀", callback_data="bk:cal:" + prev_m.strftime("%Y-%m"))
        if month > _month_start(today) else nop(" "),
        nop("%s %d" % (T.MONTHS[month.month - 1], month.year)),
        InlineKeyboardButton("▶", callback_data="bk:cal:" + next_m.strftime("%Y-%m"))
        if next_m <= last else nop(" "),
    ]
    rows = [head, [nop(w) for w in T.WEEKDAYS_SHORT]]
    for week in calendar.monthcalendar(month.year, month.month):
        row = []
        for n in week:
            d = date(month.year, month.month, n) if n else None
            if d is None:
                row.append(nop(" "))
            elif d in free:
                row.append(InlineKeyboardButton(str(n), callback_data="bk:day:" + d.isoformat()))
            else:
                row.append(nop("·"))
        rows.append(row)
    return InlineKeyboardMarkup(rows + [_nav(back)])


async def show_days(update, context, month: date = None, keep: bool = False):
    """The calendar. Opens on this month, or the first month with free days; `keep` reopens the
    month the client was looking at (coming back from the times)."""
    client, _, _ = _deps(context)
    bk = _bk(context)
    today = datetime.now(client.tz).date()
    last = today + timedelta(days=DAYS_AHEAD - 1)
    back = None if bk.get("reschedule") else "svc"
    explicit = month is not None or (keep and bk.get("cal"))
    if month is None:
        month = date.fromisoformat(bk["cal"]) if keep and bk.get("cal") else _month_start(today)
    try:
        days = await _free_days(client, bk, month, today, last)
        while not days and not explicit and _next_month(month) <= last:  # nothing this month: look ahead
            month = _next_month(month)
            days = await _free_days(client, bk, month, today, last)
    except AltegioError as e:
        return await _api_failed(update, context, e)
    if not days and not explicit:
        await _show(update, T.NO_DAYS, InlineKeyboardMarkup([_nav(None if bk.get("reschedule") else "stf")]))
        return DAY
    bk["cal"] = month.isoformat()
    text = T.PICK_DAY if days else T.NO_DAYS_MONTH.format(month=T.MONTHS[month.month - 1].lower())
    await _show(update, text, calendar_markup(month, days, today, last, back))
    return DAY


async def on_cal(update, context):
    """◀ / ▶ in the calendar."""
    bk = _bk(context)
    if "staff_id" not in bk:
        return await _expired(update, context)
    try:
        month = date.fromisoformat(update.callback_query.data.split(":")[2] + "-01")
    except ValueError:
        return await _expired(update, context)
    return await show_days(update, context, month=month)


async def on_nop(update, context):
    """A calendar cell that is not a free day (or a header): nothing to do."""
    await update.callback_query.answer()
    return None


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
    bk["free_seen"] = [s.strftime("%H:%M") for s in starts]
    wait = [] if bk.get("reschedule") else [[InlineKeyboardButton(T.WAITLIST_BUTTON, callback_data="bk:wl")]]
    if not starts:
        text = (prefix + "\n\n" if prefix else "") + T.NO_TIME + ("\n" + T.WAITLIST_HINT if wait else "")
        await _show(update, text, InlineKeyboardMarkup(wait + [_nav("day")]))
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
    await _show(update, text, InlineKeyboardMarkup(_rows(buttons, 4) + wait + [_nav("day")]))
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
        if bk.get("style"):
            text += "\n" + T.STYLE_SUMMARY.format(name=bk["style"]["name"])
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


def style_button(index: int) -> InlineKeyboardMarkup:
    """Under each AI haircut photo: book exactly this one."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(T.STYLE_BOOK, callback_data="bk:st:%d" % index)]])


async def start_with_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """bk:st:{i} under an AI haircut photo: a normal booking that carries that haircut along."""
    client, _, locations = _deps(context)
    q = update.callback_query
    styles = context.user_data.get("styles") or []
    try:
        style = styles[int(q.data.split(":")[2])]
    except (IndexError, ValueError):
        await q.answer(T.SESSION_EXPIRED, show_alert=True)
        return None
    if client is None or not locations:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return None
    await q.answer()
    context.user_data["bk"] = {"style": {"name": style.get("name", ""), "file_id": style.get("file_id")}}
    await update.effective_chat.send_message(T.STYLE_PICKED.format(name=style.get("name", "")))
    return await show_locations(_NoCallback(update), context)


async def start_repeat_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🔁 Як минулого разу" (menu) or bk:last: the client's latest visit -> same barber + service."""
    client, links, locations = _deps(context)
    if update.callback_query is not None:
        await update.callback_query.answer()
    chat = update.effective_chat
    if client is None or not locations:
        await chat.send_message(T.UNAVAILABLE)
        return ConversationHandler.END
    last = await links.last_for(chat.id)
    if last is None or last[0] not in locations:
        phone = await links.phone_for_chat(chat.id)
        if not phone:
            context.user_data["after_contact"] = "repeat"
            kb = ReplyKeyboardMarkup([[KeyboardButton(T.SHARE_PHONE_BUTTON, request_contact=True)]],
                                     resize_keyboard=True, one_time_keyboard=True)
            await chat.send_message(T.REPEAT_ASK_PHONE, reply_markup=kb)
            return ConversationHandler.END
        await chat.send_action(ChatAction.TYPING)  # a few seconds of Altegio history
        try:
            last = await find_last_visit(client, links, locations, chat.id, phone)
        except AltegioError as e:
            return await _api_failed(update, context, e)
        if last is None:
            await chat.send_message(T.REPEAT_NONE, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(T.VISITS_BOOK, callback_data="bk:new")]]))
            return ConversationHandler.END
    try:
        past = await client.get_appointment(*last)
    except AltegioError as e:
        if e.status == 404:  # deleted in Altegio: look again next time
            await links._pipe(["GETDEL", "bb:last:%d" % chat.id])
        return await _api_failed(update, context, e)
    return await _repeat(update, context, last[0], past)


REPEAT_HISTORY_DAYS = 90


async def find_last_visit(client, links, locations, chat_id: int, phone: str):
    """The client's latest visit (a past one that happened, else an upcoming booking) by phone or the
    Altegio client id; remembered for next time. (location_id, appointment_id) or None."""
    now = datetime.now(client.tz)
    best = None
    for loc in locations:
        cid = await links.client_id_for(loc, phone)
        appts = await client.list_appointments(loc, now - timedelta(days=REPEAT_HISTORY_DAYS), now + timedelta(days=60))
        for a in appts:
            mine = (a.client_phone and a.client_phone == phone) or (cid and a.client_id == cid)
            happened = a.start <= now and a.status in ("arrived", "confirmed")
            upcoming = a.start > now and a.active
            if not mine or not (happened or upcoming):
                continue
            rank = (happened, a.start if happened else -a.start.timestamp())  # newest past, else soonest upcoming
            if best is None or rank > best[0]:
                best = (rank, loc, a)
    if best is None:
        return None
    _, loc, a = best
    await links.remember_last(chat_id, loc, a.id)
    await links.remember_appointment(loc, a.id, chat_id)
    return loc, a.id


async def start_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cb:rp:{loc}:{past appointment} from the "time for a haircut" invitation (comeback.py):
    same barber and same service as that visit, straight to the day picker."""
    client, links, locations = _deps(context)
    q = update.callback_query
    try:
        _, _, loc, appt_id = q.data.split(":")
        loc, appt_id = int(loc), int(appt_id)
    except ValueError:
        await q.answer()
        return None
    if client is None or loc not in locations:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return None
    if await links.chat_for_appointment(loc, appt_id) != update.effective_chat.id:
        await q.answer(T.NOT_YOURS, show_alert=True)
        return None
    try:
        past = await client.get_appointment(loc, appt_id)
    except AltegioError as e:
        return await _api_failed(update, context, e)
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)  # keep the invitation text, drop its buttons
    except Exception:
        pass
    return await _repeat(update, context, loc, past)


async def _repeat(update, context, loc: int, past):
    """Book again like `past`: same barber and service, straight to the calendar. Barber gone ->
    the barber list; service no longer offered -> that barber's services."""
    client, _, _ = _deps(context)
    try:
        names = {s.id: s.name for s in await client.list_staff(loc)}
    except AltegioError as e:
        return await _api_failed(update, context, e)
    here = _NoCallback(update)
    if past.staff_id not in names:  # the barber left or is hidden: pick another one
        context.user_data["bk"] = {"loc": loc}
        staff = past.staff_name or "—"
        await update.effective_chat.send_message(T.COMEBACK_STAFF_GONE.format(staff=staff))
        return await show_staff(here, context, back=None)
    bk = context.user_data["bk"] = {"loc": loc, "staff_id": past.staff_id, "staff_name": names[past.staff_id],
                                    "staff_names": names}
    try:
        services = await client.list_services(loc, past.staff_id)
    except AltegioError as e:
        return await _api_failed(update, context, e)
    bk["services"] = {s.id: s for s in services}
    svc = next((s for s in services if s.id in past.service_ids), None)
    if svc is None:  # that service is no longer offered: choose one
        return await show_services(here, context)
    bk.update(service_id=svc.id, duration=svc.duration)
    await update.effective_chat.send_message(T.REPEAT_PICKED.format(staff=names[past.staff_id], service=svc.title))
    return await show_days(here, context)


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


async def on_waitlist(update, context):
    """bk:wl — no time suits: remember the wish and end the flow."""
    client, links, _ = _deps(context)
    bk = _bk(context)
    if "day" not in bk or "staff_id" not in bk or bk.get("reschedule"):
        return await _expired(update, context)
    await update.callback_query.answer()
    res = await waitlist.add(links, update.effective_chat.id, bk, bk.get("free_seen") or [], datetime.now(client.tz))
    day = T.day_button(date.fromisoformat(bk["day"]))
    if res == "full":
        text = T.WAITLIST_FULL
    else:
        text = T.WAITLIST_ADDED.format(staff=bk.get("staff_name", ""), day=day)
    await _show(update, text)
    return await _finish(update, context, None)


async def start_on_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """bk:wd:{loc}:{staff}:{service}:{YYYYMMDD} from a waitlist message: straight to that day's times."""
    client, _, locations = _deps(context)
    q = update.callback_query
    try:
        _, _, loc, staff_id, svc_id, ymd = q.data.split(":")
        loc, staff_id, svc_id = int(loc), int(staff_id), int(svc_id)
        day = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
    except ValueError:
        await q.answer()
        return None
    if client is None or loc not in locations:
        await q.answer(T.UNAVAILABLE, show_alert=True)
        return None
    if day < datetime.now(client.tz).date():
        await q.answer(T.SESSION_EXPIRED, show_alert=True)
        return None
    try:
        names = {s.id: s.name for s in await client.list_staff(loc)}
        services = await client.list_services(loc, staff_id) if staff_id in names else []
    except AltegioError as e:
        return await _api_failed(update, context, e)
    svc = next((s for s in services if s.id == svc_id), None)
    if svc is None:
        await q.answer(T.SESSION_EXPIRED, show_alert=True)
        return None
    await q.answer()
    context.user_data["bk"] = {"loc": loc, "staff_id": staff_id, "staff_name": names[staff_id], "staff_names": names,
                               "services": {s.id: s for s in services}, "service_id": svc.id,
                               "duration": svc.duration, "day": day.isoformat()}
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
                    comment=_comment(bk), api_id=api_id,
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
        if not bk.get("reschedule"):
            await links.remember_last(chat_id, bk["loc"], appt.id)  # "🔁 Як минулого разу"
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
    try:  # the summary stays above the "done" message, now with "📅 Додати в календар"
        await q.edit_message_reply_markup(visit_calendar.markup(bk["loc"], appt.id))
    except Exception:
        pass
    await admin_notify.count_event("moved" if bk.get("reschedule") else "booked")
    try:  # the barber, if he is linked to the bot (team.py)
        if bk.get("reschedule"):
            old = bk["appt"]
            await team.notify_staff(getattr(context, "bot", None), bk["staff_id"], "moved", appt.id, start,
                                    old.service_titles, old.client_name, old.client_phone, old_start=old.start)
        else:
            await team.notify_staff(getattr(context, "bot", None), bk["staff_id"], "booked", appt.id, start,
                                    svc.title, bk.get("name") or admin_notify.user_name(update.effective_user),
                                    bk.get("phone", ""))
    except Exception:
        logger.exception("booking %s: barber notification failed", appt.id)
    try:
        user = update.effective_user
        who = (bk.get("name") or admin_notify.user_name(user), bk.get("phone") or appt.client_phone,
               getattr(user, "username", None))
        if bk.get("reschedule"):
            note = admin_notify.moved_text(*who, bk.get("staff_name", ""), T.when(bk["appt"].start), T.when(start))
        else:
            note = admin_notify.booked_text(*who, bk.get("staff_name", ""), svc.title, T.when(start),
                                            locations.get(bk["loc"], ""))
        if admin_notify.notify_chat_ids():
            await admin_notify.notify_admins(context, note + (T.MOCK_NOTE if client.is_mock else ""))
    except Exception:
        logger.exception("booking %s: admin notification failed", appt.id)
    if bk.get("style") and not bk.get("reschedule"):
        try:
            await _send_style(context, bk, start, bk.get("name") or admin_notify.user_name(update.effective_user))
        except Exception:
            logger.exception("booking %s: haircut photo for the barber failed", appt.id)
    if client.is_mock:
        text += T.MOCK_NOTE
    return await _finish(update, context, text)


def _comment(bk) -> str:
    comment = "Запис через Telegram-бот"
    if bk.get("style"):
        comment += "; стрижка з ШІ-підбору: «%s»" % bk["style"]["name"]
    return comment


async def _send_style(context, bk, start, client_name) -> int:
    """The haircut the client picked from the AI photos, to the barber (if linked) and the shop chat."""
    bot, style = getattr(context, "bot", None), bk["style"]
    if bot is None or not style.get("file_id"):
        return 0
    try:
        member = (await team.members()).get(int(bk["staff_id"])) or {}
    except Exception:  # team store down: the shop chat still gets the photo
        logger.exception("haircut photo: team lookup failed")
        member = {}
    chats = list(dict.fromkeys([c for c in [member.get("chat")] if c] + admin_notify.notify_chat_ids()))
    caption = T.STYLE_FOR_BARBER.format(name=style["name"], client=client_name, staff=bk.get("staff_name", ""),
                                        when=T.when(start))
    sent = 0
    for chat in chats:
        try:
            await bot.send_photo(chat_id=int(chat), photo=style["file_id"], caption=caption[:1024])
            sent += 1
        except Exception as e:
            logger.warning("haircut photo to %s failed: %s", chat, e)
    return sent


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
    return await show_days(update, context, keep=True)  # back from the times: same month


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
            CallbackQueryHandler(start_repeat, pattern=r"^cb:rp:\d+:\d+$"),
            CallbackQueryHandler(start_with_style, pattern=r"^bk:st:\d+$"),
            CallbackQueryHandler(start_repeat_last, pattern=r"^bk:last$"),
            MessageHandler(filters.Regex("^%s$" % T.MENU_REPEAT), start_repeat_last),
            CallbackQueryHandler(start_on_day, pattern=r"^bk:wd:\d+:\d+:\d+:\d{8}$"),
            voice,
        ],
        states={
            LOC: [CallbackQueryHandler(on_location, pattern=r"^bk:loc:\d+$")] + nav,
            STAFF: [CallbackQueryHandler(on_staff, pattern=r"^bk:stf:\d+$"),
                    CallbackQueryHandler(on_any, pattern=r"^bk:any$")] + nav,
            ANY: [CallbackQueryHandler(on_any_service, pattern=r"^bk:asv:\d+$"),
                  CallbackQueryHandler(on_any_go, pattern=r"^bk:ago:\d+:\d+:\d{12}$")] + nav,
            SERVICE: [CallbackQueryHandler(on_service, pattern=r"^bk:svc:\d+$")] + nav,
            DAY: [CallbackQueryHandler(on_day, pattern=r"^bk:day:[\d-]+$"),
                  CallbackQueryHandler(on_cal, pattern=r"^bk:cal:\d{4}-\d{2}$"),
                  CallbackQueryHandler(on_nop, pattern=r"^bk:nop$")] + nav,
            TIME: [CallbackQueryHandler(on_time, pattern=r"^bk:t:\d{4}$"),
                   CallbackQueryHandler(on_waitlist, pattern=r"^bk:wl$")] + nav,
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
