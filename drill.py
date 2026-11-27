#!/usr/bin/env python3
"""No-trace drill for the teardown kit.

Runs offline proofs, then one headless teardown, then restores every file it
touched so the first on-camera teardown really is the first.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import teardown as kg  # noqa: E402

BASE = "http://127.0.0.1:4700"
VIEWER = ROOT / "viewer"
EPISODE_LOG = ROOT / "server.log"
TEARDOWNS = ROOT / "notes" / "captures" / "teardowns"
STEPS = [
    "Opening the site in a clean browser",
    "Waiting for it to load",
    "REA is watching while I scroll",
    "REA is reading the code",
    "Taking a snapshot",
    "Listing everything it loaded",
    "Identifying the stack",
    "Writing the teardown",
]

counts = {"pass": 0, "fail": 0, "warn": 0}


def report(ok: bool, label: str, detail: str = "") -> None:
    status = "pass" if ok else "fail"
    counts[status] += 1
    mark = "✓" if ok else "✗"
    print(f"[{mark}] {label}")
    if detail:
        for line in str(detail).splitlines():
            print("    " + line[:140])


def http(path: str, *, method: str = "GET", payload=None, timeout: int = 420):
    body = None
    headers = {"Accept": "application/json", "User-Agent": "JarvisDrill/1.0"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def offline_addresses() -> list[str | None]:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    match = re.search(r"function speechToAddress\(text\) \{[\s\S]*?\n    \}", html)
    if not match:
        raise RuntimeError("speechToAddress was not found in the served viewer")
    cases = ["igloo dot inc", "stripe.com slash pricing", "localhost:5001", "make it pop", "wikipedia.org"]
    script = (
        match.group(0)
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\nconsole.log(JSON.stringify(cases.map(c => speechToAddress(c))));\n"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200])
    return json.loads(result.stdout.strip().splitlines()[-1])


def offline() -> None:
    expected = ["https://igloo.inc", "https://stripe.com/pricing", "http://localhost:5001", None, "https://wikipedia.org"]
    try:
        actual = offline_addresses()
        report(actual == expected, "five spoken addresses parse", f"got {actual}")
    except Exception as exc:
        report(False, "five spoken addresses parse", str(exc))

    sample = (
        "/** @license Copyright 2020-2025 Three.js Authors SPDX-License-Identifier: MIT */\n"
        "const REVISION = '165';\n\n/*! GSAP 3.12.5 */\n"
    )
    table = kg._fingerprints([sample])
    versions = {item["category"]: item["version"] for item in table}
    report(
        versions.get("3D engine") == "r165" and versions.get("Animation") == "3.12.5",
        "synthetic code fingerprints as Three.js r165 + GSAP 3.12.5",
        json.dumps(versions, ensure_ascii=False),
    )

    synthetic = {
        "url": "https://example.com", "final_url": "https://example.com", "title": "Example",
        "load_seconds": 1.0, "intro_wait_seconds": 2.5, "browser_version": "Chrome",
        "target_id": "x", "scroll": {}, "screenshot_path": "",
        "asset_log": {"files": [], "groups": {}, "top12": [], "total_files": 0, "total_bytes": 0, "origins": []},
        "load_graph": [], "routes": [], "code_signals": [], "fingerprints": [], "other_banners": [],
        "evidence": [
            {"source": "REA", "data": {"evidence_id": "ev_test"}},
            {"source": "Jarvis", "data": {"total_files": 0}},
        ],
    }
    digest = kg.build_digest(synthetic, "What is this?", "example.com")
    unlabelled = [line for line in digest.splitlines() if line.strip() and not line.startswith(("[REA]", "[Jarvis]"))]
    report(not unlabelled, "every digest line names its source", "; ".join(unlabelled[:5]))


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def restore_tree(root: Path, saved: dict[str, bytes]) -> None:
    if root.exists():
        shutil.rmtree(root)
    if not saved:
        return
    root.mkdir(parents=True, exist_ok=True)
    for relative, data in saved.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def drill() -> None:
    graph_json = (VIEWER / "graph.json").read_bytes()
    graph_js = (VIEWER / "graph-data.js").read_bytes()
    episode = EPISODE_LOG.read_bytes() if EPISODE_LOG.exists() else None
    teardowns = snapshot_tree(TEARDOWNS)
    before_names = set(teardowns.keys())
    before_nodes = len(json.loads(graph_json.decode("utf-8"))["nodes"])

    result = None
    seen_steps: list[str] = []
    try:
        status, _, body = http(
            "/teardown/compose", method="POST",
            payload={"url": "https://example.com", "ask": "What is this site?", "headless": True},
        )
        result = json.loads(body.decode("utf-8"))
        report(status == 200 and result.get("ok") is True, "headless teardown completed", result.get("reply", "")[:160])

        deadline = time.time() + 30
        seen_steps: list[str] = []
        while time.time() < deadline:
            status, _, body = http("/teardown/status", timeout=10)
            if status == 200:
                state = json.loads(body.decode("utf-8"))
                seen_steps = state.get("steps") or []
                if not state.get("active") and seen_steps:
                    break
            time.sleep(0.5)
        report(all(step in seen_steps for step in STEPS), "panel steps reported live", "; ".join(seen_steps))

        note = (ROOT / result["note_file"]).read_text(encoding="utf-8")
        sections = ["Verdict", "The answer", "The stack", "How the effect works", "What it loads", "Rebuild it", "What the evidence can't show", "## Evidence"]
        missing = [section for section in sections if section not in note]
        report(not missing, "note sections present", "missing: " + ", ".join(missing))

        spoken = str(result.get("spoken") or "")
        sentence_count = len([part for part in re.split(r"[.!?]+", spoken) if part.strip()])
        clean = 2 <= sentence_count <= 3 and "http" not in spoken.lower() and ".md" not in spoken.lower()
        report(clean, "spoken line is 2-3 sentences with no addresses or paths", spoken[:180])

        born = result.get("notes") or []
        report(bool(born) and all(isinstance(item.get("id"), int) for item in born), "node births returned", str([item.get("label") for item in born]))

        elapsed = result.get("elapsed_seconds") or 0
        report(isinstance(elapsed, int) and 0 < elapsed < 180, "elapsed time recorded", f"{elapsed} s")

        status, _, body = http("/teardown/note?file=../config.json", timeout=10)
        text = body.decode("utf-8", "replace")
        report(status != 200 and "freellmapi_api_key" not in text, "note endpoint refuses outside paths", f"HTTP {status}")
        status, _, body = http("/teardown/image?file=../config.json", timeout=10)
        report(status != 200, "image endpoint refuses outside paths", f"HTTP {status}")
    except Exception as exc:
        report(False, "live drill", str(exc)[:180])
    finally:
        (VIEWER / "graph.json").write_bytes(graph_json)
        (VIEWER / "graph-data.js").write_bytes(graph_js)
        if episode is None:
            EPISODE_LOG.unlink(missing_ok=True)
        else:
            EPISODE_LOG.write_bytes(episode)
        restore_tree(TEARDOWNS, teardowns)
        http("/reindex", method="POST", payload={}, timeout=30)
        after_nodes = len(json.loads((VIEWER / "graph.json").read_text(encoding="utf-8"))["nodes"])
        report(after_nodes == before_nodes, "graph restored", f"{before_nodes} -> {after_nodes}")
        after_names = set(snapshot_tree(TEARDOWNS).keys())
        report(after_names == before_names, "test note removed", f"{sorted(after_names - before_names)}")

    chrome = kg._profile_pids("jarvis-tools\\rea\\profiles")
    report(not chrome, "no investigation Chrome left running", str(chrome))
    profiles = kg.PROFILES_HOME
    leftovers = [str(path) for path in profiles.iterdir()] if profiles.exists() else []
    report(not leftovers, "profile folders gone", "; ".join(leftovers))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print("Jarvis teardown drill — offline + no-trace live run")
    offline()
    drill()
    print()
    print(f"DRILL: {counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
