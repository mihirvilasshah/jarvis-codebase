"""THE BAY - Jarvis-owned Chrome + read-only REA website observation.

Standard library only. Soft-imported by server.py, so Jarvis boots without it.

REA never opens a browser itself here: for every teardown we create a brand-new
profile, launch our own Chrome with loopback CDP, attach REA to that browser,
observe the page, then terminate Chrome, kill anything still using the profile,
and delete the folder. Nothing touches the user's everyday Chrome profile.

THE LAW: REA observes. It never clicks, types, logs in, or changes anything.
Only public http(s) websites may be passed to it. Our DevTools client has two
jobs: Runtime.evaluate for read-only facts, and mouseWheel scrolling.
"""

from __future__ import annotations

import base64
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
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PACKAGE = "rea-agents"
EXPECTED_VERSION = "6.10.1"
REPO_HINT = "github.com/morluto/rea"
TOOLS_HOME = Path.home() / ".jarvis-tools" / "rea"
PROFILES_HOME = TOOLS_HOME / "profiles"
PACKAGE_JSON = TOOLS_HOME / "node_modules" / PACKAGE / "package.json"
PIN_FILE = TOOLS_HOME / "package.json"
REA_SCRIPT = TOOLS_HOME / "node_modules" / PACKAGE / "scripts" / "rea.mjs"

CDP_HOST = "127.0.0.1"
CDP_PORT = 9333
WINDOW_SIZE = "1280,820"
INTRO_WAIT_SECONDS = 2.5
CDP_WAIT_SECONDS = 25.0

# Commands that attach to an existing loopback CDP browser (never launch one).
ATTACH_COMMANDS = {"analyze-web-bundle", "inspect-web-page", "capture-web-screenshot", "discover-webmcp-tools"}
DEFAULT_COMMAND = "analyze-web-bundle"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NODE_WINDOWS = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]
NODE_UNIX = ["/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"]
CHROME_WINDOWS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_MAC = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
CHROME_LINUX = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]


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
                [str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
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
    candidates.append(
        str(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    )
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
            if isinstance(installed, dict) and isinstance(installed.get("repository"), dict)
            else None
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
    """Just enough WebSocket for Runtime.evaluate and mouseWheel input."""

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
            self.send_frame(0xA, payload)  # pong
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
            # other non-control opcodes are ignored

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
# browser lifecycle helpers
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
    """Find processes whose command line contains the unique profile marker."""
    if sys.platform == "win32":
        safe = marker.replace("'", "''")
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*"
            + safe
            + "*' } | Select-Object -ExpandProperty ProcessId"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
        except Exception:
            return []
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=20
        )
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
                    capture_output=True,
                    timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    time.sleep(0.6)
    for pid in pids:
        try:
            if sys.platform != "win32":
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
# the teardown
# ---------------------------------------------------------------------------


def run_teardown(url, command=None, headless: bool = False) -> dict:
    """Observe one public website with REA attached to our own fresh Chrome."""
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
        return {
            "ok": False,
            "reply": f"Loopback port {CDP_PORT} is busy, sir. Nothing was touched.",
            "diagnostics": diag,
        }

    job_id = secrets.token_hex(8)
    profile = PROFILES_HOME / job_id
    try:
        profile.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        return {"ok": False, "reply": f"I could not make a clean profile, sir: {_snippet(exc)}", "diagnostics": diag}

    chrome = diag["chrome"]["path"]
    node = diag["node"]["path"]
    args = [
        chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={CDP_PORT}",
        f"--remote-debugging-address={CDP_HOST}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--window-size={WINDOW_SIZE}",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url.strip())

    proc = None
    client: CDPClient | None = None
    started = time.monotonic()
    result: dict = {"ok": False, "diagnostics": diag, "job_id": job_id, "headless": bool(headless)}

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

        deadline = time.monotonic() + CDP_WAIT_SECONDS
        version = None
        while time.monotonic() < deadline:
            version = _http_json(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=1.0)
            if isinstance(version, dict):
                break
            time.sleep(0.25)
        if not isinstance(version, dict):
            raise RuntimeError("Chrome's DevTools endpoint did not answer within 25 seconds")

        target = None
        while time.monotonic() < deadline:
            targets = _http_json(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=1.0)
            if isinstance(targets, list):
                target = next(
                    (
                        item
                        for item in targets
                        if isinstance(item, dict)
                        and item.get("type") == "page"
                        and str(item.get("url", "")).startswith(("http://", "https://"))
                        and _host_matches(str(item.get("url", "")), url)
                    ),
                    None,
                )
                if target:
                    break
            time.sleep(0.25)
        if not target:
            raise RuntimeError("No Chrome page target for that website was found")

        client = CDPClient(str(target["webSocketDebuggerUrl"]), timeout=CDP_WAIT_SECONDS).connect()

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

        title = str(client.evaluate("document.title") or "")
        href = str(client.evaluate("location.href") or "")

        before_scroll = client.evaluate("window.scrollY || 0")
        for step in range(3):
            client.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": 640,
                    "y": 410,
                    "deltaX": 0,
                    "deltaY": 600,
                    "pointerType": "mouse",
                },
            )
            time.sleep(0.25)
        after_scroll = client.evaluate("window.scrollY || 0")
        client.close()
        client = None

        rea_argv = [
            node,
            str(REA_SCRIPT),
            chosen,
            f"http://{CDP_HOST}:{CDP_PORT}",
            str(target["id"]),
            "--json",
        ]
        completed = subprocess.run(
            rea_argv,
            capture_output=True,
            text=True,
            timeout=240,
            creationflags=CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"REA failed: {_snippet(completed.stderr or completed.stdout, 200)}")
        try:
            evidence = json.loads(completed.stdout)
        except Exception:
            evidence = {"raw": completed.stdout[:2000]}

        result.update(
            {
                "ok": True,
                "reply": f"Observed {url.strip()} read-only with REA {diag['installed_version']}, sir.",
                "url": url.strip(),
                "final_url": href,
                "title": title,
                "load_seconds": load_seconds,
                "intro_wait_seconds": INTRO_WAIT_SECONDS,
                "scroll": {"before": before_scroll, "after": after_scroll},
                "command": chosen,
                "cdp_endpoint": f"http://{CDP_HOST}:{CDP_PORT}",
                "target_id": str(target["id"]),
                "browser_version": str(version.get("Browser", "")),
                "data": evidence,
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
    parser = __import__("argparse").ArgumentParser(description="Own the browser and observe a public website with REA.")
    parser.add_argument("--url", required=True, help="public http(s) website")
    parser.add_argument("--headless", action="store_true", help="run Chrome with no window")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="attach-only REA command")
    args = parser.parse_args()

    result = run_teardown(args.url, command=args.command, headless=args.headless)
    print(f"Job: {result.get('job_id')}")
    print(f"Headless: {result.get('headless')}")
    print(f"Title: {result.get('title') or '(none)'}")
    print(f"Loaded in: {result.get('load_seconds')}s (+{result.get('intro_wait_seconds')}s intro wait)")
    print(f"Scroll: {result.get('scroll')}")
    print(f"Chrome closed: {result.get('chrome_closed')}")
    print(f"Remaining processes: {result.get('remaining_processes')}")
    print(f"Profile removed: {result.get('profile_cleaned')}")
    print(f"REA: {result.get('reply')}")
    if result.get("data", {}).get("evidence_id"):
        print(f"Evidence: {result['data']['evidence_id']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
