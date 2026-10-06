"""Rename and reproject client UAV orthomosaics to Cloud Optimized GeoTIFFs."""

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
	from osgeo import gdal, osr
except Exception as error:
	run_report.exception("import_dependencies", error)
	run_report.close()
	raise

# ---- configuration ----
BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive")
CLIENT_FOLDERS = {
	"CVE": "Cenovus",
	"TOU": "Tourmaline",
	"WCP": "Whitecap",
}
SOURCE_SUBDIRECTORY = "Imagery/UAV"
SOURCE_ORTHO_FOLDER = "Orthomosaic"
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\4_reproject_ortho_report.csv")
RENAME_REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\4_reproject_ortho_rename_report.csv")
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
		r"(?:[- _]*(?P<embedded_type>Mosaic|Orthomosaic|Ortho))?(?P<suffix>(?:[- _]+.+)?)$",
		re.IGNORECASE,
	),
	re.compile(
		r"(?P<day>\d{1,2})[- ](?P<month>[A-Za-z]{3})[- ](?P<year>\d{4})"
		r"(?:[- _]*(?P<embedded_type>Mosaic|Orthomosaic|Ortho))?(?P<suffix>(?:[- _]+.+)?)$",
		re.IGNORECASE,
	),
)
CANONICAL_NAME_PATTERN = re.compile(
	r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})-Mosaic(?P<suffix>(?:-.+)?)$",
	re.IGNORECASE,
)

TARGET_CRS_CODE = "EPSG:2955"  # NAD83(CSRS) UTM Zone 11N

try:
	gdal.UseExceptions()
	gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
	gdal.SetConfigOption("GDAL_CACHEMAX", "1024")
	gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
except Exception as error:
	run_report.exception("configure_gdal", error)
	run_report.close()
	raise


def resampling_for_source(src_ds):
	"""Choose nearest for palette indices; use bilinear for continuous imagery."""
	palette_bands = [
		band_index
		for band_index in range(1, src_ds.RasterCount + 1)
		if src_ds.GetRasterBand(band_index).GetColorTable() is not None
	]
	if palette_bands:
		return gdal.GRA_NearestNeighbour, "NEAREST", f"palette-index band(s) {palette_bands}"
	return gdal.GRA_Bilinear, "BILINEAR", "non-paletted imagery"


def create_cog_from_tif(src_tif_path, output_path, target_crs_code):
	"""Reproject an orthomosaic while retaining its source pixel type and bands."""
	try:
		src_ds = gdal.Open(str(src_tif_path))
		if src_ds is None:
			print(f"ERROR: Could not open raster dataset: {src_tif_path}")
			run_report.record("open_source_raster", "failed", input_path=src_tif_path,
						  output_path=output_path, message="GDAL could not open source raster")
			return False

		src_crs = osr.SpatialReference(wkt=src_ds.GetProjection())
		src_crs_name = (
			src_crs.GetAttrValue("PROJCS")
			or src_crs.GetAttrValue("GEOGCS")
			or "UNKNOWN"
		)
		src_nodata = src_ds.GetRasterBand(1).GetNoDataValue()
		resampling, overview_resampling, resampling_reason = resampling_for_source(src_ds)
		palette = any(src_ds.GetRasterBand(i).GetColorTable() is not None
					  for i in range(1, src_ds.RasterCount + 1))
		if palette:
			run_report.record("inspect_source_raster", "info", input_path=src_tif_path,
						  output_path=output_path,
						  message="Palette-index raster detected; nearest-neighbor selected",
						  details=f"bands={src_ds.RasterCount}; resampling={overview_resampling}")
		print(f"REPROJECT: {src_tif_path}")
		print(f"Input CRS: {src_crs_name} -- Output CRS: {target_crs_code}")
		print(f"Resampling: {overview_resampling} ({resampling_reason})")

		dst_crs = osr.SpatialReference()
		dst_crs.ImportFromEPSG(int(target_crs_code.split(":")[1]))
		vrt_ds = gdal.AutoCreateWarpedVRT(
			src_ds, None, dst_crs.ExportToWkt(), resampling
		)
		if vrt_ds is None:
			print(f"ERROR: Could not compute output dimensions for: {src_tif_path}")
			run_report.record("calculate_warp_dimensions", "failed", input_path=src_tif_path,
						  output_path=output_path, message="GDAL AutoCreateWarpedVRT returned no dataset")
			src_ds = None
			return False

		width, height = vrt_ds.RasterXSize, vrt_ds.RasterYSize
		geotransform = vrt_ds.GetGeoTransform()
		x_res, y_res = abs(geotransform[1]), abs(geotransform[5])
		vrt_ds = None
		overview_count = (
			max(1, math.ceil(math.log2(max(width, height) / 256)))
			if max(width, height) > 256
			else 1
		)

		warp_kwargs = {
			"format": "COG",
			"dstSRS": target_crs_code,
			"resampleAlg": resampling,
			"xRes": x_res,
			"yRes": y_res,
			"creationOptions": [
				"COMPRESS=LZW",
				"PREDICTOR=2",
				"BLOCKSIZE=512",
				f"OVERVIEW_COUNT={overview_count}",
				f"OVERVIEW_RESAMPLING={overview_resampling}",
				"NUM_THREADS=ALL_CPUS",
				"BIGTIFF=IF_SAFER",
				"STATISTICS=YES",
			],
			"callback": gdal.TermProgress_nocb,
		}
		if src_nodata is not None:
			warp_kwargs["srcNodata"] = src_nodata

		dst_ds = gdal.Warp(
			str(output_path), src_ds, options=gdal.WarpOptions(**warp_kwargs)
		)
		src_ds = None
		if dst_ds is None:
			print(f"ERROR: Failed to create COG: {output_path}")
			run_report.record("warp_to_cog", "failed", input_path=src_tif_path,
						  output_path=output_path, message="GDAL Warp returned no output dataset")
			return False

		for band_index in range(1, dst_ds.RasterCount + 1):
			dst_ds.GetRasterBand(band_index).ComputeStatistics(False)
		dst_ds.FlushCache()
		dst_ds = None
		return True

	except Exception as error:
		print(f"ERROR in create_cog_from_tif: {error}")
		run_report.exception("create_cog", error, input_path=src_tif_path, output_path=output_path)
		return False


def is_valid_existing_output(path):
	"""Return True if path is a TIFF GDAL can open with a valid raster."""
	try:
		if not path.exists() or path.stat().st_size == 0:
			return False
		dataset = gdal.Open(str(path))
	except Exception:
		return False
	if dataset is None:
		return False
	valid = dataset.RasterXSize > 0 and dataset.RasterYSize > 0
	dataset = None
	return valid


def iter_source_tifs():
	"""Yield (client, job number, type, source TIFF, job output directory)."""
	for client_code, client_folder in CLIENT_FOLDERS.items():
		client_root = BASE_DIR / client_folder / SOURCE_SUBDIRECTORY
		if not client_root.is_dir():
			print(f"Source directory not found, skipping: {client_root}")
			continue

		for job_dir in sorted(client_root.iterdir()):
			if not job_dir.is_dir():
				continue
			ortho_dir = job_dir / SOURCE_ORTHO_FOLDER
			if not ortho_dir.is_dir():
				run_report.record("discover_ortho_folder", "info", client=client_folder,
							  project_or_job=job_dir.name, input_path=ortho_dir,
							  message="No Orthomosaic source folder in this job; skipped")
				continue
			tif_paths = sorted(ortho_dir.glob("*.tif")) + sorted(
				ortho_dir.glob("*.tiff")
			)
			for tif_path in tif_paths:
				yield client_code, job_dir.name, "Mosaic", tif_path, job_dir


def expected_tif_name(tif_path):
	"""Return a canonical Mosaic name, falling back to the file's modified date."""

	def modified_date_fallback():
		try:
			modified = datetime.fromtimestamp(tif_path.stat().st_mtime)
		except OSError:
			return None
		# Keep the original stem as a suffix to distinguish files with no date.
		return f"{modified:%d-%m-%Y}-Mosaic-{tif_path.stem}{tif_path.suffix}"

	stem = tif_path.stem
	canonical_match = CANONICAL_NAME_PATTERN.fullmatch(stem)
	if canonical_match:
		day = canonical_match.group("day")
		month = canonical_match.group("month")
		year = canonical_match.group("year")
		suffix = canonical_match.group("suffix")
	else:
		date_match = next(
			(
				match
				for pattern in DATE_NAME_PATTERNS
				if (match := pattern.search(stem)) is not None
			),
			None,
		)
		if date_match is None:
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

	return f"{day}-{month}-{year}-Mosaic{suffix}{tif_path.suffix}"


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


def preview_or_apply_renames():
	"""Preview or apply canonical Mosaic filenames in each source folder."""
	rows = []
	rename_plans = []
	planned_targets = {}

	for client_code, job_number, type_tag, tif_path, _ in iter_source_tifs():
		expected_name = expected_tif_name(tif_path)
		if expected_name is None:
			rows.append({
				"client": client_code,
				"job_number": job_number,
				"type": type_tag,
				"input_path": str(tif_path),
				"output_path": "",
				"status": "invalid_name",
				"message": "Could not find a valid filename date or read the file modified date",
			})
			continue

		target_path = tif_path.with_name(expected_name)
		if target_path == tif_path:
			rows.append({
				"client": client_code,
				"job_number": job_number,
				"type": type_tag,
				"input_path": str(tif_path),
				"output_path": str(target_path),
				"status": "already_correct",
				"message": "",
			})
			continue

		sidecar_pairs = sidecar_rename_pairs(tif_path, target_path)
		file_pairs = [(tif_path, target_path), *sidecar_pairs]
		collision_message = ""
		for _, planned_path in file_pairs:
			target_key = str(planned_path).casefold()
			if planned_path.exists():
				collision_message = f"Target already exists: {planned_path}"
				break
			if target_key in planned_targets:
				collision_message = f"Target duplicates {planned_targets[target_key]}"
				break

		if collision_message:
			rows.append({
				"client": client_code,
				"job_number": job_number,
				"type": type_tag,
				"input_path": str(tif_path),
				"output_path": str(target_path),
				"status": "collision",
				"message": collision_message,
			})
			continue

		for source_path, planned_path in file_pairs:
			planned_targets[str(planned_path).casefold()] = str(source_path)
		rename_plans.append((tif_path, target_path, sidecar_pairs))
		rows.append({
			"client": client_code,
			"job_number": job_number,
			"type": type_tag,
			"input_path": str(tif_path),
			"output_path": str(target_path),
			"status": "preview_rename" if DRY_RUN else "renamed",
			"message": "",
		})

	if not DRY_RUN:
		for source_path, target_path, sidecar_pairs in rename_plans:
			try:
				for sidecar_source, sidecar_target in sidecar_pairs:
					sidecar_source.rename(sidecar_target)
				source_path.rename(target_path)
				run_report.record("rename_source_and_sidecars", "completed",
							  input_path=source_path, output_path=target_path,
							  message=f"Renamed {len(sidecar_pairs)} sidecar(s)")
			except Exception as error:
				row = next(row for row in rows if row["input_path"] == str(source_path))
				row["status"] = "rename_failed"
				row["message"] = f"{type(error).__name__}: {error}"
				run_report.exception("rename_source_and_sidecars", error,
								 input_path=source_path, output_path=target_path,
								 details=f"Sidecars planned: {sidecar_pairs}")

	with RENAME_REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		writer = csv.DictWriter(
			report_handle,
			fieldnames=["client", "job_number", "type", "input_path", "output_path", "status", "message"],
		)
		writer.writeheader()
		writer.writerows(rows)

	rename_failures = [row for row in rows if row["status"] == "rename_failed"]
	if rename_failures:
		raise OSError(f"{len(rename_failures)} TIFF rename operation(s) failed; see {RENAME_REPORT_CSV}")

	for row in rows:
		if row["status"] in {"collision", "invalid_name"}:
			run_report.record("rename_preflight", "warning", client=row["client"],
						  project_or_job=row["job_number"], data_type=row["type"],
						  input_path=row["input_path"], output_path=row["output_path"],
						  message=row["message"] or row["status"])

	print(f"Rename report written: {RENAME_REPORT_CSV}")
	print(f"Rename preview: {sum(row['status'] == 'preview_rename' for row in rows)} files")
	print(f"Already correct: {sum(row['status'] == 'already_correct' for row in rows)} files")
	print(f"Collisions: {sum(row['status'] == 'collision' for row in rows)} files")
	print(f"Invalid names: {sum(row['status'] == 'invalid_name' for row in rows)} files")
	sidecar_label = "Sidecars renamed" if not DRY_RUN else "Sidecars to rename"
	print(f"{sidecar_label}: {sum(len(plan[2]) for plan in rename_plans)} files")


def main():
	run_report.record("run", "started", message="Orthomosaic rename and reprojection run started",
				  details=f"BASE_DIR={BASE_DIR}; DRY_RUN={DRY_RUN}; target_crs={TARGET_CRS_CODE}")
	preview_or_apply_renames()
	if DRY_RUN:
		print("DRY_RUN is enabled; stopping after rename preview.")
		run_report.record("run", "completed", message="Dry run completed; reprojection intentionally not run")
		return 0

	processed = 0
	skipped = 0
	already_done = 0

	with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		writer = csv.DictWriter(
			report_handle,
			fieldnames=["client", "job_number", "type", "input_path", "output_file_path", "status"],
		)
		writer.writeheader()

		for client_code, job_number, type_tag, src_tif, output_dir in iter_source_tifs():
			output_path = output_dir / src_tif.name
			if is_valid_existing_output(output_path):
				print(f"Already reprojected, skipping: {output_path}\n")
				already_done += 1
				status = "skipped_already_exists"
				run_report.record("reprojection", "skipped", client=client_code,
							  project_or_job=job_number, data_type=type_tag,
							  input_path=src_tif, output_path=output_path,
							  message="Existing output opened successfully; reproject skipped")
			else:
				try:
					if output_path.exists():
						output_path.unlink()  # remove only an invalid/partial generated output
					if create_cog_from_tif(src_tif, output_path, TARGET_CRS_CODE):
						print(f"Done: {output_path}\n")
						processed += 1
						status = "completed"
						run_report.record("reprojection", "completed", client=client_code,
									  project_or_job=job_number, data_type=type_tag,
								  input_path=src_tif, output_path=output_path,
								  message="COG created successfully")
					else:
						skipped += 1
						status = "skipped"
						run_report.record("reprojection", "failed", client=client_code,
									  project_or_job=job_number, data_type=type_tag,
								  input_path=src_tif, output_path=output_path,
								  message="COG creation returned failure; inspect preceding diagnostic events")
				except Exception as error:
					print(f"Error processing {src_tif}: {error}\n")
					skipped += 1
					status = "skipped"
					run_report.exception("reprojection", error, client=client_code,
									 project_or_job=job_number, data_type=type_tag,
									 input_path=src_tif, output_path=output_path)

			writer.writerow({
				"client": client_code,
				"job_number": job_number,
				"type": type_tag,
				"input_path": str(src_tif),
				"output_file_path": str(output_path),
				"status": status,
			})
			report_handle.flush()

	print(f"Report written: {REPORT_CSV}")
	print(
		f"\nFinished: {processed} COG files created, "
		f"{already_done} already done, {skipped} skipped"
	)
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
