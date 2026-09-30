import csv
import time
import traceback
from pathlib import Path

import arcpy

# ---- configuration ----
BASE_DIR = Path(r"P:\IGG\Z_Drive")
STAGING_GDB = Path(r"P:\IGG\Z_Drive\Staging\UAV_Staging.gdb")
PRODUCTION_GDB = Path(r"P:\IGG\Z_Drive\UAV_Mosaics.gdb")
CLIENT_FOLDERS = {
	"CVE": "Cenovus",
	"TOU": "Tourmaline",
	"WCP": "Whitecap",
}
REPORT_CSV = Path(r"c:\Users\tblanchard\Documents\Tracy\Code\UAV Updates\5_ortho_mosaic_add_report.csv")
MOSAIC_SUFFIX = "Orthomosaic"
SOURCE_SUBDIRECTORY = Path("Imagery") / "UAV"

MOSAIC_SCOPE = {
	("Cenovus", "Cenovus_Orthomosaic"),
	("Tourmaline", "Tourmaline_Orthomosaic"),
	("Whitecap", "Whitecap_Orthomosaic"),
}

arcpy.env.overwriteOutput = True


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

	output_table = arcpy.CreateUniqueName(f"uav_paths_{mosaic_path.name}", str(mosaic_path.parent))
	try:
		arcpy.management.ExportMosaicDatasetPaths(
			in_mosaic_dataset=mosaic_dataset_path,
			output_table=output_table,
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
			report_handle.flush()
	else:
		log(f"No new TIFFs to add to {mosaic_dataset_path}")

	if item_paths is not None:
		update_footprint_attributes(mosaic_dataset_path, item_paths)
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
	return updated_count


def build_production_overviews(mosaic_dataset_path):
	"""Define and regenerate all overviews for a production mosaic dataset."""
	try:
		arcpy.management.BuildOverviews(
			in_mosaic_dataset=mosaic_dataset_path,
			define_missing="DEFINE_MISSING_OVERVIEWS",
			generate_overviews="GENERATE_OVERVIEWS",
			generate_missing_images="GENERATE_MISSING_IMAGES",
			regenerate_stale_images="REGENERATE_STALE_IMAGES",
		)
		log(f"Production overviews generated: {mosaic_dataset_path}")
	except Exception as overview_error:
		raise RuntimeError(
			f"Production overview generation stopped for {mosaic_dataset_path}: {overview_error}"
		) from overview_error


def main():
	start_time = time.perf_counter()
	log("Starting Add TIFFs To Orthomosaic process")

	grouped = {}
	for client_code, client_folder, job_number, mosaic_name, tif_path in iter_output_tifs():
		grouped.setdefault((client_folder, mosaic_name), []).append(
			(client_code, client_folder, job_number, tif_path)
		)

	if not grouped:
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
					continue

				if not arcpy.Exists(mosaic_dataset_path):
					log(f"Mosaic dataset not found, skipping: {mosaic_dataset_path}")
					if target_name == "production":
						raise FileNotFoundError(
							f"Production mosaic dataset not found: {mosaic_dataset_path}"
						)
					continue

				log(f"Found {len(tif_rows)} TIFFs for {mosaic_dataset_path}")
				added_count, failed_count = add_tifs_to_mosaic(
					mosaic_dataset_path,
					tif_rows,
					writer,
					report_handle,
				)
				if target_name == "production" and added_count > 0:
					production_mosaics_to_overview.add(mosaic_dataset_path)
				total_added += added_count
				total_failed += failed_count

			if target_name == "production":
				for mosaic_dataset_path in sorted(production_mosaics_to_overview):
					build_production_overviews(mosaic_dataset_path)

	log(f"Report written: {REPORT_CSV}")
	elapsed_seconds = time.perf_counter() - start_time
	log(f"Finished: {total_added} added, {total_failed} failed, in {elapsed_seconds:.2f} seconds")


if __name__ == "__main__":
	try:
		main()
	except Exception as exc:
		log("Error while adding TIFFs to orthomosaic datasets")
		log(str(exc))
		log(traceback.format_exc())
