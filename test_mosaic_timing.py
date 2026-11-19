"""Local-only tests for mosaic timing diagnostics; ArcPy calls are mocked."""

import csv
import importlib.util
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parent


class MosaicTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uav_timing_")
        self.addCleanup(self.tmp.cleanup)

    def load_mosaic(self, filename):
        arcpy = types.ModuleType("arcpy")
        arcpy.env = types.SimpleNamespace(overwriteOutput=False)
        arcpy.management = types.SimpleNamespace()
        arcpy.GetMessages = lambda severity: "mock ArcGIS error"
        spec = importlib.util.spec_from_file_location("test_mosaic_module", ROOT / filename)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"UAV_REPORT_DIR": self.tmp.name}), \
                mock.patch.dict("sys.modules", {"arcpy": arcpy}):
            spec.loader.exec_module(module)
        self.addCleanup(module.run_report.close)
        return module, arcpy

    def events(self, module):
        with module.run_report.path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_lidar_maintenance_records_each_operation_and_arguments(self):
        module, arcpy = self.load_mosaic("2_UAV_LiDAR_to_Mosaic_nightly.py")
        calls = []
        def capture(name):
            def operation(**kwargs):
                calls.append((name, kwargs))
                current = self.events(module)[-1]
                self.assertEqual((current["stage"], current["status"]), (name, "started"))
                self.assertEqual(current["output_path"], "production/test_mosaic")
            return operation
        arcpy.management.CalculateCellSizeRanges = capture("calculate_cell_size_ranges")
        arcpy.management.BuildOverviews = capture("build_overviews")
        arcpy.management.CalculateStatistics = capture("calculate_statistics")

        self.assertTrue(module.build_production_overviews("production/test_mosaic"))
        self.assertEqual([name for name, _ in calls],
                         ["calculate_cell_size_ranges", "build_overviews", "calculate_statistics"])
        self.assertEqual(calls[0][1]["where_clause"],
                         "Category = 1 AND (MinPS IS NULL OR MaxPS IS NULL OR (MinPS = 0 AND MaxPS = 0))")
        self.assertEqual(calls[1][1]["regenerate_stale_images"], "REGENERATE_STALE_IMAGES")
        self.assertEqual(calls[2][1]["x_skip_factor"], 10)
        self.assertEqual(calls[2][1]["ignore_values"], [-9999])
        events = self.events(module)
        for stage, _ in calls:
            stage_events = [event for event in events if event["stage"] == stage]
            self.assertEqual([event["status"] for event in stage_events], ["started", "completed"])
            self.assertIn("elapsed_seconds=", stage_events[-1]["details"])
        self.assertEqual(module.run_report.issue_count, 0)

    def test_ortho_overview_error_is_timed_and_counted_once(self):
        module, arcpy = self.load_mosaic("2_UAV_ortho_to_mosaic_nightly.py")
        def fail(**kwargs):
            last = self.events(module)[-1]
            self.assertEqual((last["stage"], last["status"]),
                             ("build_production_overviews_timing", "started"))
            raise RuntimeError("synthetic overview failure")
        arcpy.management.BuildOverviews = fail
        self.assertFalse(module.build_production_overviews("production/test_mosaic"))
        events = self.events(module)
        timing = [event for event in events if event["stage"] == "build_production_overviews_timing"]
        self.assertEqual([event["status"] for event in timing], ["started", "info"])
        self.assertIn("synthetic overview failure", timing[-1]["details"])
        self.assertIn("elapsed_seconds=", timing[-1]["details"])
        self.assertEqual(module.run_report.issue_count, 1)

    def test_lidar_statistics_error_is_timed_and_counted_once(self):
        module, arcpy = self.load_mosaic("2_UAV_LiDAR_to_Mosaic_nightly.py")
        arcpy.management.CalculateCellSizeRanges = lambda **kwargs: None
        arcpy.management.BuildOverviews = lambda **kwargs: None
        def fail(**kwargs):
            last = self.events(module)[-1]
            self.assertEqual((last["stage"], last["status"]), ("calculate_statistics", "started"))
            raise RuntimeError("synthetic statistics failure")
        arcpy.management.CalculateStatistics = fail
        self.assertFalse(module.build_production_overviews("production/test_mosaic"))
        events = self.events(module)
        timing = [event for event in events if event["stage"] == "calculate_statistics"]
        self.assertEqual([event["status"] for event in timing], ["started", "info"])
        self.assertIn("synthetic statistics failure", timing[-1]["details"])
        self.assertIn("elapsed_seconds=", timing[-1]["details"])
        self.assertEqual(module.run_report.issue_count, 1)

    def test_start_is_flushed_while_operation_is_blocked(self):
        for filename, helper_name in (("2_UAV_LiDAR_to_Mosaic_nightly.py", "timed_operation"),
                                      ("2_UAV_ortho_to_mosaic_nightly.py", "timed_stage")):
            with self.subTest(filename=filename):
                module, _ = self.load_mosaic(filename)
                entered, release = threading.Event(), threading.Event()
                errors = []
                def worker():
                    try:
                        with getattr(module, helper_name)("synthetic_block", output_path="production/test"):
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError("test release timed out")
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=worker)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    events = [event for event in self.events(module) if event["stage"] == "synthetic_block"]
                    self.assertEqual([event["status"] for event in events], ["started"])
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                events = [event for event in self.events(module) if event["stage"] == "synthetic_block"]
                self.assertEqual([event["status"] for event in events], ["started", "completed"])


if __name__ == "__main__":
    unittest.main()