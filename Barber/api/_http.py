"""
Minimal request/response plumbing for Vercel's Python runtime.

Each endpoint is a plain function `handle(req) -> (status, payload)`, wrapped
into the BaseHTTPRequestHandler class Vercel expects via `make_handler`.
The same functions are called directly by dev_server.py and the tests.
"""
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler
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


def make_handler(fn, methods=("GET",)):
    class handler(BaseHTTPRequestHandler):
        def _run(self):
            if self.command not in methods:
                return self._send(405, {"error": "method_not_allowed"})
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 64 * 1024)) if length else b""
            req = Request(self.command, self.path, self.headers.items(), body)
            status, payload = dispatch(fn, req)
            self._send(status, payload)

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _run
        do_POST = _run

    return handler
