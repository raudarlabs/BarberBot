"""Bookings analytics (analytics.py), the dashboard API (api/dashboard.py) and hidden barbers."""
import asyncio
import json
import os
import sys
import time
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


class MoneyAndKpiTests(unittest.TestCase):
    def tx(self, d, a, c=5, cat="Services rendered", acc=575311, m=0):
        return {"id": abs(hash((d, a, c, m))) % 10**9, "d": d, "a": a, "c": c, "cat": cat, "acc": acc, "m": m, "goods": c == 7}

    def test_money_summary(self):
        accounts = {575311: {"id": 575311, "title": "Основная касса", "cash": True},
                    575312: {"id": 575312, "title": "Расчетный счет", "cash": False}}
        txs = [
            self.tx("2026-11-02", 700), self.tx("2026-11-02", 300, acc=575312), self.tx("2026-11-03", 400, c=7, cat="Product sales"),
            self.tx("2026-11-03", -100),                                                   # a refund
            self.tx("2026-11-04", -2000, c=3, cat="Team salaries", m=VIKTOR),
            self.tx("2026-11-04", -10056.28, c=61336, cat="Комуналка", acc=575312),
            self.tx("2026-11-04", -1000, c=61478, cat="Реклама, типография"),
            self.tx("2026-11-05", -8500, c=0, cat=""), self.tx("2026-11-05", 8500, c=0, cat=""),  # transfer
            self.tx("2026-10-30", 999),                                                     # outside
        ]
        m = analytics.money_summary(txs, date(2026, 11, 1), date(2026, 11, 14), accounts)
        t = m["totals"]
        self.assertEqual((t["income"], t["expense"], t["profit"]), (1300, 13056, 1300 - 13056))
        self.assertEqual((t["services"], t["goods"], t["salaries"]), (900, 400, 2000))
        self.assertEqual(t["cash_share"], round(100 * 1000 / 1300, 1))
        self.assertEqual([x["title"] for x in m["expenses"]], ["Комуналка", "Зарплати команди", "Реклама, друк"])
        self.assertEqual({x["title"] for x in m["accounts"]}, {"Основна каса", "Розрахунковий рахунок"})
        self.assertEqual(m["salaries"], {str(VIKTOR): 2000})
        self.assertEqual(m["daily"]["income"][1], 1000)

    def test_month_period(self):
        today = date(2026, 11, 14)
        p = analytics.period("2026-09", today)
        self.assertEqual((p["first"], p["last"], p["prev_first"], p["prev_last"], p["label"]),
                         (date(2026, 9, 1), date(2026, 9, 30), date(2026, 8, 1), date(2026, 8, 31), "Вересень 2026"))
        self.assertEqual(analytics.period("2026-11", today)["label"], "Цей місяць")
        self.assertIsNone(analytics.month_param("2026-12", today))   # the future
        self.assertIsNone(analytics.month_param("2019-01", today))   # before FIRST_MONTH
        self.assertIsNone(analytics.month_param("junk", today))

    def test_kpis_and_team(self):
        now = datetime(2026, 11, 14, 12, tzinfo=TZ)
        d = datetime(2026, 11, 10, 10, tzinfo=TZ)
        recs = [rec(IGOR, d, cost=700, client=1), rec(IGOR, d + timedelta(days=1), cost=700, client=1),
                rec(VIKTOR, d, cost=300, client=2, src="online"), rec(VIKTOR, d - timedelta(days=30), cost=300, client=2)]
        cur = analytics.summarize(recs, date(2026, 11, 1), date(2026, 11, 14), now)
        prev = analytics.summarize(recs, date(2026, 10, 1), date(2026, 10, 14), now)
        book = {"current": cur, "previous": {"first": prev["first"], "last": prev["last"], "totals": prev["totals"],
                                             "barbers": prev["barbers"]}}
        ov = {"income_total": 1700, "avg_check": 566.67, "occupancy": 60.6, "new_clients": 3, "return_percent": 90,
              "lost_clients": 5, "cancelled_percent": 18}
        money = {"current": {"totals": {"profit": 500, "margin": 30, "salaries": 400, "income": 1700, "goods": 0},
                             "salaries": {str(IGOR): 500}},
                 "previous": {"totals": {"profit": 100, "margin": 10, "salaries": 300, "income": 300, "goods": 0}}}
        k = {x["key"]: x for x in analytics.kpis(book, money, ov, {"income_total": 300, "occupancy": 50})}
        self.assertGreaterEqual(len(k), 10)
        self.assertEqual(k["occupancy"]["value"], 60.6)
        self.assertEqual(k["visits_day"]["value"], round(3 / 14, 1))
        self.assertEqual(k["online"]["value"], round(100 / 3, 1))
        self.assertEqual(k["barber_day"]["value"], round(1700 / 3))  # 3 barber-days with visits
        self.assertEqual(k["cancelled"]["good"], "down")
        rows = analytics.team(book, money, {IGOR: {"occupancy": 70, "new_clients": 1}}, {})
        self.assertEqual([r["name"] for r in rows], ["Ігор Халай", "Віктор Дяченко"])
        igor = rows[0]
        self.assertEqual((igor["rank"], igor["revenue"], igor["days"], igor["rev_day"], igor["repeat"]), (1, 1400, 2, 700, 1))
        self.assertEqual((igor["salary"], igor["salary_share"], igor["occupancy"]), (500, round(100 * 500 / 1400, 1), 70))
        self.assertEqual(rows[1]["revenue_prev"], 300)
        staff = analytics.hide_money({"bookings": {"current": cur, "previous": book["previous"], "upcoming": {"expected": 5}},
                                      "kpi": list(k.values()), "money": money, "overall": {"current": dict(ov), "previous": None},
                                      "team": rows})
        self.assertFalse(any(x["money"] for x in staff["kpi"]))
        self.assertIsNone(staff["money"])
        self.assertNotIn("team", staff)
        self.assertIsNone(staff["overall"]["current"]["income_total"])


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        _kv._memory = None
        import dashboard
        from test_api import sign
        self.dashboard, self.sign = dashboard, sign
        now = datetime.now(TZ)
        self.rows = [(IGOR, now - timedelta(days=1), 700), (VIKTOR, now - timedelta(days=1), 300)]

    def make_client(self):
        c = fixture_client()
        for staff, when, cost in self.rows:
            c.backend.add_appointment(LOC, staff, when.replace(hour=11, minute=0), phone="+38067%07d" % (staff % 10000000),
                                      attendance=1, services=[{"id": 1, "title": "Стрижка", "cost": cost}])
            c.backend.add_transaction(LOC, when.replace(hour=11, minute=30), cost)
        c.backend.add_transaction(LOC, self.rows[0][1].replace(hour=19), -400, item_id=3, item_title="Team salaries",
                                  master_id=IGOR)
        c.backend.set_overall(LOC, income=1000, avg_check=500, occupancy=61.5, new_clients=2)
        return c

    def call(self, query="p=month", **headers):
        from _http import Request, dispatch
        with mock.patch.object(self.dashboard, "make_client", self.make_client), \
                mock.patch.dict(os.environ, {"ARCADE_ADMIN_KEY": "k3y", "ADMIN_CHAT_IDS": "169742339"}):
            resp = dispatch(self.dashboard.handle, Request("GET", "/api/dashboard?" + query, headers.items(), b""))
        status, payload = resp[0], resp[-1]
        return status, json.loads(payload) if isinstance(payload, (bytes, str)) else payload

    def tg(self, uid, **kw):
        return {"X-Telegram-Init-Data": self.sign({"id": uid, "first_name": "U"}, **kw)}

    def test_telegram_only(self):
        self.assertEqual(self.call()[0], 401)                                   # no Telegram
        self.assertEqual(self.call(**{"X-Admin-Key": "k3y"})[0], 401)          # the staff key no longer opens it
        self.assertEqual(self.call(**self.tg(555))[0], 403)                    # a client
        forged = {"X-Telegram-Init-Data": self.tg(555)["X-Telegram-Init-Data"].replace("555", "169742339")}
        self.assertEqual(self.call(**forged)[0], 403)                          # someone else's id, bad signature
        old = self.tg(169742339, auth_date=time.time() - 2 * 3600)
        self.assertEqual(self.call(**old)[0], 401)                             # an old session
        self.assertEqual(self.call(query="p=2099-01", **self.tg(169742339))[0], 400)

    def test_admin_sees_everything(self):
        status, d = self.call(**self.tg(169742339))
        self.assertEqual(status, 200, d)
        r = d["report"]
        self.assertEqual(d["role"], "admin")
        self.assertIn("games", d)
        self.assertEqual(r["overall"]["current"]["occupancy"], 61.5)
        self.assertTrue(any(k["key"] == "profit" for k in r["kpi"]))
        if date.today().day > 1:  # yesterday is in this month
            self.assertEqual(r["bookings"]["current"]["totals"]["revenue"], 1000)
            self.assertEqual(r["money"]["current"]["totals"]["income"], 1000)
            self.assertEqual(r["money"]["current"]["totals"]["salaries"], 400)
            self.assertEqual(r["team"][0]["salary"], 400)
        status, d = self.call(query="p=7&staff=%d" % VIKTOR, **self.tg(169742339))
        self.assertEqual([b["id"] for b in d["report"]["bookings"]["current"]["barbers"]], [VIKTOR])
        self.assertNotIn("games", d)
        self.assertNotIn("team", d["report"])
        status, d = self.call(query="p=2026-09", **self.tg(169742339))
        self.assertEqual((status, d["report"]["label"]), (200, "Вересень 2026"))

    def test_barber_sees_only_himself_without_money(self):
        _kv.pipeline(["HSET", "bb:team_chat", "777", str(VIKTOR)])
        status, d = self.call(query="p=7&staff=%d" % IGOR, **self.tg(777))
        r = d["report"]
        self.assertEqual((status, d["role"]), (200, "staff"))
        self.assertEqual([b["id"] for b in r["bookings"]["current"]["barbers"]], [VIKTOR])  # staff= ignored
        self.assertIsNone(r["bookings"]["current"]["totals"]["revenue"])
        self.assertIsNone(r["money"])
        self.assertFalse(any(k["money"] for k in r["kpi"]))
        self.assertTrue(any(k["key"] == "occupancy" for k in r["kpi"]))
        self.assertNotIn("bot", d)


if __name__ == "__main__":
    unittest.main()
