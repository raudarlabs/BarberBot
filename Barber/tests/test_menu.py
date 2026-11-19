"""Main menu tests: keyboard, games links, prices, contacts, "Мій запис"."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Contact  # noqa: E402

import menu  # noqa: E402
import texts as T  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from config import SHOP  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402
from linkstore import LinkStore  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR = 297851, 875690
USER = 4242
ARCADE = "https://barber-psi-ochre.vercel.app/"


class StaticTests(unittest.TestCase):
    def test_keyboard_layout(self):
        kb = menu.main_menu_keyboard()
        labels = [[b.text for b in row] for row in kb.keyboard]
        self.assertEqual(labels, [[T.BOOK_BUTTON, T.MENU_REPEAT], [T.MENU_VISITS, T.MENU_STYLE],
                                  [T.MENU_GAMES, T.MENU_PRICES], [T.MENU_CONTACTS]])
        self.assertTrue(all(b.web_app is None for row in kb.keyboard for b in row))  # arcade only inline
        self.assertTrue(kb.is_persistent)

    def test_game_links(self):
        self.assertEqual(menu.game_url(ARCADE, "snake"), ARCADE + "?game=snake")
        self.assertEqual(menu.game_url("https://x.app/play?v=2", "fade"), "https://x.app/play?v=2&game=fade")
        self.assertEqual(menu.game_url("https://x.app", "beard"), "https://x.app/?game=beard")
        markup = menu.games_markup(ARCADE)
        urls = [b.web_app.url for row in markup.inline_keyboard for b in row]
        self.assertEqual(urls[0], ARCADE)
        self.assertEqual(urls[1:], [ARCADE + "?game=" + g for g, _, _ in menu.GAMES])

    def test_chat_menu_button(self):
        booking_url = "https://n308628.alteg.io/company/297851/personal/menu?o="
        b = menu.chat_menu_button("booking", ARCADE, booking_url, "Записатись")
        self.assertEqual((b.text, b.web_app.url), ("Записатись", booking_url))
        a = menu.chat_menu_button("arcade", ARCADE, booking_url, "Записатись")
        self.assertEqual((a.text, a.web_app.url), (T.MENU_GAMES, ARCADE))
        self.assertIsNone(menu.chat_menu_button("none", ARCADE, booking_url, "x"))
        self.assertIsNone(menu.chat_menu_button("booking", ARCADE, "", "x"))

    def test_game_ids_match_arcade(self):
        games_dir = os.path.join(ROOT, "public", "arcade", "games")
        if os.path.isdir(games_dir):
            files = {f[:-3] for f in os.listdir(games_dir) if f.endswith(".js")}
            self.assertEqual({g for g, _, _ in menu.GAMES}, files)


class MenuScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = fixture_client()
        self.links = LinkStore(memory=True)
        self.ctx = FakeContext({"altegio": self.client, "links": self.links, "main_menu": "MENU",
                                "locations": {LOC: "вул. Володимира Великого, 75"},
                                "arcade_url": ARCADE, "shop": SHOP})
        self.chat = FakeChat(USER)

    async def asyncTearDown(self):
        await self.client.aclose()

    def text_update(self, text):
        return FakeUpdate(self.chat, USER, message=FakeMessage(self.chat, text=text))

    async def test_dispatch_routes_legacy_buttons(self):
        await menu.dispatch(self.text_update("Play"), self.ctx)
        self.assertEqual(self.chat.sent[-2][0], T.GAMES_TEXT)
        self.assertEqual(self.chat.sent[-1][0], T.MENU_UPDATED)  # replaces the old keyboard
        await menu.dispatch(self.text_update(T.LEGACY_GAMES_UK), self.ctx)
        self.assertEqual(self.chat.sent[-1][0], T.MENU_UPDATED)
        await menu.dispatch(self.text_update(T.MENU_GAMES), self.ctx)
        self.assertEqual(self.chat.sent[-1][0], T.GAMES_TEXT)  # the new button: no extra message
        await menu.dispatch(self.text_update("Choose Hair Style"), self.ctx)
        self.assertEqual(self.chat.sent[-1][0], T.STYLE_PROMPT)

    async def test_contacts(self):
        await menu.dispatch(self.text_update(T.MENU_CONTACTS), self.ctx)
        text, markup = self.chat.sent[-1]
        self.assertIn(SHOP["phone_display"], text)
        self.assertIn(SHOP["address"], text)
        self.assertEqual(markup.inline_keyboard[0][0].url, SHOP["maps"])

    async def test_prices_then_book(self):
        await menu.dispatch(self.text_update(T.MENU_PRICES), self.ctx)
        self.assertIn(("💈 Ігор Халай", "pr:stf:%d:%d" % (LOC, IGOR)), buttons(self.chat.sent[-1][1]))
        q = FakeQuery("pr:stf:%d:%d" % (LOC, IGOR))
        await menu.on_prices_button(FakeUpdate(self.chat, USER, query=q), self.ctx)
        text, markup = q.edits[-1]
        self.assertIn("• Чоловіча стрижка — 800 ₴", text)
        self.assertIn("• Камуфлювання — 300–500 ₴", text)
        self.assertEqual(buttons(markup)[0], ("📅 Записатися до Ігор", "bk:go:%d:%d" % (LOC, IGOR)))

    async def test_prices_unavailable_without_altegio(self):
        self.ctx.application.bot_data["altegio"] = None
        await menu.dispatch(self.text_update(T.MENU_PRICES), self.ctx)
        self.assertIn(SHOP["phone_display"], self.chat.sent[-1][0])

    async def test_visits_ask_phone_then_list(self):
        start = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(days=2)
        appt_id = self.client.backend.add_appointment(
            LOC, IGOR, start, phone="+380671234567", services=[{"id": 10808322, "title": "Чоловіча стрижка"}])
        self.client.backend.add_appointment(LOC, IGOR, start + timedelta(hours=2), phone="+380500000000")

        await menu.dispatch(self.text_update(T.MENU_VISITS), self.ctx)
        text, kb = self.chat.sent[-1]
        self.assertEqual(text, T.VISITS_ASK_PHONE)
        self.assertTrue(kb.keyboard[0][0].request_contact)

        msg = FakeMessage(self.chat, contact=Contact("+380671234567", "Тарас", user_id=USER))
        await menu.on_contact_link(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        visit_text, visit_kb = self.chat.sent[-1]
        self.assertIn("Ігор Халай · Чоловіча стрижка", visit_text)
        self.assertEqual([b[1] for b in buttons(visit_kb)],
                         ["rm:ok:%d:%d" % (LOC, appt_id), "rm:mv:%d:%d" % (LOC, appt_id), "rm:cx:%d:%d" % (LOC, appt_id)])
        self.assertEqual(await self.links.chat_for_appointment(LOC, appt_id), USER)  # buttons work here
        self.assertEqual(sum(1 for t, _ in self.chat.sent if t.startswith("💈")), 1)  # not the other client's

        # Second time: the learned client id narrows the query server-side.
        await menu.dispatch(self.text_update(T.MENU_VISITS), self.ctx)
        params = [r[2] for r in self.client.backend.requests if r[1].endswith("/appointments")][-1]
        self.assertIn("client_id", params)

    async def test_no_visits_offers_booking(self):
        await self.links.link(USER, "380671234567")
        await menu.dispatch(self.text_update(T.MENU_VISITS), self.ctx)
        text, markup = self.chat.sent[-1]
        self.assertEqual((text, buttons(markup)), (T.VISITS_NONE, [(T.VISITS_BOOK, "bk:new")]))

    async def test_foreign_contact_rejected(self):
        msg = FakeMessage(self.chat, contact=Contact("+380500000000", "X", user_id=1))
        await menu.on_contact_link(FakeUpdate(self.chat, USER, message=msg), self.ctx)
        self.assertEqual(msg.replies[-1][0], T.NOT_YOUR_CONTACT)
        self.assertIsNone(await self.links.phone_for_chat(USER))


class ArcadeUrlTests(unittest.TestCase):
    """Bug seen in production: every game button opened Snake (base URL was …/snake_app.html)."""

    def test_old_single_game_pages_map_to_the_hub(self):
        from config import arcade_webapp_url
        site = "https://barber-psi-ochre.vercel.app"
        self.assertEqual(arcade_webapp_url({"SNAKE_WEBAPP_URL": site + "/snake_app.html"}), site + "/")
        self.assertEqual(arcade_webapp_url({"GAMES_WEBAPP_URL": site + "/", "SNAKE_WEBAPP_URL": site + "/snake_app.html"}),
                         site + "/")
        self.assertEqual(arcade_webapp_url({"ARCADE_WEBAPP_URL": site + "/tetris_app.html"}), site + "/")
        self.assertEqual(arcade_webapp_url({"ARCADE_WEBAPP_URL": site + "/hub/"}), site + "/hub/")

    def test_each_game_button_opens_its_own_game(self):
        from config import arcade_webapp_url
        base = arcade_webapp_url({"SNAKE_WEBAPP_URL": "https://barber-psi-ochre.vercel.app/snake_app.html"})
        urls = {b.text: b.web_app.url for row in menu.games_markup(base).inline_keyboard for b in row}
        self.assertEqual(urls["🧩 Укладка"], "https://barber-psi-ochre.vercel.app/?game=blocks")
        self.assertEqual(urls["💨 Фен-пілот"], "https://barber-psi-ochre.vercel.app/?game=dryer")
        self.assertEqual(urls[T.GAMES_HUB_BUTTON], "https://barber-psi-ochre.vercel.app/")


if __name__ == "__main__":
    unittest.main()
