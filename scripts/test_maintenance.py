"""Offline tests of cleanup_jobs and publish_to with fake job directories (no SolidWorks)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
import uuid

import tool_service
from tool_service import cleanup_jobs, publish_job, read_json, sha256, write_json


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs"
        self.original_jobs, tool_service.JOBS = tool_service.JOBS, self.jobs

    def tearDown(self):
        tool_service.JOBS = self.original_jobs
        self.temporary.cleanup()

    def job(self, days, status="succeeded", passed=True, model="C:/cad/robot.SLDASM", package="robot_description", cad="v1", snapshot=True, **extra):
        identifier = uuid.uuid4().hex
        directory = self.jobs / identifier
        cad_dir = directory / "output" / "cad"
        record = {"job_id": identifier, "status": status, "passed": passed, "operation": "export", "model_path": model, "created_at": ago(days), "cad_version_sha256": cad}
        if status in tool_service.TERMINAL:
            record["finished_at"] = ago(days)
        if snapshot:
            cad_dir.mkdir(parents=True)
            (cad_dir / "robot.SLDASM").write_bytes(b"x" * 1000)
            write_json(cad_dir / "solidworks-urdf-snapshot.json", {"source_path": str(cad_dir / "robot.SLDASM")})
            record["reusable_model"] = str(cad_dir / "robot.SLDASM")
        record.update(extra)
        meshes = directory / "output" / package / "meshes"
        meshes.mkdir(parents=True)
        (meshes / "base_link.STL").write_bytes(b"mesh")
        (directory / "output" / package / "urdf").mkdir()
        (directory / "output" / package / "urdf" / f"{package}.urdf").write_text("<robot/>", encoding="utf-8")
        write_json(directory / "request.json", {"model_path": model, "package_name": package})
        write_json(directory / "robot-config.json", {"links": []})
        write_json(directory / "mesh-keys.json", {"base_link": {"key": "k", "stl": str(meshes / "base_link.STL")}})
        (directory / "stdout.log").write_text("log", encoding="utf-8")
        write_json(directory / "result.json", record)
        return identifier

    def cad(self, identifier):
        return self.jobs / identifier / "output" / "cad"


class CleanupTests(Fixture):
    def test_dry_run_lists_and_deletes_nothing(self):
        old, new = self.job(40), self.job(1)
        report = cleanup_jobs(older_than_days=30, jobs_root=self.jobs)
        self.assertTrue(report["dry_run"])
        self.assertEqual([c["job_id"] for c in report["cleaned"]], [old])
        self.assertGreater(report["bytes_freed"], 1000)
        self.assertTrue(self.cad(old).is_dir() and self.cad(new).is_dir())

    def test_delete_keeps_results_package_and_mesh_keys(self):
        old, _ = self.job(40), self.job(1)
        report = cleanup_jobs(older_than_days=30, dry_run=False, jobs_root=self.jobs)
        self.assertEqual(len(report["cleaned"]), 1)
        directory = self.jobs / old
        self.assertFalse(self.cad(old).exists())
        for kept in ("result.json", "request.json", "robot-config.json", "mesh-keys.json", "stdout.log", "output/robot_description/meshes/base_link.STL"):
            self.assertTrue((directory / kept).is_file(), kept)
        record = read_json(directory / "result.json")
        self.assertIn("cleaned_at", record)
        self.assertNotIn("reusable_model", record)
        self.assertTrue(record["reusable_model_removed"].endswith("robot.SLDASM"))
        self.assertEqual(record["cleanup"][0]["removed"], [str(self.cad(old))])

    def test_logs_only_on_request(self):
        old, _ = self.job(40), self.job(1)
        cleanup_jobs(older_than_days=30, dry_run=False, include_logs=True, jobs_root=self.jobs)
        self.assertFalse((self.jobs / old / "stdout.log").exists())

    def test_running_jobs_are_skipped(self):
        running = self.job(40, status="running", passed=False)
        report = cleanup_jobs(older_than_days=30, dry_run=False, keep_latest_success=False, jobs_root=self.jobs)
        self.assertEqual(report["cleaned"], [])
        self.assertIn("not terminal", report["skipped"][0]["reason"])
        self.assertTrue(self.cad(running).is_dir())

    def test_latest_success_is_kept_unless_disabled(self):
        only = self.job(40)
        self.assertEqual(cleanup_jobs(older_than_days=30, jobs_root=self.jobs)["cleaned"], [])
        self.assertEqual([c["job_id"] for c in cleanup_jobs(older_than_days=30, keep_latest_success=False, jobs_root=self.jobs)["cleaned"]], [only])

    def test_keep_latest_per_model_and_package(self):
        a_old, a_new = self.job(3), self.job(2)
        b_old = self.job(5, model="C:/cad/other.SLDASM", status="failed", passed=False)
        report = cleanup_jobs(keep_latest=1, jobs_root=self.jobs)
        self.assertEqual([c["job_id"] for c in report["cleaned"]], [a_old])
        self.assertTrue(all(c["job_id"] not in {a_new, b_old} for c in report["cleaned"]))

    def test_snapshot_used_by_kept_job_is_skipped(self):
        source = self.job(40, status="failed", passed=False)
        user = self.job(1, model=str(self.cad(source) / "robot.SLDASM"), original_model_path="C:/cad/robot.SLDASM", snapshot=False)
        write_json(self.jobs / user / "source-manifest.json", {"source_path": str(self.cad(source) / "robot.SLDASM")})
        report = cleanup_jobs(older_than_days=30, dry_run=False, jobs_root=self.jobs)
        self.assertEqual(report["cleaned"], [])
        self.assertIn(user, report["skipped"][0]["reason"])
        self.assertTrue(self.cad(source).is_dir())

    def test_equivalent_snapshot_of_same_cad_version(self):
        old = self.job(40, status="failed", passed=False)
        kept = self.job(1)
        report = cleanup_jobs(older_than_days=30, dry_run=False, jobs_root=self.jobs)
        self.assertEqual(report["cleaned"][0]["equivalent_snapshot"]["job_id"], kept)
        self.assertIn(kept, read_json(self.jobs / old / "result.json")["reusable_model_hint"])

    def test_needs_a_selection(self):
        with self.assertRaises(ValueError):
            cleanup_jobs(jobs_root=self.jobs)


class PublishTests(Fixture):
    def record(self, identifier, target):
        record = read_json(self.jobs / identifier / "result.json")
        record["publish_to"] = str(target)
        return record

    def test_publish_copies_and_moves_old_files_to_history(self):
        target = self.root / "twin"
        first = self.job(2)
        published = publish_job(self.jobs / first, self.record(first, target))
        self.assertTrue(published["passed"], published)
        self.assertEqual(published["moved_to_history"], [])
        urdf = target / "urdf" / "robot_description" / "meshes" / "base_link.STL"
        self.assertTrue(urdf.is_file())
        for name in ("export-config.json", "export-request.json", "export-result.json"):
            self.assertEqual(published["files"][name]["sha256"], sha256(target / "export" / name))
        self.assertEqual(read_json(target / "export" / "export-config.json"), {"links": []})

        second = self.job(1)
        (self.jobs / second / "output" / "robot_description" / "meshes" / "base_link.STL").write_bytes(b"new mesh")
        published = publish_job(self.jobs / second, self.record(second, target))
        self.assertTrue(published["passed"], published)
        history = Path(published["history"])
        self.assertTrue(history.name.endswith("_job-" + second[:12]))
        self.assertEqual(history.parent, target / "export" / "history")
        self.assertEqual((history / "urdf" / "robot_description" / "meshes" / "base_link.STL").read_bytes(), b"mesh")
        self.assertEqual(read_json(history / "export-result.json")["job_id"], first)
        self.assertEqual(urdf.read_bytes(), b"new mesh")
        self.assertEqual(len(published["moved_to_history"]), 4)

    def test_failed_export_is_not_published(self):
        failed = self.job(1, status="failed", passed=False)
        published = publish_job(self.jobs / failed, self.record(failed, self.root / "twin"))
        self.assertTrue(published["skipped"])
        self.assertFalse((self.root / "twin").exists())

    def test_missing_package_reports_error(self):
        identifier = self.job(1, package="robot_description")
        write_json(self.jobs / identifier / "request.json", {"package_name": "other"})
        published = publish_job(self.jobs / identifier, self.record(identifier, self.root / "twin"))
        self.assertFalse(published["passed"])
        self.assertEqual(published["error"]["code"], "PUBLISH_FAILED")

    def test_publish_target_checks(self):
        self.jobs.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            tool_service.check_publish_to(self.jobs / "x")
        (self.root / "file").write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            tool_service.check_publish_to(self.root / "file")
        self.assertEqual(tool_service.check_publish_to(self.root / "twin"), str((self.root / "twin").resolve()))


if __name__ == "__main__":
    unittest.main()
