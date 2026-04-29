"""Tests for the legacy Flask backend's behaviour-critical helpers.

These cover the bug fixes applied in this round:

- `sse_stream` exits on a terminal status and cleans `active_queues`
- `/getFile` rejects path-traversal attempts via robust ``Path.resolve``
"""

from __future__ import annotations

import importlib.util
import unittest
from queue import Queue


flask_available = importlib.util.find_spec("flask") is not None


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class SseStreamTests(unittest.TestCase):
    def test_exits_on_terminal_success_payload(self) -> None:
        from sau_backend import active_queues, sse_stream

        queue = Queue()
        queue.put("data:image/png;base64,abc")
        queue.put("200")
        active_queues["t1"] = queue

        out = list(sse_stream(queue, login_id="t1"))
        self.assertEqual(out[-1], "data: 200\n\n")
        self.assertEqual(len(out), 2)
        self.assertNotIn("t1", active_queues)

    def test_exits_on_terminal_failure_payload(self) -> None:
        from sau_backend import active_queues, sse_stream

        queue = Queue()
        queue.put("500")
        active_queues["t2"] = queue

        out = list(sse_stream(queue, login_id="t2"))
        self.assertEqual(out, ["data: 500\n\n"])
        self.assertNotIn("t2", active_queues)

    def test_idle_timeout_emits_500(self) -> None:
        from sau_backend import sse_stream

        queue = Queue()
        # Use a tiny idle timeout so the test does not block.
        out = list(sse_stream(queue, login_id="t3", idle_timeout=0.1))
        self.assertEqual(out, ["data: 500\n\n"])


@unittest.skipUnless(flask_available, "Flask not installed (optional [web] extra)")
class GetFileTraversalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        from sau_backend import app

        self.client = app.test_client()

    def test_dotdot_traversal_rejected(self) -> None:
        response = self.client.get("/getFile?filename=../conf.py")
        self.assertEqual(response.status_code, 400)

    def test_absolute_path_rejected(self) -> None:
        response = self.client.get("/getFile?filename=/etc/passwd")
        self.assertEqual(response.status_code, 400)

    def test_missing_filename_rejected(self) -> None:
        response = self.client.get("/getFile")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
