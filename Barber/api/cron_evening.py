"""
GET /api/cron_evening — 21:00 Kyiv time: visits nobody marked in Altegio today (unmarked.py),
to the admins (all, by barber) and to each linked barber (his own). Vercel Cron runs in UTC, so
it calls at 18:00 and 19:00 UTC and only the run that falls on 21:xx in Kyiv sends; a per-day mark
makes it once a day. `?force=1` (still needs the cron secret) sends now, for testing.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlparse

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from telegram.error import TelegramError  # noqa: E402
from telegram.ext import Application  # noqa: E402

import _games  # noqa: E402
import _kv  # noqa: E402
import admin_notify  # noqa: E402
import bot  # noqa: E402
import team  # noqa: E402
import unmarked  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("cron_evening")
EVENING_HOUR = 21


async def run_once(force=False) -> dict:
    if not force and _games.kyiv_now().hour != EVENING_HOUR:
        return {"ok": True, "skipped": "not %d:00 in Kyiv" % EVENING_HOUR}
    ids = admin_notify.notify_chat_ids()
    staff = await team.members()
    if not ids and not staff:
        return {"ok": True, "skipped": "no notification chats and no team"}
    mark = "bb:evening:%s" % _games.day_id()
    if not force and _kv.cmd("SET", mark, "1", "EX", 2 * 86400, "NX") is None:
        return {"ok": True, "skipped": "already sent today"}
    app = Application.builder().token(bot.TELEGRAM_BOT_TOKEN).job_queue(None).build()
    out = {"ok": True, "admins": 0, "barbers": 0}
    async with app:
        bot.prepare(app)
        client = app.bot_data.get("altegio")
        if client is None:
            return {"ok": True, "skipped": "altegio_not_configured"}
        locations = app.bot_data.get("locations") or {}
        try:
            todays, older = await unmarked.collect(client, locations, datetime.now(client.tz))
            out["unmarked_today"], out["unmarked_older"] = len(todays), older
            text = unmarked.admin_text(todays, older, await unmarked._names(client, locations))
            for chat_id in ids if text else []:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                    out["admins"] += 1
                except TelegramError as e:
                    logger.warning("evening to admin %s failed: %s", chat_id, e)
            for sid, m in staff.items():
                own = unmarked.staff_text([a for a in todays if a.staff_id == sid])
                if not own:
                    continue
                try:
                    await app.bot.send_message(chat_id=int(m["chat"]), text=own)
                    out["barbers"] += 1
                except TelegramError as e:
                    logger.warning("evening to barber %s failed: %s", sid, e)
        finally:
            await client.aclose()
    logger.info("evening: %s", out)
    return out


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
            logger.exception("evening failed")
            self._reply(500, {"ok": False})
