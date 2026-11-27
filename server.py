#!/usr/bin/env python3
"""Local server for the knowledge galaxy.

Serves only viewer/ on 127.0.0.1:4700 and exposes POST /chat. The same brain is
used by the Vercel function at api/chat.py.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import traceback
import unicodedata
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import brain
import build

try:
    import teardown
except Exception:  # THE BAY is optional; Jarvis still boots without it.
    teardown = None

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
    "TEARDOWN": {
        "help": "observe one public website read-only with REA; pass the http(s) URL",
        "run": "_tag_teardown",
    },
}

NO_NOTES_REPLY = (
    "I'm afraid the notes are silent on that, sir. I won't invent a source to "
    "fill the silence."
)

CAPTURE_REPLY = 'Very good, sir. "{label}" is filed and already in the index.'
CAPTURE_FAIL_REPLY = "I'm afraid that capture failed, sir. Nothing has been added to the galaxy."


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


def _tag_teardown(arg: str) -> tuple[bool, str]:
    if teardown is None:
        return (
            False,
            "I'm afraid the teardown bay isn't fitted, sir. Install teardown.py and "
            "REA 6.10.1; nothing was touched.",
        )
    result = teardown.run_teardown(arg)
    reply = str(result.get("reply") or "The teardown bay is not ready, sir. Nothing was touched.")
    return bool(result.get("ok")), reply


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


# ---------------------------------------------------------------------------
# ONE DOOR FOR NEW NOTES
#
# remember() is the only function that creates a note. It writes the Markdown
# file, builds its node the way build.py does, finds the links a rebuild would
# find, rewrites both graph files, and puts the note into the live search index
# before returning it. A later feature that births notes calls this and nothing
# else; a failure here is raised, never swallowed, so the browser can say so.
# ---------------------------------------------------------------------------

REMEMBER_RE = re.compile(r"^\s*remember\s+that\b[\s,:;\-–—]*(.*)$", re.I | re.S)
CAPTURE_LOCK = threading.Lock()


def _slugify(value: str, limit: int = 60) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug[:limit].rstrip("-") or "capture"


def _capture_title(body: str, words: int = 8) -> str:
    short = " ".join(body.split()[:words]).strip(" .,;:!?")
    return build.title_case(short or body[:60])


def _capture_path(folder: Path, title: str) -> Path:
    base = _slugify(title)
    path = folder / (base + ".md")
    counter = 2
    while path.exists():
        path = folder / (base + "-" + str(counter) + ".md")
        counter += 1
    return path


def _capture_links(raw: str, label: str, path: Path, graph_nodes: list[dict]) -> list[int]:
    """Exactly the links build.py would make for this file: wikilinks and titles."""
    raw_lower = raw.lower()
    targets: set[int] = set()

    for link in build.WIKILINK_RE.findall(raw):
        wanted = build.normalize(link)
        for node in graph_nodes:
            if build.normalize(node["label"]) == wanted:
                targets.add(node["id"])

    for node in graph_nodes:
        if build.mentions_title(raw_lower, node["label"]):
            targets.add(node["id"])

    # A full rebuild also scans the older notes for a mention of the new title.
    for other in build.NOTES_DIR.rglob("*.md"):
        if other == path:
            continue
        try:
            text = other.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not build.mentions_title(text.lower(), label):
            continue
        other_label = build.label_from_path(other)
        for node in graph_nodes:
            if node["label"] == other_label:
                targets.add(node["id"])
                break

    return sorted(targets)


def _capture_anchor(body: str, label: str, graph_nodes: list[dict]) -> int | None:
    """The most related existing note: the capture is born at its position."""
    hits = brain.retrieve(body, "", graph_nodes)
    if hits:
        return hits[0]["id"]
    terms = set(brain.tokenize(body + " " + label))
    best = None
    best_score = 0
    for node in graph_nodes:
        shared = terms & set(brain.tokenize(node["label"] + " " + node.get("excerpt", "")))
        if len(shared) > best_score:
            best, best_score = node["id"], len(shared)
    return best


def _write_graph_files(graph: dict) -> None:
    payload = json.dumps(graph, ensure_ascii=False, indent=2)
    js = (
        "// Generated by build.py. Do not edit by hand.\n"
        "const GRAPH = " + payload + ";\n"
        "window.GRAPH = GRAPH;\n"
    )
    js_tmp = build.JS_PATH.with_name(build.JS_PATH.name + ".tmp")
    json_tmp = build.JSON_PATH.with_name(build.JSON_PATH.name + ".tmp")
    js_tmp.write_text(js, encoding="utf-8")
    json_tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(js_tmp, build.JS_PATH)
    os.replace(json_tmp, build.JSON_PATH)


def remember(raw_text) -> dict:
    """One door for new notes: file, node, links, graph files, live index."""
    if not isinstance(raw_text, str):
        raise ValueError("There was nothing to remember, sir.")
    match = REMEMBER_RE.match(raw_text)
    body = re.sub(r"\s+", " ", match.group(1) if match else raw_text).strip()
    if len(body) < 3:
        raise ValueError('I need something after "remember that", sir.')

    with CAPTURE_LOCK:
        folder = build.NOTES_DIR / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        title = _capture_title(body)
        path = _capture_path(folder, title)
        today = datetime.date.today().isoformat()
        content = "---\ncaptured: " + today + "\n---\n\n# " + title + "\n\n" + body + "\n"

        path.write_text(content, encoding="utf-8")
        try:
            label = build.label_from_path(path)
            group = build.group_from_path(path)
            excerpt = build.make_excerpt(build.plain_text(content))

            graph = json.loads(build.JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(graph.get("nodes"), list):
                raise ValueError("The graph file has no node list.")
            node_id = len(graph["nodes"])
            links = _capture_links(content, label, path, graph["nodes"])
            anchor = _capture_anchor(body, label, brain.load_graph())

            node = {"id": node_id, "label": label, "group": group, "excerpt": excerpt}
            graph["nodes"].append(node)
            graph["links"].extend(
                {"source": min(node_id, other), "target": max(node_id, other)} for other in links
            )
            graph["links"].sort(key=lambda item: (int(item["source"]), int(item["target"])))
            _write_graph_files(graph)
            brain.add_note(node)
        except Exception:
            # No half-captures: if indexing failed, remove the file we just wrote.
            try:
                path.unlink()
            except OSError:
                pass
            raise

    return {
        "ok": True,
        "id": node_id,
        "label": label,
        "group": group,
        "excerpt": excerpt,
        "links": links,
        "anchor": anchor,
        "file": path.relative_to(ROOT).as_posix(),
        "reply": CAPTURE_REPLY.format(label=label),
    }


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
        if urlparse(self.path).path not in ("/chat", "/api/chat", "/remember", "/reindex", "/teardown"):
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
        if path == "/teardown/diag":
            if teardown is None:
                self._json(
                    200,
                    {
                        "ready": False,
                        "installed_version": None,
                        "pinned_version": None,
                        "expected_version": "6.10.1",
                        "node": {"path": None, "version": None, "ok": False, "need": "22.23.4+"},
                        "chrome": {"path": None, "found": False},
                        "problems": ["teardown.py is missing from the project."],
                    },
                )
            else:
                self._json(200, teardown.diagnose())
            return
        if path == "/":
            self.path = "/index.html"
        # Decode and normalise before the safety check so encoded traversal
        # (including Windows backslashes) cannot reach files outside viewer/.
        decoded = unquote(unquote(path)).replace("\\", "/")
        if (
            ".." in decoded.split("/")
            or decoded.lower().startswith("/config")
            or "\x00" in decoded
        ):
            self._json(404, {"error": "Not found"})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/chat", "/api/chat", "/remember", "/reindex", "/teardown"):
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

        if path == "/teardown":
            if teardown is None:
                self._json(
                    200,
                    {
                        "ok": False,
                        "reply": "I'm afraid the teardown bay isn't fitted, sir. Install teardown.py and REA 6.10.1; nothing was touched.",
                    },
                )
                return
            value = payload.get("url", payload.get("question", payload.get("text")))
            result = teardown.run_teardown(
                value, payload.get("command"), headless=payload.get("headless") is True
            )
            self._json(200, result)
            return

        if path == "/reindex":
            try:
                notes = brain.reload_graph()
                self._json(200, {"ok": True, "notes": len(notes)})
            except brain.BrainError as exc:
                self._json(exc.status, {"ok": False, "error": exc.message, "code": exc.code})
            except Exception:
                traceback.print_exc()
                self._json(500, {"ok": False, "error": "Re-index failed.", "code": "server_error"})
            return

        if path == "/remember":
            try:
                self._json(200, remember(payload.get("text", payload.get("question"))))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc), "reply": CAPTURE_FAIL_REPLY})
            except Exception:
                traceback.print_exc()
                self._json(
                    500,
                    {
                        "ok": False,
                        "error": "The note could not be written or indexed, so nothing was added.",
                        "reply": CAPTURE_FAIL_REPLY,
                    },
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
