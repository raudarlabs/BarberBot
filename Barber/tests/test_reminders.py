"""Reminder job tests: windows, dedupe, chat lookup, confirm button. Fixture + in-memory Redis."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NOTIFY_CHAT_IDS"] = "admins"  # notifications follow ADMIN_CHAT_IDS in these tests

import reminders  # noqa: E402
import texts as T  # noqa: E402
from altegio import Appointment  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR = 297851, 875690
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}
NOW = datetime(2030, 1, 7, 9, 0, tzinfo=TZ)  # Monday


class DueKindTests(unittest.TestCase):
    def appt(self, minutes, status="waiting"):
        return Appointment(id=1, location_id=LOC, staff_id=IGOR, start=NOW + timedelta(minutes=minutes), status=status)

    def test_windows(self):
        self.assertEqual(reminders.due_kind(self.appt(24 * 60), NOW), "24h")
        self.assertEqual(reminders.due_kind(self.appt(4 * 60), NOW), "24h")
        self.assertIsNone(reminders.due_kind(self.appt(24 * 60 + 5), NOW))
        self.assertIsNone(reminders.due_kind(self.appt(150), NOW))  # between windows
        self.assertEqual(reminders.due_kind(self.appt(120), NOW), "2h")
        self.assertEqual(reminders.due_kind(self.appt(10), NOW), "2h")
        self.assertIsNone(reminders.due_kind(self.appt(-5), NOW))

    def test_inactive_skipped(self):
        for status in ("cancelled", "arrived", "no_show"):
            self.assertIsNone(reminders.due_kind(self.appt(60, status), NOW))
        self.assertEqual(reminders.due_kind(self.appt(60, "confirmed"), NOW), "2h")


class Outbox:
    def __init__(self):
        self.messages = []

    async def __call__(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


class RunRemindersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        self.out = Outbox()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def run_at(self, now):
        return await reminders.run_reminders(self.client, self.links, LOCATIONS, self.out, now=now)

    async def test_phone_linked_client_gets_24h_then_2h_once_each(self):
        start = NOW + timedelta(hours=23)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, phone="+380671234567", name="Олена",
                                               services=[{"id": 10808322, "title": "Чоловіча стрижка"}])
        await self.links.link(555, "380671234567")

        self.assertEqual(await self.run_at(NOW), 1)
        chat, text, markup = self.out.messages[-1]
        self.assertEqual(chat, 555)
        self.assertIn("завтра о 08:00", text)
        self.assertIn("Ігор Халай", text)
        self.assertIn("Чоловіча стрижка", text)
        self.assertEqual(buttons(markup), [(T.REMINDER_CONFIRM, "rm:ok:%d:%d" % (LOC, appt_id)),
                                           (T.REMINDER_MOVE, "rm:mv:%d:%d" % (LOC, appt_id)),
                                           (T.REMINDER_CANCEL, "rm:cx:%d:%d" % (LOC, appt_id))])

        self.assertEqual(await self.run_at(NOW + timedelta(minutes=10)), 0)  # deduped
        self.assertEqual(await self.run_at(start - timedelta(minutes=110)), 1)
        self.assertIn("Через 2 години (08:00)", self.out.messages[-1][1])
        self.assertEqual(await self.run_at(start - timedelta(minutes=100)), 0)
        # The appointment is now tied to the chat, so its buttons work there.
        self.assertEqual(await self.links.chat_for_appointment(LOC, appt_id), 555)

    async def test_unknown_client_gets_nothing_and_card_lookup_is_cached(self):
        start = NOW + timedelta(hours=5)
        self.backend.add_appointment(LOC, IGOR, start, phone="+380509998877", name="Хтось")
        # Simulate Altegio not including the client in the list response.
        original = self.backend._list_appointments

        def without_included(params, body, loc):
            params = {**params, "include": ""}
            return original(params, body, loc)

        self.backend._list_appointments = without_included
        self.assertEqual(await self.run_at(NOW), 0)
        self.assertEqual(await self.run_at(NOW + timedelta(minutes=10)), 0)
        card_calls = [r for r in self.backend.requests if r[1].startswith("/client/")]
        self.assertEqual(len(card_calls), 1)

        # Once that phone is linked, the cached card phone finds the chat.
        await self.links.link(777, "380509998877")
        self.assertEqual(await self.run_at(NOW + timedelta(minutes=20)), 1)
        self.assertEqual(self.out.messages[-1][0], 777)

    async def test_booked_in_bot_found_without_phone(self):
        start = NOW + timedelta(hours=1)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, client_id=0)
        await self.links.remember_appointment(LOC, appt_id, 999)
        self.assertEqual(await self.run_at(NOW), 1)
        self.assertEqual(self.out.messages[-1][0], 999)

    async def test_confirmed_has_no_confirm_button_and_cancelled_is_skipped(self):
        a = self.backend.add_appointment(LOC, IGOR, NOW + timedelta(hours=1), phone="+380671234567", attendance=2)
        self.backend.add_appointment(LOC, IGOR, NOW + timedelta(hours=1, minutes=30), phone="+380671234567", deleted=True)
        await self.links.link(555, "380671234567")
        self.assertEqual(await self.run_at(NOW), 1)
        self.assertEqual(buttons(self.out.messages[-1][2]), [(T.REMINDER_MOVE, "rm:mv:%d:%d" % (LOC, a)),
                                                             (T.REMINDER_CANCEL, "rm:cx:%d:%d" % (LOC, a))])

    async def test_moved_visit_is_reminded_again(self):
        start = NOW + timedelta(hours=1)
        appt_id = self.backend.add_appointment(LOC, IGOR, start, phone="+380671234567")
        await self.links.link(555, "380671234567")
        self.assertEqual(await self.run_at(NOW), 1)
        self.backend.appointments[appt_id]["datetime"] = (start + timedelta(minutes=30)).isoformat()
        self.assertEqual(await self.run_at(NOW + timedelta(minutes=10)), 1)

    async def test_opt_out(self):
        self.backend.add_appointment(LOC, IGOR, NOW + timedelta(hours=1), phone="+380671234567")
        await self.links.link(555, "380671234567")
        self.assertTrue(await self.links.unlink(555))
        self.assertEqual(await self.run_at(NOW), 0)

    async def test_api_failure_is_contained(self):
        self.backend.fail_next = (503, "maintenance")
        self.assertEqual(await self.run_at(NOW), 0)


class ConfirmButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS})
        self.appt_id = self.client.backend.add_appointment(LOC, IGOR, NOW + timedelta(days=1), phone="+380671234567")
        await self.links.remember_appointment(LOC, self.appt_id, 555)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_confirm(self):
        q = FakeQuery("rm:ok:%d:%d" % (LOC, self.appt_id), message_text="Нагадуємо: ...")
        await reminders.on_confirm_visit(FakeUpdate(FakeChat(555), 555, query=q), self.ctx)
        self.assertEqual(self.client.backend.appointments[self.appt_id]["attendance"], 2)
        text, markup = q.edits[-1]
        self.assertTrue(text.endswith(T.CONFIRMED))
        self.assertEqual([b[0] for b in buttons(markup)], [T.REMINDER_MOVE, T.REMINDER_CANCEL])

    async def test_other_chat_cannot_confirm(self):
        q = FakeQuery("rm:ok:%d:%d" % (LOC, self.appt_id))
        await reminders.on_confirm_visit(FakeUpdate(FakeChat(666), 666, query=q), self.ctx)
        self.assertEqual(self.client.backend.appointments[self.appt_id]["attendance"], 0)
        self.assertEqual(q.answers, [T.NOT_YOURS])


class CancelButtonTests(unittest.IsolatedAsyncioTestCase):
    """✖️ Скасувати in "Мій запис" / reminders: ask first, own visits only, not too close to the visit."""

    async def asyncSetUp(self):
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS,
                                "shop": {"phone_display": "+380 98 033 77 33"}})
        self.start = (datetime.now(self.client.tz) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        self.appt_id = self.client.backend.add_appointment(LOC, IGOR, self.start, phone="+380671234567")
        await self.links.remember_appointment(LOC, self.appt_id, 555)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press(self, action, chat=555, appt_id=None, text="💈 Ігор · завтра 12:00"):
        q = FakeQuery("rm:%s:%d:%d" % (action, LOC, appt_id or self.appt_id), message_text=text)
        await reminders.on_cancel_visit(FakeUpdate(FakeChat(chat), chat, query=q), self.ctx)
        return q

    def deleted(self, appt_id=None):
        return self.client.backend.appointments[appt_id or self.appt_id]["deleted"]

    async def test_visit_buttons_have_cancel(self):
        appt = await self.client.get_appointment(LOC, self.appt_id)
        self.assertIn((T.REMINDER_CANCEL, "rm:cx:%d:%d" % (LOC, self.appt_id)), buttons(reminders.move_confirm_buttons(appt)))

    async def test_ask_then_cancel(self):
        q = await self.press("cx")
        text, markup = q.edits[-1]
        self.assertTrue(text.endswith(T.CANCEL_ASK))
        self.assertEqual([b[1] for b in buttons(markup)],
                         ["rm:cxy:%d:%d" % (LOC, self.appt_id), "rm:cxn:%d:%d" % (LOC, self.appt_id)])
        self.assertFalse(self.deleted())  # asking does not cancel
        q = await self.press("cxy", text=text)
        self.assertTrue(self.deleted())
        text, markup = q.edits[-1]
        self.assertTrue(text.endswith(T.VISIT_CANCELLED))
        self.assertNotIn(T.CANCEL_ASK, text)
        self.assertIsNone(markup)
        self.assertIn("DELETE", [r[0] for r in self.client.backend.requests])
        self.assertEqual(self.client.backend.requests[-1][0], "DELETE")  # no extra lookups without admins

    async def test_no_keeps_visit_and_restores_buttons(self):
        q = await self.press("cx")
        q = await self.press("cxn", text=q.edits[-1][0])
        self.assertFalse(self.deleted())
        text, markup = q.edits[-1]
        self.assertNotIn(T.CANCEL_ASK, text)
        self.assertIn("rm:cx:%d:%d" % (LOC, self.appt_id), [b[1] for b in buttons(markup)])

    async def test_admins_are_notified_of_a_cancellation(self):
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, **kw):
                sent.append((chat_id, text))
        self.ctx.bot = Bot()
        os.environ["ADMIN_CHAT_IDS"] = "111"
        try:
            await self.press("cxy")
        finally:
            os.environ.pop("ADMIN_CHAT_IDS", None)
        self.assertTrue(self.deleted())
        self.assertEqual(len(sent), 1)
        self.assertIn("Скасування", sent[0][1])
        self.assertIn("+380671234567", sent[0][1])

    async def test_other_chat_cannot_cancel(self):
        q = await self.press("cxy", chat=666)
        self.assertFalse(self.deleted())
        self.assertEqual(q.answers, [T.NOT_YOURS])

    async def test_too_close_to_the_visit_asks_to_call(self):
        soon = datetime.now(self.client.tz) + timedelta(minutes=90)
        appt_id = self.client.backend.add_appointment(LOC, IGOR, soon, phone="+380671234567")
        await self.links.remember_appointment(LOC, appt_id, 555)
        q = await self.press("cxy", appt_id=appt_id)
        self.assertFalse(self.deleted(appt_id))
        self.assertIn("+380 98 033 77 33", q.answers[-1])

    async def test_already_cancelled_visit(self):
        self.client.backend.appointments[self.appt_id]["deleted"] = True
        q = await self.press("cxy")
        self.assertEqual(q.answers[-1], T.CANT_CHANGE)


if __name__ == "__main__":
    unittest.main()


class ReviewFixReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.backend = self.client.backend
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press_confirm(self, appt_id, chat=555):
        await self.links.remember_appointment(LOC, appt_id, chat)
        q = FakeQuery("rm:ok:%d:%d" % (LOC, appt_id), message_text="Нагадуємо")
        await reminders.on_confirm_visit(FakeUpdate(FakeChat(chat), chat, query=q), self.ctx)
        return q

    async def test_confirm_never_overwrites_final_status(self):
        future = datetime.now(TZ) + timedelta(days=1)
        for attendance, deleted in ((-1, False), (1, False), (0, True)):
            a = self.backend.add_appointment(LOC, IGOR, future, attendance=attendance, deleted=deleted)
            q = await self.press_confirm(a)
            self.assertEqual(self.backend.appointments[a]["attendance"], attendance)
            self.assertIn(q.answers[-1], (T.CANT_CHANGE,))
            future += timedelta(hours=2)

    async def test_confirm_past_visit_refused_and_confirmed_is_noop(self):
        past = self.backend.add_appointment(LOC, IGOR, datetime.now(TZ) - timedelta(hours=1))
        q = await self.press_confirm(past)
        self.assertEqual((self.backend.appointments[past]["attendance"], q.answers[-1]), (0, T.CANT_CHANGE))
        done = self.backend.add_appointment(LOC, IGOR, datetime.now(TZ) + timedelta(days=2), attendance=2)
        before = len(self.backend.requests)
        q = await self.press_confirm(done)
        self.assertEqual(q.answers[-1], T.CONFIRMED)
        self.assertFalse([r for r in self.backend.requests[before:] if r[1].endswith("/attendance")])

    async def test_network_error_retries_next_pass_and_others_still_go(self):
        from telegram.error import TimedOut
        a = self.backend.add_appointment(LOC, IGOR, NOW + timedelta(hours=1), phone="+380671234567")
        self.backend.add_appointment(LOC, IGOR, NOW + timedelta(hours=1, minutes=30), phone="+380509998877")
        await self.links.link(555, "380671234567")
        await self.links.link(777, "380509998877")
        delivered = []

        async def flaky(chat_id, text, reply_markup=None):
            if chat_id == 555 and not delivered:
                delivered.append("fail")
                raise TimedOut()
            delivered.append(chat_id)

        n = await reminders.run_reminders(self.client, self.links, LOCATIONS, flaky, now=NOW)
        self.assertEqual((n, delivered), (1, ["fail", 777]))
        self.assertFalse(await self.links.was_sent(a, "2h:%d" % int((NOW + timedelta(hours=1)).timestamp())))
        n = await reminders.run_reminders(self.client, self.links, LOCATIONS, flaky, now=NOW + timedelta(minutes=10))
        self.assertEqual((n, delivered[-1]), (1, 555))


class LinkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_relink_and_unlink_do_not_touch_other_chats(self):
        links = LinkStore(memory=True)
        await links.link(111, "380671234567")
        await links.link(222, "380671234567")  # same number moves to another chat
        self.assertEqual(await links.chat_for_phone("380671234567"), 222)
        self.assertIsNone(await links.phone_for_chat(111))
        await links.link(333, "380500000000")
        await links.link(333, "380509999999")  # 333 changes its number
        self.assertIsNone(await links.chat_for_phone("380500000000"))
        self.assertTrue(await links.unlink(222))
        self.assertIsNone(await links.chat_for_phone("380671234567"))
        self.assertEqual(await links.chat_for_phone("380509999999"), 333)
        await links.link(444, "380631112233")
        await links.link(555, "380631112233")
        self.assertFalse(await links.unlink(444))  # 444 lost the number already
        self.assertEqual(await links.chat_for_phone("380631112233"), 555)
