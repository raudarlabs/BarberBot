"""Bookings analytics (analytics.py), the dashboard API (api/dashboard.py) and hidden barbers."""
import asyncio
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ARCADE_DEV"] = "1"
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
    os.environ.pop(k, None)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

import _kv  # noqa: E402
import altegio  # noqa: E402
import analytics  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR, NIKITA = 297851, 875690, 2106676, 2222418


def rec(staff, when, st="arrived", cost=700, client=1, src="admin", title="Чоловіча стрижка", name=None):
    return {"s": staff, "n": name or {IGOR: "Ігор Халай", VIKTOR: "Віктор Дяченко"}.get(staff, ""),
            "t": when.isoformat(), "st": st, "c": cost, "k": client, "src": src, "sv": [[title, cost]]}


class PeriodTests(unittest.TestCase):
    def test_ranges(self):
        today = date(2026, 11, 14)
        p = analytics.period("month", today)
        self.assertEqual((p["first"], p["last"], p["prev_first"], p["prev_last"]),
                         (date(2026, 11, 1), today, date(2026, 10, 1), date(2026, 10, 14)))
        p = analytics.period("prev", today)
        self.assertEqual((p["first"], p["last"], p["prev_first"], p["prev_last"]),
                         (date(2026, 10, 1), date(2026, 10, 31), date(2026, 9, 1), date(2026, 9, 30)))
        p = analytics.period("7", today)
        self.assertEqual((p["first"], p["prev_first"], p["prev_last"]),
                         (date(2026, 11, 8), date(2026, 11, 1), date(2026, 11, 7)))
        # 31 March compared with February: the comparison stops at the end of February
        p = analytics.period("month", date(2027, 3, 31))
        self.assertEqual(p["prev_last"], date(2027, 2, 28))

    def test_delta(self):
        self.assertEqual(analytics.delta(110, 100), "+10%")
        self.assertEqual(analytics.delta(90, 100), "−10%")
        self.assertEqual(analytics.delta(5, 0), "")


class SummaryTests(unittest.TestCase):
    def test_counts_money_sources_and_barbers(self):
        d = datetime(2026, 11, 10, 10, tzinfo=TZ)
        now = datetime(2026, 11, 14, 12, tzinfo=TZ)
        recs = [
            rec(IGOR, d, cost=700, client=1, src="bot"),
            rec(IGOR, d + timedelta(days=1), cost=500, client=1, title="Борода"),
            rec(VIKTOR, d, cost=300, client=2, src="online"),
            rec(VIKTOR, d + timedelta(hours=2), st="no_show", client=3),
            rec(VIKTOR, d + timedelta(hours=3), st="waiting", client=4),            # never marked
            rec(IGOR, now + timedelta(hours=2), st="waiting", client=5),            # later today: upcoming
            rec(IGOR, d - timedelta(days=30), cost=999, client=6),                  # outside the period
        ]
        s = analytics.summarize(recs, date(2026, 11, 1), date(2026, 11, 14), now)
        t = s["totals"]
        self.assertEqual((t["visits"], t["revenue"], t["avg_check"], t["no_show"], t["unmarked"]), (3, 1500, 500, 1, 1))
        self.assertEqual((t["clients"], t["repeat_clients"], t["booked"]), (2, 1, 5))
        self.assertEqual(s["sources"], {"bot": 1, "online": 1, "admin": 3})
        self.assertEqual([b["name"] for b in s["barbers"]], ["Ігор Халай", "Віктор Дяченко"])
        self.assertEqual(s["barbers"][0]["avg"], 600)
        self.assertEqual(s["services"][0], {"title": "Чоловіча стрижка", "count": 2, "revenue": 1000})
        self.assertEqual(s["daily"]["visits"][9], 2)   # 10.11
        self.assertEqual(s["heat"][d.weekday()][10], 2)
        up = analytics.upcoming(recs, now)
        self.assertEqual((up["count"], up["by_barber"]), (1, {"Ігор Халай": 1}))
        mine = analytics.summarize(recs, date(2026, 11, 1), date(2026, 11, 14), now, staff_id=VIKTOR)
        self.assertEqual((mine["totals"]["visits"], [b["id"] for b in mine["barbers"]]), (1, [VIKTOR]))

    def test_hide_money(self):
        now = datetime(2026, 11, 14, 12, tzinfo=TZ)
        rep = {"current": analytics.summarize([rec(IGOR, now - timedelta(days=1))], date(2026, 11, 1), now.date(), now),
               "previous": {"totals": {"revenue": 5, "avg_check": 5}, "daily": {"revenue": [1]}},
               "upcoming": {"expected": 100}}
        out = analytics.hide_money(rep)
        self.assertIsNone(out["current"]["totals"]["revenue"])
        self.assertIsNone(out["current"]["barbers"][0]["revenue"])
        self.assertIsNone(out["current"]["services"][0]["revenue"])
        self.assertIsNone(out["upcoming"]["expected"])
        self.assertEqual(out["current"]["totals"]["visits"], 1)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_closed_days_come_from_the_cache(self):
        today = datetime.now(TZ).date()
        old = today - timedelta(days=20)
        start = datetime(old.year, old.month, old.day, 11, tzinfo=TZ)
        b = self.client.backend
        b.add_appointment(LOC, IGOR, start, phone="+380671234567", name="Тарас", attendance=1,
                          services=[{"id": 1, "title": "Стрижка", "cost": 700}], online=True)
        b.add_appointment(LOC, IGOR, start + timedelta(hours=2), phone="+380671234568", deleted=True)
        first = await analytics.records(self.client, [LOC], old, today, today)
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["src"], first[0]["c"], first[0]["st"]), ("online", 700.0, "arrived"))
        calls = len([r for r in b.requests if r[1].endswith("/appointments")])
        b.appointments.clear()  # a closed day is not asked again
        again = await analytics.records(self.client, [LOC], old, today, today)
        self.assertEqual(len(again), 1)
        new_calls = [r for r in b.requests if r[1].endswith("/appointments")][calls:]
        self.assertTrue(new_calls)
        self.assertTrue(all(r[2]["start_date"] >= (today - timedelta(days=analytics.FRESH_DAYS)).isoformat()
                            for r in new_calls))

    async def test_long_ranges_are_split(self):
        today = datetime.now(TZ).date()
        await analytics.records(self.client, [LOC], today - timedelta(days=60), today, today)
        spans = [(date.fromisoformat(r[2]["start_date"]), date.fromisoformat(r[2]["end_date"]))
                 for r in self.client.backend.requests if r[1].endswith("/appointments")]
        self.assertTrue(all((hi - lo).days <= analytics.CHUNK_DAYS for lo, hi in spans))


class HiddenStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_left_barber_is_not_offered(self):
        self.assertIn(NIKITA, altegio.hidden_staff({}))
        self.assertEqual(altegio.hidden_staff({"ALTEGIO_HIDE_STAFF": ""}), [])
        client = fixture_client()
        try:
            ids = [s.id for s in await client.list_staff(LOC)]
            self.assertIn(IGOR, ids)
            client.hidden_staff = {IGOR}
            self.assertNotIn(IGOR, [s.id for s in await client.list_staff(LOC)])
        finally:
            await client.aclose()


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        _kv._memory = None
        import dashboard
        from test_api import sign
        self.dashboard, self.sign = dashboard, sign
        now = datetime.now(TZ)
        self.backend_rows = [(IGOR, now - timedelta(days=1), 700), (VIKTOR, now - timedelta(days=1), 300)]

    def make_client(self):
        c = fixture_client()
        for staff, when, cost in self.backend_rows:
            c.backend.add_appointment(LOC, staff, when.replace(hour=11, minute=0), phone="+38067%07d" % (staff % 10000000),
                                      attendance=1, services=[{"id": 1, "title": "Стрижка", "cost": cost}])
        return c

    def call(self, query="p=month", **headers):
        from _http import Request, dispatch
        return dispatch(self.dashboard.handle, Request("GET", "/api/dashboard?" + query, headers.items(), b""))

    def run_call(self, **kw):
        with mock.patch.object(self.dashboard, "make_client", self.make_client), \
                mock.patch.dict(os.environ, {"ARCADE_ADMIN_KEY": "k3y", "ADMIN_CHAT_IDS": "169742339"}):
            return self.call(**kw)

    def body(self, resp):
        status, payload = resp[0], resp[-1]
        return status, json.loads(payload) if isinstance(payload, (bytes, str)) else payload

    def test_access(self):
        status, _ = self.body(self.run_call())
        self.assertEqual(status, 403)
        status, _ = self.body(self.run_call(**{"X-Admin-Key": "wrong"}))
        self.assertEqual(status, 403)
        status, _ = self.body(self.run_call(**{"X-Telegram-Init-Data": self.sign({"id": 555, "first_name": "Client"})}))
        self.assertEqual(status, 403)

    def test_admin_sees_everything(self):
        status, d = self.body(self.run_call(**{"X-Telegram-Init-Data": self.sign({"id": 169742339, "first_name": "Owner"})}))
        self.assertEqual(status, 200, d)
        self.assertEqual(d["role"], "admin")
        if date.today().day > 1:  # yesterday is in this month
            self.assertEqual(d["bookings"]["current"]["totals"]["revenue"], 1000)
        self.assertIn("games", d)
        status, d = self.body(self.run_call(query="p=7&staff=%d" % VIKTOR, **{"X-Admin-Key": "k3y"}))
        self.assertEqual([b["id"] for b in d["bookings"]["current"]["barbers"]], [VIKTOR])
        self.assertNotIn("games", d)

    def test_barber_sees_only_himself_without_money(self):
        _kv.pipeline(["HSET", "bb:team_chat", "777", str(VIKTOR)])
        init = self.sign({"id": 777, "first_name": "Viktor"})
        status, d = self.body(self.run_call(query="p=7&staff=%d" % IGOR, **{"X-Telegram-Init-Data": init}))
        self.assertEqual((status, d["role"]), (200, "staff"))
        self.assertEqual([b["id"] for b in d["bookings"]["current"]["barbers"]], [VIKTOR])  # staff= ignored
        self.assertIsNone(d["bookings"]["current"]["totals"]["revenue"])
        self.assertTrue(d["bookings"]["money_hidden"])
        self.assertNotIn("bot", d)


if __name__ == "__main__":
    unittest.main()
