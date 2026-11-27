#!/usr/bin/env python3
"""Local server for the knowledge galaxy.

Serves only viewer/ on 127.0.0.1:4700 and exposes POST /chat. The same brain is
used by the Vercel function at api/chat.py.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import brain

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
HOST = "127.0.0.1"
PORT = int(os.environ.get("KG_PORT") or os.environ.get("PORT") or "4700")
MAX_BODY_BYTES = 64 * 1024


class Handler(SimpleHTTPRequestHandler):
    server_version = "KnowledgeGalaxy/1.0"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER), **kwargs)

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
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if self.path in ("/", "/index.html") or self.path.endswith(("graph-data.js", "graph.json")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if urlparse(self.path).path not in ("/chat", "/api/chat"):
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "notes": brain.note_count(),
                    "configured": brain.is_configured(),
                },
            )
            return
        if path == "/":
            self.path = "/index.html"
        if ".." in path or path.startswith("/config"):
            self._json(404, {"error": "Not found"})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/chat", "/api/chat"):
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
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
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
        sys.stderr.write("[server] " + (fmt % args) + "\n")


def main() -> int:
    if not (VIEWER / "index.html").exists():
        print("viewer/index.html is missing. Run `python build.py` first.", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Knowledge Galaxy running at http://{HOST}:{PORT}")
    print(f"Notes indexed: {brain.note_count()}  FreeLLMAPI configured: {brain.is_configured()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
