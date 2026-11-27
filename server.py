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
import shutil
import sys
import threading
import time
import traceback
import unicodedata
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import brain
import build
import research

# The gateway occasionally takes longer than its default 45 s under load.
brain.REQUEST_TIMEOUT = 120

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
        "help": "when asked to reverse engineer a named site with no address, reply with [TEARDOWN:SITE|QUESTION] and nothing else; use 'this' for the current tab",
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
    parts = [part.strip() for part in arg.split("|", 1)]
    url = parts[0] if parts else ""
    question = parts[1] if len(parts) > 1 and parts[1] else ""
    result = compose_teardown(url, question=question)
    if not result.get("ok"):
        return False, str(result.get("reply") or "The teardown could not be completed, sir.")
    return True, str(result.get("label") or "Teardown complete, sir.")


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
TEARDOWN_LOCK = threading.Lock()
TEARDOWN_STATUS = {
    "active": False, "site": "", "step": 0, "label": "", "started": 0.0, "steps": []
}
TEARDOWN_CANCEL = False
LAST_TEARDOWN = {}


def _cancel_teardown() -> None:
    global TEARDOWN_CANCEL
    TEARDOWN_CANCEL = True


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


def remember(
    raw_text,
    *,
    subfolder: str = "captures",
    title: str | None = None,
    body: str | None = None,
    filename: str | None = None,
    reply: str | None = None,
) -> dict:
    """One door for new notes: file, node, links, graph files, live index."""
    if not isinstance(raw_text, str) and body is None:
        raise ValueError("There was nothing to remember, sir.")
    if body is None:
        match = REMEMBER_RE.match(raw_text)
        body = re.sub(r"\s+", " ", match.group(1) if match else raw_text).strip()
    if not isinstance(body, str) or len(body.strip()) < 3:
        raise ValueError('I need something after "remember that", sir.')

    with CAPTURE_LOCK:
        folder = build.NOTES_DIR / subfolder
        folder.mkdir(parents=True, exist_ok=True)
        note_title = title or _capture_title(body)
        path = (folder / filename) if filename else _capture_path(folder, note_title)
        today = datetime.date.today().isoformat()
        if filename and body.lstrip().startswith(("---", "#")):
            content = body
        else:
            content = "---\ncaptured: " + today + "\n---\n\n# " + note_title + "\n\n" + body + "\n"

        existed = filename is not None and path.exists()
        path.write_text(content, encoding="utf-8")
        try:
            label = build.label_from_path(path)
            group = build.group_from_path(path)
            excerpt = build.make_excerpt(build.plain_text(content))

            graph = json.loads(build.JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(graph.get("nodes"), list):
                raise ValueError("The graph file has no node list.")
            links = _capture_links(content, label, path, graph["nodes"])
            anchor = _capture_anchor(body, label, brain.load_graph())
            existing = next(
                (node for node in graph["nodes"] if node.get("label") == label), None
            ) if existed else None

            if existing is not None:
                node_id = int(existing["id"])
                existing["group"] = group
                existing["excerpt"] = excerpt
                graph["links"] = [
                    link for link in graph["links"]
                    if link.get("source") != node_id and link.get("target") != node_id
                ]
                node = existing
            else:
                node_id = len(graph["nodes"])
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
        "reply": reply or CAPTURE_REPLY.format(label=label),
    }


ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
HOST = "127.0.0.1"
# PORT=0 means "any free port" to the OS; this server always wants 4700.
PORT = int(os.environ.get("KG_PORT") or os.environ.get("PORT") or "4700") or 4700
MAX_BODY_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024


TEARDOWN_SYSTEM = """You are Jarvis, the butler of a knowledge galaxy, writing a teardown of a website.
Rules you must not break:
- Every claim must trace to the labelled evidence. If something is an inference, say "likely".
- Never invent a library, file, technique, route or version.
- Keep attribution straight: REA captured some lines, Jarvis read others from its own browser. Say which when it matters.
- Do not include web addresses or file names in SPOKEN.
Output EXACTLY this structure and nothing else:
LABEL: Teardown · HOST
SPOKEN: two or three sentences; headline answer first, then the most surprising detail.
SUMMARY: one short paragraph.
---NOTE---
A markdown note containing, in order: a one-line verdict, "The answer", "The stack" with each item's evidence, "How the effect works" as numbered steps, "What it loads", "Rebuild it" with 5 to 7 steps for Claude Code, and "What the evidence can't show".
Think and answer inside one 7000-token budget."""


def _front_tab_url() -> str | None:
    """On a Mac, read the front Chrome tab - never the Jarvis tab itself."""
    if sys.platform != "darwin":
        return None
    try:
        script = 'tell application "Google Chrome" to get URL of active tab of front window'
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
        url = (result.stdout or "").strip()
        parsed = urlparse(url)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port in {4700, None}:
            return None
        return url or None
    except Exception:
        return None


def _teardown_progress(number: int, label: str) -> None:
    TEARDOWN_STATUS.update({"active": True, "step": number, "label": label})
    seen = TEARDOWN_STATUS.setdefault("steps", [])
    if label not in seen:
        seen.append(label)
    if TEARDOWN_CANCEL:
        raise RuntimeError("cancelled")


def _teardowns_root() -> Path:
    return build.NOTES_DIR / "captures" / "teardowns"


def _safe_teardown_path(raw, extensions) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    decoded = unquote(unquote(raw)).replace("\\", "/")
    if decoded.startswith(("/", "file:")) or ".." in decoded.split("/") or "\x00" in decoded:
        return None
    root = _teardowns_root().resolve()
    candidate = (root / decoded).resolve()
    if root not in candidate.parents and candidate != root:
        return None
    if candidate.suffix.lower() not in extensions or not candidate.is_file():
        return None
    return candidate


def _last_teardown_note() -> Path | None:
    folder = build.NOTES_DIR / "captures" / "teardowns"
    if not folder.exists():
        return None
    notes = [path for path in folder.glob("*.md") if path.is_file()]
    if not notes:
        return None
    return max(notes, key=lambda path: path.stat().st_mtime)


def _last_snapshot() -> Path | None:
    remembered = str(LAST_TEARDOWN.get("snapshot") or "")
    if remembered and Path(remembered).exists():
        return Path(remembered)
    note = _last_teardown_note()
    if note is None:
        return None
    candidate = note.parent / "assets" / (note.stem + ".png")
    return candidate if candidate.exists() else None


def _teardown_host(url: str) -> str:
    host = urlparse(str(url or "")).hostname or ""
    if host.lower().startswith("www."):
        host = host[4:]
    return re.sub(r"[^A-Za-z0-9.-]", "", host) or "site"


def _teardown_brain(digest: str, question: str, host: str) -> str:
    config = brain.get_config(require_key=True)
    user = (
        "Host: " + host + "\nQuestion: " + question + "\n\n"
        "EVIDENCE (each line labelled REA or Jarvis):\n" + digest
    )
    previous_timeout = brain.REQUEST_TIMEOUT
    brain.REQUEST_TIMEOUT = 180
    try:
        data = brain._post_chat(
            config,
            {
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": TEARDOWN_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 7000,
                "stream": False,
            },
        )
    finally:
        brain.REQUEST_TIMEOUT = previous_timeout
    text = brain._extract_text(data)
    if not text:
        raise brain.BrainError(502, "empty_response", "The teardown writer returned nothing, sir.")
    return text


def _parse_teardown(text: str) -> dict:
    label = ""
    spoken = ""
    summary = ""
    note = ""
    match = re.search(r"^\s*LABEL:\s*(.+?)\s*$", text, re.M)
    if match:
        label = match.group(1).strip()
    match = re.search(r"^\s*SPOKEN:\s*(.*?)(?=^\s*SUMMARY:\s*|\Z)", text, re.M | re.S)
    if match:
        spoken = re.sub(r"\s+", " ", match.group(1)).strip()
    match = re.search(r"^\s*SUMMARY:\s*(.*?)(?=^\s*---NOTE---\s*$|\Z)", text, re.M | re.S)
    if match:
        summary = re.sub(r"\s+", " ", match.group(1)).strip()
    if "---NOTE---" in text:
        note = text.split("---NOTE---", 1)[1].strip()
    return {"label": label, "spoken": spoken, "summary": summary, "note": note}


def _ensure_concept_notes() -> list[dict]:
    """Birth Jarvis and [[REA]] through the one note door, REA before teardown."""
    records: list[dict] = []
    today = datetime.date.today().isoformat()
    known = {str(node.get("label", "")) for node in brain.load_graph()}

    jarvis_path = build.NOTES_DIR / "captures" / "Jarvis.md"
    if not jarvis_path.exists() and "Jarvis" not in known:
        body = (
            "---\ncaptured: " + today + "\n---\n\n# Jarvis\n\n"
            "Jarvis is the butler of the knowledge galaxy: dry, impeccably polite, "
            "and honest about the evidence. His reverse-engineering kit is [[REA]].\n"
        )
        records.append(
            remember(
                body,
                title="Jarvis",
                filename="Jarvis.md",
                body=body,
                reply="Jarvis has a place in the galaxy, sir.",
            )
        )
    rea_path = build.NOTES_DIR / "captures" / "REA.md"
    if not rea_path.exists() and "REA" not in known:
        body = (
            "---\ncaptured: " + today + "\n---\n\n# REA\n\n"
            "REA (Reverse Engineer Anything) is the read-only observation kit used by "
            "[[Jarvis]] on websites. It attaches to a browser Jarvis owns; it never "
            "clicks, types, logs in, or changes anything.\n"
        )
        records.append(
            remember(
                body,
                title="REA",
                filename="REA.md",
                body=body,
                reply="REA has a place in the galaxy, sir.",
            )
        )
    return records


def _write_teardown_note(host: str, parsed: dict, result: dict) -> tuple[dict, str]:
    today = datetime.date.today().isoformat()
    stem = host + "-" + today
    folder = build.NOTES_DIR / "captures" / "teardowns"
    assets = folder / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    snapshot = assets / (stem + ".png")
    source = str(result.get("screenshot_path") or "")
    if source and Path(source).exists():
        shutil.copyfile(source, snapshot)

    assets_data = result.get("asset_log") or {}
    ids = teardown.evidence_ids(result) if teardown is not None else []
    stack_lines = []
    for item in result.get("fingerprints") or []:
        if item.get("found"):
            stack_lines.append(f"- {item['category']}: {item.get('version') or 'present'} (Jarvis reading REA's code)")
    evidence = [
        "## Evidence",
        "",
        f"- Snapshot: ![{stem}](assets/{stem}.png)  (REA captured, Jarvis decoded)",
        "- Stack (Jarvis reading REA's captured code):",
        *(stack_lines or ["- none identified"]),
        f"- Assets: {assets_data.get('total_files', 0)} files, {assets_data.get('total_bytes', 0):,} bytes (Jarvis browser read)",
        "- Origins: " + ", ".join(str(item) for item in (assets_data.get("origins") or [])) + " (Jarvis browser read)",
        "- REA evidence ids: " + (", ".join(ids) if ids else "none reported"),
        "- Attribution: REA lines were captured by REA; Jarvis lines were read from the owned browser or REA's code.",
        "- Limits: REA attaches to an already-running page; library names come from banners and strings, not a package manifest.",
        "",
        f"[[REA]] · [[Jarvis]]",
    ]
    note = (parsed.get("note") or parsed.get("summary") or "Teardown complete.").rstrip()
    note = note + "\n\n" + "\n".join(evidence) + "\n"
    record = remember(
        note,
        subfolder="captures/teardowns",
        title="Teardown · " + host,
        body=note,
        filename=stem + ".md",
        reply='Filed the teardown of ' + host + ', sir.',
    )
    return record, str(snapshot)


def compose_teardown(url, question=None, headless: bool = False) -> dict:
    """One-at-a-time evidence, one brain call, one filed note, one flight."""
    global TEARDOWN_CANCEL, LAST_TEARDOWN
    if teardown is None:
        return {"ok": False, "reply": "I'm afraid the teardown bay isn't fitted, sir. Install teardown.py and REA 6.10.1."}
    if not TEARDOWN_LOCK.acquire(blocking=False):
        return {"ok": False, "reply": "I'm already taking one apart, sir."}

    TEARDOWN_CANCEL = False
    TEARDOWN_STATUS.update(
        {"active": True, "site": str(url or ""), "step": 0, "label": "", "started": time.time(), "steps": []}
    )
    try:
        site = str(url or "").strip()
        if site.lower() in {"this", "this site", "this page", "current", "current tab"}:
            site = _front_tab_url() or ""
            if not site:
                return {"ok": False, "reply": "Please give me the site's name, sir."}
        asked = str(question or "").strip() or "What is this site and how does it work?"
        capture = teardown.run_teardown(site, headless=headless, progress=_teardown_progress)
        if not capture.get("ok"):
            return {"ok": False, "reply": str(capture.get("reply") or "The teardown failed, sir.")}
        _teardown_progress(8, "Writing the teardown")
        host = _teardown_host(site)
        digest = teardown.build_digest(capture, asked, host)
        written = _teardown_brain(digest, asked, host)
        parsed = _parse_teardown(written)
        if not parsed["label"].lower().startswith("teardown"):
            parsed["label"] = "Teardown · " + host
        if not parsed["spoken"]:
            parsed["spoken"] = "A tidy little teardown, sir; the evidence is filed."
        if not parsed["summary"]:
            parsed["summary"] = "The teardown has been filed with its evidence."
        concept_notes = _ensure_concept_notes()
        record, snapshot = _write_teardown_note(host, parsed, capture)
        full_note = (ROOT / record["file"]).read_text(encoding="utf-8")
        assets_data = capture.get("asset_log") or {}
        stack = [
            {"category": item.get("category"), "version": item.get("version") or "present"}
            for item in (capture.get("fingerprints") or []) if item.get("found")
        ]
        elapsed = max(1, round(time.time() - TEARDOWN_STATUS["started"]))
        result = {
            "ok": True,
            "reply": parsed["summary"],
            "spoken": parsed["spoken"],
            "label": parsed["label"],
            "summary": parsed["summary"],
            "note": parsed["note"],
            "full_note": full_note,
            "note_file": record["file"],
            "snapshot": snapshot,
            "notes": concept_notes + [record],
            "evidence": capture.get("evidence", []),
            "digest": digest,
            "digest_chars": len(digest),
            "host": host,
            "site": site,
            "stack": stack,
            "totals": {
                "files": assets_data.get("total_files", 0),
                "bytes": assets_data.get("total_bytes", 0),
            },
            "elapsed_seconds": elapsed,
        }
        LAST_TEARDOWN = result
        return result
    except brain.BrainError as exc:
        return {"ok": False, "reply": exc.message}
    except Exception as exc:
        detail = str(exc).replace("\r", " ").replace("\n", " ")[:180]
        if "cancel" in detail.lower():
            return {"ok": False, "reply": "Very good, sir — I stopped taking it apart."}
        traceback.print_exc()
        return {"ok": False, "reply": f"The teardown could not be written, sir: {detail}"}
    finally:
        TEARDOWN_STATUS.update({"active": False, "step": 8, "label": ""})
        TEARDOWN_LOCK.release()


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
        if urlparse(self.path).path not in ("/chat", "/api/chat", "/remember", "/reindex", "/teardown", "/teardown/compose", "/teardown/cancel", "/see", "/research", "/research/history", "/research/save", "/research/delete", "/research/export"):
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/research/history":
            self._json(200, {"ok": True, "items": research.STORE.list()})
            return
        if path.startswith("/research/"):
            research_id = path.rsplit("/", 1)[-1]
            report = research.STORE.get(research_id)
            if report:
                self._json(200, {"ok": True, "research": report})
            else:
                self._json(404, {"ok": False, "error": "Research session not found."})
            return
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
        if path in ("/teardown/note", "/teardown/image"):
            query = parse_qs(urlparse(self.path).query)
            raw = (query.get("file") or [""])[0]
            if path == "/teardown/note":
                safe = _safe_teardown_path(raw, {".md"})
                if safe is None:
                    self._json(403, {"ok": False, "error": "Outside the teardowns folder."})
                    return
                self._json(200, {"ok": True, "label": safe.stem, "content": safe.read_text(encoding="utf-8")})
                return
            safe = _safe_teardown_path(raw, {".png", ".jpg", ".jpeg", ".webp"})
            if safe is None:
                self._json(403, {"ok": False, "error": "Outside the teardowns folder."})
                return
            body = safe.read_bytes()
            media = "image/png" if safe.suffix.lower() == ".png" else "image/jpeg" if safe.suffix.lower() in {".jpg", ".jpeg"} else "image/webp"
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/teardown/status":
            self._json(200, dict(TEARDOWN_STATUS))
            return
        if path == "/teardown/snapshot":
            snapshot = _last_snapshot()
            if snapshot is None:
                self._json(404, {"error": "No teardown snapshot yet."})
                return
            body = snapshot.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/teardown/last":
            note = _last_teardown_note()
            if note is None:
                self._json(404, {"ok": False, "reply": "No teardown has been filed yet, sir."})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "label": note.stem.replace("-", " "),
                    "full_note": note.read_text(encoding="utf-8"),
                    "snapshot": "/teardown/snapshot",
                    "note_file": note.relative_to(ROOT).as_posix(),
                },
            )
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
        if path not in ("/chat", "/api/chat", "/remember", "/reindex", "/teardown", "/teardown/compose", "/teardown/cancel", "/see", "/research", "/research/history", "/research/save", "/research/delete", "/research/export"):
            self._json(404, {"error": "Not found", "nodes": []})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if path == "/see":
            media_type = str(self.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
            if media_type != "image/jpeg":
                self._json(
                    415,
                    {
                        "error": "Send one JPEG frame with Content-Type image/jpeg.",
                        "code": "unsupported_media_type",
                        "nodes": [],
                    },
                )
                return
            if length > MAX_IMAGE_BYTES:
                self._json(
                    413,
                    {"error": "That frame is too large.", "code": "payload_too_large", "nodes": []},
                )
                return
            frame = self.rfile.read(length)
            query = parse_qs(urlparse(self.path).query)
            question = (query.get("question") or [""])[0]
            try:
                answer = brain.see_image(frame, question)
                self._json(200, {"answer": answer, "nodes": []})
            except brain.BrainError as exc:
                self._json(exc.status, {"error": exc.message, "code": exc.code, "nodes": []})
            except Exception:
                traceback.print_exc()
                self._json(500, {"error": "The screen could not be read, sir.", "code": "server_error", "nodes": []})
            return

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

        if path == "/research":
            question = payload.get("question", payload.get("topic"))
            if not isinstance(question, str) or not question.strip():
                self._json(400, {"ok": False, "error": "Please provide a research topic."})
                return
            intent = research.detect_intent(question)
            research_id = payload.get("research_id")
            existing = research.ACTIVE.get(research_id) if research_id else None
            if intent["kind"] == "follow_up" and existing:
                followups = existing.setdefault("followups", [])
                followups.append({"question": question, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                research.STORE.save(existing)
                research_topic = existing.get("topic", question)
                if re.search(r"\b(?:compare|advantages|disadvantages|more|five points|third|top)\b", question, re.I):
                    research_topic = existing.get("topic", question) + " " + question
            else:
                research_topic = intent["topic"] if intent["kind"] == "research" else question
            try:
                report = research.run_research(research_topic, intent)
                self._json(200, {"ok": True, "research": report})
            except Exception:
                traceback.print_exc()
                self._json(500, {"ok": False, "error": "Live research could not be completed."})
            return

        if path == "/research/save":
            research_id = payload.get("research_id")
            report = research.STORE.get(research_id) if research_id else None
            if not report:
                self._json(404, {"ok": False, "error": "Research session not found."})
                return
            report["saved"] = True
            research.STORE.save(report, saved=True)
            self._json(200, {"ok": True, "research": report})
            return

        if path == "/research/delete":
            research_id = payload.get("research_id")
            deleted = research.STORE.delete(research_id) if research_id else False
            self._json(200, {"ok": True, "deleted": deleted})
            return

        if path == "/research/export":
            research_id = payload.get("research_id")
            report = research.STORE.get(research_id) if research_id else None
            if not report:
                self._json(404, {"ok": False, "error": "Research session not found."})
                return
            self._json(200, {"ok": True, "markdown": research.markdown(report)})
            return

        if path == "/teardown/cancel":
            _cancel_teardown()
            self._json(200, {"ok": True, "reply": "Very good, sir — I'll stop after this step."})
            return

        if path == "/teardown/compose":
            value = payload.get("url") or payload.get("site") or payload.get("question")
            result = compose_teardown(
                value,
                question=payload.get("ask") or payload.get("detail") or "",
                headless=payload.get("headless") is True,
            )
            self._json(200, result)
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
