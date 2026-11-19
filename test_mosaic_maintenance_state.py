"""Local-only tests; no production dataset or report directory is accessed."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mosaic_maintenance_state as maintenance


class MaintenanceStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uav_maintenance_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "state"
        self.dataset = self.root / "production.gdb" / "Mosaic"
        self.state = maintenance.MaintenanceState(self.directory)

    def record_path(self, dataset=None):
        key = maintenance.dataset_key(dataset or self.dataset)
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def run_child(self, code, *args):
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.directory), *map(str, args)],
            cwd=str(Path(__file__).parent), env=env, capture_output=True, text=True, timeout=30,
        )

    def test_bootstrap_persists_clean_skips_and_force_is_durable(self):
        with self.state.lock():
            self.assertTrue(self.state.ensure(self.dataset))
            path = self.record_path()
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["dataset"], maintenance.dataset_key(self.dataset))
            self.assertEqual(record["version"], 1)
            self.assertEqual(record["reason"], "bootstrap")
            self.assertTrue(record["timestamp"])
            self.state.clear(self.dataset)
            before = path.read_bytes()
            self.assertFalse(self.state.is_pending(self.dataset))
            self.assertFalse(self.state.ensure(self.dataset))
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(self.state.ensure(self.dataset, force=True))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["reason"], "forced")
        with maintenance.MaintenanceState(self.directory).lock() as restarted:
            self.assertTrue(restarted.is_pending(self.dataset))

    def test_crash_after_mark_pending_survives_process_exit(self):
        with self.state.lock():
            self.state.ensure(self.dataset)
            self.state.clear(self.dataset)
        child = self.run_child(
            "import os, sys; from mosaic_maintenance_state import MaintenanceState\n"
            "s = MaintenanceState(sys.argv[1])\n"
            "with s.lock():\n"
            "    s.mark_pending(sys.argv[2], 'before mutation')\n"
            "    os._exit(17)\n",
            self.dataset,
        )
        self.assertEqual(child.returncode, 17, child.stderr)
        with maintenance.MaintenanceState(self.directory).lock() as restarted:
            self.assertTrue(restarted.is_pending(self.dataset))
            record = json.loads(self.record_path().read_text(encoding="utf-8"))
            self.assertEqual(record["reason"], "before mutation")

    def test_datasets_are_isolated_and_paths_normalized(self):
        other = self.root / "production.gdb" / "Other"
        alternate = str(self.root / "production.gdb" / "." / "MOSAIC")
        with self.state.lock():
            self.assertTrue(self.state.ensure(self.dataset))
            self.assertTrue(self.state.ensure(other))
            self.state.clear(self.dataset)
            self.assertFalse(self.state.is_pending(alternate))
            self.assertTrue(self.state.is_pending(other))
            self.state.mark_pending(alternate, "catalog add")
            self.state.clear(other)
            self.assertTrue(self.state.is_pending(self.dataset))
            self.assertFalse(self.state.is_pending(other))
            self.assertNotEqual(self.record_path(), self.record_path(other))

    def test_corrupt_state_never_overwritten_by_any_operation(self):
        with self.state.lock():
            self.state.ensure(self.dataset)
            path = self.record_path()
            valid = json.loads(path.read_text(encoding="utf-8"))
            invalid_records = [
                "{", "[]", '{"version":1,"version":1}',
                json.dumps(dict(valid, version=2)),
                json.dumps(dict(valid, version=True)),
                json.dumps(dict(valid, dataset="another dataset")),
                json.dumps(dict(valid, pending="false")),
                json.dumps(dict(valid, reason="")),
                json.dumps(dict(valid, timestamp="not a timestamp")),
                json.dumps(dict(valid, timestamp="2026-01-01T00:00:00")),
            ]
            for content in invalid_records:
                for operation in (
                    lambda: self.state.ensure(self.dataset),
                    lambda: self.state.ensure(self.dataset, force=True),
                    lambda: self.state.is_pending(self.dataset),
                    lambda: self.state.mark_pending(self.dataset, "change"),
                    lambda: self.state.clear(self.dataset),
                ):
                    with self.subTest(content=content, operation=operation):
                        path.write_text(content, encoding="utf-8")
                        before = path.read_bytes()
                        with self.assertRaises(maintenance.StateError):
                            operation()
                        self.assertEqual(path.read_bytes(), before)

    def test_replace_failure_preserves_existing_record_and_cleans_temp(self):
        with self.state.lock():
            self.state.ensure(self.dataset)
            self.state.clear(self.dataset)
            before = self.record_path().read_bytes()
            with mock.patch.object(maintenance.os, "replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    self.state.mark_pending(self.dataset, "mutation")
            self.assertEqual(self.record_path().read_bytes(), before)
            self.assertEqual(list(self.directory.glob(".maintenance-*.tmp")), [])
            self.assertFalse(self.state.is_pending(self.dataset))

    def test_fsync_failure_cleans_temp_and_does_not_create_record(self):
        with self.state.lock():
            with mock.patch.object(maintenance.os, "fsync", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    self.state.ensure(self.dataset)
            self.assertFalse(self.record_path().exists())
            self.assertEqual(list(self.directory.glob(".maintenance-*.tmp")), [])

    def test_lock_excludes_another_process_and_lockfile_persists(self):
        code = (
            "import sys; from mosaic_maintenance_state import MaintenanceState, LockError\n"
            "try:\n"
            "    with MaintenanceState(sys.argv[1]).lock():\n"
            "        pass\n"
            "except LockError:\n"
            "    sys.exit(23)\n"
        )
        with self.state.lock():
            result = self.run_child(code)
            self.assertEqual(result.returncode, 23, result.stderr)
            with self.assertRaises(maintenance.LockError):
                with self.state.lock():
                    pass
        lockfile = self.directory / "maintenance.lock"
        self.assertTrue(lockfile.exists())
        result = self.run_child(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(lockfile.exists())

    def test_lock_is_required_and_missing_state_cannot_be_cleared(self):
        for operation in (lambda: self.state.ensure(self.dataset),
                          lambda: self.state.is_pending(self.dataset),
                          lambda: self.state.mark_pending(self.dataset, "change"),
                          lambda: self.state.clear(self.dataset)):
            with self.assertRaises(maintenance.LockError):
                operation()
        with self.state.lock():
            with self.assertRaises(maintenance.StateError):
                self.state.clear(self.dataset)
            self.assertFalse(self.record_path().exists())
            self.assertTrue(self.state.is_pending(self.dataset))

    def test_configuration_is_strict_and_does_not_touch_disk(self):
        report_root = self.root / "reports"
        override = self.root / "override"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(maintenance.state_directory(report_root), report_root / "maintenance_state")
            self.assertFalse(maintenance.force_maintenance())
        with mock.patch.dict(os.environ, {"UAV_REPORT_DIR": str(report_root)}, clear=True):
            self.assertEqual(maintenance.state_directory(), report_root / "maintenance_state")
        with mock.patch.dict(os.environ, {"UAV_MAINTENANCE_STATE_DIR": str(override)}, clear=True):
            self.assertEqual(maintenance.state_directory(report_root), override)
        for value in ("true", "1", "yes", " TRUE "):
            with mock.patch.dict(os.environ, {"UAV_FORCE_MAINTENANCE": value}):
                self.assertTrue(maintenance.force_maintenance())
        for value in ("false", "0", "no", " NO "):
            with mock.patch.dict(os.environ, {"UAV_FORCE_MAINTENANCE": value}):
                self.assertFalse(maintenance.force_maintenance())
        for value in ("", "on", "truth"):
            with mock.patch.dict(os.environ, {"UAV_FORCE_MAINTENANCE": value}):
                with self.assertRaises(ValueError):
                    maintenance.force_maintenance()
        with mock.patch.dict(os.environ, {"UAV_MAINTENANCE_STATE_DIR": "  "}):
            with self.assertRaises(ValueError):
                maintenance.state_directory(report_root)
        self.assertFalse(report_root.exists())
        self.assertFalse(override.exists())


if __name__ == "__main__":
    unittest.main()