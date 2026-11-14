"""
POST /api/score  {session, score}
Consumes the run's one-time session, checks the score is plausible for how long
the run lasted, and keeps each player's best score on the monthly and all-time boards.
Monthly boards are never expired: they are the history (top 10 per month, winners get prizes).
"""
import json
import os
import sys
import time

from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _games import board_key, max_allowed  # noqa: E402
from _http import BadRequest, serve  # noqa: E402
from _kv import cmd, pipeline  # noqa: E402
from _tg import current_user  # noqa: E402


def _int(v):
    return None if v is None else int(float(v))


def handle(req):
    user = current_user(req.init_data)
    body = req.json()
    sid = body.get("session")
    score = body.get("score")
    if not isinstance(sid, str) or not sid:
        raise BadRequest("session_required")
    if isinstance(score, bool) or not isinstance(score, int) or score < 0:
        raise BadRequest("invalid_score")

    raw = cmd("GETDEL", "sess:" + sid)
    if raw is None:
        raise BadRequest("session_invalid", 409)
    sess = json.loads(raw)
    uid = str(user["id"])
    if sess["u"] != uid:
        raise BadRequest("session_invalid", 409)

    game = sess["g"]
    duration = time.time() - float(sess["t"])
    if score > max_allowed(game, duration):
        print("[score] rejected uid=%s game=%s score=%d duration=%.1fs" % (uid, game, score, duration),
              file=sys.stderr)
        raise BadRequest("implausible_score", 422)

    month, alltime = board_key(game, "month"), board_key(game, "all")
    res = pipeline(
        ["ZADD", month, "GT", score, uid],
        ["ZADD", alltime, "GT", score, uid],
        ["ZSCORE", month, uid],
        ["ZREVRANK", month, uid],
        ["ZSCORE", alltime, uid],
        ["ZREVRANK", alltime, uid],
    )
    month_best, month_rank, all_best, all_rank = res[2], res[3], res[4], res[5]
    if _int(month_best) == score:
        # Keep how long the best run took, so staff can sanity-check a prize-winning score.
        cmd("HSET", "runs:" + month, uid, json.dumps({"d": int(duration), "t": int(time.time())}))
    return 200, {
        "game": game,
        "score": score,
        "month": {"best": _int(month_best), "rank": _int(month_rank) + 1},
        "all": {"best": _int(all_best), "rank": _int(all_rank) + 1},
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("POST",))

    def do_POST(self):
        serve(self, handle, ("POST",))
