"""Admin panel (/admin), free windows, statistics and the morning digest — fixture Altegio, in-memory store."""
import asyncio
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
os.environ["NOTIFY_CHAT_IDS"] = "admins"  # notifications follow ADMIN_CHAT_IDS in these tests
os.environ["ARCADE_DEV"] = "1"
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
    os.environ.pop(k, None)

import _kv  # noqa: E402
import admin  # noqa: E402
import admin_notify  # noqa: E402
import texts as T  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR = 297851, 875690, 2106676
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}
ADMIN, CLIENT = 1051263236, 555


class FreeRangesTests(unittest.TestCase):
    def grid(self, free_minutes, start="10:00", end="13:00"):
        sh, sm = map(int, start.split(":")); eh, em = map(int, end.split(":"))
        return [{"time": "%d:%02d" % divmod(m, 60), "is_free": m in free_minutes}
                for m in range(sh * 60 + sm, eh * 60 + em, 5)]

    def test_stretches_of_30_min_or_more(self):
        free = set(range(600, 660, 5)) | set(range(700, 715, 5)) | set(range(750, 780, 5))
        self.assertEqual(admin.free_ranges(self.grid(free), date(2026, 11, 16)),
                         [("10:00", "11:00"), ("12:30", "13:00")])  # 11:40–11:55 is too short

    def test_today_cuts_off_the_past(self):
        free = set(range(600, 780, 5))
        now = datetime(2026, 11, 16, 11, 7, tzinfo=TZ)
        self.assertEqual(admin.free_ranges(self.grid(free), now.date(), now), [("11:10", "13:00")])
        self.assertEqual(admin.free_ranges(self.grid(free), date(2026, 11, 17), now), [("10:00", "13:00")])

    def test_empty_and_fully_busy(self):
        self.assertEqual(admin.free_ranges([], date(2026, 11, 16)), [])
        self.assertEqual(admin.free_ranges(self.grid(set()), date(2026, 11, 16)), [])


class AdminPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.ctx = FakeContext({"altegio": self.client, "locations": LOCATIONS, "main_menu": "MENU"})
        self.env = mock.patch.dict(os.environ, {"ADMIN_CHAT_IDS": "%d,169742339" % ADMIN})
        self.env.start()
        today = datetime.now(TZ).date()
        self.tomorrow = today + timedelta(days=(7 - today.weekday()) or 7)  # next Monday: a working day
        t = lambda h, m=0: datetime(self.tomorrow.year, self.tomorrow.month, self.tomorrow.day, h, m, tzinfo=TZ)  # noqa: E731
        b = self.client.backend
        self.bot_appt = b.add_appointment(LOC, IGOR, t(11), phone="+380671234567", name="Тарас",
                                          services=[{"id": 1, "title": "Чоловіча стрижка"}])
        b.appointments[self.bot_appt]["api_id"] = "tg-555-1"
        b.add_appointment(LOC, VIKTOR, t(10, 30), phone="+380501112233", name="Олена",
                          services=[{"id": 2, "title": "Стрижка машинкою"}], attendance=2)
        b.add_appointment(LOC, IGOR, t(15), phone="+380509999999", name="Скасований", deleted=True)

    async def asyncTearDown(self):
        self.env.stop()
        await self.client.aclose()

    async def press(self, data, chat=ADMIN):
        q = FakeQuery(data)
        await admin.on_admin_button(FakeUpdate(FakeChat(chat), chat, query=q), self.ctx)
        return q

    async def test_only_admins_get_the_panel(self):
        chat = FakeChat(CLIENT)
        await admin.admin_command(FakeUpdate(chat, CLIENT, message=FakeMessage(chat)), self.ctx)
        self.assertEqual(chat.sent[-1][0], T.FALLBACK)
        q = await self.press("adm:stats", chat=CLIENT)
        self.assertEqual(q.edits, [])
        chat = FakeChat(ADMIN)
        await admin.admin_command(FakeUpdate(chat, ADMIN, message=FakeMessage(chat)), self.ctx)
        self.assertIn("Адмін-панель", chat.sent[-1][0])

    async def test_appointments_tomorrow(self):
        text = await admin.appointments_text(self.client, LOCATIONS, self.tomorrow)
        markup = admin.panel()
        self.assertIn("Записи", text)
        self.assertIn(": 2 (через бот: 1)", text)  # the deleted one is not listed
        self.assertIn("11:00 · Тарас +380671234567 · Чоловіча стрижка 🤖", text)
        self.assertIn("10:30 · Олена +380501112233 · Стрижка машинкою ✅", text)
        self.assertNotIn("Скасований", text)
        self.assertIn("adm:stats", [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_free_windows_tomorrow(self):
        text = await admin.free_windows_text(self.client, LOCATIONS, self.tomorrow)
        self.assertIn("Вільні вікна", text)
        self.assertIn("💈 Ігор Халай:", text)
        self.assertNotIn("11:00–", text.split("Ігор Халай:")[1].split("\n")[0][:12])  # 11:00 is booked

    async def test_statistics(self):
        await admin_notify.count_event("booked")
        await admin_notify.count_event("cancelled")
        await admin_notify.count_event("reminded", 3)
        text, _ = (await self.press("adm:stats")).edits[-1]
        self.assertIn("Бот за місяць: записів 1 · переносів 0 · скасувань 1", text)
        self.assertIn("Цей місяць", text)
        self.assertIn("Наступні 7 днів: 2 записів", text)
        self.assertIn("Клієнтів у боті", text)
        self.assertIn("Ігри за місяць", text)

    async def test_morning_digest(self):
        now = datetime.combine(self.tomorrow, datetime.min.time(), TZ).replace(hour=9)
        text = await admin.digest_text(self.client, LOCATIONS, now)
        self.assertIn("Доброго ранку", text)
        self.assertIn("Записів сьогодні: 2 (через бот: 1)", text)
        self.assertIn("Вільні вікна", text)

    async def test_digest_button(self):
        text, _ = (await self.press("adm:digest")).edits[-1]
        self.assertIn("Доброго ранку", text)

    async def test_digest_on_a_day_off(self):
        sunday = self.tomorrow - timedelta(days=1)
        now = datetime.combine(sunday, datetime.min.time(), TZ).replace(hour=9)
        self.assertIn("Сьогодні вихідний", await admin.digest_text(self.client, LOCATIONS, now))


class DigestCronTests(unittest.TestCase):
    def setUp(self):
        _kv._memory = None

    def test_sends_only_at_9_kyiv_once_a_day(self):
        import cron_digest
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(chat_id)

        class App:
            def __init__(self):
                self.bot, self.bot_data = Bot(), {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Builder:
            def token(self, *a): return self
            def job_queue(self, *a): return self
            def build(self): return App()

        async def fake_digest(*a, **k):
            return "digest"

        class Client:
            tz = TZ

            async def aclose(self):
                pass
        client = Client()

        def prepare(app):
            app.bot_data.update(altegio=client, locations=LOCATIONS)

        with mock.patch.dict(os.environ, {"ADMIN_CHAT_IDS": "1,2"}), \
                mock.patch.object(cron_digest, "Application", mock.MagicMock(builder=lambda: Builder())), \
                mock.patch.object(cron_digest.bot, "prepare", prepare), \
                mock.patch.object(cron_digest.admin, "digest_text", fake_digest):
            async def scenario():
                with mock.patch.object(cron_digest._games, "kyiv_now", lambda ts=None: datetime(2026, 11, 16, 8, 0)):
                    self.assertIn("skipped", await cron_digest.run_once())
                with mock.patch.object(cron_digest._games, "kyiv_now", lambda ts=None: datetime(2026, 11, 16, 9, 0)):
                    self.assertEqual(await cron_digest.run_once(), {"ok": True, "sent": 2, "staff_sent": 0})
                    self.assertEqual((await cron_digest.run_once())["skipped"], "already sent today")
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(scenario())
            finally:
                loop.close()
        self.assertEqual(sent, [1, 2])


if __name__ == "__main__":
    unittest.main()
