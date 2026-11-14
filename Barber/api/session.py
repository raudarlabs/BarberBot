"""
POST /api/session  {game}
Starts a server-timed run. The returned one-time token must accompany the score,
so the server knows how long the run lasted and that it was not replayed.
"""
import json
import os
import secrets
import sys
import time

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _games import GAMES, RATE_LIMIT_PER_MIN, SESSION_TTL, stat_keys  # noqa: E402
from _http import BadRequest, serve  # noqa: E402
from _kv import RedisError, pipeline  # noqa: E402
from _tg import AuthError, current_user, display_name  # noqa: E402


def handle(req):
    game = req.json().get("game")
    if game not in GAMES:
        raise BadRequest("unknown_game")
    try:
        user = current_user(req.init_data)
    except AuthError:
        # Played without Telegram identity (old keyboard button, plain browser): count it, then refuse.
        try:
            pipeline(*[["INCR", k] for k in stat_keys("guest", game)])
        except RedisError:
            pass
        raise

    uid = str(user["id"])
    now = time.time()
    rl_key = "rl:%s:%d" % (uid, int(now // 60))
    sid = secrets.token_urlsafe(18)
    count = pipeline(
        ["INCR", rl_key],
        ["EXPIRE", rl_key, 120],
        ["SET", "sess:" + sid, json.dumps({"u": uid, "g": game, "t": now}), "EX", SESSION_TTL],
        ["HSET", "users", uid, display_name(user)],
        *[["INCR", k] for k in stat_keys("plays", game)]
    )
    count = count[0]
    if int(count) > RATE_LIMIT_PER_MIN:
        raise BadRequest("rate_limited", 429)
    return 200, {"session": sid}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("POST",))

    def do_POST(self):
        serve(self, handle, ("POST",))
