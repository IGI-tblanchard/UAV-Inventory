"""Benchmark mosaic statistics on independent synthetic copies, never on user data.

Run with the ArcGIS Pro Python interpreter. All generated data is placed in a
new local temporary directory and removed at exit. No input paths are accepted.
The illustrative grayscale stretch is not an ArcGIS Pro renderer export.
"""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import arcpy
import numpy as np


TILE_SIZE = 1024
NODATA = -9999


def local_temp_root():
    """Refuse network, removable and redirected temporary volumes on Windows."""
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    if os.name != "nt" or not root.drive or root.drive.startswith("\\\\"):
        raise RuntimeError(f"This benchmark requires a local Windows temp drive: {root}")
    drive_type = ctypes.windll.kernel32.GetDriveTypeW(root.anchor)
    if drive_type != 3:  # DRIVE_FIXED; mapped network drives are DRIVE_REMOTE.
        raise RuntimeError(f"Temp directory is not on a fixed local drive: {root} (type {drive_type})")
    return root


def make_seed(root):
    source_dir = root / "seed" / "tiffs"
    source_dir.mkdir(parents=True)
    gdb = str(root / "seed" / "seed.gdb")
    arcpy.management.CreateFileGDB(str(source_dir.parent), "seed.gdb")
    sr = arcpy.SpatialReference(2955)
    mosaic = str(Path(gdb) / "synthetic_dem")
    arcpy.management.CreateMosaicDataset(gdb, "synthetic_dem", sr, 1, "32_BIT_FLOAT")
    rng = np.random.default_rng(20261118)
    yy, xx = np.indices((TILE_SIZE, TILE_SIZE), dtype=np.float32)
    for row in range(2):
        for col in range(2):
            data = (450 + 0.045 * (xx + col * TILE_SIZE)
                    + 0.025 * (yy + row * TILE_SIZE)
                    + 8 * np.sin(xx / 89) * np.cos(yy / 73)
                    + rng.normal(0, 1.5, (TILE_SIZE, TILE_SIZE))).astype(np.float32)
            # Sparse extremes and a NoData corner make sampling error observable.
            data[111:116, 333:338] += 120
            data[:24, :32] = NODATA
            path = str(source_dir / f"tile_{row}_{col}.tif")
            raster = arcpy.NumPyArrayToRaster(data, arcpy.Point(500000 + col * TILE_SIZE,
                                              6000000 + row * TILE_SIZE), 1, 1, NODATA)
            raster.save(path)
            del raster
            arcpy.management.DefineProjection(path, sr)
            arcpy.management.AddRastersToMosaicDataset(
                mosaic, "Raster Dataset", path, update_cellsize_ranges="UPDATE_CELL_SIZES",
                update_boundary="UPDATE_BOUNDARY", update_overviews="NO_OVERVIEWS",
                calculate_statistics="NO_STATISTICS", build_pyramids="NO_PYRAMIDS")
    return mosaic, source_dir


def validate_paths(mosaic, source_dir, allow_overviews=False):
    """Require the four TIFFs and ensure all catalog paths stay in this copy."""
    table = arcpy.CreateUniqueName("check_paths", str(Path(mosaic).parent))
    try:
        arcpy.management.ExportMosaicDatasetPaths(mosaic, table, export_mode="ALL", types_of_paths="RASTER")
        with arcpy.da.SearchCursor(table, ["Path"]) as cursor:
            paths = [Path(row[0]).resolve(strict=True) for row in cursor if row[0]]
        expected = {p.resolve(strict=True) for p in source_dir.glob("*.tif")}
        if len(expected) != 4 or not expected.issubset(paths) or (not allow_overviews and (len(paths) != 4 or set(paths) != expected)):
            raise RuntimeError(f"Unexpected source paths in {mosaic}: {paths}")
        copy_root = source_dir.parent.resolve(strict=True)
        if any(not path.is_relative_to(copy_root) for path in paths):
            raise RuntimeError(f"Mosaic references outside its own copy: {paths}")
        return [str(path) for path in paths]
    finally:
        if arcpy.Exists(table):
            arcpy.management.Delete(table)


def make_copy(root, seed_mosaic, seed_sources, factor):
    folder = root / f"copy_{factor}"
    folder.mkdir()
    sources = folder / "tiffs"
    shutil.copytree(seed_sources, sources)
    arcpy.management.CreateFileGDB(str(folder), "copy.gdb")
    mosaic = str(folder / "copy.gdb" / "synthetic_dem")
    arcpy.management.Copy(seed_mosaic, mosaic)
    arcpy.management.RepairMosaicDatasetPaths(mosaic, [[str(seed_sources), str(sources)]])
    return mosaic, validate_paths(mosaic, sources)


def properties(mosaic):
    return {name.lower(): float(arcpy.management.GetRasterProperties(mosaic, name).getOutput(0))
            for name in ("MINIMUM", "MAXIMUM", "MEAN", "STD")}


def numeric_stats(values):
    values = values[values != NODATA].astype(np.float64)
    if not values.size:
        raise ValueError("No valid pixels available")
    return {"minimum": float(values.min()), "maximum": float(values.max()),
            "mean": float(values.mean()), "std": float(values.std())}


def stretch(values, stats):
    """Simple mean +/- 2 std grayscale simulation, not Pro display output."""
    low, high = stats["mean"] - 2 * stats["std"], stats["mean"] + 2 * stats["std"]
    if not high > low:
        raise ValueError("Invalid stretch range")
    return np.rint(np.clip((values - low) * (255.0 / (high - low)), 0, 255)).astype(np.uint8)


def run_worker(root):
        root = Path(root).resolve(strict=True)
        if root.parent != local_temp_root() or not root.name.startswith("synthetic_stats_benchmark_"):
            raise RuntimeError("Worker directory must be a benchmark directory directly under local temp")
        if any(root.iterdir()):
            raise RuntimeError("Worker directory must start empty")
        print(f"Synthetic workspace: {root}", flush=True)
        arcpy.ResetEnvironments()
        arcpy.env.workspace = str(root)
        arcpy.env.scratchWorkspace = str(root)
        arcpy.env.overwriteOutput = False
        arcpy.env.pyramid = "NONE"
        arcpy.env.rasterStatistics = "NONE"
        seed, sources = make_seed(root)
        copies = {factor: make_copy(root, seed, sources, factor) for factor in (10, 20)}
        arcpy.ClearEnvironment("rasterStatistics")
        for factor, (mosaic, _) in copies.items():
            print(f"Building local overviews for copy {factor}", flush=True)
            arcpy.management.BuildOverviews(mosaic, define_missing_tiles="DEFINE_MISSING_TILES",
                generate_overviews="GENERATE_OVERVIEWS", generate_missing_images="GENERATE_MISSING_IMAGES",
                regenerate_stale_images="REGENERATE_STALE_IMAGES")
            copies[factor] = (mosaic, validate_paths(mosaic, root / f"copy_{factor}" / "tiffs", True))
        results = {}
        # Both copies are made and validated before the first timed calculation.
        for factor, (mosaic, paths) in copies.items():
            started = time.perf_counter()
            operation = arcpy.management.CalculateStatistics(
                in_raster_dataset=mosaic, x_skip_factor=factor, y_skip_factor=factor,
                ignore_values=[NODATA])  # Default OVERWRITE, matching the production call.
            elapsed = time.perf_counter() - started
            print(f"skip={factor} elapsed={elapsed:.3f}s: {operation.getMessages()}", flush=True)
            messages = operation.getMessages()
            # A success status with warning 004093 must never be treated as valid stats.
            stats = None
            error = None
            if operation.maxSeverity > 0:
                error = "ArcPy returned warning(s); persisted statistics not trusted"
            else:
                try:
                    stats = properties(mosaic)
                except arcpy.ExecuteError as exc:
                    error = str(exc)
            results[factor] = {"seconds": elapsed, "statistics": stats, "statistics_error": error,
                               "source_paths": paths, "messages": messages}
        arrays = {factor: arcpy.RasterToNumPyArray(mosaic, nodata_to_value=NODATA)
                  for factor, (mosaic, _) in copies.items()}
        if arrays[10].shape != (2 * TILE_SIZE, 2 * TILE_SIZE):
            raise RuntimeError(f"Unexpected mosaic shape: {arrays[10].shape}")
        if not np.array_equal(arrays[10], arrays[20]):
            raise RuntimeError("The two mosaics do not have identical raw pixel values")
        valid = arrays[10][arrays[10] != NODATA].astype(np.float64)
        sample_stats = {factor: numeric_stats(arrays[factor][::factor, ::factor]) for factor in (10, 20)}
        # A simulated stretch based on a fixed NumPy grid is a fallback, not an ArcGIS renderer.
        stats_source = "persisted ArcPy statistics" if all(results[f]["statistics"] for f in (10, 20)) else "NumPy fixed-grid samples (not ArcPy statistics)"
        stretch_stats = {f: results[f]["statistics"] if stats_source.startswith("persisted") else sample_stats[f]
                         for f in (10, 20)}
        a, b = stretch(valid, stretch_stats[10]), stretch(valid, stretch_stats[20])
        delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
        report = {
            "arcgis_version": arcpy.GetInstallInfo().get("Version"),
            "pixel_shape": list(arrays[10].shape), "valid_pixels": int(valid.size),
            "raw_sha256_both": hashlib.sha256(arrays[10].tobytes()).hexdigest(),
            "full_pixel_reference": numeric_stats(arrays[10]),
            "benchmark": results,
            "numpy_fixed_grid_sample_stats_not_arcpy": sample_stats,
            "illustrative_mean_plus_minus_2_std_stretch": {
                "statistics_source": stats_source,
                "mean_absolute_8bit_difference": float(delta.mean()),
                "max_absolute_8bit_difference": int(delta.max()),
                "percent_changed": float(np.mean(delta != 0) * 100),
            },
            "caveat": "One synthetic run; no real data or actual ArcGIS renderer export.",
        }
        print(json.dumps(report, indent=2), flush=True)
        del arrays, valid, a, b, delta
        arcpy.management.ClearWorkspaceCache()
        return 0 if all(results[f]["statistics"] is not None for f in (10, 20)) else 1


def main():
    with tempfile.TemporaryDirectory(prefix="synthetic_stats_benchmark_", dir=str(local_temp_root())) as tmp:
        # A separate process releases all geodatabase locks before parent cleanup.
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", tmp], check=False)
        return result.returncode


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        sys.exit(run_worker(sys.argv[2]))
    elif len(sys.argv) == 1:
        sys.exit(main())
    else:
        raise SystemExit("No input paths are accepted")