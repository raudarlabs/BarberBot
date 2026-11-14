"""
End-to-end: the Vercel webhook (api/webhook.process_update) driven update by update, exactly
like production — a fresh Application per update, conversation state only in the store
(in-memory stand-in here), Altegio on the recorded fixture (ALTEGIO_MOCK=1), and Telegram's
HTTP API replaced at the transport level so every bot call is recorded.

    python -m unittest tests.test_webhook_flow      (needs requirements.txt installed)
"""
import asyncio
import itertools
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))

os.environ.update({"ARCADE_DEV": "1", "ALTEGIO_MOCK": "1", "VOICE_BOOKING": "0",
                   "ARCADE_WEBAPP_URL": "https://barber-psi-ochre.vercel.app/"})
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")  # keep another suite's token if it set one
for k in ("DATABASE_URL", "POSTGRES_URL", "ARCADE_DATABASE_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL",
          "ALTEGIO_PARTNER_TOKEN", "ALTEGIO_USER_TOKEN"):
    os.environ.pop(k, None)

try:
    from telegram.request import HTTPXRequest
    import _kv
    import texts as T
    import webhook
except ImportError as e:  # bot dependencies not installed
    webhook = None
    IMPORT_ERROR = e

CALLS = []
_ids = itertools.count(1000)


async def fake_do_request(self, url, method, request_data=None, **kw):
    api = url.rsplit("/", 1)[-1]
    params = {}
    if request_data is not None:
        params = {k: json.loads(v) if isinstance(v, str) and v[:1] in "[{" else v
                  for k, v in (request_data.json_parameters or {}).items()}
    CALLS.append((api, params))
    if api == "getMe":
        result = {"id": 1, "is_bot": True, "first_name": "Bot", "username": "custom_true_bot"}
    elif api.startswith(("send", "edit")):
        chat = int(params.get("chat_id") or 42)
        result = {"message_id": next(_ids), "date": 0, "chat": {"id": chat, "type": "private"},
                  "text": params.get("text", "")}
    else:
        result = True
    return 200, json.dumps({"ok": True, "result": result}).encode()


USER = {"id": 42, "is_bot": False, "first_name": "Taras", "username": "taras"}
CHAT = {"id": 42, "type": "private"}
_uid = itertools.count(1)


def text_update(text):
    msg = {"message_id": next(_ids), "date": 0, "chat": CHAT, "from": USER, "text": text}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": next(_uid), "message": msg}


def contact_update(phone):
    return {"update_id": next(_uid), "message": {
        "message_id": next(_ids), "date": 0, "chat": CHAT, "from": USER,
        "contact": {"phone_number": phone, "first_name": "Taras", "user_id": 42}}}


def callback_update(data, message_id=1):
    return {"update_id": next(_uid), "callback_query": {
        "id": str(next(_ids)), "from": USER, "chat_instance": "x", "data": data,
        "message": {"message_id": message_id, "date": 0, "chat": CHAT, "from": {"id": 1, "is_bot": True, "first_name": "Bot"},
                    "text": "…"}}}


def run(body):
    CALLS.clear()
    asyncio.run(webhook.process_update(body))
    return list(CALLS)


def buttons(calls):
    """callback_data of the inline buttons in the last message the bot sent or edited."""
    for api, p in reversed(calls):
        markup = p.get("reply_markup")
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            return [b.get("callback_data") for row in markup["inline_keyboard"] for b in row if b.get("callback_data")]
    return []


def texts(calls):
    return [p.get("text", "") for api, p in calls if api.startswith(("send", "edit")) and p.get("text")]


@unittest.skipIf(webhook is None, "bot dependencies not installed")
class WebhookFlow(unittest.TestCase):
    def setUp(self):
        _kv._memory = None
        self._orig = HTTPXRequest.do_request
        HTTPXRequest.do_request = fake_do_request

    def tearDown(self):
        HTTPXRequest.do_request = self._orig

    def pick(self, calls, prefix):
        options = [b for b in buttons(calls) if b and b.startswith(prefix)]
        self.assertTrue(options, "no %s* button in %s" % (prefix, buttons(calls)))
        return options[0]

    def test_booking_survives_a_fresh_process_per_update(self):
        calls = run(text_update("/start"))
        self.assertIn("Custom True", texts(calls)[0])

        calls = run(text_update(T.BOOK_BUTTON))
        step = 0
        # location → staff → service → day → time, each a separate "process"
        for prefix in ("bk:loc:", "bk:stf:", "bk:svc:", "bk:day:", "bk:t:"):
            options = [b for b in buttons(calls) if b and b.startswith(prefix)]
            if not options and prefix == "bk:loc:":
                continue  # a single location skips this step
            calls = run(callback_update(self.pick(calls, prefix)))
            step += 1
        self.assertGreaterEqual(step, 4)
        # the bot now asks for the phone; the state must have come back from the store
        calls = run(contact_update("+380501112233"))
        self.assertIn("bk:ok", buttons(calls), texts(calls))
        calls = run(callback_update("bk:ok"))
        self.assertTrue(any("✅" in t or "запис" in t.lower() for t in texts(calls)), texts(calls))
        # conversation finished → nothing left in the store for this user
        self.assertEqual(_kv.pipeline(["HGETALL", "bb:ptb:conv:booking"])[0], [])

    def test_legacy_keyboard_labels_and_games_button(self):
        calls = run(text_update(T.LEGACY_GAMES_UK))
        markup = [p.get("reply_markup") for api, p in calls if p.get("reply_markup")][-1]
        webapps = [b for row in markup["inline_keyboard"] for b in row if "web_app" in b]
        self.assertTrue(webapps, markup)
        self.assertTrue(webapps[0]["web_app"]["url"].startswith("https://"))  # config may be loaded by another suite
        calls = run(text_update(T.LEGACY_STYLE_UK))
        self.assertTrue(texts(calls))

    def test_stale_conversation_is_dropped(self):
        run(text_update(T.BOOK_BUTTON))
        conv = _kv.pipeline(["HGETALL", "bb:ptb:conv:booking"])[0]
        self.assertTrue(conv)
        field, value = conv[0], json.loads(conv[1])
        value["t"] -= 3600
        _kv.pipeline(["HSET", "bb:ptb:conv:booking", field, json.dumps(value)])
        # a plain text an hour later must not be taken as a phone number / booking step
        calls = run(text_update("привіт"))
        self.assertTrue(texts(calls))
        self.assertNotIn("bk:ok", buttons(calls))


if __name__ == "__main__":
    unittest.main()
