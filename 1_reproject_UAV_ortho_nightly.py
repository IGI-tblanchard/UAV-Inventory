"""Reproject client UAV orthomosaics to Cloud Optimized GeoTIFFs."""

import csv
import json
import math
import os
import re
import stat
import sys
import tempfile
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
	gdal.SetConfigOption("GDAL_PAM_ENABLED", "YES")
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
		reason = rgba_problem(src_ds)
		if reason:
			raise ValueError(f"Source changed or is not RGBA: {reason}")

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


def rgba_problem(dataset):
	"""Return a reason unless the raster has explicitly identified RGBA bands."""
	if dataset is None:
		raise ValueError("GDAL could not open raster")
	if dataset.RasterXSize < 1 or dataset.RasterYSize < 1:
		raise ValueError("Raster has invalid dimensions")
	if dataset.RasterCount != 4:
		return f"Expected four RGBA bands; found {dataset.RasterCount}"
	bands = [dataset.GetRasterBand(i) for i in range(1, 5)]
	if any(band is None for band in bands):
		raise ValueError("GDAL could not read raster band metadata")
	roles = [band.GetColorInterpretation() for band in bands]
	expected = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand, gdal.GCI_AlphaBand]
	if roles != expected:
		return f"Band color interpretations are not Red, Green, Blue, Alpha: {roles}"
	if any(band.GetColorTable() is not None for band in bands):
		return "RGBA bands must not have a color table"
	return ""


def existing_output_problem(path, full_check=False):
	"""Explain why an existing raster cannot be treated as an RGBA output."""
	try:
		if path.stat().st_size == 0:
			return "Output is empty"
		dataset = gdal.Open(str(path))
		problem = rgba_problem(dataset)
		if problem:
			return problem
		target = osr.SpatialReference()
		if target.ImportFromEPSG(2955) != 0:
			raise ValueError("Could not construct EPSG:2955")
		actual = dataset.GetSpatialRef()
		if actual is None or not actual.IsSame(target):
			return "Output CRS is not EPSG:2955"
		if dataset.GetGeoTransform(can_return_null=True) is None:
			return "Output geotransform is missing"
		# Existing files are checked without repeatedly reading entire large COGs
		# over the network. Newly created files receive the full block check.
		height = dataset.RasterYSize if full_check else min(dataset.RasterYSize, 512)
		width = dataset.RasterXSize if full_check else min(dataset.RasterXSize, 512)
		for y in range(0, height, 512):
			for x in range(0, width, 512):
				if dataset.ReadRaster(x, y, min(512, dataset.RasterXSize - x),
						min(512, dataset.RasterYSize - y)) is None:
					return "Cannot decode output raster pixels"
		return ""
	except Exception as error:
		return f"Cannot validate output: {type(error).__name__}: {error}"


def is_valid_existing_output(path):
	return not existing_output_problem(path)


def optional_directory(path):
	"""Treat only a missing optional directory as absent; propagate access errors."""
	try:
		return stat.S_ISDIR(path.stat().st_mode)
	except FileNotFoundError:
		return False


def queue_configuration():
	"""Remove a previous run's queue before discovery and validate configuration."""
	queue_name = os.environ.get("UAV_REPAIR_QUEUE", "")
	run_id = os.environ.get("UAV_REPAIR_RUN_ID", "")
	queue_name = queue_name if queue_name.strip() else ""
	run_id = run_id if run_id.strip() else ""
	if queue_name:
		queue_path = Path(queue_name).absolute()
		queue_path.unlink(missing_ok=True)
	else:
		queue_path = None
	if bool(queue_path) != bool(run_id):
		raise ValueError("UAV_REPAIR_QUEUE and UAV_REPAIR_RUN_ID must both be set or both be unset")
	return queue_path, run_id


def publish_repair_queue(queue_path, run_id, items):
	"""Publish the complete run queue by replacing a temporary file in its directory."""
	temp_path = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=queue_path.parent,
				prefix=f".{queue_path.name}.", suffix=".tmp", delete=False) as handle:
			temp_path = Path(handle.name)
			json.dump({"version": 1, "run_id": run_id, "complete": True, "items": items}, handle, indent=2)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temp_path, queue_path)
	finally:
		if temp_path is not None:
			temp_path.unlink(missing_ok=True)


def create_cog_without_overwrite(source_path, output_path, target_crs_code):
	"""Create in the output directory, then atomically publish without replacement."""
	temp_path = output_path.with_name(f".__uav_ortho_{uuid.uuid4().hex}.tif")
	sidecar_paths = output_sidecar_paths(output_path)
	try:
		if output_path.exists():
			raise FileExistsError(f"Refusing to overwrite an existing output: {output_path}")
		for sidecar in sidecar_paths:
			if os.path.lexists(sidecar):
				raise FileExistsError(f"Refusing to publish beside an existing output sidecar: {sidecar}")
		if not create_cog_from_tif(source_path, temp_path, target_crs_code):
			return False
		if any(Path(str(temp_path) + suffix).exists() for suffix in (".aux.xml", ".msk", ".ovr")):
			raise RuntimeError("Temporary COG depends on sidecars and cannot be published safely")
		problem = existing_output_problem(temp_path, full_check=True)
		if problem:
			raise ValueError(f"Temporary COG failed validation: {problem}")
		if os.name == "nt":
			os.rename(temp_path, output_path)
		else:
			os.link(temp_path, output_path)
		return True
	finally:
		temp_path.unlink(missing_ok=True)
		for suffix in (".aux.xml", ".msk", ".ovr"):
			Path(str(temp_path) + suffix).unlink(missing_ok=True)


def output_sidecar_paths(output_path):
	"""Return common files that can alter a TIFF's metadata or displayed pixels."""
	return (
		Path(str(output_path) + ".aux.xml"),
		Path(str(output_path) + ".msk"),
		Path(str(output_path) + ".ovr"),
		output_path.with_suffix(".tfw"),
		output_path.with_suffix(".tifw"),
		output_path.with_suffix(".wld"),
		output_path.with_suffix(".prj"),
	)


def iter_source_tifs():
	"""Yield (client, job number, type, source TIFF, job output directory)."""
	client_dirs = sorted((path for path in BASE_DIR.iterdir() if stat.S_ISDIR(path.stat().st_mode)), key=lambda path: path.name.casefold())

	for client_dir in client_dirs:
		client_folder = client_dir.name
		client_code = CLIENT_CODES.get(client_folder, client_folder)
		client_root = client_dir / SOURCE_SUBDIRECTORY
		if not optional_directory(client_root):
			run_report.record("discover_source_root", "info", client=client_folder,
						  input_path=client_root,
						  message="No UAV source folder for this client; skipped")
			continue

		for job_dir in sorted(client_root.iterdir()):
			if not stat.S_ISDIR(job_dir.stat().st_mode):
				continue
			ortho_dir = job_dir / SOURCE_ORTHO_FOLDER
			if not optional_directory(ortho_dir):
				run_report.record("discover_ortho_folder", "info", client=client_folder,
							  project_or_job=job_dir.name, input_path=ortho_dir,
							  message="No Orthomosaic source folder in this job; skipped")
				continue
			tif_paths = sorted(path for path in ortho_dir.iterdir()
				if path.suffix.casefold() in {".tif", ".tiff"}
				and stat.S_ISREG(path.stat().st_mode))
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
	queue_path, run_id = queue_configuration()
	source_items = list(iter_source_tifs())
	records = []
	new_records = []
	for client_code, job_number, type_tag, src_tif, output_dir in source_items:
		try:
			matching_cogs = sorted(
				candidate for candidate in output_dir.iterdir()
				if candidate.suffix.casefold() in {".tif", ".tiff"}
				and candidate.stem.casefold() == src_tif.stem.casefold()
				and stat.S_ISREG(candidate.stat().st_mode)
			)
			if matching_cogs:
				# An existing output takes precedence over canonical source naming.
				# This avoids warning on oddly named sources that already have a
				# usable same-basename COG.
				problems = [(candidate, existing_output_problem(candidate)) for candidate in matching_cogs]
				valid = [(candidate, problem) for candidate, problem in problems if not problem]
				if valid:
					records.append({"client": client_code, "job_number": job_number, "type": type_tag,
						"input_path": src_tif, "source_path": src_tif, "output_path": valid[0][0],
						"status": "skipped_already_exists",
						"message": "Same-basename valid COG exists; source, output, and sidecars left unchanged"})
					continue

				is_reduced_source = bool(
					REDUCED_PATTERN.search(src_tif.stem)
					or RESOLUTION_PATTERN.search(src_tif.stem)
				)
				if is_reduced_source:
					candidate, problem = problems[0]
					message = f"Reduced source ignored; same-basename output left untouched ({problem})"
					records.append({"client": client_code, "job_number": job_number, "type": type_tag,
						"input_path": src_tif, "source_path": src_tif, "output_path": candidate,
						"status": "skipped_reduced", "message": message})
					run_report.record("skip_reduced_source", "info", client=client_code,
						project_or_job=job_number, input_path=src_tif,
						output_path=candidate, message=message)
					continue

				for candidate, problem in problems:
					message = f"Same-basename output left untouched: {problem}"
					records.append({"client": client_code, "job_number": job_number, "type": type_tag,
						"input_path": src_tif, "source_path": src_tif, "output_path": candidate,
						"status": "skipped_invalid_same_name_output", "message": message})
					run_report.record("check_existing_output", "warning", client=client_code,
						project_or_job=job_number, input_path=src_tif,
						output_path=candidate, message=message)
				continue

			# Only sources without a same-basename output need canonical-name
			# validation and Reduced detection.
			new_name, name_note = expected_tif_name(src_tif)
			if new_name is None:
				is_reduced_source = bool(
					REDUCED_PATTERN.search(src_tif.stem)
					or RESOLUTION_PATTERN.search(src_tif.stem)
				)
				if is_reduced_source:
					message = "Reduced source ignored; no safe canonical rename could be derived, left unchanged"
					records.append({"client": client_code, "job_number": job_number, "type": type_tag,
						"input_path": src_tif, "source_path": src_tif, "output_path": "",
						"status": "skipped_reduced", "message": message})
					run_report.record("skip_reduced_source", "info", client=client_code,
						project_or_job=job_number, input_path=src_tif, message=message)
					continue
				message = name_note or "Could not determine a safe source name"
				records.append({"client": client_code, "job_number": job_number, "type": type_tag,
					"input_path": src_tif, "source_path": src_tif, "output_path": "",
					"status": "skipped_invalid_name", "message": message})
				run_report.record("name_new_source", "warning", client=client_code,
					project_or_job=job_number, input_path=src_tif, message=message)
				continue

			target_source = src_tif.with_name(new_name)
			is_reduced = "reduced" in target_source.stem.casefold()
			if is_reduced:
				repair_reason = ""
			else:
				dataset = gdal.Open(str(src_tif))
				repair_reason = rgba_problem(dataset)
				dataset = None
				if repair_reason and queue_path is None:
					raise ValueError(f"Repair required ({repair_reason}), but UAV_REPAIR_QUEUE and UAV_REPAIR_RUN_ID are not set")

			new_records.append({
				"client": client_code, "job_number": job_number, "type": type_tag,
				"input_path": src_tif, "source_path": target_source,
				"output_dir": output_dir, "output_path": output_dir / new_name,
				"name_note": name_note, "needs_rename": target_source != src_tif,
				"sidecars": matching_sidecars(src_tif, target_source) if target_source != src_tif else [],
				"repair_reason": repair_reason, "is_reduced": is_reduced,
				"status": "pending", "message": "",
			})
		except Exception as error:
			message = f"{type(error).__name__}: {error}"
			records.append({"client": client_code, "job_number": job_number, "type": type_tag,
				"input_path": src_tif, "source_path": src_tif, "output_path": "",
				"status": "failed", "message": message})
			run_report.exception("inspect_source", error, client=client_code,
				project_or_job=job_number, input_path=src_tif)

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
			try:
				candidate_sidecars = (
					matching_sidecars(record["input_path"], candidate_source)
					if candidate_source != record["input_path"] else []
				)
			except Exception as error:
				record["status"] = "failed"
				record["message"] = f"{type(error).__name__}: {error}"
				record["source_path"] = record["input_path"]
				run_report.exception("inspect_source_sidecars", error, client=record["client"],
					project_or_job=record["job_number"], input_path=record["input_path"])
				break
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
			if not record["is_reduced"] and (candidate_output.exists() or any(os.path.lexists(sidecar)
					for sidecar in output_sidecar_paths(candidate_output))):
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
				run_report.record("check_new_source_name", "warning", client=record["client"],
					project_or_job=record["job_number"], input_path=record["input_path"],
					output_path=candidate_output, message=record["message"])
				break

		if record["status"] != "pending":
			continue
		record["source_path"] = candidate_source
		record["output_path"] = candidate_output
		record["sidecars"] = candidate_sidecars
		for source_path, target_path in pairs:
			reserved_targets[str(target_path).casefold()] = source_path
		if not record["is_reduced"]:
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
	for record in new_records:
		if record["status"] != "pending" or not record["is_reduced"]:
			continue
		record["status"] = "skipped_reduced"
		record["message"] = "Reduced source ignored; source renamed, existing output left untouched, reprojection and repair skipped"
		run_report.record("skip_reduced_source", "info", client=record["client"],
					  project_or_job=record["job_number"], input_path=record["input_path"],
					  output_path=record["source_path"], message=record["message"])
	queue_items = []
	for record in new_records:
		if record["status"] != "pending" or not record["repair_reason"]:
			continue
		if record["output_path"].exists():
			record["status"] = "skipped_invalid_same_name_output"
			record["message"] = "Reserved output appeared after naming; left untouched"
			run_report.record("check_existing_output", "warning", client=record["client"],
				project_or_job=record["job_number"], input_path=record["source_path"],
				output_path=record["output_path"], message=record["message"])
			continue
		queue_items.append({"source_path": str(record["source_path"].absolute()),
			"output_path": str(record["output_path"].absolute()), "client": str(record["client"]),
			"job_number": str(record["job_number"]), "reason": record["repair_reason"]})
		record["status"] = "queued_for_repair"
		record["message"] = record["repair_reason"]
	if queue_path is not None:
		publish_repair_queue(queue_path, run_id, queue_items)
		for record in new_records:
			if record["status"] == "queued_for_repair":
				run_report.record("queue_repair", "info", client=record["client"],
					project_or_job=record["job_number"], input_path=record["source_path"],
					output_path=record["output_path"], message=record["message"])

	processed = 0
	skipped = 0
	already_done = 0
	collision_count = 0
	naming_review_count = 0
	queued_count = 0
	reduced_skipped = 0

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
			if status in {"pending", "queued_for_repair"} and name_note:
				naming_review_count += 1
				print(f"NAME REVIEW: {src_tif}: {name_note}")
				run_report.record("name_output_tif", "warning", client=client_code,
							  project_or_job=job_number, input_path=src_tif,
							  output_path=output_path, message=name_note)
			if status == "skipped_already_exists":
				print(f"Already reprojected, skipping without changes: {output_path}\n")
				already_done += 1
			elif status == "skipped_reduced":
				print(f"Reduced source renamed; reprojection skipped: {src_tif}\n")
				reduced_skipped += 1
			elif status == "queued_for_repair":
				queued_count += 1
				print(f"Queued for repair: {src_tif} -> {output_path}: {message}")
			elif status == "pending":
				try:
					if output_path.exists():
						raise FileExistsError(f"Refusing to overwrite an existing output: {output_path}")
					if create_cog_without_overwrite(src_tif, output_path, TARGET_CRS_CODE):
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
			elif status not in {"failed", "skipped_reduced", "skipped_name_collision", "skipped_invalid_name", "skipped_invalid_same_name_output"}:
				status = "skipped"

			if status in {"failed", "skipped_name_collision", "skipped_invalid_name", "skipped_invalid_same_name_output"} and record["status"] != "pending":
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
		f"{reduced_skipped} Reduced source(s) ignored, {collision_count} naming collisions, "
		f"{naming_review_count} naming review(s), {queued_count} queued for repair"
	)
	run_report.record("run", "completed" if run_report.issue_count == 0 else "completed_with_issues",
				  message=(f"processed={processed}; already_done={already_done}; skipped={skipped}; "
						   f"reduced_ignored={reduced_skipped}; name_collisions={collision_count}; "
						   f"naming_reviews={naming_review_count}; queued_for_repair={queued_count}"),
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
