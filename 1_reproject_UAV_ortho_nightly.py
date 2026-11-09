"""Reproject client UAV orthomosaics to Cloud Optimized GeoTIFFs."""

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
BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client")
CLIENT_FOLDERS = {
	"CVE": "Cenovus",
	"TOU": "Tourmaline",
	"WCP": "Whitecap",
}
SOURCE_SUBDIRECTORY = "Imagery/UAV"
SOURCE_ORTHO_FOLDER = "Orthomosaic"
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports\4_reproject_ortho_report.csv")

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
	re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_ ](?P<month>\d{1,2})[-_ ](?P<year>20\d{2})(?!\d)"),
	re.compile(r"(?<!\d)(?P<year>20\d{2})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})(?!\d)"),
	re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_ ](?P<month_name>[A-Za-z]{3})[-_ ](?P<year>20\d{2})(?!\d)", re.IGNORECASE),
	re.compile(r"(?<!\d)(?P<month_name>[A-Za-z]{3})[-_ ](?P<day>\d{1,2})[-_ ](?P<year>20\d{2})(?!\d)", re.IGNORECASE),
	re.compile(r"(?<!\d)(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)"),
)
PART_PATTERN = re.compile(r"(?<![A-Za-z])Part[\s_-]*(?P<number>\d+)(?!\d)", re.IGNORECASE)
FLIGHT_PATTERN = re.compile(
	r"(?<![A-Za-z])Flights?[\s_-]*(?P<numbers>\d+(?:\s*(?:-|and|&)\s*\d+)*)(?!\d)",
	re.IGNORECASE,
)
NUMBERED_QUALIFIER_PATTERNS = (
	("Option", re.compile(r"(?<![A-Za-z])Option[\s_-]*(?P<number>\d+)(?!\d)", re.IGNORECASE)),
	("Borrow", re.compile(r"(?<![A-Za-z])Borrow[\s_-]*(?P<number>\d+)(?!\d)", re.IGNORECASE)),
)
FIXED_QUALIFIER_PATTERNS = (
	("Thermal-Base", re.compile(r"(?<![A-Za-z])Thermal[\s_-]+Base(?![A-Za-z])", re.IGNORECASE)),
	("No-GCPS", re.compile(r"(?<![A-Za-z])No[\s_-]*GCPs?(?![A-Za-z])", re.IGNORECASE)),
	("Combined", re.compile(r"(?<![A-Za-z])Combined(?![A-Za-z])", re.IGNORECASE)),
	("Lease", re.compile(r"(?<![A-Za-z])Lease(?![A-Za-z])", re.IGNORECASE)),
	("Fixed", re.compile(r"(?<![A-Za-z])Fixed(?![A-Za-z])", re.IGNORECASE)),
)
REDUCED_PATTERN = re.compile(
	r"(?<![A-Za-z])(?:reduc(?:e|ed|ted|d)?|rduced|redced|reduse|reduected)(?![A-Za-z])",
	re.IGNORECASE,
)
RESOLUTION_PATTERN = re.compile(
	r"(?<!\d)0[._-](?:05|1|10|2|20)m?(?![A-Za-z\d])|(?<!\d)(?:05|5|10|20)\s?cm(?![A-Za-z])",
	re.IGNORECASE,
)
IGNORED_NAME_WORDS = re.compile(
	r"(?<![A-Za-z])(?:transparent|mosaic|orthomosaic|ortho|lidar|rgb|group\d*|group|"
	r"grid|spacing|decimation|decimated|photo|jpg|dom|rerun|result|updated|riser|"
	r"shift|if|using)(?![A-Za-z])",
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
	"""Return a canonical output name and any naming review notes.

	Source TIFFs are never renamed; this name is used only for the output COG.
	"""
	stem = tif_path.stem
	dates = []
	for pattern in DATE_NAME_PATTERNS:
		for match in pattern.finditer(stem):
			groups = match.groupdict()
			month_text = groups.get("month_name")
			month = MONTHS.get(month_text.lower()) if month_text else groups.get("month")
			try:
				parsed = date(int(groups["year"]), int(month), int(groups["day"]))
			except (TypeError, ValueError):
				continue
			dates.append((match.start(), match.end(), parsed))

	dates = sorted(set(dates), key=lambda item: item[0])
	name_notes = []
	if dates:
		selected_date = dates[1][2] if len(dates) > 1 else dates[0][2]
	else:
		try:
			selected_date = datetime.fromtimestamp(tif_path.stat().st_mtime).date()
			name_notes.append("No date in source filename; filesystem modified date used")
		except OSError:
			return None, "No filename date or readable modified date"

	qualifiers = []
	for match in PART_PATTERN.finditer(stem):
		qualifiers.append((match.start(), f"Part{match.group('number')}"))
	for match in FLIGHT_PATTERN.finditer(stem):
		numbers = re.findall(r"\d+", match.group("numbers"))
		if numbers:
			qualifiers.append((match.start(), f"Flight{'-'.join(numbers)}"))
	for label, pattern in NUMBERED_QUALIFIER_PATTERNS:
		for match in pattern.finditer(stem):
			qualifiers.append((match.start(), f"{label}{match.group('number')}"))
	for label, pattern in FIXED_QUALIFIER_PATTERNS:
		for match in pattern.finditer(stem):
			qualifiers.append((match.start(), label))

	residual = stem
	for pattern in DATE_NAME_PATTERNS:
		residual = pattern.sub(" ", residual)
	for pattern in (PART_PATTERN, FLIGHT_PATTERN):
		residual = pattern.sub(" ", residual)
	for _, pattern in NUMBERED_QUALIFIER_PATTERNS:
		residual = pattern.sub(" ", residual)
	for _, pattern in FIXED_QUALIFIER_PATTERNS:
		residual = pattern.sub(" ", residual)
	residual = REDUCED_PATTERN.sub(" ", residual)
	residual = RESOLUTION_PATTERN.sub(" ", residual)
	residual = IGNORED_NAME_WORDS.sub(" ", residual)
	residual = re.sub(r"(?<!\d)\d{5}(?!\d)", " ", residual)
	residual = re.sub(r"\b\d+(?:-\d+)*\b", " ", residual)
	unknown_words = [
		word for word in re.split(r"[^A-Za-z0-9]+", residual)
		if len(word) > 1 and not word.isdigit()
	]
	if unknown_words:
		unknown_clarifier = "-".join(unknown_words)
		qualifiers.append((len(stem), unknown_clarifier))
		name_notes.append(f"Unrecognized filename text retained as clarifier: {unknown_clarifier}")

	parts = [selected_date.strftime("%d-%m-%Y"), "Mosaic"]
	seen_qualifiers = set()
	for _, qualifier in sorted(qualifiers, key=lambda item: item[0]):
		key = qualifier.casefold()
		if key not in seen_qualifiers:
			parts.append(qualifier)
			seen_qualifiers.add(key)

	is_reduced = bool(REDUCED_PATTERN.search(stem) or RESOLUTION_PATTERN.search(stem))
	if is_reduced:
		parts.append("Reduced")

	return f"{'-'.join(parts)}{tif_path.suffix}", "; ".join(name_notes)


def main():
	run_report.record("run", "started", message="Orthomosaic reprojection run started",
				  details=f"BASE_DIR={BASE_DIR}; target_crs={TARGET_CRS_CODE}")
	source_items = list(iter_source_tifs())
	output_names = {}
	target_owners = {}
	name_collisions = set()
	for item in source_items:
		_, _, _, src_tif, output_dir = item
		output_name, name_note = expected_tif_name(src_tif)
		if output_name is None:
			output_names[src_tif] = (None, name_note)
			continue
		output_path = output_dir / output_name
		output_names[src_tif] = (output_path, name_note)
		target_key = str(output_path).casefold()
		if target_key in target_owners and target_owners[target_key] != src_tif:
			name_collisions.add(target_owners[target_key])
			name_collisions.add(src_tif)
		else:
			target_owners[target_key] = src_tif

	processed = 0
	skipped = 0
	already_done = 0
	collision_count = 0
	naming_review_count = 0

	with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		writer = csv.DictWriter(
			report_handle,
			fieldnames=["client", "job_number", "type", "input_path", "output_file_path", "status"],
		)
		writer.writeheader()

		for client_code, job_number, type_tag, src_tif, output_dir in source_items:
			output_path, name_note = output_names[src_tif]
			if name_note:
				naming_review_count += 1
				print(f"NAME REVIEW: {src_tif}: {name_note}")
				run_report.record("name_output_tif", "warning", client=client_code,
							  project_or_job=job_number, input_path=src_tif,
							  output_path=output_path or "", message=name_note)

			if output_path is None:
				skipped += 1
				status = "skipped_invalid_name"
			elif src_tif in name_collisions:
				skipped += 1
				collision_count += 1
				status = "skipped_name_collision"
				message = f"More than one source maps to output name: {output_path}"
				print(f"NAME COLLISION: {message}")
				run_report.record("check_output_name", "warning", client=client_code,
							  project_or_job=job_number, input_path=src_tif,
							  output_path=output_path, message=message)
			elif is_valid_existing_output(output_path):
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
				"output_file_path": str(output_path) if output_path is not None else "",
				"status": status,
			})
			report_handle.flush()

	print(f"Report written: {REPORT_CSV}")
	print(
		f"\nFinished: {processed} COG files created, "
		f"{already_done} already done, {skipped} skipped, "
		f"{collision_count} naming collisions, {naming_review_count} naming review(s)"
	)
	run_report.record("run", "completed" if skipped == 0 else "completed_with_issues",
				  message=(f"processed={processed}; already_done={already_done}; skipped={skipped}; "
						   f"name_collisions={collision_count}; naming_reviews={naming_review_count}"),
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
