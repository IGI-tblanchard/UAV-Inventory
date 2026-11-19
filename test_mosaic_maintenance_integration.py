"""Local integration tests: no real ArcPy, network shares, or production data."""

import csv
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

import mosaic_maintenance_state as maintenance


ROOT = Path(__file__).parent
SCRIPTS = (
    ("lidar", "2_UAV_LiDAR_to_Mosaic_nightly.py"),
    ("ortho", "2_UAV_ortho_to_mosaic_nightly.py"),
)


class FakeReport:
    """Avoid RunReport's startup probe of the real network share."""

    def __init__(self, _script):
        self.path = "mock-report"
        self.issue_count = 0
        self.events = []

    def record(self, stage, status, **context):
        self.events.append((stage, status, context))
        if status in {"failed", "warning"}:
            self.issue_count += 1

    def exception(self, stage, error, **context):
        self.record(stage, "failed", message=str(error), **context)

    def close(self):
        pass


class FakeArcPy(types.ModuleType):
    """In-memory catalog; every accepted dataset is under the test directory."""

    def __init__(self, root, production, staging, state_dir):
        super().__init__("arcpy")
        self.env = types.SimpleNamespace()
        self.root = root
        self.production = str(production)
        self.staging = str(staging)
        self.state_dir = state_dir
        self.datasets = {self.production: [], self.staging: []}
        self.tables = {}
        self.events = []
        self.add_failure = False
        self.omit_production_verification = False
        self.maintenance_failure = False
        self.probe_lock = False
        self.management = types.SimpleNamespace(
            GetCount=self.get_count,
            ExportMosaicDatasetPaths=self.export_paths,
            Delete=self.delete,
            AddRastersToMosaicDataset=self.add,
            CalculateCellSizeRanges=lambda **kw: self.maintain("cell_sizes", **kw),
            BuildOverviews=lambda **kw: self.maintain("overviews", **kw),
            CalculateStatistics=lambda **kw: self.maintain("statistics", **kw),
        )
        self.da = types.SimpleNamespace(SearchCursor=self.search_cursor)

    def state_record(self, dataset):
        key = maintenance.dataset_key(dataset)
        path = self.state_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def Exists(self, path):
        path = str(path)
        self.check_lock()
        allowed_gdbs = {str(Path(self.production).parent), str(Path(self.staging).parent)}
        catalogs = {str(Path(p).parent / ("AMD_" + Path(p).name + "_CAT")) for p in self.datasets}
        if path not in allowed_gdbs | set(self.datasets) | catalogs | set(self.tables):
            raise AssertionError("Unexpected ArcPy path: " + path)
        return True

    def GetMessages(self, _severity):
        return "synthetic ArcPy failure"

    def get_count(self, dataset):
        count = len(self.datasets[str(dataset)])
        return types.SimpleNamespace(getOutput=lambda _index: str(count))

    def CreateUniqueName(self, name, workspace):
        self.assert_local(workspace)
        return str(Path(workspace) / (name + "_test_table"))

    def export_paths(self, in_mosaic_dataset, out_table, **_kwargs):
        paths = self.datasets[str(in_mosaic_dataset)]
        if str(in_mosaic_dataset) == self.production and self.omit_production_verification:
            paths = []
        self.tables[out_table] = list(enumerate(paths, 1))

    def ListFields(self, path):
        # Only exported path tables expose the two fields; the catalog has no
        # editable Path fields, so no real cursor or geodatabase is required.
        return [types.SimpleNamespace(name=name) for name in ("SourceOID", "Path")] if path in self.tables else []

    @contextmanager
    def search_cursor(self, table, _fields):
        yield iter(self.tables[table])

    def delete(self, table):
        del self.tables[table]

    def assert_local(self, path):
        if not Path(path).is_relative_to(self.root):
            raise AssertionError("Non-temporary path: " + str(path))

    def check_lock(self):
        if self.probe_lock:
            with unittest.TestCase().assertRaises(maintenance.LockError):
                with maintenance.MaintenanceState(self.state_dir).lock():
                    pass

    def add(self, in_mosaic_dataset, input_path, **_kwargs):
        dataset = str(in_mosaic_dataset)
        self.assert_local(dataset)
        self.assert_local(input_path)
        self.check_lock()
        self.events.append(("add", dataset, self.state_record(dataset)))
        if dataset == self.production and self.add_failure:
            raise RuntimeError("synthetic add failure")
        self.datasets[dataset].append(input_path)

    def maintain(self, stage, **kwargs):
        dataset = str(kwargs.get("in_mosaic_dataset", kwargs.get("in_raster_dataset")))
        if dataset != self.production:
            raise AssertionError("Maintenance attempted on non-production dataset")
        self.check_lock()
        self.events.append((stage, dataset, self.state_record(dataset)))
        if self.maintenance_failure and stage == "overviews":
            raise RuntimeError("synthetic overview failure")


@contextmanager
def workflow(kind, filename, candidate=False):
    with tempfile.TemporaryDirectory(prefix="uav_mosaic_integration_") as temporary:
        root = Path(temporary)
        base = root / "inputs"
        base.mkdir()
        state_dir = root / "state"
        staging_gdb = root / "staging.gdb"
        production_gdb = root / "production.gdb"
        name = "Client_DSM" if kind == "lidar" else "Client_Orthomosaic"
        staging = staging_gdb / name
        production = production_gdb / name
        source_root = (base / "Client" / "LiDAR" / "UAV" / "FullFeature"
                       if kind == "lidar" else base / "Client" / "Imagery" / "UAV")
        source_root.mkdir(parents=True)
        if candidate:
            job = source_root / "job1"
            job.mkdir()
            (job / ("01-DSM.tif" if kind == "lidar" else "01-Mosaic.tif")).write_bytes(b"fake")

        arcpy = FakeArcPy(root, production, staging, state_dir)
        fake_report_module = types.ModuleType("task_run_report")
        fake_report_module.RunReport = FakeReport
        env = {"UAV_REPORT_DIR": str(root), "UAV_MAINTENANCE_STATE_DIR": str(state_dir),
               "UAV_FORCE_MAINTENANCE": "false"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {
                "arcpy": arcpy, "task_run_report": fake_report_module}), redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location("integration_" + kind, ROOT / filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.BASE_DIR = base
            module.STAGING_GDB = staging_gdb
            module.PRODUCTION_GDB = production_gdb
            module.REPORT_CSV = root / "results.csv"
            module.CLIENT_FOLDERS = {"TST": "Client"}
            module.MOSAIC_SCOPE = {("Client", name)}
            if kind == "lidar":
                module.TYPE_MAP = {"FullFeature": "DSM"}
            store = maintenance.MaintenanceState(state_dir)
            yield types.SimpleNamespace(module=module, arcpy=arcpy, store=store,
                                        production=str(production), staging=str(staging),
                                        root=root, state_dir=state_dir)


class MosaicMaintenanceIntegrationTests(unittest.TestCase):
    def cases(self, candidate=False):
        for kind, filename in SCRIPTS:
            with self.subTest(script=kind), workflow(kind, filename, candidate) as case:
                yield case

    def make_clean(self, case):
        with case.store.lock():
            case.store.ensure(case.production)
            case.store.clear(case.production)

    def run_locked(self, case, force=False):
        with case.store.lock():
            return case.module.run_locked(case.store, force)

    def record(self, case):
        return case.arcpy.state_record(case.production)

    def rows(self, case):
        with case.module.REPORT_CSV.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_clean_skips_maintenance(self):
        for case in self.cases():
            self.make_clean(case)
            before = self.record(case)
            self.assertEqual(self.run_locked(case), 0)
            self.assertEqual(case.arcpy.events, [])
            self.assertEqual(self.record(case), before)

    def test_existing_candidates_do_not_dirty_clean_production(self):
        for case in self.cases(candidate=True):
            self.make_clean(case)
            candidate = next(case.module.BASE_DIR.rglob("*.tif"))
            case.arcpy.datasets[case.staging].append(str(candidate))
            case.arcpy.datasets[case.production].append(str(candidate))
            before = self.record(case)
            self.assertEqual(self.run_locked(case), 0)
            self.assertEqual(case.arcpy.events, [])
            self.assertEqual(self.record(case), before)
            self.assertEqual([row["status"] for row in self.rows(case)],
                             ["already_present", "already_present"])

    def test_missing_state_bootstraps_pending_then_clears(self):
        for case in self.cases():
            self.assertIsNone(self.record(case))
            self.assertEqual(self.run_locked(case), 0)
            events = case.arcpy.events
            self.assertIn("overviews", [event[0] for event in events])
            self.assertTrue(all(event[2]["pending"] and event[2]["reason"] == "bootstrap" for event in events))
            self.assertFalse(self.record(case)["pending"])

    def test_pending_retry_runs_with_no_candidates(self):
        for case in self.cases():
            self.make_clean(case)
            with case.store.lock():
                case.store.mark_pending(case.production, "previous failure")
            self.assertEqual(self.run_locked(case), 0)
            self.assertEqual([e[0] for e in case.arcpy.events].count("overviews"), 1)
            self.assertEqual(case.arcpy.events[0][2]["reason"], "previous failure")
            self.assertFalse(self.record(case)["pending"])
            self.assertEqual(self.rows(case), [])

    def test_force_transitions_clean_to_pending_before_maintenance(self):
        for case in self.cases():
            self.make_clean(case)
            self.assertEqual(self.run_locked(case, force=True), 0)
            self.assertTrue(case.arcpy.events)
            self.assertTrue(all(e[2]["pending"] and e[2]["reason"] == "forced" for e in case.arcpy.events))
            self.assertFalse(self.record(case)["pending"])

    def test_add_marks_pending_before_mutation_staging_has_no_state(self):
        for case in self.cases(candidate=True):
            self.make_clean(case)
            self.assertEqual(self.run_locked(case), 0)
            adds = [event for event in case.arcpy.events if event[0] == "add"]
            self.assertEqual([event[1] for event in adds], [case.staging, case.production])
            self.assertIsNone(adds[0][2])
            self.assertTrue(adds[1][2]["pending"])
            self.assertIn("add attempted", adds[1][2]["reason"])
            self.assertIsNone(case.arcpy.state_record(case.staging))
            self.assertFalse(self.record(case)["pending"])
            self.assertEqual([row["status"] for row in self.rows(case)], ["added", "added"])
            self.assertEqual(len(list(case.state_dir.glob("*.json"))), 1)

    def test_persistence_failure_prevents_production_add(self):
        for case in self.cases(candidate=True):
            self.make_clean(case)
            with mock.patch.object(maintenance.os, "replace", side_effect=OSError("synthetic disk failure")):
                with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                    self.run_locked(case)
            self.assertEqual([e[1] for e in case.arcpy.events if e[0] == "add"], [case.staging])
            self.assertEqual(case.arcpy.datasets[case.production], [])
            self.assertFalse(self.record(case)["pending"])
            self.assertIsNone(case.arcpy.state_record(case.staging))
            self.assertEqual(list(case.state_dir.glob(".maintenance-*.tmp")), [])

    def test_failed_add_or_missing_verification_remains_pending(self):
        for failure in ("add", "verification"):
            for case in self.cases(candidate=True):
                with self.subTest(failure=failure):
                    self.make_clean(case)
                    case.arcpy.add_failure = failure == "add"
                    case.arcpy.omit_production_verification = failure == "verification"
                    self.assertEqual(self.run_locked(case), 1)
                    self.assertTrue(self.record(case)["pending"])
                    self.assertIn("overviews", [e[0] for e in case.arcpy.events])
                    production_rows = [r for r in self.rows(case) if r["mosaic_dataset"] == case.production]
                    self.assertEqual(production_rows[0]["status"],
                                     "failed" if failure == "add" else "verification_failed")

    def test_failed_maintenance_remains_pending(self):
        for case in self.cases():
            case.arcpy.maintenance_failure = True
            self.assertEqual(self.run_locked(case), 1)
            self.assertTrue(self.record(case)["pending"])
            self.assertEqual([e[0] for e in case.arcpy.events].count("overviews"), 1)

    def test_discovery_uncertainty_keeps_bootstrap_pending(self):
        for case in self.cases():
            source_root = (case.module.BASE_DIR / "Client" / "LiDAR" / "UAV" / "FullFeature"
                           if "lidar" in case.module.__name__ else
                           case.module.BASE_DIR / "Client" / "Imagery" / "UAV")
            source_root.rmdir()
            self.assertEqual(self.run_locked(case), 1)
            self.assertTrue(self.record(case)["pending"])
            self.assertIn("overviews", [e[0] for e in case.arcpy.events])

    def test_discovery_uncertainty_marks_previously_clean_mosaic_pending(self):
        for case in self.cases():
            self.make_clean(case)
            source_root = (case.module.BASE_DIR / "Client" / "LiDAR" / "UAV" / "FullFeature"
                           if "lidar" in case.module.__name__ else
                           case.module.BASE_DIR / "Client" / "Imagery" / "UAV")
            source_root.rmdir()
            self.assertEqual(self.run_locked(case), 1)
            self.assertTrue(self.record(case)["pending"])
            self.assertEqual(self.record(case)["reason"], "production input discovery uncertain")

    def test_main_wrapper_holds_lock_and_honors_force_environment(self):
        for case in self.cases(candidate=True):
            self.make_clean(case)
            case.arcpy.probe_lock = True
            with mock.patch.dict(os.environ, {"UAV_FORCE_MAINTENANCE": "true"}):
                self.assertEqual(case.module.main(), 0)
            adds = [e for e in case.arcpy.events if e[0] == "add"]
            self.assertEqual(adds[0][2], None)
            self.assertEqual(adds[1][2]["pending"], True)
            self.assertFalse(self.record(case)["pending"])
            with maintenance.MaintenanceState(case.state_dir).lock():
                pass

    def test_main_wrapper_refuses_existing_lock_before_any_work(self):
        for case in self.cases(candidate=True):
            with case.store.lock():
                with self.assertRaises(maintenance.LockError):
                    case.module.main()
            self.assertEqual(case.arcpy.events, [])
            self.assertIsNone(self.record(case))
            self.assertFalse(case.module.REPORT_CSV.exists())


if __name__ == "__main__":
    unittest.main()