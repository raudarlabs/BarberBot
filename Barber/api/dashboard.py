"""
GET /api/dashboard?p=7|30|90|month|prev[&staff=<id>][&fresh=1]

The shop dashboard (public/stats.html, opened from the bot as a Telegram Mini App): bookings
from Altegio — visits, revenue, average check, no-shows, where bookings come from, barbers,
services, busy hours, the coming week — compared with the previous period, plus the bot's
own activity and the games.

Who may look:
- an admin: Telegram initData of a user in ADMIN_CHAT_IDS, or the staff key (X-Admin-Key);
  everything, optionally one barber (`staff=`);
- a barber linked in the bot's team: only his own bookings, without money unless
  TEAM_SHOW_REVENUE=1, and without the bot/games blocks.
The answer is cached for 5 minutes; closed days are cached by analytics.py, so only the first
load of a long range is slow.
"""
import asyncio
import hmac
import json
import logging
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import _games  # noqa: E402
import _kv  # noqa: E402
import analytics  # noqa: E402
from _http import BadRequest, serve  # noqa: E402
from _tg import validate_init_data  # noqa: E402
from admin_notify import admin_chat_ids  # noqa: E402
from altegio import AltegioError, make_client  # noqa: E402
from config import booking_locations  # noqa: E402
from stats import collect  # noqa: E402

logger = logging.getLogger("dashboard")
PERIODS = ("7", "30", "90", "month", "prev")
CACHE_TTL = 300


def viewer(req) -> dict:
    """{"role": "admin"} or {"role": "staff", "staff": id}; raises 403 otherwise."""
    key = os.environ.get("ARCADE_ADMIN_KEY", "")
    sent = req.headers.get("x-admin-key", "")
    if key and sent and hmac.compare_digest(sent.encode(), key.encode()):
        return {"role": "admin"}
    user = validate_init_data(req.init_data, os.environ.get("TELEGRAM_BOT_TOKEN", "")) if req.init_data else None
    if user and user.get("id"):
        uid = int(user["id"])
        if uid in admin_chat_ids():
            return {"role": "admin", "name": user.get("first_name") or ""}
        sid = _kv.pipeline(["HMGET", "bb:team_chat", str(uid)])[0][0]
        if sid:
            return {"role": "staff", "staff": int(sid), "name": user.get("first_name") or ""}
    raise BadRequest("forbidden", 403)


async def _bookings(p: str, staff_id=None) -> dict:
    client = make_client()
    if client is None:
        return {"error": "altegio_not_configured"}
    try:
        locations = booking_locations()
        out = await analytics.report(client, locations, p, datetime.now(client.tz), staff_id=staff_id)
        out.pop("_records", None)
        return out
    except AltegioError as e:
        logger.warning("dashboard altegio: %s", e)
        return {"error": "altegio_error"}
    finally:
        await client.aclose()


def _bot() -> dict:
    import admin  # the bot's counters
    return {"events": admin._event_counts(), "chats": admin._chat_counts()}


def handle(req):
    who = viewer(req)
    p = req.query.get("p", "month")
    if p not in PERIODS:
        raise BadRequest("bad_period")
    staff = who.get("staff")
    if who["role"] == "admin" and str(req.query.get("staff", "")).isdigit():
        staff = int(req.query["staff"])
    key = "bb:dash:%s:%s" % (p, staff or "all")
    data = None
    if req.query.get("fresh") != "1":
        cached = _kv.cmd("GET", key)
        data = json.loads(cached) if cached else None
    if data is None:
        data = {"period": p, "staff": staff, "bookings": asyncio.run(_bookings(p, staff)),
                "generated": _games.kyiv_now().strftime("%d.%m %H:%M")}
        if staff is None:
            data["games"] = collect()
            try:
                data["bot"] = _bot()
            except Exception as e:  # noqa: BLE001
                logger.warning("dashboard bot counters: %s", e)
                data["bot"] = None
        if "error" not in data["bookings"]:
            _kv.cmd("SET", key, json.dumps(data, ensure_ascii=False), "EX", CACHE_TTL)
    data = dict(data, role=who["role"])
    if who["role"] == "staff" and os.environ.get("TEAM_SHOW_REVENUE") != "1" and "error" not in data["bookings"]:
        data["bookings"] = analytics.hide_money(json.loads(json.dumps(data["bookings"])))
    return 200, data


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("GET",))
