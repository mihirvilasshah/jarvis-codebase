"""Vercel Python function: POST /api/chat.

Keeps the same JSON contract as the local POST /chat endpoint:
{"answer": "...", "nodes": [0, 3, 7]}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import brain  # noqa: E402

MAX_BODY_BYTES = 64 * 1024


class handler(BaseHTTPRequestHandler):
    server_version = "KnowledgeGalaxy/1.0"
    sys_version = ""

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and brain.origin_allowed(origin, host):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/chat", "/api/chat/", "/chat"):
            self._json(
                200,
                {
                    "ok": True,
                    "notes": brain.note_count(),
                    "configured": brain.is_configured(),
                },
            )
            return
        self._json(404, {"error": "Not found", "nodes": []})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/chat/", "/chat"):
            self._json(404, {"error": "Not found", "nodes": []})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._json(
                400,
                {"error": "Please send a JSON request body.", "code": "invalid_request", "nodes": []},
            )
            return
        if length > MAX_BODY_BYTES:
            self._json(
                413,
                {"error": "The request body is too large.", "code": "payload_too_large", "nodes": []},
            )
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(
                400,
                {"error": "The request body must be valid JSON.", "code": "invalid_json", "nodes": []},
            )
            return
        if not isinstance(payload, dict):
            self._json(
                400,
                {"error": "The request body must be a JSON object.", "code": "invalid_request", "nodes": []},
            )
            return

        question = payload.get("question", payload.get("message"))
        try:
            result = brain.answer_question(
                question,
                session_id=payload.get("session_id"),
                client_history=payload.get("history"),
            )
            self._json(200, result)
        except brain.BrainError as exc:
            self._json(exc.status, {"error": exc.message, "code": exc.code, "nodes": []})
        except Exception:
            traceback.print_exc()
            self._json(
                500,
                {"error": "Unexpected server error.", "code": "server_error", "nodes": []},
            )

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[api] " + (fmt % args) + "\n")
