"""THE BAY - read-only REA website observation kit for Jarvis.

Standard library only. Soft-imported by server.py, so Jarvis boots without it.
REA is installed outside the project in ~/.jarvis-tools/rea and is always
called as: node HOME/node_modules/rea-agents/scripts/rea.mjs COMMAND --json

THE LAW: REA observes. It never clicks, types, logs in, or changes anything.
Only public http(s) websites may be passed to it. No local files, no private
addresses, no credentials in URLs, no interactive browser actions.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

PACKAGE = "rea-agents"
EXPECTED_VERSION = "6.10.1"
REPO_HINT = "github.com/morluto/rea"
TOOLS_HOME = Path.home() / ".jarvis-tools" / "rea"
PACKAGE_JSON = TOOLS_HOME / "node_modules" / PACKAGE / "package.json"
PIN_FILE = TOOLS_HOME / "package.json"
REA_SCRIPT = TOOLS_HOME / "node_modules" / PACKAGE / "scripts" / "rea.mjs"

# Only passive website observation is allowed. Never add scenario commands
# here: capture-browser-scenario is driven with a fixed goto/wait scenario.
OBSERVE_COMMANDS = {"capture-browser-scenario"}
DEFAULT_COMMAND = "capture-browser-scenario"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NODE_WINDOWS = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]
NODE_UNIX = [
    "/usr/local/bin/node",
    "/opt/homebrew/bin/node",
    "/usr/bin/node",
]

CHROME_WINDOWS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
CHROME_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def _snippet(value, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def node_version_ok(version: str) -> tuple[bool, str]:
    """Pin the floor published by REA 6.10.1's package engines."""
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
    """Find Node on PATH or in common install folders; ignore the caller's PATH."""
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
    first_version: str | None = None
    first_path: str | None = None
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
    """Find Google Chrome on Windows, macOS, or Linux."""
    candidates = list(CHROME_WINDOWS) + list(CHROME_MAC) + list(CHROME_LINUX)
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(
            str(Path(localappdata) / "Google" / "Chrome" / "Application" / "chrome.exe")
        )
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
    """Return the /teardown/diag payload. Never raises."""
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
        problems.append(
            f"REA is not installed. Install {PACKAGE} {EXPECTED_VERSION} into {TOOLS_HOME}."
        )
    elif installed_version != EXPECTED_VERSION:
        problems.append(
            f"REA {installed_version} is installed; pin {EXPECTED_VERSION} instead."
        )
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
        "node": {
            "path": node_path,
            "version": node_version,
            "ok": node_ok,
            "need": node_need,
        },
        "chrome": {"path": chrome, "found": bool(chrome)},
        "rea_script": str(REA_SCRIPT),
        "tools_home": str(TOOLS_HOME),
        "problems": problems,
        "law": "REA observes only: no clicks, typing, logins, or changes; websites only.",
    }


def missing_line(diag: dict) -> str:
    """One line, in character, saying what to install. Nothing else."""
    problems = [str(item) for item in (diag.get("problems") or [])]
    if not problems:
        return "The teardown bay is ready, sir."
    joined = " ".join(problems)
    fixes: list[str] = []
    if any(
        word in joined
        for word in ("REA", "REA launcher", "pinned", "official repo", "not installed")
    ):
        fixes.append(
            f"install REA {EXPECTED_VERSION} into {TOOLS_HOME} with "
            f"`npm install rea-agents@{EXPECTED_VERSION} --save-exact --ignore-scripts`"
        )
    if "Node.js" in joined:
        need = (diag.get("node") or {}).get("need") or "22.23.4+"
        fixes.append(f"install Node.js {need}")
    if "Chrome" in joined:
        fixes.append("install Google Chrome")
    if not fixes:
        fixes.append(problems[0])
    return (
        "I'm afraid the teardown bay is not ready, sir: "
        + " and ".join(fixes)
        + ". Nothing was touched."
    )


def validate_website(url) -> tuple[bool, str]:
    """Accept only public http(s) websites; no credentials, local, or private targets."""
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
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False, "Private, local, or reserved addresses are not allowed."
    except ValueError:
        if host.endswith((".local", ".internal", ".test", ".invalid")):
            return False, "Local or internal names are not allowed."
    return True, ""


def _scenario_for(url: str, chrome: str) -> dict:
    """A fixed observe-only scenario: launch Chrome, go to the page, wait, stop."""
    parsed = urlparse(url.strip())
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    query = [
        {"name": name, "value": {"source": "literal", "value": value}}
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    start = {"url": base}
    destination = {"url": base}
    if query:
        start["query"] = query
        destination["query"] = query
    return {
        "browser": {"mode": "launch", "executable_path": chrome, "headless": True},
        "start_url": start,
        "environment": {
            "viewport": {"width": 1280, "height": 720, "device_scale_factor": 1},
            "locale": "en-US",
            "timezone": "UTC",
            "color_scheme": "light",
            "reduced_motion": "reduce",
            "service_workers": "block",
        },
        "actions": [
            {
                "step_id": "observe",
                "action": "goto",
                "destination": destination,
                "wait_until": "load",
            },
            {"step_id": "settle", "action": "wait_for_timeout", "duration_ms": 800},
        ],
    }


def run_teardown(url, command=None) -> dict:
    """Observe one public website with REA. No mocks, no interaction, no writes to the site."""
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
    if chosen not in OBSERVE_COMMANDS:
        return {
            "ok": False,
            "reply": "I only use REA's read-only website observation, sir. Nothing was touched.",
            "diagnostics": diag,
        }

    node = diag["node"]["path"]
    chrome = diag["chrome"]["path"]
    scenario = _scenario_for(url, chrome)
    argv = [
        node,
        str(REA_SCRIPT),
        chosen,
        json.dumps(scenario, ensure_ascii=False, separators=(",", ":")),
        "--json",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reply": "REA took too long observing that site, sir. Nothing was changed.",
            "diagnostics": diag,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reply": f"The teardown bay could not start, sir: {_snippet(exc)}",
            "diagnostics": diag,
        }

    if completed.returncode != 0:
        detail = _snippet(completed.stderr or completed.stdout, 180)
        return {
            "ok": False,
            "reply": f"REA could not observe that site, sir. {detail}",
            "diagnostics": diag,
        }
    try:
        data = json.loads(completed.stdout)
    except Exception:
        data = {"raw": completed.stdout[:2000]}

    return {
        "ok": True,
        "reply": f"Observed {url.strip()} read-only with REA {diag['installed_version']}, sir.",
        "url": url.strip(),
        "command": chosen,
        "data": data,
        "diagnostics": diag,
    }
