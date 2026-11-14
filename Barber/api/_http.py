"""
Minimal request/response plumbing for Vercel's Python runtime.

Each endpoint is a plain function `handle(req) -> (status, payload)`; its module
declares the literal `class handler(BaseHTTPRequestHandler)` Vercel looks for and
calls `serve(self, handle, methods)`.
The same functions are called directly by dev_server.py and the tests.
"""
import json
import sys
import traceback
from urllib.parse import parse_qs, urlparse

from _kv import RedisError
from _tg import AuthError

INIT_DATA_HEADER = "X-Telegram-Init-Data"


class Request:
    def __init__(self, method, path, headers, body=b""):
        self.method = method
        parsed = urlparse(path)
        self.path = parsed.path
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = {k.lower(): v for k, v in dict(headers).items()}
        self.body = body or b""

    @property
    def init_data(self):
        return self.headers.get(INIT_DATA_HEADER.lower(), "")

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except ValueError:
            raise BadRequest("invalid_json")
        if not isinstance(data, dict):
            raise BadRequest("invalid_json")
        return data


class BadRequest(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def dispatch(fn, req):
    """Run an endpoint function and map known errors to JSON responses."""
    try:
        return fn(req)
    except BadRequest as e:
        return e.status, {"error": e.code}
    except AuthError:
        return 401, {"error": "unauthorized"}
    except RedisError:
        traceback.print_exc(file=sys.stderr)
        return 503, {"error": "storage_unavailable"}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 500, {"error": "internal"}


def serve(h, fn, methods=("GET",)):
    """Run endpoint `fn` for a BaseHTTPRequestHandler instance `h` (Vercel's Python entrypoint)."""
    if h.command not in methods:
        status, payload = 405, {"error": "method_not_allowed"}
    else:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(min(length, 64 * 1024)) if length else b""
        status, payload = dispatch(fn, Request(h.command, h.path, h.headers.items(), body))
    data = json.dumps(payload, ensure_ascii=False).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)
