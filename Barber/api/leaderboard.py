"""
GET /api/leaderboard?game=snake&period=month|all&limit=20
GET /api/leaderboard?game=snake&month=2026-10          (history: top 10 of a finished month)

Top players for one game. If the request carries valid initData, the caller's
own row is flagged (and returned separately when outside the top).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _games import (  # noqa: E402
    FIRST_MONTH, GAMES, HISTORY_SIZE, PERIODS, board_key, month_ends_at, month_id, past_months, valid_month,
)
from _http import BadRequest, make_handler  # noqa: E402
from _kv import cmd, pipeline  # noqa: E402
from _tg import current_user  # noqa: E402


def handle(req):
    game = req.query.get("game")
    if game not in GAMES:
        raise BadRequest("unknown_game")

    history_month = req.query.get("month")
    if history_month is not None:
        if not valid_month(history_month) or not (FIRST_MONTH <= history_month < month_id()):
            raise BadRequest("unknown_month")
        period, limit = history_month, HISTORY_SIZE
    else:
        period = req.query.get("period", "month")
        if period not in PERIODS:
            raise BadRequest("unknown_period")
        try:
            limit = max(1, min(int(req.query.get("limit", "20")), 100))
        except ValueError:
            raise BadRequest("invalid_limit")

    user = current_user(req.init_data, required=False)
    uid = str(user["id"]) if user else None
    key = board_key(game, period)

    cmds = [["ZREVRANGE", key, 0, limit - 1, "WITHSCORES"]]
    if uid:
        cmds += [["ZSCORE", key, uid], ["ZREVRANK", key, uid]]
    res = pipeline(*cmds)
    flat = res[0] or []
    ids = flat[0::2]
    scores = flat[1::2]
    names = cmd("HMGET", "users", *ids) if ids else []

    entries = [
        {"rank": i + 1, "name": names[i] or "Player", "score": int(float(scores[i])), "me": ids[i] == uid}
        for i in range(len(ids))
    ]
    me = None
    if uid and res[1] is not None:
        me = {"rank": int(res[2]) + 1, "score": int(float(res[1]))}
    return 200, {
        "game": game,
        "period": "history" if history_month else period,
        "month": history_month or month_id(),
        "ends_at": None if history_month else month_ends_at(),
        "months": past_months(),
        "entries": entries,
        "me": me,
    }


handler = make_handler(handle)
