"""
API tests against the in-memory Redis.

    cd Barber && python -m unittest discover -s tests
"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN"):
    os.environ.pop(k, None)
os.environ["ARCADE_DEV"] = "1"  # enables the in-memory Redis
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN"

import _kv  # noqa: E402
import _games  # noqa: E402
import leaderboard  # noqa: E402
import prize  # noqa: E402
import score  # noqa: E402
import session  # noqa: E402
import summary  # noqa: E402
from _http import Request, dispatch  # noqa: E402
from _tg import validate_init_data  # noqa: E402

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


def sign(user, token=TOKEN, auth_date=None, tamper=False):
    fields = {
        "auth_date": str(int(time.time() if auth_date is None else auth_date)),
        "query_id": "AAH",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(k + "=" + fields[k] for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = fields["user"].replace("Olena", "Admin")
    return urlencode(fields)


OLENA = {"id": 111, "first_name": "Olena", "last_name": "Koval", "username": "olena"}
TARAS = {"id": 222, "first_name": "Taras"}


def call(fn, method="GET", path="/", body=None, init_data=""):
    headers = {"X-Telegram-Init-Data": init_data}
    raw = json.dumps(body).encode() if body is not None else b""
    return dispatch(fn, Request(method, path, headers.items(), raw))


class RedisEnvTests(unittest.TestCase):
    def test_prefixed_vercel_kv_names(self):
        env = {"barber_bot_KV_REST_API_URL": "https://kv.example", "barber_bot_KV_REST_API_TOKEN": "tok",
               "barber_bot_KV_REST_API_READ_ONLY_TOKEN": "ro"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(_kv._creds(), ("https://kv.example", "tok"))


class InitDataTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_init_data(sign(OLENA), TOKEN)["id"], 111)

    def test_tampered(self):
        self.assertIsNone(validate_init_data(sign(OLENA, tamper=True), TOKEN))

    def test_wrong_token(self):
        self.assertIsNone(validate_init_data(sign(OLENA, token="999:OTHER"), TOKEN))

    def test_expired(self):
        self.assertIsNone(validate_init_data(sign(OLENA, auth_date=time.time() - 3 * 86400), TOKEN))

    def test_garbage(self):
        self.assertIsNone(validate_init_data("hash=abc&&&", TOKEN))
        self.assertIsNone(validate_init_data("", TOKEN))


class ArcadeFlowTests(unittest.TestCase):
    def setUp(self):
        _kv._memory = None
        os.environ["ARCADE_DEV"] = "1"

    def start(self, user, game="dryer"):
        status, data = call(session.handle, "POST", body={"game": game}, init_data=sign(user))
        self.assertEqual(status, 200, data)
        return data["session"]

    def age_session(self, sid, seconds):
        key = "sess:" + sid
        sess = json.loads(_kv.memory().data[key])
        sess["t"] -= seconds
        _kv.memory().data[key] = json.dumps(sess)

    def submit(self, user, sid, value):
        return call(score.handle, "POST", body={"session": sid, "score": value}, init_data=sign(user))

    def test_full_flow_and_best_per_player(self):
        sid = self.start(OLENA)
        self.age_session(sid, 30)
        status, data = self.submit(OLENA, sid, 25)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["month"], {"best": 25, "rank": 1})

        # A worse run doesn't lower the best
        sid = self.start(OLENA)
        self.age_session(sid, 30)
        status, data = self.submit(OLENA, sid, 10)
        self.assertEqual(data["month"]["best"], 25)

        sid = self.start(TARAS)
        self.age_session(sid, 60)
        status, data = self.submit(TARAS, sid, 40)
        self.assertEqual(data["month"]["rank"], 1)

        status, board = call(leaderboard.handle, path="/?game=dryer&period=month", init_data=sign(OLENA))
        self.assertEqual(status, 200)
        self.assertEqual([(e["name"], e["score"], e["me"]) for e in board["entries"]],
                         [("Taras", 40, False), ("Olena K.", 25, True)])
        self.assertEqual(board["me"], {"rank": 2, "score": 25})

        status, summ = call(summary.handle, init_data=sign(OLENA))
        self.assertEqual(summ["games"]["dryer"]["leader"], {"name": "Taras", "score": 40})
        self.assertEqual(summ["games"]["dryer"]["best"], {"month": 25, "all": 25})
        self.assertIsNone(summ["games"]["snake"]["leader"])

    def test_best_run_duration_is_recorded(self):
        sid = self.start(OLENA)
        self.age_session(sid, 42)
        self.submit(OLENA, sid, 30)
        sid = self.start(OLENA)
        self.age_session(sid, 90)
        self.submit(OLENA, sid, 12)  # worse run must not overwrite the best run's duration
        key = "runs:" + _games.board_key("dryer", "month")
        self.assertEqual(json.loads(_kv.memory().data[key]["111"])["d"], 42)

    def test_session_is_single_use(self):
        sid = self.start(OLENA)
        self.age_session(sid, 10)
        self.assertEqual(self.submit(OLENA, sid, 5)[0], 200)
        self.assertEqual(self.submit(OLENA, sid, 5)[0], 409)

    def test_session_belongs_to_its_player(self):
        sid = self.start(OLENA)
        self.age_session(sid, 10)
        self.assertEqual(self.submit(TARAS, sid, 5)[0], 409)

    def test_implausible_score_rejected(self):
        sid = self.start(OLENA)  # dryer: at most 4 points per second
        self.age_session(sid, 10)
        status, data = self.submit(OLENA, sid, 500)
        self.assertEqual((status, data["error"]), (422, "implausible_score"))

    def test_bad_input(self):
        sid = self.start(OLENA)
        self.assertEqual(self.submit(OLENA, sid, -1)[0], 400)
        self.assertEqual(self.submit(OLENA, sid, "9")[0], 400)
        self.assertEqual(call(session.handle, "POST", body={"game": "chess"}, init_data=sign(OLENA))[0], 400)
        self.assertEqual(call(leaderboard.handle, path="/?game=dryer&period=year")[0], 400)

    def test_auth_required_outside_dev(self):
        with mock.patch.dict(os.environ, {"ARCADE_DEV": "0"}):
            self.assertEqual(call(session.handle, "POST", body={"game": "snake"})[0], 401)
            self.assertEqual(
                call(session.handle, "POST", body={"game": "snake"}, init_data=sign(OLENA, tamper=True))[0], 401)

    def test_rate_limit(self):
        for _ in range(30):
            self.start(OLENA)
        status, data = call(session.handle, "POST", body={"game": "dryer"}, init_data=sign(OLENA))
        self.assertEqual(status, 429)


DEC_10_2026 = 1796860800.0  # 2026-12-10 00:00 UTC
JAN_10_2027 = 1799539200.0  # 2027-01-10 00:00 UTC


class CalendarTests(unittest.TestCase):
    def test_kyiv_month_rollover(self):
        # 2026-11-30 22:30 UTC is already 2026-12-01 00:30 in Kyiv (UTC+2 in winter)
        self.assertEqual(_games.month_id(1796077800), "2026-12")
        self.assertEqual(_games.month_id(1796074200), "2026-11")  # 21:30 UTC = 23:30 Kyiv
        # summer: UTC+3
        self.assertEqual(_games.month_id(1780261200), "2026-06")  # 2026-05-31 21:00 UTC = 06-01 00:00 Kyiv

    def test_month_end_and_history_months(self):
        self.assertEqual(_games.month_ends_at(DEC_10_2026), 1798754400)  # 2026-12-31 22:00 UTC
        self.assertEqual(_games.past_months(JAN_10_2027), ["2026-12", "2026-11"])
        self.assertEqual(_games.past_months(DEC_10_2026), ["2026-11"])

    def test_prize_code_is_stable_and_specific(self):
        a = _games.prize_code("2026-12", "snake", "111")
        self.assertEqual(a, _games.prize_code("2026-12", "snake", "111"))
        self.assertEqual(len(a), 8)
        self.assertNotEqual(a, _games.prize_code("2026-12", "snake", "222"))
        self.assertNotEqual(a, _games.prize_code("2026-11", "snake", "111"))


class MonthlyHistoryAndPrizeTests(unittest.TestCase):
    def setUp(self):
        _kv._memory = None
        os.environ["ARCADE_DEV"] = "1"
        os.environ["ARCADE_ADMIN_KEY"] = "staff-key"
        mem = _kv.memory()
        # December 2026 board: Olena wins snake, Taras second; 11 more players to test the top-10 cut
        mem.execute(["HSET", "users", "111", "Olena K.", "222", "Taras"])
        mem.execute(["ZADD", "lb:snake:2026-12", "500", "111", "300", "222"])
        for i in range(11):
            mem.execute(["ZADD", "lb:snake:2026-12", str(10 + i), "9%02d" % i])
        mem.execute(["ZADD", "lb:dryer:2026-12", "40", "222"])
        self.clock = mock.patch("time.time", return_value=JAN_10_2027)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()

    def test_history_board_is_top_10(self):
        status, board = call(leaderboard.handle, path="/?game=snake&month=2026-12", init_data=sign(OLENA))
        self.assertEqual(status, 200, board)
        self.assertEqual(len(board["entries"]), 10)
        self.assertEqual(board["entries"][0], {"rank": 1, "name": "Olena K.", "score": 500, "me": True})
        self.assertEqual(board["months"], ["2026-12", "2026-11"])
        self.assertEqual(board["period"], "history")

    def test_current_and_future_months_are_not_history(self):
        self.assertEqual(call(leaderboard.handle, path="/?game=snake&month=2027-01")[0], 400)
        self.assertEqual(call(leaderboard.handle, path="/?game=snake&month=2026-1")[0], 400)

    def test_new_month_starts_empty(self):
        status, board = call(leaderboard.handle, path="/?game=snake&period=month")
        self.assertEqual((board["month"], board["entries"]), ("2027-01", []))

    def test_winner_sees_prize_and_staff_redeems_it(self):
        status, summ = call(summary.handle, init_data=sign(OLENA))
        self.assertEqual(status, 200)
        self.assertEqual(summ["games"]["snake"]["last_winner"]["name"], "Olena K.")
        self.assertEqual(summ["games"]["dryer"]["last_winner"]["name"], "Taras")
        wins = summ["wins"]
        self.assertEqual([(w["game"], w["month"], w["redeemed"]) for w in wins], [("snake", "2026-12", False)])
        code = wins[0]["code"]

        # Taras has his own prize, not Olena's
        _, taras = call(summary.handle, init_data=sign(TARAS))
        self.assertEqual([w["game"] for w in taras["wins"]], ["dryer"])

        staff = {"X-Admin-Key": "staff-key"}
        req = lambda m, path, body=None, h=staff: dispatch(
            prize.handle, Request(m, path, h.items(), json.dumps(body).encode() if body else b""))
        _kv.memory().execute(["HSET", "runs:lb:snake:2026-12", "111", json.dumps({"d": 754, "t": 0})])
        status, info = req("GET", "/?code=" + code.lower())
        self.assertEqual((status, info["name"], info["game"], info["redeemed_at"], info["duration"]),
                         (200, "Olena K.", "snake", None, 754))
        status, info = req("POST", "/", {"code": code})
        self.assertEqual((status, info["already_redeemed"]), (200, False))
        status, info = req("POST", "/", {"code": code})
        self.assertTrue(info["already_redeemed"])
        _, summ = call(summary.handle, init_data=sign(OLENA))
        self.assertTrue(summ["wins"][0]["redeemed"])

        self.assertEqual(req("GET", "/?code=AAAAAAAA")[0], 404)
        self.assertEqual(req("GET", "/?code=" + code, h={"X-Admin-Key": "wrong"})[0], 403)
        with mock.patch.dict(os.environ, {"ARCADE_ADMIN_KEY": ""}):
            self.assertEqual(req("GET", "/?code=" + code)[0], 503)


if __name__ == "__main__":
    unittest.main()
