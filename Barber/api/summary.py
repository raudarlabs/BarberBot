"""
GET /api/summary
One call for the hub screen: this month's leader for every game, last month's
winners, and — when the request carries valid initData — the caller's own bests
and any monthly prizes they won (with the code to show at the shop).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _games import GAMES, board_key, month_ends_at, month_id, past_months, prize_code  # noqa: E402
from _http import make_handler  # noqa: E402
from _kv import cmd, pipeline  # noqa: E402
from _tg import current_user  # noqa: E402

MAX_PRIZE_MONTHS = 12


def _int(v):
    return None if v is None else int(float(v))


def handle(req):
    user = current_user(req.init_data, required=False)
    uid = str(user["id"]) if user else None
    games = list(GAMES)
    months = past_months()[:MAX_PRIZE_MONTHS]

    cmds, slots = [], []
    for g in games:
        slots.append(("leader", g, None)); cmds.append(["ZREVRANGE", board_key(g, "month"), 0, 0, "WITHSCORES"])
        if uid:
            slots.append(("best_month", g, None)); cmds.append(["ZSCORE", board_key(g, "month"), uid])
            slots.append(("best_all", g, None)); cmds.append(["ZSCORE", board_key(g, "all"), uid])
        for m in months:
            slots.append(("winner", g, m)); cmds.append(["ZREVRANGE", board_key(g, m), 0, 0, "WITHSCORES"])
    res = pipeline(*cmds) if cmds else []

    ids = {r[0] for (kind, _, _), r in zip(slots, res) if kind in ("leader", "winner") and r}
    ids = sorted(ids)
    names = dict(zip(ids, cmd("HMGET", "users", *ids))) if ids else {}

    out = {g: {"leader": None, "best": None, "last_winner": None} for g in games}
    wins = []
    for (kind, g, m), r in zip(slots, res):
        if kind == "leader" and r:
            out[g]["leader"] = {"name": names.get(r[0]) or "Player", "score": _int(r[1])}
        elif kind == "best_month":
            out[g]["best"] = {"month": _int(r), "all": None}
        elif kind == "best_all":
            out[g]["best"]["all"] = _int(r)
        elif kind == "winner" and r:
            if m == (months[0] if months else None):
                out[g]["last_winner"] = {"name": names.get(r[0]) or "Player", "score": _int(r[1]), "month": m}
            if uid and r[0] == uid:
                code = prize_code(m, g, uid)
                wins.append({"game": g, "month": m, "score": _int(r[1]), "code": code})

    if wins:
        redeemed = pipeline(*[["GET", "prize:redeemed:" + w["code"]] for w in wins])
        for w, r in zip(wins, redeemed):
            w["redeemed"] = r is not None

    return 200, {"month": month_id(), "ends_at": month_ends_at(), "games": out, "wins": wins}


handler = make_handler(handle)
