import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import actions
import action_router
import brain
import server


def future():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "actions.db"
        self.store = actions.ActionStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_task_lifecycle_and_restart(self):
        item = self.store.create("task", "Call the supplier")
        self.assertTrue(self.store.update(item, "postpone", future()))
        reopened = actions.ActionStore(self.path)
        self.assertIsNotNone(reopened.list_items()[0]["due_at"])
        self.assertTrue(reopened.update(item, "complete"))
        self.assertEqual(reopened.list_items()[0]["completed"], 1)
        self.assertFalse(reopened.update(item, "complete"))
        self.assertTrue(reopened.update(item, "delete"))
        self.assertEqual(reopened.list_items(), [])

    def test_reminder_validation_and_single_claim(self):
        for invalid in (None, "2026-01-01T10:00", "not a date", "2000-01-01T00:00:00Z"):
            with self.assertRaises(ValueError):
                self.store.create("reminder", "Test", invalid)
        item = self.store.create("reminder", "A reminder", future())
        self.assertFalse(self.store.update(item, "claim_notification"))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE items SET due_at=? WHERE id=?", ("2000-01-01T00:00:00.000+00:00", item))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.store.update(item, "claim_notification"), range(2)))
        self.assertEqual(sorted(claims), [False, True])
        self.assertTrue(self.store.update(item, "postpone", future()))
        self.assertEqual(self.store.list_items()[0]["notified"], 0)
        self.assertTrue(self.store.update(item, "dismiss"))

    def test_invalid_operation_cannot_cross_kinds(self):
        item = self.store.create("task", "A task")
        self.assertFalse(self.store.update(item, "dismiss"))
        with self.assertRaises(ValueError):
            self.store.update(item, "run shell")


@unittest.skipUnless(sys.platform == "win32", "Firefox launcher uses Windows installation paths")
class FirefoxTests(unittest.TestCase):
    def test_launch_uses_only_installed_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Mozilla Firefox" / "firefox.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch("winreg.OpenKey", side_effect=FileNotFoundError), \
                    patch.dict("os.environ", {"PROGRAMFILES": directory}, clear=True), \
                    patch("actions.subprocess.Popen") as popen:
                actions.launch_firefox()
            popen.assert_called_once_with([str(executable)], close_fds=True)

    def test_new_tab_uses_default_browser_and_fixed_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch("winreg.OpenKey", side_effect=FileNotFoundError), \
                    patch("actions._default_browser", return_value="chrome"), \
                    patch.dict("os.environ", {"PROGRAMFILES": directory}, clear=True), \
                    patch("actions.subprocess.Popen") as popen:
                actions.launch_browser("default", True)
            popen.assert_called_once_with([str(executable), "--new-tab", "about:blank"], close_fds=True)

    def test_rejects_unapproved_browsers_and_arguments(self):
        for browser, tab in (("cmd.exe", False), ("chrome", "https://example.com"), (None, False)):
            with self.assertRaises(ValueError):
                actions.launch_browser(browser, tab)

    def test_web_url_passed_as_single_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Mozilla Firefox" / "firefox.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch("winreg.OpenKey", side_effect=FileNotFoundError), \
                    patch.dict("os.environ", {"PROGRAMFILES": directory}, clear=True), \
                    patch("actions.subprocess.Popen") as popen:
                actions.launch_browser("firefox", True, "https://apple.com/mac")
            popen.assert_called_once_with([str(executable), "-new-tab", "https://apple.com/mac"], close_fds=True)

    def test_file_explorer_launch_uses_fixed_system_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "explorer.exe"
            executable.touch()
            with patch.dict("os.environ", {"SystemRoot": directory}), patch("actions.subprocess.Popen") as popen:
                actions.launch_file_explorer()
            popen.assert_called_once_with([str(executable)], close_fds=True)


class RouterTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(brain, "get_config", return_value={"model": "auto"})
        self.addCleanup(config.stop)
        config.start()
        call = patch.object(brain, "_call_llm")
        self.addCleanup(call.stop)
        self.call = call.start()

    def test_url_validation(self):
        self.assertEqual(actions.validate_web_url("https://apple.com/mac"), "https://apple.com/mac")
        for value in ("javascript:alert(1)", "file:///test", "https://apple.com@evil.com",
                      "http://127.0.0.1/admin", "https://apple.com\\@evil.com",
                      "https://apple.com\n.evil.com", "https://apple.com:99999", "apple.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                actions.validate_web_url(value)

    def test_model_uses_only_url_from_user_input(self):
        self.call.return_value = '{"type":"open_url","browser":"default","new_tab":true}'
        result = action_router.classify("Could you take me to apple dot com?")
        self.assertEqual(result, {"type": "open_url", "browser": "default", "new_tab": True, "url": "https://apple.com"})
        self.assertEqual(action_router.classify("Open apple.com and example.com"), {"type": "clarify"})
        self.assertEqual(action_router.classify("Open a website"), {"type": "clarify"})
        self.assertEqual(action_router.classify("Do not open apple.com"), {"type": "clarify"})
        self.assertEqual(action_router.classify("Open https://apple.com:8080"), {"type": "clarify"})

    def test_rejects_invalid_or_invented_actions(self):
        self.call.return_value = '{"type":"open_browser","browser":"chrome","new_tab":false}'
        self.assertEqual(action_router.classify("open a browser"), {"type": "clarify"})
        self.call.return_value = '{"type":"open_url","browser":"chrome","new_tab":true}'
        self.assertEqual(action_router.classify("take me to chrome.com"), {"type": "clarify"})
        self.assertEqual(action_router.classify("take me to apple.com in chrome")["browser"], "chrome")
        self.call.return_value = '{"type":"open_url","browser":"default","new_tab":true,"url":"https://evil.com"}'
        with self.assertRaises(RuntimeError):
            action_router.classify("open apple.com")
        self.call.return_value = '{"type":"run_shell","browser":"default","new_tab":false}'
        with self.assertRaises(RuntimeError):
            action_router.classify("open a browser")

    def test_classifier_reports_model_errors(self):
        self.call.return_value = "not JSON"
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            action_router.classify("open a browser")
        self.call.return_value = '```json\n{"type":"open_url","browser":"default","new_tab":true}\n```'
        self.assertEqual(action_router.classify("open apple.com")["url"], "https://apple.com")

    def test_model_file_explorer_requires_user_mention(self):
        self.call.return_value = '{"type":"open_file_explorer","browser":"default","new_tab":false}'
        self.assertEqual(action_router.classify("Please bring up file explorer"), {"type": "open_file_explorer"})
        self.assertEqual(action_router.classify("Jarvis, bring up Windows Explorer"), {"type": "open_file_explorer"})
        self.assertEqual(action_router.classify("open a browser"), {"type": "clarify"})
        self.assertEqual(action_router.classify("open file explorer to downloads"), {"type": "clarify"})
        self.assertEqual(action_router.classify("don't open file explorer"), {"type": "clarify"})


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = actions.ActionStore(Path(self.tmp.name) / "actions.db")
        self.patch = patch.object(server, "ACTION_STORE", self.store)
        self.patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.host = "127.0.0.1:%s" % self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        self.patch.stop()
        self.tmp.cleanup()

    def request(self, method, body=None, headers=None, path="/actions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            result = conn.getresponse()
            return result.status, json.loads(result.read())
        finally:
            conn.close()

    def test_local_crud_and_origin_protection(self):
        body = json.dumps({"operation": "create", "kind": "task", "title": "Call"})
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        self.assertEqual(self.request("POST", body, headers)[0], 201)
        self.assertEqual(self.request("GET")[1]["items"][0]["title"], "Call")
        self.assertEqual(self.request("POST", None, {**headers, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", None, {**headers, "Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", None, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("POST", None, {**headers, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(len(self.store.list_items()), 1)

    def test_bad_json_and_invalid_values(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        self.assertEqual(self.request("POST", "{", headers)[0], 400)
        invalid = json.dumps({"operation": "create", "kind": "reminder", "title": "Call"})
        self.assertEqual(self.request("POST", invalid, headers)[0], 400)

    def test_firefox_requires_confirmation_and_same_origin(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        with patch.object(actions, "launch_firefox") as launch:
            body = json.dumps({"operation": "open_firefox"})
            self.assertEqual(self.request("POST", body, headers)[0], 400)
            body = json.dumps({"operation": "open_firefox", "confirmed": True})
            self.assertEqual(self.request("POST", None, {**headers, "Origin": "https://evil.example"})[0], 403)
            launch.assert_not_called()
            self.assertEqual(self.request("POST", body, headers)[0], 200)
            launch.assert_called_once_with()

    def test_firefox_not_installed_is_reported(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        body = json.dumps({"operation": "open_firefox", "confirmed": True})
        with patch.object(actions, "launch_firefox", side_effect=FileNotFoundError("Firefox was not found.")):
            status, result = self.request("POST", body, headers)
        self.assertEqual(status, 503)
        self.assertEqual(result["error"], "Firefox was not found.")

    def test_browser_action_validates_and_confirms(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        with patch.object(actions, "launch_browser") as launch:
            body = json.dumps({"operation": "open_browser", "browser": "chrome", "new_tab": True})
            self.assertEqual(self.request("POST", body, headers)[0], 400)
            body = json.dumps({"operation": "open_browser", "browser": "chrome", "new_tab": True, "confirmed": True})
            self.assertEqual(self.request("POST", body, headers)[0], 200)
            launch.assert_called_once_with("chrome", True)
        body = json.dumps({"operation": "open_browser", "browser": "cmd.exe", "new_tab": False, "confirmed": True})
        self.assertEqual(self.request("POST", body, headers)[0], 400)

    def test_research_alias_for_custom_local_port(self):
        body = json.dumps({"question": "research batteries"})
        with patch.object(server.research, "run_research", return_value={"topic": "batteries"}) as run:
            status, result = self.request("POST", body, {"Content-Type": "application/json"}, "/api/research")
        self.assertEqual(status, 200)
        self.assertEqual(result["research"]["topic"], "batteries")
        run.assert_called_once()

    def test_website_action_requires_confirmation_and_validates_url(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        payload = {"operation": "open_url", "browser": "default", "new_tab": True, "url": "https://apple.com"}
        with patch.object(actions, "launch_browser") as launch:
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 400)
            payload["confirmed"] = True
            payload["url"] = "javascript:alert(1)"
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 400)
            launch.assert_not_called()
            payload["url"] = "https://apple.com"
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 200)
            launch.assert_called_once_with("default", True, "https://apple.com")

    def test_file_explorer_requires_confirmation_and_no_arguments(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        payload = {"operation": "open_file_explorer"}
        with patch.object(actions, "launch_file_explorer") as launch:
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 400)
            payload["confirmed"] = True
            payload["path"] = "C:\\"
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 400)
            launch.assert_not_called()
            del payload["path"]
            self.assertEqual(self.request("POST", json.dumps(payload), headers)[0], 200)
            launch.assert_called_once_with()
        with patch.object(actions, "launch_file_explorer", side_effect=FileNotFoundError("File Explorer was not found.")):
            status, data = self.request("POST", json.dumps(payload), headers)
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "File Explorer was not found.")

    def test_model_classification_endpoint(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        body = json.dumps({"operation": "classify", "text": "Take me to apple.com"})
        result = {"type": "open_url", "browser": "default", "new_tab": True, "url": "https://apple.com"}
        with patch.object(action_router, "classify", return_value=result):
            status, data = self.request("POST", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(data["action"], result)
        with patch.object(action_router, "classify", side_effect=RuntimeError("Invalid response.")):
            status, data = self.request("POST", body, headers)
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "Invalid response.")

    def test_freellm_error_is_reported_by_api(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        body = json.dumps({"operation": "classify", "text": "Take me to apple.com"})
        with patch.object(action_router, "classify", side_effect=brain.BrainError(429, "rate_limited", "Rate limited.")):
            status, data = self.request("POST", body, headers)
        self.assertEqual(status, 429)
        self.assertEqual(data["error"], "Rate limited.")
        self.assertEqual(data["code"], "rate_limited")

    def test_api_classifies_with_freellm_even_when_ollama_is_configured(self):
        headers = {"Origin": "http://" + self.host, "Content-Type": "application/json", "X-Jarvis-Action": "1"}
        body = json.dumps({"operation": "classify", "text": "Take me to apple.com"})
        response = '{"type":"open_url","browser":"default","new_tab":true}'
        with patch.dict("os.environ", {"OLLAMA_MODEL": "configured"}), \
                patch.object(brain, "get_config", return_value={"model": "auto"}), \
                patch.object(brain, "_post_chat", return_value={"choices": [{"message": {"content": response}}]}) as post, \
                patch.object(actions, "launch_browser") as launch:
            status, data = self.request("POST", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(data["action"]["url"], "https://apple.com")
        messages = post.call_args.args[1]["messages"]
        self.assertEqual(messages[-1], {"role": "user", "content": json.dumps("Take me to apple.com")})
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
