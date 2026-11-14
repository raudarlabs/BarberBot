"""
Vercel Function: Telegram bot webhook handler.
Receives POST from Telegram, processes the update, returns 200.
"""

import asyncio
import json
import logging
import sys
import os
import traceback
from http.server import BaseHTTPRequestHandler

# Allow imports from project root
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Allow imports from api/ directory
API_DIR = os.path.dirname(os.path.abspath(__file__))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from telegram import Update
from _legacy_bot import build_application

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _process_update(body: dict):
    """Build a fresh Application and process a single Telegram update."""
    app = build_application()
    async with app:
        update = Update.de_json(body, app.bot)
        await app.process_update(update)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))

            logger.info(f"Webhook received update_id={body.get('update_id')}")

            # Run the async handler
            asyncio.run(_process_update(body))

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Webhook error: {tb}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False}).encode())

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "Barber bot webhook is running"}).encode())
