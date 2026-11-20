""""⚡ Вільно сьогодні", the morning message, the booking funnel and tomorrow's no-phone visits."""
import os
import sys
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

from telegram.error import Forbidden  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402

import _kv  # noqa: E402
import admin_notify  # noqa: E402
import booking  # noqa: E402
import free_windows as FW  # noqa: E402
import texts as T  # noqa: E402
import unmarked  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR, CHAT = 297851, 875690, 2106676, 555
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}


def monday_morning():
    d = datetime.now(TZ).date() + timedelta(days=1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 8, 0, tzinfo=TZ)


class Bot:
    def __init__(self, blocked=()):
        self.sent, self.blocked = [], set(blocked)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            raise Forbidden("blocked")
        self.sent.append((chat_id, text, reply_markup))


class FreeWindowsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS,
                                "main_menu": "MENU"})
        self.chat = FakeChat(CHAT)
        self.now = monday_morning()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_collect_three_per_barber_sorted(self):
        slots = await FW.collect(self.client, self.links, LOCATIONS, self.now.date(), self.now)
        self.assertTrue(slots)
        self.assertEqual(slots, sorted(slots, key=lambda x: (x["start"], x["name"])))
        for sid in (IGOR, VIKTOR):
            mine = [x for x in slots if x["staff"] == sid]
            self.assertLessEqual(len(mine), FW.PER_STAFF)
        self.assertTrue(all(x["start"] >= self.now + FW.LEAD for x in slots))
        self.assertEqual({x["service"] for x in slots if x["staff"] == IGOR}, {10808322})  # Чоловіча стрижка
        before = len([r for r in self.client.backend.requests if "/services" in r[1]])
        await FW.collect(self.client, self.links, LOCATIONS, self.now.date(), self.now)
        after = len([r for r in self.client.backend.requests if "/services" in r[1]])
        self.assertEqual(before, after)  # barbers' services are cached

    async def test_menu_screen_and_one_tap_booking(self):
        await self.links.link(CHAT, "380671234567")
        now = datetime.now(TZ)
        with mock.patch.object(FW, "EVENING_HOUR", 25):
            await FW.show(FakeUpdate(self.chat, CHAT, message=FakeMessage(self.chat, text=T.MENU_FREE)), self.ctx)
        text, markup = self.chat.sent[-1]
        times = [b for b in buttons(markup) if b[1].startswith("bk:fw:")]
        self.assertIn((T.FREE_SUB, "fw:sub"), buttons(markup))
        if not times:  # late in the day / closed today: tomorrow or none
            self.assertTrue(text.startswith(("⚡ Сьогодні вже", "Сьогодні й завтра")))
            return
        label, data = times[0]
        self.assertRegex(label, r"^\d\d:\d\d · \w+")
        q = FakeQuery(data)
        state = await booking.start_free_window(FakeUpdate(self.chat, CHAT, query=q), self.ctx)
        self.assertEqual(state, booking.CONFIRM)  # phone known: straight to the summary
        self.assertIn(data[-4:-2] + ":" + data[-2:], self.chat.sent[-1][0])
        state = await booking.on_confirm(FakeUpdate(self.chat, CHAT, query=FakeQuery("bk:ok")), self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertGreater(now.year, 2000)

    async def test_taken_slot_shows_that_barbers_times(self):
        slots = await FW.collect(self.client, self.links, LOCATIONS, self.now.date(), self.now)
        x = slots[0]
        self.client.backend.add_appointment(LOC, x["staff"], x["start"], duration=3600)  # someone took it
        data = "bk:fw:%d:%d:%d:%s" % (LOC, x["staff"], x["service"], x["start"].strftime("%Y%m%d%H%M"))
        state = await booking.start_free_window(FakeUpdate(self.chat, CHAT, query=FakeQuery(data)), self.ctx)
        self.assertEqual(state, booking.TIME)
        self.assertTrue(self.chat.sent[-1][0].startswith(T.SLOT_TAKEN))

    async def test_subscribe_toggle(self):
        q = FakeQuery("fw:sub")
        await FW.on_sub(FakeUpdate(self.chat, CHAT, query=q), self.ctx)
        self.assertTrue(await FW._subscribed(self.links, CHAT))
        q = FakeQuery("fw:unsub")
        await FW.on_sub(FakeUpdate(self.chat, CHAT, query=q), self.ctx)
        self.assertFalse(await FW._subscribed(self.links, CHAT))

    async def test_morning_once_and_blocked_unsubscribed(self):
        await self.links._pipe(["HSET", FW.SUBS, str(CHAT), "1"], ["HSET", FW.SUBS, "777", "1"])
        bot = Bot(blocked={777})
        at10 = self.now.replace(hour=10, minute=5)
        self.assertEqual(await FW.run_morning(self.client, self.links, LOCATIONS, bot, now=self.now), 0)  # 08:00
        self.assertEqual(await FW.run_morning(self.client, self.links, LOCATIONS, bot, now=at10), 1)
        self.assertTrue(bot.sent[-1][1].startswith("☀️"))
        self.assertIn((T.FREE_UNSUB, "fw:unsub"), buttons(bot.sent[-1][2]))
        self.assertFalse(await FW._subscribed(self.links, 777))  # blocked the bot
        self.assertEqual(await FW.run_morning(self.client, self.links, LOCATIONS, bot,
                                              now=at10 + timedelta(minutes=10)), 0)  # once a day


class FunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        await self.links.link(CHAT, "380671234567")
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS,
                                "main_menu": "MENU"})
        self.chat = FakeChat(CHAT)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press(self, fn, data):
        q = FakeQuery(data)
        return await fn(FakeUpdate(self.chat, CHAT, query=q), self.ctx), q

    async def test_counts_each_step_once_per_dialog(self):
        await booking.start_booking(FakeUpdate(self.chat, CHAT, message=FakeMessage(self.chat)), self.ctx)
        await self.press(booking.on_staff, "bk:stf:%d" % IGOR)
        await self.press(booking.on_back, "bk:back:stf")  # back and forth: still once
        await self.press(booking.on_staff, "bk:stf:%d" % IGOR)
        _, q = await self.press(booking.on_service, "bk:svc:10808322")
        day = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:day:")][0]
        _, q = await self.press(booking.on_day, day)
        f = admin_notify.funnel_counts()
        self.assertEqual([f[s]["today"] for s in admin_notify.FUNNEL], [1, 1, 1, 1, 0, 0, 0])
        t = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")][0]
        await self.press(booking.on_time, t)
        await self.press(booking.on_confirm, "bk:ok")
        f = admin_notify.funnel_counts()
        self.assertEqual([f[s]["month"] for s in admin_notify.FUNNEL], [1, 1, 1, 1, 1, 1, 1])

    async def test_shortcut_counts_implied_steps_and_moves_are_not_counted(self):
        self.ctx.user_data["bk"] = {"loc": LOC}
        await booking._step(self.ctx, "time")
        f = admin_notify.funnel_counts()
        self.assertEqual([f[s]["today"] for s in admin_notify.FUNNEL], [1, 1, 1, 1, 1, 0, 0])
        self.ctx.user_data["bk"] = {"loc": LOC, "reschedule": 1}
        await booking._step(self.ctx, "booked")
        self.assertEqual(admin_notify.funnel_counts()["booked"]["today"], 0)


class NoPhoneTomorrowTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_only_real_visits_without_phone(self):
        client = fixture_client()
        now = datetime.now(TZ).replace(hour=21, minute=0, second=0, microsecond=0)
        t = (now + timedelta(days=1)).replace(hour=9)
        svc = [{"id": 10808299, "title": "Чоловіча стрижка"}]
        client.backend.add_appointment(LOC, VIKTOR, t, name="Вітя", services=svc, client_id=0)  # no phone
        client.backend.add_appointment(LOC, IGOR, t, phone="+380671234567", services=svc)  # has phone
        client.backend.add_appointment(LOC, IGOR, t + timedelta(hours=3), client_id=0)  # a break, no services
        client.backend.add_appointment(LOC, VIKTOR, t + timedelta(hours=2), services=svc, client_id=0, deleted=True)
        items = await unmarked.no_phone_tomorrow(client, LOCATIONS, now)
        await client.aclose()
        self.assertEqual([(a.staff_id, a.start) for a in items], [(VIKTOR, t)])
        text = unmarked.no_phone_text(items, {VIKTOR: "Віктор Дяченко"})
        self.assertIn("📵 Завтра без телефону в записі: 1 візит", text)
        self.assertIn("💈 Віктор Дяченко — 09:00", text)
        self.assertIsNone(unmarked.no_phone_text([], {}))


if __name__ == "__main__":
    unittest.main()
