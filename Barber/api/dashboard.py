"""
GET /api/dashboard?p=7|30|90|month|prev|YYYY-MM[&staff=<id>][&fresh=1]     header X-Telegram-Init-Data

The shop dashboard (public/stats.html), opened from the bot as a Telegram Mini App. Tabs:
KPI, bookings, money, team, games and the bot — see analytics.py for what each number means.

Access is by Telegram identity only (no keys, no links that work outside Telegram): the
request must carry Mini App initData signed with the bot token, at most SESSION_MAX_AGE old.
- an admin (user id in ADMIN_CHAT_IDS): everything, optionally one barber (`staff=`);
- a barber linked in the bot's team: only his own bookings and KPIs, without money unless
  TEAM_SHOW_REVENUE=1;
- anyone else: 403.
The answer is cached for 5 minutes; closed days are cached by analytics.py, so only the first
load of a long range is slow.
"""
import asyncio
import json
import logging
import os
import sys
import time
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
from _tg import failure_reason, validate_init_data  # noqa: E402
from admin_notify import admin_chat_ids  # noqa: E402
from altegio import AltegioError, make_client  # noqa: E402
from config import booking_locations  # noqa: E402
from stats import collect  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")
PERIODS = ("7", "30", "90", "month", "prev")
CACHE_TTL = 300
SESSION_MAX_AGE = 3600  # reopen the dashboard from the bot after an hour


def viewer(req) -> dict:
    """{"role": "admin", "user": id} or {"role": "staff", "staff": staff_id, "user": id}."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    init = req.init_data
    if not init:
        raise BadRequest("telegram_only", 401)
    user = validate_init_data(init, token, max_age=SESSION_MAX_AGE)
    if not user or not user.get("id"):
        reason = failure_reason(init, token, max_age=SESSION_MAX_AGE)
        logger.info("[dashboard] refused: %s", reason)
        raise BadRequest("expired" if reason == "expired" else "forbidden", 401 if reason == "expired" else 403)
    uid = int(user["id"])
    if uid in admin_chat_ids():
        return {"role": "admin", "user": uid}
    sid = _kv.pipeline(["HMGET", "bb:team_chat", str(uid)])[0][0]
    if sid:
        return {"role": "staff", "staff": int(sid), "user": uid}
    logger.info("[dashboard] refused: user %s is not an admin or a team member", uid)
    raise BadRequest("forbidden", 403)


async def _report(p: str, staff_id, admin: bool) -> dict:
    client = make_client()
    if client is None:
        return {"error": "altegio_not_configured"}
    try:
        show_money = admin or os.environ.get("TEAM_SHOW_REVENUE") == "1"
        return await analytics.full_report(client, booking_locations(), p, datetime.now(client.tz),
                                           staff_id=staff_id, with_money=show_money, with_team=admin)
    except AltegioError as e:
        logger.warning("dashboard altegio: %s", e)
        return {"error": "altegio_error"}
    finally:
        await client.aclose()


START_SOURCES = ("site", "qr_desk", "qr_mirror", "instagram", "other")


def _sources() -> dict:
    """People who came to the bot by source (t.me/...?start=<source>): this month and all time."""
    month = _games.month_id()
    cmds = []
    for src in START_SOURCES:
        for kind in ("joined", "open"):
            base = "stats:bot:%s_%s" % (kind, src)
            cmds += [["GET", base + ":" + month], ["GET", base]]
    vals = [int(float(v)) if v else 0 for v in _kv.pipeline(*cmds)]
    out = {}
    for i, src in enumerate(START_SOURCES):
        j, m, o, oa = vals[4 * i: 4 * i + 4]
        out[src] = {"joined_month": j, "joined_all": m, "opens_month": o, "opens_all": oa}
    return out


def _bot() -> dict:
    import admin  # the bot's counters
    import admin_notify  # booking funnel counters
    return {"events": admin._event_counts(), "chats": admin._chat_counts(), "sources": _sources(),
            "funnel": admin_notify.funnel_counts()}


def handle(req):
    who = viewer(req)
    admin = who["role"] == "admin"
    p = req.query.get("p", "month")
    if p not in PERIODS and analytics.month_param(p, _games.kyiv_now().date()) is None:
        raise BadRequest("bad_period")
    staff = who.get("staff")
    if admin and str(req.query.get("staff", "")).isdigit():
        staff = int(req.query["staff"])
    key = "bb:dash3:%s:%s:%s" % (p, staff or "all", "a" if admin else "s")
    data = None
    if req.query.get("fresh") != "1":
        cached = _kv.cmd("GET", key)
        data = json.loads(cached) if cached else None
    if data is None:
        started = time.monotonic()
        data = {"period": p, "staff": staff, "report": asyncio.run(_report(p, staff, admin)),
                "generated": _games.kyiv_now().strftime("%d.%m %H:%M")}
        r = data["report"]
        logger.info("[dashboard] built role=%s p=%s staff=%s visits=%s team=%s kpi=%s money=%s error=%s %.1fs",
                    who["role"], p, staff or "-", (r.get("bookings") or {}).get("current", {}).get("totals", {}).get("visits"),
                    len(r.get("team") or []), len(r.get("kpi") or []), r.get("money") is not None, r.get("error"),
                    time.monotonic() - started)
        if admin and staff is None:
            data["games"] = collect()
            try:
                data["bot"] = _bot()
            except Exception as e:  # noqa: BLE001
                logger.warning("dashboard bot counters: %s", e)
                data["bot"] = None
        if "error" not in data["report"]:
            _kv.cmd("SET", key, json.dumps(data, ensure_ascii=False), "EX", CACHE_TTL)
    data = dict(data, role=who["role"])
    if not admin and os.environ.get("TEAM_SHOW_REVENUE") != "1" and "error" not in data["report"]:
        data["report"] = analytics.hide_money(json.loads(json.dumps(data["report"])))
    return 200, data


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("GET",))
