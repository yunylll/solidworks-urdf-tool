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

    def test_preference_restore_decision(self):
        _, directory = self.fake_job(5)
        needed = tool_service.preference_restore_needed
        self.assertFalse(needed(directory, {}), "no snapshot means nothing was changed")
        write_json(directory / "output" / "preferences-snapshot.json", {})
        self.assertTrue(needed(directory, {}), "killed run without a report")
        self.assertFalse(needed(directory, {"bridge": {"status": "failed"}}), "failed before preferences were touched")
        self.assertTrue(needed(directory, {"bridge": {"preferencesBefore": {}, "preferencesRestored": False}}))
        self.assertFalse(needed(directory, {"bridge": {"preferencesBefore": {}, "preferencesRestored": True}}))

    def test_interrupted_job_queues_restore(self):
        identifier, directory = self.fake_job(tool_service.WORKER_START_GRACE_SECONDS + 30)
        write_json(directory / "output" / "preferences-snapshot.json", {})
        marker = tool_service.PENDING_RESTORES / f"{identifier}.json"
        try:
            state = get_job(identifier)
            self.assertTrue(state["preference_restore"]["pending"])
            self.assertTrue(marker.is_file())
        finally:
            marker.unlink(missing_ok=True)

    def test_optional_paths_are_keyword_only(self):
        with self.assertRaises(TypeError):
            tool_service.export_urdf("robot.SLDASM", "robot_description", "config.json")
        with self.assertRaises(TypeError):
            tool_service.start_export("robot.SLDASM", "robot_description", "config.json")

    def test_live_worker_kept(self):
        identifier, directory = self.fake_job(5)
        write_json(directory / "worker.json", process_identity(tool_service.os.getpid()))
        self.assertEqual(get_job(identifier)["status"], "queued")

    def running_record(self, directory, **limits):
        record = {"job_id": directory.name, "status": "running", "operation": "export", "created_at": tool_service.stamp(), "started_at": tool_service.stamp(), "phase": "native", "timeout_seconds": 600, "stall_timeout_seconds": 600}
        record.update(limits)
        return record

    def test_idle_process_is_stalled(self):
        _, directory = self.fake_job(5)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        original = tool_service.ACTIVITY_SAMPLE_SECONDS
        tool_service.ACTIVITY_SAMPLE_SECONDS = 1
        try:
            record = self.running_record(directory, stall_timeout_seconds=3)
            start = time.monotonic()
            self.assertIsNone(tool_service._wait_native(directory, record, child))
        finally:
            tool_service.ACTIVITY_SAMPLE_SECONDS = original
            if child.poll() is None:
                child.kill()
        self.assertEqual(record["status"], "timed_out")
        self.assertEqual(record["error"]["code"], "CAD_STALLED")
        self.assertLess(time.monotonic() - start, 30)

    def test_busy_process_is_not_stalled_until_hard_limit(self):
        _, directory = self.fake_job(5)
        # The venv python.exe is a launcher; only the base interpreter itself uses the CPU.
        child = subprocess.Popen([sys._base_executable, "-c", "import time\nend = time.time() + 60\nwhile time.time() < end: pass"])
        original = tool_service.ACTIVITY_SAMPLE_SECONDS
        tool_service.ACTIVITY_SAMPLE_SECONDS = 1
        try:
            record = self.running_record(directory, stall_timeout_seconds=4, timeout_seconds=10)
            self.assertIsNone(tool_service._wait_native(directory, record, child))
        finally:
            tool_service.ACTIVITY_SAMPLE_SECONDS = original
            if child.poll() is None:
                child.kill()
        self.assertEqual(record["error"]["code"], "CAD_TIMEOUT", record["error"])

    def test_progress_keeps_job_alive(self):
        _, directory = self.fake_job(5)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        progress = directory / "output" / "progress.json"
        stop = threading.Event()

        def report():
            step = 0
            while not stop.wait(1):
                step += 1
                write_json(progress, {"stage": "export_meshes", "detail": f"link_{step}", "current": step, "total": 18, "stage_started_at": tool_service.stamp(), "updated_at": tool_service.stamp()})

        thread = threading.Thread(target=report)
        thread.start()
        try:
            record = self.running_record(directory, stall_timeout_seconds=3, timeout_seconds=8)
            tool_service._wait_native(directory, record, child)
        finally:
            stop.set()
            thread.join()
            if child.poll() is None:
                child.kill()
        self.assertEqual(record["error"]["code"], "CAD_TIMEOUT", "steady progress must not count as a stall")
        self.assertIn("Exporting STL meshes", record["error"]["message"])

    def test_cancel_running_process(self):
        _, directory = self.fake_job(5)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        write_json(directory / "cancel-request.json", {})
        try:
            record = self.running_record(directory)
            self.assertIsNone(tool_service._wait_native(directory, record, child))
        finally:
            if child.poll() is None:
                child.kill()
        self.assertEqual(record["status"], "cancelled")
        self.assertIsNotNone(child.poll(), "the native process must be stopped")

    def test_progress_summary(self):
        identifier, directory = self.fake_job(5)
        record = self.running_record(directory)
        write_json(directory / "result.json", record)
        write_json(directory / "worker.json", process_identity(tool_service.os.getpid()))
        write_json(directory / "output" / "progress.json", {"stage": "export_meshes", "detail": "gripper_z", "current": 7, "total": 18, "stage_started_at": "2026-10-08T05:50:14.1234567Z", "updated_at": "2026-10-08T05:51:00.0000000Z"})
        progress = get_job(identifier)["progress"]
        self.assertEqual(progress["summary"], "Exporting STL meshes: 7/18 (gripper_z)")
        self.assertGreater(progress["stage_elapsed_seconds"], 0)
        self.assertIn("elapsed_seconds", progress)

    def test_wait_returns_when_finished(self):
        identifier, directory = self.fake_job(5)
        write_json(directory / "worker.json", process_identity(tool_service.os.getpid()))
        finished = {"job_id": identifier, "status": "succeeded", "passed": True, "operation": "export", "created_at": tool_service.stamp(), "finished_at": tool_service.stamp()}
        threading.Timer(2, lambda: write_json(directory / "result.json", finished)).start()
        start = time.monotonic()
        state = tool_service.wait_job(identifier, 30, poll_seconds=0.5)
        self.assertEqual(state["status"], "succeeded")
        self.assertLess(time.monotonic() - start, 10)
        start = time.monotonic()
        other, other_directory = self.fake_job(5)
        write_json(other_directory / "worker.json", process_identity(tool_service.os.getpid()))
        self.assertEqual(tool_service.wait_job(other, 2, poll_seconds=0.5)["status"], "queued")
        self.assertLess(time.monotonic() - start, 6)

    def test_cancel_queued_job(self):
        """A job waiting for the CAD lock stops without starting SolidWorks."""
        model = tool_service.WORKSPACE / "vendor" / "solidworks_urdf_exporter" / "examples" / "TOY_BLOCK" / "BlockA.SLDPRT"
        identifier = tool_service.create_job("inspect", model)
        self.created.append(tool_service.JOBS / identifier)
        release, held = threading.Event(), threading.Event()

        def holder():
            with native_lock(5):
                held.set()
                release.wait(60)

        lock_thread = threading.Thread(target=holder)
        lock_thread.start()
        held.wait(5)
        worker = threading.Thread(target=tool_service.run_job, args=(identifier,))
        worker.start()
        try:
            deadline = time.monotonic() + 20
            while get_job(identifier).get("progress", {}).get("stage") != "waiting_for_cad_lock" and time.monotonic() < deadline:
                time.sleep(0.5)
            state = tool_service.cancel_job(identifier, 20)
        finally:
            release.set()
            lock_thread.join()
            worker.join(30)
        self.assertEqual(state["status"], "cancelled", state)
        self.assertEqual(state["error"]["code"], "CANCELLED")
        self.assertFalse((tool_service.JOBS / identifier / "output" / "session.json").exists())

    def test_cancel_finished_job_is_harmless(self):
        identifier, directory = self.fake_job(5)
        write_json(directory / "result.json", {"job_id": identifier, "status": "failed", "passed": False, "operation": "export", "created_at": tool_service.stamp()})
        state = tool_service.cancel_job(identifier, 0)
        self.assertEqual(state["status"], "failed")
        self.assertFalse((directory / "cancel-request.json").exists())

    def test_input_errors_name_the_value(self):
        with self.assertRaisesRegex(ValueError, "nothing.SLDASM"):
            tool_service.create_job("export", tool_service.WORKSPACE / "nothing.SLDASM")
        model = tool_service.WORKSPACE / "vendor" / "solidworks_urdf_exporter" / "examples" / "TOY_BLOCK" / "BlockA.SLDPRT"
        with self.assertRaisesRegex(ValueError, "got 5"):
            tool_service.create_job("export", model, timeout_seconds=5)
        with self.assertRaisesRegex(ValueError, "config_path"):
            tool_service.create_job("check", model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
