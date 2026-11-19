import csv
import re
import stat
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from mosaic_maintenance_state import MaintenanceState, force_maintenance, state_directory
from task_run_report import RunReport

run_report = RunReport(__file__)
print(f"Detailed diagnostics: {run_report.path}")
try:
    import arcpy
except Exception as error:
    run_report.exception("import_arcpy", error)
    run_report.close()
    raise

# ---- configuration ----
BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client")
STAGING_GDB = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Staging.gdb")
PRODUCTION_GDB = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Geodatabase\UAV_Mosaics.gdb")
CLIENT_FOLDERS = {
    "CVE": "Cenovus",
    "TOU": "Tourmaline",
    "WCP": "Whitecap",
}
# base_subdir -> type_tag
TYPE_MAP = {
    "FullFeature": "DSM",
    "BareEarth": "DTM",
}
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\5_mosaic_add_report.csv")

MOSAIC_SCOPE = {
    ("Cenovus", "Cenovus_DSM"),
    ("Cenovus", "Cenovus_DTM"),
    ("Tourmaline", "Tourmaline_DSM"),
    ("Tourmaline", "Tourmaline_DTM"),
    ("Whitecap", "Whitecap_DSM"),
    ("Whitecap", "Whitecap_DTM"),
}

try:
    arcpy.env.overwriteOutput = True
except Exception as error:
    run_report.exception("configure_arcpy_environment", error)
    run_report.close()
    raise


def log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{timestamp}] {message}", flush=True)


@contextmanager
def timed_operation(stage, *, input_path="", output_path="", **context):
    """Record a flushed start and elapsed finish without counting failures twice."""
    target = f"; mosaic={output_path}" if output_path else ""
    source = f"; input={input_path}" if input_path else ""
    log(f"START {stage}{target}{source}")
    run_report.record(stage, "started", input_path=input_path, output_path=output_path,
                      message="Timing started", **context)
    started = time.perf_counter()
    outcome = {"error": ""}
    try:
        yield outcome
    except BaseException as error:
        elapsed = time.perf_counter() - started
        log(f"FINISH {stage}{target}{source}; outcome=error; elapsed_seconds={elapsed:.2f}; {type(error).__name__}: {error}")
        run_report.record(stage, "info", input_path=input_path, output_path=output_path,
                          message="Timing finished; outcome=error",
                          details=f"elapsed_seconds={elapsed:.2f}; error={type(error).__name__}: {error}",
                          **context)
        raise
    else:
        elapsed = time.perf_counter() - started
        status = "info" if outcome["error"] else "completed"
        result = "error" if outcome["error"] else "completed"
        detail = f"elapsed_seconds={elapsed:.2f}"
        if outcome["error"]:
            detail += f"; error={outcome['error']}"
        log(f"FINISH {stage}{target}{source}; outcome={result}; {detail}")
        run_report.record(stage, status, input_path=input_path, output_path=output_path,
                          message=f"Timing finished; outcome={result}", details=detail, **context)


def staging_mosaic_name(client_folder, type_tag):
    return f"{client_folder}_{type_tag}"


def iter_output_tifs(discovery_uncertain):
    """Yield (client_code, client_folder, project, type_tag, mosaic_name, tif_path)
    for each reprojected DSM/DTM tif sitting directly in a project-id folder
    (step 4 reprojects in place and leaves the file in the project folder,
    not in a DSM/DTM subfolder)."""
    # Unlike Path.is_dir/glob, stat and iterdir propagate access and network
    # errors rather than silently presenting them as an empty input set.
    if not stat.S_ISDIR(BASE_DIR.stat().st_mode):
        raise NotADirectoryError(f"Input base directory is not a directory: {BASE_DIR}")
    for client_code, client_folder in CLIENT_FOLDERS.items():
        for base_subdir, type_tag in TYPE_MAP.items():
            mosaic_name = staging_mosaic_name(client_folder, type_tag)
            if (client_folder, mosaic_name) not in MOSAIC_SCOPE:
                continue
            type_root = BASE_DIR / client_folder / "LiDAR" / "UAV" / base_subdir
            try:
                root_mode = type_root.stat().st_mode
            except FileNotFoundError:
                discovery_uncertain.add((client_folder, mosaic_name))
                message = "Input directory is missing or inaccessible; discovery is uncertain"
                log(f"{message}: {type_root}")
                run_report.record("discover_mosaic_inputs", "warning", client=client_folder,
                                  data_type=type_tag, input_path=type_root, message=message)
                continue
            if not stat.S_ISDIR(root_mode):
                raise NotADirectoryError(f"Input directory is not a directory: {type_root}")
            for project_dir in sorted(type_root.iterdir()):
                if not stat.S_ISDIR(project_dir.stat().st_mode):
                    continue
                tif_paths = sorted(
                    path
                    for path in project_dir.iterdir()
                    if path.suffix.casefold() in {".tif", ".tiff"}
                    and re.search(rf"-{re.escape(type_tag)}(?:[-_ ]|$)", path.stem, re.IGNORECASE)
                    and stat.S_ISREG(path.stat().st_mode)
                )
                for tif_path in tif_paths:
                    yield client_code, client_folder, project_dir.name, type_tag, mosaic_name, tif_path


def normalize_raster_path(path):
    return str(path).replace("/", "\\").casefold()


def mosaic_item_paths(mosaic_dataset_path):
    """Export and read each mosaic item's actual source raster path by OID."""
    mosaic_path = Path(mosaic_dataset_path)
    catalog_path = mosaic_path.parent / f"AMD_{mosaic_path.name}_CAT"
    with timed_operation("inspect_mosaic_catalog", output_path=mosaic_dataset_path):
        if not arcpy.Exists(str(catalog_path)):
            raise FileNotFoundError(f"Mosaic footprint catalog not found: {catalog_path}")

        item_count = int(arcpy.management.GetCount(mosaic_dataset_path).getOutput(0))
    if item_count == 0:
        message = "Mosaic contains no items; skipping path export and treating existing paths as empty"
        log(f"{message}: {mosaic_dataset_path}")
        run_report.record("export_existing_mosaic_paths", "skipped",
                          output_path=mosaic_dataset_path, message=message)
        return {}

    with timed_operation("create_mosaic_path_table_name", output_path=mosaic_dataset_path):
        output_table = arcpy.CreateUniqueName(f"uav_paths_{mosaic_path.name}", str(mosaic_path.parent))
    try:
        with timed_operation("export_mosaic_paths", input_path=output_table, output_path=mosaic_dataset_path):
            arcpy.management.ExportMosaicDatasetPaths(
                in_mosaic_dataset=mosaic_dataset_path,
                out_table=output_table,
                export_mode="ALL",
                types_of_paths="RASTER",
            )
        with timed_operation("read_mosaic_paths", input_path=output_table, output_path=mosaic_dataset_path):
            output_fields = {field.name.casefold(): field.name for field in arcpy.ListFields(output_table)}
            source_oid_field = output_fields.get("sourceoid")
            path_field = output_fields.get("path")
            if not source_oid_field or not path_field:
                raise RuntimeError(f"Exported mosaic path table is missing SourceOID/Path fields: {output_table}")

            item_paths = {}
            with arcpy.da.SearchCursor(output_table, [source_oid_field, path_field]) as cursor:
                for source_oid, source_path in cursor:
                    if source_path:
                        item_paths.setdefault(source_oid, str(source_path))
        return item_paths
    finally:
        with timed_operation("cleanup_mosaic_paths", input_path=output_table, output_path=mosaic_dataset_path):
            if arcpy.Exists(output_table):
                arcpy.management.Delete(output_table)


def add_tifs_to_mosaic(mosaic_dataset_path, tif_rows, writer, report_handle, maintenance_store=None):
    """Skip catalog paths and add each remaining TIFF independently."""
    added_count = 0
    failed_count = 0
    try:
        with timed_operation("discover_existing_mosaic_paths", output_path=mosaic_dataset_path):
            item_paths = mosaic_item_paths(mosaic_dataset_path)
    except Exception as path_err:
        if maintenance_store is not None:
            maintenance_store.mark_pending(mosaic_dataset_path, "production path inspection failed")
        message = f"Could not read existing mosaic source paths: {path_err}"
        log(f"Cannot safely check {mosaic_dataset_path}; skipping its TIFFs")
        run_report.exception("export_existing_mosaic_paths", path_err,
                             output_path=mosaic_dataset_path)
        for client_code, project, type_tag, tif_path in tif_rows:
            writer.writerow({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "tif_path": str(tif_path),
                "mosaic_dataset": mosaic_dataset_path,
                "status": "check_failed",
                "message": message,
            })
        report_handle.flush()
        return added_count, len(tif_rows)

    existing_paths = {normalize_raster_path(path) for path in item_paths.values()}
    pending_rows = []
    successful_rows = []

    for row in tif_rows:
        client_code, project, type_tag, tif_path = row
        if normalize_raster_path(tif_path) in existing_paths:
            writer.writerow({
                "client": client_code,
                "project": project,
                "type": type_tag,
                "tif_path": str(tif_path),
                "mosaic_dataset": mosaic_dataset_path,
                "status": "already_present",
                "message": "Source path already exists in mosaic catalog",
            })
            run_report.record("duplicate_check", "skipped", client=client_code,
                              project_or_job=project, data_type=type_tag,
                              input_path=tif_path, output_path=mosaic_dataset_path,
                              message="Source path already exists in mosaic")
        else:
            pending_rows.append(row)
    report_handle.flush()

    if pending_rows:
        log(f"Adding {len(pending_rows)} new TIFF(s) individually to {mosaic_dataset_path}")
        for client_code, project, type_tag, tif_path in pending_rows:
            if maintenance_store is not None:
                # This write must succeed before every production add attempt.
                # Keep it outside the add exception handler so persistence errors
                # abort the run instead of being mistaken for ArcGIS failures.
                maintenance_store.mark_pending(mosaic_dataset_path, "production add attempted")
            try:
                with timed_operation("add_raster_timing", client=client_code, project_or_job=project,
                                     data_type=type_tag, input_path=tif_path, output_path=mosaic_dataset_path):
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
            except Exception as add_err:
                failed_count += 1
                error_message = arcpy.GetMessages(2) or str(add_err)
                log(f"FAILED: {tif_path}")
                log(error_message)
                run_report.exception("add_raster_to_mosaic", add_err, client=client_code,
                                     project_or_job=project, data_type=type_tag,
                                     input_path=tif_path, output_path=mosaic_dataset_path,
                                     details=error_message)
                writer.writerow({
                    "client": client_code,
                    "project": project,
                    "type": type_tag,
                    "tif_path": str(tif_path),
                    "mosaic_dataset": mosaic_dataset_path,
                    "status": "failed",
                    "message": error_message,
                })
            else:
                successful_rows.append((client_code, project, type_tag, tif_path))
            report_handle.flush()

        if successful_rows:
            verify_error_message = ""
            try:
                with timed_operation("verify_mosaic_paths_timing", output_path=mosaic_dataset_path):
                    item_paths = mosaic_item_paths(mosaic_dataset_path)
            except Exception as verify_err:
                item_paths = None
                verify_error_message = str(verify_err)
                log(f"Could not verify added TIFFs in {mosaic_dataset_path}: {verify_err}")
                run_report.exception("verify_mosaic_additions", verify_err,
                                     output_path=mosaic_dataset_path)

            with timed_operation("verify_mosaic_items_timing", output_path=mosaic_dataset_path) as timing:
                verified_paths = (
                    {normalize_raster_path(path) for path in item_paths.values()}
                    if item_paths is not None
                    else set()
                )
                for client_code, project, type_tag, tif_path in successful_rows:
                    if item_paths is None:
                        status = "verification_failed"
                        message = f"Add tool succeeded, but catalog verification failed: {verify_error_message}"
                        failed_count += 1
                    elif normalize_raster_path(tif_path) in verified_paths:
                        status = "added"
                        message = ""
                        added_count += 1
                    else:
                        status = "verification_failed"
                        message = "Add tool succeeded, but source path is absent from the mosaic catalog"
                        failed_count += 1
                    if status == "verification_failed":
                        timing["error"] = "One or more mosaic items could not be verified"
                        run_report.record("verify_mosaic_addition_issue", "warning", client=client_code,
                                          project_or_job=project, data_type=type_tag,
                                          input_path=tif_path, output_path=mosaic_dataset_path,
                                          message=message)
                    writer.writerow({
                        "client": client_code,
                        "project": project,
                        "type": type_tag,
                        "tif_path": str(tif_path),
                        "mosaic_dataset": mosaic_dataset_path,
                        "status": status,
                        "message": message,
                    })
                    run_report.record("verify_mosaic_addition", status, client=client_code,
                                      project_or_job=project, data_type=type_tag,
                                      input_path=tif_path, output_path=mosaic_dataset_path,
                                      message=message or "Source path found in mosaic catalog")
                report_handle.flush()
    else:
        log(f"No new TIFFs to add to {mosaic_dataset_path}")

    if item_paths is not None:
        try:
            with timed_operation("update_catalog_attributes_timing", output_path=mosaic_dataset_path):
                update_footprint_attributes(mosaic_dataset_path, item_paths)
        except Exception as update_error:
            log(f"Failed updating mosaic Path/FolderPath fields: {update_error}")
            run_report.exception("update_catalog_attributes", update_error,
                                 output_path=mosaic_dataset_path,
                                 details=arcpy.GetMessages(2))
            failed_count += 1
    else:
        log(f"Skipped Path/FolderPath update because mosaic paths could not be read: {mosaic_dataset_path}")

    report_handle.flush()
    log(f"Add completed for {mosaic_dataset_path}. Added: {added_count}, Failed: {failed_count}, Already present: {len(tif_rows) - len(pending_rows)}")

    try:
        with timed_operation("final_mosaic_count_timing", output_path=mosaic_dataset_path):
            result = arcpy.management.GetCount(mosaic_dataset_path)
            item_count = int(result.getOutput(0))
            log(f"Mosaic item count: {item_count}")
    except Exception:
        log("Mosaic item count: unavailable")

    return added_count, failed_count


def update_footprint_attributes(mosaic_dataset_path, item_paths):
    """Store each item's source path and parent folder in Path/FolderPath."""
    mosaic_path = Path(mosaic_dataset_path)
    catalog_path = mosaic_path.parent / f"AMD_{mosaic_path.name}_CAT"
    if not arcpy.Exists(str(catalog_path)):
        log(f"Footprint catalog not found, skipping: {catalog_path}")
        return 0

    field_names = {field.name.upper() for field in arcpy.ListFields(str(catalog_path))}
    required_fields = {"PATH", "FOLDERPATH"}
    missing_fields = required_fields - field_names
    if missing_fields:
        log(f"Footprint fields missing from {catalog_path}: {sorted(missing_fields)}")
        return 0

    oid_field = arcpy.Describe(str(catalog_path)).OIDFieldName
    updated_count = 0
    with arcpy.da.UpdateCursor(str(catalog_path), [oid_field, "Path", "FolderPath"]) as cursor:
        for row in cursor:
            source_path = item_paths.get(row[0])
            if source_path is None:
                continue
            path_value = (source_path, str(Path(source_path).parent))
            if (row[1], row[2]) == path_value:
                continue
            row[1], row[2] = path_value
            cursor.updateRow(row)
            updated_count += 1

    log(f"Footprint attributes updated: {updated_count} rows for {catalog_path}")
    run_report.record("update_catalog_attributes", "completed", output_path=catalog_path,
                      message=f"Updated {updated_count} Path/FolderPath row(s)")
    return updated_count


def build_production_overviews(mosaic_dataset_path):
    """Define and regenerate all overviews for a production mosaic dataset."""
    try:
        # Items added with NO_CELL_SIZES have blank MinPS/MaxPS and do not draw.
        with timed_operation("calculate_cell_size_ranges", output_path=mosaic_dataset_path):
            arcpy.management.CalculateCellSizeRanges(
                in_mosaic_dataset=mosaic_dataset_path,
                where_clause="Category = 1 AND (MinPS IS NULL OR MaxPS IS NULL OR (MinPS = 0 AND MaxPS = 0))",
            )
        with timed_operation("build_overviews", output_path=mosaic_dataset_path):
            arcpy.management.BuildOverviews(
                in_mosaic_dataset=mosaic_dataset_path,
                define_missing_tiles="DEFINE_MISSING_TILES",
                generate_overviews="GENERATE_OVERVIEWS",
                generate_missing_images="GENERATE_MISSING_IMAGES",
                regenerate_stale_images="REGENERATE_STALE_IMAGES",
            )
        log(f"Production overviews generated: {mosaic_dataset_path}")
        with timed_operation("calculate_statistics", output_path=mosaic_dataset_path):
            arcpy.management.CalculateStatistics(
                in_raster_dataset=mosaic_dataset_path,
                x_skip_factor=10,
                y_skip_factor=10,
                ignore_values=[-9999],
            )
        run_report.record("build_production_overviews", "completed", output_path=mosaic_dataset_path,
                  message="Production overview build completed")
        return True
    except Exception as overview_error:
        run_report.exception("build_production_overviews", overview_error,
                             output_path=mosaic_dataset_path,
                             details=arcpy.GetMessages(2))
        log(f"Production overview generation failed for {mosaic_dataset_path}; continuing with other mosaics")
        return False


def main():
    store = MaintenanceState(state_directory())
    with store.lock():
        force = force_maintenance()
        log(f"Maintenance state directory: {store.directory}; UAV_FORCE_MAINTENANCE={force}")
        if force:
            log("Forced maintenance enabled for every existing scoped production mosaic")
        return run_locked(store, force)


def run_locked(store, force):
    start_time = time.perf_counter()
    log("Starting Add TIFFs To DSM/DTM Mosaic process")
    run_report.record("run", "started", message="LiDAR mosaic update started",
                      details=f"BASE_DIR={BASE_DIR}; staging={STAGING_GDB}; production={PRODUCTION_GDB}")

    # Bootstrap or force every existing scoped production mosaic before input
    # discovery, even when there are no candidates for a mosaic.
    with timed_operation("discover_geodatabase_timing", output_path=PRODUCTION_GDB):
        production_exists = arcpy.Exists(str(PRODUCTION_GDB))
    if not production_exists:
        raise FileNotFoundError(f"Production geodatabase not found: {PRODUCTION_GDB}")
    production_mosaics = set()
    for client_folder, mosaic_name in sorted(MOSAIC_SCOPE):
        mosaic_dataset_path = str(PRODUCTION_GDB / mosaic_name)
        with timed_operation("check_mosaic_exists_timing", output_path=mosaic_dataset_path):
            mosaic_exists = arcpy.Exists(mosaic_dataset_path)
        if mosaic_exists:
            pending = store.ensure(mosaic_dataset_path, force=force)
            production_mosaics.add(mosaic_dataset_path)
            log(f"Production maintenance state: {mosaic_dataset_path}; pending={pending}; forced={force}")
        else:
            message = "Scoped production mosaic does not exist or is inaccessible; state could not be initialized"
            log(f"{message}: {mosaic_dataset_path}")
            run_report.record("check_mosaic_dataset", "warning", client=client_folder,
                              output_path=mosaic_dataset_path, message=message)

    # group discovered tifs by (client_folder, mosaic_name)
    grouped = {}
    discovery_uncertain = set()
    with timed_operation("discover_mosaic_inputs_timing", input_path=BASE_DIR):
        for client_code, client_folder, project, type_tag, mosaic_name, tif_path in iter_output_tifs(discovery_uncertain):
            key = (client_folder, mosaic_name)
            grouped.setdefault(key, []).append((client_code, project, type_tag, tif_path))

    if not grouped:
        log("No candidate TIFFs found; checking for pending production maintenance")
        run_report.record("discover_inputs", "info", input_path=BASE_DIR,
                          message="No candidate TIFFs found; pending maintenance will still run")

    total_added = 0
    total_failed = 0
    production_failed = set()
    production_uncertain = {str(PRODUCTION_GDB / name) for _, name in discovery_uncertain}

    with open(REPORT_CSV, "w", newline="") as report_handle:
        writer = csv.DictWriter(
            report_handle,
            fieldnames=["client", "project", "type", "tif_path", "mosaic_dataset", "status", "message"],
        )
        writer.writeheader()

        for target_name, gdb_path in (("staging", STAGING_GDB), ("production", PRODUCTION_GDB)):
            log(f"Starting {target_name} geodatabase update: {gdb_path}")
            with timed_operation("discover_geodatabase_timing", output_path=gdb_path):
                gdb_exists = arcpy.Exists(str(gdb_path))
            if not gdb_exists:
                log(f"Geodatabase not found, skipping: {gdb_path}")
                run_report.record("check_geodatabase", "failed", issue_category="path_or_network",
                                  output_path=gdb_path, message="Geodatabase does not exist or is inaccessible")
                if target_name == "production":
                    raise FileNotFoundError(f"Production geodatabase not found: {gdb_path}")
                continue

            arcpy.env.workspace = str(gdb_path)

            for client_folder in CLIENT_FOLDERS.values():
                for type_tag in TYPE_MAP.values():
                    mosaic_name = staging_mosaic_name(client_folder, type_tag)
                    if (client_folder, mosaic_name) not in MOSAIC_SCOPE:
                        continue
                    tif_rows = grouped.get((client_folder, mosaic_name))
                    mosaic_dataset_path = str(gdb_path / mosaic_name)

                    if not tif_rows:
                        log(f"No tifs found for {client_folder} / {mosaic_name}, skipping")
                        run_report.record("discover_mosaic_inputs", "info", client=client_folder,
                                          data_type=type_tag, output_path=mosaic_dataset_path,
                                          message="No candidate TIFFs for this mosaic on this run; skipped")
                        continue

                    with timed_operation("check_mosaic_exists_timing", output_path=mosaic_dataset_path):
                        mosaic_exists = arcpy.Exists(mosaic_dataset_path)
                    if not mosaic_exists:
                        log(f"Mosaic dataset not found, skipping: {mosaic_dataset_path}")
                        run_report.record("check_mosaic_dataset", "failed", client=client_folder,
                                          data_type=type_tag, output_path=mosaic_dataset_path,
                                          message="Mosaic dataset does not exist or is inaccessible")
                        if target_name == "production":
                            raise FileNotFoundError(f"Production mosaic dataset not found: {mosaic_dataset_path}")
                        continue

                    if target_name == "production":
                        if mosaic_dataset_path not in production_mosaics:
                            store.ensure(mosaic_dataset_path, force=force)
                            production_mosaics.add(mosaic_dataset_path)

                    log(f"Found {len(tif_rows)} tifs for {mosaic_dataset_path}")
                    with timed_operation("mosaic_add_stage", client=client_folder, data_type=type_tag,
                                         output_path=mosaic_dataset_path) as timing:
                        added_count, failed_count = add_tifs_to_mosaic(
                            mosaic_dataset_path,
                            tif_rows,
                            writer,
                            report_handle,
                            maintenance_store=store if target_name == "production" else None,
                        )
                        if failed_count:
                            timing["error"] = f"{failed_count} item or attribute update failure(s)"
                    total_added += added_count
                    total_failed += failed_count
                    if target_name == "production" and failed_count:
                        # Includes failed initial path checks, even when no add was attempted.
                        store.mark_pending(mosaic_dataset_path, "production add or path verification failed")
                        production_failed.add(mosaic_dataset_path)

            if target_name == "production":
                for mosaic_dataset_path in sorted(production_mosaics):
                    if mosaic_dataset_path in production_uncertain:
                        store.mark_pending(mosaic_dataset_path, "production input discovery uncertain")
                    if not store.is_pending(mosaic_dataset_path):
                        log(f"Production maintenance not pending; skipping: {mosaic_dataset_path}")
                        continue
                    with timed_operation("production_overview_stage", output_path=mosaic_dataset_path) as timing:
                        if not build_production_overviews(mosaic_dataset_path):
                            timing["error"] = "Production overview processing failed"
                            total_failed += 1
                            continue
                    if mosaic_dataset_path in production_failed or mosaic_dataset_path in production_uncertain:
                        log(f"Production maintenance remains pending due to add, verification, or discovery uncertainty: {mosaic_dataset_path}")
                        continue
                    try:
                        store.clear(mosaic_dataset_path)
                    except Exception as clear_error:
                        # A write may have replaced the record before reporting an
                        # error (e.g. during a directory flush). Restore pending.
                        try:
                            store.mark_pending(mosaic_dataset_path, "maintenance state clear failed")
                        except Exception as restore_error:
                            raise RuntimeError(f"Could not restore pending state after clear failed for {mosaic_dataset_path}: {restore_error}") from clear_error
                        raise
                    log(f"Production maintenance completed and cleared: {mosaic_dataset_path}")

    log(f"Report written: {REPORT_CSV}")
    elapsed_seconds = time.perf_counter() - start_time
    log(f"Finished: {total_added} added, {total_failed} failed, in {elapsed_seconds:.2f} seconds")
    run_report.record("run", "completed" if run_report.issue_count == 0 and total_failed == 0 else "completed_with_issues",
                      message=f"added={total_added}; failed={total_failed}; elapsed_seconds={elapsed_seconds:.2f}",
                      output_path=REPORT_CSV)
    return 1 if run_report.issue_count or total_failed else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        log("Error while adding TIFFs to mosaic datasets")
        log(str(exc))
        log(traceback.format_exc())
        run_report.exception("run_fatal", exc)
        print(f"Detailed diagnostics: {run_report.path}")
        run_report.close()
        sys.exit(1)
    print(f"Detailed diagnostics: {run_report.path}")
    run_report.close()
    sys.exit(exit_code)
