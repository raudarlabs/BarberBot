"""Minimal stand-ins for python-telegram-bot objects, enough to drive the handlers."""
from types import SimpleNamespace


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.sent = []  # (text, reply_markup)

    async def send_message(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(text=text)

    async def send_action(self, action, **kw):
        pass


class FakeQuery:
    def __init__(self, data, message_text=""):
        self.data = data
        self.answers = []
        self.edits = []  # (text, reply_markup); text None = markup-only edit
        self.message = SimpleNamespace(text=message_text)

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.edits.append((None, reply_markup))


class FakeVoice:
    def __init__(self, duration=4, data=b"OggS-fake"):
        self.duration = duration
        self._data = data

    async def get_file(self):
        data = self._data

        class _File:
            async def download_as_bytearray(self):
                return bytearray(data)

        return _File()


class FakeMessage:
    def __init__(self, chat, contact=None, text=None, voice=None):
        self.chat = chat
        self.contact = contact
        self.text = text
        self.voice = voice
        self.audio = None
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))
        await self.chat.send_message(text, reply_markup=reply_markup)


class FakeUpdate:
    def __init__(self, chat, user_id, query=None, message=None):
        self.effective_chat = chat
        self.effective_user = SimpleNamespace(id=user_id, first_name="Тарас", last_name="Коваль")
        self.callback_query = query
        self.message = message


class FakeContext:
    def __init__(self, bot_data):
        self.application = SimpleNamespace(bot_data=bot_data)
        self.user_data = {}


def buttons(markup):
    """All (text, callback_data) pairs of an inline keyboard."""
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

