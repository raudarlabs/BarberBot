"""
GET /api/cron_digest — the admins' morning digest at 9:00 Kyiv time (Vercel Cron, see vercel.json).

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

from telegram.error import TelegramError  # noqa: E402
from telegram.ext import Application  # noqa: E402

import _games  # noqa: E402
import _kv  # noqa: E402
import admin  # noqa: E402
import admin_notify  # noqa: E402
import bot  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("cron_digest")
DIGEST_HOUR = 9


async def run_once(force=False) -> dict:
    now_kyiv = _games.kyiv_now()
    if not force and now_kyiv.hour != DIGEST_HOUR:
        return {"ok": True, "skipped": "not %d:00 in Kyiv" % DIGEST_HOUR}
    ids = admin_notify.admin_chat_ids()
    if not ids:
        return {"ok": True, "skipped": "no ADMIN_CHAT_IDS"}
    mark = "bb:digest:%s" % _games.day_id()
    if not force and _kv.cmd("SET", mark, "1", "EX", 2 * 86400, "NX") is None:
        return {"ok": True, "skipped": "already sent today"}
    app = Application.builder().token(bot.TELEGRAM_BOT_TOKEN).job_queue(None).build()
    async with app:
        bot.prepare(app)
        client = app.bot_data.get("altegio")
        if client is None:
            return {"ok": True, "skipped": "altegio_not_configured"}
        try:
            from datetime import datetime
            text = await admin.digest_text(client, app.bot_data.get("locations") or {}, datetime.now(client.tz))
        finally:
            await client.aclose()
        sent = 0
        for chat_id in ids:
            try:
                await app.bot.send_message(chat_id=chat_id, text=text)
                sent += 1
            except TelegramError as e:
                logger.warning("digest to %s failed: %s", chat_id, e)
    return {"ok": True, "sent": sent}


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
