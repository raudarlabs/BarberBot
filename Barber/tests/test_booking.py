"""Booking conversation tests: the handlers driven step by step against the fixture."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Contact  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402
os.environ["NOTIFY_CHAT_IDS"] = "admins"  # notifications follow ADMIN_CHAT_IDS in these tests

import booking  # noqa: E402
import texts as T  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, HAIRCUT = 297851, 875690, 10808322
LOCATIONS = {LOC: "вул. Володимира Великого, 75", 708761: "вул. Гагаріна, 29/2"}
USER = 4242


def next_monday():
    d = datetime.now(TZ).date() + timedelta(days=2)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


class _FlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links,
                                "locations": dict(LOCATIONS), "main_menu": "MENU"})
        self.chat = FakeChat(USER)
        self.day = next_monday()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press(self, handler, data):
        q = FakeQuery(data)
        state = await handler(FakeUpdate(self.chat, USER, query=q), self.ctx)
        return state, q

    async def test_fully_booked_day_is_not_offered(self):
        """Days come from Altegio's online booking (/book_dates), like the widget calendar: a working
        day with no free time for the service is not offered (bug seen on the real API)."""
        for h in range(9, 21):  # fill Ігор's whole Monday
            self.backend.add_appointment(LOC, IGOR, datetime(self.day.year, self.day.month, self.day.day, h, 0,
                                                             tzinfo=TZ), duration=3600)
        await booking.start_booking(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat)), self.ctx)
        await self.press(booking.on_location, "bk:loc:%d" % LOC)
        await self.press(booking.on_staff, "bk:stf:%d" % IGOR)
        state, q = await self.press(booking.on_service, "bk:svc:%d" % HAIRCUT)
        days = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:day:")]
        self.assertNotIn("bk:day:" + self.day.isoformat(), days)
        self.assertIn("bk:day:" + (self.day + timedelta(days=1)).isoformat(), days)
        paths = [r[1] for r in self.backend.requests]
        self.assertTrue(any(p.startswith("/book_dates/") for p in paths))
        self.assertFalse(any(p.startswith("/timetable/dates/") for p in paths))

    async def walk_to_time(self):
        state = await booking.start_booking(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat)), self.ctx)
        self.assertEqual(state, booking.LOC)
        self.assertIn(("📍 вул. Гагаріна, 29/2", "bk:loc:708761"), buttons(self.chat.sent[-1][1]))

        state, q = await self.press(booking.on_location, "bk:loc:%d" % LOC)
        self.assertEqual(state, booking.STAFF)
        self.assertEqual([b for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:stf")],
                         [("💈 Ігор Халай", "bk:stf:875690"), ("💈 Віктор Дяченко", "bk:stf:2106676")])

        state, q = await self.press(booking.on_staff, "bk:stf:%d" % IGOR)
        self.assertEqual(state, booking.SERVICE)
        self.assertIn(("Чоловіча стрижка · 800 ₴", "bk:svc:%d" % HAIRCUT), buttons(q.edits[-1][1]))
        self.assertIn("Ігор Халай", q.edits[-1][0])

        state, q = await self.press(booking.on_service, "bk:svc:%d" % HAIRCUT)
        self.assertEqual(state, booking.DAY)
        self.assertIn(("Пн %02d.%02d" % (self.day.day, self.day.month), "bk:day:" + self.day.isoformat()),
                      buttons(q.edits[-1][1]))

        state, q = await self.press(booking.on_day, "bk:day:" + self.day.isoformat())
        self.assertEqual(state, booking.TIME)
        times = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")]
        self.assertIn("bk:t:1200", times)
        self.assertNotIn("bk:t:1300", times)  # fixture lunch block
        return times



class BookingFlowTests(_FlowBase):
    async def test_admins_are_notified_of_a_new_booking(self):
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, **kw):
                sent.append((chat_id, text))
        self.ctx.bot = Bot()
        os.environ["ADMIN_CHAT_IDS"] = "111, 222"
        try:
            await self.walk_to_time()
            await self.press(booking.on_time, "bk:t:1500")
            own = Contact("+380 67 123 45 67", "Тарас", last_name="Коваль", user_id=USER)
            await booking.on_contact(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat, contact=own)), self.ctx)
            state, q = await self.press(booking.on_confirm, "bk:ok")
        finally:
            os.environ.pop("ADMIN_CHAT_IDS", None)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual([c for c, _ in sent], [111, 222])
        text = sent[0][1]
        for part in ("Новий запис", "Тарас Коваль", "+380671234567", "Ігор Халай", "15:00"):
            self.assertIn(part, text)

    async def test_any_barber_earliest_time(self):
        await booking.start_booking(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat)), self.ctx)
        state, q = await self.press(booking.on_location, "bk:loc:%d" % LOC)
        self.assertEqual(buttons(q.edits[-1][1])[0], (T.ANY_BUTTON, "bk:any"))

        state, q = await self.press(booking.on_any, "bk:any")
        self.assertEqual(state, booking.ANY)
        services = [b for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:asv:")]
        # each barber's own "Чоловіча стрижка" is one choice, offered by both → listed first
        self.assertTrue(services[0][0].startswith("Чоловіча стрижка · від"))

        state, q = await self.press(booking.on_any_service, services[0][1])
        self.assertEqual(state, booking.ANY)
        self.assertEqual(q.edits[0][0], T.ANY_SEARCHING)
        text, markup = q.edits[-1]
        self.assertIn("Чоловіча стрижка", text)
        options = [b for b in buttons(markup) if b[1].startswith("bk:ago:")]
        self.assertGreaterEqual(len(options), 2)  # Ігор and Віктор both have time
        stamps = [o[1].rsplit(":", 1)[1] for o in options]
        self.assertEqual(stamps, sorted(stamps))  # earliest first
        self.assertTrue(any("Ігор" in o[0] for o in options) and any("Віктор" in o[0] for o in options))

        pick = options[0][1]
        _, _, sid, svc_id, stamp = pick.split(":")
        state, q = await self.press(booking.on_any_go, pick)
        self.assertEqual(state, booking.PHONE)
        own = Contact("+380 67 123 45 67", "Тарас", user_id=USER)
        state = await booking.on_contact(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat, contact=own)), self.ctx)
        self.assertEqual(state, booking.CONFIRM)
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        appt = next(iter(self.backend.appointments.values()))
        self.assertEqual(appt["staff_id"], int(sid))
        self.assertTrue(appt["datetime"].startswith("%s-%s-%sT%s:%s" % (stamp[:4], stamp[4:6], stamp[6:8], stamp[8:10], stamp[10:12])))
        body = [r for r in self.backend.requests if r[0] == "POST"][-1][3]
        self.assertEqual(body["services"], [{"id": int(svc_id)}])

    async def test_full_booking_with_contact_share(self):
        await self.walk_to_time()
        state, q = await self.press(booking.on_time, "bk:t:1500")
        self.assertEqual(state, booking.PHONE)
        text, kb = self.chat.sent[-1]
        self.assertEqual(text, T.ASK_PHONE)
        self.assertTrue(kb.keyboard[0][0].request_contact)

        # Someone else's contact is refused.
        foreign = Contact("+380500000000", "Інший", user_id=1)
        msg = FakeMessage(self.chat, contact=foreign)
        state = await booking.on_contact(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        self.assertEqual((state, msg.replies[-1][0]), (booking.PHONE, T.NOT_YOUR_CONTACT))

        own = Contact("+380 67 123 45 67", "Тарас", last_name="Коваль", user_id=USER)
        state = await booking.on_contact(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat, contact=own)), self.ctx)
        self.assertEqual(state, booking.CONFIRM)
        summary, markup = self.chat.sent[-1]
        self.assertIn("Ігор Халай", summary)
        self.assertIn("о 15:00", summary)
        self.assertIn("+380671234567", summary)
        self.assertIn(T.MOCK_NOTE.strip(), summary)
        self.assertIn((T.CONFIRM_BUTTON, "bk:ok"), buttons(markup))
        self.assertEqual(await self.links.phone_for_chat(USER), "380671234567")

        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(len(self.backend.appointments), 1)
        appt_id, appt = next(iter(self.backend.appointments.items()))
        self.assertEqual(appt["staff_id"], IGOR)
        self.assertTrue(appt["datetime"].startswith(self.day.isoformat() + "T15:00:00"))
        create_body = [r for r in self.backend.requests if r[0] == "POST"][-1][3]
        self.assertEqual(create_body["client"], {"phone": "+380671234567", "name": "Тарас Коваль"})
        self.assertEqual(await self.links.chat_for_appointment(LOC, appt_id), USER)
        final, menu = self.chat.sent[-1]
        self.assertTrue(final.startswith("Готово! Ви записані"))
        self.assertEqual(menu, "MENU")
        self.assertNotIn("bk", self.ctx.user_data)

    async def test_linked_phone_skips_contact_step(self):
        await self.links.link(USER, "380671234567")
        await self.walk_to_time()
        state, q = await self.press(booking.on_time, "bk:t:1200")
        self.assertEqual(state, booking.CONFIRM)
        self.assertIn("+380671234567", q.edits[-1][0])

    async def test_slot_taken_returns_to_times(self):
        await self.links.link(USER, "380671234567")
        await self.walk_to_time()
        await self.press(booking.on_time, "bk:t:1600")
        # Someone grabs 16:00 before the client confirms.
        start = datetime(self.day.year, self.day.month, self.day.day, 16, 0, tzinfo=TZ)
        self.backend.add_appointment(LOC, IGOR, start)
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, booking.TIME)
        text, markup = q.edits[-1]
        self.assertTrue(text.startswith(T.SLOT_TAKEN))
        self.assertNotIn("bk:t:1600", [b[1] for b in buttons(markup)])

    async def test_back_and_cancel(self):
        await self.walk_to_time()
        state, q = await self.press(booking.on_back, "bk:back:day")
        self.assertEqual(state, booking.DAY)
        state, q = await self.press(booking.on_back, "bk:back:stf")
        self.assertEqual(state, booking.STAFF)
        state, q = await self.press(booking.on_cancel, "bk:x")
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.chat.sent[-1][0], T.CANCELLED)
        self.assertIn("не створено", T.CANCELLED)  # leaving the flow is not "visit cancelled"
        self.assertEqual(self.backend.appointments, {})

    async def test_single_location_skips_picker(self):
        self.ctx.application.bot_data["locations"] = {LOC: LOCATIONS[LOC]}
        state = await booking.start_booking(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat)), self.ctx)
        self.assertEqual(state, booking.STAFF)
        self.assertNotIn("bk:back:loc", [b[1] for b in buttons(self.chat.sent[-1][1])])

    async def test_unavailable_without_client(self):
        self.ctx.application.bot_data["altegio"] = None
        state = await booking.start_booking(FakeUpdate(self.chat, USER, message=FakeMessage(self.chat)), self.ctx)
        self.assertEqual((state, self.chat.sent[-1][0]), (ConversationHandler.END, T.UNAVAILABLE))

    async def test_stale_buttons_expire(self):
        state, q = await self.press(booking.on_day, "bk:day:2030-01-07")
        self.assertEqual((state, q.answers), (ConversationHandler.END, [T.SESSION_EXPIRED]))

    async def test_reschedule_from_reminder(self):
        start = datetime(self.day.year, self.day.month, self.day.day, 11, 0, tzinfo=TZ)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, phone="+380671234567",
                                               services=[{"id": HAIRCUT, "cost": 800}])
        await self.links.remember_appointment(LOC, appt_id, USER)

        # Another chat cannot move it.
        q = FakeQuery("rm:mv:%d:%d" % (LOC, appt_id))
        state = await booking.start_reschedule(FakeUpdate(FakeChat(1), 1, query=q), self.ctx)
        self.assertEqual((state, q.answers), (None, [T.NOT_YOURS]))  # None: nothing starts or ends

        state, q = await self.press(booking.start_reschedule, "rm:mv:%d:%d" % (LOC, appt_id))
        self.assertEqual(state, booking.DAY)
        self.assertEqual(q.edits[-1], (None, None))  # reminder buttons removed
        self.assertEqual(self.chat.sent[-1][0], T.PICK_DAY)

        state, q = await self.press(booking.on_day, "bk:day:" + self.day.isoformat())
        self.assertEqual(state, booking.TIME)
        state, q = await self.press(booking.on_time, "bk:t:1700")
        self.assertEqual(state, booking.CONFIRM)
        self.assertIn("Ігор Халай", q.edits[-1][0])
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(len(self.backend.appointments), 1)  # moved, not duplicated
        self.assertTrue(self.backend.appointments[appt_id]["datetime"].startswith(self.day.isoformat() + "T17:00"))
        self.assertTrue(self.chat.sent[-1][0].startswith("Готово! Візит перенесено"))

    def test_handler_builds(self):
        h = booking.build_handler()
        self.assertEqual(h.name, "booking")


if __name__ == "__main__":
    unittest.main()


class ReviewFixBookingTests(_FlowBase):
    """Regressions for issues found in review (reuses the flow helpers above)."""

    async def test_all_evening_times_offered(self):
        await self.links.link(USER, "380671234567")
        self.ctx.user_data["bk"] = {"loc": LOC, "staff_id": 2106676, "staff_name": "Віктор Дяченко",
                                    "service_id": 10808287, "duration": 1800,  # 30-min "Стрижка машинкою"
                                    "day": (self.day + timedelta(days=1)).isoformat()}
        state, q = await self.press(booking.on_back, "bk:back:day")  # renders days
        state, q = await self.press(booking.on_day, "bk:day:" + (self.day + timedelta(days=1)).isoformat())
        times = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")]
        self.assertEqual(times[-1], "bk:t:1930")

    async def test_retry_after_created_returns_booked(self):
        await self.links.link(USER, "380671234567")
        await self.walk_to_time()
        await self.press(booking.on_time, "bk:t:1500")  # summary sets api_id
        api_id = self.ctx.user_data["bk"]["api_id"]
        start = datetime(self.day.year, self.day.month, self.day.day, 15, 0, tzinfo=TZ)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, phone="+380671234567")  # first try got through
        self.backend.appointments[appt_id]["api_id"] = api_id
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        self.assertTrue(self.chat.sent[-1][0].startswith("Готово! Ви записані"))
        self.assertEqual(len(self.backend.appointments), 1)

    async def test_bookkeeping_failure_still_confirms(self):
        await self.links.link(USER, "380671234567")
        await self.walk_to_time()
        await self.press(booking.on_time, "bk:t:1500")

        async def boom(*a, **k):
            raise RuntimeError("redis down")

        self.links.remember_appointment = boom
        state, q = await self.press(booking.on_confirm, "bk:ok")
        self.assertEqual(state, ConversationHandler.END)
        self.assertTrue(self.chat.sent[-1][0].startswith("Готово! Ви записані"))

    async def _start_move(self, hour=11):
        start = datetime(self.day.year, self.day.month, self.day.day, hour, 0, tzinfo=TZ)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, phone="+380671234567",
                                               services=[{"id": HAIRCUT, "cost": 800}])
        await self.links.remember_appointment(LOC, appt_id, USER)
        await self.press(booking.start_reschedule, "rm:mv:%d:%d" % (LOC, appt_id))
        return appt_id

    async def test_reschedule_offers_overlap_with_own_visit(self):
        await self._start_move(11)
        state, q = await self.press(booking.on_day, "bk:day:" + self.day.isoformat())
        times = [b[1] for b in buttons(q.edits[-1][1]) if b[1].startswith("bk:t:")]
        self.assertIn("bk:t:1130", times)
        self.assertIn("bk:t:1030", times)

    async def test_leaving_reschedule_says_visit_kept(self):
        await self._start_move(11)
        state, q = await self.press(booking.on_cancel, "bk:x")
        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("залишається", self.chat.sent[-1][0])
        self.assertIn("о 11:00", self.chat.sent[-1][0])

    async def test_failed_move_tap_keeps_running_booking(self):
        await self.walk_to_time()
        q = FakeQuery("rm:mv:%d:%d" % (LOC, 123))  # not linked to this chat
        state = await booking.start_reschedule(FakeUpdate(self.chat, USER, query=q), self.ctx)
        self.assertIsNone(state)
        self.assertIn("day", self.ctx.user_data["bk"])

    async def test_menu_button_leaves_booking(self):
        await self.walk_to_time()
        seen = []

        async def dispatch(update, context):
            seen.append(update.message.text)

        self.ctx.application.bot_data["menu_dispatch"] = dispatch
        msg = FakeMessage(self.chat, text=T.MENU_GAMES)
        state = await booking.on_menu_exit(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        self.assertEqual((state, seen), (ConversationHandler.END, [T.MENU_GAMES]))
        self.assertNotIn("bk", self.ctx.user_data)

    async def test_phone_step_does_not_swallow_menu(self):
        import re
        self.assertTrue(re.match(T.MENU_REGEX, T.MENU_GAMES))
        self.assertTrue(re.match(T.MENU_REGEX, "Play"))
        self.assertIsNone(re.match(T.MENU_REGEX, T.BOOK_BUTTON))

    async def test_book_from_prices(self):
        q = FakeQuery("bk:go:%d:%d" % (LOC, IGOR))
        state = await booking.start_with_staff(FakeUpdate(self.chat, USER, query=q), self.ctx)
        self.assertEqual(state, booking.SERVICE)
        self.assertIn("Ігор Халай", q.edits[-1][0])
