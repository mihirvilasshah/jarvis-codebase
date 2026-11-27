#!/usr/bin/env python3
"""Index every Markdown note into viewer/graph-data.js and viewer/graph.json.

Standard library only. Node ids are always the node's index in the nodes array.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "notes"
VIEWER_DIR = ROOT / "viewer"
JS_PATH = VIEWER_DIR / "graph-data.js"
JSON_PATH = VIEWER_DIR / "graph.json"

FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
BULLET_RE = re.compile(r"^\s*[-+*]\s+", re.M)
NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+", re.M)
BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.M)


SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to"}


def title_case(value: str) -> str:
    words = []
    for index, word in enumerate(value.split()):
        if index > 0 and word.lower() in SMALL_WORDS:
            words.append(word.lower())
        elif word.isupper() and len(word) <= 5:
            words.append(word)
        elif word[:1].isdigit():
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def label_from_path(path: Path) -> str:
    stem = re.sub(r"^\d+\s*[-_. ]+\s*", "", path.stem)
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return title_case(stem or path.stem)


def group_from_path(path: Path) -> str:
    if path.parent == NOTES_DIR:
        return "General"
    parent = re.sub(r"^\d+\s*[-_. ]+\s*", "", path.parent.name)
    parent = re.sub(r"[-_]+", " ", parent)
    return title_case(re.sub(r"\s+", " ", parent).strip() or "General")


def plain_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = FRONTMATTER_RE.sub("", text)
    text = CODE_FENCE_RE.sub(" ", text)
    text = IMAGE_RE.sub(" ", text)
    text = WIKILINK_RE.sub(lambda m: m.group(1).strip(), text)
    text = MD_LINK_RE.sub(lambda m: m.group(1).strip(), text)
    text = INLINE_CODE_RE.sub(lambda m: m.group(1).strip(), text)
    text = HEADING_RE.sub("", text)
    text = BULLET_RE.sub("", text)
    text = NUMBERED_RE.sub("", text)
    text = BLOCKQUOTE_RE.sub("", text)
    text = BOLD_RE.sub(r"\2", text)
    text = ITALIC_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_excerpt(text: str, limit: int = 700) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit - 120:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def normalize(value: str) -> str:
    value = value.lower().replace("’", "'")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def mentions_title(text_lower: str, title: str) -> bool:
    if len(title) < 4:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(title.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, text_lower) is not None


def main() -> int:
    if not NOTES_DIR.exists():
        raise SystemExit(f"Notes folder not found: {NOTES_DIR}")

    files = sorted(NOTES_DIR.rglob("*.md"), key=lambda p: p.relative_to(NOTES_DIR).as_posix())
    if not files:
        raise SystemExit(f"No .md files found in {NOTES_DIR}")

    records = []
    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        records.append(
            {
                "path": path,
                "label": label_from_path(path),
                "group": group_from_path(path),
                "raw": raw,
                "text": plain_text(raw),
            }
        )

    aliases: dict[str, int] = {}
    for index, record in enumerate(records):
        path = record["path"]
        stem = re.sub(r"^\d+\s*[-_. ]+\s*", "", path.stem)
        for alias in (record["label"], path.stem, stem):
            aliases.setdefault(normalize(alias), index)

    links: set[tuple[int, int]] = set()
    for index, record in enumerate(records):
        raw = record["raw"]
        raw_lower = raw.lower()
        for target in WIKILINK_RE.findall(raw):
            target_index = aliases.get(normalize(target))
            if target_index is not None and target_index != index:
                links.add((min(index, target_index), max(index, target_index)))
        for other_index, other in enumerate(records):
            if other_index == index:
                continue
            if mentions_title(raw_lower, other["label"]):
                links.add((min(index, other_index), max(index, other_index)))

    nodes = []
    for index, record in enumerate(records):
        nodes.append(
            {
                "id": index,
                "label": record["label"],
                "group": record["group"],
                "excerpt": make_excerpt(record["text"]),
            }
        )

    graph = {
        "nodes": nodes,
        "links": [{"source": source, "target": target} for source, target in sorted(links)],
    }

    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(graph, ensure_ascii=False, indent=2)
    JS_PATH.write_text(
        "// Generated by build.py. Do not edit by hand.\n"
        f"const GRAPH = {payload};\n"
        "window.GRAPH = GRAPH;\n",
        encoding="utf-8",
    )
    JSON_PATH.write_text(payload + "\n", encoding="utf-8")

    groups = sorted({node["group"] for node in nodes})
    print(f"Indexed {len(nodes)} notes across {len(groups)} groups with {len(graph['links'])} links.")
    print(f"Wrote {JS_PATH.relative_to(ROOT)} and {JSON_PATH.relative_to(ROOT)}")
    print("Groups: " + ", ".join(groups))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
