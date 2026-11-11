import csv
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path

from task_run_report import RunReport

run_report = RunReport(__file__)
print(f"Detailed diagnostics: {run_report.path}")
try:
    import numpy as np
    from osgeo import gdal, osr
except Exception as error:
    run_report.exception("import_dependencies", error)
    run_report.close()
    raise

# ---- configuration ----
BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client")
CLIENT_CODES = {
    "Cenovus": "CVE",
    "Tourmaline": "TOU",
    "Whitecap": "WCP",
}
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\4_reproject_report.csv")
RENAME_REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\4_reproject_dsm_dtm_ouput.csv")

MONTHS = {
    "jan": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "may": "05",
    "jun": "06",
    "jul": "07",
    "aug": "08",
    "sep": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}

DATE_NAME_PATTERNS = (
    re.compile(
        r"(?P<month>[A-Za-z]{3})[- ](?P<day>\d{1,2})[- ](?P<year>\d{4})"
        r"(?:[- _]*(?P<embedded_type>DSM|DTM))?(?P<suffix>(?:[- _]+.+)?)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<day>\d{1,2})[- ](?P<month>[A-Za-z]{3})[- ](?P<year>\d{4})"
        r"(?:[- _]*(?P<embedded_type>DSM|DTM))?(?P<suffix>(?:[- _]+.+)?)$",
        re.IGNORECASE,
    ),
)
CANONICAL_NAME_PATTERN = re.compile(
    r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})-(?P<type>DSM|DTM)(?P<suffix>(?:-.+)?)$",
    re.IGNORECASE,
)

# Float32 max — the sentinel some UAV software burns in for out-of-footprint fill pixels.
FALLBACK_NODATA_SENTINEL = 3.4028235e+38
# Any pixel beyond this is not a real elevation — catches leftover sentinel/blended artifacts post-warp.
SENTINEL_CLEANUP_THRESHOLD = 100000.0

TARGET_CRS_CODE = "EPSG:2955"  # NAD83(CSRS) UTM Zone 11N
resampling = gdal.GRA_Bilinear

# Enable exceptions for GDAL
try:
    gdal.UseExceptions()
    # Suppress .aux.xml sidecars; statistics are embedded internally.
    gdal.SetConfigOption('GDAL_PAM_ENABLED', 'NO')
    gdal.SetConfigOption('GDAL_CACHEMAX', '1024')
    gdal.SetConfigOption('GDAL_NUM_THREADS', 'ALL_CPUS')
except Exception as error:
    run_report.exception("configure_gdal", error)
    run_report.close()
    raise


def create_cog_from_tif(src_tif_path, output_path, target_crs_code):
    """
    Reproject a tif to target CRS and create Cloud Optimized GeoTIFF (COG).
    Same pipeline/settings as LiDAR Updates Real/5_reproject_DEM_v4.py.
    """
    try:
        src_ds = gdal.Open(str(src_tif_path))
        if not src_ds:
            print(f"ERROR: Could not open raster dataset: {src_tif_path}")
            run_report.record("open_source_raster", "failed", input_path=src_tif_path,
                              output_path=output_path, message="GDAL could not open source raster")
            return False

        src_crs = osr.SpatialReference(wkt=src_ds.GetProjection())
        src_crs_name = src_crs.GetAttrValue('PROJCS') or src_crs.GetAttrValue('GEOGCS') or 'UNKNOWN'

        # Detect source NoData — prevents internal mask band creation during warp.
        src_nodata = src_ds.GetRasterBand(1).GetNoDataValue()
        if src_nodata is None:
            print(f"WARNING: no NoData tag on source, using fallback sentinel: {src_tif_path}")
            run_report.record("inspect_source_raster", "warning", input_path=src_tif_path,
                              output_path=output_path, message="Source has no NoData tag; fallback sentinel used")
            src_nodata = FALLBACK_NODATA_SENTINEL

        print(f"REPROJECT: {src_tif_path}")
        print(f"Input CRS: {src_crs_name} -- Output CRS: {target_crs_code}")

        # Compute output dimensions without performing the warp, to size overviews.
        dst_crs = osr.SpatialReference()
        dst_crs.ImportFromEPSG(int(target_crs_code.split(':')[1]))
        vrt_ds = gdal.AutoCreateWarpedVRT(src_ds, None, dst_crs.ExportToWkt(), resampling)
        if vrt_ds is None:
            print(f"ERROR: Could not compute output dimensions for: {src_tif_path}")
            run_report.record("calculate_warp_dimensions", "failed", input_path=src_tif_path,
                              output_path=output_path, message="GDAL AutoCreateWarpedVRT returned no dataset")
            src_ds = None
            return False

        w, h = vrt_ds.RasterXSize, vrt_ds.RasterYSize
        # Native output resolution — matches the source's ground sample distance in the target CRS.
        vrt_gt = vrt_ds.GetGeoTransform()
        x_res, y_res = abs(vrt_gt[1]), abs(vrt_gt[5])
        vrt_ds = None

        # ceil(log2(max(w,h)/256)) — overview levels down to 256-px threshold.
        overview_count = max(1, math.ceil(math.log2(max(w, h) / 256))) if max(w, h) > 256 else 1

        warp_options = gdal.WarpOptions(
            format='COG',
            dstSRS=target_crs_code,
            resampleAlg=resampling,
            xRes=x_res,
            yRes=y_res,
            outputType=gdal.GDT_Float32,
            srcNodata=src_nodata,
            dstNodata=-9999,
            warpOptions=['INIT_DEST=-9999'],
            creationOptions=[
                'COMPRESS=LZW',
                'PREDICTOR=2',
                'BLOCKSIZE=512',
                f'OVERVIEW_COUNT={overview_count}',
                'OVERVIEW_RESAMPLING=BILINEAR',
                'NUM_THREADS=ALL_CPUS',
                'BIGTIFF=IF_SAFER',
                'STATISTICS=YES',
            ],
            callback=gdal.TermProgress_nocb,
        )

        dst_ds = gdal.Warp(str(output_path), src_ds, options=warp_options)
        src_ds = None

        if dst_ds is None:
            print(f"ERROR: Failed to create COG: {output_path}")
            run_report.record("warp_to_cog", "failed", input_path=src_tif_path,
                              output_path=output_path, message="GDAL Warp returned no output dataset")
            return False

        _clean_sentinel_values(dst_ds)

        # Embed statistics so ArcPro reads them internally without .aux.xml.
        for b in range(1, dst_ds.RasterCount + 1):
            dst_ds.GetRasterBand(b).ComputeStatistics(False)
        dst_ds.FlushCache()
        dst_ds = None

        return True

    except Exception as e:
        print(f"ERROR in create_cog_from_tif: {str(e)}")
        run_report.exception("create_cog", e, input_path=src_tif_path, output_path=output_path)
        return False


def _clean_sentinel_values(dst_ds, threshold=SENTINEL_CLEANUP_THRESHOLD, nodata_value=-9999.0):
    """Remap leftover +/-sentinel or blended-edge artifacts to NoData, block-wise to bound memory use."""
    for b in range(1, dst_ds.RasterCount + 1):
        band = dst_ds.GetRasterBand(b)
        block_x, block_y = band.GetBlockSize()
        for y in range(0, band.YSize, block_y):
            rows = min(block_y, band.YSize - y)
            for x in range(0, band.XSize, block_x):
                cols = min(block_x, band.XSize - x)
                arr = band.ReadAsArray(x, y, cols, rows)
                mask = np.abs(arr) > threshold
                if mask.any():
                    arr[mask] = nodata_value
                    band.WriteArray(arr, x, y)


def is_valid_existing_output(path):
    """Return True if path is a tif that GDAL can open with a real raster inside."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        ds = gdal.Open(str(path))
    except Exception:
        return False
    if ds is None:
        return False
    valid = ds.RasterXSize > 0 and ds.RasterYSize > 0
    ds = None
    return valid


def iter_source_tifs():
    """Yield (client, project, type_tag, source_tif, output_dir) for each DSM/DTM tif."""
    try:
        client_dirs = sorted((path for path in BASE_DIR.iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
    except OSError as error:
        run_report.exception("discover_client_directories", error, input_path=BASE_DIR)
        return

    for client_dir in client_dirs:
        client_folder = client_dir.name
        client_code = CLIENT_CODES.get(client_folder, client_folder)
        for type_tag, base_subdir in (("DSM", "FullFeature"), ("DTM", "BareEarth")):
            type_root = client_dir / "LiDAR" / "UAV" / base_subdir
            if not type_root.is_dir():
                run_report.record("discover_source_root", "info", client=client_folder,
                                  data_type=type_tag, input_path=type_root,
                                  message="No UAV LiDAR source folder for this client/type; skipped")
                continue
            for project_dir in sorted(type_root.iterdir()):
                if not project_dir.is_dir():
                    continue
                source_type_dir = project_dir / type_tag
                if not source_type_dir.is_dir():
                    run_report.record("discover_source_type_folder", "info", client=client_folder,
                                      project_or_job=project_dir.name, data_type=type_tag,
                                      input_path=source_type_dir,
                                      message="No source folder of this raster type in the project; skipped")
                    continue
                tif_paths = sorted(source_type_dir.glob("*.tif")) + sorted(source_type_dir.glob("*.tiff"))
                for tif_path in tif_paths:
                    yield client_code, project_dir.name, type_tag, tif_path, project_dir


def expected_tif_name(tif_path, type_tag):
    """Return a canonical DSM/DTM name, or None when its suffix is too long."""

    def modified_date_fallback():
        try:
            modified = datetime.fromtimestamp(tif_path.stat().st_mtime)
        except OSError:
            return None
        suffix = re.sub(r"[^A-Za-z0-9]+", "-", tif_path.stem).strip("-")
        if len(suffix) > 20:
            return None
        return f"{modified:%d-%m-%Y}-{type_tag}{f'-{suffix}' if suffix else ''}{tif_path.suffix}"

    stem = tif_path.stem
    canonical_match = CANONICAL_NAME_PATTERN.fullmatch(stem)
    if canonical_match:
        day = canonical_match.group("day")
        month = canonical_match.group("month")
        year = canonical_match.group("year")
        suffix = canonical_match.group("suffix")
        if len((suffix or "").lstrip("-")) > 20:
            return None
    else:
        date_match = next((pattern.search(stem) for pattern in DATE_NAME_PATTERNS if pattern.search(stem)), None)
        if not date_match:
            return modified_date_fallback()
        month = MONTHS.get(date_match.group("month").lower())
        if month is None:
            return modified_date_fallback()
        day = date_match.group("day").zfill(2)
        year = date_match.group("year")
        suffix = date_match.group("suffix") or ""
        if date_match.group("embedded_type"):
            suffix = re.sub(r"^[- _]+", "", suffix)
            suffix = f"-{suffix}" if suffix else ""

    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return modified_date_fallback()

    suffix = re.sub(r"^[- _]+", "", suffix or "")
    suffix = re.sub(r"[ _]+", "-", suffix)
    suffix = re.sub(r"-+", "-", suffix).strip("-")
    if len(suffix) > 20:
        return None
    return f"{day}-{month}-{year}-{type_tag}{f'-{suffix}' if suffix else ''}{tif_path.suffix}"


def sidecar_rename_pairs(tif_path, target_path):
    """Return adjacent files sharing the TIFF name stem and their renamed paths."""
    pairs = []
    tif_prefix = f"{tif_path.name}.".casefold()
    stem_prefix = f"{tif_path.stem}.".casefold()

    for candidate in sorted(tif_path.parent.iterdir()):
        if not candidate.is_file():
            continue

        candidate_name = candidate.name
        folded_name = candidate_name.casefold()
        if folded_name.startswith(tif_prefix):
            suffix = candidate_name[len(tif_path.name):]
            new_name = f"{target_path.name}{suffix}"
        elif folded_name.startswith(stem_prefix):
            if candidate.suffix.casefold() in {".tif", ".tiff"}:
                continue
            suffix = candidate_name[len(tif_path.stem):]
            new_name = f"{target_path.stem}{suffix}"
        else:
            continue

        pairs.append((candidate, candidate.with_name(new_name)))

    return pairs


def rename_file_group(file_pairs):
    """Rename TIFFs and sidecars as one staged group, rolling back on failure."""
    staged = []
    try:
        for source_path, target_path in file_pairs:
            temp_path = source_path.with_name(f".__uav_lidar_rename_{time.time_ns()}_{len(staged)}.tmp")
            while temp_path.exists():
                temp_path = source_path.with_name(f".__uav_lidar_rename_{time.time_ns()}_{len(staged)}.tmp")
            source_path.rename(temp_path)
            staged.append((source_path, temp_path, target_path, False))

        for index, (source_path, temp_path, target_path, _) in enumerate(staged):
            temp_path.rename(target_path)
            staged[index] = (source_path, temp_path, target_path, True)
    except Exception:
        for source_path, temp_path, target_path, completed in reversed(staged):
            try:
                if completed and target_path.exists():
                    target_path.rename(temp_path)
                if temp_path.exists():
                    temp_path.rename(source_path)
            except OSError as rollback_error:
                run_report.exception("rollback_source_rename", rollback_error,
                                 input_path=temp_path, output_path=source_path)
        raise


def prepare_source_records():
    """Check existing COGs first, then preflight new-source names and sidecars."""
    records = []
    pending = []
    for client_code, project, type_tag, tif_path, output_dir in iter_source_tifs():
        try:
            matching_outputs = [
                candidate for candidate in output_dir.iterdir()
                if candidate.is_file()
                and candidate.suffix.casefold() in {".tif", ".tiff"}
                and candidate.stem.casefold() == tif_path.stem.casefold()
            ]
        except OSError as error:
            run_report.exception("find_existing_lidar_cog", error, client=client_code,
                             project_or_job=project, input_path=tif_path, output_path=output_dir)
            records.append({"client": client_code, "project": project, "type": type_tag,
                "input_path": tif_path, "source_path": tif_path, "output_path": "",
                "status": "output_folder_error", "message": str(error)})
            continue

        valid_matches = [path for path in matching_outputs if is_valid_existing_output(path)]
        if valid_matches:
            records.append({"client": client_code, "project": project, "type": type_tag,
                "input_path": tif_path, "source_path": tif_path, "output_path": valid_matches[0],
                "status": "skipped_already_exists",
                "message": "Same-basename COG exists; source, COG, and sidecars left unchanged"})
            continue
        if matching_outputs:
            message = "Same-basename output exists but is invalid; source and files left untouched"
            records.append({"client": client_code, "project": project, "type": type_tag,
                "input_path": tif_path, "source_path": tif_path, "output_path": matching_outputs[0],
                "status": "skipped_invalid_same_name_output", "message": message})
            run_report.record("check_existing_output", "warning", client=client_code,
                          project_or_job=project, input_path=tif_path,
                          output_path=matching_outputs[0], message=message)
            continue

        expected_name = expected_tif_name(tif_path, type_tag)
        if expected_name is None:
            message = "Could not derive a safe name or qualifier exceeds 20 characters"
            records.append({"client": client_code, "project": project, "type": type_tag,
                "input_path": tif_path, "source_path": tif_path, "output_path": "",
                "status": "invalid_name", "message": message})
            run_report.record("name_new_source", "warning", client=client_code,
                          project_or_job=project, input_path=tif_path, message=message)
            continue

        target_source = tif_path.with_name(expected_name)
        pending.append({
            "client": client_code, "project": project, "type": type_tag,
            "input_path": tif_path, "source_path": target_source,
            "output_dir": output_dir, "output_path": output_dir / expected_name,
            "needs_rename": target_source != tif_path,
            "sidecars": sidecar_rename_pairs(tif_path, target_source) if target_source != tif_path else [],
            "status": "pending", "message": "",
        })

    move_sources = {
        path for record in pending if record["needs_rename"]
        for path in [record["input_path"], *(source for source, _ in record["sidecars"])]
    }
    reserved = {}
    pending.sort(key=lambda record: str(record["input_path"]).casefold())
    for record in pending:
        base_stem = record["source_path"].stem
        extension = record["source_path"].suffix
        part = 1
        while True:
            candidate_stem = base_stem if part == 1 else f"{base_stem}-Part{part}"
            candidate_source = record["input_path"].with_name(candidate_stem + extension)
            candidate_output = record["output_dir"] / candidate_source.name
            candidate_sidecars = (
                sidecar_rename_pairs(record["input_path"], candidate_source)
                if candidate_source != record["input_path"] else []
            )
            pairs = ([(record["input_path"], candidate_source)] if candidate_source != record["input_path"] else []) + candidate_sidecars
            collision = False
            for _, target in pairs:
                target_key = str(target).casefold()
                if target_key in reserved:
                    collision = True
                    break
                if target.exists() and target not in move_sources:
                    collision = True
                    break
            output_key = str(candidate_output).casefold()
            if output_key in reserved or candidate_output.exists():
                collision = True
            if not collision:
                break
            if not record["needs_rename"]:
                record["status"] = "collision"
                record["message"] = "Canonical name conflicts with another source or existing file; left unchanged"
                record["source_path"] = record["input_path"]
                record["output_path"] = candidate_output
                run_report.record("name_collision", "warning", client=record["client"],
                              project_or_job=record["project"], input_path=record["input_path"],
                              output_path=candidate_output, message=record["message"])
                break
            part += 1
            if part > 9999:
                record["status"] = "collision"
                record["message"] = "Could not allocate a unique PartN name; left unchanged"
                record["source_path"] = record["input_path"]
                record["output_path"] = candidate_output
                break

        if record["status"] != "pending":
            continue
        record["source_path"] = candidate_source
        record["output_path"] = candidate_output
        record["sidecars"] = candidate_sidecars
        for source_path, target in pairs:
            reserved[str(target).casefold()] = source_path
        reserved[str(candidate_output).casefold()] = record["input_path"]

    rename_pairs = [
        pair for record in pending if record["status"] == "pending"
        for pair in ([(record["input_path"], record["source_path"])] if record["needs_rename"] else [])
        + record["sidecars"]
    ]
    if rename_pairs:
        try:
            rename_file_group(rename_pairs)
        except Exception as error:
            for record in pending:
                if record["status"] == "pending" and record["needs_rename"]:
                    record["status"] = "rename_failed"
                    record["message"] = f"{type(error).__name__}: {error}"
                    run_report.exception("rename_source_and_sidecars", error, client=record["client"],
                                     project_or_job=record["project"], input_path=record["input_path"],
                                     output_path=record["source_path"])
        else:
            for record in pending:
                if record["status"] == "pending" and record["needs_rename"]:
                    run_report.record("rename_source_and_sidecars", "completed", client=record["client"],
                                  project_or_job=record["project"], input_path=record["input_path"],
                                  output_path=record["source_path"],
                                  message=f"Renamed source and {len(record['sidecars'])} sidecar(s)")
	
    records.extend(pending)

    with RENAME_REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
        writer = csv.DictWriter(report_handle,
            fieldnames=["client", "project", "type", "input_path", "output_path", "status", "message"])
        writer.writeheader()
        for record in records:
            writer.writerow({key: str(record.get(key, "") or "") for key in writer.fieldnames})
    print(f"Rename/status report written: {RENAME_REPORT_CSV}")
    return records


def main():
    run_report.record("run", "started", message="LiDAR rename and reprojection run started",
                      details=f"BASE_DIR={BASE_DIR}; target_crs={TARGET_CRS_CODE}")
    records = prepare_source_records()

    processed = 0
    skipped = 0
    already_done = 0

    with open(REPORT_CSV, "w", newline="") as report_handle:
        writer = csv.DictWriter(
            report_handle,
            fieldnames=["client", "project", "type", "input_path", "renamed_source_path",
                        "output_file_path", "status", "message"],
        )
        writer.writeheader()

        for record in records:
            client_code = record["client"]
            project = record["project"]
            type_tag = record["type"]
            input_path = record["input_path"]
            src_tif = record["source_path"]
            output_path = record["output_path"]
            status = record["status"]
            message = record["message"]

            if status == "skipped_already_exists":
                print(f"Already reprojected, skipping without changes: {output_path}\n")
                already_done += 1
            elif status == "pending":
                try:
                    if output_path.exists():
                        raise FileExistsError(f"Refusing to overwrite an existing output: {output_path}")
                    if create_cog_from_tif(src_tif, output_path, TARGET_CRS_CODE):
                        print(f"Done: {output_path}\n")
                        processed += 1
                        status = "completed"
                        message = "COG created successfully"
                        run_report.record("reprojection", "completed", client=client_code,
                                          project_or_job=project, data_type=type_tag,
                                          input_path=src_tif, output_path=output_path,
                                          message=message)
                    else:
                        skipped += 1
                        status = "failed"
                        message = "COG creation returned failure; inspect preceding diagnostic events"
                        run_report.record("reprojection", "failed", client=client_code,
                                          project_or_job=project, data_type=type_tag,
                                          input_path=src_tif, output_path=output_path,
                                          message=message)
                except Exception as e:
                    print(f"Error processing {src_tif}: {str(e)}\n")
                    skipped += 1
                    status = "failed"
                    message = f"{type(e).__name__}: {e}"
                    run_report.exception("reprojection", e, client=client_code,
                                         project_or_job=project, data_type=type_tag,
                                         input_path=src_tif, output_path=output_path)
            else:
                skipped += 1

            writer.writerow({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "input_path": str(input_path),
                "renamed_source_path": str(src_tif),
                "output_file_path": str(output_path) if output_path else "",
                "status": status,
                "message": message,
            })
            report_handle.flush()

    print(f"Report written: {REPORT_CSV}")
    print(f"\nFinished: {processed} COG files created, {already_done} already done, {skipped} skipped")
    run_report.record("run", "completed" if skipped == 0 else "completed_with_issues",
                      message=f"processed={processed}; already_done={already_done}; skipped={skipped}",
                      output_path=REPORT_CSV)
    return 1 if run_report.issue_count else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        run_report.exception("run_fatal", error)
        print(f"Fatal run error. Diagnostics: {run_report.path}")
        run_report.close()
        sys.exit(1)
    print(f"Detailed diagnostics: {run_report.path}")
    run_report.close()
    sys.exit(exit_code)
