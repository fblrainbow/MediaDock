"""Stage-002 Task model tests (T201-T203)."""
import json
import unittest

from core_task import LEGACY_FIELDS, Task, legacy_view, task_from_dict


class TestTaskModel(unittest.TestCase):
    def test_defaults_complete(self):
        t = Task(task_id="abc123", url="https://example.com/v")
        d = t.to_dict()
        for name in ("task_id", "type", "status", "url", "platform",
                     "title", "percent", "speed", "eta", "file_path",
                     "error_code", "error_message", "created_at",
                     "started_at", "updated_at", "completed_at",
                     "completion_order"):
            self.assertIn(name, d, name)
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["percent"], 0.0)
        self.assertEqual(d["type"], "download")

    def test_json_serializable(self):
        d = Task(task_id="abc123", url="https://example.com/v").to_dict()
        json.dumps(d, ensure_ascii=False)

    def test_copy_semantics(self):
        t = Task(task_id="abc123", url="https://example.com/v")
        d = t.to_dict()
        d["percent"] = 999
        d["title"] = "mutated"
        self.assertNotEqual(t.percent, 999)
        self.assertEqual(t.title, "")

    def test_legacy_view_subset(self):
        d = legacy_view(Task(task_id="abc123", url="https://example.com/v"))
        self.assertEqual(set(d.keys()), set(LEGACY_FIELDS))

    def test_from_legacy_dict(self):
        legacy = {"status": "downloading", "percent": 8.8,
                  "speed": "1KiB/s", "eta": "00:01",
                  "url": "https://example.com/v", "title": "t",
                  "created_at": "2026-09-19T00:00:00",
                  "updated_at": "2026-09-19T00:00:00"}
        t = task_from_dict(legacy)
        self.assertEqual(t.percent, 8.8)
        self.assertEqual(t.status, "downloading")


if __name__ == "__main__":
    unittest.main()
