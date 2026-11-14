"""
python-telegram-bot persistence on the shared store (api/_kv.py: Neon Postgres in production).

On Vercel every Telegram update is handled by a fresh process, so the booking conversation's
state and `user_data` must live in the database between updates. Stored per user / per
conversation key (one hash field each), so two clients booking at the same time never
overwrite each other.

Without a JobQueue there is no conversation_timeout, so entries older than `expire_after`
seconds are treated as abandoned when loaded — the same 30 minutes the polling bot uses.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pickle
import sys
import time
from typing import Dict, Optional

from telegram.ext import BasePersistence, PersistenceInput

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

USER_DATA = "bb:ptb:user_data"
CONVERSATIONS = "bb:ptb:conv:"


def _dump(obj) -> str:
    return base64.b64encode(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)).decode()


def _load(s: str):
    # Only ever written by this module into our own private database.
    return pickle.loads(base64.b64decode(s))


class KVPersistence(BasePersistence):
    def __init__(self, expire_after: Optional[float] = 30 * 60):
        super().__init__(
            store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True, callback_data=False),
            update_interval=3600,  # writes happen on shutdown, i.e. at the end of each webhook call
        )
        self.expire_after = expire_after

    async def _call(self, *commands):
        return await asyncio.to_thread(_kv.pipeline, *commands)

    def _fresh(self, ts) -> bool:
        return not self.expire_after or (time.time() - float(ts or 0)) <= self.expire_after

    # ---------------------------------------------------------------- user_data

    async def get_user_data(self) -> Dict[int, dict]:
        raw = (await self._call(["HGETALL", USER_DATA]))[0] or []
        out = {}
        for i in range(0, len(raw), 2):
            data = _load(raw[i + 1])
            bk = data.get("bk")
            if isinstance(bk, dict) and not self._fresh(bk.get("ts")):
                data.pop("bk")  # an abandoned booking flow
            out[int(raw[i])] = data
        return out

    async def update_user_data(self, user_id: int, data: dict) -> None:
        if data:
            await self._call(["HSET", USER_DATA, str(user_id), _dump(data)])
        else:
            await self._call(["HDEL", USER_DATA, str(user_id)])  # keep the hash to active users only

    async def drop_user_data(self, user_id: int) -> None:
        await self._call(["HDEL", USER_DATA, str(user_id)])

    async def refresh_user_data(self, user_id: int, user_data: dict) -> None:
        pass

    # ---------------------------------------------------------------- conversations

    async def get_conversations(self, name: str):
        raw = (await self._call(["HGETALL", CONVERSATIONS + name]))[0] or []
        out = {}
        for i in range(0, len(raw), 2):
            entry = json.loads(raw[i + 1])
            if self._fresh(entry.get("t")):
                out[tuple(json.loads(raw[i]))] = entry["s"]
        return out

    async def update_conversation(self, name: str, key, new_state) -> None:
        field = json.dumps(list(key))
        if new_state is None:
            await self._call(["HDEL", CONVERSATIONS + name, field])
        else:
            await self._call(["HSET", CONVERSATIONS + name, field, json.dumps({"s": new_state, "t": time.time()})])

    # ---------------------------------------------------------------- not stored

    async def get_chat_data(self):
        return {}

    async def update_chat_data(self, chat_id, data) -> None:
        pass

    async def drop_chat_data(self, chat_id) -> None:
        pass

    async def refresh_chat_data(self, chat_id, chat_data) -> None:
        pass

    async def get_bot_data(self):
        return {}

    async def update_bot_data(self, data) -> None:
        pass

    async def refresh_bot_data(self, bot_data) -> None:
        pass

    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data) -> None:
        pass

    async def flush(self) -> None:
        pass
