"""Post-visit ratings (reviews.py), the waitlist (waitlist.py) and unmarked visits (unmarked.py)."""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ARCADE_DEV"] = "1"
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
    os.environ.pop(k, None)

import _kv  # noqa: E402
import booking  # noqa: E402
import reviews  # noqa: E402
import texts as T  # noqa: E402
import unmarked  # noqa: E402
import waitlist  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR = 297851, 875690, 2106676
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}
CLIENT, ADMIN = 555, 1051263236


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


def next_monday(now):
    d = now.date() + timedelta(days=(7 - now.weekday()) or 7)
    return d


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.bot = Bot()
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS})
        self.ctx.bot = self.bot
        self.env = mock.patch.dict(os.environ, {"ADMIN_CHAT_IDS": str(ADMIN), "GOOGLE_REVIEW_URL": "https://g.page/r/test/review"})
        self.env.start()
        await self.links.link(CLIENT, "380671234567")
        self.now = datetime(2030, 1, 7, 15, 0, tzinfo=TZ)  # Monday
        b = self.client.backend
        self.done = b.add_appointment(LOC, IGOR, self.now - timedelta(hours=4), phone="+380671234567", attendance=1,
                                      services=[{"id": 1, "title": "Чоловіча стрижка", "cost": 700}])
        self.recent = b.add_appointment(LOC, IGOR, self.now - timedelta(hours=2), phone="+380671234567", attendance=1)
        self.unpaid = b.add_appointment(LOC, IGOR, self.now - timedelta(hours=5), phone="+380671234567", attendance=0)
        self.stranger = b.add_appointment(LOC, IGOR, self.now - timedelta(hours=6), phone="+380509999999", attendance=1)

    async def asyncTearDown(self):
        self.env.stop()
        await self.client.aclose()

    async def test_asks_once_after_a_finished_visit(self):
        self.assertEqual(await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now), 1)
        chat, text, markup = self.bot.sent[-1]
        self.assertEqual(chat, CLIENT)
        self.assertIn("Як вам візит", text)
        self.assertEqual([d for _, d in buttons(markup)], ["rv:%d:%d:%d" % (LOC, self.done, n) for n in range(1, 6)])
        # the 2-hours-ago visit ends at now-1h: not yet; the same visit is never asked twice
        self.assertEqual(await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now), 0)
        self.assertEqual(await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now + timedelta(hours=2)), 1)

    async def test_quiet_hours_and_opt_out(self):
        self.assertEqual(await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now.replace(hour=22)), 0)
        await self.links._pipe(["SET", "bb:optout:%d" % CLIENT, "1"])
        self.assertEqual(await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now), 0)

    async def rate(self, stars, chat=CLIENT, appt=None):
        q = FakeQuery("rv:%d:%d:%d" % (LOC, appt or self.done, stars))
        await reviews.on_rate(FakeUpdate(FakeChat(chat), chat, query=q), self.ctx)
        return q

    async def test_five_stars_offer_google(self):
        await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now)
        self.bot.sent.clear()
        q = await self.rate(5)
        text, markup = q.edits[-1]
        self.assertIn("Дякуємо", text)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://g.page/r/test/review")
        self.assertNotIn("rv_wait", self.ctx.user_data)
        self.assertEqual(self.bot.sent, [])                   # no admin alert for a 5
        q = await self.rate(1)                                # a second press does not overwrite
        self.assertIn("вже збережено", q.answers[-1])
        self.assertEqual(reviews.ratings(self.links._run, "2030-01-01", "2030-01-31")[0]["r"], 5)

    async def test_low_rating_asks_and_forwards_the_comment(self):
        await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now)
        self.bot.sent.clear()
        q = await self.rate(2)
        text, markup = q.edits[-1]
        self.assertIn("Що нам зробити краще", text)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://g.page/r/test/review")  # Google for everyone
        self.assertIn("Оцінка 2/5", self.bot.sent[-1][1])
        chat = FakeChat(CLIENT)
        msg = FakeMessage(chat, text="Довго чекав на рецепції")
        self.assertTrue(await reviews.take_feedback(FakeUpdate(chat, CLIENT, message=msg), self.ctx))
        self.assertIn("«Довго чекав на рецепції»", self.bot.sent[-1][1])
        self.assertIn("+380671234567", self.bot.sent[-1][1])
        self.assertIn("Передав власнику", msg.replies[-1][0])
        self.assertFalse(await reviews.take_feedback(FakeUpdate(chat, CLIENT, message=FakeMessage(chat, text="ще")), self.ctx))

    async def test_only_the_client_of_the_visit_can_rate(self):
        await reviews.run(self.client, self.links, LOCATIONS, self.bot, self.now)
        q = await self.rate(1, chat=777)
        self.assertEqual(q.edits, [])
        self.assertEqual(reviews.ratings(self.links._run, "2030-01-01", "2030-01-31"), [])

    async def test_stale_comment_window(self):
        self.ctx.user_data["rv_wait"] = {"appt": self.done, "loc": LOC, "r": 2, "n": "", "until": time.time() - 1}
        chat = FakeChat(CLIENT)
        self.assertFalse(await reviews.take_feedback(FakeUpdate(chat, CLIENT, message=FakeMessage(chat, text="x")), self.ctx))


class WaitlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.bot = Bot()
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS, "main_menu": "MENU"})
        self.ctx.bot = self.bot
        self.day = next_monday(datetime.now(TZ))
        svcs = await self.client.list_services(LOC, IGOR)
        self.svc = svcs[0]
        # the whole working day is booked
        b = self.client.backend
        self.appts = [b.add_appointment(LOC, IGOR, datetime(self.day.year, self.day.month, self.day.day, h, tzinfo=TZ),
                                        duration=3600, phone="+38067000%04d" % h) for h in range(8, 23)]

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press(self, data, chat=CLIENT):
        q = FakeQuery(data)
        upd = FakeUpdate(FakeChat(chat), chat, query=q)
        return q, upd

    async def test_booking_offers_the_waitlist_and_a_freed_time_is_sent(self):
        self.ctx.user_data["bk"] = {"loc": LOC, "staff_id": IGOR, "staff_name": "Ігор Халай", "service_id": self.svc.id,
                                    "services": {self.svc.id: self.svc}, "duration": self.svc.duration,
                                    "day": self.day.isoformat()}
        q, upd = await self.press("bk:day:" + self.day.isoformat())
        await booking.show_times(upd, self.ctx)
        text, markup = q.edits[-1]
        self.assertIn(T.NO_TIME, text)
        self.assertIn((T.WAITLIST_BUTTON, "bk:wl"), buttons(markup))
        q, upd = await self.press("bk:wl")
        await booking.on_waitlist(upd, self.ctx)
        self.assertIn("Напишу, щойно у Ігор Халай звільниться час", q.edits[-1][0])
        self.assertNotIn("bk", self.ctx.user_data)

        self.assertEqual(await waitlist.run(self.client, self.links, self.bot), 0)        # nothing freed yet
        self.client.backend.appointments[self.appts[6]]["deleted"] = True               # 14:00 cancelled
        self.assertEqual(await waitlist.run(self.client, self.links, self.bot), 1)
        chat, text, markup = self.bot.sent[-1]
        self.assertEqual(chat, CLIENT)
        self.assertIn("Звільнився час у Ігор Халай", text)
        self.assertIn("14:00", text)
        wd = "bk:wd:%d:%d:%d:%s" % (LOC, IGOR, self.svc.id, self.day.strftime("%Y%m%d"))
        self.assertIn(wd, [d for _, d in buttons(markup)])
        self.assertEqual(await waitlist.run(self.client, self.links, self.bot), 0)        # told once per freed time

        # the button opens booking on that day with the freed time
        self.ctx.user_data.clear()
        q, upd = await self.press(wd)
        await booking.start_on_day(upd, self.ctx)
        self.assertIn("bk:t:1400", [d for _, d in buttons(q.edits[-1][1])])

    async def test_list_remove_and_limits(self):
        now = datetime.now(TZ)
        for i in range(5):
            bk = {"loc": LOC, "staff_id": IGOR + i, "staff_name": "M%d" % i, "day": self.day.isoformat(), "service_id": 1}
            self.assertEqual(await waitlist.add(self.links, CLIENT, bk, [], now), "added")
        bk = {"loc": LOC, "staff_id": VIKTOR, "staff_name": "Віктор", "day": self.day.isoformat(), "service_id": 1}
        self.assertEqual(await waitlist.add(self.links, CLIENT, bk, [], now), "full")
        chat = FakeChat(CLIENT)
        await waitlist.show(FakeUpdate(chat, CLIENT, message=FakeMessage(chat)), self.ctx)
        self.assertIn("Ваш лист очікування", chat.sent[-1][0])
        eid = waitlist.entry_id(CLIENT, IGOR, self.day.isoformat())
        q, upd = await self.press("wl:rm:" + eid, chat=777)                     # someone else's: ignored
        await waitlist.on_remove(upd, self.ctx)
        self.assertIn(eid, await waitlist._all(self.links))
        q, upd = await self.press("wl:rm:" + eid)
        await waitlist.on_remove(upd, self.ctx)
        self.assertNotIn(eid, await waitlist._all(self.links))

    async def test_past_days_are_dropped(self):
        bk = {"loc": LOC, "staff_id": IGOR, "staff_name": "Ігор", "day": "2020-01-06", "service_id": 1}
        await waitlist.add(self.links, CLIENT, bk, [], datetime.now(TZ))
        await waitlist.run(self.client, self.links, self.bot)
        self.assertEqual(await waitlist._all(self.links), {})


class UnmarkedTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_todays_and_counts_older(self):
        client = fixture_client()
        try:
            b = client.backend
            now = datetime(2030, 1, 7, 21, 0, tzinfo=TZ)
            b.add_appointment(LOC, IGOR, now.replace(hour=10), phone="+380671234567", name="Олег", attendance=0)
            b.add_appointment(LOC, IGOR, now.replace(hour=12), phone="+380671234568", name="Петро", attendance=2)
            b.add_appointment(LOC, IGOR, now.replace(hour=14), phone="+380671234569", attendance=1)     # marked
            b.add_appointment(LOC, VIKTOR, now.replace(hour=11), phone="+380671234560", attendance=-1)  # no-show
            b.add_appointment(LOC, VIKTOR, now - timedelta(days=2), phone="+380671234561", attendance=0)
            b.add_appointment(LOC, VIKTOR, now.replace(hour=22), phone="+380671234562", attendance=0)  # later
            todays, older = await unmarked.collect(client, LOCATIONS, now)
            self.assertEqual(([a.client_name for a in todays], older), (["Олег", "Петро"], 1))
            text = unmarked.admin_text(todays, older, {IGOR: "Ігор Халай"})
            self.assertIn("Не відмічено сьогодні: 2 візити", text)
            self.assertIn("💈 Ігор Халай — 2: 10:00 Олег, 12:00 Петро", text)
            self.assertIn("+ ще 1 візит за минулі 7 днів", text)
            self.assertIn("1 візит сьогодні", unmarked.staff_text(todays[:1]))
            self.assertIsNone(unmarked.admin_text([], 0, {}))
            self.assertEqual([unmarked._visits(n) for n in (1, 2, 5, 11, 21, 22)],
                             ["візит", "візити", "візитів", "візитів", "візит", "візити"])
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
