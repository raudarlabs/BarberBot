"""
Bookings analytics from Altegio — for the admin dashboard (/stats.html), the bot's /admin
statistics and a barber's own numbers.

Every appointment is reduced to a small record:
    {"s": staff_id, "n": staff name, "t": start ISO, "st": arrived|no_show|waiting|confirmed,
     "c": cost after discounts, "k": client_id, "src": bot|online|admin, "sv": [[title, cost], ...]}

Altegio returns whole days only and a busy month is thousands of rows, so closed days (older
than FRESH_DAYS — by then visits are marked) are cached in the store per location and day;
the recent week is always fetched fresh. Cancelled (deleted) appointments are not counted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from altegio import AltegioError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

logger = logging.getLogger(__name__)

DAY_KEY = "bb:an:%d:%s"   # location, YYYY-MM-DD -> JSON list of records
FRESH_DAYS = 7
DAY_TTL = 200 * 86400
CHUNK_DAYS = 10           # days per Altegio request run (keeps each run well under the page cap)
MAX_DAYS = 200
SOURCES = ("bot", "online", "admin")


# ------------------------------------------------------------------ records

def via_bot(a) -> bool:
    return (a.api_id or "").startswith("tg-") or "Telegram" in (a.comment or "")


def source(a) -> str:
    if via_bot(a):
        return "bot"
    return "online" if getattr(a, "online", False) else "admin"


def _cost(svc) -> float:
    try:
        return float(svc.get("cost") or 0)
    except (TypeError, ValueError):
        return 0.0


def to_record(a) -> dict:
    sv = [[s.get("title") or "—", _cost(s)] for s in a.services]
    return {"s": a.staff_id, "n": a.staff_name, "t": a.start.isoformat(), "st": a.status,
            "c": round(sum(c for _, c in sv), 2), "k": a.client_id, "src": source(a), "sv": sv}


def _runs(days: List[date], size: int) -> List[Tuple[date, date]]:
    """Sorted days -> contiguous (first, last) runs of at most `size` days."""
    out: List[Tuple[date, date]] = []
    for d in sorted(days):
        if out and (d - out[-1][1]).days == 1 and (d - out[-1][0]).days < size:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


async def _kvs(*commands):
    return await asyncio.to_thread(_kv.pipeline, *commands)


async def day_records(client, loc: int, first: date, last: date, today: date) -> Dict[date, list]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    closed = [d for d in days if (today - d).days > FRESH_DAYS]
    out: Dict[date, list] = {}
    if closed:
        try:
            for d, v in zip(closed, await _kvs(*[["GET", DAY_KEY % (loc, d.isoformat())] for d in closed])):
                if v is not None:
                    out[d] = json.loads(v)
        except Exception as e:  # noqa: BLE001 — the cache is an optimisation
            logger.warning("analytics cache read: %s", e)
    for lo, hi in _runs([d for d in days if d not in out], CHUNK_DAYS):
        start = datetime(lo.year, lo.month, lo.day, tzinfo=client.tz)
        appts = await client.list_appointments(loc, start, start + timedelta(days=(hi - lo).days + 1))
        got: Dict[date, list] = {}
        for a in appts:
            if a.status != "cancelled" and lo <= a.start.date() <= hi:
                got.setdefault(a.start.date(), []).append(to_record(a))
        writes = []
        for i in range((hi - lo).days + 1):
            d = lo + timedelta(days=i)
            out[d] = got.get(d, [])
            if (today - d).days > FRESH_DAYS:
                writes.append(["SET", DAY_KEY % (loc, d.isoformat()),
                               json.dumps(out[d], ensure_ascii=False, separators=(",", ":")), "EX", DAY_TTL])
        if writes:
            try:
                await _kvs(*writes)
            except Exception as e:  # noqa: BLE001
                logger.warning("analytics cache write: %s", e)
    return out


async def records(client, locations: Iterable[int], first: date, last: date, today: date) -> List[dict]:
    """All records from `first` to `last` (inclusive) over the locations, staff names filled in."""
    out: List[dict] = []
    for loc in locations:
        by_day = await day_records(client, loc, first, last, today)
        recs = [r for d in sorted(by_day) for r in by_day[d]]
        if any(not r.get("n") for r in recs):
            try:
                names = {s.id: s.name for s in await client.list_staff(loc)}
            except AltegioError:
                names = {}
            for r in recs:
                r["n"] = r.get("n") or names.get(r["s"], "#%s" % r["s"])
        out += recs
    return out


# ------------------------------------------------------------------ periods

def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_month(d: date) -> date:
    return (_month_start(d) - timedelta(days=1)).replace(day=1)


def period(p: str, today: date) -> dict:
    """p: 7 | 30 | 90 | month | prev → current and comparison ranges (inclusive dates)."""
    if p == "month":
        first, last = _month_start(today), today
        pf = _prev_month(today)
        pl = min(pf + timedelta(days=(last - first).days), first - timedelta(days=1))
        label = "Цей місяць"
    elif p == "prev":
        first = _prev_month(today)
        last = _month_start(today) - timedelta(days=1)
        pf, pl = _prev_month(first), first - timedelta(days=1)
        label = "Минулий місяць"
    else:
        n = max(1, min(int(p) if str(p).isdigit() else 30, 90))
        first, last = today - timedelta(days=n - 1), today
        pf, pl = first - timedelta(days=n), first - timedelta(days=1)
        label = "%d днів" % n
    return {"first": first, "last": last, "prev_first": pf, "prev_last": pl, "label": label}


# ------------------------------------------------------------------ summaries

def _start(r) -> datetime:
    return datetime.fromisoformat(r["t"])


def summarize(recs: List[dict], first: date, last: date, now: datetime, staff_id: Optional[int] = None) -> dict:
    """What happened from `first` to `last` up to `now` (later slots of today count as upcoming)."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    index = {d: i for i, d in enumerate(days)}
    daily = {"visits": [0] * len(days), "revenue": [0.0] * len(days), "booked": [0] * len(days)}
    t = Counter()
    sources = Counter()
    clients = Counter()
    barbers: Dict[int, dict] = {}
    services: Dict[str, list] = {}
    heat = [[0] * 24 for _ in range(7)]
    for r in recs:
        if staff_id is not None and r["s"] != staff_id:
            continue
        start = _start(r)
        d = start.date()
        if d not in index or start >= now:
            continue
        i = index[d]
        b = barbers.setdefault(r["s"], {"id": r["s"], "name": r.get("n") or "#%s" % r["s"], "visits": 0,
                                        "revenue": 0.0, "no_show": 0, "unmarked": 0, "bot": 0, "_clients": set()})
        t["booked"] += 1
        daily["booked"][i] += 1
        sources[r["src"]] += 1
        b["bot"] += r["src"] == "bot"
        st = r["st"]
        if st == "arrived":
            t["visits"] += 1
            t["revenue"] += r["c"]
            daily["visits"][i] += 1
            daily["revenue"][i] += r["c"]
            b["visits"] += 1
            b["revenue"] += r["c"]
            heat[start.weekday()][start.hour] += 1
            if r.get("k"):
                clients[r["k"]] += 1
                b["_clients"].add(r["k"])
            for title, cost in r["sv"]:
                s = services.setdefault(title, [0, 0.0])
                s[0] += 1
                s[1] += cost
        elif st == "no_show":
            t["no_show"] += 1
            b["no_show"] += 1
        else:
            t["unmarked"] += 1
            b["unmarked"] += 1
    visits = t["visits"]
    rows = []
    for b in barbers.values():
        b["clients"] = len(b.pop("_clients"))
        b["revenue"] = round(b["revenue"])
        b["avg"] = round(b["revenue"] / b["visits"]) if b["visits"] else 0
        rows.append(b)
    rows.sort(key=lambda b: (-b["revenue"], -b["visits"], b["name"]))
    return {
        "first": first.isoformat(), "last": last.isoformat(), "days": [d.isoformat() for d in days],
        "totals": {
            "booked": t["booked"], "visits": visits, "revenue": round(t["revenue"]),
            "avg_check": round(t["revenue"] / visits) if visits else 0,
            "no_show": t["no_show"], "unmarked": t["unmarked"],
            "no_show_rate": round(100 * t["no_show"] / (visits + t["no_show"]), 1) if visits + t["no_show"] else 0,
            "clients": len(clients), "repeat_clients": sum(1 for n in clients.values() if n > 1),
            "per_day": round(visits / len(days), 1) if days else 0,
        },
        "sources": {s: sources.get(s, 0) for s in SOURCES},
        "daily": {k: [round(x) for x in v] for k, v in daily.items()},
        "barbers": rows,
        "services": [{"title": k, "count": v[0], "revenue": round(v[1])}
                     for k, v in sorted(services.items(), key=lambda kv: (-kv[1][0], kv[0]))[:12]],
        "heat": heat,
    }


def upcoming(recs: List[dict], now: datetime, days: int = 7, staff_id: Optional[int] = None) -> dict:
    """Booked from now until the end of today + `days`."""
    until = now.date() + timedelta(days=days)
    by_day: Dict[str, int] = {}
    by_barber: Dict[str, int] = Counter()
    total, expected = 0, 0.0
    for r in recs:
        if staff_id is not None and r["s"] != staff_id:
            continue
        start = _start(r)
        if start < now or start.date() > until:
            continue
        total += 1
        expected += r["c"]
        by_day[start.date().isoformat()] = by_day.get(start.date().isoformat(), 0) + 1
        by_barber[r.get("n") or "#%s" % r["s"]] += 1
    return {"count": total, "expected": round(expected), "by_day": dict(sorted(by_day.items())),
            "by_barber": dict(by_barber.most_common())}


def delta(cur, prev) -> str:
    """'+12%' / '−5%' / '' when there is nothing to compare with."""
    if not prev:
        return ""
    pct = round(100 * (cur - prev) / prev)
    return ("+%d%%" % pct) if pct >= 0 else ("−%d%%" % -pct)


async def report(client, locations: Iterable[int], p: str, now: datetime, ahead: int = 7,
                 staff_id: Optional[int] = None) -> dict:
    """Current period, comparison period and the coming days in one Altegio pass (one barber's
    when `staff_id` is given)."""
    per = period(p, now.date())
    first = min(per["prev_first"], per["first"])
    last = max(per["last"], now.date() + timedelta(days=ahead))
    first = max(first, last - timedelta(days=MAX_DAYS))
    recs = await records(client, list(locations), first, last, now.date())
    cur = summarize(recs, per["first"], per["last"], now, staff_id)
    prev = summarize(recs, per["prev_first"], per["prev_last"], now, staff_id)
    return {
        "label": per["label"], "current": cur,
        "previous": {"first": prev["first"], "last": prev["last"], "totals": prev["totals"],
                     "sources": prev["sources"], "daily": prev["daily"]},
        "upcoming": upcoming(recs, now, ahead, staff_id),
        "_records": recs,
    }


def hide_money(rep: dict) -> dict:
    """A barber's view without revenue figures (unless the owner turns them on)."""
    for part in (rep.get("current", {}), rep.get("previous", {})):
        for k in ("revenue", "avg_check"):
            if k in part.get("totals", {}):
                part["totals"][k] = None
        if "revenue" in part.get("daily", {}):
            part["daily"]["revenue"] = None
    for b in rep.get("current", {}).get("barbers", []):
        b["revenue"] = b["avg"] = None
    for s in rep.get("current", {}).get("services", []):
        s["revenue"] = None
    if "upcoming" in rep:
        rep["upcoming"]["expected"] = None
    rep["money_hidden"] = True
    return rep


def money(v) -> str:
    return "{:,.0f}".format(v or 0).replace(",", " ") + " ₴"
