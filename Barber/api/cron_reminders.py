"""
GET /api/cron_reminders — visit reminders, run by Vercel Cron (see vercel.json, every 10 minutes).

Replaces the polling bot's JobQueue job: one pass of reminders.run_reminders over the next
day of Altegio appointments; already-sent reminders are remembered in the database, so
overlapping or repeated runs never send twice. Vercel calls it with
`Authorization: Bearer $CRON_SECRET`; anything else is refused.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from telegram.ext import Application  # noqa: E402

import bot  # noqa: E402
import comeback  # noqa: E402
import reminders  # noqa: E402
import reviews  # noqa: E402
import team  # noqa: E402
import waitlist  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("cron_reminders")


async def run_once() -> dict:
    """Visit reminders (when REMINDERS_ENABLED=1) and the team's change notifications (team.sync_changes)."""
    reminders_on = os.environ.get("REMINDERS_ENABLED") == "1"
    app = Application.builder().token(bot.TELEGRAM_BOT_TOKEN).job_queue(None).build()
    out = {"ok": True}
    async with app:
        bot.prepare(app)
        client = app.bot_data.get("altegio")
        if client is None:
            return {"ok": True, "sent": 0, "skipped": "altegio_not_configured"}
        locations = app.bot_data.get("locations") or {}
        try:
            if reminders_on:
                out["sent"] = await reminders.run_reminders(client, app.bot_data["links"], locations,
                                                            app.bot.send_message)
            else:
                out["reminders"] = "disabled (set REMINDERS_ENABLED=1)"
            try:
                out["team_notified"] = await team.sync_changes(client, locations, app.bot)
            except Exception:
                logger.exception("team sync failed")
                out["team_notified"] = "error"
            try:
                out["reviews_asked"] = await reviews.run(client, app.bot_data["links"], locations, app.bot)
            except Exception:
                logger.exception("reviews failed")
                out["reviews_asked"] = "error"
            try:  # once a day, only with COMEBACK_ENABLED=1
                out["comeback_invited"] = await comeback.run(client, app.bot_data["links"], locations, app.bot)
            except Exception:
                logger.exception("comeback failed")
                out["comeback_invited"] = "error"
            try:
                out["waitlist_told"] = await waitlist.run(client, app.bot_data["links"], app.bot)
            except Exception:
                logger.exception("waitlist failed")
                out["waitlist_told"] = "error"
            try:
                refreshed = await team.refresh_commands(app.bot)
                if refreshed:
                    out["team_commands"] = refreshed
            except Exception:
                logger.exception("team commands refresh failed")
        finally:
            await client.aclose()
    logger.info("cron: %s", out)
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
        given = self.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(given.encode(), ("Bearer " + secret).encode()):
            return self._reply(401, {"ok": False})
        try:
            self._reply(200, asyncio.run(run_once()))
        except Exception:
            logger.exception("reminders pass failed")
            self._reply(500, {"ok": False})
