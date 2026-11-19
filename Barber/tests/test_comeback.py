""""Time for a haircut" invitations (comeback.py) and the one-tap repeat booking (booking.start_repeat)."""
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

from telegram.ext import ConversationHandler  # noqa: E402

import booking  # noqa: E402
import comeback  # noqa: E402
import texts as T  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, GONE, HAIRCUT = 297851, 875690, 875756, 10808322  # 875756: dismissed in the fixture
CHAT, PHONE = 555, "380671234567"
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


class ComebackRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        self.bot = Bot()
        self.now = datetime.now(TZ).replace(hour=12, minute=30, second=0, microsecond=0)
        await self.links.link(CHAT, PHONE)

    async def asyncTearDown(self):
        await self.client.aclose()

    def visit(self, days_ago=30, staff=IGOR, attendance=1, phone="+" + PHONE, **kw):
        start = (self.now - timedelta(days=days_ago)).replace(hour=11, minute=0)
        services = kw.pop("services", [{"id": HAIRCUT, "title": "Чоловіча стрижка", "cost": 800}])
        return self.backend.add_appointment(LOC, staff, start, phone=phone, name="Тарас",
                                            attendance=attendance, services=services, **kw)

    async def run_(self, now=None, force=True):
        return await comeback.run(self.client, self.links, LOCATIONS, self.bot, now=now or self.now, force=force)

    async def test_invites_once_with_repeat_button(self):
        appt = self.visit()
        self.assertEqual(await self.run_(), 1)
        chat, text, markup = self.bot.sent[-1]
        self.assertEqual(chat, CHAT)
        self.assertIn("Ігор Халай", text)
        self.assertEqual(buttons(markup), [
            ("🔁 Повторити: Ігор · Чоловіча стрижка", "cb:rp:%d:%d" % (LOC, appt)),
            (T.COMEBACK_OTHER, "bk:new"),
            (T.COMEBACK_OFF, "cb:off"),
        ])
        self.assertEqual(await self.links.chat_for_appointment(LOC, appt), CHAT)  # the button will work
        self.assertEqual(await self.run_(now=self.now + timedelta(days=1)), 0)  # once per visit + cooldown

    async def test_catches_up_a_missed_day_but_not_older(self):
        self.visit(days_ago=32)
        self.assertEqual(await self.run_(), 1)
        self.bot.sent.clear()
        other = LinkStore(memory=True)
        await other.link(CHAT, PHONE)
        self.links = other
        self.backend.appointments.clear()
        self.visit(days_ago=33)  # outside the 3-day catch-up window
        self.assertEqual(await self.run_(), 0)

    async def test_no_invite_when_already_back_or_booked(self):
        self.visit()
        self.visit(days_ago=-3, attendance=0)  # booked for in 3 days
        self.assertEqual(await self.run_(), 0)
        self.backend.appointments.clear()
        self.visit()
        self.visit(days_ago=10)  # came again 10 days ago
        self.assertEqual(await self.run_(), 0)

    async def test_later_cancelled_booking_does_not_count(self):
        self.visit()
        self.visit(days_ago=-3, attendance=0, deleted=True)
        self.assertEqual(await self.run_(), 1)

    async def test_only_visits_that_happened(self):
        self.visit(attendance=0)  # never marked as arrived/confirmed
        self.visit(attendance=-1, days_ago=31)  # no-show
        self.assertEqual(await self.run_(), 0)

    async def test_unlinked_and_opted_out_get_nothing(self):
        self.visit(phone="+380500000000")  # nobody linked this phone
        self.assertEqual(await self.run_(), 0)
        self.backend.appointments.clear()
        self.visit()
        await self.links._pipe(["SET", "bb:cbout:%d" % CHAT, "1"])
        self.assertEqual(await self.run_(), 0)
        await self.links._pipe(["GETDEL", "bb:cbout:%d" % CHAT])
        await self.links.unlink(CHAT)  # /stop_reminders
        self.assertEqual(await self.run_(), 0)

    async def test_departed_barber_offers_any_barber(self):
        self.visit(staff=GONE)
        self.assertEqual(await self.run_(), 1)
        _, text, markup = self.bot.sent[-1]
        self.assertEqual(text, T.COMEBACK_NO_STAFF)
        self.assertEqual([b[1] for b in buttons(markup)], ["bk:new", "cb:off"])

    async def test_switch_hours_and_once_a_day(self):
        self.visit()
        with mock.patch.dict(os.environ, {"COMEBACK_ENABLED": ""}):
            self.assertEqual(await self.run_(force=False), 0)
        with mock.patch.dict(os.environ, {"COMEBACK_ENABLED": "1"}):
            self.assertEqual(await self.run_(now=self.now.replace(hour=9), force=False), 0)
            self.assertEqual(await self.run_(force=False), 1)
            self.visit(days_ago=31, phone="+380631112233")
            await self.links.link(777, "380631112233")
            self.assertEqual(await self.run_(now=self.now + timedelta(minutes=10), force=False), 0)  # done today


class RepeatBookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        await self.links.link(CHAT, PHONE)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS,
                                "main_menu": "MENU"})
        self.chat = FakeChat(CHAT)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def invited(self, staff=IGOR):
        start = datetime.now(TZ).replace(hour=11, minute=0, second=0, microsecond=0) - timedelta(days=30)
        appt = self.backend.add_appointment(LOC, staff, start, phone="+" + PHONE, attendance=1,
                                            services=[{"id": HAIRCUT, "title": "Чоловіча стрижка"}])
        await self.links.remember_appointment(LOC, appt, CHAT)
        return appt

    async def press(self, fn, data, chat=None):
        q = FakeQuery(data)
        c = chat or self.chat
        return await fn(FakeUpdate(c, c.id, query=q), self.ctx), q

    async def test_one_tap_goes_to_days_then_books(self):
        appt = await self.invited()
        state, q = await self.press(booking.start_repeat, "cb:rp:%d:%d" % (LOC, appt))
        self.assertEqual(state, booking.DAY)
        self.assertEqual(q.edits[-1], (None, None))  # invitation buttons removed
        bk = self.ctx.user_data["bk"]
        self.assertEqual((bk["staff_id"], bk["service_id"], bk["duration"]), (IGOR, HAIRCUT, 3600))
        day = [b[1] for b in buttons(self.chat.sent[-1][1]) if b[1].startswith("bk:day:")][0]
        state, q = await self.press(booking.on_day, day)
        self.assertEqual(state, booking.TIME)
        time_btn = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")][0]
        state, q = await self.press(booking.on_time, time_btn)
        self.assertEqual(state, booking.CONFIRM)  # phone already linked: straight to the summary
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        new = [a for a in self.backend.appointments.values() if a["id"] != appt]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["staff_id"], IGOR)

    async def test_other_chat_cannot_use_button(self):
        appt = await self.invited()
        state, q = await self.press(booking.start_repeat, "cb:rp:%d:%d" % (LOC, appt), chat=FakeChat(1))
        self.assertEqual((state, q.answers), (None, [T.NOT_YOURS]))

    async def test_departed_barber_goes_to_staff_list(self):
        appt = await self.invited(staff=GONE)
        state, q = await self.press(booking.start_repeat, "cb:rp:%d:%d" % (LOC, appt))
        self.assertEqual(state, booking.STAFF)
        self.assertTrue(any(t.startswith("Майстер") for t, _ in self.chat.sent))

    async def test_turn_off(self):
        q = FakeQuery("cb:off")
        await comeback.on_off(FakeUpdate(self.chat, CHAT, query=q), self.ctx)
        self.assertEqual(q.answers, [T.COMEBACK_OFF_DONE])
        self.assertTrue(await comeback._off(self.links, CHAT))


if __name__ == "__main__":
    unittest.main()
