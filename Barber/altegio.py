"""
Altegio REST client for the bot: barbers, their services, free slots, appointments.

    client = make_client()          # real API, or the recorded fixture when ALTEGIO_MOCK=1
    staff = await client.list_staff(297851)

Contracts come from the Altegio API catalog (base https://api.alteg.io/api/v1):

    GET  /locations/{loc}/team_members                     barbers (JSON:API)
    GET  /locations/{loc}/services?staff_id=               a barber's services (JSON:API)
    GET  /timetable/dates/{loc}/{date}?staff_id=           working dates
    GET  /timetable/seances/{loc}/{staff}/{date}           5-minute grid: [{time, is_free}]
    GET  /book_dates/{loc}?staff_id=&service_ids[]=        online-bookable days (widget calendar)
    GET  /book_times/{loc}/{staff}/{date}?service_ids[]=   online-bookable start times
    GET  /locations/{loc}/appointments                     start_date, end_date (days), include=client
    GET  /locations/{loc}/appointments/{id}
    POST /locations/{loc}/appointments                     create (staff_id, services, client, datetime, seance_length)
    PUT  /locations/{loc}/appointments/{id}                full update (used to move the time)
    POST /locations/{loc}/appointments/{id}/attendance     {"attendance": 2} = client confirmed
    DELETE /locations/{loc}/appointments/{id}              client cancelled (204)
    GET  /client/{loc}/{id}                                client card (phone)

Every call needs `Authorization: Bearer <partner token>, User <user token>`.
Paths live in one place (`HttpAltegioClient`) so a contract fix is a one-line change.
Responses are parsed leniently: JSON:API resource objects and legacy flat objects,
`{success, data, meta}` and `{data, meta}` envelopes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.alteg.io/api/v1"
DEFAULT_TZ = "Europe/Kyiv"
DEFAULT_DURATION = 3600  # used only when Altegio does not say how long a service takes

ATTENDANCE_STATUS = {-1: "no_show", 0: "waiting", 1: "arrived", 2: "confirmed"}


class AltegioError(RuntimeError):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


# ------------------------------------------------------------------ models

@dataclass(frozen=True)
class Staff:
    id: int
    name: str
    specialization: str = ""


@dataclass(frozen=True)
class Service:
    id: int
    title: str
    price_min: float = 0
    price_max: float = 0
    duration: int = DEFAULT_DURATION  # seconds

    @property
    def price_label(self) -> str:
        lo, hi = int(self.price_min or 0), int(self.price_max or 0)
        if not lo and not hi:
            return ""
        if hi and hi != lo:
            return "%d–%d ₴" % (lo, hi)
        return "%d ₴" % (lo or hi)


@dataclass
class Appointment:
    id: int
    location_id: int
    staff_id: int
    start: datetime  # timezone-aware, location time
    duration: int = DEFAULT_DURATION
    status: str = "waiting"
    staff_name: str = ""
    client_id: int = 0
    client_phone: str = ""
    client_name: str = ""
    services: List[dict] = field(default_factory=list)  # [{"id", "title", "cost", "first_cost", "discount"}]
    comment: str = ""
    api_id: str = ""
    online: bool = False  # booked by the client through Altegio's online booking (widget / site)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def active(self) -> bool:
        return self.status in ("waiting", "confirmed")

    @property
    def service_ids(self) -> List[int]:
        return [int(s["id"]) for s in self.services if s.get("id")]

    @property
    def service_titles(self) -> str:
        return ", ".join(s.get("title", "") for s in self.services if s.get("title"))


# ------------------------------------------------------------------ helpers

def normalize_phone(raw: str) -> str:
    """Ukrainian-friendly E.164 digits without '+': '067 123 45 67' -> '380671234567'."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 9:
        digits = "380" + digits
    return digits


def compute_free_starts(
    grid: Sequence[dict],
    day: date,
    duration: int,
    tz: ZoneInfo,
    now: Optional[datetime] = None,
    lead_minutes: int = 30,
    align_minutes: int = 15,
    exclude: Sequence = (),
) -> List[datetime]:
    """
    Start times on `day` where `duration` seconds fit into free grid cells.

    `grid` is Altegio's appointment-calendar grid: [{"time": "11:00", "is_free": bool}, ...],
    one cell per step (usually 5 min); the working day ends one step after the last cell.
    Starts are offered on `align_minutes` boundaries and at least `lead_minutes` from now.
    `exclude` = [(start, end), ...] busy intervals to treat as free, e.g. the visit being moved.
    """
    freed = []
    for lo, hi in exclude:
        lo, hi = lo.astimezone(tz), hi.astimezone(tz)
        if lo.date() <= day <= hi.date():
            a = lo.hour * 60 + lo.minute if lo.date() == day else 0
            b = hi.hour * 60 + hi.minute if hi.date() == day else 24 * 60
            freed.append((a, b))
    cells = []
    for item in grid:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(item.get("time", "")))
        if not m:
            continue
        minute = int(m.group(1)) * 60 + int(m.group(2))
        free = _truthy(item.get("is_free", item.get("free", False)))
        if not free and any(a <= minute < b for a, b in freed):
            free = True
        cells.append((minute, free))
    if not cells:
        return []
    cells.sort()
    step = cells[1][0] - cells[0][0] if len(cells) > 1 else 5
    step = step if step > 0 else 5
    free_at = {m: f for m, f in cells}
    need = max(1, -(-int(duration) // (step * 60)))  # ceil(duration / step)

    earliest = None
    if now is not None:
        earliest = now.astimezone(tz) + timedelta(minutes=lead_minutes)

    out = []
    for start, is_free in cells:
        if not is_free or start % align_minutes:
            continue
        if all(free_at.get(start + i * step) for i in range(need)):
            dt = datetime(day.year, day.month, day.day, start // 60, start % 60, tzinfo=tz)
            if earliest is None or dt >= earliest:
                out.append(dt)
    return out


def _flatten(item) -> dict:
    """JSON:API resource object -> flat dict (id + attributes); plain dicts pass through."""
    if not isinstance(item, dict):
        return {}
    if isinstance(item.get("attributes"), dict):
        flat = dict(item["attributes"])
        flat.setdefault("id", item.get("id"))
        if "relationships" in item:
            flat["relationships"] = item["relationships"]
        return flat
    return item


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "null")
    return bool(v)


def parse_staff(raw) -> Optional[Staff]:
    s = _flatten(raw)
    if not s.get("id"):
        return None
    hidden = s.get("hidden", s.get("hidden_from_online_booking"))
    fired = s.get("fired", s.get("dismissed"))
    if _truthy(hidden) or _truthy(fired) or _truthy(s.get("is_deleted", s.get("deleted"))):
        return None
    # Note: the staff list's `bookable` is false for everyone on the real API (it depends on the
    # booking context), so it is not a visibility flag; `hidden` / `fired` are.
    return Staff(id=_int(s["id"]), name=str(s.get("name", "")).strip(),
                 specialization=str(s.get("specialization", "") or ""))


def parse_service(raw, staff_id: Optional[int] = None) -> Optional[Service]:
    s = _flatten(raw)
    if not s.get("id"):
        return None
    if "active" in s and not _truthy(s["active"]):
        return None
    price_min, price_max = s.get("price_min"), s.get("price_max")
    duration = s.get("duration") or s.get("seance_length")
    links = s.get("company_links") or []
    if isinstance(links, list) and links:
        price_min = price_min if price_min is not None else links[0].get("price_min")
        price_max = price_max if price_max is not None else links[0].get("price_max")
    for link in s.get("staff") or []:  # legacy: per-barber session length
        if isinstance(link, dict) and staff_id and _int(link.get("id")) == staff_id:
            duration = link.get("seance_length") or duration
    return Service(
        id=_int(s["id"]),
        title=str(s.get("title", "")).strip(),
        price_min=float(price_min or 0),
        price_max=float(price_max or price_min or 0),
        duration=_int(duration, DEFAULT_DURATION) or DEFAULT_DURATION,
    )


def _parse_local(value, tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz)
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)  # +0200 -> +02:00 (Python < 3.11)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def parse_appointment(raw, location_id: int, tz: ZoneInfo, included: Optional[dict] = None) -> Optional[Appointment]:
    a = _flatten(raw)
    if not a.get("id"):
        return None
    start = (_parse_local(a.get("datetime"), tz) or _parse_local(a.get("date"), tz)
             or _parse_local(a.get("timestamp"), tz))
    if start is None:
        return None

    if _truthy(a.get("deleted")):
        status = "cancelled"
    elif isinstance(a.get("status"), str) and a["status"] in ATTENDANCE_STATUS.values():
        status = a["status"]
    else:
        status = ATTENDANCE_STATUS.get(_int(a.get("attendance", a.get("visit_attendance")), 0), "waiting")

    client = a.get("client") if isinstance(a.get("client"), dict) else {}
    rel = (a.get("relationships") or {}).get("client", {}).get("data") or {}
    if included and rel.get("id"):
        client = {**_flatten(included.get(("client", str(rel["id"])), {})), **client}

    staff = a.get("staff") or a.get("team_member") or {}
    services = []
    for svc in a.get("services") or []:
        if isinstance(svc, dict) and svc.get("id"):
            services.append({"id": _int(svc["id"]), "title": svc.get("title", ""),
                             "cost": svc.get("cost", svc.get("first_cost", 0)),
                             "first_cost": svc.get("first_cost", svc.get("cost", 0)),
                             "discount": svc.get("discount", 0)})

    return Appointment(
        id=_int(a["id"]),
        location_id=_int(a.get("company_id") or a.get("location_id"), location_id) or location_id,
        staff_id=_int(a.get("staff_id") or a.get("team_member_id") or staff.get("id")),
        start=start,
        duration=_int(a.get("seance_length") or a.get("length") or a.get("duration_seconds"), DEFAULT_DURATION),
        status=status,
        staff_name=str(staff.get("name", "") or ""),
        client_id=_int(a.get("client_id") or client.get("id") or rel.get("id")),
        client_phone=normalize_phone(client.get("phone") or a.get("client_fictive_phone") or ""),
        client_name=str(client.get("name") or a.get("client_fictive_name") or ""),
        services=services,
        comment=str(a.get("comment") or ""),
        api_id=str(a.get("api_id") or a.get("external_id") or ""),
        online=_truthy(a.get("online")),
    )


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v or 0).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return 0.0


def parse_transaction(raw, tz: ZoneInfo) -> Optional[dict]:
    """A money movement: {"id", "d": local day, "a": signed amount, "c": payment item id (0 = transfer
    between accounts), "cat": item title, "acc": account id, "m": team member id, "goods": bool}."""
    t = _flatten(raw)
    if not t.get("id"):
        return None
    when = _parse_local(t.get("date"), tz)
    if when is None:
        return None
    exp = t.get("expense") if isinstance(t.get("expense"), dict) else {}
    acc = t.get("account") if isinstance(t.get("account"), dict) else {}
    master = t.get("master") if isinstance(t.get("master"), dict) else {}
    return {"id": _int(t["id"]), "d": when.date().isoformat(), "a": round(_num(t.get("amount")), 2),
            "c": _int(exp.get("id")), "cat": str(exp.get("title") or ""), "acc": _int(acc.get("id")),
            "m": _int(master.get("id")), "goods": t.get("sold_item_type") == "goods_transaction"}


def parse_overall(data) -> dict:
    d = data if isinstance(data, dict) else {}

    def money(k):
        return round(_num((d.get(k) or {}).get("current_sum")), 2)

    rec, cl, full = d.get("record_stats") or {}, d.get("client_stats") or {}, d.get("fullness_stats") or {}
    return {
        "income_total": money("income_total_stats"), "income_services": money("income_services_stats"),
        "income_goods": money("income_goods_stats"), "avg_check": money("income_average_stats"),
        "avg_services_check": money("income_average_services_stats"),
        "occupancy": round(_num(full.get("current_percent")), 1),
        "records": _int(rec.get("current_total_count")), "completed": _int(rec.get("current_completed_count")),
        "pending": _int(rec.get("current_pending_count")), "cancelled": _int(rec.get("current_canceled_count")),
        "cancelled_percent": _int(rec.get("current_canceled_percent")),
        "clients_total": _int(cl.get("total_count")), "new_clients": _int(cl.get("new_count")),
        "new_percent": _int(cl.get("new_percent")), "returning": _int(cl.get("return_count")),
        "return_percent": _int(cl.get("return_percent")), "active_clients": _int(cl.get("active_count")),
        "lost_clients": _int(cl.get("lost_count")), "lost_percent": _int(cl.get("lost_percent")),
    }


def _unwrap(payload):
    """{success, data, meta} / {data, meta} / bare -> (data, included)."""
    if isinstance(payload, dict):
        if payload.get("success") is False:
            meta = payload.get("meta") or {}
            msg = meta.get("message") if isinstance(meta, dict) else None
            raise AltegioError(msg or "altegio_error", payload=payload)
        if "data" in payload:
            return payload["data"], payload.get("included") or []
    return payload, []


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "records", "booking_dates", "dates"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


# ------------------------------------------------------------------ client

class HttpAltegioClient:
    """Async Altegio client. Pass `transport` to run against a fixture (see altegio_fixture.py)."""

    def __init__(self, partner_token: str, user_token: str, base_url: str = DEFAULT_BASE_URL,
                 tz: str = DEFAULT_TZ, transport: Optional[httpx.AsyncBaseTransport] = None,
                 min_interval: float = 0.22, is_mock: bool = False, hidden_staff: Iterable[int] = ()):
        self.tz = ZoneInfo(tz)
        # Barbers still active in Altegio but no longer working at the shop (not offered anywhere).
        self.hidden_staff = set(int(x) for x in hidden_staff)
        self.is_mock = is_mock
        self._min_interval = min_interval  # Altegio allows 5 req/s per IP
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=15.0,
            headers={
                "Authorization": "Bearer %s, User %s" % (partner_token, user_token),
                "Accept": "application/vnd.api.v2+json",
                "Content-Type": "application/json",
                "User-Agent": "barber-telegram-bot",
            },
        )

    async def aclose(self):
        await self._http.aclose()

    async def _request(self, method: str, path: str, params=None, json=None):
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        try:
            resp = await self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as e:
            raise AltegioError("network: %s" % e) from e
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"raw": resp.text[:500]}
        if resp.status_code >= 400:
            meta = payload.get("meta") if isinstance(payload, dict) else None
            msg = (meta or {}).get("message") if isinstance(meta, dict) else None
            raise AltegioError(msg or "http %d" % resp.status_code, resp.status_code, payload)
        return _unwrap(payload)

    # -- catalog

    async def list_staff(self, location_id: int) -> List[Staff]:
        data, _ = await self._request(
            "GET", "/locations/%d/team_members" % location_id,
            params={"filter[deleted]": 0, "filter[fired]": 0},
        )
        # Keep Altegio's order: it is the order the shop set for its online booking page.
        return [s for s in (parse_staff(x) for x in _as_list(data)) if s and s.id not in self.hidden_staff]

    async def list_services(self, location_id: int, staff_id: int) -> List[Service]:
        # The catalog names the filter `team_member_id`; legacy endpoints call it `staff_id`.
        data, _ = await self._request(
            "GET", "/locations/%d/services" % location_id,
            params={"staff_id": staff_id, "team_member_id": staff_id, "limit": 100},
        )
        return [s for s in (parse_service(x, staff_id) for x in _as_list(data)) if s]

    # -- availability

    async def working_dates(self, location_id: int, staff_id: int, start: date, days: int) -> List[date]:
        data, _ = await self._request(
            "GET", "/timetable/dates/%d/%s" % (location_id, start.isoformat()),
            params={"staff_id": staff_id, "team_member_id": staff_id},
        )
        last = start + timedelta(days=days - 1)
        out = []
        for d in _as_list(data):
            try:
                dd = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
            if start <= dd <= last:
                out.append(dd)
        return sorted(set(out))

    async def booking_dates(self, location_id: int, staff_id: int, service_ids: Sequence[int],
                            start: date, days: int) -> List[date]:
        """Days a client can book online (the Altegio booking widget's calendar): free time for these
        services under the location's online-booking rules — not merely the barber's working days."""
        params = {"staff_id": staff_id}
        if service_ids:
            params["service_ids[]"] = [int(x) for x in service_ids]
        data, _ = await self._request("GET", "/book_dates/%d" % location_id, params=params)
        raw = data.get("booking_dates") if isinstance(data, dict) else _as_list(data)
        last = start + timedelta(days=days - 1)
        out = []
        for d in raw or []:
            try:
                dd = date.fromisoformat(str(d)[:10])
            except ValueError:
                continue
            if start <= dd <= last:
                out.append(dd)
        return sorted(set(out))

    async def booking_times(self, location_id: int, staff_id: int, day: date,
                            service_ids: Sequence[int]) -> List[datetime]:
        """Start times the Altegio booking widget offers on `day` for these services."""
        params = {"service_ids[]": [int(x) for x in service_ids]} if service_ids else None
        data, _ = await self._request(
            "GET", "/book_times/%d/%d/%s" % (location_id, staff_id, day.isoformat()), params=params)
        out = []
        for x in _as_list(data):
            raw = x.get("datetime") if isinstance(x, dict) else None
            try:
                dt = datetime.fromisoformat(str(raw))
            except (TypeError, ValueError):
                continue
            dt = dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt.astimezone(self.tz)
            if dt.date() == day:
                out.append(dt)
        return sorted(set(out))

    async def day_grid(self, location_id: int, staff_id: int, day: date) -> List[dict]:
        data, _ = await self._request(
            "GET", "/timetable/seances/%d/%d/%s" % (location_id, staff_id, day.isoformat())
        )
        return _as_list(data)

    async def free_starts(self, location_id: int, staff_id: int, day: date, duration: int,
                          now: Optional[datetime] = None, exclude: Sequence = ()) -> List[datetime]:
        grid = await self.day_grid(location_id, staff_id, day)
        return compute_free_starts(grid, day, duration, self.tz, now=now or datetime.now(self.tz),
                                   exclude=exclude)

    # -- appointments

    async def create_appointment(self, location_id: int, staff_id: int, services: Iterable[Service],
                                 start: datetime, phone: str, name: str,
                                 comment: str = "", api_id: str = "") -> Appointment:
        services = list(services)
        body = {
            "staff_id": staff_id,          # legacy name the REST API uses...
            "team_member_id": staff_id,    # ...and the catalog's name; send both
            "services": [{"id": s.id} for s in services],
            "client": {"phone": "+" + normalize_phone(phone), "name": name or "Telegram"},
            "datetime": start.astimezone(self.tz).isoformat(),
            "seance_length": sum(s.duration for s in services) or DEFAULT_DURATION,
            "save_if_busy": False,
            "comment": comment,
        }
        if api_id:
            body["api_id"] = api_id
        data, _ = await self._request("POST", "/locations/%d/appointments" % location_id, json=body)
        raw = data[0] if isinstance(data, list) and data else data
        appt = parse_appointment(raw, location_id, self.tz)
        if appt is None:
            raise AltegioError("unexpected create response", payload=data)
        if not appt.client_phone:
            appt.client_phone = normalize_phone(phone)
        return appt

    async def list_appointments(self, location_id: int, start: datetime, end: datetime,
                                client_id: Optional[int] = None) -> List[Appointment]:
        # The real API filters by whole days (start_date/end_date, inclusive); `filter[...]` params
        # are ignored there and the full history (tens of thousands) comes back. Exact times are
        # filtered below.
        lo, hi = start.astimezone(self.tz), end.astimezone(self.tz)
        out, seen, page, page_size = [], set(), 1, None
        while page <= 50:
            params = {
                "start_date": lo.date().isoformat(),
                "end_date": hi.date().isoformat(),
                "include": "client",
                "page": page,
                "count": 100,
            }
            if client_id:
                params["client_id"] = client_id
            data, included = await self._request("GET", "/locations/%d/appointments" % location_id, params=params)
            index = {(str(i.get("type")), str(i.get("id"))): i for i in included if isinstance(i, dict)}
            items = _as_list(data)
            fresh = [a for a in (parse_appointment(x, location_id, self.tz, index) for x in items)
                     if a and a.id not in seen]
            if not fresh:  # empty page, or the server ignored `page` and repeated itself
                break
            seen.update(a.id for a in fresh)
            out += fresh
            # The server may cap the page below 100: stop on the first page shorter than page 1.
            page_size = page_size or len(items)
            if len(items) < page_size:
                break
            page += 1
        else:
            logger.warning("list_appointments: stopped after 50 pages for location %s", location_id)
        return [a for a in out if a.start < hi and a.start + timedelta(seconds=a.duration or 0) > lo]

    # -- finance and analytics (read-only; for the admin dashboard)

    async def list_transactions(self, location_id: int, start: date, end: date) -> List[dict]:
        """Money in and out from `start` to `end` (inclusive days), see parse_transaction."""
        out, seen, page = [], set(), 1
        while page <= 50:
            data, _ = await self._request("GET", "/transactions/%d" % location_id, params={
                "start_date": start.isoformat(), "end_date": end.isoformat(), "page": page, "count": 1000})
            items = _as_list(data)
            fresh = [t for t in (parse_transaction(x, self.tz) for x in items) if t and t["id"] not in seen]
            if not fresh:
                break
            seen.update(t["id"] for t in fresh)
            out += fresh
            if len(items) < 1000:
                break
            page += 1
        return [t for t in out if start.isoformat() <= t["d"] <= end.isoformat()]

    async def list_accounts(self, location_id: int) -> List[dict]:
        """Cash registers and bank accounts: [{"id", "title", "cash"}]."""
        data, _ = await self._request("GET", "/accounts/%d" % location_id)
        return [{"id": _int(x.get("id")), "title": str(x.get("title") or ""),
                 "cash": str(x.get("type_slug") or "") != "cashless" and _int(x.get("type")) != 1}
                for x in _as_list(data) if isinstance(x, dict) and x.get("id")]

    async def analytics_overall(self, location_id: int, start: date, end: date,
                                staff_id: Optional[int] = None) -> dict:
        """Altegio's own report for the period (occupancy, clients, records, income), see parse_overall."""
        params = {"date_from": start.isoformat(), "date_to": end.isoformat()}
        if staff_id:
            params["staff_id"] = staff_id
        data, _ = await self._request("GET", "/company/%d/analytics/overall/" % location_id, params=params)
        return parse_overall(data)

    async def get_appointment(self, location_id: int, appointment_id: int) -> Appointment:
        data, included = await self._request(
            "GET", "/locations/%d/appointments/%d" % (location_id, appointment_id)
        )
        index = {(str(i.get("type")), str(i.get("id"))): i for i in included if isinstance(i, dict)}
        appt = parse_appointment(data, location_id, self.tz, index)
        if appt is None:
            raise AltegioError("appointment not found", 404)
        return appt

    async def confirm_appointment(self, location_id: int, appointment_id: int) -> None:
        await self._request(
            "POST", "/locations/%d/appointments/%d/attendance" % (location_id, appointment_id),
            json={"attendance": 2},
        )

    async def cancel_appointment(self, location_id: int, appointment_id: int) -> None:
        """Client cancelled: delete the appointment (what Altegio does when a client cancels online). 204."""
        await self._request("DELETE", "/locations/%d/appointments/%d" % (location_id, appointment_id))

    async def reschedule_appointment(self, appt: Appointment, new_start: datetime) -> Appointment:
        """PUT is a full update: resend everything we know unchanged, only the time moves."""
        services = []
        for s in appt.services:
            item = {"id": s["id"]}
            for key in ("cost", "first_cost", "discount"):
                if s.get(key) is not None:
                    item[key] = s[key]
            services.append(item)
        body = {
            "staff_id": appt.staff_id,
            "team_member_id": appt.staff_id,
            "services": services,
            "client": {"id": appt.client_id} if appt.client_id else
                      {"phone": "+" + appt.client_phone, "name": appt.client_name},
            "datetime": new_start.astimezone(self.tz).isoformat(),
            "seance_length": appt.duration,
            "attendance": 2 if appt.status == "confirmed" else 0,
            "save_if_busy": False,
        }
        if appt.comment:
            body["comment"] = appt.comment
        if appt.api_id:
            body["api_id"] = appt.api_id
        data, _ = await self._request(
            "PUT", "/locations/%d/appointments/%d" % (appt.location_id, appt.id), json=body
        )
        updated = parse_appointment(data, appt.location_id, self.tz)
        return updated or Appointment(**{**appt.__dict__, "start": new_start})

    async def get_client_phone(self, location_id: int, client_id: int) -> str:
        data, _ = await self._request("GET", "/client/%d/%d" % (location_id, client_id))
        return normalize_phone(_flatten(data).get("phone", ""))


# Left the shop but still active in Altegio (Nov 2026): Нікіта Василенко. ALTEGIO_HIDE_STAFF overrides.
DEFAULT_HIDDEN_STAFF = "2222418"


def hidden_staff(env=None) -> List[int]:
    env = os.environ if env is None else env
    raw = env.get("ALTEGIO_HIDE_STAFF", DEFAULT_HIDDEN_STAFF)
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()]


def make_client(env=None) -> Optional[HttpAltegioClient]:
    """
    Real client when both tokens are set; the recorded fixture when ALTEGIO_MOCK=1;
    otherwise None (booking and reminders stay off - never show test data to real clients).
    """
    env = os.environ if env is None else env
    tz = env.get("ALTEGIO_TZ", DEFAULT_TZ)
    partner, user = env.get("ALTEGIO_PARTNER_TOKEN"), env.get("ALTEGIO_USER_TOKEN")
    if partner and user:
        return HttpAltegioClient(partner, user, env.get("ALTEGIO_BASE_URL", DEFAULT_BASE_URL), tz,
                                 hidden_staff=hidden_staff(env))
    if env.get("ALTEGIO_MOCK") == "1":
        from altegio_fixture import fixture_client
        logger.warning("ALTEGIO_MOCK=1: booking runs against the recorded fixture, nothing reaches Altegio")
        return fixture_client(tz=tz)
    logger.warning("ALTEGIO_PARTNER_TOKEN / ALTEGIO_USER_TOKEN not set: booking and reminders are off")
    return None
