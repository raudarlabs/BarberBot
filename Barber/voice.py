"""
Voice booking: a Telegram voice note -> text -> what the client asked for.

    assistant = make_assistant()            # None without OPENAI_API_KEY
    text = await assistant.transcribe(ogg_bytes)
    intent = await assistant.extract(text, staff_by_location, locations, today)

The language model only fills in fields (barber id from the given list, the
service as said, date, time); booking.py resolves them against Altegio and the
client still confirms with a button, so a misheard phrase never books anything.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import texts as T

logger = logging.getLogger(__name__)

MAX_VOICE_SECONDS = 60
STT_MODEL = os.getenv("VOICE_STT_MODEL", "gpt-4o-mini-transcribe")
LLM_MODEL = os.getenv("VOICE_LLM_MODEL", "gpt-4o-mini")


@dataclass
class Intent:
    location_id: Optional[int] = None
    staff_id: Optional[int] = None
    service: Optional[str] = None  # as the client said it, e.g. "стрижка бороди"
    day: Optional[date] = None
    time: Optional[str] = None  # "HH:MM"
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    cancel: bool = False  # stop the booking dialog
    cancel_visit: bool = False  # wants to cancel an existing appointment (not supported in the bot)
    said: str = ""  # the transcript, kept for picking the service from the barber's list


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _hhmm(v) -> Optional[str]:
    m = _HHMM.match(str(v or "").strip())
    return "%02d:%s" % (int(m.group(1)), m.group(2)) if m else None


def intent_from_json(data: dict, staff_by_location: Dict[int, Sequence], today: date,
                     days_ahead: int = 14) -> Intent:
    """Validate the model's JSON: ids must come from the lists, dates from the booking window."""
    valid_locs = set(staff_by_location)
    valid_staff = {s.id: loc for loc, staff in staff_by_location.items() for s in staff}
    it = Intent(cancel=bool(data.get("cancel")), cancel_visit=bool(data.get("cancel_visit")))
    try:
        loc = int(data.get("location_id")) if data.get("location_id") is not None else None
    except (TypeError, ValueError):
        loc = None
    it.location_id = loc if loc in valid_locs else None
    try:
        staff = int(data.get("staff_id")) if data.get("staff_id") is not None else None
    except (TypeError, ValueError):
        staff = None
    if staff in valid_staff:
        it.staff_id = staff
        it.location_id = valid_staff[staff]
    service = str(data.get("service") or "").strip()
    it.service = service[:80] or None
    try:
        d = date.fromisoformat(str(data.get("date") or "")[:10])
        if today <= d < today + timedelta(days=days_ahead):
            it.day = d
    except ValueError:
        pass
    it.time = _hhmm(data.get("time"))
    it.time_from = _hhmm(data.get("time_from"))
    it.time_to = _hhmm(data.get("time_to"))
    return it


# ------------------------------------------------------------------ service matching

def _tokens(s: str) -> List[str]:
    return [t for t in re.findall(r"[\w']+", s.lower().replace("ʼ", "'")) if len(t) >= 3]


def _token_hit(q: str, t: str) -> bool:
    if q == t:
        return True
    n = 0
    for a, b in zip(q, t):
        if a != b:
            break
        n += 1
    return n >= min(5, len(q), len(t)) and n >= 4  # Ukrainian endings: бороди/борода, Ігоря/Ігор


def exact_service(query: str, services: Sequence) -> Optional[object]:
    q = _tokens(query or "")
    return next((s for s in services if q and _tokens(s.title) == q), None)


def match_service(query: str, services: Sequence) -> Optional[object]:
    """Best service for what the client said, or None when unclear (then the buttons decide)."""
    q = _tokens(query or "")
    if not q or not services:
        return None
    for s in services:  # said exactly
        if _tokens(s.title) == q:
            return s
    scored = []
    for s in services:
        title = _tokens(s.title)
        hits = sum(1 for qt in q if any(_token_hit(qt, tt) for tt in title))
        if not hits:
            continue
        extra = len(title) - hits  # prefer titles without words the client did not say
        ratio = difflib.SequenceMatcher(None, " ".join(q), " ".join(title)).ratio()
        scored.append((hits / len(q) - 0.1 * max(extra, 0) + 0.3 * ratio, s))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        return None
    return scored[0][1]


# ------------------------------------------------------------------ OpenAI

SYSTEM_PROMPT = """You turn a barbershop client's spoken booking request (Ukrainian, Russian or English) into JSON.
Return ONLY a JSON object with these keys, null when the client did not say it:
  "location_id": id from LOCATIONS,
  "staff_id": id from BARBERS (match names in any grammatical case, nicknames like Настя=Анастасія, Вітя=Віктор),
  "service": the service as the client said it, in nominative case, Ukrainian (e.g. "стрижка бороди"),
  "date": "YYYY-MM-DD" (resolve "сьогодні", "завтра", "в четвер", "15-го" from TODAY),
  "time": "HH:MM" for an exact time; at a barbershop "о третій" / "в 3" means 15:00, "о десятій" 10:00,
  "time_from", "time_to": "HH:MM" for a range ("після обіду" 13:00-, "ввечері" 17:00-, "зранку" -12:00),
  "cancel": true only if the client wants to stop this booking dialog ("не треба", "передумав"),
  "cancel_visit": true if the client wants to cancel an appointment they already have.
Never invent ids that are not in the lists."""

PICK_SYSTEM = """A barbershop client asked for a service out loud. Pick the ONE service from SERVICES that
matches what they asked for. A haircut together with a beard trim is the combined service if one
exists (e.g. "Комплекс" or "... + борода"), not the beard-only one. If the request fits several
services equally or none, answer null. Return ONLY JSON: {"service_id": <id from SERVICES or null>}."""


class OpenAIVoiceAssistant:
    def __init__(self, api_key: str, stt_model: str = STT_MODEL, llm_model: str = LLM_MODEL):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)
        self.stt_model = stt_model
        self.llm_model = llm_model

    async def transcribe(self, audio: bytes) -> str:
        res = await self._client.audio.transcriptions.create(
            model=self.stt_model, file=("voice.ogg", audio, "audio/ogg"), language="uk",
        )
        return (getattr(res, "text", "") or "").strip()

    async def extract(self, text: str, staff_by_location: Dict[int, Sequence], locations: Dict[int, str],
                      today: date, current: Optional[dict] = None) -> Intent:
        context = {
            "TODAY": "%s (%s)" % (today.isoformat(), T.WEEKDAYS[today.weekday()]),
            "LOCATIONS": [{"id": loc, "address": addr} for loc, addr in locations.items()],
            "BARBERS": [{"id": s.id, "name": s.name, "location_id": loc}
                        for loc, staff in staff_by_location.items() for s in staff],
        }
        if current:
            context["ALREADY_CHOSEN"] = current
        res = await self._client.chat.completions.create(
            model=self.llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False) + "\n\nCLIENT SAID: " + text},
            ],
        )
        try:
            data = json.loads(res.choices[0].message.content or "{}")
        except ValueError:
            data = {}
        return intent_from_json(data if isinstance(data, dict) else {}, staff_by_location, today)

    async def pick_service(self, said: str, services: Sequence) -> Optional[int]:
        listing = [{"id": s.id, "title": s.title} for s in services]
        res = await self._client.chat.completions.create(
            model=self.llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PICK_SYSTEM},
                {"role": "user", "content": json.dumps({"SERVICES": listing}, ensure_ascii=False)
                 + "\n\nCLIENT SAID: " + said},
            ],
        )
        try:
            sid = int(json.loads(res.choices[0].message.content or "{}").get("service_id"))
        except (TypeError, ValueError, AttributeError):
            return None
        return sid if any(s.id == sid for s in services) else None


def make_assistant(env=None):
    env = os.environ if env is None else env
    key = env.get("OPENAI_API_KEY")
    if not key or env.get("VOICE_BOOKING", "1") == "0":
        return None
    return OpenAIVoiceAssistant(key, env.get("VOICE_STT_MODEL", STT_MODEL), env.get("VOICE_LLM_MODEL", LLM_MODEL))
