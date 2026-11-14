"""
Visits nobody marked in Altegio (neither "client arrived" nor "no-show") — they drop out of the
statistics, the dashboard and the payroll. At 21:00 (api/cron_evening.py) each linked barber gets
his own list, and the shop's admin account (admin_notify.notify_chat_ids) gets everyone's, grouped
by barber, plus how many are still open from the last 7 days. Nothing is sent when all is marked.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import admin
from altegio import AltegioError

PAST_DAYS = 7
FOOTER = ("Відмітьте в Altegio «Клієнт прийшов» або «Не прийшов» — інакше візит не потрапить "
          "у статистику, дашборд і розрахунок зарплати.")


def _open(a, now: datetime) -> bool:
    return a.status in ("waiting", "confirmed") and a.start < now


async def collect(client, locations: Dict[int, str], now: datetime):
    """(today's unmarked visits, how many are still unmarked from the previous PAST_DAYS days)."""
    today = now.date()
    start = datetime(today.year, today.month, today.day, tzinfo=client.tz)
    todays: List = []
    older = 0
    for loc in locations:
        for a in await client.list_appointments(loc, start - timedelta(days=PAST_DAYS), now):
            if not _open(a, now):
                continue
            if a.start.date() == today:
                todays.append(a)
            else:
                older += 1
    todays.sort(key=lambda a: (a.staff_name or "", a.start))
    return todays, older


async def _names(client, locations) -> Dict[int, str]:
    out = {}
    for loc in locations:
        try:
            out.update({s.id: s.name for s in await client.list_staff(loc)})
        except AltegioError:
            pass
    return out


def _line(a) -> str:
    return "%s %s" % (a.start.strftime("%H:%M"), a.client_name or "—")


def admin_text(todays, older: int, names: Dict[int, str]) -> Optional[str]:
    if not todays and not older:
        return None
    lines = ["🌙 Не відмічено сьогодні: %d %s" % (len(todays), _visits(len(todays)))]
    by: Dict[str, list] = {}
    for a in todays:
        by.setdefault(a.staff_name or names.get(a.staff_id, "#%d" % a.staff_id), []).append(a)
    for staff, items in by.items():
        lines.append("💈 %s — %d: %s" % (staff, len(items), ", ".join(_line(a) for a in items)))
    if older:
        lines.append("\n+ ще %d %s за минулі %d днів" % (older, _visits(older), PAST_DAYS))
    lines += ["", FOOTER]
    return admin._clip("\n".join(lines))


def staff_text(items) -> Optional[str]:
    if not items:
        return None
    return admin._clip("🌙 У вас не відмічено %d %s сьогодні: %s\n\n%s" % (
        len(items), _visits(len(items)), ", ".join(_line(a) for a in items), FOOTER))


def _visits(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "візит"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "візити"
    return "візитів"
