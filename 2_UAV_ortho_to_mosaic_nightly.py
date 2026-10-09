import csv
import sys
import time
import traceback
from pathlib import Path

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
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\5_ortho_mosaic_add_report.csv")
MOSAIC_SUFFIX = "Orthomosaic"
SOURCE_SUBDIRECTORY = Path("Imagery") / "UAV"

MOSAIC_SCOPE = {
	("Cenovus", "Cenovus_Orthomosaic"),
	("Tourmaline", "Tourmaline_Orthomosaic"),
	("Whitecap", "Whitecap_Orthomosaic"),
}

try:
	arcpy.env.overwriteOutput = True
except Exception as error:
	run_report.exception("configure_arcpy_environment", error)
	run_report.close()
	raise


def log(message):
	timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
	print(f"[{timestamp}] {message}")


def mosaic_name_for(client_folder):
	return f"{client_folder}_{MOSAIC_SUFFIX}"


def iter_output_tifs():
	"""Yield reprojected Mosaic TIFFs in each client's immediate job folders."""
	for client_code, client_folder in CLIENT_FOLDERS.items():
		client_root = BASE_DIR / client_folder / SOURCE_SUBDIRECTORY
		if not client_root.is_dir():
			log(f"Source directory not found, skipping: {client_root}")
			run_report.record("discover_client_root", "warning", client=client_folder,
						  input_path=client_root, message="Client source root is missing or inaccessible")
			continue

		mosaic_name = mosaic_name_for(client_folder)
		for job_dir in sorted(client_root.iterdir()):
			if not job_dir.is_dir():
				continue
			# Reprojection outputs are written into the job folder, beside the
			# Orthomosaic source directory. Exclude unrelated TIFFs by requiring
			# the output naming tag produced by the reprojection script.
			tif_paths = sorted(
				path
				for path in job_dir.iterdir()
				if path.is_file()
				and path.suffix.casefold() in {".tif", ".tiff"}
				and "-mosaic" in path.stem.casefold()
			)
			for tif_path in tif_paths:
				yield client_code, client_folder, job_dir.name, mosaic_name, tif_path


def normalize_raster_path(path):
	return str(path).replace("/", "\\").casefold()


def mosaic_item_paths(mosaic_dataset_path):
	"""Export and read each mosaic item's actual source raster path by OID."""
	mosaic_path = Path(mosaic_dataset_path)
	catalog_path = mosaic_path.parent / f"AMD_{mosaic_path.name}_CAT"
	if not arcpy.Exists(str(catalog_path)):
		raise FileNotFoundError(f"Mosaic footprint catalog not found: {catalog_path}")

	item_count = int(arcpy.management.GetCount(mosaic_dataset_path).getOutput(0))
	if item_count == 0:
		message = "Mosaic contains no items; skipping path export and treating existing paths as empty"
		log(f"{message}: {mosaic_dataset_path}")
		run_report.record("export_existing_mosaic_paths", "skipped",
					  output_path=mosaic_dataset_path, message=message)
		return {}

	output_table = arcpy.CreateUniqueName(f"uav_paths_{mosaic_path.name}", str(mosaic_path.parent))
	try:
		arcpy.management.ExportMosaicDatasetPaths(
			in_mosaic_dataset=mosaic_dataset_path,
			out_table=output_table,
			export_mode="ALL",
			types_of_paths="RASTER",
		)
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
		if arcpy.Exists(output_table):
			arcpy.management.Delete(output_table)


def add_tifs_to_mosaic(mosaic_dataset_path, tif_rows, writer, report_handle):
	"""Skip catalog paths and add each remaining TIFF independently."""
	added_count = 0
	failed_count = 0
	try:
		item_paths = mosaic_item_paths(mosaic_dataset_path)
	except Exception as path_error:
		message = f"Could not read existing mosaic source paths: {path_error}"
		log(f"Cannot safely check {mosaic_dataset_path}; skipping its TIFFs")
		run_report.exception("export_existing_mosaic_paths", path_error,
						  output_path=mosaic_dataset_path)
		for client_code, client_folder, job_number, tif_path in tif_rows:
			writer.writerow({
				"client": client_code,
				"job_number": job_number,
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
		client_code, client_folder, job_number, tif_path = row
		if normalize_raster_path(tif_path) in existing_paths:
			writer.writerow({
				"client": client_code,
				"job_number": job_number,
				"tif_path": str(tif_path),
				"mosaic_dataset": mosaic_dataset_path,
				"status": "already_present",
				"message": "Source path already exists in mosaic catalog",
			})
			run_report.record("duplicate_check", "skipped", client=client_folder,
						  project_or_job=job_number, input_path=tif_path,
						  output_path=mosaic_dataset_path,
						  message="Source path already exists in mosaic")
		else:
			pending_rows.append(row)
	report_handle.flush()

	if pending_rows:
		log(f"Adding {len(pending_rows)} new TIFF(s) individually to {mosaic_dataset_path}")
		for client_code, client_folder, job_number, tif_path in pending_rows:
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
			except Exception as add_error:
				failed_count += 1
				error_message = arcpy.GetMessages(2) or str(add_error)
				log(f"FAILED: {tif_path}")
				log(error_message)
				run_report.exception("add_raster_to_mosaic", add_error, client=client_folder,
								 project_or_job=job_number, input_path=tif_path,
								 output_path=mosaic_dataset_path, details=error_message)
				writer.writerow({
					"client": client_code,
					"job_number": job_number,
					"tif_path": str(tif_path),
					"mosaic_dataset": mosaic_dataset_path,
					"status": "failed",
					"message": error_message,
				})
			else:
				successful_rows.append((client_code, job_number, tif_path))
			report_handle.flush()

		if successful_rows:
			try:
				item_paths = mosaic_item_paths(mosaic_dataset_path)
			except Exception as verify_error:
				item_paths = None
				log(f"Could not verify added TIFFs in {mosaic_dataset_path}: {verify_error}")
				run_report.exception("verify_mosaic_additions", verify_error,
								  output_path=mosaic_dataset_path)

			verified_paths = (
				{normalize_raster_path(path) for path in item_paths.values()}
				if item_paths is not None
				else set()
			)
			for client_code, job_number, tif_path in successful_rows:
				if item_paths is None:
					status = "verification_failed"
					message = f"Add tool succeeded, but catalog verification failed: {verify_error}"
					failed_count += 1
				elif normalize_raster_path(tif_path) in verified_paths:
					status = "added"
					message = ""
					added_count += 1
				else:
					status = "verification_failed"
					message = "Add tool succeeded, but source path is absent from the mosaic catalog"
					failed_count += 1
				writer.writerow({
					"client": client_code,
					"job_number": job_number,
					"tif_path": str(tif_path),
					"mosaic_dataset": mosaic_dataset_path,
					"status": status,
					"message": message,
				})
				run_report.record("verify_mosaic_addition", status, client=client_folder,
							  project_or_job=job_number, input_path=tif_path,
							  output_path=mosaic_dataset_path,
							  message=message or "Source path found in mosaic catalog")
			report_handle.flush()
	else:
		log(f"No new TIFFs to add to {mosaic_dataset_path}")

	if item_paths is not None:
		try:
			update_footprint_attributes(mosaic_dataset_path, item_paths)
		except Exception as update_error:
			log(f"Failed updating mosaic ProductName/GroupName fields: {update_error}")
			run_report.exception("update_catalog_attributes", update_error,
						  output_path=mosaic_dataset_path, details=arcpy.GetMessages(2))
			failed_count += 1
	else:
		log(f"Skipped ProductName/GroupName update because mosaic paths could not be read: {mosaic_dataset_path}")

	report_handle.flush()
	log(
		f"Add completed for {mosaic_dataset_path}. Added: {added_count}, "
		f"Failed: {failed_count}, Already present: {len(tif_rows) - len(pending_rows)}"
	)
	try:
		result = arcpy.management.GetCount(mosaic_dataset_path)
		log(f"Mosaic item count: {int(result.getOutput(0))}")
	except Exception:
		log("Mosaic item count: unavailable")

	return added_count, failed_count


def update_footprint_attributes(mosaic_dataset_path, item_paths):
	"""Store each item's source path and parent folder in ProductName/GroupName."""
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

	oid_field = arcpy.Describe(str(catalog_path)).OIDFieldName
	updated_count = 0
	with arcpy.da.UpdateCursor(str(catalog_path), [oid_field, "ProductName", "GroupName"]) as cursor:
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
				  message=f"Updated {updated_count} ProductName/GroupName row(s)")
	return updated_count


def build_production_overviews(mosaic_dataset_path):
	"""Define and regenerate all overviews for a production mosaic dataset."""
	try:
		arcpy.management.BuildOverviews(
			in_mosaic_dataset=mosaic_dataset_path,
			define_missing_tiles="DEFINE_MISSING_TILES",
			generate_overviews="GENERATE_OVERVIEWS",
			generate_missing_images="GENERATE_MISSING_IMAGES",
			regenerate_stale_images="REGENERATE_STALE_IMAGES",
		)
		log(f"Production overviews generated: {mosaic_dataset_path}")
		run_report.record("build_production_overviews", "completed", output_path=mosaic_dataset_path,
					  message="Production overview build completed")
		return True
	except Exception as overview_error:
		run_report.exception("build_production_overviews", overview_error,
						  output_path=mosaic_dataset_path, details=arcpy.GetMessages(2))
		log(f"Production overview generation failed for {mosaic_dataset_path}; continuing with other mosaics")
		return False


def main():
	start_time = time.perf_counter()
	log("Starting Add TIFFs To Orthomosaic process")
	run_report.record("run", "started", message="Orthomosaic mosaic update started",
				  details=f"BASE_DIR={BASE_DIR}; staging={STAGING_GDB}; production={PRODUCTION_GDB}")

	grouped = {}
	for client_code, client_folder, job_number, mosaic_name, tif_path in iter_output_tifs():
		grouped.setdefault((client_folder, mosaic_name), []).append(
			(client_code, client_folder, job_number, tif_path)
		)

	if not grouped:
		run_report.record("discover_inputs", "failed", issue_category="path_or_network",
					  input_path=BASE_DIR, message="No reprojected Mosaic TIFFs found")
		raise ValueError(f"No reprojected Mosaic TIFFs found under {BASE_DIR}")

	total_added = 0
	total_failed = 0
	production_mosaics_to_overview = set()
	with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		writer = csv.DictWriter(
			report_handle,
			fieldnames=["client", "job_number", "tif_path", "mosaic_dataset", "status", "message"],
		)
		writer.writeheader()

		for target_name, gdb_path in (("staging", STAGING_GDB), ("production", PRODUCTION_GDB)):
			log(f"Starting {target_name} geodatabase update: {gdb_path}")
			if not arcpy.Exists(str(gdb_path)):
				log(f"Geodatabase not found, skipping: {gdb_path}")
				run_report.record("check_geodatabase", "failed", issue_category="path_or_network",
							  output_path=gdb_path, message="Geodatabase does not exist or is inaccessible")
				if target_name == "production":
					raise FileNotFoundError(f"Production geodatabase not found: {gdb_path}")
				continue

			arcpy.env.workspace = str(gdb_path)
			for client_folder in CLIENT_FOLDERS.values():
				mosaic_name = mosaic_name_for(client_folder)
				if (client_folder, mosaic_name) not in MOSAIC_SCOPE:
					continue
				tif_rows = grouped.get((client_folder, mosaic_name))
				mosaic_dataset_path = str(gdb_path / mosaic_name)

				if not tif_rows:
					log(f"No TIFFs found for {client_folder} / {mosaic_name}, skipping")
					run_report.record("discover_mosaic_inputs", "info", client=client_folder,
								  output_path=mosaic_dataset_path,
								  message="No candidate TIFFs for this mosaic on this run; skipped")
					if target_name == "production" and arcpy.Exists(mosaic_dataset_path):
						production_mosaics_to_overview.add(mosaic_dataset_path)
					continue

				if not arcpy.Exists(mosaic_dataset_path):
					log(f"Mosaic dataset not found, skipping: {mosaic_dataset_path}")
					run_report.record("check_mosaic_dataset", "failed", client=client_folder,
								  output_path=mosaic_dataset_path,
								  message="Mosaic dataset does not exist or is inaccessible")
					if target_name == "production":
						raise FileNotFoundError(
							f"Production mosaic dataset not found: {mosaic_dataset_path}"
						)
					continue

				if target_name == "production":
					production_mosaics_to_overview.add(mosaic_dataset_path)

				log(f"Found {len(tif_rows)} TIFFs for {mosaic_dataset_path}")
				added_count, failed_count = add_tifs_to_mosaic(
					mosaic_dataset_path,
					tif_rows,
					writer,
					report_handle,
				)
				total_added += added_count
				total_failed += failed_count

			if target_name == "production":
				for mosaic_dataset_path in sorted(production_mosaics_to_overview):
					build_production_overviews(mosaic_dataset_path)

	log(f"Report written: {REPORT_CSV}")
	elapsed_seconds = time.perf_counter() - start_time
	log(f"Finished: {total_added} added, {total_failed} failed, in {elapsed_seconds:.2f} seconds")
	run_report.record("run", "completed" if run_report.issue_count == 0 else "completed_with_issues",
				  message=f"added={total_added}; failed={total_failed}; elapsed_seconds={elapsed_seconds:.2f}",
				  output_path=REPORT_CSV)
	return 1 if run_report.issue_count else 0


if __name__ == "__main__":
	try:
		exit_code = main()
	except Exception as exc:
		log("Error while adding TIFFs to orthomosaic datasets")
		log(str(exc))
		log(traceback.format_exc())
		run_report.exception("run_fatal", exc)
		print(f"Detailed diagnostics: {run_report.path}")
		run_report.close()
		sys.exit(1)
	print(f"Detailed diagnostics: {run_report.path}")
	run_report.close()
	sys.exit(exit_code)
