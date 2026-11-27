"""Local task and reminder storage."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import ipaddress
import re
import sqlite3
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jarvis_actions.sqlite3"


def _default_browser():
    import winreg

    key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            progid, _ = winreg.QueryValueEx(key, "ProgId")
    except FileNotFoundError:
        raise ValueError("Windows has no supported default browser configured. Name Firefox, Chrome, or Edge.") from None
    if isinstance(progid, str):
        for prefix, browser in (("firefoxurl", "firefox"), ("chromehtml", "chrome"), ("msedgehtm", "edge")):
            if progid.lower().startswith(prefix):
                return browser
    raise ValueError("The default browser is not supported. Name Firefox, Chrome, or Edge.")


def validate_web_url(value):
    """Validate a navigation URL without fetching it."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Provide a valid HTTP or HTTPS website address.")
    if re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ValueError("Website addresses cannot contain whitespace or backslashes.")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                raise ValueError
            if labels[-1].isdigit():
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority += f":{port}"
        return urlunsplit((parts.scheme.lower(), authority, parts.path, parts.query, parts.fragment))
    except (ValueError, UnicodeError):
        raise ValueError("Provide a valid public HTTP or HTTPS website address without credentials.") from None


def launch_browser(browser, new_tab=False, url=None):
    """Launch an allowlisted Windows browser with an optional validated website URL."""
    browsers = {
        "firefox": ("firefox.exe", ("Mozilla Firefox", "firefox.exe")),
        "chrome": ("chrome.exe", ("Google", "Chrome", "Application", "chrome.exe")),
        "edge": ("msedge.exe", ("Microsoft", "Edge", "Application", "msedge.exe")),
    }
    if not isinstance(browser, str) or browser not in (*browsers, "default") or not isinstance(new_tab, bool):
        raise ValueError("Choose Firefox, Chrome, Edge, or the default browser.")
    if url is not None:
        url = validate_web_url(url)
    if sys.platform != "win32":
        raise FileNotFoundError("Opening browsers is available on Windows only.")

    import winreg

    if browser == "default":
        browser = _default_browser()
    executable, relative = browsers[browser]
    candidates = []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + executable
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                if isinstance(value, str):
                    candidates.append(Path(os.path.expandvars(value.strip().strip('"'))))
            except FileNotFoundError:
                continue
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(name)
        if root:
            candidates.append(Path(root).joinpath(*relative))
    for path in candidates:
        if path.is_absolute() and path.name.lower() == executable and path.is_file():
            arguments = [str(path)]
            if new_tab:
                arguments.extend(["-new-tab" if browser == "firefox" else "--new-tab", url or "about:blank"])
            elif url is not None:
                arguments.append(url)
            subprocess.Popen(arguments, close_fds=True)
            return
    raise FileNotFoundError(f"{browser.title()} was not found. Install it and try again.")


def launch_firefox():
    launch_browser("firefox")


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
        raise ValueError("Provide a title between 1 and 300 characters.")
    return value.strip()


def _instant(value, future=False):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Provide a valid date and time with a timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("Provide a valid date and time with a timezone.") from None
    if future and parsed <= datetime.now(timezone.utc):
        raise ValueError("Choose a time in the future.")
    return parsed.isoformat(timespec="milliseconds")


class ActionStore:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                due_at TEXT, completed INTEGER NOT NULL DEFAULT 0,
                notified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def list_items(self):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM items ORDER BY completed, COALESCE(due_at, '9999'), created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, kind, title, due_at=None):
        if kind not in ("task", "reminder"):
            raise ValueError("Unknown item type.")
        title = _text(title)
        if kind == "reminder" and not due_at:
            raise ValueError("Reminders need a date and time.")
        due_at = _instant(due_at, future=True) if due_at else None
        item_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO items (id,kind,title,due_at,created_at) VALUES (?,?,?,?,?)",
                (item_id, kind, title, due_at, created_at),
            )
        return item_id

    def update(self, item_id, operation, due_at=None):
        if not isinstance(item_id, str) or len(item_id) != 32:
            raise ValueError("Invalid item ID.")
        if operation not in ("complete", "dismiss", "postpone", "delete", "claim_notification"):
            raise ValueError("Unknown action.")
        if operation == "postpone":
            due_at = _instant(due_at, future=True)
        with closing(self._connect()) as db, db:
            if operation == "delete":
                cursor = db.execute("DELETE FROM items WHERE id = ?", (item_id,))
            elif operation == "postpone":
                cursor = db.execute(
                    "UPDATE items SET due_at=?, notified=0 WHERE id=? AND completed=0",
                    (due_at, item_id),
                )
            elif operation == "claim_notification":
                now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                cursor = db.execute(
                    "UPDATE items SET notified=1 WHERE id=? AND kind='reminder' "
                    "AND completed=0 AND notified=0 AND due_at<=?", (item_id, now)
                )
            else:
                kind = "task" if operation == "complete" else "reminder"
                cursor = db.execute(
                    "UPDATE items SET completed=1 WHERE id=? AND kind=? AND completed=0",
                    (item_id, kind),
                )
        return cursor.rowcount == 1
