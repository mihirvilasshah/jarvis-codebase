"""THE BAY - Jarvis-owned Chrome + read-only REA website evidence capture.

Standard library only. Soft-imported by server.py, so Jarvis boots without it.

Five evidence steps, each labelled with who captured it:
  REA     - watch while scrolling, read the code, snapshot the page
  Jarvis  - resource log from our browser, license fingerprints from REA's code

REA never launches a browser here: we own a fresh Chrome profile, attach REA to
it, and always tear it down in a finally block. No clicks, typing, logins, or
changes; only public http(s) websites.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PACKAGE = "rea-agents"
EXPECTED_VERSION = "6.10.1"
REPO_HINT = "github.com/morluto/rea"
TOOLS_HOME = Path.home() / ".jarvis-tools" / "rea"
PROFILES_HOME = TOOLS_HOME / "profiles"
SNAPSHOTS_HOME = TOOLS_HOME / "snapshots"
PACKAGE_JSON = TOOLS_HOME / "node_modules" / PACKAGE / "package.json"
PIN_FILE = TOOLS_HOME / "package.json"
REA_SCRIPT = TOOLS_HOME / "node_modules" / PACKAGE / "scripts" / "rea.mjs"

CDP_HOST = "127.0.0.1"
CDP_PORT = 9333
WINDOW_SIZE = "1280,820"
INTRO_WAIT_SECONDS = 2.5
CDP_WAIT_SECONDS = 25.0
WHEEL_STEPS = 16
WHEEL_STEP_SECONDS = 0.12

ATTACH_COMMANDS = {"analyze-web-bundle", "inspect-web-page", "capture-web-screenshot", "discover-webmcp-tools"}
DEFAULT_COMMAND = "analyze-web-bundle"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NODE_WINDOWS = [r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"]
NODE_UNIX = ["/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"]
CHROME_WINDOWS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_MAC = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
CHROME_LINUX = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]

ATTRIBUTION_NOTE = (
    "Every evidence line below is labelled REA (captured by REA) or Jarvis "
    "(read from our own browser). Keep that attribution straight in any answer."
)


def _snippet(value, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# environment discovery
# ---------------------------------------------------------------------------


def node_version_ok(version: str) -> tuple[bool, str]:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(version).strip())
    if not match:
        return False, "22.23.4+ (or 24.22.1+, or 26.12.1+)"
    major, minor, patch = (int(part) for part in match.groups())
    if major == 22:
        return (minor, patch) >= (23, 4), "22.23.4+"
    if major == 24:
        return (minor, patch) >= (22, 1), "24.22.1+"
    if major >= 26:
        return (major, minor, patch) >= (26, 12, 1), "26.12.1+"
    return False, "22.23.4+ (or 24.22.1+, or 26.12.1+)"


def find_node() -> tuple[str | None, str | None, bool, str]:
    candidates: list[str] = []
    which = shutil.which("node")
    if which:
        candidates.append(which)
    candidates.extend(NODE_WINDOWS)
    candidates.extend(NODE_UNIX)
    version_dir = Path.home() / ".nvm" / "versions" / "node"
    if version_dir.exists():
        for child in sorted(version_dir.iterdir(), reverse=True):
            for relative in ("node.exe", "bin/node"):
                candidate = child / relative
                if candidate.exists():
                    candidates.append(str(candidate))
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(str(Path(localappdata) / "Programs" / "nodejs" / "node.exe"))

    seen: set[str] = set()
    first_path: str | None = None
    first_version: str | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            result = subprocess.run(
                [str(path), "--version"], capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            version = result.stdout.strip() or result.stderr.strip()
        except Exception:
            continue
        if first_version is None:
            first_path, first_version = str(path), version
        ok, need = node_version_ok(version)
        if ok:
            return str(path), version, True, ""
    if first_version is not None:
        _, need = node_version_ok(first_version)
        return first_path, first_version, False, need
    return None, None, False, "Node.js was not found"


def find_chrome() -> str | None:
    candidates = list(CHROME_WINDOWS) + list(CHROME_MAC) + list(CHROME_LINUX)
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(str(Path(localappdata) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    candidates.append(str(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _repository_ok(package: dict | None) -> bool:
    if not isinstance(package, dict):
        return False
    repo = package.get("repository")
    url = repo.get("url") if isinstance(repo, dict) else (repo if isinstance(repo, str) else "")
    return REPO_HINT in str(url).lower()


def diagnose() -> dict:
    installed = _read_json(PACKAGE_JSON)
    pin = _read_json(PIN_FILE)
    installed_version = installed.get("version") if isinstance(installed, dict) else None
    pinned = None
    if isinstance(pin, dict) and isinstance(pin.get("dependencies"), dict):
        pinned = pin["dependencies"].get(PACKAGE)
    node_path, node_version, node_ok, node_need = find_node()
    chrome = find_chrome()
    script_ok = REA_SCRIPT.exists()
    provenance_ok = _repository_ok(installed)

    problems: list[str] = []
    if installed_version is None:
        problems.append(f"REA is not installed. Install {PACKAGE} {EXPECTED_VERSION} into {TOOLS_HOME}.")
    elif installed_version != EXPECTED_VERSION:
        problems.append(f"REA {installed_version} is installed; pin {EXPECTED_VERSION} instead.")
    if pinned != EXPECTED_VERSION:
        problems.append(f"REA is not pinned to {EXPECTED_VERSION} in {PIN_FILE}.")
    if not script_ok:
        problems.append(f"The REA launcher is missing at {REA_SCRIPT}.")
    if installed is not None and not provenance_ok:
        problems.append("REA did not come from github.com/morluto/rea; reinstall from the official repo.")
    if not node_path:
        problems.append("Node.js was not found on PATH or in common install folders.")
    elif not node_ok:
        problems.append(f"Node.js {node_version} is too old; install {node_need}.")
    if not chrome:
        problems.append("Google Chrome was not found; install Google Chrome.")

    return {
        "ready": not problems,
        "installed_version": installed_version,
        "pinned_version": pinned,
        "expected_version": EXPECTED_VERSION,
        "repository": (
            (installed or {}).get("repository", {}).get("url")
            if isinstance(installed, dict) and isinstance(installed.get("repository"), dict) else None
        ),
        "provenance_ok": provenance_ok,
        "node": {"path": node_path, "version": node_version, "ok": node_ok, "need": node_need},
        "chrome": {"path": chrome, "found": bool(chrome)},
        "rea_script": str(REA_SCRIPT),
        "tools_home": str(TOOLS_HOME),
        "cdp_port": CDP_PORT,
        "problems": problems,
        "law": "REA observes only: no clicks, typing, logins, or changes; websites only.",
    }


def missing_line(diag: dict) -> str:
    problems = [str(item) for item in (diag.get("problems") or [])]
    if not problems:
        return "The teardown bay is ready, sir."
    joined = " ".join(problems)
    fixes: list[str] = []
    if any(word in joined for word in ("REA", "REA launcher", "pinned", "official repo", "not installed")):
        fixes.append(
            f"install REA {EXPECTED_VERSION} into {TOOLS_HOME} with "
            f"`npm install rea-agents@{EXPECTED_VERSION} --save-exact --ignore-scripts`"
        )
    if "Node.js" in joined:
        fixes.append(f"install Node.js {(diag.get('node') or {}).get('need') or '22.23.4+'}")
    if "Chrome" in joined:
        fixes.append("install Google Chrome")
    if not fixes:
        fixes.append(problems[0])
    return "I'm afraid the teardown bay is not ready, sir: " + " and ".join(fixes) + ". Nothing was touched."


def validate_website(url) -> tuple[bool, str]:
    if not isinstance(url, str) or not url.strip():
        return False, "Give me a public website URL, sir."
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "That URL could not be parsed, sir."
    if parsed.scheme not in ("http", "https"):
        return False, "Only public http and https websites may be observed."
    if parsed.username or parsed.password:
        return False, "URLs with credentials are not allowed."
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "That URL has no host, sir."
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return False, "Local websites are not allowed."
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified:
            return False, "Private, local, or reserved addresses are not allowed."
    except ValueError:
        if host.endswith((".local", ".internal", ".test", ".invalid")):
            return False, "Local or internal names are not allowed."
    return True, ""


# ---------------------------------------------------------------------------
# minimal standard-library DevTools WebSocket client
# ---------------------------------------------------------------------------


class CDPClient:
    """Handshake, masked frames, large/fragmented replies, ping and pong."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        parsed = urlparse(ws_url)
        self.host = parsed.hostname or CDP_HOST
        self.port = parsed.port or 80
        self.path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.next_id = 0
        self.events: list[dict] = []

    def connect(self) -> "CDPClient":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake closed before headers")
            response += chunk
            if len(response) > 65536:
                raise RuntimeError("WebSocket handshake headers too large")
        header, _, rest = response.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(f"WebSocket handshake failed: {status[:120]!r}")
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        expected = base64.b64encode(hashlib.sha1((key + magic).encode("ascii")).digest()).decode("ascii")
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-accept:"):
                if line.split(b":", 1)[1].strip().decode("ascii", "replace") != expected:
                    raise RuntimeError("WebSocket Sec-WebSocket-Accept mismatch")
        self.sock = sock
        self.buffer = rest
        return self

    def _recv_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(4096, count - len(self.buffer)))
            if not chunk:
                raise RuntimeError("WebSocket connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            head = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if second & 0x80 else None
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("WebSocket close frame received")
        if opcode == 0x9:
            self.send_frame(0xA, payload)
            return self._frame()
        if opcode == 0xA:
            return self._frame()
        return fin, opcode, payload

    def recv_message(self) -> bytes:
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x1:
                if fin:
                    return b"".join(parts) + payload
                parts = [payload]
            elif opcode == 0x0:
                parts.append(payload)
                if fin:
                    return b"".join(parts)

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        self.send_frame(0x1, json.dumps(message, ensure_ascii=False).encode("utf-8"))
        while True:
            data = json.loads(self.recv_message().decode("utf-8"))
            if data.get("id") == self.next_id:
                if "error" in data:
                    raise RuntimeError(f"{method} failed: {_snippet(data['error'])}")
                return data.get("result", {})
            self.events.append(data)

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return result.get("result", {}).get("value")

    def wheel(self, delta_y: int, x: int = 640, y: int = 410) -> None:
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": delta_y, "pointerType": "mouse"},
        )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
# browser lifecycle
# ---------------------------------------------------------------------------


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((CDP_HOST, port))
            return True
        except OSError:
            return False


def _http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def _normal_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_matches(page_url: str, target_url: str) -> bool:
    page = _normal_host(urlparse(page_url).hostname or "")
    target = _normal_host(urlparse(target_url).hostname or "")
    if not page or not target:
        return False
    return page == target or page.endswith("." + target) or target.endswith("." + page)


def _profile_pids(marker: str) -> list[int]:
    if sys.platform == "win32":
        safe = marker.replace("'", "''")
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*"
            + safe
            + "*' } | Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script], capture_output=True,
                text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
            )
            return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
        except Exception:
            return []
    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=20)
        pids = []
        for line in result.stdout.splitlines():
            if marker in line and ("chrome" in line.lower() or "chromium" in line.lower()):
                first = line.strip().split(None, 1)[0]
                if first.isdigit():
                    pids.append(int(first))
        return pids
    except Exception:
        return []


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                    capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    time.sleep(0.6)
    if sys.platform != "win32":
        for pid in pids:
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def _remove_profile(profile: Path) -> bool:
    for attempt in range(6):
        if not profile.exists():
            return True
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return True
        time.sleep(0.4 * (attempt + 1))
    return not profile.exists()


# ---------------------------------------------------------------------------
# REA and evidence helpers
# ---------------------------------------------------------------------------


def _run_rea(node: str, args: list[str], timeout: int = 240) -> dict:
    completed = subprocess.run(
        [node, str(REA_SCRIPT), *args, "--json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"REA {args[0]} failed: {_snippet(completed.stderr or completed.stdout, 200)}")
    try:
        return json.loads(completed.stdout)
    except Exception:
        return {"raw": completed.stdout[:2000]}


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _strings(obj) -> list[str]:
    found: list[str] = []
    for _, value in _walk(obj):
        if isinstance(value, str) and value:
            found.append(value)
    return found


def _rea_targets(data, url: str) -> list[str]:
    ids: list[str] = []
    for key, value in _walk(data):
        if isinstance(value, dict):
            candidate_id = value.get("id") or value.get("targetId")
            candidate_url = value.get("url") or value.get("targetUrl")
            if isinstance(candidate_id, str) and isinstance(candidate_url, str) and _host_matches(candidate_url, url):
                ids.append(candidate_id)
    return list(dict.fromkeys(ids))


def _extract_routes(data) -> list[str]:
    keys = {"routes", "route", "paths", "endpoints", "javascript_routes"}
    routes: list[str] = []
    for key, value in _walk(data):
        if key.lower() in keys:
            if isinstance(value, str) and value:
                routes.append(value)
            elif isinstance(value, list):
                for item in value[:50]:
                    if isinstance(item, str):
                        routes.append(item)
                    elif isinstance(item, dict):
                        for inner in ("path", "route", "url", "endpoint"):
                            if isinstance(item.get(inner), str):
                                routes.append(item[inner])
            elif isinstance(value, dict):
                for inner in ("path", "route", "url", "endpoint"):
                    if isinstance(value.get(inner), str):
                        routes.append(value[inner])
    return list(dict.fromkeys(route for route in routes if route and len(route) < 300))[:25]


def _extract_load_graph(data) -> list[str]:
    """Map REA's script-key graph to readable 'file -> import' lines."""
    names: dict[str, str] = {}
    pairs: list[str] = []
    for key, value in _walk(data):
        if key == "nodes" and isinstance(value, list):
            for node in value:
                if isinstance(node, dict) and isinstance(node.get("script_key"), str):
                    url = node.get("url") or node.get("path") or node.get("key")
                    if isinstance(url, str):
                        names[node["script_key"]] = url
        if key == "edges" and isinstance(value, list):
            for edge in value:
                if not isinstance(edge, dict):
                    continue
                source = names.get(edge.get("from_script_key", ""), str(edge.get("from_script_key", ""))[:12])
                target = edge.get("resolved_url") or edge.get("specifier") or edge.get("to_script_key")
                if isinstance(target, str) and target:
                    pairs.append(f"{_snippet(source, 90)} -> {_snippet(target, 90)}")
    return list(dict.fromkeys(pairs))[:25]


SIGNAL_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:Buffer|Map|Pass|Shader|Uniform|Texture|CoC|DOF|Bloom|SSAO|FXAA|SMAA|ToneMapping|PMREM)\b"
    r"|\b(?:gl_FragColor|texture2D|depthTexture|nearCoCBuffer|envMap|normalMap)\b"
)


def _code_signals(strings: list[str]) -> list[dict]:
    counts: collections.Counter = collections.Counter()
    for text in strings:
        if len(text) > 2_000_000:
            text = text[:2_000_000]
        for match in SIGNAL_RE.findall(text):
            counts[match] += 1
    return [{"signal": name, "count": count} for name, count in counts.most_common(20)]


FINGERPRINT_PATTERNS = [
    ("3D engine", re.compile(r"Three\.js Authors.{0,400}?(?:REVISION\s*=\s*['\"]?(\d{2,4})|\br(\d{2,4})\b)", re.S | re.I)),
    ("Animation", re.compile(r"(?i)gsap\s*3\.(\d+)\.(\d+)(?:\.(\d+))?")),
    ("Smooth scroll", re.compile(r"(?i)(lenis|scrollsmoother|locomotive-?scroll|smooth-?scrollbar)")),
    ("Visual effects", re.compile(r"(?i)postprocessing\s*v?(\d+)\.(\d+)")),
    ("Compression", re.compile(r"(?i)(KTX2Loader|DRACOLoader|MeshoptDecoder|BasisUniversal|\bktx2\b)")),
    ("Build tools", re.compile(r"(?i)(__webpack_require__|\bvite\b|\brollup\b|\besbuild\b)")),
]


def _fingerprints(strings: list[str]) -> list[dict]:
    table: list[dict] = []
    for priority, (category, pattern) in enumerate(FINGERPRINT_PATTERNS, start=1):
        version = ""
        match = None
        for text in strings:
            if len(text) > 2_000_000:
                text = text[:2_000_000]
            match = pattern.search(text)
            if match:
                groups = [group for group in match.groups() if group]
                version = ".".join(groups) if category in ("Animation", "Visual effects", "3D engine") else (groups[0] if groups else "")
                if category == "3D engine" and version and not version.startswith("r"):
                    version = "r" + version
                if category == "Visual effects" and version:
                    version = "v" + version
                break
        table.append(
            {
                "priority": priority,
                "category": category,
                "found": bool(match),
                "version": version,
                "match": _snippet(match.group(0), 140) if match else "",
            }
        )
    return table


def _other_banners(strings: list[str]) -> list[str]:
    banners: list[str] = []
    known = ("three.js authors", "gsap", "postprocessing")
    for text in strings[:5000]:
        head = text[:260]
        if not re.search(r"(?i)(copyright|©|\bMIT\b|\bApache\b|\bGPL\b|\bBSD\b|license)", head):
            continue
        if any(item in head.lower() for item in known):
            continue
        banner = _snippet(head, 160)
        if banner not in banners:
            banners.append(banner)
        if len(banners) >= 10:
            break
    return banners


KIND_EXTENSIONS = [
    ("3D models", (".glb", ".gltf", ".obj", ".fbx", ".usdz", ".blend", ".stl", ".ply", ".splat", ".3ds", ".dae")),
    ("Compressed textures", (".ktx2", ".ktx", ".basis", ".astc", ".dds", ".etc", ".pvr")),
    ("Video", (".mp4", ".webm", ".mov", ".m4v", ".ogv")),
    ("Audio", (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac")),
    ("Fonts", (".woff2", ".woff", ".ttf", ".otf", ".eot")),
    ("WebAssembly", (".wasm",)),
    ("Scripts", (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")),
]


def _classify_asset(name: str, initiator: str) -> str:
    path = name.split("?", 1)[0].split("#", 1)[0].lower()
    for kind, extensions in KIND_EXTENSIONS:
        if path.endswith(extensions):
            return kind
    if initiator == "script":
        return "Scripts"
    return "Other"


def _asset_log(entries) -> dict:
    if not isinstance(entries, list):
        entries = []
    files: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        size = int(entry.get("transferSize") or entry.get("decodedBodySize") or entry.get("encodedBodySize") or 0)
        files.append(
            {
                "name": name,
                "origin": urlparse(name).netloc,
                "kind": _classify_asset(name, str(entry.get("initiatorType") or "")),
                "size": size,
                "transfer_size": int(entry.get("transferSize") or 0),
                "encoded_size": int(entry.get("encodedBodySize") or 0),
                "decoded_size": int(entry.get("decodedBodySize") or 0),
                "initiator": str(entry.get("initiatorType") or ""),
                "duration_ms": round(float(entry.get("duration") or 0), 1),
            }
        )
    groups: dict[str, dict] = {}
    for item in files:
        group = groups.setdefault(item["kind"], {"files": 0, "bytes": 0})
        group["files"] += 1
        group["bytes"] += item["size"]
    top = sorted(files, key=lambda item: item["size"], reverse=True)[:12]
    return {
        "total_files": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "origins": sorted({item["origin"] for item in files if item["origin"]}),
        "groups": groups,
        "top12": top,
        "files": files[:500],
    }


def _decode_snapshot(data, destination: Path) -> bool:
    for text in _strings(data):
        if len(text) < 1000 or len(text) > 60_000_000:
            continue
        candidate = re.sub(r"\s+", "", text)
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", candidate):
            continue
        try:
            raw = base64.b64decode(candidate)
        except Exception:
            continue
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            return True
    return False


def evidence_ids(result: dict) -> list[str]:
    ids: list[str] = []
    for line in result.get("evidence", []):
        if not isinstance(line, dict) or line.get("source") != "REA":
            continue
        data = line.get("data")
        for _, value in _walk(data):
            if isinstance(value, str) and value.startswith("ev_") and value not in ids:
                ids.append(value)
    return ids


def build_digest(result: dict, question: str, host: str) -> str:
    """Plain-text evidence bundle, under ~15,000 characters, with attribution."""
    assets = result.get("asset_log") or {}
    fingerprints = result.get("fingerprints") or []
    stack = [f"{item['category']} {item['version']}".strip() for item in fingerprints if item.get("found")]
    for banner in result.get("other_banners") or []:
        stack.append("other: " + _snippet(banner, 120))

    lines = [
        "TEARDOWN DIGEST - every line is labelled with who captured it.",
        f"[Jarvis] site: {result.get('url')}",
        f"[Jarvis] question: {question}",
        f"[Jarvis] host: {host}",
        "[REA] page: " + _snippet(result.get("title"), 200),
        "[Jarvis] final URL: " + _snippet(result.get("final_url"), 200),
        "[Jarvis] loaded in: " + str(result.get("load_seconds")) + "s + " + str(result.get("intro_wait_seconds")) + "s intro wait",
        "[Jarvis] browser: " + _snippet(result.get("browser_version"), 120),
        "[Jarvis] CDP target: " + str(result.get("target_id")),
        "[Jarvis] scroll: " + str(result.get("scroll")),
        "",
        "SCRIPTS (Jarvis read from performance entries):",
    ]
    scripts = sorted(
        (item for item in (assets.get("files") or []) if item.get("kind") == "Scripts"),
        key=lambda item: item.get("size", 0), reverse=True,
    )
    if scripts:
        for item in scripts:
            lines.append(f"[Jarvis] script: {item['name']} | {item['origin']} | {item['size']:,} bytes")
    else:
        lines.append("[Jarvis] script: none logged")
    lines.append("")
    lines.append("WHICH FILE LOADS WHICH (REA analyze-web-bundle):")
    loads = result.get("load_graph") or []
    lines.extend(f"[REA] load: {item}" for item in loads[:40] or ["[REA] load: none reported"])
    lines.append("")
    lines.append("ROUTES (REA analyze-web-bundle):")
    routes = result.get("routes") or []
    lines.extend(f"[REA] route: {item}" for item in routes[:40] or ["[REA] route: 0 declared"])
    lines.append("")
    lines.append("CODE SIGNALS handed to the graphics card (REA):")
    signals = result.get("code_signals") or []
    if signals:
        lines.extend(f"[REA] signal: {item['signal']} x{item['count']}" for item in signals)
    else:
        lines.append("[REA] signal: none")
    lines.append("")
    lines.append("STACK / LICENCE BANNERS (Jarvis reading REA's captured code, designer priority):")
    for item in fingerprints:
        lines.append(
            f"[Jarvis] stack: {item['category']} | found={item['found']} | "
            f"version={item.get('version') or '-'} | evidence={_snippet(item.get('match') or '-', 160)}"
        )
    lines.append("")
    lines.append("ASSET TOTALS AND LARGEST FILES (Jarvis):")
    lines.append(
        f"[Jarvis] total: {assets.get('total_files', 0)} files, {assets.get('total_bytes', 0):,} bytes"
    )
    for kind, group in sorted((assets.get("groups") or {}).items()):
        lines.append(f"[Jarvis] group: {kind} | {group['files']} files | {group['bytes']:,} bytes")
    for item in (assets.get("top12") or []):
        lines.append(
            f"[Jarvis] largest: {item['name']} | {item['kind']} | {item['origin']} | {item['size']:,} bytes"
        )
    lines.append("")
    lines.append("ORIGINS (Jarvis):")
    lines.extend(f"[Jarvis] origin: {item}" for item in (assets.get("origins") or []) or ["[Jarvis] origin: none"])
    lines.append("")
    lines.append("SNAPSHOT (REA capture, Jarvis decode):")
    lines.append("[REA] screenshot: " + str(result.get("screenshot_path") or "not saved"))
    lines.append("")
    lines.append("REA EVIDENCE IDS:")
    lines.extend(f"[REA] evidence_id: {item}" for item in evidence_ids(result) or ["[REA] evidence_id: none"])
    lines.append("")
    lines.append("LIMITS:")
    lines.append("[Jarvis] REA attaches to a page that is already running in our own browser; it does not launch or drive the site.")
    lines.append("[Jarvis] Library names come from licence banners and strings in captured code, not from a package manifest; absent banners do not prove absence.")
    lines.append("[Jarvis] REA reports only its own origin from the moment it attached; the asset log is the browser's whole resource timeline.")
    lines.append("[Jarvis] Routes are static candidates in the captured code; reachability is unverified.")
    lines.append("[Jarvis] Scroll effects and lazy loads are whatever fired during the 16 wheel events in the observation window.")
    digest = "\n".join(lines)
    return digest[:15000]


# ---------------------------------------------------------------------------
# the teardown
# ---------------------------------------------------------------------------


def run_teardown(url, command=None, headless: bool = False, progress=None) -> dict:
    """Capture five labelled evidence streams attached to our own fresh Chrome."""
    def step(number: int, label: str) -> None:
        if progress:
            progress(number, label)
    diag = diagnose()
    if not diag["ready"]:
        return {"ok": False, "reply": missing_line(diag), "diagnostics": diag}
    allowed, reason = validate_website(url)
    if not allowed:
        return {
            "ok": False,
            "reply": f"I'm afraid that is not a website I may look at, sir. {reason} Nothing was touched.",
            "diagnostics": diag,
        }
    chosen = str(command or DEFAULT_COMMAND).strip()
    if chosen not in ATTACH_COMMANDS:
        return {
            "ok": False,
            "reply": "I only use REA's read-only website observation, sir. Nothing was touched.",
            "diagnostics": diag,
        }
    if not _port_free(CDP_PORT):
        return {"ok": False, "reply": f"Loopback port {CDP_PORT} is busy, sir. Nothing was touched.", "diagnostics": diag}

    job_id = secrets.token_hex(8)
    profile = PROFILES_HOME / job_id
    snapshot_path = SNAPSHOTS_HOME / (job_id + ".png")
    try:
        profile.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        return {"ok": False, "reply": f"I could not make a clean profile, sir: {_snippet(exc)}", "diagnostics": diag}

    chrome = diag["chrome"]["path"]
    node = diag["node"]["path"]
    endpoint = f"http://{CDP_HOST}:{CDP_PORT}"
    args = [
        chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={CDP_PORT}",
        f"--remote-debugging-address={CDP_HOST}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", f"--window-size={WINDOW_SIZE}",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url.strip())

    proc = None
    client: CDPClient | None = None
    started = time.monotonic()
    result: dict = {
        "ok": False, "diagnostics": diag, "job_id": job_id, "headless": bool(headless),
        "attribution_note": ATTRIBUTION_NOTE, "evidence": [],
    }

    def evidence(source: str, step: str, tool: str, payload, summary: str = "") -> None:
        result["evidence"].append(
            {"source": source, "step": step, "tool": tool, "summary": summary, "data": payload}
        )

    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW
        )
        step(1, "Opening the site in a clean browser")
        deadline = time.monotonic() + CDP_WAIT_SECONDS
        version = None
        while time.monotonic() < deadline:
            version = _http_json(endpoint + "/json/version", timeout=1.0)
            if isinstance(version, dict):
                break
            time.sleep(0.25)
        if not isinstance(version, dict):
            raise RuntimeError("Chrome's DevTools endpoint did not answer within 25 seconds")

        cdp_targets = None
        while time.monotonic() < deadline:
            cdp_targets = _http_json(endpoint + "/json/list", timeout=1.0)
            if isinstance(cdp_targets, list) and any(
                isinstance(item, dict) and item.get("type") == "page"
                and str(item.get("url", "")).startswith(("http://", "https://"))
                and _host_matches(str(item.get("url", "")), url) for item in cdp_targets
            ):
                break
            time.sleep(0.25)
        cdp_target = next(
            (
                item for item in (cdp_targets or [])
                if isinstance(item, dict) and item.get("type") == "page"
                and str(item.get("url", "")).startswith(("http://", "https://"))
                and _host_matches(str(item.get("url", "")), url)
            ),
            None,
        )
        if not cdp_target:
            raise RuntimeError("No Chrome page target for that website was found")
        client = CDPClient(str(cdp_target["webSocketDebuggerUrl"]), timeout=CDP_WAIT_SECONDS).connect()

        ready_at = None
        while time.monotonic() < deadline:
            if client.evaluate("document.readyState") == "complete":
                ready_at = time.monotonic()
                break
            time.sleep(0.2)
        if ready_at is None:
            raise RuntimeError("The page did not finish loading within 25 seconds")
        load_seconds = round(ready_at - started, 2)
        time.sleep(INTRO_WAIT_SECONDS)
        step(2, "Waiting for it to load")
        title = str(client.evaluate("document.title") or "")
        href = str(client.evaluate("location.href") or "")

        # Target id must come from REA's own target listing.
        listing = _run_rea(node, ["list-browser-targets", endpoint], timeout=60)
        rea_ids = _rea_targets(listing, url)
        target_id = rea_ids[0] if rea_ids else str(cdp_target["id"])
        evidence("REA", "target", "list-browser-targets", {"target_ids": rea_ids, "used": target_id},
                 f"target {target_id[:12]}…")

        # 1. WATCH WHILE SCROLLING
        watch_holder: dict = {}

        def watch() -> None:
            try:
                watch_holder["data"] = _run_rea(
                    node,
                    [
                        "inspect-web-page", endpoint, target_id,
                        "--observation-ms", "7000", "--include-script-sources",
                    ],
                    timeout=120,
                )
            except Exception as exc:
                watch_holder["error"] = str(exc)

        watcher = threading.Thread(target=watch, name="rea-watch", daemon=True)
        step(3, "REA is watching while I scroll")
        watcher.start()
        time.sleep(1.0)
        before_scroll = client.evaluate("window.scrollY || 0")
        for _ in range(WHEEL_STEPS):
            client.wheel(600)
            time.sleep(WHEEL_STEP_SECONDS)
        after_scroll = client.evaluate("window.scrollY || 0")
        watcher.join(timeout=120)
        if "error" in watch_holder:
            raise RuntimeError(f"inspect-web-page failed: {_snippet(watch_holder['error'])}")
        watch_data = watch_holder.get("data", {})
        evidence("REA", "watch", "inspect-web-page", watch_data,
                 f"observed {WHEEL_STEPS} wheel steps for 7000ms with script sources")
        evidence("Jarvis", "scroll", "Input.dispatchMouseEvent",
                 {"before": before_scroll, "after": after_scroll, "steps": WHEEL_STEPS},
                 f"wheel scroll {before_scroll} -> {after_scroll}")

        # 2. READ THE CODE
        step(4, "REA is reading the code")
        bundle = _run_rea(node, ["analyze-web-bundle", endpoint, target_id], timeout=240)
        strings = _strings(watch_data) + _strings(bundle)
        routes = _extract_routes(bundle)
        load_graph = _extract_load_graph(bundle)
        code_signals = _code_signals(strings)
        evidence("REA", "code", "analyze-web-bundle", bundle,
                 f"{len(routes)} routes, {len(load_graph)} load links, {len(code_signals)} code signals")
        evidence("REA", "routes", "analyze-web-bundle", routes, f"{len(routes)} routes")
        evidence("REA", "load-graph", "analyze-web-bundle", load_graph, f"{len(load_graph)} file links")
        evidence("REA", "code-signals", "analyze-web-bundle", code_signals,
                 "shader/uniform strings handed to the graphics card")

        # 3. SNAPSHOT: wheel back to the top, then capture.
        for _ in range(WHEEL_STEPS + 8):
            client.wheel(-900)
            time.sleep(0.05)
        top = client.evaluate("window.scrollY || 0")
        step(5, "Taking a snapshot")
        screenshot = _run_rea(node, ["capture-web-screenshot", endpoint, target_id], timeout=120)
        png_saved = _decode_snapshot(screenshot, snapshot_path)
        evidence("Jarvis", "scroll-top", "Input.dispatchMouseEvent", {"scrollY": top}, "wheeled back to top")
        evidence("REA", "snapshot", "capture-web-screenshot",
                 {"saved": png_saved, "path": str(snapshot_path), "bytes": snapshot_path.stat().st_size if png_saved else 0},
                 f"PNG saved: {png_saved}")

        # 4. ASSET LOG - Jarvis's own browser reads.
        step(6, "Listing everything it loaded")
        entries = client.evaluate(
            "performance.getEntriesByType('resource').map(e => ({name:e.name,transferSize:e.transferSize,"
            "encodedBodySize:e.encodedBodySize,decodedBodySize:e.decodedBodySize,"
            "initiatorType:e.initiatorType,duration:e.duration}))"
        )
        assets = _asset_log(entries)
        evidence("Jarvis", "asset-log", "performance.getEntriesByType",
                 assets, f"{assets['total_files']} files, {assets['total_bytes']} bytes, {len(assets['top12'])} largest")

        # 5. FINGERPRINTS - Jarvis reads REA's captured code.
        step(7, "Identifying the stack")
        fingerprints = _fingerprints(strings)
        other_banners = _other_banners(strings)
        evidence("Jarvis", "fingerprints", "REA code strings",
                 {"table": fingerprints, "other_banners": other_banners},
                 "license banners in designer priority order")

        script_files = sorted(
            (item for item in assets["files"] if item["kind"] == "Scripts"),
            key=lambda item: item["size"],
            reverse=True,
        )[:6]
        stack = [
            f"{item['category']} {item['version']}".strip()
            for item in fingerprints if item["found"]
        ]
        if other_banners:
            stack.append("other: " + "; ".join(_snippet(item, 60) for item in other_banners[:2]))
        digest_parts = [
            "[Jarvis] scripts: "
            + ("; ".join(f"{item['name'].split('/')[-1][:32]} {item['size']:,}B" for item in script_files)
               if script_files else "none logged"),
            "[REA] routes: "
            + (", ".join(routes[:8]) if routes else "0 declared"),
            "[REA] loads: "
            + ("; ".join(load_graph[:4]) if load_graph else "none reported"),
            "[REA] code signals: "
            + (", ".join(f"{item['signal']} ({item['count']})" for item in code_signals[:5])
               if code_signals else "none"),
            "[REA] stack: " + (", ".join(stack) if stack else "no known banners"),
            f"[Jarvis] assets: {assets['total_files']} files, {assets['total_bytes']:,} bytes "
            f"from {len(assets['origins'])} origin(s); screenshot {str(snapshot_path) if png_saved else 'not saved'}",
        ]
        result.update(
            {
                "ok": True,
                "reply": f"Observed {url.strip()} read-only with REA {diag['installed_version']}, sir.",
                "url": url.strip(),
                "final_url": href,
                "title": title,
                "load_seconds": load_seconds,
                "intro_wait_seconds": INTRO_WAIT_SECONDS,
                "scroll": {"before": before_scroll, "after": after_scroll, "top": top, "steps": WHEEL_STEPS},
                "command": chosen,
                "cdp_endpoint": endpoint,
                "target_id": target_id,
                "browser_version": str(version.get("Browser", "")),
                "data": bundle,
                "routes": routes,
                "load_graph": load_graph,
                "code_signals": code_signals,
                "asset_log": assets,
                "fingerprints": fingerprints,
                "other_banners": other_banners,
                "screenshot_path": str(snapshot_path) if png_saved else "",
                "screenshot_saved": png_saved,
                "digest": "\n".join(digest_parts),
                "digest_line": ATTRIBUTION_NOTE + " " + " ".join(digest_parts),
            }
        )
    except Exception as exc:
        result["reply"] = f"The teardown could not be completed, sir: {_snippet(exc, 200)}"
    finally:
        if client is not None:
            client.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        _kill_pids(_profile_pids(job_id))
        result["profile_cleaned"] = _remove_profile(profile)
        remaining = _profile_pids(job_id)
        result["remaining_processes"] = remaining
        result["chrome_closed"] = not remaining
        if remaining:
            result["ok"] = False
            result["reply"] = f"A browser with that profile is still running, sir ({remaining})."
    return result


def main() -> int:
    parser = __import__("argparse").ArgumentParser(description="Own the browser and capture labelled REA + Jarvis evidence.")
    parser.add_argument("--url", required=True, help="public http(s) website, preferably a 3D site")
    parser.add_argument("--headless", action="store_true", help="run Chrome with no window")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="attach-only REA command")
    args = parser.parse_args()
    result = run_teardown(args.url, command=args.command, headless=args.headless)

    print(f"Job: {result.get('job_id')}")
    print(f"Headless: {result.get('headless')}")
    print(f"Title: {result.get('title') or '(none)'}")
    print(f"Loaded in: {result.get('load_seconds')}s (+{result.get('intro_wait_seconds')}s intro wait)")
    print(f"Scroll: {result.get('scroll')}")
    print(f"Screenshot: {result.get('screenshot_path') or 'not saved'}")
    print(f"Chrome closed: {result.get('chrome_closed')}")
    print(f"Remaining processes: {result.get('remaining_processes')}")
    print(f"Profile removed: {result.get('profile_cleaned')}")
    print("--- evidence digest ---")
    print(result.get("digest") or result.get("reply"))
    print("--- labelled evidence ---")
    for item in result.get("evidence", []):
        print(f"[{item['source']}] {item['step']}: {item['summary']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
