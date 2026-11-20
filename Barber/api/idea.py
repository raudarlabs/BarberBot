"""
Game ideas from players.

POST /api/idea {title?, text}   (Telegram initData required) → stores the idea and notifies the owner
GET  /api/idea                  (X-Admin-Key) → latest ideas, newest first (used by public/ideas.html)

Every idea is stored first, so nothing is lost if a notification fails. Notifications go to
whatever is configured:
- e-mail via Resend: RESEND_API_KEY, IDEAS_EMAIL (default raudar.aborsen@gmail.com),
  IDEAS_EMAIL_FROM (default "Barber Games <onboarding@resend.dev>")
- Telegram: IDEAS_TELEGRAM_CHAT_ID (sent by TELEGRAM_BOT_TOKEN's bot)
"""
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _http import BadRequest, serve  # noqa: E402
from _kv import cmd, pipeline  # noqa: E402
from _tg import current_user, display_name  # noqa: E402

DEFAULT_EMAIL = "raudar.aborsen@gmail.com"
MAX_PER_DAY = 5
MAX_TEXT = 1500
MAX_TITLE = 80


def _post_json(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _message(idea):
    who = idea["name"] + (" (@%s)" % idea["username"] if idea.get("username") else "")
    title = idea.get("title") or "без назви"
    return "Нова ідея гри для Barber Games\n\nВід: %s\nTelegram ID: %s\nНазва: %s\n\n%s" % (
        who, idea["uid"], title, idea["text"])


def notify(idea):
    """Best effort: returns the list of channels that accepted the message."""
    sent = []
    text = _message(idea)
    key = os.environ.get("RESEND_API_KEY")
    if key:
        try:
            _post_json("https://api.resend.com/emails", {
                "from": os.environ.get("IDEAS_EMAIL_FROM") or "Barber Games <onboarding@resend.dev>",
                "to": [os.environ.get("IDEAS_EMAIL") or DEFAULT_EMAIL],
                "subject": "Barber Games: ідея гри від %s" % idea["name"],
                "text": text,
            }, {"Authorization": "Bearer " + key})
            sent.append("email")
        except Exception as e:  # noqa: BLE001 - delivery is best effort, the idea is already stored
            print("[idea] email failed: %s" % e, file=sys.stderr)
    chat = os.environ.get("IDEAS_TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if chat and token:
        try:
            _post_json("https://api.telegram.org/bot%s/sendMessage" % token, {"chat_id": chat, "text": text}, {})
            sent.append("telegram")
        except Exception as e:  # noqa: BLE001
            print("[idea] telegram failed: %s" % e, file=sys.stderr)
    return sent


def _check_admin(req):
    expected = os.environ.get("ARCADE_ADMIN_KEY", "")
    if not expected:
        raise BadRequest("not_configured", 503)
    if not hmac.compare_digest(req.headers.get("x-admin-key", "").encode(), expected.encode()):
        raise BadRequest("forbidden", 403)


def handle(req):
    if req.method == "GET":
        _check_admin(req)
        ids = cmd("ZREVRANGE", "ideas:index", 0, 99) or []
        rows = cmd("HMGET", "ideas", *ids) if ids else []
        return 200, {"ideas": [json.loads(r) for r in rows if r]}

    user = current_user(req.init_data)
    body = req.json()
    text = str(body.get("text") or "").strip()
    title = str(body.get("title") or "").strip()[:MAX_TITLE]
    if len(text) < 10:
        raise BadRequest("too_short")
    if len(text) > MAX_TEXT:
        raise BadRequest("too_long")

    uid = str(user["id"])
    now = time.time()
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    rl = "rl:idea:%s:%s" % (uid, day)
    count, _ = pipeline(["INCR", rl], ["EXPIRE", rl, 2 * 86400])
    if int(count) > MAX_PER_DAY:
        raise BadRequest("rate_limited", 429)

    idea_id = "%d:%s:%s" % (int(now * 1000), uid, secrets.token_hex(3))
    idea = {
        "id": idea_id, "uid": uid, "name": display_name(user), "username": user.get("username") or "",
        "title": title, "text": text, "t": int(now),
    }
    pipeline(["HSET", "ideas", idea_id, json.dumps(idea, ensure_ascii=False)],
             ["ZADD", "ideas:index", int(now * 1000), idea_id])
    sent = notify(idea)
    if sent:
        cmd("HSET", "ideas", idea_id, json.dumps(dict(idea, sent=sent), ensure_ascii=False))
    return 200, {"ok": True}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, handle, ("GET", "POST"))

    def do_POST(self):
        serve(self, handle, ("GET", "POST"))
