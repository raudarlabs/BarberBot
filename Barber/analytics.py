"""
Shop analytics from Altegio — for the dashboard (public/stats.html), the bot's /admin summary
and a barber's own numbers.

Three Altegio sources, each answering a different question:
- appointments → bookings: visits, no-shows, sources, barbers, services, busy hours, the coming
  week. "Revenue" here is the price of the services done (after discounts).
- transactions → money actually moved: income by item and account, expenses by item, profit,
  salaries paid. Transfers between accounts (no payment item) are left out.
- analytics/overall → Altegio's own report: income, average check, occupancy, new / returning /
  lost clients, cancellations — the numbers the owner sees in Altegio.

Every appointment is reduced to a small record:
    {"s": staff_id, "n": staff name, "t": start ISO, "st": arrived|no_show|waiting|confirmed,
     "c": cost after discounts, "k": client_id, "src": bot|online|admin, "sv": [[title, cost], ...]}
Altegio returns whole days and a month is thousands of rows, so closed days (older than
FRESH_DAYS — by then visits are marked and paid) are cached in the store per location and day;
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
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from altegio import AltegioError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import _kv  # noqa: E402

logger = logging.getLogger(__name__)

DAY_KEY = "bb:an:%d:%s"   # location, YYYY-MM-DD -> JSON list of appointment records
TX_KEY = "bb:tx:%d:%s"    # location, YYYY-MM-DD -> JSON list of transactions
FRESH_DAYS = 7
DAY_TTL = 200 * 86400
CHUNK_DAYS = 10           # days per Altegio request run (keeps each run well under the page cap)
MAX_DAYS = 200
SOURCES = ("bot", "online", "admin")

# Altegio's system payment items (ids are the same in every account) and the shop's own ones.
SERVICES_ITEM, GOODS_ITEM, SALARY_ITEM, CERTIFICATE_ITEM = 5, 7, 3, 12
INCOME_ITEMS = {5: "Послуги", 7: "Товари", 12: "Сертифікати", 8: "Інші доходи"}
ITEM_LABELS = {3: "Зарплати команди", 4: "Податки й збори", 9: "Інші витрати", 1: "Інші витрати"}
TITLE_UK = {
    "Реклама, типография": "Реклама, друк", "Комиссия банка": "Комісія банку",
    "Хозяйственные затраты": "Господарські витрати", "Аренда офиса": "Оренда",
    "Аренда": "Оренда", "Коммуналка": "Комуналка", "Канцелярия": "Канцелярія",
    "Services rendered": "Послуги", "Product sales": "Товари", "Gift card sales": "Сертифікати",
    "Team salaries": "Зарплати команди", "Taxes and fees": "Податки й збори",
    "Miscellaneous income": "Інші доходи", "Miscellaneous expenses": "Інші витрати",
}
ACCOUNT_UK = {"Основная касса": "Основна каса", "Расчетный счет": "Розрахунковий рахунок", "Сейф": "Сейф"}


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


async def _by_day(key: str, loc: int, first: date, last: date, today: date,
                  fetch: Callable[[date, date], Awaitable[Dict[date, list]]]) -> Dict[date, list]:
    """{day: [items]} for the range: closed days from the store, the rest from `fetch(lo, hi)`."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    closed = [d for d in days if (today - d).days > FRESH_DAYS]
    out: Dict[date, list] = {}
    if closed:
        try:
            for d, v in zip(closed, await _kvs(*[["GET", key % (loc, d.isoformat())] for d in closed])):
                if v is not None:
                    out[d] = json.loads(v)
        except Exception as e:  # noqa: BLE001 — the cache is an optimisation
            logger.warning("analytics cache read: %s", e)
    for lo, hi in _runs([d for d in days if d not in out], CHUNK_DAYS):
        got = await fetch(lo, hi)
        writes = []
        for i in range((hi - lo).days + 1):
            d = lo + timedelta(days=i)
            out[d] = got.get(d, [])
            if (today - d).days > FRESH_DAYS:
                writes.append(["SET", key % (loc, d.isoformat()),
                               json.dumps(out[d], ensure_ascii=False, separators=(",", ":")), "EX", DAY_TTL])
        if writes:
            try:
                await _kvs(*writes)
            except Exception as e:  # noqa: BLE001
                logger.warning("analytics cache write: %s", e)
    return out


async def day_records(client, loc: int, first: date, last: date, today: date) -> Dict[date, list]:
    async def fetch(lo, hi):
        start = datetime(lo.year, lo.month, lo.day, tzinfo=client.tz)
        got: Dict[date, list] = {}
        for a in await client.list_appointments(loc, start, start + timedelta(days=(hi - lo).days + 1)):
            if a.status != "cancelled" and lo <= a.start.date() <= hi:
                got.setdefault(a.start.date(), []).append(to_record(a))
        return got
    return await _by_day(DAY_KEY, loc, first, last, today, fetch)


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


async def transactions(client, locations: Iterable[int], first: date, last: date, today: date) -> List[dict]:
    out: List[dict] = []
    for loc in locations:
        async def fetch(lo, hi, loc=loc):
            got: Dict[date, list] = {}
            for t in await client.list_transactions(loc, lo, hi):
                got.setdefault(date.fromisoformat(t["d"]), []).append(t)
            return got
        by_day = await _by_day(TX_KEY, loc, first, last, today, fetch)
        out += [t for d in sorted(by_day) for t in by_day[d]]
    return out


# ------------------------------------------------------------------ periods

def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_month(d: date) -> date:
    return (_month_start(d) - timedelta(days=1)).replace(day=1)


MONTHS_UK = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень",
             "вересень", "жовтень", "листопад", "грудень"]
FIRST_MONTH = date(2023, 1, 1)  # the earliest month the dashboard offers


def month_param(p: str, today: date) -> Optional[date]:
    """'YYYY-MM' → the first day of that month, if it is between FIRST_MONTH and this month."""
    try:
        y, m = (int(x) for x in str(p).split("-"))
        d = date(y, m, 1)
    except (TypeError, ValueError):
        return None
    return d if FIRST_MONTH <= d <= _month_start(today) else None


def period(p: str, today: date) -> dict:
    """p: 7 | 30 | 90 | month | prev | YYYY-MM → current and comparison ranges (inclusive dates)."""
    picked = month_param(p, today) if "-" in str(p) else None
    if picked == _month_start(today):
        p = "month"
    elif picked is not None:
        first = picked
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        pf, pl = _prev_month(first), first - timedelta(days=1)
        return {"first": first, "last": last, "prev_first": pf, "prev_last": pl,
                "label": "%s %d" % (MONTHS_UK[first.month - 1].capitalize(), first.year)}
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


# ------------------------------------------------------------------ bookings

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
                                        "revenue": 0.0, "no_show": 0, "unmarked": 0, "bot": 0, "online": 0,
                                        "booked": 0, "services": 0, "_clients": Counter(), "_days": set()})
        t["booked"] += 1
        b["booked"] += 1
        daily["booked"][i] += 1
        sources[r["src"]] += 1
        b["bot"] += r["src"] == "bot"
        b["online"] += r["src"] == "online"
        st = r["st"]
        if st == "arrived":
            t["visits"] += 1
            t["revenue"] += r["c"]
            t["services"] += len(r["sv"])
            daily["visits"][i] += 1
            daily["revenue"][i] += r["c"]
            b["visits"] += 1
            b["revenue"] += r["c"]
            b["services"] += len(r["sv"])
            b["_days"].add(d)
            heat[start.weekday()][start.hour] += 1
            if r.get("k"):
                clients[r["k"]] += 1
                b["_clients"][r["k"]] += 1
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
        cl = b.pop("_clients")
        b["clients"] = len(cl)
        b["repeat"] = sum(1 for n in cl.values() if n > 1)
        b["days"] = len(b.pop("_days"))
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
            "services": t["services"],
            "barber_days": sum(b["days"] for b in rows),
            "online_share": round(100 * (sources["online"] + sources["bot"]) / t["booked"], 1) if t["booked"] else 0,
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
    """Bookings: current period, comparison period and the coming days in one Altegio pass (one
    barber's when `staff_id` is given)."""
    per = period(p, now.date())
    locations = list(locations)
    first = min(per["prev_first"], per["first"])
    today, until = now.date(), now.date() + timedelta(days=ahead)
    if per["last"] >= today - timedelta(days=1):  # one contiguous range up to the coming week
        recs = await records(client, locations, first, until, today)
    else:  # an older month: that range, and the coming week separately
        recs = await records(client, locations, first, per["last"], today)
        recs += await records(client, locations, today, until, today)
    cur = summarize(recs, per["first"], per["last"], now, staff_id)
    prev = summarize(recs, per["prev_first"], per["prev_last"], now, staff_id)
    return {
        "label": per["label"], "current": cur,
        "previous": {"first": prev["first"], "last": prev["last"], "totals": prev["totals"],
                     "sources": prev["sources"], "daily": prev["daily"], "barbers": prev["barbers"]},
        "upcoming": upcoming(recs, now, ahead, staff_id),
        "_records": recs,
        "_period": per,
    }


# ------------------------------------------------------------------ money

def item_label(t: dict) -> str:
    if t["c"] in INCOME_ITEMS and t["c"] != 8:
        return INCOME_ITEMS[t["c"]]
    if t["c"] in ITEM_LABELS:
        return ITEM_LABELS[t["c"]]
    title = (t.get("cat") or "").strip()
    return TITLE_UK.get(title, title or "Без статті")


def money_summary(txs: List[dict], first: date, last: date, accounts: Optional[Dict[int, dict]] = None) -> dict:
    """Money in and out: by item, by account, per day; salaries paid per team member."""
    accounts = accounts or {}
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    index = {d.isoformat(): i for i, d in enumerate(days)}
    income, expense, by_acc, salaries = Counter(), Counter(), Counter(), Counter()
    daily_in, daily_out = [0.0] * len(days), [0.0] * len(days)
    cash = 0.0
    for t in txs:
        i = index.get(t["d"])
        if i is None or not t["c"]:  # outside the period, or a transfer between accounts
            continue
        a = t["a"]
        if t["c"] in INCOME_ITEMS or (a > 0 and t["c"] not in ITEM_LABELS and t["c"] != SALARY_ITEM):
            income[item_label(t)] += a          # a refund is a negative income line
            daily_in[i] += a
            acc = accounts.get(t["acc"], {})
            by_acc[ACCOUNT_UK.get(acc.get("title", ""), acc.get("title") or "Інший рахунок")] += a
            cash += a if acc.get("cash", True) else 0
        else:
            expense[item_label(t)] += -a
            daily_out[i] += -a
            if t["c"] == SALARY_ITEM and t["m"]:
                salaries[t["m"]] += -a
    inc, exp = sum(income.values()), sum(expense.values())
    return {
        "totals": {
            "income": round(inc), "expense": round(exp), "profit": round(inc - exp),
            "margin": round(100 * (inc - exp) / inc, 1) if inc else 0,
            "services": round(income.get("Послуги", 0)), "goods": round(income.get("Товари", 0)),
            "salaries": round(expense.get("Зарплати команди", 0)),
            "cash_share": round(100 * cash / inc, 1) if inc else 0,
        },
        "income": [{"title": k, "sum": round(v)} for k, v in income.most_common() if round(v)],
        "expenses": [{"title": k, "sum": round(v)} for k, v in expense.most_common() if round(v)],
        "accounts": [{"title": k, "sum": round(v)} for k, v in by_acc.most_common() if round(v)],
        "daily": {"income": [round(x) for x in daily_in], "expense": [round(x) for x in daily_out]},
        "salaries": {str(k): round(v) for k, v in salaries.items()},
    }


# ------------------------------------------------------------------ Altegio's own report

async def overall(client, locations: Iterable[int], first: date, last: date,
                  staff_id: Optional[int] = None) -> Optional[dict]:
    """analytics/overall merged over the locations (counts and money summed, percents averaged)."""
    parts = []
    for loc in locations:
        try:
            parts.append(await client.analytics_overall(loc, first, last, staff_id))
        except AltegioError as e:
            logger.warning("analytics overall %s/%s: %s", loc, staff_id, e)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    out = {}
    for k in parts[0]:
        vals = [p[k] for p in parts]
        out[k] = round(sum(vals) / len(vals), 1) if k.endswith("percent") or k == "occupancy" else sum(vals)
    out["avg_check"] = round(out["income_total"] / out["completed"], 2) if out.get("completed") else 0
    return out


# ------------------------------------------------------------------ client ratings (reviews.py)

RATINGS_KEY = "bb:ratings"


def ratings_between(first: date, last: date, staff_id: Optional[int] = None) -> List[dict]:
    raw = _kv.pipeline(["HGETALL", RATINGS_KEY])[0] or []
    out = []
    for i in range(1, len(raw), 2):
        try:
            r = json.loads(raw[i])
        except (TypeError, ValueError):
            continue
        if first.isoformat() <= r.get("d", "") <= last.isoformat() and (staff_id is None or r.get("s") == staff_id):
            out.append(r)
    return out


def rating_stats(rs: List[dict]) -> dict:
    by: Dict[int, list] = {}
    for r in rs:
        by.setdefault(r.get("s") or 0, []).append(r["r"])
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    return {"avg": avg([r["r"] for r in rs]), "count": len(rs), "low": sum(1 for r in rs if r["r"] <= 3),
            "by_staff": {str(k): {"avg": avg(v), "count": len(v)} for k, v in by.items()}}


# ------------------------------------------------------------------ KPIs and the team

def _kpi(key, label, value, prev, fmt, good, hint, money=False):
    return {"key": key, "label": label, "value": value, "prev": prev, "fmt": fmt, "good": good,
            "hint": hint, "money": money}


def _per(v, n, digits=0):
    return round(v / n, digits) if n else 0


def kpis(book: dict, money: Optional[dict], ov: Optional[dict], ov_prev: Optional[dict],
         rating: Optional[dict] = None, rating_prev: Optional[dict] = None) -> List[dict]:
    """The KPI cards: value, previous period, which direction is good, and how it is counted."""
    c, p = book["current"]["totals"], book["previous"]["totals"]
    nd, npd = len(book["current"]["days"]), max(1, (date.fromisoformat(book["previous"]["last"])
                                                     - date.fromisoformat(book["previous"]["first"])).days + 1)
    ov, ovp = ov or {}, ov_prev or {}
    out = []
    if ov:
        out += [
            _kpi("revenue", "Виручка", ov["income_total"], ovp.get("income_total"), "money", "up",
                 "Звіт Altegio: оплачені послуги й товари", True),
            _kpi("avg_check", "Середній чек", ov["avg_check"], ovp.get("avg_check"), "money", "up",
                 "Виручка ÷ кількість оплачених візитів (Altegio)", True),
            _kpi("revenue_day", "Виручка за день", _per(ov["income_total"], nd), _per(ovp.get("income_total", 0), npd),
                 "money", "up", "Виручка ÷ дні періоду", True),
        ]
    out += [
        _kpi("visits_day", "Візитів за день", _per(c["visits"], nd, 1), _per(p["visits"], npd, 1), "num1", "up",
             "Візити зі статусом «прийшов» ÷ дні періоду"),
    ]
    if ov:
        out += [
            _kpi("occupancy", "Завантаженість", ov["occupancy"], ovp.get("occupancy"), "pct", "up",
                 "Частка робочого часу майстрів, зайнята записами (Altegio)"),
            _kpi("new_clients", "Нові клієнти", ov["new_clients"], ovp.get("new_clients"), "int", "up",
                 "Прийшли вперше за цей період (Altegio)"),
            _kpi("return", "Повертаються", ov["return_percent"], ovp.get("return_percent"), "pct", "up",
                 "Частка клієнтів, які вже бували раніше (Altegio)"),
            _kpi("lost", "Втрачені клієнти", ov["lost_clients"], ovp.get("lost_clients"), "int", "down",
                 "Давно не приходили — за правилом Altegio"),
            _kpi("cancelled", "Скасування", ov["cancelled_percent"], ovp.get("cancelled_percent"), "pct", "down",
                 "Частка скасованих записів (Altegio)"),
        ]
    out += [
        _kpi("no_show", "Неявки", c["no_show_rate"], p["no_show_rate"], "pct", "down",
             "Записи «не прийшов» ÷ (візити + неявки)"),
        _kpi("online", "Онлайн-запис", c["online_share"], p["online_share"], "pct", "up",
             "Записалися самі: онлайн-запис Altegio і бот"),
        _kpi("services_visit", "Послуг на візит", _per(c["services"], c["visits"], 2),
             _per(p["services"], p["visits"], 2), "num2", "up", "Скільки послуг у середньому за один візит"),
    ]
    if rating and rating.get("count"):
        out.append(_kpi("rating", "Оцінка клієнтів", rating["avg"], (rating_prev or {}).get("avg"), "num2", "up",
                        "Середня оцінка після візиту в боті: %d оцінок, з них %d на 3 і нижче"
                        % (rating["count"], rating["low"])))
    if ov:
        out.append(_kpi("barber_day", "Виручка майстра за день", _per(c["revenue"], c["barber_days"]),
                        _per(p["revenue"], p["barber_days"]), "money", "up",
                        "Вартість послуг ÷ дні, коли майстер мав візити", True))
    if money:
        m, mp = money["current"]["totals"], money["previous"]["totals"]
        out += [
            _kpi("profit", "Прибуток", m["profit"], mp["profit"], "money", "up",
                 "Надходження − витрати за касами Altegio", True),
            _kpi("margin", "Маржа", m["margin"], mp["margin"], "pct", "up", "Прибуток ÷ надходження", True),
            _kpi("salary_share", "Зарплати / надходження", _per(100 * m["salaries"], m["income"], 1),
                 _per(100 * mp["salaries"], mp["income"], 1), "pct", "down",
                 "Виплати команді ÷ надходження (виплати за період, а не нараховано)", True),
            _kpi("goods", "Продаж товарів", m["goods"], mp["goods"], "money", "up", "Надходження за товари", True),
        ]
    return out


def team(book: dict, money: Optional[dict], ov_staff: Dict[int, dict], ov_staff_prev: Dict[int, dict],
         rating: Optional[dict] = None) -> List[dict]:
    """One row per barber with visits in either period: performance and its change."""
    cur = {b["id"]: b for b in book["current"]["barbers"]}
    prev = {b["id"]: b for b in book["previous"].get("barbers", [])}
    total_rev = sum(b["revenue"] for b in cur.values()) or 0
    salaries = (money or {}).get("current", {}).get("salaries", {})
    rows = []
    for sid in list(cur) + [s for s in prev if s not in cur]:
        b, pb = cur.get(sid, {}), prev.get(sid, {})
        o, op = ov_staff.get(sid) or {}, ov_staff_prev.get(sid) or {}
        rev, visits, days = b.get("revenue", 0), b.get("visits", 0), b.get("days", 0)
        salary = salaries.get(str(sid))
        rt = ((rating or {}).get("by_staff") or {}).get(str(sid)) or {}
        rows.append({
            "rating": rt.get("avg"), "ratings": rt.get("count", 0),
            "id": sid, "name": b.get("name") or pb.get("name") or "#%s" % sid,
            "revenue": rev, "revenue_prev": pb.get("revenue", 0),
            "share": round(100 * rev / total_rev, 1) if total_rev else 0,
            "visits": visits, "visits_prev": pb.get("visits", 0),
            "avg": b.get("avg", 0), "avg_prev": pb.get("avg", 0),
            "days": days, "rev_day": _per(rev, days), "rev_day_prev": _per(pb.get("revenue", 0), pb.get("days", 0)),
            "visits_day": _per(visits, days, 1),
            "clients": b.get("clients", 0), "repeat": b.get("repeat", 0),
            "no_show": b.get("no_show", 0), "unmarked": b.get("unmarked", 0),
            "online": _per(100 * (b.get("online", 0) + b.get("bot", 0)), b.get("booked", 0), 1),
            "occupancy": o.get("occupancy"), "occupancy_prev": op.get("occupancy"),
            "new_clients": o.get("new_clients"), "return_percent": o.get("return_percent"),
            "salary": salary, "salary_share": _per(100 * salary, rev, 1) if salary else None,
        })
    rows.sort(key=lambda r: (-r["revenue"], -r["visits"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


async def list_accounts(client, locations: Iterable[int]) -> Dict[int, dict]:
    out = {}
    for loc in locations:
        try:
            for a in await client.list_accounts(loc):
                out[a["id"]] = a
        except AltegioError as e:
            logger.warning("accounts %s: %s", loc, e)
    return out


async def full_report(client, locations: Iterable[int], p: str, now: datetime,
                      staff_id: Optional[int] = None, with_money: bool = True, with_team: bool = True) -> dict:
    """Everything the dashboard shows. `staff_id`: one barber. `with_money`: transactions and the
    money KPIs. `with_team`: Altegio's report per barber for the team page."""
    locations = list(locations)
    book = await report(client, locations, p, now, staff_id=staff_id)
    per = book.pop("_period")
    recs = book.pop("_records")
    del recs
    ov = await overall(client, locations, per["first"], per["last"], staff_id)
    ov_prev = await overall(client, locations, per["prev_first"], per["prev_last"], staff_id)
    money = None
    if with_money:
        first = min(per["prev_first"], per["first"])
        txs = await transactions(client, locations, first, per["last"], now.date())
        accounts = await list_accounts(client, locations)
        cur_m = money_summary(txs, per["first"], per["last"], accounts)
        prev_m = money_summary(txs, per["prev_first"], per["prev_last"], accounts)
        if staff_id is not None:  # one barber: only what was paid to him
            for part in (cur_m, prev_m):
                part["salaries"] = {k: v for k, v in part["salaries"].items() if k == str(staff_id)}
        money = {"current": cur_m, "previous": {"totals": prev_m["totals"]}}
    try:
        rating = rating_stats(await asyncio.to_thread(ratings_between, per["first"], per["last"], staff_id))
        rating_prev = rating_stats(await asyncio.to_thread(ratings_between, per["prev_first"], per["prev_last"], staff_id))
    except Exception as e:  # noqa: BLE001 — ratings are an extra
        logger.warning("ratings: %s", e)
        rating = rating_prev = None
    out = {"label": per["label"], "bookings": book, "overall": {"current": ov, "previous": ov_prev},
           "money": money, "rating": rating, "kpi": kpis(book, money, ov, ov_prev, rating, rating_prev)}
    if with_team and staff_id is None:
        ids = sorted({b["id"] for b in book["current"]["barbers"]} | {b["id"] for b in book["previous"]["barbers"]})
        cur_s, prev_s = {}, {}
        for sid in ids:
            cur_s[sid] = await overall(client, locations, per["first"], per["last"], sid)
            prev_s[sid] = await overall(client, locations, per["prev_first"], per["prev_last"], sid)
        out["team"] = team(book, money, cur_s, prev_s, rating)
    return out


def hide_money(rep: dict) -> dict:
    """A barber's view without money (unless the owner turns it on). Works on a bookings report
    ({current, previous, upcoming}) and on a full report ({bookings, money, kpi, overall})."""
    if "bookings" in rep:
        hide_money(rep["bookings"])
        rep["money"] = None
        rep["kpi"] = [k for k in rep.get("kpi", []) if not k.get("money")]
        for part in (rep.get("overall") or {}).values():
            if part:
                for k in ("income_total", "income_services", "income_goods", "avg_check", "avg_services_check"):
                    part[k] = None
        rep.pop("team", None)
        rep["money_hidden"] = True
        return rep
    for part in (rep.get("current", {}), rep.get("previous", {})):
        for k in ("revenue", "avg_check"):
            if k in part.get("totals", {}):
                part["totals"][k] = None
        if "revenue" in part.get("daily", {}):
            part["daily"]["revenue"] = None
        for b in part.get("barbers", []):
            b["revenue"] = b["avg"] = None
    for s in rep.get("current", {}).get("services", []):
        s["revenue"] = None
    if "upcoming" in rep:
        rep["upcoming"]["expected"] = None
    rep["money_hidden"] = True
    return rep


def money(v) -> str:
    return "{:,.0f}".format(v or 0).replace(",", " ") + " ₴"
