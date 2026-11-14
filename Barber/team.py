"""
The team (barbers) in the bot.

- Admin: /admin → 👥 Команда → per barber "➕ Запросити" gives a one-time invite link
  t.me/<bot>?start=team_<token> (7 days) to forward to the barber; "🗑 Відключити" unlinks.
  (A bot cannot look people up by phone number, so linking goes through the barber's own tap.)
- Barber: opens the link → linked to his Altegio team member; /me shows his schedule and free
  windows for today / tomorrow.
- Notifications to the barber about his clients: new booking, moved visit, cancellation —
  right away for actions made through the bot, and from any source (admin, phone, widget)
  by comparing Altegio's next 48 hours every 10 minutes (sync_changes, run by the cron).
  Each event is sent once (a mark per appointment/event/time in the store).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, filters

import admin
import admin_notify
import texts as T
from altegio import AltegioError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

logger = logging.getLogger(__name__)

TEAM = "bb:team"            # hash: staff_id -> {"chat", "name", "at"}
TEAM_CHAT = "bb:team_chat"  # hash: chat_id -> staff_id
INVITE = "bb:invite:"       # + token -> staff_id, 7 days
SNAP = "bb:snap:"           # + location -> {appointment_id: [staff_id, start, client, phone, service]}
MARK = "bb:tn:"             # + appointment:event:start -> "1" (notified once)
INVITE_TTL = 7 * 86400
MARK_TTL = 4 * 86400
WINDOW = timedelta(hours=48)


async def _kvs(*commands):
    return await asyncio.to_thread(_kv.pipeline, *commands)


# ------------------------------------------------------------------ store

async def members() -> Dict[int, dict]:
    raw = (await _kvs(["HGETALL", TEAM]))[0] or []
    return {int(raw[i]): json.loads(raw[i + 1]) for i in range(0, len(raw), 2)}


async def staff_for_chat(chat_id: int) -> Optional[int]:
    v = (await _kvs(["HMGET", TEAM_CHAT, str(chat_id)]))[0][0]
    return int(v) if v else None


async def make_invite(staff_id: int) -> str:
    token = secrets.token_urlsafe(9)
    await _kvs(["SET", INVITE + token, str(staff_id), "EX", INVITE_TTL])
    return token


async def accept_invite(token: str, chat_id: int, name: str) -> Optional[int]:
    """One-time: the token is consumed. Re-linking a barber moves him to the new chat."""
    sid = (await _kvs(["GETDEL", INVITE + token]))[0]
    if not sid:
        return None
    sid = int(sid)
    old = (await members()).get(sid)
    cmds = [["HSET", TEAM, str(sid), json.dumps({"chat": chat_id, "name": name, "at": int(time.time())},
                                                 ensure_ascii=False)],
            ["HSET", TEAM_CHAT, str(chat_id), str(sid)]]
    if old and int(old["chat"]) != chat_id:
        cmds.append(["HDEL", TEAM_CHAT, str(old["chat"])])
    await _kvs(*cmds)
    return sid


async def unlink(staff_id: int) -> Optional[int]:
    m = (await members()).get(staff_id)
    if not m:
        return None
    await _kvs(["HDEL", TEAM, str(staff_id)], ["HDEL", TEAM_CHAT, str(m["chat"])])
    return int(m["chat"])


# ------------------------------------------------------------------ notifications

def _event_text(event: str, start: datetime, service: str, client: str, phone: str,
                old_start: Optional[datetime] = None) -> str:
    who = (client or "клієнт") + (" +" + phone.lstrip("+") if phone else "")
    if event == "booked":
        head, when = "🆕 Новий запис до вас", T.when(start)
    elif event == "moved":
        head, when = "🔁 Запис перенесено", "було %s → стало %s" % (T.when(old_start), T.when(start)) if old_start else T.when(start)
    else:
        head, when = "❌ Запис скасовано", T.when(start)
    return "%s\n🕒 %s%s\n👤 %s" % (head, when, ("\n✂️ " + service) if service else "", who)


async def notify_staff(bot, staff_id: int, event: str, appt_id: int, start: datetime, service: str = "",
                       client: str = "", phone: str = "", old_start: Optional[datetime] = None) -> bool:
    """Tell the barber about one of his visits, once per (visit, event, time)."""
    try:
        m = (await members()).get(int(staff_id))
        if not m or bot is None:
            return False
        mark = "%s%d:%s:%d" % (MARK, appt_id, event, int(start.timestamp()))
        if (await _kvs(["SET", mark, "1", "EX", MARK_TTL, "NX"]))[0] is None:
            return False  # already told (e.g. the bot did it before the 10-minute sync)
        await bot.send_message(chat_id=int(m["chat"]),
                               text=_event_text(event, start, service, client, phone, old_start))
        return True
    except (TelegramError, _kv.RedisError) as e:
        logger.warning("team notify %s/%s failed: %s", staff_id, appt_id, e)
        return False


def _snap_entry(a) -> list:
    return [a.staff_id, a.start.isoformat(), a.client_name or "", a.client_phone or "", a.service_titles or ""]


async def sync_changes(client, locations: Dict[int, str], bot, now: Optional[datetime] = None) -> int:
    """Compare Altegio's next 48 hours with the previous run and notify barbers. Returns notifications sent.
    The first run only takes a snapshot (nothing to compare with)."""
    team = await members()
    if not team:
        return 0
    now = now or datetime.now(client.tz)
    sent = 0
    for loc in locations:
        appts = await client.list_appointments(loc, now, now + WINDOW)
        cur = {a.id: a for a in appts if a.active and a.start > now}
        raw = (await _kvs(["GET", SNAP + str(loc)]))[0]
        await _kvs(["SET", SNAP + str(loc), json.dumps({str(i): _snap_entry(a) for i, a in cur.items()},
                                                      ensure_ascii=False)])
        if raw is None:
            continue
        old = {int(k): v for k, v in json.loads(raw).items()}
        for aid, a in cur.items():
            prev = old.get(aid)
            if prev is None:
                sent += await notify_staff(bot, a.staff_id, "booked", aid, a.start, a.service_titles,
                                           a.client_name, a.client_phone)
            elif prev[1] != a.start.isoformat() or int(prev[0]) != a.staff_id:
                old_start = datetime.fromisoformat(prev[1])
                if int(prev[0]) != a.staff_id:  # moved to another barber
                    sent += await notify_staff(bot, int(prev[0]), "cancelled", aid, old_start, prev[4], prev[2], prev[3])
                    sent += await notify_staff(bot, a.staff_id, "booked", aid, a.start, a.service_titles,
                                               a.client_name, a.client_phone)
                else:
                    sent += await notify_staff(bot, a.staff_id, "moved", aid, a.start, a.service_titles,
                                               a.client_name, a.client_phone, old_start=old_start)
        for aid, prev in old.items():
            if aid in cur:
                continue
            old_start = datetime.fromisoformat(prev[1])
            if old_start <= now:
                continue  # simply in the past now
            try:
                a = await client.get_appointment(loc, aid)
            except AltegioError:
                a = None  # deleted
            if a is not None and a.active and a.start > now + WINDOW:
                sent += await notify_staff(bot, a.staff_id, "moved", aid, a.start, a.service_titles,
                                           a.client_name, a.client_phone, old_start=old_start)
            elif a is None or not a.active:
                sent += await notify_staff(bot, int(prev[0]), "cancelled", aid, old_start, prev[4], prev[2], prev[3])
    return sent


# ------------------------------------------------------------------ admin: 👥 Команда

async def team_screen(client, locations) -> tuple:
    team = await members()
    lines, rows = ["👥 Команда — хто бачить свій графік і отримує сповіщення:", ""], []
    for loc in locations:
        for s in await client.list_staff(loc):
            if s.id in team:
                lines.append("✅ %s" % s.name)
                rows.append([InlineKeyboardButton("🗑 Відключити: " + s.name, callback_data="adm:tdel:%d" % s.id)])
            else:
                lines.append("➕ %s — не підключено" % s.name)
                rows.append([InlineKeyboardButton("➕ Запросити: " + s.name, callback_data="adm:tinv:%d" % s.id)])
    lines += ["", "Натисніть «Запросити» — я дам посилання, перешліть його майстру."]
    rows.append([InlineKeyboardButton("◀️ Адмін-панель", callback_data="adm:panel")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_team_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not admin.is_admin(update):
        await q.answer()
        return
    bd = context.application.bot_data
    client, locations = bd.get("altegio"), bd.get("locations") or {}
    action = q.data.split(":")
    await q.answer()
    if action[1] == "panel":
        return await _edit(q, update, "🛠 Адмін-панель Custom True", admin.panel())
    if client is None:
        return await _edit(q, update, T.UNAVAILABLE, admin.panel())
    try:
        if action[1] == "tinv":
            sid = int(action[2])
            names = {s.id: s.name for loc in locations for s in await client.list_staff(loc)}
            token = await make_invite(sid)
            username = getattr(context.bot, "username", None) or "custom_true_bot"
            text = ("Запрошення для %s (одноразове, діє 7 днів):\n\nhttps://t.me/%s?start=team_%s\n\n"
                    "Перешліть майстру — він натисне Start і побачить свій графік, а сповіщення про його "
                    "клієнтів почнуть приходити йому в Telegram." % (names.get(sid, "майстра"), username, token))
            back = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Команда", callback_data="adm:team")]])
            return await _edit(q, update, text, back)
        if action[1] == "tdel":
            chat = await unlink(int(action[2]))
            if chat:
                try:
                    await context.bot.send_message(chat_id=chat, text="Вас відключено від графіка Custom True.")
                except TelegramError:
                    pass
        text, markup = await team_screen(client, locations)
        await _edit(q, update, text, markup)
    except AltegioError as e:
        logger.warning("team admin: %s", e)
        await _edit(q, update, T.API_ERROR, admin.panel())


async def _edit(q, update, text, markup):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest:
        await update.effective_chat.send_message(text, reply_markup=markup)


# ------------------------------------------------------------------ barber: link + /me

def me_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓 Мій графік сьогодні", callback_data="tm:appts:0"),
         InlineKeyboardButton("🗓 Завтра", callback_data="tm:appts:1")],
        [InlineKeyboardButton("🕳 Мої вікна сьогодні", callback_data="tm:free:0"),
         InlineKeyboardButton("🕳 Завтра", callback_data="tm:free:1")],
    ])


async def _set_commands(bot, chat_id: int, is_admin: bool) -> None:
    cmds = [BotCommand("start", "Головне меню"), BotCommand("book", "Записатися"),
            BotCommand("me", "Мій графік і вікна")]
    if is_admin:
        cmds.append(BotCommand("admin", "Адмін-панель: записи, вікна, статистика"))
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id))
    except TelegramError as e:
        logger.warning("set commands for %s: %s", chat_id, e)


async def team_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start team_<token> from an invite link."""
    arg = (context.args or [""])[0] if getattr(context, "args", None) else ""
    token = arg[len("team_"):] if arg.startswith("team_") else ""
    chat = update.effective_chat
    user = update.effective_user
    sid = await accept_invite(token, chat.id, admin_notify.user_name(user)) if token else None
    if sid is None:
        await chat.send_message("Посилання недійсне або вже використане. Попросіть адміністратора надіслати нове.",
                                reply_markup=context.application.bot_data.get("main_menu"))
        return
    name = ""
    client, locations = context.application.bot_data.get("altegio"), context.application.bot_data.get("locations") or {}
    if client is not None:
        try:
            name = {s.id: s.name for loc in locations for s in await client.list_staff(loc)}.get(sid, "")
        except AltegioError:
            pass
    await _set_commands(context.bot, chat.id, admin.is_admin(update))
    await chat.send_message(
        "Вітаю%s! 💈 Тепер ви в команді Custom True у цьому боті.\n\n"
        "• /me — ваш графік і вільні вікна на сьогодні й завтра\n"
        "• сповіщення про нові записи, перенесення й скасування ваших клієнтів приходитимуть сюди"
        % (", " + name if name else ""), reply_markup=me_panel())


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await staff_for_chat(update.effective_chat.id) is None:
        await update.effective_chat.send_message(T.FALLBACK, reply_markup=context.application.bot_data.get("main_menu"))
        return
    await update.effective_chat.send_message("🗓 Ваш графік", reply_markup=me_panel())


async def on_me_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    sid = await staff_for_chat(update.effective_chat.id)
    if sid is None:
        await q.answer()
        return
    bd = context.application.bot_data
    client, locations = bd.get("altegio"), bd.get("locations") or {}
    _, what, offset = q.data.split(":")
    await q.answer("⏳")
    if client is None:
        text = T.UNAVAILABLE
    else:
        try:
            day = datetime.now(client.tz).date() + timedelta(days=int(offset))
            fn = admin.appointments_text if what == "appts" else admin.free_windows_text
            text = await fn(client, locations, day, staff_id=sid)
        except AltegioError as e:
            logger.warning("me %s: %s", q.data, e)
            text = T.API_ERROR
    await _edit(q, update, text, me_panel())


def handlers():
    return [
        CommandHandler("start", team_start, filters=filters.Regex(r"^/start team_")),
        CommandHandler("me", me_command),
        CallbackQueryHandler(on_team_admin, pattern=r"^adm:(team|panel|tinv:\d+|tdel:\d+)$"),
        CallbackQueryHandler(on_me_button, pattern=r"^tm:(appts|free):[01]$"),
    ]
