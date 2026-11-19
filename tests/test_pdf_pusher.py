import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf_pusher import (  # noqa: E402
    CaptureResult,
    Task,
    build_email,
    load_tasks,
    parse_manual_tasks,
    safe_pdf_path,
    same_origin,
)


class TaskTests(unittest.TestCase):
    def test_parse_multiple_manual_tasks(self):
        tasks = parse_manual_tasks(
            "资料 4 | https://example.com/page#4; https://example.com/long\n资料 6|https://example.com/page#6"
        )
        self.assertEqual([task.name for task in tasks], ["资料 4", "手动任务 2", "资料 6"])
        self.assertEqual(tasks[0].url, "https://example.com/page#4")
        self.assertEqual(tasks[2].url, "https://example.com/page#6")

    def test_empty_manual_input(self):
        self.assertEqual(parse_manual_tasks(" ; \n"), [])

    def test_rejects_invalid_urls(self):
        for value in ("file:///tmp/a", "https://user:pass@example.com", "https://exa mple.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_manual_tasks(value)

    def test_load_only_enabled_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {"name": "启用", "url": "https://example.com", "frame_selector": "#main"},
                            {"name": "停用", "url": "https://example.com/off", "enabled": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            tasks = load_tasks(path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].frame_selector, "#main")

    def test_pdf_filename_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = safe_pdf_path(Path(directory), Task('资料 / 4:"测试"', "https://example.com"))
        self.assertNotRegex(path.name, r'[\\/:*?"<>|\s]')
        self.assertTrue(path.name.endswith(".pdf"))

    def test_same_origin_default_ports(self):
        self.assertTrue(same_origin("https://example.com/a", "https://example.com:443/b"))
        self.assertFalse(same_origin("https://example.com/a", "https://other.example/b"))

    def test_email_contains_body_and_pdf_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "test.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            result = CaptureResult(Task("测试", "https://example.com"), pdf_path, "https://example.com")
            message = build_email(result, "sender@qq.com", ["receiver@qq.com"], True)
        self.assertIsNotNone(message.get_body(preferencelist=("plain",)))
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "test.pdf")


if __name__ == "__main__":
    unittest.main()
