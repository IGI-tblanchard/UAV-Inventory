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


def add_tifs_to_mosaic(mosaic_dataset_path, tif_rows, writer, report_handle, stop_on_error=False):
	"""Add each TIFF to the mosaic dataset; tif_rows contains source metadata."""
	added_count = 0
	failed_count = 0

	for client_code, client_folder, job_number, tif_path in tif_rows:
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
		except Exception as add_error:
			failed_count += 1
			status = "failed"
			message = str(add_error)
			log(f"FAILED: {tif_path}")
			log(message)
			if stop_on_error:
				raise RuntimeError(
					f"Production mosaic update stopped for {mosaic_dataset_path}: {add_error}"
				) from add_error

		writer.writerow({
			"client": client_code,
			"job_number": job_number,
			"tif_path": str(tif_path),
			"mosaic_dataset": mosaic_dataset_path,
			"status": status,
			"message": message,
		})
		report_handle.flush()

	log(
		f"Per-file add completed for {mosaic_dataset_path}. "
		f"Added: {added_count}, Failed: {failed_count}"
	)
	try:
		result = arcpy.management.GetCount(mosaic_dataset_path)
		log(f"Mosaic item count: {int(result.getOutput(0))}")
	except Exception:
		log("Mosaic item count: unavailable")

	return added_count, failed_count


def update_footprint_attributes(mosaic_dataset_path, tif_rows):
	"""Set ProductName and GroupName for matching TIFFs in the mosaic catalog."""
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
			if not row[0]:
				continue
			normalized_product = str(row[0]).replace("/", "\\").casefold()
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
					if target_name == "production":
						if not arcpy.Exists(mosaic_dataset_path):
							raise FileNotFoundError(
								f"Production mosaic dataset not found: {mosaic_dataset_path}"
							)
						build_production_overviews(mosaic_dataset_path)
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
		log("Error while adding TIFFs to orthomosaic datasets")
		log(str(exc))
		log(traceback.format_exc())
