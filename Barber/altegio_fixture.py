"""
A fake Altegio server for local runs (ALTEGIO_MOCK=1) and tests.

It answers the same HTTP paths as the real API from fixtures/altegio.json, so
HttpAltegioClient's request building and response parsing run unchanged.
Appointments live in memory: created ones block their time in the grid.
Nothing here ever talks to the network.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from altegio import HttpAltegioClient

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "altegio.json")


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


class FixtureBackend:
    def __init__(self, fixture: Optional[dict] = None, tz: str = "Europe/Kyiv"):
        if fixture is None:
            with open(FIXTURE_PATH, encoding="utf-8") as f:
                fixture = json.load(f)
        self.fx = fixture
        self.tz = ZoneInfo(tz)
        self.appointments = {}  # id -> wire-format dict (+ "_loc")
        self.clients = {}  # (loc, client_id) -> {"id", "name", "phone"}
        self.requests = []  # (method, path, params, body) for assertions
        self.fail_next = None  # (status, message) to simulate an API error once
        self.page_cap = None  # simulate a server that returns fewer rows per page than asked
        self._next_id = 900000001

    # -- test helpers

    def add_appointment(self, location_id, staff_id, start: datetime, duration=3600,
                        phone="", name="", attendance=0, deleted=False, services=None, client_id=None):
        appt_id = self._next_id
        self._next_id += 1
        cid = client_id if client_id is not None else (appt_id + 5000 if phone else 0)
        if cid:
            self.clients[(int(location_id), int(cid))] = {"id": cid, "name": name, "phone": phone}
        self.appointments[appt_id] = {
            "_loc": int(location_id),
            "id": appt_id,
            "staff_id": int(staff_id),
            "datetime": start.astimezone(self.tz).isoformat(),
            "seance_length": duration,
            "attendance": attendance,
            "deleted": deleted,
            "client_id": cid,
            "services": services or [],
        }
        return appt_id

    # -- routing

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        path = path[path.index("/api/v1") + len("/api/v1"):] if "/api/v1" in path else path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, params, body))

        if self.fail_next:
            status, msg = self.fail_next
            self.fail_next = None
            return httpx.Response(status, json={"success": False, "data": None, "meta": {"message": msg}})

        routes = [
            ("GET", r"/locations/(\d+)/team_members", self._team_members),
            ("GET", r"/locations/(\d+)/services", self._services),
            ("GET", r"/timetable/dates/(\d+)/([\d-]+)", self._dates),
            ("GET", r"/timetable/seances/(\d+)/(\d+)/([\d-]+)", self._seances),
            ("GET", r"/book_dates/(\d+)", self._book_dates),
            ("GET", r"/book_times/(\d+)/(\d+)/([\d-]+)", self._book_times),
            ("GET", r"/locations/(\d+)/appointments", self._list_appointments),
            ("POST", r"/locations/(\d+)/appointments", self._create),
            ("GET", r"/locations/(\d+)/appointments/(\d+)", self._get_appointment),
            ("PUT", r"/locations/(\d+)/appointments/(\d+)", self._update),
            ("POST", r"/locations/(\d+)/appointments/(\d+)/attendance", self._attendance),
            ("GET", r"/client/(\d+)/(\d+)", self._client),
        ]
        for method, pattern, fn in routes:
            m = re.fullmatch(pattern, path)
            if m and method == request.method:
                status, payload = fn(params, body, *m.groups())
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"success": False, "meta": {"message": "not found"}})

    @staticmethod
    def _ok(data, status=200, **extra):
        return status, {"success": True, "data": data, "meta": {}, **extra}

    def _loc(self, loc):
        return self.fx["locations"].get(str(loc), {})

    def _team_members(self, params, body, loc):
        return self._ok(self._loc(loc).get("team_members", []))

    def _services(self, params, body, loc):
        staff = params.get("staff_id") or params.get("team_member_id")
        by_staff = self._loc(loc).get("services_by_staff", {})
        if staff:
            return self._ok(by_staff.get(str(staff), []))
        return self._ok([s for items in by_staff.values() for s in items])

    def _works(self, day: date) -> bool:
        return day.weekday() not in self.fx["schedule"].get("days_off", [])

    def _dates(self, params, body, loc, day):
        start = date.fromisoformat(day)
        return self._ok([(start + timedelta(days=i)).isoformat() for i in range(31)
                         if self._works(start + timedelta(days=i))])

    def _service_length(self, loc, staff, service_id):
        for svc in self._loc(loc).get("services_by_staff", {}).get(str(staff), []):
            attrs = svc.get("attributes", svc)
            if str(svc.get("id")) == str(service_id):
                return int(attrs.get("seance_length") or attrs.get("duration") or attrs.get("length") or 3600)
        return 3600

    def _book_times(self, params, body, loc, staff, day):
        from altegio import compute_free_starts
        d = date.fromisoformat(day)
        service = params.get("service_ids[]")
        length = self._service_length(loc, staff, service) if service else 3600
        grid = self._seances(params, body, loc, staff, day)[1]["data"]
        starts = compute_free_starts(grid, d, length, self.tz, now=datetime.now(self.tz))
        return self._ok([{"time": s.strftime("%H:%M"), "seance_length": length, "sum_length": length,
                          "datetime": s.isoformat()} for s in starts])

    def _book_dates(self, params, body, loc):
        staff = params.get("staff_id")
        today = datetime.now(self.tz).date()
        working, booking = [], []
        for i in range(62):
            d = today + timedelta(days=i)
            if not self._works(d):
                continue
            working.append(d.isoformat())
            if staff and self._book_times(params, body, loc, staff, d.isoformat())[1]["data"]:
                booking.append(d.isoformat())
        return self._ok({"booking_dates": booking, "working_dates": working})

    def _seances(self, params, body, loc, staff, day):
        d = date.fromisoformat(day)
        if not self._works(d):
            return self._ok([])
        sch = self.fx["schedule"]
        step = int(sch.get("step_minutes", 5))
        busy = []
        for b in self.fx.get("busy", []):
            if str(b["staff_id"]) == staff and d.weekday() in b.get("weekdays", []):
                busy.append((_minutes(b["from"]), _minutes(b["to"])))
        for a in self.appointments.values():
            if a["deleted"] or str(a["staff_id"]) != staff or a["_loc"] != int(loc):
                continue
            s = datetime.fromisoformat(a["datetime"]).astimezone(self.tz)
            if s.date() == d:
                m = s.hour * 60 + s.minute
                busy.append((m, m + a["seance_length"] // 60))
        cells = []
        for m in range(_minutes(sch["open"]), _minutes(sch["close"]), step):
            free = not any(lo <= m < hi for lo, hi in busy)
            cells.append({"time": "%02d:%02d" % (m // 60, m % 60), "is_free": free})
        return self._ok(cells)

    def _wire(self, a):
        out = {k: v for k, v in a.items() if k != "_loc"}
        return {"type": "record", "id": str(a["id"]), "attributes": out,
                "relationships": {"client": {"data": {"type": "client", "id": str(a["client_id"])}
                                             if a["client_id"] else None}}}

    def _list_appointments(self, params, body, loc):
        # Like the real API: whole days from start_date..end_date (inclusive); `filter[...]` is
        # NOT supported there (it is silently ignored and the full history comes back).
        lo_day, hi_day = params.get("start_date"), params.get("end_date")
        rows, included = [], []
        for a in self.appointments.values():
            if a["_loc"] != int(loc) or a["deleted"]:
                continue
            if params.get("client_id") and str(a["client_id"]) != params["client_id"]:
                continue
            day = a["datetime"][:10]
            if (not lo_day or day >= lo_day) and (not hi_day or day <= hi_day):
                rows.append(self._wire(a))
                c = self.clients.get((int(loc), a["client_id"]))
                if c and "client" in params.get("include", ""):
                    included.append({"type": "client", "id": str(c["id"]),
                                     "attributes": {"name": c["name"], "phone": c["phone"]}})
        page, limit = int(params.get("page", 1)), int(params.get("count", params.get("limit", 50)))
        limit = min(limit, self.page_cap or limit)
        return self._ok(rows[(page - 1) * limit: page * limit], included=included)

    def _busy_conflict(self, loc, staff, start, length, ignore=None):
        for a in self.appointments.values():
            if a["deleted"] or a["id"] == ignore or a["_loc"] != int(loc) or a["staff_id"] != int(staff):
                continue
            s = datetime.fromisoformat(a["datetime"])
            if s < start + timedelta(seconds=length) and start < s + timedelta(seconds=a["seance_length"]):
                return True
        return False

    def _create(self, params, body, loc):
        for key in ("staff_id", "services", "client", "datetime", "seance_length"):
            if key not in body:
                return 422, {"success": False, "meta": {"message": "%s is required" % key}}
        start = datetime.fromisoformat(body["datetime"])
        if self._busy_conflict(loc, body["staff_id"], start, body["seance_length"]):
            return 422, {"success": False, "meta": {"message": "Team member is busy at this time"}}
        client = body["client"]
        appt_id = self.add_appointment(loc, body["staff_id"], start, body["seance_length"],
                                       phone=client.get("phone", ""), name=client.get("name", ""),
                                       services=[{"id": s["id"]} for s in body["services"]])
        a = self.appointments[appt_id]
        a.update(comment=body.get("comment", ""), api_id=body.get("api_id", ""))
        return self._ok([{**{k: v for k, v in a.items() if k != "_loc"},
                          "client": {"phone": client.get("phone", ""), "name": client.get("name", "")}}], 201)

    def _get_appointment(self, params, body, loc, appt_id):
        a = self.appointments.get(int(appt_id))
        if not a or a["_loc"] != int(loc):
            return 404, {"success": False, "meta": {"message": "Appointment not found"}}
        return self._ok(self._wire(a))

    def _update(self, params, body, loc, appt_id):
        a = self.appointments.get(int(appt_id))
        if not a:
            return 404, {"success": False, "meta": {"message": "Appointment not found"}}
        start = datetime.fromisoformat(body["datetime"])
        if self._busy_conflict(loc, body["staff_id"], start, body["seance_length"], ignore=a["id"]):
            return 422, {"success": False, "meta": {"message": "Team member is busy at this time"}}
        a.update(datetime=start.astimezone(self.tz).isoformat(), staff_id=int(body["staff_id"]),
                 seance_length=body["seance_length"], services=body["services"],
                 comment=body.get("comment", ""), api_id=body.get("api_id", ""),
                 attendance=body.get("attendance", 0))
        return self._ok({k: v for k, v in a.items() if k != "_loc"})

    def _attendance(self, params, body, loc, appt_id):
        a = self.appointments.get(int(appt_id))
        if not a:
            return 404, {"success": False, "meta": {"message": "Appointment not found"}}
        a["attendance"] = body["attendance"]
        return self._ok({"attendance": body["attendance"], "records": []})

    def _client(self, params, body, loc, client_id):
        c = self.clients.get((int(loc), int(client_id)))
        if not c:
            return 404, {"success": False, "meta": {"message": "Client not found"}}
        return self._ok(c)


def fixture_client(fixture: Optional[dict] = None, tz: str = "Europe/Kyiv"):
    """HttpAltegioClient wired to a FixtureBackend. The backend is on `client.backend`."""
    backend = FixtureBackend(fixture, tz)
    client = HttpAltegioClient("mock-partner", "mock-user", tz=tz,
                               transport=httpx.MockTransport(backend.handler),
                               min_interval=0, is_mock=True)
    client.backend = backend
    return client
