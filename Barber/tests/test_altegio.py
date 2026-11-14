"""
Altegio client tests, against the recorded fixture (no network).

    cd Barber && python -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from altegio import (  # noqa: E402
    AltegioError,
    HttpAltegioClient,
    compute_free_starts,
    make_client,
    normalize_phone,
    parse_service,
    parse_staff,
)
from altegio_fixture import fixture_client  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
IGOR, VIKTOR = 875690, 2106676
HAIRCUT_IGOR = 10808322  # Чоловіча стрижка, 3600 s


def next_weekday(weekday, min_days=2):
    d = datetime.now(TZ).date() + timedelta(days=min_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def grid(open_="10:00", close="12:00", busy=()):
    out = []
    m = int(open_[:2]) * 60
    end = int(close[:2]) * 60
    while m < end:
        t = "%02d:%02d" % (m // 60, m % 60)
        out.append({"time": t, "is_free": not any(a <= t < b for a, b in busy)})
        m += 5
    return out


class PhoneTests(unittest.TestCase):
    def test_formats(self):
        for raw in ("+380 67 123 45 67", "380671234567", "0671234567", "067-123-45-67", "671234567", "00380671234567"):
            self.assertEqual(normalize_phone(raw), "380671234567", raw)

    def test_foreign_kept(self):
        self.assertEqual(normalize_phone("+48 600 700 800"), "48600700800")
        self.assertEqual(normalize_phone(""), "")


class FreeStartsTests(unittest.TestCase):
    day = date(2030, 1, 7)

    def starts(self, g, duration, **kw):
        return [s.strftime("%H:%M") for s in compute_free_starts(g, self.day, duration, TZ, **kw)]

    def test_hour_fits_until_end_of_day(self):
        self.assertEqual(self.starts(grid(), 3600), ["10:00", "10:15", "10:30", "10:45", "11:00"])

    def test_busy_block_splits_day(self):
        g = grid(close="13:00", busy=[("11:00", "11:30")])
        self.assertEqual(self.starts(g, 1800), ["10:00", "10:15", "10:30", "11:30", "11:45", "12:00", "12:15", "12:30"])
        self.assertEqual(self.starts(g, 3600), ["10:00", "11:30", "11:45", "12:00"])

    def test_lead_time(self):
        now = datetime(2030, 1, 7, 10, 20, tzinfo=TZ)
        self.assertEqual(self.starts(grid(), 1800, now=now), ["11:00", "11:15", "11:30"])

    def test_empty_and_garbage(self):
        self.assertEqual(self.starts([], 1800), [])
        self.assertEqual(self.starts([{"time": "xx"}], 1800), [])

    def test_legacy_free_key(self):
        g = [{"time": "10:00", "free": True}, {"time": "10:05", "free": True}]
        self.assertEqual(self.starts(g, 600), ["10:00"])


class ParseTests(unittest.TestCase):
    def test_staff_filters_hidden_and_fired(self):
        self.assertIsNone(parse_staff({"id": "1", "attributes": {"name": "A", "hidden": 1, "fired": 0}}))
        self.assertIsNone(parse_staff({"id": 1, "name": "A", "dismissed": True}))
        self.assertEqual(parse_staff({"id": "2", "attributes": {"name": " Таня ", "hidden": 0}}).name, "Таня")

    def test_service_price_and_duration(self):
        s = parse_service({"id": "5", "attributes": {"title": "Камуфлювання", "price_min": 300, "price_max": 500}})
        self.assertEqual(s.price_label, "300–500 ₴")
        self.assertEqual(s.duration, 3600)  # default when Altegio gives none
        s = parse_service({"id": 6, "title": "X", "company_links": [{"price_min": 800, "price_max": 800}],
                           "staff": [{"id": 9, "seance_length": 2700}]}, staff_id=9)
        self.assertEqual((s.price_label, s.duration), ("800 ₴", 2700))


class FixtureClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.monday = next_weekday(0)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_staff_and_services(self):
        staff = await self.client.list_staff(297851)
        self.assertEqual([s.name for s in staff], ["Ігор Халай", "Віктор Дяченко"])
        services = await self.client.list_services(297851, IGOR)
        haircut = next(s for s in services if s.id == HAIRCUT_IGOR)
        self.assertEqual((haircut.title, haircut.price_label, haircut.duration), ("Чоловіча стрижка", "800 ₴", 3600))
        method, path, params, _ = self.backend.requests[-1]
        self.assertEqual((path, params["staff_id"]), ("/locations/297851/services", str(IGOR)))

    async def test_headers(self):
        seen = {}

        def capture(request):
            seen.update(request.headers)
            return self.backend.handler(request)

        import httpx
        c = HttpAltegioClient("P", "U", transport=httpx.MockTransport(capture), min_interval=0)
        await c.list_staff(297851)
        await c.aclose()
        self.assertEqual(seen["authorization"], "Bearer P, User U")
        self.assertIn("application/vnd.api.v2+json", seen["accept"])

    async def test_working_dates_skip_day_off(self):
        start = datetime.now(TZ).date()
        days = await self.client.working_dates(297851, IGOR, start, 14)
        self.assertTrue(days)
        self.assertTrue(all(d.weekday() != 6 for d in days))
        self.assertTrue(all(start <= d < start + timedelta(days=14) for d in days))

    async def test_free_starts_respect_busy_block(self):
        starts = [s.strftime("%H:%M") for s in await self.client.free_starts(297851, IGOR, self.monday, 3600)]
        self.assertIn("12:00", starts)
        self.assertNotIn("12:15", starts)  # would run into the 13:00-14:00 block
        self.assertNotIn("13:30", starts)
        self.assertEqual(starts[-1], "19:00")

    async def test_create_blocks_slot_and_conflict_is_rejected(self):
        services = await self.client.list_services(297851, IGOR)
        haircut = [s for s in services if s.id == HAIRCUT_IGOR]
        start = datetime(self.monday.year, self.monday.month, self.monday.day, 15, 0, tzinfo=TZ)
        appt = await self.client.create_appointment(297851, IGOR, haircut, start, "067 123 45 67", "Тарас",
                                                    api_id="tg-1-1")
        self.assertEqual((appt.staff_id, appt.start, appt.client_phone), (IGOR, start, "380671234567"))
        body = self.backend.requests[-1][3]
        self.assertEqual(body["client"]["phone"], "+380671234567")
        self.assertEqual(body["seance_length"], 3600)
        self.assertFalse(body["save_if_busy"])

        starts = [s.strftime("%H:%M") for s in await self.client.free_starts(297851, IGOR, self.monday, 3600)]
        self.assertNotIn("15:00", starts)
        self.assertNotIn("14:15", starts)
        self.assertIn("16:00", starts)

        with self.assertRaises(AltegioError) as cm:
            await self.client.create_appointment(297851, IGOR, haircut, start + timedelta(minutes=30), "0671112233", "X")
        self.assertEqual(cm.exception.status, 422)

    async def test_list_confirm_reschedule(self):
        start = datetime(self.monday.year, self.monday.month, self.monday.day, 11, 0, tzinfo=TZ)
        appt_id = self.backend.add_appointment(297851, IGOR, start, phone="+380501112233", name="Олена",
                                               services=[{"id": HAIRCUT_IGOR, "title": "Чоловіча стрижка"}])
        appts = await self.client.list_appointments(297851, start - timedelta(hours=1), start + timedelta(hours=1))
        self.assertEqual(len(appts), 1)
        a = appts[0]
        self.assertEqual((a.id, a.status, a.client_phone, a.service_titles),
                         (appt_id, "waiting", "380501112233", "Чоловіча стрижка"))
        params = self.backend.requests[-1][2]
        self.assertEqual(params["filter[date_intersect_from]"], (start - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"))

        await self.client.confirm_appointment(297851, appt_id)
        self.assertEqual((await self.client.get_appointment(297851, appt_id)).status, "confirmed")

        moved = await self.client.reschedule_appointment(a, start + timedelta(hours=5))
        self.assertEqual(moved.start, start + timedelta(hours=5))
        method, path, _, body = self.backend.requests[-1]
        self.assertEqual((method, path), ("PUT", "/locations/297851/appointments/%d" % appt_id))
        for key in ("staff_id", "services", "client", "datetime", "seance_length"):
            self.assertIn(key, body)  # PUT is a full update

    async def test_error_envelope(self):
        self.backend.fail_next = (401, "Partner token is invalid")
        with self.assertRaises(AltegioError) as cm:
            await self.client.list_staff(297851)
        self.assertEqual((cm.exception.status, str(cm.exception)), (401, "Partner token is invalid"))


class MakeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_modes(self):
        self.assertIsNone(make_client({}))
        mock = make_client({"ALTEGIO_MOCK": "1"})
        self.assertTrue(mock.is_mock)
        await mock.aclose()
        real = make_client({"ALTEGIO_PARTNER_TOKEN": "p", "ALTEGIO_USER_TOKEN": "u", "ALTEGIO_MOCK": "1"})
        self.assertFalse(real.is_mock)  # real tokens always win over the fixture
        self.assertEqual(str(real._http.base_url).rstrip("/"), "https://api.alteg.io/api/v1")
        await real.aclose()


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    """Regressions for issues found in review."""

    def test_offset_without_colon(self):
        from altegio import _parse_local
        dt = _parse_local("2026-11-15T10:00:00+0200", TZ)
        self.assertEqual((dt.hour, dt.utcoffset()), (10, timedelta(hours=2)))

    def test_string_flags_and_unpadded_times(self):
        day = date(2030, 1, 7)
        g = [{"time": "9:00", "is_free": "1"}, {"time": "9:05", "is_free": "1"},
             {"time": "9:10", "is_free": "0"}, {"time": "9:15", "is_free": "true"}, {"time": "9:20", "is_free": "1"}]
        starts = [s.strftime("%H:%M") for s in compute_free_starts(g, day, 600, TZ)]
        self.assertEqual(starts, ["09:00", "09:15"])

    def test_exclude_frees_own_visit(self):
        day = date(2030, 1, 7)
        g = grid(close="13:00", busy=[("11:00", "12:00")])
        own = (datetime(2030, 1, 7, 11, 0, tzinfo=TZ), datetime(2030, 1, 7, 12, 0, tzinfo=TZ))
        starts = [s.strftime("%H:%M") for s in compute_free_starts(g, day, 3600, TZ, exclude=[own])]
        self.assertIn("11:30", starts)

    async def test_pagination_with_server_page_cap(self):
        client = fixture_client()
        start = datetime(2030, 1, 7, 10, 0, tzinfo=TZ)
        for i in range(23):
            client.backend.add_appointment(297851, IGOR, start + timedelta(minutes=10 * i), duration=600)
        client.backend.page_cap = 7
        appts = await client.list_appointments(297851, start, start + timedelta(hours=5))
        await client.aclose()
        self.assertEqual(len({a.id for a in appts}), 23)

    async def test_reschedule_keeps_fields(self):
        client = fixture_client()
        start = datetime(2030, 1, 7, 11, 0, tzinfo=TZ)
        appt_id = client.backend.add_appointment(297851, IGOR, start, phone="+380671234567", attendance=2,
                                                 services=[{"id": HAIRCUT_IGOR, "cost": 720, "first_cost": 800,
                                                            "discount": 10}])
        client.backend.appointments[appt_id].update(comment="VIP", api_id="tg-1-2")
        appt = await client.get_appointment(297851, appt_id)
        await client.reschedule_appointment(appt, start + timedelta(hours=2))
        body = client.backend.requests[-1][3]
        await client.aclose()
        self.assertEqual(body["services"], [{"id": HAIRCUT_IGOR, "cost": 720, "first_cost": 800, "discount": 10}])
        self.assertEqual((body["comment"], body["api_id"], body["attendance"]), ("VIP", "tg-1-2", 2))
