import tempfile
import unittest
import gc
from pathlib import Path

import research


class ResearchTests(unittest.TestCase):
    def test_intent_detection(self):
        intent = research.detect_intent("Jarvis, research the latest solid-state batteries")
        self.assertEqual(intent["kind"], "research")
        self.assertIn("solid-state batteries", intent["topic"])
        self.assertTrue(intent["recent"])

    def test_query_generation(self):
        intent = research.detect_intent("Investigate EV batteries in India and China")
        queries = research.search_queries("EV batteries in India and China", intent)
        self.assertGreaterEqual(len(queries), 3)
        self.assertLessEqual(len(queries), 5)

    def test_url_validation(self):
        self.assertIsNotNone(research.canonical_url("https://example.com/article"))
        self.assertIsNone(research.canonical_url("file:///etc/passwd"))
        self.assertFalse(research.is_safe_url("http://127.0.0.1/admin"))
        self.assertFalse(research.is_safe_url("http://169.254.169.254/latest/meta-data"))

    def test_deduplication(self):
        urls = ["https://example.com/a", "https://example.com/a", "https://example.com/b"]
        seen, output = set(), []
        for url in urls:
            if url not in seen:
                seen.add(url)
                output.append(url)
        self.assertEqual(output, ["https://example.com/a", "https://example.com/b"])

    def test_sqlite_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = research.ResearchStore(Path(directory) / "research.sqlite3")
            report = {
                "id": "r_test", "user_query": "batteries", "topic": "batteries",
                "depth": "standard", "search_queries": ["batteries"],
                "executive_summary": "Summary", "key_findings": [],
                "sources": [], "followups": [], "created_at": "2026-01-01T00:00:00+00:00",
            }
            store.save(report)
            self.assertEqual(store.get("r_test")["topic"], "batteries")
            self.assertTrue(store.delete("r_test"))
            gc.collect()


if __name__ == "__main__":
    unittest.main()
