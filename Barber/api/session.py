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

sys.path.insert(0, os.path.dirname(__file__))
from _games import GAMES, RATE_LIMIT_PER_MIN, SESSION_TTL  # noqa: E402
from _http import BadRequest, make_handler  # noqa: E402
from _kv import pipeline  # noqa: E402
from _tg import current_user, display_name  # noqa: E402


def handle(req):
    user = current_user(req.init_data)
    game = req.json().get("game")
    if game not in GAMES:
        raise BadRequest("unknown_game")

    uid = str(user["id"])
    now = time.time()
    rl_key = "rl:%s:%d" % (uid, int(now // 60))
    sid = secrets.token_urlsafe(18)
    count, _, _, _, _ = pipeline(
        ["INCR", rl_key],
        ["EXPIRE", rl_key, 120],
        ["SET", "sess:" + sid, json.dumps({"u": uid, "g": game, "t": now}), "EX", SESSION_TTL],
        ["HSET", "users", uid, display_name(user)],
        ["INCR", "stats:plays:" + game],
    )
    if int(count) > RATE_LIMIT_PER_MIN:
        raise BadRequest("rate_limited", 429)
    return 200, {"session": sid}


handler = make_handler(handle, methods=("POST",))
