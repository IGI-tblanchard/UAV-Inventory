"""Synthetic, local-only tests for the repair consumer (no production writes)."""

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from osgeo import gdal, osr

SCRIPT = Path(__file__).with_name("1.1_Repair_Single_Band_Rasters.py")
spec = importlib.util.spec_from_file_location("repair_consumer", SCRIPT)
repair_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair_module)
gdal, osr, np = repair_module.configure_gdal()


def spatial_ref():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2955)
    return srs


def create_rgb(path):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 4, 3, 3, gdal.GDT_Byte,
                                               options=["PHOTOMETRIC=RGB"])
    ds.SetProjection(spatial_ref().ExportToWkt())
    ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
    pixels = np.array([[[0, 255, 17, 100], [40, 50, 60, 70], [1, 2, 3, 4]],
                       [[0, 255, 17, 110], [40, 50, 60, 70], [1, 2, 3, 4]],
                       [[0, 255, 17, 120], [40, 50, 60, 70], [1, 2, 3, 4]]], dtype=np.uint8)
    for i in range(3):
        band = ds.GetRasterBand(i + 1)
        band.WriteArray(pixels[i])
        band.SetNoDataValue(17)
    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "NO")
    try:
        ds.CreateMaskBand(gdal.GMF_PER_DATASET)
        ds.GetRasterBand(1).GetMaskBand().WriteArray(
            np.array([[255, 255, 255, 128], [0, 255, 255, 255], [255, 255, 255, 255]], dtype=np.uint8))
    finally:
        gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", None)
    ds = None
    return pixels


def create_palette_vrt(folder):
    indices = folder / "indices.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(indices), 4, 2, 1, gdal.GDT_Byte)
    ds.GetRasterBand(1).WriteArray(np.array([[0, 1, 2, 3], [3, 2, 1, 0]], dtype=np.uint8))
    ds = None
    mask = folder / "mask.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(mask), 4, 2, 1, gdal.GDT_Byte)
    ds.GetRasterBand(1).WriteArray(np.array([[255, 255, 255, 128], [0, 255, 255, 255]], dtype=np.uint8))
    ds = None
    source = folder / "palette.vrt"
    source.write_text(f'''<VRTDataset rasterXSize="4" rasterYSize="2">
<SRS>{escape(spatial_ref().ExportToWkt())}</SRS><GeoTransform>500000,1,0,5700000,0,-1</GeoTransform>
<VRTRasterBand dataType="Byte" band="1"><ColorInterp>Palette</ColorInterp><NoDataValue>2</NoDataValue>
<ColorTable><Entry c1="0" c2="0" c3="0" c4="255"/><Entry c1="255" c2="255" c3="255" c4="255"/>
<Entry c1="44" c2="45" c3="46" c4="255"/><Entry c1="10" c2="20" c3="30" c4="128"/></ColorTable>
<SimpleSource><SourceFilename relativeToVRT="1">indices.tif</SourceFilename><SourceBand>1</SourceBand></SimpleSource>
<MaskBand><VRTRasterBand dataType="Byte"><SimpleSource><SourceFilename relativeToVRT="1">mask.tif</SourceFilename>
<SourceBand>1</SourceBand></SimpleSource></VRTRasterBand></MaskBand></VRTRasterBand></VRTDataset>''', encoding="utf-8")
    return source


class RepairConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repair_tests_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_palette_alpha_mask_nodata_and_valid_black_white(self):
        source = create_palette_vrt(self.root)
        output = self.root / "out.tif"
        msg = repair_module.repair(source, output, gdal, osr, np)
        self.assertIn("resampling=near", msg)
        repair_module.validate_cog(output, gdal, osr)
        ds = gdal.Open(str(output))
        pixels = ds.ReadAsArray()
        self.assertEqual(pixels[:, 0, 0].tolist(), [0, 0, 0, 255])
        self.assertEqual(pixels[:, 0, 1].tolist(), [255, 255, 255, 255])
        self.assertEqual(int(pixels[3, 0, 2]), 0)
        self.assertEqual(int(pixels[3, 0, 3]), 64)
        self.assertEqual(int(pixels[3, 1, 0]), 0)
        ds = None

    def test_rgb_alpha_mask_nodata_and_valid_black_white(self):
        source, output = self.root / "rgb.tif", self.root / "out.tif"
        create_rgb(source)
        self.assertIn("resampling=bilinear", repair_module.repair(source, output, gdal, osr, np))
        ds = gdal.Open(str(output))
        pixels = ds.ReadAsArray()
        self.assertEqual(pixels[:, 0, 0].tolist(), [0, 0, 0, 255])
        self.assertEqual(pixels[:, 0, 1].tolist(), [255, 255, 255, 255])
        self.assertEqual(int(pixels[3, 0, 2]), 0)
        self.assertEqual(int(pixels[3, 0, 3]), 128)
        self.assertEqual(int(pixels[3, 1, 0]), 0)
        ds = None

    def test_pam_nodata_is_read_and_sources_unchanged(self):
        source, output = self.root / "rgb.tif", self.root / "out.tif"
        create_rgb(source)
        ds = gdal.Open(str(source), gdal.GA_Update)
        for i in range(1, 4):
            ds.GetRasterBand(i).DeleteNoDataValue()
        ds = None
        sidecar = Path(str(source) + ".aux.xml")
        sidecar.write_text('<PAMDataset>' + ''.join(
            f'<PAMRasterBand band="{i}"><NoDataValue>17</NoDataValue></PAMRasterBand>'
            for i in range(1, 4)) + '</PAMDataset>', encoding="utf-8")
        before = {p: p.read_bytes() for p in (source, sidecar, Path(str(source) + ".msk"))}
        ds = gdal.Open(str(source))
        self.assertEqual(ds.GetRasterBand(1).GetNoDataValue(), 17)
        ds = None
        repair_module.repair(source, output, gdal, osr, np)
        ds = gdal.Open(str(output))
        self.assertEqual(int(ds.GetRasterBand(4).ReadAsArray()[0, 2]), 0)
        ds = None
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_rgb_nodata_tuple_does_not_mask_valid_zero_channels(self):
        source, output = self.root / "rgb.tif", self.root / "out.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(source), 3, 1, 3, gdal.GDT_Byte,
                                                   options=["PHOTOMETRIC=RGB"])
        ds.SetProjection(spatial_ref().ExportToWkt())
        ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
        for i, values in enumerate(([0, 0, 255], [10, 0, 255], [20, 0, 255]), 1):
            band = ds.GetRasterBand(i)
            band.WriteArray(np.array([values], dtype=np.uint8))
            band.SetNoDataValue(0)
        band = None
        ds = None
        repair_module.repair(source, output, gdal, osr, np)
        ds = gdal.Open(str(output))
        pixels = ds.ReadAsArray()
        self.assertEqual(pixels[:, 0, 0].tolist(), [0, 10, 20, 255])
        self.assertEqual(int(pixels[3, 0, 1]), 0)
        self.assertEqual(pixels[:, 0, 2].tolist(), [255, 255, 255, 255])
        ds = None

    def test_reprojection_has_transparent_uncovered_corners(self):
        source, output = self.root / "rotated.tif", self.root / "out.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(source), 30, 30, 3, gdal.GDT_Byte,
                                                   options=["PHOTOMETRIC=RGB"])
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32611)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform((500000, 1, 0.4, 5700000, 0.4, -1))
        for i in range(1, 4):
            ds.GetRasterBand(i).Fill(80 * i)
        ds = None
        repair_module.repair(source, output, gdal, osr, np)
        ds = gdal.Open(str(output))
        alpha = ds.GetRasterBand(4).ReadAsArray()
        self.assertEqual(int(alpha[0, 0]), 0)
        self.assertEqual(int(alpha[-1, -1]), 0)
        self.assertGreater(int(alpha[alpha.shape[0] // 2, alpha.shape[1] // 2]), 0)
        ds = None

    def test_other_raster_layouts_rejected(self):
        for count, dtype in [(2, gdal.GDT_Byte), (4, gdal.GDT_Byte), (3, gdal.GDT_UInt16)]:
            with self.subTest(count=count, dtype=dtype):
                path = self.root / f"unsupported_{count}_{dtype}.tif"
                ds = gdal.GetDriverByName("GTiff").Create(str(path), 2, 2, count, dtype)
                ds.SetProjection(spatial_ref().ExportToWkt())
                ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
                with self.assertRaisesRegex(ValueError, "Unsupported"):
                    repair_module.classify_source(ds, gdal)
                ds = None

    def test_unsupported_grayscale_and_collision_are_errors(self):
        gray = self.root / "gray.tif"
        ds = gdal.GetDriverByName("GTiff").Create(str(gray), 3, 3, 1, gdal.GDT_Byte)
        ds.SetProjection(spatial_ref().ExportToWkt())
        ds.SetGeoTransform((500000, 1, 0, 5700000, 0, -1))
        ds = None
        out = self.root / "out.tif"
        with self.assertRaisesRegex(ValueError, "palette"):
            repair_module.repair(gray, out, gdal, osr, np)
        out.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            repair_module.repair(gray, out, gdal, osr, np)
        self.assertEqual(out.read_bytes(), b"do not overwrite")
        self.assertFalse(any(p.name.startswith(".uav_repair_") for p in self.root.iterdir()))

    def test_atomic_publish_refuses_existing_file(self):
        tmp, out = self.root / "temp.tif", self.root / "out.tif"
        tmp.write_bytes(b"new")
        out.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            repair_module.publish_no_replace(tmp, out)
        self.assertEqual(out.read_bytes(), b"old")
        self.assertEqual(tmp.read_bytes(), b"new")

    def test_output_sidecar_collision_is_refused(self):
        out = self.root / "out.tif"
        sidecar = Path(str(out) + ".aux.xml")
        sidecar.write_text("untouched", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            repair_module.ensure_free_output(out)
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "untouched")

    def test_queue_validation(self):
        path = self.root / "queue.json"
        queue = {"version": 1, "run_id": "abc", "complete": True, "items": [], "extra": 123}
        path.write_text(json.dumps(queue), encoding="utf-8")
        self.assertEqual(repair_module.load_queue(path, "abc"), [])
        for key, value in [("version", True), ("version", 2), ("run_id", "old"), ("complete", False), ("items", {})]:
            invalid = dict(queue, **{key: value})
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                repair_module.load_queue(path, "abc")

    def invoke(self, queue):
        path = self.root / "queue.json"
        path.write_text(json.dumps(queue), encoding="utf-8")
        reports = self.root / "reports"
        env = dict(os.environ, UAV_REPAIR_QUEUE=str(path), UAV_REPAIR_RUN_ID="run1",
                   UAV_REPORT_DIR=str(reports), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-B", str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=120)
        summaries = list(reports.glob("*_summary.csv"))
        self.assertEqual(len(summaries), 1, proc.stdout + proc.stderr)
        with summaries[0].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return proc, rows

    def test_run_all_records_and_duplicate_reservations(self):
        good = self.root / "good.tif"
        create_rgb(good)
        base = {"client": "CVE", "job_number": "123", "reason": "repair"}
        def item(source, output):
            return dict(base, source_path=str(source), output_path=str(output))
        duplicate = self.root / "duplicate.tif"
        good_out = self.root / "good_out.tif"
        queue = {"version": 1, "run_id": "run1", "complete": True, "items": [
            item(good, duplicate), item(good, duplicate), {"broken": True},
            item(self.root / "missing.tif", self.root / "missing_out.tif"), item(good, good_out)]}
        proc, rows = self.invoke(queue)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual([r["status"] for r in rows], ["failed"] * 4 + ["completed"])
        self.assertFalse(duplicate.exists())
        self.assertTrue(good_out.exists())
        self.assertEqual(len(list((self.root / "reports").glob("*_diagnostics.csv"))), 1)

    def test_empty_and_mismatched_runs(self):
        queue = {"version": 1, "run_id": "run1", "complete": True, "items": []}
        proc, rows = self.invoke(queue)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(rows, [])

    def test_stale_queue_fails_without_processing(self):
        queue = {"version": 1, "run_id": "old", "complete": True, "items": [{}]}
        proc, rows = self.invoke(queue)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual([row["status"] for row in rows], ["failed"])
        self.assertIn("does not match", rows[0]["message"])


if __name__ == "__main__":
    unittest.main()