"""
Local dev server for the arcade: serves public/ and the /api/* functions.

    python dev_server.py            # http://localhost:8787

Without Upstash env vars it uses an in-memory Redis, and requests without
Telegram initData play as a fixed "Dev Player" (ARCADE_DEV=1); the staff prize
page key is "dev". Never set ARCADE_DEV in production.
"""
import importlib
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("ARCADE_DEV", "1")
os.environ.setdefault("ARCADE_ADMIN_KEY", "dev")  # staff page key for local testing
sys.path.insert(0, os.path.join(ROOT, "api"))

from _http import Request, dispatch  # noqa: E402

ENDPOINTS = {
    name: importlib.import_module(name).handle
    for name in ("session", "score", "leaderboard", "summary", "prize", "idea")
}


class DevHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.join(ROOT, "public"), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _api(self):
        name = self.path.split("?")[0][len("/api/"):]
        fn = ENDPOINTS.get(name)
        if fn is None:
            status, payload = 404, {"error": "not_found"}
        else:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload = dispatch(fn, Request(self.command, self.path, self.headers.items(), body))
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api()
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._api()
        self.send_error(405)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8787"))
    print("Barber Arcade dev server: http://localhost:%d" % port)
    ThreadingHTTPServer(("0.0.0.0", port), DevHandler).serve_forever()
