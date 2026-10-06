"""Shared, stdlib-only diagnostics for UAV scripts run by Task Scheduler."""

from __future__ import annotations

import csv
import getpass
import os
import platform
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path

FIELDS = (
    "timestamp", "script", "stage", "status", "issue_category", "client",
    "project_or_job", "data_type", "input_path", "output_path", "message", "details",
)


def classify_issue(text: str) -> str:
    value = text.casefold()
    if any(s in value for s in ("schema lock", "schema-lock", "000464", "locked by another", "cannot acquire a lock")):
        return "schema_lock"
    if any(s in value for s in ("permission denied", "access is denied", "unauthorized", "not have permission", "read-only", "winerror 5")):
        return "permissions"
    if any(s in value for s in ("modulenotfounderror", "no module named", "importerror", "license", "licensing", "failed to initialize", "not initialized")):
        return "environment_or_license"
    if any(s in value for s in ("network path", "network name", "drive not found", "system cannot find", "no such file", "not found", "does not exist", "unavailable")):
        return "path_or_network"
    if any(s in value for s in ("gdal", "raster", "geotiff", "cog", "projection", "coordinate system", "invalid dataset")):
        return "raster_or_data"
    if any(s in value for s in ("arcpy", "geoprocessing", "error 000", "executeerror")):
        return "geoprocessing"
    return "other"


class RunReport:
    """Timestamped append-only CSV report, flushed after every event."""

    def __init__(self, script_file: str | Path):
        script = Path(script_file).resolve()
        report_dir = Path(os.environ.get("UAV_REPORT_DIR", script.parent.parent))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = report_dir / f"{script.stem}_{stamp}_{os.getpid()}_diagnostics.csv"
        report_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._handle, fieldnames=FIELDS, extrasaction="ignore")
        self._writer.writeheader()
        self._handle.flush()
        self.issue_count = 0
        data_share_visible = Path("\\\\IGG-QNAP12\\IGG_Archive\\IGG\\Z_Drive").exists()
        self.record(
            "startup", "info", message="Run started; execution environment snapshot",
            details=(
                f"python={sys.version.replace(os.linesep, ' ')}; executable={sys.executable}; "
                f"platform={platform.platform()}; host={socket.gethostname()}; "
                f"user={getpass.getuser()}; cwd={Path.cwd()}; UNC_data_share_visible={data_share_visible}"
            ),
        )

    def record(self, stage: str, status: str, *, issue_category: str = "", client: str = "",
               project_or_job: str = "", data_type: str = "", input_path: str | Path = "",
               output_path: str | Path = "", message: str = "", details: str = "") -> None:
        self._writer.writerow({
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "script": Path(sys.argv[0]).name,
            "stage": stage,
            "status": status,
            "issue_category": issue_category or (classify_issue(message + " " + details) if status in {"failed", "warning"} else ""),
            "client": client,
            "project_or_job": project_or_job,
            "data_type": data_type,
            "input_path": str(input_path) if input_path else "",
            "output_path": str(output_path) if output_path else "",
            "message": message,
            "details": details,
        })
        self._handle.flush()
        if status in {"failed", "warning"}:
            self.issue_count += 1

    def exception(self, stage: str, error: BaseException, **context) -> None:
        supplemental_details = context.pop("details", "")
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        if supplemental_details:
            details = f"{details}\nAdditional geoprocessing details:\n{supplemental_details}"
        self.record(stage, "failed", issue_category=classify_issue(f"{type(error).__name__}: {error}\n{details}"),
                    message=f"{type(error).__name__}: {error}", details=details, **context)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
