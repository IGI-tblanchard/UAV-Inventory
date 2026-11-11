"""Reproject client UAV orthomosaics to Cloud Optimized GeoTIFFs."""

import csv
import math
import re
import sys
import uuid
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
CLIENT_CODES = {
	"Cenovus": "CVE",
	"Tourmaline": "TOU",
	"Whitecap": "WCP",
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
	re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_ ](?P<month>\d{1,2})[-_ ](?P<year>(?:20\d{2}|\d{2}))(?!\d)"),
	re.compile(r"(?<!\d)(?P<year>20\d{2})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})(?!\d)"),
	re.compile(r"(?<!\d)(?P<day>\d{1,2})[-_ ](?P<month_name>[A-Za-z]{3})[-_ ](?P<year>(?:20\d{2}|\d{2}))(?!\d)", re.IGNORECASE),
	re.compile(r"(?<!\d)(?P<month_name>[A-Za-z]{3})[-_ ](?P<day>\d{1,2})[-_ ](?P<year>(?:20\d{2}|\d{2}))(?!\d)", re.IGNORECASE),
	re.compile(r"(?<!\d)(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)"),
)
CANONICAL_SOURCE_PATTERN = re.compile(
	r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})-Mosaic"
	r"(?:-(?P<qualifier>(?!Reduced(?:$|-))[A-Za-z0-9](?:[A-Za-z0-9-]{0,18}[A-Za-z0-9])?))?"
	r"(?:-Reduced)?$",
	re.IGNORECASE,
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
	try:
		client_dirs = sorted((path for path in BASE_DIR.iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
	except OSError as error:
		run_report.exception("discover_client_directories", error, input_path=BASE_DIR)
		return

	for client_dir in client_dirs:
		client_folder = client_dir.name
		client_code = CLIENT_CODES.get(client_folder, client_folder)
		client_root = client_dir / SOURCE_SUBDIRECTORY
		if not client_root.is_dir():
			run_report.record("discover_source_root", "info", client=client_folder,
						  input_path=client_root,
						  message="No UAV source folder for this client; skipped")
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
	"""Return the canonical source name, preserving valid names with short qualifiers."""
	stem = tif_path.stem
	canonical_match = CANONICAL_SOURCE_PATTERN.fullmatch(stem)
	if canonical_match:
		try:
			date(
				int(canonical_match.group("year")),
				int(canonical_match.group("month")),
				int(canonical_match.group("day")),
			)
			return tif_path.name, ""
		except ValueError:
			pass

	dates = []
	for pattern in DATE_NAME_PATTERNS:
		for match in pattern.finditer(stem):
			groups = match.groupdict()
			month_text = groups.get("month_name")
			month = MONTHS.get(month_text.lower()) if month_text else groups.get("month")
			year_text = groups["year"]
			year = int(year_text)
			if len(year_text) == 2:
				year += 2000
			try:
				parsed = date(year, int(month), int(groups["day"]))
			except (TypeError, ValueError):
				continue
			dates.append((match.start(), match.end(), parsed))

	dates = sorted(set(dates), key=lambda item: item[0])
	name_notes = []
	if dates:
		selected_date = dates[1][2] if len(dates) > 1 else dates[0][2]
	else:
		try:
			stat = tif_path.stat()
			created_timestamp = getattr(stat, "st_birthtime", stat.st_ctime)
			selected_date = datetime.fromtimestamp(created_timestamp).date()
			name_notes.append("No date in source filename; file creation date used")
		except (OSError, AttributeError, OverflowError, ValueError):
			return None, "No filename date or readable file creation date"

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
		if len(unknown_clarifier) > 20:
			return None, f"Unrecognized clarifier exceeds 20 characters: {unknown_clarifier}"
		qualifiers.append((len(stem), unknown_clarifier))
		name_notes.append(f"Unrecognized filename text retained as clarifier: {unknown_clarifier}")

	parts = [selected_date.strftime("%d-%m-%Y"), "Mosaic"]
	seen_qualifiers = set()
	for _, qualifier in sorted(qualifiers, key=lambda item: item[0]):
		key = qualifier.casefold()
		if key not in seen_qualifiers:
			parts.append(qualifier)
			seen_qualifiers.add(key)
	qualifier_text = "-".join(parts[2:])
	if qualifier_text.casefold().endswith("-reduced"):
		qualifier_text = qualifier_text[:-8]
	if len(qualifier_text) > 20:
		return None, f"Normalized qualifier exceeds 20 characters: {qualifier_text}"

	is_reduced = bool(REDUCED_PATTERN.search(stem) or RESOLUTION_PATTERN.search(stem))
	if is_reduced:
		parts.append("Reduced")

	return f"{'-'.join(parts)}{tif_path.suffix}", "; ".join(name_notes)


def matching_sidecars(tif_path, target_path):
	"""Find companion files for a TIFF and return their corresponding new paths."""
	pairs = []
	tif_prefix = f"{tif_path.name}.".casefold()
	stem_prefix = f"{tif_path.stem}.".casefold()

	for candidate in sorted(tif_path.parent.iterdir()):
		if not candidate.is_file() or candidate == tif_path:
			continue
		folded_name = candidate.name.casefold()
		if folded_name.startswith(tif_prefix):
			suffix = candidate.name[len(tif_path.name):]
			new_name = f"{target_path.name}{suffix}"
		elif folded_name.startswith(stem_prefix) and candidate.suffix.casefold() not in {".tif", ".tiff"}:
			suffix = candidate.name[len(tif_path.stem):]
			new_name = f"{target_path.stem}{suffix}"
		else:
			continue
		pairs.append((candidate, candidate.with_name(new_name)))
	return pairs


def rename_file_group(file_pairs):
	"""Stage then rename a source TIFF and its sidecars, rolling back on errors."""
	staged = []
	try:
		for source_path, target_path in file_pairs:
			temp_path = source_path.with_name(f".__ortho_rename_{uuid.uuid4().hex}.tmp")
			while temp_path.exists():
				temp_path = source_path.with_name(f".__ortho_rename_{uuid.uuid4().hex}.tmp")
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
				run_report.exception(
					"rollback_source_rename", rollback_error,
					input_path=temp_path, output_path=source_path,
				)
		raise


def main():
	run_report.record("run", "started", message="Orthomosaic reprojection run started",
				  details=f"BASE_DIR={BASE_DIR}; target_crs={TARGET_CRS_CODE}")
	source_items = list(iter_source_tifs())
	records = []
	new_records = []
	for client_code, job_number, type_tag, src_tif, output_dir in source_items:
		matching_cogs = [
			candidate for candidate in output_dir.iterdir()
			if candidate.is_file()
			and candidate.suffix.casefold() in {".tif", ".tiff"}
			and candidate.stem.casefold() == src_tif.stem.casefold()
		]
		valid_matches = [candidate for candidate in matching_cogs if is_valid_existing_output(candidate)]
		if valid_matches:
			records.append({
				"client": client_code, "job_number": job_number, "type": type_tag,
				"input_path": src_tif, "source_path": src_tif,
				"output_path": valid_matches[0], "status": "skipped_already_exists",
				"message": "Same-basename COG exists; source, COG, and sidecars left unchanged",
			})
			continue
		if matching_cogs:
			message = "Same-basename output exists but is not a valid raster; left untouched"
			records.append({
				"client": client_code, "job_number": job_number, "type": type_tag,
				"input_path": src_tif, "source_path": src_tif,
				"output_path": matching_cogs[0], "status": "skipped_invalid_same_name_output",
				"message": message,
			})
			run_report.record("check_existing_output", "warning", client=client_code,
						  project_or_job=job_number, input_path=src_tif,
						  output_path=matching_cogs[0], message=message)
			continue

		new_name, name_note = expected_tif_name(src_tif)
		if new_name is None:
			message = name_note or "Could not determine a safe source name"
			records.append({
				"client": client_code, "job_number": job_number, "type": type_tag,
				"input_path": src_tif, "source_path": src_tif,
				"output_path": "", "status": "skipped_invalid_name", "message": message,
			})
			run_report.record("name_new_source", "warning", client=client_code,
						  project_or_job=job_number, input_path=src_tif, message=message)
			continue

		target_source = src_tif.with_name(new_name)
		new_records.append({
			"client": client_code, "job_number": job_number, "type": type_tag,
			"input_path": src_tif, "source_path": target_source,
			"output_dir": output_dir, "output_path": output_dir / new_name,
			"name_note": name_note, "needs_rename": target_source != src_tif,
			"sidecars": matching_sidecars(src_tif, target_source) if target_source != src_tif else [],
			"status": "pending", "message": "",
		})

	pending_move_sources = {
		path for record in new_records if record["needs_rename"]
		for path in [record["input_path"], *(source for source, _ in record["sidecars"])]
	}
	reserved_targets = {}
	new_records.sort(key=lambda record: (record["needs_rename"], str(record["input_path"]).casefold()))
	for record in new_records:
		base_name = record["source_path"].stem
		extension = record["source_path"].suffix
		part_number = 1
		while True:
			if part_number == 1:
				candidate_stem = base_name
			elif base_name.casefold().endswith("-reduced"):
				candidate_stem = f"{base_name[:-8]}-Part{part_number}-Reduced"
			else:
				candidate_stem = f"{base_name}-Part{part_number}"
			candidate_source = record["input_path"].with_name(candidate_stem + extension)
			candidate_output = record["output_dir"] / candidate_source.name
			candidate_sidecars = (
				matching_sidecars(record["input_path"], candidate_source)
				if candidate_source != record["input_path"] else []
			)
			pairs = ([(record["input_path"], candidate_source)] if candidate_source != record["input_path"] else []) + candidate_sidecars
			collision = False
			for source_path, target_path in pairs:
				target_key = str(target_path).casefold()
				if target_key in reserved_targets and reserved_targets[target_key] != source_path:
					collision = True
					break
				if target_path.exists() and target_path != source_path and target_path not in pending_move_sources:
					collision = True
					break
			output_key = str(candidate_output).casefold()
			if output_key in reserved_targets and reserved_targets[output_key] != record["input_path"]:
				collision = True
			if candidate_output.exists():
				collision = True
			if not collision:
				break
			if not record["needs_rename"]:
				record["status"] = "skipped_name_collision"
				record["message"] = "Canonical source name conflicts with another source or existing file; left unchanged"
				record["source_path"] = record["input_path"]
				record["output_path"] = candidate_output
				run_report.record("check_new_source_name", "warning", client=record["client"],
							  project_or_job=record["job_number"], input_path=record["input_path"],
							  output_path=candidate_output, message=record["message"])
				break
			part_number += 1
			if part_number > 9999:
				record["status"] = "skipped_name_collision"
				record["message"] = "Could not find an unused canonical name"
				record["source_path"] = record["input_path"]
				record["output_path"] = candidate_output
				break

		if record["status"] != "pending":
			continue
		record["source_path"] = candidate_source
		record["output_path"] = candidate_output
		record["sidecars"] = candidate_sidecars
		for source_path, target_path in pairs:
			reserved_targets[str(target_path).casefold()] = source_path
		reserved_targets[output_key] = record["input_path"]

	rename_pairs = [
		pair for record in new_records if record["status"] == "pending"
		for pair in ([(record["input_path"], record["source_path"])] if record["needs_rename"] else [])
		+ record["sidecars"]
	]
	if rename_pairs:
		rename_file_group(rename_pairs)
		for record in new_records:
			if record["status"] == "pending" and record["needs_rename"]:
				run_report.record("rename_new_source_and_sidecars", "completed",
							  client=record["client"], project_or_job=record["job_number"],
							  input_path=record["input_path"], output_path=record["source_path"],
							  message=f"Renamed source and {len(record['sidecars'])} sidecar(s)")
	records.extend(new_records)

	processed = 0
	skipped = 0
	already_done = 0
	collision_count = 0
	naming_review_count = 0

	with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		writer = csv.DictWriter(
			report_handle,
			fieldnames=["client", "job_number", "type", "input_path", "renamed_source_path", "output_file_path", "status", "message"],
		)
		writer.writeheader()

		for record in records:
			client_code = record["client"]
			job_number = record["job_number"]
			type_tag = record["type"]
			src_tif = record["source_path"]
			output_path = record["output_path"]
			status = record["status"]
			message = record["message"]
			name_note = record.get("name_note", "")
			if status == "pending" and name_note:
				naming_review_count += 1
				print(f"NAME REVIEW: {src_tif}: {name_note}")
				run_report.record("name_output_tif", "warning", client=client_code,
							  project_or_job=job_number, input_path=src_tif,
							  output_path=output_path, message=name_note)
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
						run_report.record("reprojection", "completed", client=client_code,
									  project_or_job=job_number, data_type=type_tag,
								  input_path=src_tif, output_path=output_path,
								  message="COG created successfully")
					else:
						skipped += 1
						status = "failed"
						message = "COG creation returned failure; inspect preceding diagnostic events"
						run_report.record("reprojection", "failed", client=client_code,
									  project_or_job=job_number, data_type=type_tag,
								  input_path=src_tif, output_path=output_path,
								  message=message)
				except Exception as error:
					print(f"Error processing {src_tif}: {error}\n")
					skipped += 1
					status = "failed"
					message = f"{type(error).__name__}: {error}"
					run_report.exception("reprojection", error, client=client_code,
									 project_or_job=job_number, data_type=type_tag,
									 input_path=src_tif, output_path=output_path)
			elif status not in {"skipped_name_collision", "skipped_invalid_name", "skipped_invalid_same_name_output"}:
				status = "skipped"

			if status in {"skipped_name_collision", "skipped_invalid_name", "skipped_invalid_same_name_output"}:
				skipped += 1
			if status == "skipped_name_collision":
				collision_count += 1

			writer.writerow({
				"client": client_code,
				"job_number": job_number,
				"type": type_tag,
				"input_path": str(record["input_path"]),
				"renamed_source_path": str(src_tif),
				"output_file_path": str(output_path) if output_path else "",
				"status": status,
				"message": message or name_note,
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
