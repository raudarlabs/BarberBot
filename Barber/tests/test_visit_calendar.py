""""📅 Додати в календар": .ics file, Google link, who may get it, where the button shows."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["ARCADE_DEV"] = "1"
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
    os.environ.pop(k, None)

import texts as T  # noqa: E402
import visit_calendar as VC  # noqa: E402
from altegio import Appointment  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from config import SHOP  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, CHAT = 297851, 875690, 555
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}


def appt(**kw):
    base = dict(id=42, location_id=LOC, staff_id=IGOR, start=datetime(2030, 7, 15, 9, 30, tzinfo=TZ), duration=3600,
                services=[{"id": 1, "title": "Чоловіча стрижка, укладка"}])
    base.update(kw)
    return Appointment(**base)


class IcsTests(unittest.TestCase):
    def test_event(self):
        raw = VC.build_ics(appt(), "Ігор Халай", "Custom True Barbershop, Черкаси, вул. Володимира Великого, 75",
                           now=datetime(2030, 7, 1, tzinfo=timezone.utc))
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))  # CRLF only
        unfolded = text.replace("\r\n ", "")
        self.assertIn("DTSTART:20300715T063000Z", unfolded)  # 09:30 Kyiv summer time = 06:30 UTC
        self.assertIn("DTEND:20300715T073000Z", unfolded)
        self.assertIn("SUMMARY:Custom True — Чоловіча стрижка\\, укладка", unfolded)  # comma escaped
        self.assertIn("LOCATION:Custom True Barbershop\\, Черкаси\\, вул. Володимира Великого\\, 75", unfolded)
        self.assertIn("UID:altegio-297851-42@custom-true-bot", unfolded)
        self.assertIn("TRIGGER:-PT120M", unfolded)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n")))

    def test_google_link(self):
        url = VC.google_url(appt(), "Ігор Халай", "Черкаси")
        q = parse_qs(urlparse(url).query)
        self.assertEqual(urlparse(url).netloc, "calendar.google.com")
        self.assertEqual(q["dates"], ["20300715T063000Z/20300715T073000Z"])
        self.assertEqual(q["location"], ["Черкаси"])
        self.assertIn("Ігор Халай", q["details"][0])

    def test_location_text(self):
        bd = {"shop": SHOP, "locations": LOCATIONS}
        self.assertEqual(VC.location_text(bd, LOC), "Custom True Barbershop, " + SHOP["address"])
        self.assertEqual(VC.location_text({"locations": {1: "вул. X, 1"}}, 1), "Custom True Barbershop, вул. X, 1")


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "locations": LOCATIONS,
                                "shop": SHOP, "main_menu": "MENU"})
        self.chat = FakeChat(CHAT)
        self.start = datetime.now(TZ).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
        self.appt = self.client.backend.add_appointment(LOC, IGOR, self.start, phone="+380671234567",
                                                        services=[{"id": 10808322, "title": "Чоловіча стрижка"}])

    async def asyncTearDown(self):
        await self.client.aclose()

    async def press(self, chat):
        q = FakeQuery("ics:%d:%d" % (LOC, self.appt))
        await VC.on_ics(FakeUpdate(chat, chat.id, query=q), self.ctx)
        return q

    async def test_linked_chat_gets_file_and_google_link(self):
        await self.links.remember_appointment(LOC, self.appt, CHAT)
        await self.press(self.chat)
        doc, caption, markup = self.chat.documents[-1]
        body = doc.input_file_content.decode("utf-8")
        self.assertIn("SUMMARY:Custom True — Чоловіча стрижка", body)
        self.assertTrue(doc.filename.endswith(".ics"))
        self.assertIn(T.when(self.start), caption)
        self.assertTrue(markup.inline_keyboard[0][0].url.startswith("https://calendar.google.com/"))

    async def test_other_chat_and_cancelled_visit(self):
        q = await self.press(FakeChat(1))
        self.assertEqual(q.answers, [T.NOT_YOURS])
        await self.links.remember_appointment(LOC, self.appt, CHAT)
        self.client.backend.appointments[self.appt]["deleted"] = True
        q = await self.press(self.chat)
        self.assertEqual(q.answers, [T.CANT_CHANGE])
        self.assertFalse(getattr(self.chat, "documents", []))

    async def test_button_in_my_visits(self):
        import menu
        await self.links.link(CHAT, "380671234567")
        await menu.show_visits(FakeUpdate(self.chat, CHAT, message=FakeMessage(self.chat, text=T.MENU_VISITS)), self.ctx)
        self.assertIn((T.ICS_BUTTON, "ics:%d:%d" % (LOC, self.appt)), buttons(self.chat.sent[-1][1]))


if __name__ == "__main__":
    unittest.main()
