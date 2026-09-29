import csv
import math
import re
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

# ---- configuration ----
BASE_DIR = Path(r"P:\IGG\Z_Drive")
CLIENT_FOLDERS = {
    "CVE": "Cenovus",
    "TOU": "Tourmaline",
    "WCP": "Whitecap",
}
REPORT_CSV = Path(r"c:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\4_reproject_report.csv")
RENAME_REPORT_CSV = Path(r"c:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\4_reproject_dsm_dtm_ouput.csv")
DRY_RUN = False

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

# (client_folder, type_tag) pairs that must always be regenerated, bypassing
# the existing-output check — used for one-off fixes without touching other clients.
# Left empty: the old bad Tourmaline DSM COGs were already deleted manually, so a
# resumed run can safely skip whatever's already been (re)done with the fix below.
FORCE_REPROCESS_SCOPE = set()

# Float32 max — the sentinel some UAV software burns in for out-of-footprint fill pixels.
FALLBACK_NODATA_SENTINEL = 3.4028235e+38
# Any pixel beyond this is not a real elevation — catches leftover sentinel/blended artifacts post-warp.
SENTINEL_CLEANUP_THRESHOLD = 100000.0

TARGET_CRS_CODE = "EPSG:2955"  # NAD83(CSRS) UTM Zone 11N
resampling = gdal.GRA_Bilinear

# Enable exceptions for GDAL
gdal.UseExceptions()

# Suppress .aux.xml sidecar creation entirely — statistics are embedded
# directly into the GeoTIFF/COG internal metadata via ComputeStatistics().
gdal.SetConfigOption('GDAL_PAM_ENABLED', 'NO')

# Increase GDAL's internal block cache to 1 GB.
gdal.SetConfigOption('GDAL_CACHEMAX', '1024')

# Multi-threaded compression for all GDAL operations.
gdal.SetConfigOption('GDAL_NUM_THREADS', 'ALL_CPUS')


def create_cog_from_tif(src_tif_path, output_path, target_crs_code):
    """
    Reproject a tif to target CRS and create Cloud Optimized GeoTIFF (COG).
    Same pipeline/settings as LiDAR Updates Real/5_reproject_DEM_v4.py.
    """
    try:
        src_ds = gdal.Open(str(src_tif_path))
        if not src_ds:
            print(f"ERROR: Could not open raster dataset: {src_tif_path}")
            return False

        src_crs = osr.SpatialReference(wkt=src_ds.GetProjection())
        src_crs_name = src_crs.GetAttrValue('PROJCS') or src_crs.GetAttrValue('GEOGCS') or 'UNKNOWN'

        # Detect source NoData — prevents internal mask band creation during warp.
        src_nodata = src_ds.GetRasterBand(1).GetNoDataValue()
        if src_nodata is None:
            print(f"WARNING: no NoData tag on source, using fallback sentinel: {src_tif_path}")
            src_nodata = FALLBACK_NODATA_SENTINEL

        print(f"REPROJECT: {src_tif_path}")
        print(f"Input CRS: {src_crs_name} -- Output CRS: {target_crs_code}")

        # Compute output dimensions without performing the warp, to size overviews.
        dst_crs = osr.SpatialReference()
        dst_crs.ImportFromEPSG(int(target_crs_code.split(':')[1]))
        vrt_ds = gdal.AutoCreateWarpedVRT(src_ds, None, dst_crs.ExportToWkt(), resampling)
        if vrt_ds is None:
            print(f"ERROR: Could not compute output dimensions for: {src_tif_path}")
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
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
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
    for client_code, client_folder in CLIENT_FOLDERS.items():
        for type_tag, base_subdir in (("DSM", "FullFeature"), ("DTM", "BareEarth")):
            type_root = BASE_DIR / client_folder / "LiDAR" / "UAV" / base_subdir
            if not type_root.is_dir():
                continue
            for project_dir in sorted(type_root.iterdir()):
                if not project_dir.is_dir():
                    continue
                source_type_dir = project_dir / type_tag
                if not source_type_dir.is_dir():
                    continue
                tif_paths = sorted(source_type_dir.glob("*.tif")) + sorted(source_type_dir.glob("*.tiff"))
                for tif_path in tif_paths:
                    yield client_code, project_dir.name, type_tag, tif_path, project_dir


def expected_tif_name(tif_path, type_tag):
    """Return the canonical name for a source TIFF, or None if its date is invalid."""
    stem = tif_path.stem
    canonical_match = CANONICAL_NAME_PATTERN.fullmatch(stem)
    if canonical_match:
        day = canonical_match.group("day")
        month = canonical_match.group("month")
        year = canonical_match.group("year")
        suffix = canonical_match.group("suffix")
    else:
        date_match = next((pattern.search(stem) for pattern in DATE_NAME_PATTERNS if pattern.search(stem)), None)
        if not date_match:
            return None
        month = MONTHS.get(date_match.group("month").lower())
        if month is None:
            return None
        day = date_match.group("day").zfill(2)
        year = date_match.group("year")
        suffix = date_match.group("suffix") or ""
        if date_match.group("embedded_type"):
            suffix = re.sub(r"^[- _]+", "", suffix)
            suffix = f"-{suffix}" if suffix else ""

    try:
        day_number = int(day)
        month_number = int(month)
        if not 1 <= day_number <= 31 or not 1 <= month_number <= 12:
            return None
    except ValueError:
        return None

    return f"{day}-{month}-{year}-{type_tag}{suffix}{tif_path.suffix}"


def preview_or_apply_renames():
    """Preview or apply canonical DSM/DTM source names before reprojection."""
    rows = []
    rename_plans = []
    planned_targets = {}

    for client_code, project, type_tag, tif_path, _ in iter_source_tifs():
        expected_name = expected_tif_name(tif_path, type_tag)
        if expected_name is None:
            rows.append({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "input_path": str(tif_path),
                "output_path": "",
                "status": "invalid_name",
                "message": "Could not find a valid date in the filename",
            })
            continue

        target_path = tif_path.with_name(expected_name)
        if target_path == tif_path:
            rows.append({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "input_path": str(tif_path),
                "output_path": str(target_path),
                "status": "already_correct",
                "message": "",
            })
            continue

        collision_message = ""
        target_key = str(target_path).casefold()
        if target_path.exists():
            collision_message = "Target filename already exists"
        elif target_key in planned_targets:
            collision_message = f"Target duplicates {planned_targets[target_key]}"

        if collision_message:
            rows.append({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "input_path": str(tif_path),
                "output_path": str(target_path),
                "status": "collision",
                "message": collision_message,
            })
            continue

        planned_targets[target_key] = str(tif_path)
        rename_plans.append((tif_path, target_path))
        rows.append({
            "client": client_code,
            "project": project,
            "type": type_tag,
            "input_path": str(tif_path),
            "output_path": str(target_path),
            "status": "preview_rename" if DRY_RUN else "renamed",
            "message": "",
        })

    if not DRY_RUN:
        for source_path, target_path in rename_plans:
            source_path.rename(target_path)

    with open(RENAME_REPORT_CSV, "w", newline="") as report_handle:
        writer = csv.DictWriter(
            report_handle,
            fieldnames=["client", "project", "type", "input_path", "output_path", "status", "message"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Rename report written: {RENAME_REPORT_CSV}")
    print(f"Rename preview: {sum(row['status'] == 'preview_rename' for row in rows)} files")
    print(f"Already correct: {sum(row['status'] == 'already_correct' for row in rows)} files")
    print(f"Collisions: {sum(row['status'] == 'collision' for row in rows)} files")
    print(f"Invalid names: {sum(row['status'] == 'invalid_name' for row in rows)} files")


def main():
    preview_or_apply_renames()
    if DRY_RUN:
        print("DRY_RUN is enabled; stopping after rename preview.")
        return

    processed = 0
    skipped = 0
    already_done = 0

    with open(REPORT_CSV, "w", newline="") as report_handle:
        writer = csv.DictWriter(report_handle, fieldnames=["client", "project", "type", "input_path", "output_file_path", "status"])
        writer.writeheader()

        for client_code, project, type_tag, src_tif, output_dir in iter_source_tifs():
            output_path = output_dir / src_tif.name
            force_reprocess = (CLIENT_FOLDERS[client_code], type_tag) in FORCE_REPROCESS_SCOPE

            if not force_reprocess and is_valid_existing_output(output_path):
                print(f"Already reprojected, skipping: {output_path}\n")
                already_done += 1
                status = "skipped_already_exists"
            else:
                if output_path.exists():
                    output_path.unlink()  # remove partial/corrupt file, or force a clean rebuild
                try:
                    if create_cog_from_tif(src_tif, output_path, TARGET_CRS_CODE):
                        print(f"Done: {output_path}\n")
                        processed += 1
                        status = "reprocessed_nodata_fix" if force_reprocess else "completed"
                    else:
                        skipped += 1
                        status = "skipped"
                except Exception as e:
                    print(f"Error processing {src_tif}: {str(e)}\n")
                    skipped += 1
                    status = "skipped"

            writer.writerow({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "input_path": str(src_tif),
                "output_file_path": str(output_path),
                "status": status,
            })
            report_handle.flush()

    print(f"Report written: {REPORT_CSV}")
    print(f"\nFinished: {processed} COG files created, {already_done} already done, {skipped} skipped")


if __name__ == "__main__":
    main()
