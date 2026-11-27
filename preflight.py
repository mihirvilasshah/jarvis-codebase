#!/usr/bin/env python3
"""Live preflight harness for the Northwind Knowledge Galaxy.

Every check talks to the running system: the local HTTP server, the FreeLLMAPI
gateway, the real graph files, and a real headless browser. There are no unit
tests and no mocks. A failure exits non-zero.

Usage:
    python preflight.py
    python preflight.py --base-url http://127.0.0.1:4700
    python preflight.py --no-browser
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
CONFIG_PATH = ROOT / "config.json"
GRAPH_JSON = VIEWER / "graph.json"
GRAPH_JS = VIEWER / "graph-data.js"
NOTES_DIR = ROOT / "notes"
CAPTURES_DIR = NOTES_DIR / "captures"
DEFAULT_BASE_URL = "http://127.0.0.1:4700"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DETAIL_WIDTH = 100

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class CheckFailure(Exception):
    """A check failed with a message safe to print."""

    def __init__(self, message: str, loud: bool = False):
        super().__init__(message)
        self.message = message
        self.loud = loud


@dataclass
class Outcome:
    status: str  # pass | fail | warn
    detail: str = ""
    loud: bool = False


@dataclass
class Context:
    base_url: str
    nonce: str
    skip_browser: bool = False
    config: dict | None = None
    graph: dict | None = None
    health: dict | None = None
    models: list | None = None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def is_local(url: str) -> bool:
    try:
        return urllib.parse.urlparse(url).hostname in LOCAL_HOSTS
    except Exception:
        return False


def header(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def request(
    url: str,
    *,
    method: str = "GET",
    payload=None,
    headers: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict, bytes]:
    body = None
    merged = {
        "Accept": "application/json",
        "User-Agent": "NorthwindPreflight/1.0",
        "Cache-Control": "no-cache",
    }
    if headers:
        merged.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:
            data = b""
        return exc.code, dict(exc.headers), data
    except (socket.timeout, TimeoutError):
        raise CheckFailure(f"timed out after {timeout}s: {url}")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CheckFailure(f"cannot reach {url}: {reason}")


def json_body(body: bytes, label: str):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        snippet = body[:220].decode("utf-8", "replace").replace("\n", " ").strip()
        raise CheckFailure(f"{label} did not return valid JSON (got {len(body)} bytes: {snippet!r})")


def snippet(value, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def first_difference(left: bytes, right: bytes) -> int:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))


def parse_graph_js(text: str) -> dict:
    marker = "const GRAPH ="
    index = text.find(marker)
    if index < 0:
        raise CheckFailure("graph-data.js does not define `const GRAPH`")
    start = index + len(marker)
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except Exception as exc:
        raise CheckFailure(f"graph-data.js contains invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise CheckFailure("graph-data.js does not contain a graph object")
    return data


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise CheckFailure(f"config.json is missing at {CONFIG_PATH}")
    try:
        import brain
    except Exception as exc:
        raise CheckFailure(f"brain.py could not be imported: {exc}")
    try:
        raw = brain._read_config_file()
        key = brain._pick(raw, "FREELLMAPI_API_KEY", "freellmapi_api_key", "api_key")
        base = brain._pick(
            raw,
            "FREELLMAPI_BASE_URL",
            "freellmapi_base_url",
            "base_url",
            default="http://localhost:3001/v1",
        )
        model = brain._pick(raw, "FREELLMAPI_MODEL", "freellmapi_model", "model", default="auto")
        if brain._is_placeholder(key):
            raise CheckFailure("config.json has no usable FreeLLMAPI API key")
        return {
            "api_key": key,
            "base_url": brain._normalize_base_url(base),
            "model": model or "auto",
        }
    except CheckFailure:
        raise
    except Exception as exc:
        message = getattr(exc, "message", str(exc))
        raise CheckFailure(f"config.json could not be used: {message}")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_server(ctx: Context) -> Outcome:
    status, _, body = request(ctx.base_url + "/health?preflight=" + ctx.nonce, timeout=10)
    if status != 200:
        raise CheckFailure(f"GET /health -> HTTP {status}")
    health = json_body(body, "/health")
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise CheckFailure(f"/health is not healthy: {snippet(health)}")
    ctx.health = health

    status, headers, body = request(ctx.base_url + "/?preflight=" + ctx.nonce, timeout=10)
    if status != 200:
        raise CheckFailure(f"GET / -> HTTP {status}")
    content_type = header(headers, "Content-Type")
    if "text/html" not in content_type.lower():
        raise CheckFailure(f"GET / returned {content_type or 'no content type'}, not HTML")
    text = body.decode("utf-8", "replace")
    if "Northwind Knowledge Galaxy" not in text:
        raise CheckFailure("the served page does not contain the Northwind Knowledge Galaxy title")
    if 'id="graph"' not in text:
        raise CheckFailure("the served page has no #graph mount point")
    return Outcome(
        "pass",
        f"HTTP 200 · viewer served ({len(body):,} bytes) · /health ok · "
        f"{health.get('notes')} notes · configured={health.get('configured')}",
    )


def check_graph(ctx: Context) -> Outcome:
    status, _, body = request(ctx.base_url + "/graph.json?preflight=" + ctx.nonce, timeout=15)
    if status != 200:
        raise CheckFailure(f"GET /graph.json -> HTTP {status}")
    data = json_body(body, "/graph.json")
    if not isinstance(data, dict):
        raise CheckFailure("/graph.json is not a JSON object")
    nodes = data.get("nodes")
    links = data.get("links")
    if not isinstance(nodes, list) or not nodes:
        raise CheckFailure("/graph.json has no nodes")
    if not isinstance(links, list):
        raise CheckFailure("/graph.json has no links array")

    ids = []
    for node in nodes:
        if not isinstance(node, dict):
            raise CheckFailure("/graph.json contains a node that is not an object")
        node_id = node.get("id")
        if not isinstance(node_id, int) or isinstance(node_id, bool):
            raise CheckFailure(f"/graph.json node has a non-integer id: {snippet(node_id)}")
        if node_id in ids:
            raise CheckFailure(f"/graph.json has a duplicate node id: {node_id}")
        if not isinstance(node.get("label"), str) or not node["label"].strip():
            raise CheckFailure(f"/graph.json node {node_id} has no label")
        if not isinstance(node.get("group"), str):
            raise CheckFailure(f"/graph.json node {node_id} has no group")
        if not isinstance(node.get("excerpt"), str):
            raise CheckFailure(f"/graph.json node {node_id} has no excerpt")
        ids.append(node_id)
    if ids != list(range(len(nodes))):
        raise CheckFailure("node ids are not array indexes; the viewer would open the wrong note")

    id_set = set(ids)
    for link in links:
        if not isinstance(link, dict):
            raise CheckFailure("/graph.json contains a link that is not an object")
        source, target = link.get("source"), link.get("target")
        if not isinstance(source, int) or isinstance(source, bool):
            raise CheckFailure(f"/graph.json link has an invalid source: {snippet(source)}")
        if not isinstance(target, int) or isinstance(target, bool):
            raise CheckFailure(f"/graph.json link has an invalid target: {snippet(target)}")
        if source not in id_set or target not in id_set:
            raise CheckFailure(f"/graph.json link {source}->{target} points at a missing node")

    status, _, js_body = request(ctx.base_url + "/graph-data.js?preflight=" + ctx.nonce, timeout=15)
    if status != 200:
        raise CheckFailure(f"GET /graph-data.js -> HTTP {status}")
    js_graph = parse_graph_js(js_body.decode("utf-8", "replace"))
    js_nodes = js_graph.get("nodes")
    js_links = js_graph.get("links")
    if not isinstance(js_nodes, list) or not isinstance(js_links, list):
        raise CheckFailure("graph-data.js has no nodes/links arrays")
    if len(js_nodes) != len(nodes):
        raise CheckFailure(
            f"graph.json has {len(nodes)} nodes but graph-data.js has {len(js_nodes)}"
        )
    if [node.get("id") for node in js_nodes] != ids:
        raise CheckFailure("graph.json and graph-data.js disagree on node ids")
    if len(js_links) != len(links):
        raise CheckFailure(
            f"graph.json has {len(links)} links but graph-data.js has {len(js_links)}"
        )

    if ctx.health is not None and ctx.health.get("notes") != len(nodes):
        raise CheckFailure(
            f"/health reports {ctx.health.get('notes')} notes but the graph has {len(nodes)}"
        )

    ctx.graph = data
    return Outcome(
        "pass",
        f"{len(nodes)} nodes · {len(links)} links · graph.json and graph-data.js agree",
    )


def check_chat(ctx: Context) -> Outcome:
    if ctx.graph is None:
        raise CheckFailure("graph data is unavailable, so /chat cannot be checked")
    graph_ids = {node["id"] for node in ctx.graph["nodes"]}
    question = "What are the café's opening hours?"
    session_id = "preflight-chat-" + ctx.nonce
    status, headers, body = request(
        ctx.base_url + "/chat",
        method="POST",
        payload={"question": question, "session_id": session_id},
        timeout=120,
    )
    if status != 200:
        detail = snippet(body.decode("utf-8", "replace"))
        raise CheckFailure(f"POST /chat -> HTTP {status}: {detail}")
    content_type = header(headers, "Content-Type")
    if "application/json" not in content_type.lower():
        raise CheckFailure(f"POST /chat returned {content_type or 'no content type'}, not JSON")
    data = json_body(body, "POST /chat")
    if not isinstance(data, dict):
        raise CheckFailure("POST /chat did not return a JSON object")
    if data.get("error"):
        raise CheckFailure(f"POST /chat returned an error: {snippet(data['error'])}")
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise CheckFailure("POST /chat returned no answer text")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise CheckFailure("POST /chat returned no nodes array")
    if not nodes:
        raise CheckFailure("POST /chat returned an empty nodes array for a known question")
    for node in nodes:
        if not isinstance(node, int) or isinstance(node, bool):
            raise CheckFailure(f"POST /chat returned a non-integer node id: {snippet(node)}")
        if node not in graph_ids:
            raise CheckFailure(f"POST /chat returned node {node}, which is not in the graph")
    if len(set(nodes)) != len(nodes):
        raise CheckFailure(f"POST /chat returned duplicate node ids: {nodes}")
    return Outcome(
        "pass",
        f"HTTP 200 · {len(answer.strip())} chars · nodes {nodes[:8]}"
        f"{'…' if len(nodes) > 8 else ''} · {snippet(answer.strip(), 90)!r}",
    )


def check_api_key(ctx: Context) -> Outcome:
    config = load_config()
    ctx.config = config
    url = config["base_url"].rstrip("/") + "/models"
    status, _, body = request(
        url,
        headers={"Authorization": "Bearer " + config["api_key"]},
        timeout=30,
    )
    if status in (401, 403):
        raise CheckFailure(f"FreeLLMAPI rejected the API key (HTTP {status})")
    if status != 200:
        raise CheckFailure(f"GET /models -> HTTP {status}: {snippet(body.decode('utf-8', 'replace'))}")
    data = json_body(body, "GET /models")
    if not isinstance(data, dict):
        raise CheckFailure("GET /models did not return a JSON object")
    if data.get("error"):
        raise CheckFailure(f"GET /models returned an error: {snippet(data['error'])}")
    models = data.get("data")
    if not isinstance(models, list):
        raise CheckFailure("GET /models has no data array")
    ctx.models = models
    return Outcome(
        "pass",
        f"GET /models -> 200 · {len(models)} models · key accepted",
    )


def check_model(ctx: Context) -> Outcome:
    if ctx.config is None:
        raise CheckFailure("config.json is unavailable, so the model cannot be checked")
    model = ctx.config["model"]
    encoded = urllib.parse.quote(model, safe="")
    url = ctx.config["base_url"].rstrip("/") + "/models/" + encoded
    status, _, body = request(
        url,
        headers={"Authorization": "Bearer " + ctx.config["api_key"]},
        timeout=30,
    )
    if status == 404:
        raise CheckFailure(f'Model "{model}" is not reachable: GET /models/{encoded} -> 404')
    if status in (401, 403):
        raise CheckFailure(f'Model "{model}" could not be checked: the API key was rejected (HTTP {status})')
    if status != 200:
        raise CheckFailure(
            f'Model "{model}" could not be checked: GET /models/{encoded} -> HTTP {status}: '
            f"{snippet(body.decode('utf-8', 'replace'))}"
        )
    data = json_body(body, f"GET /models/{encoded}")
    if isinstance(data, dict):
        if data.get("error"):
            raise CheckFailure(f'Model "{model}" is not reachable: {snippet(data["error"])}')
        returned_id = data.get("id")
        if returned_id != model:
            raise CheckFailure(
                f'Model "{model}" is not reachable: the Models API returned id '
                f'"{snippet(returned_id, 80)}"'
            )
        if data.get("available") is False:
            reason = data.get("unavailable_reason") or "marked unavailable by the gateway"
            raise CheckFailure(f'Model "{model}" is not available: {snippet(reason, 120)}')
    elif isinstance(data, list):
        found = next(
            (item for item in data if isinstance(item, dict) and item.get("id") == model),
            None,
        )
        if found is None:
            raise CheckFailure(f'Model "{model}" is not reachable: it is not in the Models API response')
        if found.get("available") is False:
            reason = found.get("unavailable_reason") or "marked unavailable by the gateway"
            raise CheckFailure(f'Model "{model}" is not available: {snippet(reason, 120)}')
    else:
        raise CheckFailure(f'Model "{model}" could not be checked: unexpected Models API response')

    if ctx.models is not None:
        listed = {item.get("id") for item in ctx.models if isinstance(item, dict)}
        if model not in listed:
            raise CheckFailure(f'Model "{model}" is not listed by GET /models')
    return Outcome(
        "pass",
        f'model "{model}" reachable · GET /models/{encoded} -> 200 · available',
    )


def _cleanup_remember(
    ctx: Context,
    marker: str,
    note_path: Path | None,
    note_id: int | None,
    original_count: int,
    original_labels: list[str],
) -> list[str]:
    errors: list[str] = []
    try:
        if note_path is not None and note_path.exists():
            note_path.unlink()
        if CAPTURES_DIR.exists():
            for candidate in CAPTURES_DIR.glob("*.md"):
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if marker in text:
                    candidate.unlink(missing_ok=True)
    except Exception as exc:
        errors.append(f"could not remove the test note: {exc}")

    try:
        build = subprocess.run(
            [sys.executable, str(ROOT / "build.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if build.returncode != 0:
            tail = (build.stderr or build.stdout or "").strip().splitlines()[-1:] or ["no output"]
            errors.append(f"build.py failed after cleanup: {tail[0]}")
    except Exception as exc:
        errors.append(f"build.py could not run after cleanup: {exc}")

    try:
        status, _, body = request(
            ctx.base_url + "/reindex",
            method="POST",
            payload={},
            timeout=30,
        )
        if status != 200:
            errors.append(f"POST /reindex -> HTTP {status}")
        else:
            data = json_body(body, "POST /reindex")
            if not isinstance(data, dict) or data.get("ok") is not True:
                errors.append(f"POST /reindex did not succeed: {snippet(data)}")
            elif data.get("notes") != original_count:
                errors.append(
                    f"re-index reports {data.get('notes')} notes, expected {original_count}"
                )
    except Exception as exc:
        errors.append(f"POST /reindex failed: {exc}")

    try:
        graph = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not isinstance(nodes, list):
            errors.append("viewer/graph.json has no nodes after cleanup")
        else:
            if len(nodes) != original_count:
                errors.append(
                    f"viewer/graph.json has {len(nodes)} nodes after cleanup, expected {original_count}"
                )
            labels = [str(node.get("label", "")) for node in nodes if isinstance(node, dict)]
            if collections.Counter(labels) != collections.Counter(original_labels):
                errors.append("the graph labels changed after cleanup")
            if any(marker.lower() in label.lower() for label in labels):
                errors.append("the test note is still present in viewer/graph.json")
    except Exception as exc:
        errors.append(f"viewer/graph.json could not be checked after cleanup: {exc}")

    if note_id is not None:
        try:
            status, _, body = request(
                ctx.base_url + "/chat",
                method="POST",
                payload={"question": marker, "session_id": "preflight-cleanup-" + ctx.nonce},
                timeout=90,
            )
            if status != 200:
                errors.append(f"cleanup /chat -> HTTP {status}")
            else:
                data = json_body(body, "cleanup /chat")
                if isinstance(data, dict) and note_id in (data.get("nodes") or []):
                    errors.append(f"node {note_id} is still retrievable after cleanup")
        except Exception as exc:
            errors.append(f"cleanup /chat failed: {exc}")
    return errors


def check_remember(ctx: Context) -> Outcome:
    if not is_local(ctx.base_url):
        return Outcome("warn", "/remember is local-only; skipped on a non-local target")
    if ctx.graph is None:
        raise CheckFailure("/remember cannot be checked without graph data")

    marker = "preflight-" + ctx.nonce
    original_count = len(ctx.graph["nodes"])
    original_labels = [node["label"] for node in ctx.graph["nodes"]]
    note_path: Path | None = None
    note_id: int | None = None
    detail = ""
    main_error: CheckFailure | None = None

    try:
        text = (
            f"Remember that preflight note {marker} says the launch code is {marker}."
        )
        status, _, body = request(
            ctx.base_url + "/remember",
            method="POST",
            payload={"text": text},
            timeout=60,
        )
        if status != 200:
            raise CheckFailure(
                f"POST /remember -> HTTP {status}: {snippet(body.decode('utf-8', 'replace'))}"
            )
        data = json_body(body, "POST /remember")
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise CheckFailure(f"POST /remember did not succeed: {snippet(data)}")
        note_id = data.get("id")
        if not isinstance(note_id, int) or isinstance(note_id, bool):
            raise CheckFailure(f"POST /remember returned an invalid node id: {snippet(note_id)}")
        file_value = data.get("file")
        if not isinstance(file_value, str) or not file_value.strip():
            raise CheckFailure("POST /remember returned no file path")
        note_path = (ROOT / file_value).resolve()
        captures = CAPTURES_DIR.resolve()
        if captures not in note_path.parents:
            raise CheckFailure(f"POST /remember wrote outside notes/captures: {file_value}")
        if not note_path.exists():
            raise CheckFailure(f"POST /remember reported {file_value}, but the file does not exist")
        content = note_path.read_text(encoding="utf-8", errors="replace")
        if marker not in content:
            raise CheckFailure(f"{file_value} does not contain the test marker")

        question = f"What is the launch code in preflight note {marker}?"
        status, _, body = request(
            ctx.base_url + "/chat",
            method="POST",
            payload={"question": question, "session_id": "preflight-remember-" + ctx.nonce},
            timeout=120,
        )
        if status != 200:
            raise CheckFailure(
                f"POST /chat after /remember -> HTTP {status}: "
                f"{snippet(body.decode('utf-8', 'replace'))}"
            )
        chat = json_body(body, "POST /chat after /remember")
        if not isinstance(chat, dict) or chat.get("error"):
            raise CheckFailure(f"/chat after /remember returned an error: {snippet(chat)}")
        nodes = chat.get("nodes")
        if not isinstance(nodes, list):
            raise CheckFailure("/chat after /remember returned no nodes array")
        if note_id not in nodes:
            raise CheckFailure(
                f"/remember wrote node {note_id}, but /chat did not retrieve it "
                f"(nodes: {nodes})"
            )
        answer = chat.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise CheckFailure("/chat after /remember returned no answer text")
        detail = (
            f"wrote {file_value} · /chat cited node {note_id} · "
            f"{snippet(answer.strip(), 80)!r}"
        )
    except CheckFailure as exc:
        main_error = exc
    except Exception as exc:
        main_error = CheckFailure(f"unexpected /remember error: {type(exc).__name__}: {exc}")

    cleanup_errors = _cleanup_remember(
        ctx, marker, note_path, note_id, original_count, original_labels
    )
    if cleanup_errors:
        message = "test note cleanup failed: " + "; ".join(cleanup_errors)
        if main_error is not None:
            message = f"{main_error.message}; cleanup also failed: " + "; ".join(cleanup_errors)
        raise CheckFailure(message)
    if main_error is not None:
        raise main_error
    return Outcome("pass", detail + " · removed and re-indexed")


def check_served_files(ctx: Context) -> Outcome:
    pairs = [
        ("/", VIEWER / "index.html"),
        ("/graph-data.js", GRAPH_JS),
        ("/graph.json", GRAPH_JSON),
        ("/favicon.svg", VIEWER / "favicon.svg"),
    ]
    for url_path, disk_path in pairs:
        if not disk_path.exists():
            raise CheckFailure(f"{disk_path.relative_to(ROOT)} is missing on disk")
        status, _, body = request(
            ctx.base_url + url_path + "?preflight=" + ctx.nonce, timeout=15
        )
        if status != 200:
            raise CheckFailure(f"GET {url_path} -> HTTP {status}")
        disk = disk_path.read_bytes()
        if body != disk:
            offset = first_difference(body, disk)
            served_slice = body[max(0, offset - 12) : offset + 20]
            disk_slice = disk[max(0, offset - 12) : offset + 20]
            raise CheckFailure(
                f"STALE SERVED FILE: {url_path} differs from {disk_path.name} "
                f"(served {len(body):,} bytes sha {short_hash(body)}, "
                f"disk {len(disk):,} bytes sha {short_hash(disk)}; "
                f"first diff at byte {offset}: served {served_slice!r} vs disk {disk_slice!r})"
            )
    names = ", ".join(path.name for _, path in pairs)
    return Outcome("pass", f"{len(pairs)}/{len(pairs)} byte-identical · {names}")


def check_config_secret(ctx: Context) -> Outcome:
    api_key = ctx.config["api_key"] if ctx.config else ""
    problems: list[str] = []
    paths = [
        "/config.json",
        "/config.json?preflight=" + ctx.nonce,
        "/./config.json",
        "/%2e%2e/config.json",
        "/..%2fconfig.json",
        "/..%5cconfig.json",
        "/%2e%2e%5cconfig.json",
        "/%2e%2e%2fconfig.json",
        "/%63onfig.json",
        "/CONFIG.JSON",
    ]
    for path in paths:
        try:
            status, headers, body = request(ctx.base_url + path, timeout=10)
        except CheckFailure as exc:
            problems.append(f"{path}: {exc}")
            continue
        if status in (404, 401, 403):
            continue
        if status != 200:
            problems.append(f"{path} -> HTTP {status} (could not verify)")
            continue
        text = body.decode("utf-8", "replace")
        exposed = any(
            field in text
            for field in ("freellmapi_api_key", "freellmapi_base_url", "freellmapi_model")
        )
        if api_key and api_key in text:
            exposed = True
        content_type = header(headers, "Content-Type").lower()
        if exposed:
            problems.append(f"{path} returned HTTP 200 and exposed config data")
        elif "json" in content_type:
            problems.append(f"{path} returned HTTP 200 JSON")

    for path, disk_path in (("/", VIEWER / "index.html"), ("/graph-data.js", GRAPH_JS)):
        try:
            status, _, body = request(
                ctx.base_url + path + "?preflight=" + ctx.nonce, timeout=10
            )
            if status != 200:
                problems.append(f"{path} -> HTTP {status} (could not scan for secrets)")
                continue
            text = body.decode("utf-8", "replace")
            if api_key and api_key in text:
                problems.append(f"{path} contains the FreeLLMAPI API key literal")
            if "freellmapi_api_key" in text:
                problems.append(f"{path} contains the freellmapi_api_key field name")
        except CheckFailure as exc:
            problems.append(f"{path}: {exc}")

    if (VIEWER / "config.json").exists():
        problems.append("viewer/config.json exists on disk and would be served")

    if problems:
        raise CheckFailure("; ".join(problems), loud=True)
    return Outcome(
        "pass",
        "/config.json -> 404 · traversal variants blocked · key absent from served HTML/JS",
    )


# ---------------------------------------------------------------------------
# headless browser check
# ---------------------------------------------------------------------------


def find_browser() -> str | None:
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_json(url: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.load(response)
        except Exception as exc:
            last = exc
            time.sleep(0.25)
    raise CheckFailure(f"the browser's DevTools endpoint did not start: {last}")


def check_mute(ctx: Context) -> Outcome:
    if ctx.skip_browser:
        return Outcome("warn", "skipped by --no-browser")
    browser = find_browser()
    if browser is None:
        return Outcome("warn", "no Chrome or Edge found; mute check skipped")
    try:
        from websockets.sync.client import connect
    except Exception as exc:
        return Outcome("warn", f"websockets is unavailable, so the mute check was skipped ({exc})")

    port = free_port()
    profile = tempfile.mkdtemp(prefix="kg-preflight-")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--enable-unsafe-swiftshader",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    events: list[dict] = []
    try:
        wait_json(f"http://127.0.0.1:{port}/json/version")
        targets = wait_json(f"http://127.0.0.1:{port}/json/list")
        page = next((item for item in targets if item.get("type") == "page"), None)
        if page is None:
            raise CheckFailure("the headless browser did not expose a page target")

        with connect(
            page["webSocketDebuggerUrl"],
            open_timeout=10,
            close_timeout=2,
            max_size=None,
        ) as ws:
            next_id = 0

            def call(method: str, params: dict | None = None) -> dict:
                nonlocal next_id
                next_id += 1
                ws.send(json.dumps({"id": next_id, "method": method, "params": params or {}}))
                while True:
                    try:
                        message = json.loads(ws.recv(timeout=45))
                    except TimeoutError:
                        raise CheckFailure(f"the browser did not answer {method} within 45s")
                    if message.get("id") == next_id:
                        if "error" in message:
                            raise CheckFailure(f"browser {method} failed: {snippet(message['error'])}")
                        return message.get("result", {})
                    events.append(message)
                    if len(events) > 200:
                        del events[:100]

            call("Page.enable")
            call("Runtime.enable")
            call(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                    (() => {
                      window.__preflightSpeakCalls = 0;
                      window.__preflightSpoken = [];
                      const synth = window.speechSynthesis;
                      if (!synth) return;
                      const original = synth.speak;
                      const spy = function (...args) {
                        window.__preflightSpeakCalls += 1;
                        const utterance = args[0];
                        if (utterance && utterance.text) window.__preflightSpoken.push(utterance.text);
                        return original.apply(synth, args);
                      };
                      try {
                        synth.speak = spy;
                      } catch (error) {
                        try {
                          Object.defineProperty(synth, 'speak', {
                            configurable: true,
                            value: spy
                          });
                        } catch (inner) {
                          window.__preflightSpeakSpyError = String(inner);
                        }
                      }
                    })();
                    """
                },
            )
            url = (
                ctx.base_url.rstrip("/")
                + "/?mute=1&debug=1&preflight="
                + ctx.nonce
            )
            call("Page.navigate", {"url": url})

            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    ready = call(
                        "Runtime.evaluate",
                        {
                            "expression": "typeof window.__kgSpeak === 'function'",
                            "returnByValue": True,
                        },
                    )
                    if ready.get("result", {}).get("value") is True:
                        break
                except CheckFailure:
                    pass
                time.sleep(0.25)
            else:
                page_errors = []
                try:
                    result = call(
                        "Runtime.evaluate",
                        {
                            "expression": "JSON.stringify(window.__kgErrors || [])",
                            "returnByValue": True,
                        },
                    )
                    page_errors = json.loads(result.get("result", {}).get("value") or "[]")
                except Exception:
                    pass
                browser_errors = []
                for event in events:
                    if event.get("method") == "Runtime.exceptionThrown":
                        details = event.get("params", {}).get("exceptionDetails", {})
                        browser_errors.append(
                            details.get("text")
                            or details.get("exception", {}).get("description", "")
                        )
                details = "; ".join(
                    [str(item) for item in page_errors + browser_errors if item][-5:]
                )
                raise CheckFailure(
                    "the viewer did not expose the debug speak hook within 45s"
                    + (f": {snippet(details, 300)}" if details else "")
                )

            expression = """
            (async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const line = document.getElementById('voice-line');
              const text = document.getElementById('voice-text');
              const bootCalls = window.__preflightSpeakCalls || 0;
              const bootState = line?.dataset.state || '';
              window.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
              await sleep(150);
              const afterUnlock = window.__preflightSpeakCalls || 0;
              const spokenAfterUnlock = [...(window.__preflightSpoken || [])];
              window.__preflightSpeakCalls = 0;
              window.__preflightSpoken = [];
              line.dataset.state = 'idle';
              text.textContent = 'voice ready';
              const before = line.dataset.state;
              window.__kgSpeak('preflight mute check');
              await sleep(150);
              return {
                mute: new URLSearchParams(location.search).get('mute'),
                hasSpeak: typeof window.__kgSpeak === 'function',
                hasSynth: !!window.speechSynthesis,
                bootCalls,
                bootState,
                afterUnlock,
                spokenAfterUnlock,
                spyError: window.__preflightSpeakSpyError || null,
                before,
                after: line.dataset.state,
                calls: window.__preflightSpeakCalls || 0,
                spoken: [...(window.__preflightSpoken || [])],
                text: text.textContent,
                errors: (window.__kgErrors || []).map(String)
              };
            })()
            """
            result = call(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True},
            )
            value = result.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise CheckFailure(f"the mute probe returned no result: {snippet(result)}")
            if value.get("mute") != "1":
                raise CheckFailure("the mute tab did not carry ?mute=1")
            if value.get("hasSpeak") is not True:
                raise CheckFailure("the debug speak hook is missing from the served page")
            if value.get("spyError"):
                raise CheckFailure(f"could not install the speech spy: {value['spyError']}")
            if value.get("hasSynth"):
                if (value.get("afterUnlock") or 0) < 1:
                    raise CheckFailure(
                        "audio could not be unlocked, so the mute check is inconclusive"
                    )
                spoken_after_unlock = value.get("spokenAfterUnlock") or []
                if any("hello" in str(item).lower() for item in spoken_after_unlock):
                    raise CheckFailure(
                        "the boot greeting escaped ?mute=1 and reached speech synthesis"
                    )
            if value.get("before") != "idle":
                raise CheckFailure(
                    f"the voice line did not start from idle (was {value.get('before')!r})"
                )
            if value.get("after") != "muted":
                raise CheckFailure(
                    f"speak() did not take the mute branch (voice state is {value.get('after')!r})"
                )
            if value.get("calls") != 0:
                raise CheckFailure(
                    f"?mute=1 did not silence the tab: {value.get('calls')} speech call(s)"
                )
            if value.get("spoken"):
                raise CheckFailure(f"?mute=1 still queued speech: {value.get('spoken')}")
            if "silent" not in str(value.get("text", "")).lower():
                raise CheckFailure("the voice line does not report the muted state")
            detail = "?mute=1 · boot greeting suppressed · audio unlocked · speak() refused · 0 speech calls"
            if not value.get("hasSynth"):
                detail += " · speech synthesis unavailable in this browser"
            return Outcome("pass", detail)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def check_teardown_diag(ctx: Context) -> Outcome:
    status, _, body = request(
        ctx.base_url + "/teardown/diag?preflight=" + ctx.nonce, timeout=30
    )
    if status != 200:
        raise CheckFailure(f"GET /teardown/diag -> HTTP {status}")
    data = json_body(body, "/teardown/diag")
    if not isinstance(data, dict):
        raise CheckFailure("/teardown/diag did not return a JSON object")
    if data.get("ready") is not True:
        problems = "; ".join(str(item) for item in (data.get("problems") or []))
        raise CheckFailure(f"the teardown bay is not ready: {problems or 'unknown problem'}")
    expected = data.get("expected_version")
    if not expected:
        raise CheckFailure("/teardown/diag does not report the expected REA version")
    if data.get("installed_version") != expected:
        raise CheckFailure(
            f"REA {data.get('installed_version')} is installed, expected {expected}"
        )
    if data.get("pinned_version") != expected:
        raise CheckFailure(f"REA is pinned to {data.get('pinned_version')}, expected {expected}")
    if data.get("provenance_ok") is not True:
        raise CheckFailure("REA did not come from github.com/morluto/rea")
    node = data.get("node") if isinstance(data.get("node"), dict) else {}
    if node.get("ok") is not True:
        raise CheckFailure(f"Node.js is not usable: {snippet(node)}")
    chrome = data.get("chrome") if isinstance(data.get("chrome"), dict) else {}
    if chrome.get("found") is not True:
        raise CheckFailure("Google Chrome was not found")
    if data.get("problems"):
        raise CheckFailure(f"teardown diag still reports problems: {data['problems']}")
    return Outcome(
        "pass",
        f"ready · REA {expected} pinned · Node {node.get('version')} · "
        f"Chrome found · {data.get('tools_home')}",
    )


def check_teardown_observe(ctx: Context) -> Outcome:
    status, _, body = request(
        ctx.base_url + "/teardown",
        method="POST",
        payload={"url": "https://example.com"},
        timeout=300,
    )
    if status != 200:
        raise CheckFailure(f"POST /teardown -> HTTP {status}")
    data = json_body(body, "POST /teardown")
    if not isinstance(data, dict):
        raise CheckFailure("POST /teardown did not return a JSON object")
    if data.get("ok") is not True:
        raise CheckFailure(f"live REA observation failed: {snippet(data.get('reply'))}")
    command = data.get("command")
    if command != "capture-browser-scenario":
        raise CheckFailure(f"REA used an unexpected command: {snippet(command)}")
    evidence = data.get("data")
    if not isinstance(evidence, dict) or not evidence.get("evidence_id"):
        raise CheckFailure("REA returned no evidence for the observed website")
    reply = str(data.get("reply") or "")
    if "read-only" not in reply.lower():
        raise CheckFailure(f"the teardown reply does not state it was read-only: {snippet(reply)}")
    return Outcome(
        "pass",
        f"observed https://example.com · {command} · evidence {str(evidence['evidence_id'])[:12]}…",
    )


def check_teardown_law(ctx: Context) -> Outcome:
    cases = [
        ({"url": "file:///C:/Windows/win.ini"}, "not a website"),
        ({"url": "http://127.0.0.1:4700/"}, "private"),
        ({"url": "https://example.com", "command": "click"}, "read-only"),
        ({"url": "https://user:pass@example.com"}, "credentials"),
    ]
    for payload, expected in cases:
        status, _, body = request(
            ctx.base_url + "/teardown", method="POST", payload=payload, timeout=30
        )
        if status != 200:
            raise CheckFailure(f"POST /teardown {payload} -> HTTP {status}")
        data = json_body(body, "POST /teardown law check")
        if not isinstance(data, dict) or data.get("ok") is not False:
            raise CheckFailure(f"a forbidden target was accepted: {payload}")
        reply = str(data.get("reply") or "")
        if expected not in reply.lower() and "nothing was touched" not in reply.lower():
            raise CheckFailure(f"unexpected refusal for {payload}: {snippet(reply)}")
        if "\n" in reply:
            raise CheckFailure(f"a teardown refusal was not one line: {payload}")
    return Outcome(
        "pass",
        f"{len(cases)}/{len(cases)} refusals · file, private, interactive, credentials",
    )


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


CHECKS = [
    ("Server is up and serving the viewer", check_server),
    ("Graph data loads and the node count is greater than zero", check_graph),
    ("/chat returns a well-formed answer with a nodes array", check_chat),
    ("The API key in config.json is valid", check_api_key),
    ("The configured model is reachable by id", check_model),
    ("/remember writes a note and /chat retrieves it", check_remember),
    ("Files served to the browser match the files on disk", check_served_files),
    ("config.json is not reachable from the browser", check_config_secret),
    ("A ?mute=1 tab is silent", check_mute),
    ("Teardown bay reports ready with pinned REA", check_teardown_diag),
    ("Teardown observes a public website read-only", check_teardown_observe),
    ("Teardown refuses non-website and interactive targets", check_teardown_law),
]


def print_outcome(label: str, outcome: Outcome) -> None:
    symbol = {"pass": "✓", "fail": "✗", "warn": "!"}.get(outcome.status, "?")
    print(f"[{symbol}] {label}")
    detail = (outcome.detail or "").strip()
    if detail:
        for line in detail.splitlines():
            wrapped = textwrap.wrap(line, width=DETAIL_WIDTH) or [""]
            for part in wrapped:
                print("    " + part)
    if outcome.status == "fail" and outcome.loud:
        print("    " + "!" * 68)
        print("    SECURITY FAILURE: the browser can read the server configuration.")
        print("    " + "!" * 68)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run live preflight checks against the knowledge galaxy.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PREFLIGHT_BASE_URL", DEFAULT_BASE_URL),
        help=f"server to test (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="skip the headless ?mute=1 check",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        parser.error("--base-url must start with http:// or https://")

    ctx = Context(
        base_url=base_url,
        nonce=secrets.token_hex(6),
        skip_browser=args.no_browser,
    )

    print("Northwind Knowledge Galaxy — live preflight")
    print(f"Target: {base_url}")
    print()

    counts = {"pass": 0, "fail": 0, "warn": 0}
    for label, check in CHECKS:
        try:
            outcome = check(ctx)
        except CheckFailure as exc:
            outcome = Outcome("fail", exc.message, exc.loud)
        except Exception as exc:
            outcome = Outcome("fail", f"unexpected error: {type(exc).__name__}: {exc}")
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        print_outcome(label, outcome)

    print()
    print(f"{counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
