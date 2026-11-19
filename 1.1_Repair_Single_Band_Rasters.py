"""Consume a completed UAV repair queue without changing source rasters.

Only explicitly identified Byte palette indices and Byte RGB imagery are
supported. Transparency comes from the palette, masks and nodata, never from
black or white pixel values. Scratch files are kept outside mosaic discovery.
"""

import csv
import json
import math
import os
import re
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from task_run_report import RunReport

TARGET_CRS = "EPSG:2955"
FIELDS = ("run_id", "item", "client", "job_number", "reason", "source_path",
          "output_path", "status", "message")


def configure_gdal():
    # Source PAM sidecars can contain georeferencing, color tables or nodata.
    from osgeo import gdal, osr
    import numpy as np

    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "YES")
    gdal.SetConfigOption("GDAL_CACHEMAX", "512")
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
    driver = gdal.GetDriverByName("COG")
    if driver is None:
        raise RuntimeError("GDAL COG driver is unavailable")
    options = driver.GetMetadataItem("DMD_CREATIONOPTIONLIST") or ""
    try:
        supported = any(option.get("name") == "STATISTICS" and
                        any(value.text == "YES" for value in option.findall("Value"))
                        for option in ElementTree.fromstring(options).findall("Option"))
    except ElementTree.ParseError as error:
        raise RuntimeError("Cannot inspect COG driver creation options") from error
    if not supported:
        raise RuntimeError("GDAL COG driver does not support STATISTICS=YES")
    return gdal, osr, np


def load_queue(path, run_id):
    if not path or not run_id:
        raise ValueError("UAV_REPAIR_QUEUE and UAV_REPAIR_RUN_ID must both be set")
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        queue = json.load(handle)
    if not isinstance(queue, dict):
        raise ValueError("Repair queue must be a JSON object")
    if type(queue.get("version")) is not int or queue["version"] != 1:
        raise ValueError("Unsupported repair queue version")
    if not isinstance(queue.get("run_id"), str) or queue["run_id"] != run_id:
        raise ValueError("Repair queue run_id does not match UAV_REPAIR_RUN_ID")
    if queue.get("complete") is not True:
        raise ValueError("Repair queue is not complete")
    if not isinstance(queue.get("items"), list):
        raise ValueError("Repair queue items must be a list")
    return queue["items"]


def path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def validate_item(item):
    if not isinstance(item, dict):
        raise ValueError("Queue item must be an object")
    for key in ("source_path", "output_path", "client", "job_number", "reason"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f"Queue item requires a nonempty string {key}")
    source, output = Path(item["source_path"]), Path(item["output_path"])
    if not source.is_absolute() or not output.is_absolute():
        raise ValueError("Queue paths must be absolute")
    if output.suffix.casefold() not in (".tif", ".tiff"):
        raise ValueError("Output must be a TIFF path")
    return source, output


def ensure_free_output(output):
    # Existing sidecars could change the apparent CRS, masks or pixels on open.
    candidates = [output, Path(str(output) + ".aux.xml"), Path(str(output) + ".msk"),
                  Path(str(output) + ".ovr"), output.with_suffix(".tfw"),
                  output.with_suffix(".tifw"), output.with_suffix(".wld"),
                  output.with_suffix(".prj")]
    for candidate in candidates:
        if os.path.lexists(candidate):
            raise FileExistsError(f"Output or output sidecar already exists: {candidate}")


def hide_directory(path):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel.GetFileAttributesW.restype = wintypes.DWORD
        kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel.SetFileAttributesW.restype = wintypes.BOOL
        attrs = kernel.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF or not kernel.SetFileAttributesW(str(path), attrs | 2):
            raise ctypes.WinError(ctypes.get_last_error())


def classify_source(ds, gdal):
    if ds.RasterXSize < 1 or ds.RasterYSize < 1:
        raise ValueError("Unsupported empty raster")
    if not ds.GetProjection():
        raise ValueError("Source CRS is missing")
    if ds.GetGeoTransform(can_return_null=True) is None:
        raise ValueError("Source affine geotransform is missing")
    bands = [ds.GetRasterBand(i) for i in range(1, ds.RasterCount + 1)]
    if any(b.DataType != gdal.GDT_Byte for b in bands):
        raise ValueError("Unsupported raster: all bands must be Byte")
    if len(bands) == 1:
        band = bands[0]
        table = band.GetColorTable()
        if band.GetColorInterpretation() != gdal.GCI_PaletteIndex or table is None:
            raise ValueError("Unsupported single-band raster: palette index required")
        if table.GetPaletteInterpretation() != gdal.GPI_RGB:
            raise ValueError("Unsupported non-RGB palette")
        return "palette"
    if len(bands) == 3 and [b.GetColorInterpretation() for b in bands] == [
            gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]:
        if any(b.GetColorTable() is not None for b in bands):
            raise ValueError("Unsupported RGB raster with a color table")
        return "rgb"
    raise ValueError("Unsupported raster: expected one palette-index band or three explicitly identified RGB bands")


def expand_rgba(src, dest, kind, gdal, np):
    """Materialize RGBA in fixed-size windows; do not set RGB nodata values."""
    width, height = src.RasterXSize, src.RasterYSize
    rgba = gdal.GetDriverByName("GTiff").Create(
        str(dest), width, height, 4, gdal.GDT_Byte,
        options=["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
                 "COMPRESS=LZW", "PREDICTOR=2", "BIGTIFF=IF_SAFER", "ALPHA=YES"])
    if rgba is None:
        raise RuntimeError("Could not create intermediate RGBA raster")
    try:
        rgba.SetProjection(src.GetProjection())
        rgba.SetGeoTransform(src.GetGeoTransform())
        for i, role in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand,
                                  gdal.GCI_BlueBand, gdal.GCI_AlphaBand), 1):
            rgba.GetRasterBand(i).SetColorInterpretation(role)
        bands = [src.GetRasterBand(i) for i in range(1, src.RasterCount + 1)]
        if kind == "palette":
            table = bands[0].GetColorTable()
            count = table.GetCount()
            if not 1 <= count <= 256:
                raise ValueError("Unsupported palette size")
            entries = [table.GetColorEntry(i) for i in range(count)]
            if any(entry is None or len(entry) != 4 or any(v < 0 or v > 255 for v in entry)
                   for entry in entries):
                raise ValueError("Invalid RGBA palette entry")
            lut = np.asarray(entries, dtype=np.uint8)
        for y in range(0, height, 512):
            for x in range(0, width, 512):
                w, h = min(512, width - x), min(512, height - y)
                pixels = [b.ReadAsArray(x, y, w, h) for b in bands]
                if any(p is None for p in pixels):
                    raise RuntimeError("Could not read source pixel block")
                if kind == "palette":
                    if int(pixels[0].max()) >= count:
                        raise ValueError("Source uses an index outside its color table")
                    colors = lut[pixels[0]]
                    channels = [colors[:, :, i] for i in range(3)]
                    alpha = colors[:, :, 3].copy()
                else:
                    channels = pixels
                    alpha = np.full((h, w), 255, dtype=np.uint8)
                # Combine distinct masks once, using intersection rather than
                # multiplying the same shared dataset mask for every RGB band.
                mask = np.full((h, w), 255, dtype=np.uint8)
                nodata_matches = None
                for band, pixel in zip(bands, pixels):
                    flags = band.GetMaskFlags()
                    # Nodata-derived masks are handled together below. Applying
                    # them individually makes a red=0 RGB pixel transparent even
                    # when the RGB nodata tuple is (0, 0, 0).
                    if not (flags & (gdal.GMF_ALL_VALID | gdal.GMF_NODATA)):
                        values = band.GetMaskBand().ReadAsArray(x, y, w, h)
                        if values is None:
                            raise RuntimeError("Could not read source mask block")
                        np.minimum(mask, values, out=mask)
                    nodata = band.GetNoDataValue()
                    if nodata is not None:
                        matches = pixel == nodata
                        nodata_matches = matches if nodata_matches is None else nodata_matches & matches
                if nodata_matches is not None:
                    mask[nodata_matches] = 0
                alpha = ((alpha.astype(np.uint16) * mask.astype(np.uint16) + 127) // 255).astype(np.uint8)
                for i, channel in enumerate(channels + [alpha], 1):
                    rgba.GetRasterBand(i).WriteArray(channel, x, y)
        rgba.FlushCache()
    finally:
        rgba = None


def validate_cog(path, gdal, osr):
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise ValueError("Cannot reopen output COG")
    try:
        if ds.RasterXSize < 1 or ds.RasterYSize < 1 or ds.RasterCount != 4:
            raise ValueError("Output must have positive dimensions and four bands")
        roles = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand]
        if [ds.GetRasterBand(i).GetColorInterpretation() for i in range(1, 5)] != roles:
            raise ValueError("Output band roles are not RGBA")
        if any(ds.GetRasterBand(i).DataType != gdal.GDT_Byte for i in range(1, 5)):
            raise ValueError("Output bands are not Byte")
        if any(ds.GetRasterBand(i).GetNoDataValue() is not None for i in range(1, 5)):
            raise ValueError("Output must use alpha, not RGB nodata")
        target = osr.SpatialReference()
        target.ImportFromEPSG(2955)
        actual = ds.GetSpatialRef()
        if actual is None or not actual.IsSame(target):
            raise ValueError("Output CRS is not EPSG:2955")
        if ds.GetGeoTransform(can_return_null=True) is None:
            raise ValueError("Output geotransform is missing")
        meta = ds.GetMetadata("IMAGE_STRUCTURE")
        if meta.get("LAYOUT") != "COG" or meta.get("COMPRESSION") != "LZW" or meta.get("PREDICTOR") != "2":
            raise ValueError(f"Output COG/compression/predictor invalid: {meta}")
        if any(ds.GetRasterBand(i).GetBlockSize() != [512, 512] for i in range(1, 5)):
            raise ValueError("Output block size is not 512 by 512")
        # Decode every block, not just the TIFF header, before publishing.
        has_visible_pixels = False
        for y in range(0, ds.RasterYSize, 512):
            for x in range(0, ds.RasterXSize, 512):
                width, height = min(512, ds.RasterXSize - x), min(512, ds.RasterYSize - y)
                if ds.ReadRaster(x, y, width, height) is None:
                    raise ValueError("Could not decode output block")
                if not has_visible_pixels:
                    alpha = ds.GetRasterBand(4).ReadRaster(x, y, width, height)
                    if alpha is None:
                        raise ValueError("Could not decode output alpha block")
                    has_visible_pixels = any(alpha)
        if not has_visible_pixels:
            raise ValueError("Output is entirely transparent; RGB statistics cannot be validated")
        files = ds.GetFileList() or []
        if len(files) != 1:
            raise ValueError(f"Output depends on unexpected sidecars: {files}")
    finally:
        ds = None


def validate_internal_statistics(path, gdal):
    """Check persisted per-band statistics without PAM sidecars or recomputation."""
    old_pam = gdal.GetThreadLocalConfigOption("GDAL_PAM_ENABLED", None)
    dataset = None
    try:
        gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", "NO")
        dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
        if dataset is None:
            raise ValueError("Cannot reopen COG to inspect internal statistics")
        if dataset.RasterCount != 4:
            raise ValueError("Cannot inspect statistics: output does not have four bands")
        keys = ("STATISTICS_MINIMUM", "STATISTICS_MAXIMUM", "STATISTICS_MEAN", "STATISTICS_STDDEV")
        for index in range(1, 5):
            metadata = dataset.GetRasterBand(index).GetMetadata()
            try:
                values = [float(metadata[key]) for key in keys]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Band {index} is missing valid internal statistics") from error
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"Band {index} has non-finite internal statistics")
            minimum, maximum, mean, stddev = values
            if minimum > maximum or not minimum <= mean <= maximum or stddev < 0:
                raise ValueError(f"Band {index} has inconsistent internal statistics")
            if metadata.get("STATISTICS_APPROXIMATE", "NO").upper() in {"YES", "TRUE", "1"}:
                raise ValueError(f"Band {index} has approximate statistics")
    finally:
        dataset = None
        gdal.SetThreadLocalConfigOption("GDAL_PAM_ENABLED", old_pam)


def publish_no_replace(temp, output):
    # Windows rename fails atomically if destination exists. On POSIX, rename
    # replaces files, so use an atomic hard-link creation instead, fail closed.
    if os.name == "nt":
        os.rename(temp, output)
    else:
        os.link(temp, output)
        os.unlink(temp)


def repair(source, output, gdal, osr, np):
    ensure_free_output(output)
    if not output.parent.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {output.parent}")
    src = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src is None:
        raise ValueError("GDAL could not open source raster")
    try:
        kind = classify_source(src, gdal)
        with tempfile.TemporaryDirectory(prefix=".uav_repair_", dir=str(output.parent)) as scratch:
            hide_directory(scratch)
            expanded, cog = Path(scratch) / "rgba.tif", Path(scratch) / "result.tif"
            expand_rgba(src, expanded, kind, gdal, np)
            # Close source before reprojection and publication, especially on Windows.
            src = None
            algorithm = "near" if kind == "palette" else "bilinear"
            overview = "NEAREST" if kind == "palette" else "BILINEAR"
            warped = None
            try:
                warped = gdal.Warp(str(cog), str(expanded), options=gdal.WarpOptions(
                    format="COG", dstSRS=TARGET_CRS, resampleAlg=algorithm,
                    srcAlpha=True, dstAlpha=True, multithread=True,
                    creationOptions=["COMPRESS=LZW", "PREDICTOR=2", "BLOCKSIZE=512",
                                     "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS",
                                     f"OVERVIEW_RESAMPLING={overview}", "STATISTICS=YES"]))
                if warped is None:
                    raise RuntimeError("GDAL warp failed")
                warped.FlushCache()
            finally:
                warped = None
            validate_cog(cog, gdal, osr)
            validate_internal_statistics(cog, gdal)
            # Do not publish a TIFF whose metadata or masks depend on sidecars.
            ensure_free_output_sidecars = (Path(str(cog) + suffix) for suffix in (".aux.xml", ".msk", ".ovr"))
            if any(os.path.lexists(sidecar) for sidecar in ensure_free_output_sidecars):
                raise ValueError("Output COG has unexpected sidecars")
            ensure_free_output(output)
            publish_no_replace(cog, output)
        return f"{kind} Byte to RGBA COG; resampling={algorithm}; internal band statistics included"
    finally:
        src = None


def main(report):
    run_id = os.environ.get("UAV_REPAIR_RUN_ID", "")
    queue_path = os.environ.get("UAV_REPAIR_QUEUE", "")
    report_dir = Path(os.environ.get("UAV_REPORT_DIR", Path(__file__).resolve().parent.parent))
    report_dir.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9_-]", "_", run_id)[:80] or "missing_run"
    summary = report_dir / f"repair_{label}_{uuid.uuid4().hex}_summary.csv"
    print(f"Summary: {summary}")
    with summary.open("x", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        handle.flush()
        try:
            items = load_queue(queue_path, run_id)
        except Exception as error:
            report.exception("load_queue", error, input_path=queue_path)
            writer.writerow({"run_id": run_id, "status": "failed", "message": str(error)})
            return 1
        # A dependency failure still produces one diagnostic per queued item.
        dependencies = None
        dependency_error = None
        if items:
            try:
                dependencies = configure_gdal()
            except Exception as error:
                dependency_error = f"{type(error).__name__}: {error}"
        sources = set()
        targets = Counter()
        for item in items:
            try:
                source, output = validate_item(item)
                sources.add(path_key(source))
                targets[path_key(output)] += 1
            except Exception:
                pass
        failed = 0
        for index, item in enumerate(items, 1):
            row = {"run_id": run_id, "item": index}
            if isinstance(item, dict):
                row.update({key: item.get(key, "") for key in
                            ("client", "job_number", "reason", "source_path", "output_path")})
            try:
                source, output = validate_item(item)
                if path_key(output) in sources:
                    raise FileExistsError("Output collides with a queued source")
                if targets[path_key(output)] != 1:
                    raise FileExistsError("Multiple queue items reserve the same output")
                if dependency_error:
                    raise RuntimeError(f"GDAL initialization failed: {dependency_error}")
                message = repair(source, output, *dependencies)
                row.update(status="completed", message=message)
                report.record("repair_item", "completed", client=item["client"],
                              project_or_job=item["job_number"], input_path=source,
                              output_path=output, message=message, details=f"item={index}; run_id={run_id}")
            except Exception as error:
                failed += 1
                row.update(status="failed", message=f"{type(error).__name__}: {error}")
                report.exception("repair_item", error, client=str(row.get("client", "")),
                                 project_or_job=str(row.get("job_number", "")),
                                 input_path=str(row.get("source_path", "")),
                                 output_path=str(row.get("output_path", "")),
                                 details=f"item={index}; run_id={run_id}")
            writer.writerow(row)
            handle.flush()
        message = f"run_id={run_id}; items={len(items)}; completed={len(items)-failed}; failed={failed}"
        report.record("run", "completed" if not failed else "completed_with_issues",
                      output_path=summary, message=message)
        print(message)
        return 1 if failed else 0


if __name__ == "__main__":
    run_report = RunReport(__file__)
    print(f"Detailed diagnostics: {run_report.path}")
    try:
        exit_code = main(run_report)
    except Exception as error:
        run_report.exception("run_fatal", error)
        exit_code = 1
    finally:
        run_report.close()
    sys.exit(exit_code)