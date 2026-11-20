"""
GET /api/stats   (X-Admin-Key)

Basic arcade analytics for public/stats.html: per game — runs started today / this month /
all time, runs without Telegram identity (old keyboard button or plain browser), finished runs,
unique players and the best score this month, plus a 14-day series; and hub opens.
Days and months are Kyiv time. Counters exist from the deploy that added them (Nov 2026).
"""
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _games import GAMES, board_key, day_id, last_days, month_id  # noqa: E402
from _http import BadRequest, serve  # noqa: E402
from _kv import pipeline  # noqa: E402

DAYS = 14


def _check_admin(req):
    expected = os.environ.get("ARCADE_ADMIN_KEY", "")
    if not expected:
        raise BadRequest("not_configured", 503)
    if not hmac.compare_digest(req.headers.get("x-admin-key", "").encode(), expected.encode()):
        raise BadRequest("forbidden", 403)


def _n(v):
    return int(float(v)) if v is not None else 0


def handle(req):
    _check_admin(req)
    return 200, collect()


def collect():
    """Arcade analytics (also used by the bot's admin panel)."""
    month, today, days = month_id(), day_id(), last_days(DAYS)

    slots, cmds = [], []

    def add(slot, command):
        slots.append(slot)
        cmds.append(command)

    for g in list(GAMES) + ["hub"]:
        kinds = ("opens",) if g == "hub" else ("plays", "guest", "finished")
        for kind in kinds:
            base = "stats:%s:%s" % (kind, g)
            add((g, kind, "all"), ["GET", base])
            add((g, kind, "month"), ["GET", "%s:%s" % (base, month)])
            add((g, kind, "today"), ["GET", "%s:%s" % (base, today)])
            for d in days:
                add((g, kind, "day", d), ["GET", "%s:%s" % (base, d)])
        if g != "hub":
            add((g, "players", "month"), ["ZCARD", board_key(g, "month")])
            add((g, "best", "month"), ["ZREVRANGE", board_key(g, "month"), 0, 0, "WITHSCORES"])

    out = {g: {"daily": {}} for g in list(GAMES) + ["hub"]}
    for slot, value in zip(slots, pipeline(*cmds)):
        g, kind, period = slot[0], slot[1], slot[2]
        if period == "day":
            out[g]["daily"].setdefault(kind, []).append(_n(value))
        elif kind == "best":
            out[g]["best_month"] = _n(value[1]) if value else None
        else:
            out[g].setdefault(kind, {})[period] = value if kind == "players" else _n(value)

    return {"month": month, "today": today, "days": days, "games": out}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("GET",))

    def do_POST(self):
        serve(self, handle, ("GET",))
