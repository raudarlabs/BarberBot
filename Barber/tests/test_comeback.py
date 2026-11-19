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
os.environ["ARCADE_DEV"] = "1"  # in-memory store for team.py / admin_notify.py
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
    os.environ.pop(k, None)

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


class IntervalTests(unittest.TestCase):
    def test_personal_interval(self):
        d = lambda n: datetime(2030, 1, 1, tzinfo=TZ) + timedelta(days=n)  # noqa: E731
        self.assertEqual(comeback.personal_interval([d(0)]), comeback.COMEBACK_DAYS)
        self.assertEqual(comeback.personal_interval([d(0), d(21), d(42), d(63)]), 21)
        self.assertEqual(comeback.personal_interval([d(0), d(5)]), comeback.MIN_INTERVAL)
        self.assertEqual(comeback.personal_interval([d(0), d(90)]), comeback.MAX_INTERVAL)
        self.assertEqual(comeback.personal_interval([d(0), d(20), d(40), d(100)]), 20)  # median ignores one long break
        self.assertEqual(comeback.personal_interval([d(0), d(0), d(28)]), 28)  # two visits one day = one


class ComebackRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        self.bot = Bot()
        self.now = datetime.now(TZ).replace(hour=12, minute=30, second=0, microsecond=0)
        await self.join(CHAT, PHONE)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def join(self, chat, phone):
        """A chat that wrote to the bot (bb:chats) and shared its phone."""
        await self.links._pipe(["HSET", "bb:chats", str(chat), "{}"])
        await self.links.link(chat, phone)

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
        last = self.now - timedelta(days=30)
        self.assertIn("%d %s" % (last.day, T.MONTHS_GEN[last.month - 1]), text)
        self.assertEqual(buttons(markup), [
            ("🔁 Повторити: Ігор · Чоловіча стрижка", "cb:rp:%d:%d" % (LOC, appt)),
            (T.COMEBACK_OTHER, "bk:new"),
            (T.COMEBACK_OFF, "cb:off"),
        ])
        self.assertEqual(await self.links.chat_for_appointment(LOC, appt), CHAT)  # the button will work
        self.assertEqual(await self.links.last_for(CHAT), (LOC, appt))  # "Як минулого разу"
        self.assertEqual(await self.run_(now=self.now + timedelta(days=1)), 0)  # once per visit

    async def test_personal_rhythm_every_three_weeks(self):
        for n in (63, 42, 21):
            self.visit(days_ago=n)
        self.assertEqual(await self.run_(now=self.now - timedelta(days=1)), 0)  # day 20: not yet
        self.assertEqual(await self.run_(), 1)  # day 21: due

    async def test_long_gone_clients_are_not_all_messaged(self):
        self.visit(days_ago=30 + comeback.GRACE_DAYS + 1)
        self.assertEqual(await self.run_(), 0)

    async def test_no_invite_when_booked_or_already_back(self):
        self.visit()
        self.visit(days_ago=-3, attendance=0)  # booked for in 3 days
        self.assertEqual(await self.run_(), 0)
        self.backend.appointments.clear()
        self.visit()
        self.visit(days_ago=10)  # came again 10 days ago: not due yet
        self.assertEqual(await self.run_(), 0)

    async def test_cancelled_booking_does_not_count(self):
        self.visit()
        self.visit(days_ago=-3, attendance=0, deleted=True)
        self.assertEqual(await self.run_(), 1)

    async def test_only_visits_that_happened(self):
        self.visit(attendance=0)  # never marked as arrived/confirmed
        self.visit(attendance=-1, days_ago=31)  # no-show
        self.assertEqual(await self.run_(), 0)

    async def test_unlinked_opted_out_and_cooldown(self):
        self.visit(phone="+380500000000")  # nobody shared this phone
        self.assertEqual(await self.run_(), 0)
        self.backend.appointments.clear()
        self.visit()
        await self.links._pipe(["SET", "bb:cbout:%d" % CHAT, "1"])
        self.assertEqual(await self.run_(), 0)
        await self.links._pipe(["GETDEL", "bb:cbout:%d" % CHAT])
        await self.links._pipe(["SET", "bb:cbchat:%d" % CHAT, "1", "EX", 100])
        self.assertEqual(await self.run_(), 0)
        await self.links._pipe(["GETDEL", "bb:cbchat:%d" % CHAT])
        await self.links.unlink(CHAT)  # /stop_reminders
        self.assertEqual(await self.run_(), 0)

    async def test_barbers_get_no_client_invitations(self):
        self.visit()
        await self.links._pipe(["HSET", "bb:team_chat", str(CHAT), str(IGOR)])
        self.assertEqual(await self.run_(), 0)

    async def test_departed_barber_offers_any_barber(self):
        self.visit(staff=GONE)
        self.assertEqual(await self.run_(), 1)
        _, text, markup = self.bot.sent[-1]
        self.assertTrue(text.startswith("Привіт! Здається"))
        self.assertIn("Custom True", text)
        self.assertEqual([b[1] for b in buttons(markup)], ["bk:new", "cb:off"])

    async def test_switch_hours_and_once_a_day(self):
        self.visit()
        with mock.patch.dict(os.environ, {"COMEBACK_ENABLED": ""}):
            self.assertEqual(await self.run_(force=False), 0)
        with mock.patch.dict(os.environ, {"COMEBACK_ENABLED": "1"}):
            self.assertEqual(await self.run_(now=self.now.replace(hour=9), force=False), 0)
            self.assertEqual(await self.run_(force=False), 1)
            self.visit(days_ago=31, phone="+380631112233")
            await self.join(777, "380631112233")
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


class StyleBookingTests(RepeatBookingTests):
    """AI haircut photo -> "✂️ Записатися на цю стрижку" -> booking that carries the haircut."""

    async def test_style_goes_to_comment_and_photo_to_shop(self):
        self.ctx.user_data["styles"] = [{"name": "Кроп з фейдом", "file_id": "PHOTO1"}]
        sent_photos = []

        class Bot:
            async def send_photo(self, chat_id, photo, caption=None, **kw):
                sent_photos.append((chat_id, photo, caption))

            async def send_message(self, chat_id, text, **kw):
                pass

        self.ctx.bot = Bot()
        with mock.patch("admin_notify.notify_chat_ids", return_value=[1051263236]):
            state, q = await self.press(booking.start_with_style, "bk:st:0")
            self.assertEqual(state, booking.STAFF)  # single location: straight to the barbers
            self.assertIn("Кроп з фейдом", self.chat.sent[0][0])
            await self.press(booking.on_staff, "bk:stf:%d" % IGOR)
            state, q = await self.press(booking.on_service, "bk:svc:%d" % HAIRCUT)
            day = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:day:")][0]
            state, q = await self.press(booking.on_day, day)
            t = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")][0]
            state, q = await self.press(booking.on_time, t)
            self.assertIn("Кроп з фейдом", q.edits[-1][0])  # shown in the summary
            state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        created = [a for a in self.backend.appointments.values()][-1]
        self.assertIn("Кроп з фейдом", created["comment"])
        self.assertEqual([(c, p) for c, p, _ in sent_photos], [(1051263236, "PHOTO1")])
        self.assertEqual(await self.links.last_for(CHAT), (LOC, created["id"]))  # next "як минулого разу"

    async def test_old_style_button_expires(self):
        state, q = await self.press(booking.start_with_style, "bk:st:3")
        self.assertEqual((state, q.answers), (None, [T.SESSION_EXPIRED]))


class RepeatLastTests(RepeatBookingTests):
    async def tap(self):
        from fakes import FakeMessage
        msg = FakeMessage(self.chat, text=T.MENU_REPEAT)
        return await booking.start_repeat_last(FakeUpdate(self.chat, CHAT, message=msg), self.ctx)

    async def test_found_by_phone_then_cached(self):
        appt = await self.invited()
        await self.links._pipe(["GETDEL", "bb:appt:%d:%d" % (LOC, appt)])  # not linked: found via phone
        state = await self.tap()
        self.assertEqual(state, booking.DAY)
        self.assertTrue(any(t.startswith("Як минулого разу: Ігор Халай · Чоловіча стрижка") for t, _ in self.chat.sent))
        self.assertEqual(await self.links.last_for(CHAT), (LOC, appt))
        before = len([r for r in self.backend.requests if r[1].endswith("/appointments")])
        await self.tap()
        after = len([r for r in self.backend.requests if r[1].endswith("/appointments")])
        self.assertEqual(before, after)  # second time: no history scan

    async def test_no_phone_asks_then_offers_button(self):
        await self.links.unlink(CHAT)
        state = await self.tap()
        self.assertEqual((state, self.chat.sent[-1][0]), (ConversationHandler.END, T.REPEAT_ASK_PHONE))
        self.assertEqual(self.ctx.user_data["after_contact"], "repeat")

    async def test_nothing_found_offers_booking(self):
        state = await self.tap()
        text, markup = self.chat.sent[-1]
        self.assertEqual((state, text, buttons(markup)), (ConversationHandler.END, T.REPEAT_NONE,
                                                          [(T.VISITS_BOOK, "bk:new")]))
