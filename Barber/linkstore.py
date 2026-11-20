"""
Who gets which reminder: phone <-> Telegram chat links and sent-reminder marks.

Stored in the arcade's store through api/_kv.py (Redis-style commands; Neon Postgres
when DATABASE_URL is set, else Upstash; the bb: keys land in table arcade_kv).
Without either it falls back to an in-memory store, which is fine for local runs
but forgets every link on restart.

Keys (all under "bb:"):
    bb:phone:{phone}                -> chat_id      phone shared via the contact button
    bb:chat:{chat_id}               -> phone
    bb:appt:{loc}:{appointment_id}  -> chat_id      booked in the bot / reminded here (90 days)
    bb:client:{loc}:{client_id}     -> phone or "-" Altegio client card phone cache (7 days)
    bb:sent:{appointment_id}:{kind} -> "1"          reminder already sent (4 days)
    bb:optout:{chat_id}             -> "1"          /stop_reminders; cleared by sharing the phone again
    bb:cid:{loc}:{phone}            -> client_id    Altegio client id learned from bookings (90 days)
    bb:last:{chat_id}               -> "loc:appt"   the chat's latest visit, for "Як минулого разу"
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

logger = logging.getLogger(__name__)

APPT_TTL = 90 * 86400
CLIENT_TTL = 7 * 86400
SENT_TTL = 4 * 86400


class LinkStore:
    def __init__(self, memory: bool = None):
        self.memory = (not _kv.configured()) if memory is None else memory
        if self.memory:
            self._mem = _kv.MemoryRedis()
            logger.warning("No DATABASE_URL / Upstash configured: Telegram links are kept in memory only")
        else:
            logger.info("Telegram links stored in %s", _kv.backend())

    def _run(self, *commands):
        if self.memory:
            return [self._mem.execute([str(a) for a in c]) for c in commands]
        return _kv.pipeline(*commands)

    async def _pipe(self, *commands):
        if self.memory:
            return self._run(*commands)
        return await asyncio.to_thread(self._run, *commands)

    # -- phone <-> chat

    async def link(self, chat_id: int, phone: str) -> None:
        """Tie `phone` to this chat. Whoever held the phone before loses it; other links are untouched."""
        old_phone, old_chat = await self._pipe(["GET", "bb:chat:%d" % chat_id], ["GET", "bb:phone:%s" % phone])
        cmds = [["SET", "bb:phone:%s" % phone, chat_id], ["SET", "bb:chat:%d" % chat_id, phone],
                ["GETDEL", "bb:optout:%d" % chat_id]]
        if old_phone and old_phone != phone and await self.chat_for_phone(old_phone) == chat_id:
            cmds.append(["GETDEL", "bb:phone:%s" % old_phone])
        if old_chat and int(old_chat) != chat_id:
            cmds.append(["GETDEL", "bb:chat:%d" % int(old_chat)])
        await self._pipe(*cmds)

    async def unlink(self, chat_id: int) -> bool:
        """Forget the phone and stop all reminders to this chat, including bot-made bookings."""
        phone, _ = await self._pipe(["GETDEL", "bb:chat:%d" % chat_id], ["SET", "bb:optout:%d" % chat_id, "1"])
        if phone and await self.chat_for_phone(phone) == chat_id:
            await self._pipe(["GETDEL", "bb:phone:%s" % phone])
        return bool(phone)

    async def opted_out(self, chat_id: int) -> bool:
        return bool((await self._pipe(["GET", "bb:optout:%d" % chat_id]))[0])

    async def phone_for_chat(self, chat_id: int):
        return (await self._pipe(["GET", "bb:chat:%d" % chat_id]))[0]

    async def chat_for_phone(self, phone: str):
        if not phone:
            return None
        v = (await self._pipe(["GET", "bb:phone:%s" % phone]))[0]
        return int(v) if v else None

    # -- appointment -> chat

    async def remember_appointment(self, location_id: int, appointment_id: int, chat_id: int) -> None:
        await self._pipe(["SET", "bb:appt:%d:%d" % (location_id, appointment_id), chat_id, "EX", APPT_TTL])

    async def chat_for_appointment(self, location_id: int, appointment_id: int):
        v = (await self._pipe(["GET", "bb:appt:%d:%d" % (location_id, appointment_id)]))[0]
        return int(v) if v else None

    # -- Altegio client card phone cache ("-" = card has no phone / not found)

    async def cached_client_phone(self, location_id: int, client_id: int):
        return (await self._pipe(["GET", "bb:client:%d:%d" % (location_id, client_id)]))[0]

    async def cache_client_phone(self, location_id: int, client_id: int, phone: str) -> None:
        await self._pipe(["SET", "bb:client:%d:%d" % (location_id, client_id), phone or "-", "EX", CLIENT_TTL])

    # -- Altegio client id per phone (lets "Мій запис" filter appointments server-side)

    async def remember_client_id(self, location_id: int, phone: str, client_id: int) -> None:
        if phone and client_id:
            await self._pipe(["SET", "bb:cid:%d:%s" % (location_id, phone), client_id, "EX", APPT_TTL])

    async def client_id_for(self, location_id: int, phone: str):
        v = (await self._pipe(["GET", "bb:cid:%d:%s" % (location_id, phone)]))[0] if phone else None
        return int(v) if v else None

    # -- the chat's latest visit (for "🔁 Як минулого разу")

    async def remember_last(self, chat_id: int, location_id: int, appointment_id: int) -> None:
        await self._pipe(["SET", "bb:last:%d" % chat_id, "%d:%d" % (location_id, appointment_id), "EX", 400 * 86400])

    async def last_for(self, chat_id: int):
        """(location_id, appointment_id) of the chat's latest known visit, or None."""
        v = (await self._pipe(["GET", "bb:last:%d" % chat_id]))[0]
        try:
            loc, appt = str(v).split(":")
            return int(loc), int(appt)
        except (TypeError, ValueError):
            return None

    # -- reminder dedupe

    async def was_sent(self, appointment_id: int, kind: str) -> bool:
        return bool((await self._pipe(["GET", "bb:sent:%d:%s" % (appointment_id, kind)]))[0])

    async def mark_sent(self, appointment_id: int, kind: str) -> None:
        await self._pipe(["SET", "bb:sent:%d:%s" % (appointment_id, kind), "1", "EX", SENT_TTL])
