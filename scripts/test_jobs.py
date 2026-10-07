"""Offline checks of CAD queueing and worker-liveness detection (no SolidWorks)."""
from datetime import datetime, timedelta, timezone
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid

import tool_service
from tool_service import CadBusyError, get_job, native_lock, process_identity, write_json


class JobChecks(unittest.TestCase):
    def setUp(self):
        self.created = []

    def tearDown(self):
        for directory in self.created:
            shutil.rmtree(directory, ignore_errors=True)

    def fake_job(self, age_seconds):
        identifier = uuid.uuid4().hex
        directory = tool_service.JOBS / identifier
        (directory / "output").mkdir(parents=True)
        self.created.append(directory)
        created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        write_json(directory / "result.json", {"job_id": identifier, "status": "queued", "passed": False, "operation": "export", "created_at": created.isoformat()})
        return identifier, directory

    def test_second_operation_waits_in_queue(self):
        release = threading.Event()
        held = threading.Event()

        def holder():
            with native_lock(5):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        waited = []
        threading.Timer(3, release.set).start()
        start = time.monotonic()
        with native_lock(30, lambda: waited.append(True)):
            elapsed = time.monotonic() - start
        thread.join()
        self.assertTrue(waited)
        self.assertGreater(elapsed, 2)

    def test_queue_wait_expires(self):
        release = threading.Event()
        held = threading.Event()

        def holder():
            with native_lock(5):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            with self.assertRaises(CadBusyError):
                with native_lock(1):
                    pass
        finally:
            release.set()
            thread.join()

    def test_worker_never_started(self):
        identifier, _ = self.fake_job(tool_service.WORKER_START_GRACE_SECONDS + 30)
        state = get_job(identifier)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["error"]["code"], "WORKER_INTERRUPTED")

    def test_recent_job_without_worker_stays_queued(self):
        identifier, _ = self.fake_job(5)
        self.assertEqual(get_job(identifier)["status"], "queued")

    def test_dead_worker_detected(self):
        identifier, directory = self.fake_job(5)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        identity = process_identity(child.pid)
        child.wait()
        write_json(directory / "worker.json", identity)
        self.assertEqual(get_job(identifier)["status"], "interrupted")

    def test_live_worker_kept(self):
        identifier, directory = self.fake_job(5)
        write_json(directory / "worker.json", process_identity(tool_service.os.getpid()))
        self.assertEqual(get_job(identifier)["status"], "queued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
