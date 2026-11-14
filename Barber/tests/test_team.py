"""Team: invite links from /admin, /me for barbers, notifications about their clients (bot + Altegio changes)."""
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

import _kv  # noqa: E402
import team  # noqa: E402
import texts as T  # noqa: E402
from altegio_fixture import fixture_client  # noqa: E402
from fakes import FakeChat, FakeContext, FakeMessage, FakeQuery, FakeUpdate, buttons  # noqa: E402


def buttons_of(markup):
    return [b for row in markup.inline_keyboard for b in row]

TZ = ZoneInfo("Europe/Kyiv")
LOC, IGOR, VIKTOR = 297851, 875690, 2106676
LOCATIONS = {LOC: "вул. Володимира Великого, 75"}
ADMIN, BARBER, CLIENT = 1051263236, 777, 555


class Bot:
    username = "custom_true_bot"

    def __init__(self):
        self.sent, self.commands = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def set_my_commands(self, commands, scope=None, **kw):
        self.commands.append((scope.chat_id, [c.command for c in commands]))


class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _kv._memory = None
        self.client = fixture_client()
        self.bot = Bot()
        self.ctx = FakeContext({"altegio": self.client, "locations": LOCATIONS, "main_menu": "MENU"})
        self.ctx.bot = self.bot
        self.env = mock.patch.dict(os.environ, {"ADMIN_CHAT_IDS": str(ADMIN)})
        self.env.start()
        today = datetime.now(TZ).date()
        self.day = today + timedelta(days=1)
        self.t = lambda h, m=0, d=0: datetime(self.day.year, self.day.month, self.day.day, h, m, tzinfo=TZ) + timedelta(days=d)  # noqa: E731

    async def asyncTearDown(self):
        self.env.stop()
        await self.client.aclose()

    async def admin_press(self, data, chat=ADMIN):
        q = FakeQuery(data)
        await team.on_team_admin(FakeUpdate(FakeChat(chat), chat, query=q), self.ctx)
        return q

    async def link_igor(self):
        q = await self.admin_press("adm:tinv:%d" % IGOR)
        text = q.edits[-1][0]
        token = text.split("start=team_")[1].split()[0]
        chat = FakeChat(BARBER)
        self.ctx.args = ["team_" + token]
        await team.team_start(FakeUpdate(chat, BARBER, message=FakeMessage(chat, text="/start team_" + token)), self.ctx)
        return token, chat

    async def test_invite_link_is_one_time_and_links_the_barber(self):
        q = await self.admin_press("adm:team")
        self.assertIn("➕ Ігор Халай — не підключено", q.edits[-1][0])
        token, chat = await self.link_igor()
        self.assertIn("Вітаю, Ігор Халай", chat.sent[-1][0])
        self.assertEqual((await team.members())[IGOR]["chat"], BARBER)
        self.assertEqual(self.bot.commands[-1], (BARBER, ["start", "book", "admin"]))
        # the same link again: refused
        other = FakeChat(888)
        self.ctx.args = ["team_" + token]
        await team.team_start(FakeUpdate(other, 888, message=FakeMessage(other)), self.ctx)
        self.assertIn("недійсне", other.sent[-1][0])
        q = await self.admin_press("adm:team")
        self.assertIn("✅ Ігор Халай", q.edits[-1][0])
        self.assertIn(("🗑 Відключити: Ігор Халай", "adm:tdel:%d" % IGOR), buttons(q.edits[-1][1]))

    async def test_only_admins_manage_the_team(self):
        q = await self.admin_press("adm:tinv:%d" % IGOR, chat=CLIENT)
        self.assertEqual(q.edits, [])

    async def test_me_shows_only_the_barbers_own_clients(self):
        b = self.client.backend
        b.add_appointment(LOC, IGOR, self.t(11), phone="+380671234567", name="Тарас",
                          services=[{"id": 1, "title": "Чоловіча стрижка"}])
        b.add_appointment(LOC, VIKTOR, self.t(12), phone="+380501112233", name="Олена",
                          services=[{"id": 2, "title": "Стрижка машинкою"}])
        stranger = FakeChat(CLIENT)
        await team.me_command(FakeUpdate(stranger, CLIENT, message=FakeMessage(stranger)), self.ctx)
        self.assertEqual(stranger.sent[-1][0], T.FALLBACK)
        await self.link_igor()
        q = FakeQuery("tm:appts:1")
        await team.on_me_button(FakeUpdate(FakeChat(BARBER), BARBER, query=q), self.ctx)
        text = q.edits[-1][0]
        self.assertIn("Тарас", text)
        self.assertNotIn("Олена", text)
        q = FakeQuery("tm:free:1")
        await team.on_me_button(FakeUpdate(FakeChat(BARBER), BARBER, query=q), self.ctx)
        self.assertIn("💈 Ігор Халай", q.edits[-1][0])
        self.assertNotIn("Віктор", q.edits[-1][0])

    async def test_barber_admin_opens_his_own_panel(self):
        import admin
        await self.link_igor()
        chat = FakeChat(BARBER)
        await admin.admin_command(FakeUpdate(chat, BARBER, message=FakeMessage(chat)), self.ctx)
        self.assertIn("панель майстра", chat.sent[-1][0])
        datas = [b.callback_data for b in buttons_of(chat.sent[-1][1])]
        self.assertIn("tm:day", datas)
        self.assertFalse(any(d and d.startswith("adm:") for d in datas))
        b = self.client.backend
        today = datetime.now(TZ).date()
        while today.weekday() == 6:  # the fixture works Mon–Sat
            today += timedelta(days=1)
        now = datetime(today.year, today.month, today.day, 8, tzinfo=TZ)
        b.add_appointment(LOC, IGOR, now.replace(hour=11), phone="+380671234567", name="Тарас",
                          services=[{"id": 1, "title": "Чоловіча стрижка"}])
        b.add_appointment(LOC, VIKTOR, now.replace(hour=12), phone="+380501112233", name="Олена")
        text = await team.staff_day_text(self.client, LOCATIONS, IGOR, now, greet=True)
        self.assertIn("Доброго ранку", text)
        self.assertIn("11:00 · Тарас · Чоловіча стрижка", text)
        self.assertNotIn("Олена", text)
        self.assertIn("Вільні вікна", text)

    async def test_commands_refresh_once(self):
        await self.link_igor()
        self.bot.commands.clear()
        self.assertEqual(await team.refresh_commands(self.bot), 1)
        self.assertEqual(self.bot.commands, [(BARBER, ["start", "book", "admin"])])
        self.assertEqual(await team.refresh_commands(self.bot), 0)

    async def test_admin_can_disconnect(self):
        await self.link_igor()
        await self.admin_press("adm:tdel:%d" % IGOR)
        self.assertNotIn(IGOR, await team.members())
        self.assertIsNone(await team.staff_for_chat(BARBER))
        self.assertIn((BARBER, "Вас відключено від графіка Custom True."), self.bot.sent)

    async def test_notify_once_per_event(self):
        await self.link_igor()
        start = self.t(15)
        self.assertTrue(await team.notify_staff(self.bot, IGOR, "booked", 42, start, "Стрижка", "Тарас", "380671234567"))
        self.assertFalse(await team.notify_staff(self.bot, IGOR, "booked", 42, start, "Стрижка", "Тарас", "380671234567"))
        self.assertFalse(await team.notify_staff(self.bot, VIKTOR, "booked", 43, start))  # not linked
        chat, text = self.bot.sent[-1]
        self.assertEqual(chat, BARBER)
        self.assertIn("🆕 Новий запис до вас", text)
        self.assertIn("+380671234567", text)

    async def test_sync_detects_changes_from_any_source(self):
        await self.link_igor()
        b = self.client.backend
        now = datetime.now(TZ)
        kept = b.add_appointment(LOC, IGOR, self.t(10), phone="+380670000001", name="Постійний")
        to_cancel = b.add_appointment(LOC, IGOR, self.t(11), phone="+380670000002", name="Скасує")
        to_move = b.add_appointment(LOC, IGOR, self.t(12), phone="+380670000003", name="Перенесе")
        to_other = b.add_appointment(LOC, IGOR, self.t(13), phone="+380670000004", name="До Віктора")
        self.assertEqual(await team.sync_changes(self.client, LOCATIONS, self.bot, now), 0)  # first run: snapshot only
        self.bot.sent.clear()

        b.appointments[to_cancel]["deleted"] = True                               # cancelled by the admin
        b.appointments[to_move]["datetime"] = self.t(17).isoformat()              # moved in Altegio
        b.appointments[to_other]["staff_id"] = VIKTOR                             # handed to another barber
        new = b.add_appointment(LOC, IGOR, self.t(18), phone="+380670000005", name="Новий")  # booked by phone
        sent = await team.sync_changes(self.client, LOCATIONS, self.bot, now)
        texts = [t for c, t in self.bot.sent if c == BARBER]
        self.assertEqual(sent, 4, texts)
        self.assertTrue(any(t.startswith("❌") and "Скасує" in t for t in texts))
        self.assertTrue(any(t.startswith("🔁") and "Перенесе" in t and "17:00" in t for t in texts))
        self.assertTrue(any(t.startswith("❌") and "До Віктора" in t for t in texts))
        self.assertTrue(any(t.startswith("🆕") and "Новий" in t for t in texts))
        self.assertFalse(any("Постійний" in t for t in texts))

        # nothing changed → nothing sent; a visit already notified by the bot is not repeated
        self.bot.sent.clear()
        self.assertEqual(await team.sync_changes(self.client, LOCATIONS, self.bot, now), 0)
        start = self.t(19)
        via_bot = b.add_appointment(LOC, IGOR, start, phone="+380670000006", name="Через бот")
        await team.notify_staff(self.bot, IGOR, "booked", via_bot, start, "", "Через бот", "")
        self.assertEqual(await team.sync_changes(self.client, LOCATIONS, self.bot, now), 0)
        self.assertEqual(len(self.bot.sent), 1)
        del kept, new

    async def test_sync_ignores_visits_that_simply_passed(self):
        await self.link_igor()
        b = self.client.backend
        now = datetime.now(TZ)
        b.add_appointment(LOC, IGOR, now + timedelta(minutes=30), phone="+380670000007", name="Скоро")
        await team.sync_changes(self.client, LOCATIONS, self.bot, now)
        later = now + timedelta(hours=1)
        self.assertEqual(await team.sync_changes(self.client, LOCATIONS, self.bot, later), 0)


if __name__ == "__main__":
    unittest.main()
