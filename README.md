# UAV Inventory nightly workflow

[run_uav_nightly.ps1](run_uav_nightly.ps1) runs the following scripts in order using the ArcGIS Pro Python launcher:

1. [LiDAR reprojection](1_reproject_UAV_LiDAR_nightly.py).
2. [Orthomosaic reprojection and repair queue producer](1_reproject_UAV_ortho_nightly.py).
3. [Orthomosaic repair](1.1_Repair_Single_Band_Rasters.py), only when this run's queue is valid.
4. [LiDAR mosaic update](2_UAV_LiDAR_to_Mosaic_nightly.py).
5. [Orthomosaic mosaic update](2_UAV_ortho_to_mosaic_nightly.py).

The orthomosaic producer publishes the run queue and diverts sources requiring repair to the consumer. If queue publication does not complete, repair is skipped and the workflow exits nonzero after attempting both mosaic updates.

## Repair queue and run safety

For every invocation, the wrapper generates a new GUID in `UAV_REPAIR_RUN_ID` and an absolute, GUID-specific JSON path in `UAV_REPAIR_QUEUE` under the configured reports folder. It also sets `UAV_REPORT_DIR`. Previous process environment values are restored on exit.

The producer must publish a completed JSON object at that exact path with integer `version` 1, the exact string `run_id`, boolean `complete` set to true, and an `items` array (which may be empty). Each repair item must have nonempty `client`, `job_number`, `reason`, `source_path`, and `output_path` strings; both paths must be absolute. The repair script validates the queue and each item independently.

The wrapper rejects a missing, unreadable, malformed, incomplete, wrong-version or wrong-run queue and counts the skipped repair as a failure. It refuses to reuse a queue already present at the new run's path and never searches for older queues. Old queue files are not automatically deleted; review and retain or clean them up according to local policy. A valid completed queue is consumed even if the producer exits nonzero, because it may contain work collected before a per-item or fatal failure. A producer failure still counts toward the overall exit status.

The wrapper continues to both mosaic scripts after a missing script, nonzero script exit, execution error, invalid queue, or repair failure, and returns exit code 1 if any step failed or repair was skipped. Initial preflight or lock failures stop the workflow before its steps run. Continuing the mosaic updates is not a guarantee that every source was successfully repaired or ingested.

A named Windows mutex prevents concurrent wrapper runs across logon sessions on the **same host**. It does not protect against runs on other hosts, direct invocation of the individual scripts, or other processes modifying the shared data. Run the scheduled workflow from only one designated host; permissions must allow the task account to create/open the global mutex. An abandoned mutex is acquired with a warning and the previous run should be reviewed.

## Raster handling

The repair consumer supports Byte single-band RGB palette-index rasters and Byte three-band rasters explicitly identified as red, green, and blue. It expands them to four-band RGBA COGs in EPSG:2955: palette imagery uses nearest-neighbor resampling, and RGB imagery uses bilinear resampling. Alpha derives from the palette, masks, and nodata, not from black or white pixel values. Unsupported formats, missing georeferencing, inaccessible sources, and output collisions are reported as failures for manual review; the consumer does not overwrite existing outputs or their sidecars.

The existing orthomosaic reprojection script checks for a same-basename output TIFF before validating the source filename. If that output is a valid RGBA COG in EPSG:2955, the source is skipped as ready without renaming or warning, even if its source name is not canonical. If the matching output is invalid, a Reduced source is recorded as an informational skip and left untouched; an invalid non-Reduced output is left untouched and reported for review. When no same-basename output exists, the script derives the canonical date-based name (including qualifiers or part suffixes), identifies Reduced names (including recognized resolution terms), and renames the source TIFF and matching sidecars where a safe canonical name is available. Reduced sources are then skipped without reprojection or repair queuing and are reported separately without counting as errors. Other new sources are reprojected if already RGBA or queued for repair otherwise. The repair consumer refuses an occupied output path rather than treating it as completed.

## Reports and review

The configured reports folder contains the uniquely named scheduler transcript, timestamped per-script diagnostics CSVs, and, when repair runs, a run-specific repair summary CSV with an entry for each item. The orthomosaic reprojection script also writes its configured reprojection report. Review the transcript and CSVs after a nonzero exit, an abandoned lock, naming or collision warnings, or any failed/unsupported item; verify the affected sources and mosaic results before manually correcting or retrying them. A completed queue or successful wrapper step does not replace that review.
