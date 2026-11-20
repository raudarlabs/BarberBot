"""
Staff endpoint for monthly prizes (used by public/prize.html).

GET  /api/prize?code=ABCD2345   → who won it (game, month, name, score) and whether it was handed out
POST /api/prize {code}          → mark the prize as handed out

Requires header X-Admin-Key equal to the ARCADE_ADMIN_KEY env var.
"""
import hmac
import json
import os
import sys
import time

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _games import GAMES, board_key, past_months, prize_code  # noqa: E402
from _http import BadRequest, serve  # noqa: E402
from _kv import cmd, pipeline  # noqa: E402


def _check_admin(req):
    expected = os.environ.get("ARCADE_ADMIN_KEY", "")
    if not expected:
        raise BadRequest("not_configured", 503)
    given = req.headers.get("x-admin-key", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise BadRequest("forbidden", 403)


def find_prize(code):
    code = (code or "").strip().upper().replace(" ", "")
    if len(code) != 8:
        return None
    slots, cmds = [], []
    for m in past_months():
        for g in GAMES:
            slots.append((g, m))
            cmds.append(["ZREVRANGE", board_key(g, m), 0, 0, "WITHSCORES"])
    for (g, m), r in zip(slots, pipeline(*cmds) if cmds else []):
        if r and hmac.compare_digest(prize_code(m, g, r[0]), code):
            name, run = pipeline(["HMGET", "users", r[0]], ["HMGET", "runs:" + board_key(g, m), r[0]])
            meta = json.loads(run[0]) if run and run[0] else {}
            return {"code": code, "game": g, "month": m, "name": name[0] or "Player", "score": int(float(r[1])),
                    "duration": meta.get("d")}
    return None


def handle(req):
    _check_admin(req)
    if req.method == "GET":
        prize = find_prize(req.query.get("code"))
        if not prize:
            return 404, {"error": "not_found"}
        prize["redeemed_at"] = cmd("GET", "prize:redeemed:" + prize["code"])
        return 200, prize

    prize = find_prize(req.json().get("code"))
    if not prize:
        return 404, {"error": "not_found"}
    key = "prize:redeemed:" + prize["code"]
    first = cmd("SET", key, str(int(time.time())), "NX")
    prize["redeemed_at"] = cmd("GET", key)
    prize["already_redeemed"] = first is None
    return 200, prize


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("GET", "POST"))

    def do_POST(self):
        serve(self, handle, ("GET", "POST"))
