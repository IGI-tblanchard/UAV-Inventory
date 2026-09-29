import csv
import time
import traceback
from pathlib import Path

import arcpy

# ---- configuration ----
BASE_DIR = Path(r"P:\IGG\Z_Drive")
STAGING_GDB = Path(r"P:\IGG\Z_Drive\Staging\UAV_LiDAR_Staging.gdb")
PRODUCTION_GDB = Path(r"P:\IGG\Z_Drive\UAV_LiDAR_Mosaics.gdb")
CLIENT_FOLDERS = {
    "CVE": "Cenovus",
    "TOU": "Tourmaline",
    "WCP": "Whitecap",
}
# base_subdir -> (type_tag, mosaic_dataset_name)
TYPE_MAP = {
    "FullFeature": ("DSM", "UAV_DSM"),
    "BareEarth": ("DTM", "UAV_DTM"),
}
REPORT_CSV = Path(r"c:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\5_mosaic_add_report.csv")

MOSAIC_SCOPE = {
    ("Cenovus", "Cenovus_DSM"),
    ("Cenovus", "Cenovus_DTM"),
    ("Tourmaline", "Tourmaline_DSM"),
    ("Tourmaline", "Tourmaline_DTM"),
    ("Whitecap", "Whitecap_DSM"),
    ("Whitecap", "Whitecap_DTM"),
}

arcpy.env.overwriteOutput = True


def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{timestamp}] {message}")


def staging_mosaic_name(client_folder, type_tag):
    return f"{client_folder}_{type_tag}"


def gdb_path_for(client_folder):
    return STAGING_GDB


def iter_output_tifs():
    """Yield (client_code, client_folder, project, type_tag, mosaic_name, tif_path)
    for each reprojected DSM/DTM tif sitting directly in a project-id folder
    (step 4 reprojects in place and leaves the file in the project folder,
    not in a DSM/DTM subfolder)."""
    for client_code, client_folder in CLIENT_FOLDERS.items():
        for base_subdir, (type_tag, _) in TYPE_MAP.items():
            type_root = BASE_DIR / client_folder / "LiDAR" / "UAV" / base_subdir
            if not type_root.is_dir():
                continue
            for project_dir in sorted(type_root.iterdir()):
                if not project_dir.is_dir():
                    continue
                tif_paths = sorted(project_dir.glob("*.tif")) + sorted(project_dir.glob("*.tiff"))
                for tif_path in tif_paths:
                    mosaic_name = staging_mosaic_name(client_folder, type_tag)
                    yield client_code, client_folder, project_dir.name, type_tag, mosaic_name, tif_path


def add_tifs_to_mosaic(mosaic_dataset_path, tif_rows, writer, report_handle, stop_on_error=False):
    """Add each tif to the mosaic dataset. tif_rows is a list of
    (client_code, project, type_tag, tif_path)."""
    added_count = 0
    failed_count = 0

    for client_code, project, type_tag, tif_path in tif_rows:
        status = "added"
        message = ""
        try:
            arcpy.management.AddRastersToMosaicDataset(
                in_mosaic_dataset=mosaic_dataset_path,
                raster_type="Raster Dataset",
                input_path=str(tif_path),
                update_cellsize_ranges="NO_CELL_SIZES",
                update_boundary="NO_BOUNDARY",
                update_overviews="NO_OVERVIEWS",
                duplicate_items_action="EXCLUDE_DUPLICATES",
                calculate_statistics="NO_STATISTICS",
                build_pyramids="NO_PYRAMIDS",
            )
            added_count += 1
        except Exception as add_err:
            failed_count += 1
            status = "failed"
            message = str(add_err)
            log(f"FAILED: {tif_path}")
            log(message)
            if stop_on_error:
                raise RuntimeError(f"Production mosaic update stopped for {mosaic_dataset_path}: {add_err}") from add_err

        writer.writerow({
            "client": client_code,
            "project": project,
            "type": type_tag,
            "tif_path": str(tif_path),
            "mosaic_dataset": mosaic_dataset_path,
            "status": status,
            "message": message,
        })
        report_handle.flush()

    log(f"Per-file add completed for {mosaic_dataset_path}. Added: {added_count}, Failed: {failed_count}")

    try:
        result = arcpy.management.GetCount(mosaic_dataset_path)
        item_count = int(result.getOutput(0))
        log(f"Mosaic item count: {item_count}")
    except Exception:
        log("Mosaic item count: unavailable")

    return added_count, failed_count


def update_footprint_attributes(mosaic_dataset_path, tif_rows):
    """Update ProductName and GroupName for TIFFs in the mosaic catalog."""
    mosaic_path = Path(mosaic_dataset_path)
    catalog_path = mosaic_path.parent / f"AMD_{mosaic_path.name}_CAT"
    if not arcpy.Exists(str(catalog_path)):
        log(f"Footprint catalog not found, skipping: {catalog_path}")
        return 0

    field_names = {field.name.upper() for field in arcpy.ListFields(str(catalog_path))}
    required_fields = {"PRODUCTNAME", "GROUPNAME"}
    missing_fields = required_fields - field_names
    if missing_fields:
        log(f"Footprint fields missing from {catalog_path}: {sorted(missing_fields)}")
        return 0

    path_values = {
        str(tif_path).replace("/", "\\").casefold(): (str(tif_path), str(tif_path.parent))
        for _, _, _, tif_path in tif_rows
    }
    updated_count = 0
    matched_paths = set()
    with arcpy.da.UpdateCursor(str(catalog_path), ["ProductName", "GroupName"]) as cursor:
        for row in cursor:
            product_name = row[0]
            if not product_name:
                continue
            normalized_product = str(product_name).replace("/", "\\").casefold()
            path_value = path_values.get(normalized_product)
            if path_value is None:
                continue
            row[0], row[1] = path_value
            cursor.updateRow(row)
            matched_paths.add(normalized_product)
            updated_count += 1

    unmatched_count = len(set(path_values) - matched_paths)
    log(f"Footprint attributes updated: {updated_count} rows for {catalog_path}")
    if unmatched_count:
        log(f"Footprint rows not matched: {unmatched_count} for {catalog_path}")
    return updated_count


def build_production_overviews(mosaic_dataset_path):
    """Define and regenerate all overviews for a production mosaic dataset."""
    try:
        arcpy.management.BuildOverviews(
            in_mosaic_dataset=mosaic_dataset_path,
            define_missing_overviews="DEFINE_MISSING_OVERVIEWS",
            generate_missing_overviews="GENERATE_MISSING_OVERVIEWS",
            regenerate_existing_overviews="REGENERATE_EXISTING_OVERVIEWS",
        )
        log(f"Production overviews generated: {mosaic_dataset_path}")
    except Exception as overview_error:
        raise RuntimeError(
            f"Production overview generation stopped for {mosaic_dataset_path}: {overview_error}"
        ) from overview_error


def main():
    start_time = time.perf_counter()
    log("Starting Add TIFFs To DSM/DTM Mosaic process")

    # group discovered tifs by (client_folder, mosaic_name)
    grouped = {}
    for client_code, client_folder, project, type_tag, mosaic_name, tif_path in iter_output_tifs():
        key = (client_folder, mosaic_name)
        grouped.setdefault(key, []).append((client_code, project, type_tag, tif_path))

    if not grouped:
        raise ValueError(f"No tifs found under {BASE_DIR}")

    total_added = 0
    total_failed = 0

    with open(REPORT_CSV, "w", newline="") as report_handle:
        writer = csv.DictWriter(
            report_handle,
            fieldnames=["client", "project", "type", "tif_path", "mosaic_dataset", "status", "message"],
        )
        writer.writeheader()

        for target_name, gdb_path in (("staging", STAGING_GDB), ("production", PRODUCTION_GDB)):
            log(f"Starting {target_name} geodatabase update: {gdb_path}")
            if not arcpy.Exists(str(gdb_path)):
                log(f"Geodatabase not found, skipping: {gdb_path}")
                if target_name == "production":
                    raise FileNotFoundError(f"Production geodatabase not found: {gdb_path}")
                continue

            arcpy.env.workspace = str(gdb_path)

            for client_folder in CLIENT_FOLDERS.values():
                for type_tag, _ in TYPE_MAP.values():
                    mosaic_name = staging_mosaic_name(client_folder, type_tag)
                    if (client_folder, mosaic_name) not in MOSAIC_SCOPE:
                        continue
                    tif_rows = grouped.get((client_folder, mosaic_name))
                    if not tif_rows:
                        log(f"No tifs found for {client_folder} / {mosaic_name}, skipping")
                        if target_name == "production":
                            mosaic_dataset_path = str(gdb_path / mosaic_name)
                            if not arcpy.Exists(mosaic_dataset_path):
                                raise FileNotFoundError(
                                    f"Production mosaic dataset not found: {mosaic_dataset_path}"
                                )
                            build_production_overviews(mosaic_dataset_path)
                        continue

                    mosaic_dataset_path = str(gdb_path / mosaic_name)
                    if not arcpy.Exists(mosaic_dataset_path):
                        log(f"Mosaic dataset not found, skipping: {mosaic_dataset_path}")
                        if target_name == "production":
                            raise FileNotFoundError(f"Production mosaic dataset not found: {mosaic_dataset_path}")
                        continue

                    log(f"Found {len(tif_rows)} tifs for {mosaic_dataset_path}")
                    added_count, failed_count = add_tifs_to_mosaic(
                        mosaic_dataset_path,
                        tif_rows,
                        writer,
                        report_handle,
                        stop_on_error=target_name == "production",
                    )
                    update_footprint_attributes(mosaic_dataset_path, tif_rows)
                    if target_name == "production":
                        build_production_overviews(mosaic_dataset_path)
                    total_added += added_count
                    total_failed += failed_count

    log(f"Report written: {REPORT_CSV}")
    elapsed_seconds = time.perf_counter() - start_time
    log(f"Finished: {total_added} added, {total_failed} failed, in {elapsed_seconds:.2f} seconds")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("Error while adding TIFFs to mosaic datasets")
        log(str(exc))
        log(traceback.format_exc())
