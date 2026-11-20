"""Voice booking tests: intent validation, service matching, and the flow with a stub assistant."""
import os
import sys
import time
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram.ext import ConversationHandler  # noqa: E402

import booking  # noqa: E402
import texts as T  # noqa: E402
from altegio import Service, Staff  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, FakeVoice, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402
from voice import Intent, intent_from_json, make_assistant, match_service  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR, HAIRCUT = 297851, 875690, 2106676, 10808322
USER = 4242
TODAY = date(2030, 1, 7)
CATALOG = {LOC: [Staff(IGOR, "Ігор Халай"), Staff(VIKTOR, "Віктор Дяченко")], 708761: [Staff(2051616, "Ігор Халай")]}

SERVICES = [Service(1, "Чоловіча стрижка"), Service(2, "Стрижка машинкою"), Service(3, "Стрижка бороди"),
            Service(4, "Стрижка машинкою + борода"), Service(5, "Камуфлювання"), Service(6, "Комплекс"),
            Service(7, "Дитяча стрижка (до 10 років)")]


def next_monday():
    d = datetime.now(TZ).date() + timedelta(days=2)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


class IntentTests(unittest.TestCase):
    def test_valid(self):
        it = intent_from_json({"staff_id": IGOR, "service": "стрижка", "date": "2030-01-08", "time": "9:30"},
                              CATALOG, TODAY)
        self.assertEqual((it.location_id, it.staff_id, it.day, it.time), (LOC, IGOR, date(2030, 1, 8), "09:30"))

    def test_rejects_invented_ids_and_out_of_range(self):
        it = intent_from_json({"staff_id": 1, "location_id": 5, "date": "2029-12-31", "time": "25:00",
                               "time_from": "nope"}, CATALOG, TODAY)
        self.assertEqual((it.staff_id, it.location_id, it.day, it.time, it.time_from), (None,) * 5)
        far = intent_from_json({"date": "2030-02-01"}, CATALOG, TODAY)
        self.assertIsNone(far.day)

    def test_garbage(self):
        it = intent_from_json({"staff_id": "abc", "location_id": [], "cancel": 1}, CATALOG, TODAY)
        self.assertEqual((it.staff_id, it.location_id, it.cancel), (None, None, True))


class MatchServiceTests(unittest.TestCase):
    def title(self, q):
        s = match_service(q, SERVICES)
        return s.title if s else None

    def test_clear_matches(self):
        self.assertEqual(self.title("чоловіча стрижка"), "Чоловіча стрижка")
        self.assertEqual(self.title("стрижка бороди"), "Стрижка бороди")
        self.assertEqual(self.title("камуфляж"), "Камуфлювання")
        self.assertEqual(self.title("комплекс"), "Комплекс")
        self.assertEqual(self.title("дитяча"), "Дитяча стрижка (до 10 років)")

    def test_ambiguous_or_unknown(self):
        self.assertIsNone(self.title("стрижка"))
        self.assertIsNone(self.title("манікюр"))
        self.assertIsNone(self.title(""))


class StubAssistant:
    def __init__(self, text, intent):
        self.text, self.intent = text, intent
        self.calls = []

    async def transcribe(self, audio):
        self.calls.append(("stt", audio))
        return self.text

    async def extract(self, text, staff_by_location, locations, today, current=None):
        self.calls.append(("llm", text, sorted(staff_by_location), current))
        return self.intent


class _VoiceBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "main_menu": "MENU",
                                "locations": {LOC: "вул. Володимира Великого, 75", 708761: "вул. Гагаріна, 29/2"}})
        self.chat = FakeChat(USER)
        self.day = next_monday()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def say(self, intent, text="до Ігоря", duration=4):
        self.ctx.application.bot_data["voice"] = StubAssistant(text, intent)
        msg = FakeMessage(self.chat, voice=FakeVoice(duration))
        state = await booking.on_voice(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        return state, msg



class VoiceFlowTests(_VoiceBase):
    async def test_everything_in_one_phrase_goes_to_confirm(self):
        await self.links.link(USER, "380671234567")
        state, msg = await self.say(Intent(staff_id=IGOR, location_id=LOC, service="чоловіча стрижка",
                                           day=self.day, time="15:00"), text="до Ігоря в понеділок о третій")
        self.assertEqual(state, booking.CONFIRM)
        self.assertEqual(msg.replies[0][0], T.VOICE_HEARD.format(text="до Ігоря в понеділок о третій"))
        summary, markup = self.chat.sent[-1]
        self.assertIn("Ігор Халай", summary)
        self.assertIn("Чоловіча стрижка", summary)
        self.assertIn("о 15:00", summary)
        self.assertIn((T.CONFIRM_BUTTON, "bk:ok"), buttons(markup))
        self.assertEqual(self.client.backend.appointments, {})  # nothing booked until the button

        q = FakeQuery("bk:ok")
        state = await booking.on_confirm(FakeUpdate(self.chat, USER, query=q), self.ctx)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(len(self.client.backend.appointments), 1)

    async def test_unlinked_phone_still_asks_for_contact(self):
        state, _ = await self.say(Intent(staff_id=IGOR, location_id=LOC, service="чоловіча стрижка",
                                         day=self.day, time="15:00"))
        self.assertEqual((state, self.chat.sent[-1][0]), (booking.PHONE, T.ASK_PHONE))

    async def test_busy_time_shows_free_times(self):
        state, _ = await self.say(Intent(staff_id=IGOR, location_id=LOC, service="чоловіча стрижка",
                                         day=self.day, time="13:00"))  # fixture lunch block
        self.assertEqual(state, booking.TIME)
        text, markup = self.chat.sent[-1]
        self.assertTrue(text.startswith(T.VOICE_TIME_BUSY.format(time="13:00")))
        self.assertIn("bk:t:1200", [b[1] for b in buttons(markup)])

    async def test_partial_request_opens_missing_step(self):
        state, _ = await self.say(Intent(staff_id=IGOR, location_id=LOC, service="стрижка"))  # ambiguous
        self.assertEqual(state, booking.SERVICE)
        self.assertIn("Ігор Халай", self.chat.sent[-1][0])

        # A follow-up voice note keeps the barber and adds the rest.
        state, _ = await self.say(Intent(service="стрижка бороди", day=self.day, time_from="17:00"))
        self.assertEqual(state, booking.TIME)
        times = [b[1] for b in buttons(self.chat.sent[-1][1]) if b[1].startswith("bk:t:")]
        self.assertTrue(times and all(t >= "bk:t:1700" for t in times))
        self.assertEqual(self.ctx.user_data["bk"]["service_id"], 10808315)

    async def test_no_location_info_asks_location(self):
        state, _ = await self.say(Intent(day=self.day))
        self.assertEqual(state, booking.LOC)

    async def test_cancel_by_voice(self):
        state, _ = await self.say(Intent(cancel=True), text="ні, не треба")
        self.assertEqual((state, self.chat.sent[-1][0]), (ConversationHandler.END, T.CANCELLED))

    async def test_too_long_and_voice_off(self):
        state, msg = await self.say(Intent(), duration=120)
        self.assertEqual((state, msg.replies[-1][0]), (None, T.VOICE_TOO_LONG))
        self.ctx.application.bot_data["voice"] = None
        msg = FakeMessage(self.chat, voice=FakeVoice())
        state = await booking.on_voice(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        self.assertEqual((state, msg.replies[-1][0]), (None, T.VOICE_OFF))

    async def test_stale_state_is_dropped(self):
        self.ctx.user_data["bk"] = {"loc": LOC, "staff_id": VIKTOR, "duration": 3600, "ts": time.time() - 3600}
        state, _ = await self.say(Intent(staff_id=IGOR, location_id=LOC))
        self.assertEqual(state, booking.SERVICE)
        self.assertEqual(self.ctx.user_data["bk"]["staff_id"], IGOR)

    async def test_reschedule_by_voice_only_changes_time(self):
        start = datetime(self.day.year, self.day.month, self.day.day, 11, 0, tzinfo=TZ)
        appt_id = self.client.backend.add_appointment(LOC, IGOR, start, phone="+380671234567")
        await self.links.remember_appointment(LOC, appt_id, USER)
        await booking.start_reschedule(FakeUpdate(self.chat, USER, query=FakeQuery("rm:mv:%d:%d" % (LOC, appt_id))),
                                       self.ctx)
        state, _ = await self.say(Intent(staff_id=VIKTOR, location_id=LOC, day=self.day, time="17:00"))
        self.assertEqual(state, booking.CONFIRM)
        bk = self.ctx.user_data["bk"]
        self.assertEqual((bk["staff_id"], bk["time"]), (IGOR, "1700"))  # barber is not switched
        stub = self.ctx.application.bot_data["voice"]
        self.assertEqual(stub.calls[1][2], [LOC])  # no staff catalog fetched for a move


class MakeAssistantTests(unittest.TestCase):
    def test_modes(self):
        self.assertIsNone(make_assistant({}))
        self.assertIsNone(make_assistant({"OPENAI_API_KEY": "k", "VOICE_BOOKING": "0"}))


if __name__ == "__main__":
    unittest.main()


class PickingAssistant(StubAssistant):
    def __init__(self, text, intent, pick):
        super().__init__(text, intent)
        self.pick = pick

    async def pick_service(self, said, services):
        self.calls.append(("pick", said, [s.id for s in services]))
        return self.pick


class VoiceServicePickTests(_VoiceBase):
    async def test_assistant_picks_combined_service(self):
        stub = PickingAssistant("стрижка і борода до Ігоря", Intent(staff_id=IGOR, location_id=LOC,
                                                                     service="стрижка і борода"), 10808313)
        self.ctx.application.bot_data["voice"] = stub
        msg = FakeMessage(self.chat, voice=FakeVoice())
        state = await booking.on_voice(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        self.assertEqual(state, booking.DAY)
        self.assertEqual(self.ctx.user_data["bk"]["service_id"], 10808313)  # Комплекс
        self.assertEqual(stub.calls[-1][1], "стрижка і борода до Ігоря")

    async def test_cancel_visit_is_not_faked(self):
        self.ctx.application.bot_data["shop"] = {"phone_display": "+380 98 033 77 33"}
        state, _ = await self.say(Intent(cancel_visit=True), text="скасуйте мій запис")
        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("+380 98 033 77 33", self.chat.sent[-1][0])
        self.assertNotIn("скасовано", self.chat.sent[-1][0].lower())

    async def test_voice_at_phone_step_asks_for_button(self):
        self.ctx.user_data["bk"] = {"loc": LOC, "staff_id": IGOR, "duration": 3600, "day": self.day.isoformat(),
                                    "time": "1500", "ts": time.time()}
        state, msg = await self.say(Intent(time="16:00"))
        self.assertEqual((state, msg.replies[-1][0]), (None, T.PHONE_BY_BUTTON))
