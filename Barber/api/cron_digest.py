"""
GET /api/cron_digest — the morning digest at 9:00 Kyiv time (Vercel Cron, see vercel.json):
the shop's day to the admins, and each linked barber his own day (none on his day off).

Vercel Cron runs in UTC, so it calls at 06:00 and 07:00 UTC and only the run that falls on
09:xx in Kyiv sends (UTC+3 in summer, UTC+2 in winter). A per-day mark in the store makes
sure each admin gets one digest a day even if the cron fires twice. `?force=1` (still needs
the cron secret) sends now, for testing.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlparse

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from telegram import InlineKeyboardMarkup  # noqa: E402
from telegram.error import TelegramError  # noqa: E402
from telegram.ext import Application  # noqa: E402

import _games  # noqa: E402
import _kv  # noqa: E402
import admin  # noqa: E402
import admin_notify  # noqa: E402
import bot  # noqa: E402
import team  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("cron_digest")
DIGEST_HOUR = 9


async def run_once(force=False) -> dict:
    now_kyiv = _games.kyiv_now()
    if not force and now_kyiv.hour != DIGEST_HOUR:
        return {"ok": True, "skipped": "not %d:00 in Kyiv" % DIGEST_HOUR}
    ids = admin_notify.admin_chat_ids()
    staff = await team.members()
    if not ids and not staff:
        return {"ok": True, "skipped": "no ADMIN_CHAT_IDS and no team"}
    mark = "bb:digest:%s" % _games.day_id()
    if not force and _kv.cmd("SET", mark, "1", "EX", 2 * 86400, "NX") is None:
        return {"ok": True, "skipped": "already sent today"}
    app = Application.builder().token(bot.TELEGRAM_BOT_TOKEN).job_queue(None).build()
    sent = staff_sent = 0
    async with app:
        bot.prepare(app)
        client = app.bot_data.get("altegio")
        if client is None:
            return {"ok": True, "skipped": "altegio_not_configured"}
        locations = app.bot_data.get("locations") or {}
        try:
            from datetime import datetime
            now = datetime.now(client.tz)
            text = await admin.digest_text(client, locations, now) if ids else ""
            for chat_id in ids:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(
                        [[admin.dashboard_button("📊 Відкрити дашборд")]]))
                    sent += 1
                except TelegramError as e:
                    logger.warning("digest to %s failed: %s", chat_id, e)
            # Each barber: his own day (nothing on his day off).
            for sid, m in staff.items():
                try:
                    day = await team.staff_day_text(client, locations, sid, now, greet=True)
                    if day:
                        await app.bot.send_message(chat_id=int(m["chat"]), text=day, reply_markup=team.me_panel())
                        staff_sent += 1
                except Exception as e:  # noqa: BLE001 — one barber must not stop the others
                    logger.warning("staff digest %s failed: %s", sid, e)
        finally:
            await client.aclose()
    return {"ok": True, "sent": sent, "staff_sent": staff_sent}


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        secret = os.environ.get("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                                 ("Bearer " + secret).encode()):
            return self._reply(401, {"ok": False})
        force = dict(parse_qsl(urlparse(self.path).query)).get("force") == "1"
        try:
            self._reply(200, asyncio.run(run_once(force)))
        except Exception:
            logger.exception("digest failed")
            self._reply(500, {"ok": False})
