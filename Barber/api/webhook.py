"""
Telegram webhook for @custom_true_bot (Vercel Function). Telegram POSTs every update here.

Each call builds a fresh python-telegram-bot Application from bot.py, loads the conversation
state from the database (ptb_persistence.KVPersistence), handles the one update, and saves the
state back on exit. Always answers 200 so Telegram doesn't redeliver an update that failed.

If TELEGRAM_WEBHOOK_SECRET is set (and passed to setWebhook as secret_token), requests without
the matching X-Telegram-Bot-Api-Secret-Token header are rejected.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from telegram import Update  # noqa: E402

import bot  # noqa: E402
from ptb_persistence import KVPersistence  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("webhook")


async def process_update(body: dict) -> None:
    app = bot.build_webhook_application(KVPersistence())
    async with app:  # initialize: loads state; shutdown: saves it
        bot.prepare(app)
        try:
            await app.process_update(Update.de_json(body, app.bot))
        finally:
            client = app.bot_data.get("altegio")
            if client is not None:
                await client.aclose()


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if secret and not hmac.compare_digest(
                self.headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode(), secret.encode()):
            return self._reply(403, {"ok": False})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length))
            logger.info("update %s", body.get("update_id"))
            asyncio.run(process_update(body))
            self._reply(200, {"ok": True})
        except Exception:
            logger.error("webhook error: %s", traceback.format_exc())
            self._reply(200, {"ok": False})

    def do_GET(self):
        self._reply(200, {"status": "Barber bot webhook is running"})
