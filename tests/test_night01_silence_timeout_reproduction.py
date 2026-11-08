"""Night 01 regression: prevent the parallel silence timeout.

The cancel-aware FFmpeg runner must drain stdout and stderr while the child is
running.  A child that emits more stderr than the Windows pipe buffer used to
remain blocked and was killed by the runner timeout.
"""

import sys
import threading
import time
import unittest

from silence_detection import SilenceDetectionCancelled, _run


class Night01SilenceTimeoutReproductionTests(unittest.TestCase):
    def test_large_stderr_is_drained_without_timeout(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 1048576); sys.stderr.flush()",
        ]
        started = time.monotonic()
        result = _run(command, timeout=1, cancel_event=threading.Event())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr), 1048576)
        self.assertLess(time.monotonic() - started, 5)

    def test_cancel_terminates_child_after_pipes_are_drained(self):
        command = [
            sys.executable,
            "-c",
            "import sys, time; sys.stderr.write('x' * 1048576); sys.stderr.flush(); time.sleep(30)",
        ]
        cancel_event = threading.Event()
        timer = threading.Timer(0.1, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(SilenceDetectionCancelled, "キャンセル"):
                _run(command, timeout=5, cancel_event=cancel_event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
