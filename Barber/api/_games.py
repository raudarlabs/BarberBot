"""Per-game limits, monthly leaderboard keys (Kyiv time) and prize codes."""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone

# max_per_sec: the most points a flawless player can earn per second of play.
# A submitted score above (duration + GRACE) * max_per_sec is rejected.
# Keep these in sync with the scoring rules in public/arcade/games/*.js
# (tests/sim.mjs plays every game with a perfect bot and checks it stays under these).
# Tuned with tests/sim.mjs (perfect / elite bots, Nov 2026):
#   dryer 1.2/s · fade 16.5/s (theoretical max ~33) · beard 2.7/s (theoretical ~15)
#   snake 9/s · blocks 142/s at 3 pieces/s, 247/s at 5 pieces/s (level multiplier grows the rate)
GAMES = {
    "dryer": {"max_per_sec": 4, "max_score": 5000},
    "fade": {"max_per_sec": 40, "max_score": 100000},
    "beard": {"max_per_sec": 20, "max_score": 100000},
    "snake": {"max_per_sec": 30, "max_score": 50000},
    "blocks": {"max_per_sec": 400, "max_score": 1000000},
}

GRACE_SECONDS = 3
SESSION_TTL = 2 * 3600
RATE_LIMIT_PER_MIN = 30
PERIODS = ("month", "all")
HISTORY_SIZE = 10
FIRST_MONTH = "2026-11"  # first month with a board; history starts here


# ------------------------------------------------------------------ Kyiv calendar

def _last_sunday_utc_1am(year, month):
    d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    d -= timedelta(days=(d.weekday() + 1) % 7)
    return d.replace(hour=1)


def kyiv_offset_hours(dt_utc):
    """Ukraine: UTC+2, or UTC+3 from the last Sunday of March to the last Sunday of October (01:00 UTC)."""
    y = dt_utc.year
    return 3 if _last_sunday_utc_1am(y, 3) <= dt_utc < _last_sunday_utc_1am(y, 10) else 2


def kyiv_now(ts=None):
    utc = datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
    return (utc + timedelta(hours=kyiv_offset_hours(utc))).replace(tzinfo=None)


def month_id(ts=None):
    """Current month in Kyiv time, e.g. '2026-11'. Monthly boards roll over at local midnight on the 1st."""
    return kyiv_now(ts).strftime("%Y-%m")


def day_id(ts=None):
    """Current day in Kyiv time, e.g. '2026-11-14'."""
    return kyiv_now(ts).strftime("%Y-%m-%d")


def last_days(n, ts=None):
    """The last n Kyiv days, oldest first, ending today."""
    today = kyiv_now(ts).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def stat_keys(kind, name, ts=None):
    """Counter keys for analytics: all time, this Kyiv month, today. e.g. stats:plays:snake:2026-11."""
    base = "stats:%s:%s" % (kind, name)
    return [base, "%s:%s" % (base, month_id(ts)), "%s:%s" % (base, day_id(ts))]


def _shift(month, delta):
    y, m = map(int, month.split("-"))
    m += delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return "%04d-%02d" % (y, m)


def month_ends_at(ts=None):
    """Unix time when the current Kyiv month ends."""
    nxt = _shift(month_id(ts), 1)
    y, m = map(int, nxt.split("-"))
    local_midnight = datetime(y, m, 1, tzinfo=timezone.utc)
    guess = local_midnight - timedelta(hours=2)
    off = kyiv_offset_hours(guess)
    return int((local_midnight - timedelta(hours=off)).timestamp())


def past_months(ts=None):
    """Finished months with boards, newest first."""
    out, m, current = [], _shift(month_id(ts), -1), month_id(ts)
    while m >= FIRST_MONTH and m < current:
        out.append(m)
        m = _shift(m, -1)
    return out


def valid_month(m):
    if not isinstance(m, str) or len(m) != 7 or m[4] != "-":
        return False
    try:
        y, mm = int(m[:4]), int(m[5:])
    except ValueError:
        return False
    return 2000 < y < 3000 and 1 <= mm <= 12


def board_key(game, period, ts=None):
    """period: 'month' (current), 'all', or an explicit 'YYYY-MM'. Month boards are kept forever as history."""
    if period == "all":
        return "lb:%s:all" % game
    return "lb:%s:%s" % (game, month_id(ts) if period == "month" else period)


def max_allowed(game, duration):
    cfg = GAMES[game]
    return min(cfg["max_score"], int((max(duration, 0) + GRACE_SECONDS) * cfg["max_per_sec"]))


# ------------------------------------------------------------------ prizes

def _prize_secret():
    return (os.environ.get("ARCADE_PRIZE_SECRET") or os.environ.get("TELEGRAM_BOT_TOKEN") or "dev").encode()


def prize_code(month, game, uid):
    """Short code the monthly winner shows at the shop. Deterministic, unforgeable without the secret."""
    msg = ("%s:%s:%s" % (month, game, uid)).encode()
    digest = hmac.new(_prize_secret(), msg, hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:8]
