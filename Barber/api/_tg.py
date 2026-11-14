"""
Telegram Mini App auth: validate the signed `initData` string the client sends.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    def __init__(self, reason="unauthorized"):
        super().__init__(reason)
        self.reason = reason


def failure_reason(init_data, bot_token, max_age=MAX_AGE_SECONDS, now=None):
    """Why validation failed, for logs: no_init_data, no_bot_token, no_hash, bad_hash, expired, no_user."""
    if not init_data:
        return "no_init_data"
    if not bot_token:
        return "no_bot_token"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return "unparsable"
    if "hash" not in pairs:
        return "no_hash"
    if validate_init_data(init_data, bot_token, max_age=0) is None:
        return "bad_hash" if "user" in pairs else "no_user"
    return "expired"


def validate_init_data(init_data, bot_token, max_age=MAX_AGE_SECONDS, now=None):
    """Return the Telegram user dict if `init_data` is authentic, else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None

    check_string = "\n".join(k + "=" + pairs[k] for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    now = time.time() if now is None else now
    if max_age and now - auth_date > max_age:
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def current_user(init_data, required=True):
    """
    Resolve the player from the request's initData.
    In local dev (ARCADE_DEV=1) an empty initData maps to a fixed dev player so
    the arcade can be tested in a normal browser.
    """
    if not init_data and os.environ.get("ARCADE_DEV") == "1":
        return {"id": 1, "first_name": "Dev", "last_name": "Player"}

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    user = validate_init_data(init_data, token)
    if user is None and required:
        raise AuthError(failure_reason(init_data, token))
    return user


def display_name(user):
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = first + (" " + last[0] + "." if last else "")
    if not name:
        name = user.get("username") or "Player"
    return name[:24]
