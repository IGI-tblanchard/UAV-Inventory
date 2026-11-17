"""Local synthetic GDAL tests; never access or modify production imagery."""

import csv
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from osgeo import gdal, osr


with tempfile.TemporaryDirectory(prefix="producer_import_") as import_reports:
    with mock.patch.dict(os.environ, {"UAV_REPORT_DIR": import_reports}):
        spec = importlib.util.spec_from_file_location(
            "ortho_producer", Path(__file__).with_name("1_reproject_UAV_ortho_nightly.py"))
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        producer.run_report.close()


class MemoryReport:
    def __init__(self):
        self.issue_count = 0
        self.events = []

    def record(self, stage, status, **kwargs):
        self.events.append((stage, status, kwargs))
        self.issue_count += status in {"failed", "warning"}

    def exception(self, stage, error, **kwargs):
        self.record(stage, "failed", message=str(error), **kwargs)


def make_raster(path, count=4, crs=2955, rgba=True):
    options = (["PHOTOMETRIC=RGB", "ALPHA=YES"] if rgba else ["PHOTOMETRIC=MINISBLACK"]) if count == 4 else []
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 8, 8, count, gdal.GDT_Byte, options=options)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(crs)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
    for i in range(1, count + 1):
        ds.GetRasterBand(i).Fill(i * 30)
    ds = None


class ProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ortho_producer_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / "clients" / "Cenovus" / "Imagery" / "UAV" / "12345"
        self.ortho = self.job / "Orthomosaic"
        self.ortho.mkdir(parents=True)
        self.queue = self.root / "queue.json"
        self.report = self.root / "report.csv"
        self.memory_report = MemoryReport()
        patches = [mock.patch.object(producer, "BASE_DIR", self.root / "clients"),
                   mock.patch.object(producer, "REPORT_CSV", self.report),
                   mock.patch.object(producer, "run_report", self.memory_report),
                   mock.patch.dict(os.environ, {"UAV_REPAIR_QUEUE": str(self.queue),
                                               "UAV_REPAIR_RUN_ID": "test-run"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def rows(self):
        with self.report.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_mixed_sources_collision_sidecar_and_real_warp(self):
        rgb = self.ortho / "01_02_2025_ortho.tif"
        gray = self.ortho / "01-02-2025-mosaic.tif"
        rgba = self.ortho / "02_02_2025_ortho.tif"
        make_raster(rgb, 3)
        make_raster(gray, 1)
        make_raster(rgba)
        sidecar = Path(str(rgb) + ".aux.xml")
        sidecar.write_text('<PAMDataset><Metadata><MDI key="TEST">kept</MDI></Metadata></PAMDataset>')
        self.assertEqual(producer.main(), 0)
        queue = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual((queue["version"], queue["run_id"], queue["complete"]), (1, "test-run", True))
        self.assertEqual(len(queue["items"]), 2)
        outputs = set()
        for item in queue["items"]:
            source, output = Path(item["source_path"]), Path(item["output_path"])
            self.assertTrue(source.is_absolute() and output.is_absolute() and source.exists())
            self.assertEqual(source.name, output.name)
            self.assertEqual(item["client"], "CVE")
            self.assertEqual(item["job_number"], "12345")
            self.assertTrue(item["reason"])
            outputs.add(output.stem.casefold())
        self.assertEqual(sum(Path(item["source_path"] + ".aux.xml").exists()
                             for item in queue["items"]), 1)
        self.assertEqual(len(outputs), 2)
        self.assertTrue(any("part2" in name for name in outputs))
        self.assertEqual(sorted(row["status"] for row in self.rows()),
                         ["completed", "queued_for_repair", "queued_for_repair"])
        self.assertTrue(producer.is_valid_existing_output(self.job / "02-02-2025-Mosaic.tif"))

    def test_reduced_source_is_renamed_but_not_reprojected_or_queued(self):
        source = self.ortho / "01_02_2025_ortho_reduced.tif"
        make_raster(source, 1)
        sidecar = Path(str(source) + ".aux.xml")
        sidecar.write_text("<PAMDataset />")
        expected_source = self.ortho / "01-02-2025-Mosaic-Reduced.tif"
        expected_sidecar = Path(str(expected_source) + ".aux.xml")
        output = self.job / expected_source.name
        make_raster(output, 1)
        original_output = output.read_bytes()

        with mock.patch.object(producer, "create_cog_without_overwrite") as create_cog, \
                mock.patch.object(producer, "existing_output_problem", side_effect=AssertionError("Reduced output must not be validated")) as validate_output:
            self.assertEqual(producer.main(), 0)

        self.assertTrue(expected_source.exists())
        self.assertTrue(expected_sidecar.exists())
        self.assertFalse(source.exists())
        self.assertFalse(sidecar.exists())
        self.assertEqual(output.read_bytes(), original_output)
        create_cog.assert_not_called()
        validate_output.assert_not_called()
        self.assertEqual(json.loads(self.queue.read_text())["items"], [])
        self.assertEqual(self.rows()[0]["status"], "skipped_reduced")
        self.assertEqual(self.memory_report.issue_count, 0)
        self.assertEqual(self.memory_report.events[-1][1], "completed")

    def test_existing_valid_output_precedes_invalid_source_name(self):
        source = self.ortho / "01-02-2025-Mosaic-very-long-unknown-qualifier.tif"
        output = self.job / source.name
        make_raster(source, 1)
        make_raster(output)
        original_source = source.read_bytes()
        original_output = output.read_bytes()

        with mock.patch.object(producer, "existing_output_problem", wraps=producer.existing_output_problem) as check_output:
            self.assertEqual(producer.main(), 0)

        self.assertEqual(check_output.call_count, 1)
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(output.read_bytes(), original_output)
        self.assertEqual(self.rows()[0]["status"], "skipped_already_exists")
        self.assertEqual(self.memory_report.issue_count, 0)

    def test_invalid_same_name_reduced_output_is_an_informational_skip(self):
        source = self.ortho / "01-02-2025-Mosaic-Reduced.tif"
        output = self.job / source.name
        make_raster(source, 1)
        make_raster(output, 1)
        original_source = source.read_bytes()
        original_output = output.read_bytes()

        self.assertEqual(producer.main(), 0)

        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(output.read_bytes(), original_output)
        self.assertEqual(self.rows()[0]["status"], "skipped_reduced")
        self.assertEqual(self.memory_report.issue_count, 0)
        self.assertEqual(self.memory_report.events[-1][1], "completed")

    def test_resolution_named_reduced_source_is_ignored_before_reprojection(self):
        source = self.ortho / "01_02_2025_ortho_0.1m.tif"
        make_raster(source, 1)
        expected_source = self.ortho / "01-02-2025-Mosaic-Reduced.tif"
        output = self.job / expected_source.name

        with mock.patch.object(producer, "create_cog_without_overwrite") as create_cog:
            self.assertEqual(producer.main(), 0)

        self.assertTrue(expected_source.exists())
        self.assertFalse(source.exists())
        self.assertFalse(output.exists())
        create_cog.assert_not_called()
        self.assertEqual(json.loads(self.queue.read_text())["items"], [])
        self.assertEqual(self.rows()[0]["status"], "skipped_reduced")
        self.assertEqual(self.memory_report.issue_count, 0)

    def test_existing_outputs_valid_and_invalid_individually(self):
        valid = self.ortho / "01-01-2025-Mosaic.tif"
        invalid = self.ortho / "02-01-2025-Mosaic.tif"
        wrong_crs = self.ortho / "03-01-2025-Mosaic.tif"
        for p in (valid, invalid, wrong_crs):
            make_raster(p, 3)
        make_raster(self.job / valid.name)
        make_raster(self.job / invalid.name, 3)
        (self.job / "02-01-2025-Mosaic.tiff").write_bytes(b"not a tiff")
        make_raster(self.job / wrong_crs.name, crs=32611)
        before = {p: p.read_bytes() for p in self.job.iterdir() if p.is_file()}
        self.assertEqual(producer.main(), 1)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        rows = self.rows()
        self.assertEqual(sum(r["status"] == "skipped_invalid_same_name_output" for r in rows), 3)
        self.assertEqual(sum(r["status"] == "skipped_already_exists" for r in rows), 1)
        self.assertEqual(json.loads(self.queue.read_text())["items"], [])
        self.assertTrue(all(p.exists() for p in (valid, invalid, wrong_crs)))
        self.assertTrue(any("EPSG:2955" in r["message"] for r in rows))

    def test_unreadable_source_fails_but_other_source_is_queued(self):
        (self.ortho / "01-01-2025-Mosaic.tif").write_bytes(b"bad")
        make_raster(self.ortho / "02-01-2025-Mosaic.tif", 3)
        self.assertEqual(producer.main(), 1)
        self.assertEqual(sorted(r["status"] for r in self.rows()), ["failed", "queued_for_repair"])
        self.assertEqual(len(json.loads(self.queue.read_text())["items"]), 1)

    def test_four_bands_without_rgba_roles_are_queued(self):
        source = self.ortho / "01-01-2025-Mosaic.tif"
        make_raster(source, 4, rgba=False)
        ds = gdal.Open(str(source))
        self.assertTrue(producer.rgba_problem(ds))
        ds = None
        self.assertEqual(producer.main(), 0)
        self.assertEqual(len(json.loads(self.queue.read_text())["items"]), 1)

    def test_standalone_leaves_repair_source_unchanged_and_fails(self):
        source = self.ortho / "01_01_2025_ortho.tif"
        make_raster(source, 3)
        with mock.patch.dict(os.environ, {"UAV_REPAIR_QUEUE": "", "UAV_REPAIR_RUN_ID": ""}):
            self.assertEqual(producer.main(), 1)
        self.assertTrue(source.exists())
        self.assertFalse(self.queue.exists())
        self.assertIn("UAV_REPAIR_QUEUE", self.rows()[0]["message"])

    def test_incomplete_configuration_removes_stale_queue(self):
        self.queue.write_text("stale")
        with mock.patch.dict(os.environ, {"UAV_REPAIR_RUN_ID": ""}):
            with self.assertRaisesRegex(ValueError, "both be set"):
                producer.main()
        self.assertFalse(self.queue.exists())

    def test_discovery_exception_removes_stale_queue(self):
        self.queue.write_text("stale")
        def partial_discovery():
            yield ("CVE", "12345", "Mosaic", self.ortho / "source.tif", self.job)
            raise PermissionError("discovery failed")
        with mock.patch.object(producer, "iter_source_tifs", partial_discovery):
            with self.assertRaises(PermissionError):
                producer.main()
        self.assertFalse(self.queue.exists())

    def test_publication_failure_removes_stale_queue_and_temp(self):
        self.queue.write_text("stale")
        make_raster(self.ortho / "01-01-2025-Mosaic.tif", 3)
        with mock.patch.object(producer.os, "replace", side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                producer.main()
        self.assertFalse(self.queue.exists())
        self.assertEqual(list(self.root.glob(".queue.json.*.tmp")), [])

    def test_queue_survives_report_or_reprojection_failure(self):
        make_raster(self.ortho / "01-01-2025-Mosaic.tif", 3)
        with mock.patch.object(producer, "REPORT_CSV", self.root / "missing" / "report.csv"):
            with self.assertRaises(OSError):
                producer.main()
        self.assertEqual(len(json.loads(self.queue.read_text())["items"]), 1)

    def test_queue_survives_reprojection_failure_and_consumer_can_read_it(self):
        make_raster(self.ortho / "01-01-2025-Mosaic.tif", 3)
        make_raster(self.ortho / "02-01-2025-Mosaic.tif")
        with mock.patch.object(producer, "create_cog_from_tif", return_value=False):
            self.assertEqual(producer.main(), 1)
        spec = importlib.util.spec_from_file_location(
            "repair_consumer", Path(__file__).with_name("1.1_Repair_Single_Band_Rasters.py"))
        consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(consumer)
        items = consumer.load_queue(self.queue, "test-run")
        self.assertEqual(len(items), 1)
        source, output = consumer.validate_item(items[0])
        self.assertTrue(source.exists())
        self.assertFalse(output.exists())

    def test_output_pixel_read_errors_are_invalid(self):
        output = self.job / "01-01-2025-Mosaic.tif"
        make_raster(output)
        ds = gdal.Open(str(output))
        class CorruptDataset:
            def __getattr__(self, name):
                return getattr(ds, name)

            def ReadRaster(self, *args):
                raise RuntimeError("corrupt tile")

        proxy = CorruptDataset()
        with mock.patch.object(producer.gdal, "Open", return_value=proxy):
            problem = producer.existing_output_problem(output)
        ds = None
        self.assertIn("corrupt tile", problem)

    def test_output_appearing_during_reprojection_is_not_overwritten(self):
        source = self.ortho / "01-01-2025-Mosaic.tif"
        output = self.job / source.name
        make_raster(source)

        def racing_warp(source_path, temp_path, target_crs):
            temp_path.write_bytes(b"new output")
            output.write_bytes(b"existing output")
            return True

        with mock.patch.object(producer, "create_cog_from_tif", side_effect=racing_warp), \
            mock.patch.object(producer, "existing_output_problem", return_value=""):
            self.assertEqual(producer.main(), 1)
        self.assertEqual(output.read_bytes(), b"existing output")
        self.assertEqual(list(self.job.glob(".__uav_ortho_*.tif")), [])
        self.assertEqual(self.rows()[0]["status"], "failed")

    def test_existing_output_sidecar_is_not_used_for_new_cog(self):
        source = self.ortho / "01-01-2025-Mosaic.tif"
        output = self.job / source.name
        sidecar = Path(str(output) + ".aux.xml")
        make_raster(source)
        sidecar.write_text('<PAMDataset><SRS>invalid</SRS></PAMDataset>')
        self.assertEqual(producer.main(), 1)
        self.assertFalse(output.exists())
        self.assertTrue(sidecar.exists())
        self.assertEqual(self.rows()[0]["status"], "skipped_name_collision")

    def test_indexed_queue_repair_and_next_run(self):
        import numpy as np
        source = self.ortho / "03_02_2025_ortho.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(source), 8, 8, 1, gdal.GDT_Byte)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32611)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
        band = ds.GetRasterBand(1)
        table = gdal.ColorTable()
        table.SetColorEntry(0, (0, 0, 0, 255))
        table.SetColorEntry(1, (20, 40, 60, 255))
        band.SetColorTable(table)
        band.WriteArray(np.ones((8, 8), dtype=np.uint8))
        band = None
        ds = None

        self.assertEqual(producer.main(), 0)
        spec = importlib.util.spec_from_file_location(
            "repair_integration", Path(__file__).with_name("1.1_Repair_Single_Band_Rasters.py"))
        consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(consumer)
        items = consumer.load_queue(self.queue, "test-run")
        self.assertEqual(len(items), 1)
        output = Path(items[0]["output_path"])
        self.assertEqual(output.name, "03-02-2025-Mosaic.tif")
        with mock.patch.dict(os.environ, {"UAV_REPORT_DIR": str(self.root)}):
            self.assertEqual(consumer.main(self.memory_report), 0)
        self.assertTrue(output.exists())
        consumer.validate_cog(output, gdal, osr)
        ds = gdal.Open(str(output))
        self.assertEqual(ds.ReadAsArray()[:, 3, 3].tolist(), [20, 40, 60, 255])
        ds = None
        self.assertEqual(producer.main(), 0)
        self.assertEqual(consumer.load_queue(self.queue, "test-run"), [])
        self.assertEqual(self.rows()[0]["status"], "skipped_already_exists")

    def test_run_id_is_not_trimmed(self):
        with mock.patch.dict(os.environ, {"UAV_REPAIR_RUN_ID": " run id "}):
            self.assertEqual(producer.main(), 0)
        self.assertEqual(json.loads(self.queue.read_text())["run_id"], " run id ")

    def test_pam_metadata_is_enabled(self):
        self.assertEqual(gdal.GetConfigOption("GDAL_PAM_ENABLED"), "YES")
        source = self.ortho / "01-01-2025-Mosaic.tif"
        make_raster(source, 1)
        Path(str(source) + ".aux.xml").write_text(
            '<PAMDataset><PAMRasterBand band="1"><NoDataValue>17</NoDataValue>'
            '</PAMRasterBand></PAMDataset>')
        ds = gdal.Open(str(source))
        self.assertEqual(ds.GetRasterBand(1).GetNoDataValue(), 17)
        ds = None


if __name__ == "__main__":
    unittest.main()