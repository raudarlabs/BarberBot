"""
GET /api/altegio_debug?path=/locations/297851/team_members&k=v...   (X-Admin-Key)

Staff-only diagnostics for the Altegio integration: calls the Altegio REST API with the
production tokens and returns only the SHAPE of the answer — status, envelope keys, the
type and length of `data`, the keys (and value types) of the first items. No names, phones
or other values are returned. GET requests only; paths must start with "/".
"""
import asyncio
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlparse

API_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(API_DIR)
for p in (ROOT_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx  # noqa: E402


def shape(v, depth=0):
    if isinstance(v, dict):
        if depth >= 3:
            return "dict(%d keys)" % len(v)
        return {k: shape(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return {"list_len": len(v), "first": shape(v[0], depth + 1) if v else None}
    return type(v).__name__


def aggregate(items, keys):
    """How often each value of the given (non-personal) fields occurs across the list.
    A dotted key reads a nested field (expense.title, account.title)."""
    out = {}
    for k in keys:
        counts = {}
        for it in items:
            v = it
            for part in k.split("."):  # "expense.title": nested, non-personal fields
                v = v.get(part) if isinstance(v, dict) else None
            v = json.dumps(v) if not isinstance(v, (dict, list)) else type(v).__name__
            counts[v] = counts.get(v, 0) + 1
        out[k] = counts
    return out


async def probe(path, params, agg=None):
    partner, user = os.environ.get("ALTEGIO_PARTNER_TOKEN", ""), os.environ.get("ALTEGIO_USER_TOKEN", "")
    headers = {"Authorization": "Bearer %s, User %s" % (partner, user),
               "Accept": "application/vnd.api.v2+json", "User-Agent": "barber-telegram-bot"}
    async with httpx.AsyncClient(base_url="https://api.alteg.io/api/v1", timeout=20) as c:
        r = await c.get(path, params=params, headers=headers)
    try:
        body = r.json()
    except ValueError:
        return {"status": r.status_code, "content_type": r.headers.get("content-type"), "text_len": len(r.text)}
    out = {"status": r.status_code, "envelope": shape(body)}
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list) and data and all(isinstance(x, (str, int, float)) for x in data[:20]):
        out["sample"] = data[:60]  # dates / times only, never personal data
    if isinstance(data, list) and data and isinstance(data[0], dict) and set(data[0]) <= {
            "time", "is_free", "seance_length", "datetime", "sum_length"}:
        out["sample"] = data[:60]  # free-slot grid: times and flags only
    if agg and isinstance(data, list):
        out["values"] = aggregate(data, agg)
    if isinstance(body, dict) and isinstance(body.get("meta"), dict):
        out["meta"] = {k: (v if isinstance(v, (int, bool)) or k == "message" else type(v).__name__)
                       for k, v in body["meta"].items()}
    return out


async def bot_view(location_id):
    """What the bot's own Altegio client sees: barbers, services, nearest free slots, appointment count."""
    from datetime import datetime, timedelta
    from altegio import make_client
    client = make_client()
    if client is None:
        return {"altegio": "not configured"}
    try:
        now = datetime.now(client.tz)
        staff = await client.list_staff(location_id)
        out = {"staff": [s.name for s in staff], "per_staff": {}}
        for s in staff:
            services = await client.list_services(location_id, s.id)
            entry = {"services": len(services), "online_days": [], "first_day_times": []}
            if services:
                days = await client.booking_dates(location_id, s.id, [services[0].id], now.date(), 14)
                entry["online_days"] = [d.strftime("%d.%m") for d in days]
                if days:
                    times = await client.booking_times(location_id, s.id, days[0], [services[0].id])
                    entry["first_day_times"] = [t.strftime("%H:%M") for t in times][:8]
                entry["service"] = services[0].title
            out["per_staff"][s.name] = entry
        appts = await client.list_appointments(location_id, now, now + timedelta(days=1))
        out["appointments_next_24h"] = len(appts)
        return out
    finally:
        await client.aclose()


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        key = os.environ.get("ARCADE_ADMIN_KEY", "")
        if not key or not hmac.compare_digest(self.headers.get("X-Admin-Key", "").encode(), key.encode()):
            return self._reply(403, {"error": "forbidden"})
        q = dict(parse_qsl(urlparse(self.path).query))
        if q.get("view") == "bot":
            try:
                return self._reply(200, asyncio.run(bot_view(int(q.get("location", "297851")))))
            except Exception as e:  # noqa: BLE001
                return self._reply(502, {"error": "%s: %s" % (type(e).__name__, str(e)[:200])})
        path = q.pop("path", "")
        agg = [k for k in q.pop("agg", "").split(",")
               if k and not set(k.split(".")) & {"name", "phone", "email", "user", "client", "comment", "fullname"}]
        if not path.startswith("/") or ".." in path:
            return self._reply(400, {"error": "path"})
        try:
            self._reply(200, asyncio.run(probe(path, q, agg)))
        except Exception as e:  # noqa: BLE001
            self._reply(502, {"error": type(e).__name__})
