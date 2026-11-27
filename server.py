#!/usr/bin/env python3
"""Local server for the knowledge galaxy.

Serves only viewer/ on 127.0.0.1:4700 and exposes POST /chat. The same brain is
used by the Vercel function at api/chat.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import brain

# =============================================================================
# PERSONA - the entire character lives in this block.
#
# Rewrite the butler here: his manners, his wit, his honesty rules and the
# control tags he may ask for. Nothing else in this file needs to change.
# persona_prompt() is sent as the system prompt; apply_control_tags() below
# executes whatever tag comes back, so he can never announce an action himself.
#
# To give him a new action later: add one entry to CONTROL_TAGS and write the
# matching handler underneath this block. The prompt list is generated from
# that dict, so there is exactly one place to edit.
# =============================================================================

PERSONA_RULES = """\
You are the butler of Northwind Roasters' knowledge galaxy: dry, impeccably
polite, quietly amused, and entirely unflappable - a good British butler who
has seen every crisis and considers none of them an emergency.

HOW YOU SPEAK
- One line of dry wit, then the facts. The wit is a garnish, not the meal:
  one genuinely funny line beats three bland ones. If nothing funny is
  honestly available, a dry understatement will do - but a bare fact with no
  trace of personality is a shrug, and shrugs are not answers.
- Address the user as "sir" occasionally - now and then, not in every reply.
  Overuse is the difference between charming and grating.
- Two or three sentences is ample. Understatement over enthusiasm.
- Never recite or quote a note back at the user. The note is already open on
  their screen; give only what answers the question.

HOUSE STYLE - tone only, never wording to reuse
Asked who is responsible for the impossible: "That would be whoever has the
least sense and the most authority, sir."
Asked something the notes cannot support: "I'm afraid the notes are silent on
that, sir. I would rather admit ignorance than invent a source."
Every real answer is written fresh from the notes: dry line first, then the
fact, in your own words.

HONESTY
- Answer only from the NOTES provided. When they do not cover the question,
  say so plainly and with a little dignity, then stop. Never invent a source,
  never pad, and never present a merely related note as the answer.
- Never claim an action has been taken. You cannot act by talking: the server
  performs actions and returns a receipt. Offering is permitted; announcing a
  completed action you did not perform is the one unforgivable sin.
- You know nothing of the user's private life, whereabouts or mood beyond
  what they actually say. Never invent such details to set up a joke; a
  butler who imagines your hotel is a butler who has lost the plot.
"""

ACTION_RULES = """\
ACTIONS
Some requests are actions rather than questions. For those, reply with one
tag in square brackets and nothing else - no prose around it. The server runs
the tag and its receipt becomes your answer; you never describe the result
yourself. Only these tags exist:
{tags}
If a request needs an action that is not on this list, say plainly that you
cannot do it, and offer what you can.
"""

# The one place control tags are listed. Each name maps to a handler below.
CONTROL_TAGS = {
    "COUNT": {
        "help": "use when asked how many notes are indexed; reports the exact count",
        "run": "_tag_count",
    },
    "INDEX": {
        "help": "use when asked to re-index or refresh the knowledge graph from disk",
        "run": "_tag_index",
    },
}

NO_NOTES_REPLY = (
    "I'm afraid the notes are silent on that, sir. I won't invent a source to "
    "fill the silence."
)


def persona_prompt() -> str:
    """The persona, with the tag list generated from the single dict above."""
    tags = "\n".join("  [%s] - %s" % (name, spec["help"]) for name, spec in CONTROL_TAGS.items())
    return (PERSONA_RULES.strip() + "\n\n" + ACTION_RULES.strip()).replace("{tags}", tags)


# --- control tags: the handlers named in CONTROL_TAGS -------------------------

TAG_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)(?::([^\]]{0,200}))?\]")


def _tag_count(_arg: str) -> tuple[bool, str]:
    notes = brain.note_count()
    if notes <= 0:
        return False, "There are no notes indexed, sir; the shelf is bare."
    return True, "%d notes are indexed, sir." % notes


def _tag_index(_arg: str) -> tuple[bool, str]:
    notes = brain.reload_graph()
    return True, "Re-indexed, sir: %d notes on file." % len(notes)


def run_control_tag(name: str, arg: str | None) -> tuple[bool, str]:
    spec = CONTROL_TAGS.get(name)
    if not spec:
        return False, "I can't do that, sir. Nothing has been changed."
    handler = globals().get(spec.get("run", ""))
    if not callable(handler):
        return False, "That isn't wired up, sir. Nothing has been changed."
    try:
        ok, receipt = handler((arg or "").strip())
        return bool(ok), str(receipt)
    except Exception:
        traceback.print_exc()
        return False, "That attempt came to nothing, sir. Nothing has been changed."


def apply_control_tags(result: dict) -> dict:
    """Replace every [TAG] in an answer with the receipt from actually running it."""
    answer = result.get("answer")
    if not isinstance(answer, str) or "[" not in answer:
        return result
    receipts: list[dict] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        ok, receipt = run_control_tag(name, match.group(2))
        receipts.append({"tag": name, "ok": ok, "receipt": receipt})
        return receipt

    rewritten = re.sub(r"\s+", " ", TAG_RE.sub(replace, answer)).strip()
    if not receipts:
        return result
    updated = dict(result)
    updated["answer"] = rewritten or receipts[0]["receipt"]
    updated["receipts"] = receipts
    return updated


ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
HOST = "127.0.0.1"
# PORT=0 means "any free port" to the OS; this server always wants 4700.
PORT = int(os.environ.get("KG_PORT") or os.environ.get("PORT") or "4700") or 4700
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
                persona=persona_prompt(),
                empty_reply=NO_NOTES_REPLY,
                social=payload.get("social") is True,
            )
            self._json(200, apply_control_tags(result))
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
