"""Durable, single-writer maintenance flags; no ArcGIS dependencies.

All cooperating writers must use the same state directory and hold ``lock()``
for the entire script, including every mosaic mutation and maintenance step.
This module cannot protect against writers that do not honor that policy.
"""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_REPORT_ROOT = Path(
    r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\UAV_Reports"
)
STATE_VERSION = 1


class StateError(ValueError):
    """An existing state record is invalid or belongs to another dataset."""


class LockError(RuntimeError):
    """The exclusive maintenance writer lock could not be acquired."""


def state_directory(report_root=None):
    """Resolve configuration without creating or accessing any directories."""
    override = os.environ.get("UAV_MAINTENANCE_STATE_DIR")
    if override is not None:
        if not override.strip():
            raise ValueError("UAV_MAINTENANCE_STATE_DIR must not be empty")
        return Path(override)
    if report_root is None:
        report_root = os.environ.get("UAV_REPORT_DIR") or DEFAULT_REPORT_ROOT
    return Path(report_root) / "maintenance_state"


def force_maintenance():
    """Parse UAV_FORCE_MAINTENANCE, rejecting unrecognized values."""
    value = os.environ.get("UAV_FORCE_MAINTENANCE")
    if value is None:
        return False
    value = value.strip().casefold()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError("Invalid UAV_FORCE_MAINTENANCE value: {!r}".format(value))


def dataset_key(dataset):
    """Return the normalized, case-folded absolute dataset path."""
    path = os.fspath(dataset)
    if not isinstance(path, str) or not path.strip():
        raise ValueError("dataset must be a non-empty text path")
    return os.path.normpath(os.path.abspath(path)).casefold()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateError("Duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


class MaintenanceState:
    """Store one record per dataset under a persistent directory-wide lock.

    Use ``with state.lock():`` around the entire script. ``ensure`` and
    ``is_pending`` return bool; ``mark_pending`` and ``clear`` return None.
    A missing record is first persisted pending, including when force is false.
    Only call clear after all required maintenance succeeded.
    """

    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self._lock_handle = None

    @contextmanager
    def lock(self):
        """Acquire the persistent non-blocking byte-range/flock writer lock."""
        if self._lock_handle is not None:
            raise LockError("Maintenance lock is already held by this instance")
        self.directory.mkdir(parents=True, exist_ok=True)
        # Never delete or replace this file: all writers must lock the same inode.
        handle = (self.directory / "maintenance.lock").open("a+b")
        locked = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                raise LockError("Unable to acquire maintenance writer lock") from error
            self._lock_handle = handle
            yield self
        finally:
            self._lock_handle = None
            try:
                if locked:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _require_lock(self):
        if self._lock_handle is None:
            raise LockError("Hold MaintenanceState.lock() for the entire script")

    def _path(self, key):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / (digest + ".json")

    def _read(self, key):
        path = self._path(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle, object_pairs_hook=_unique_object)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as error:
            raise StateError("Invalid maintenance state: {}".format(path)) from error
        if not isinstance(record, dict):
            raise StateError("Maintenance state is not an object: {}".format(path))
        if type(record.get("version")) is not int or record["version"] != STATE_VERSION:
            raise StateError("Unsupported maintenance state version: {}".format(path))
        if record.get("dataset") != key:
            raise StateError("Maintenance state dataset mismatch: {}".format(path))
        if type(record.get("pending")) is not bool:
            raise StateError("Invalid maintenance pending flag: {}".format(path))
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise StateError("Invalid maintenance reason: {}".format(path))
        timestamp = record.get("timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
            if parsed is None or parsed.utcoffset() is None:
                raise ValueError("timestamp must include timezone")
        except (ValueError, OverflowError) as error:
            raise StateError("Invalid maintenance timestamp: {}".format(path)) from error
        return record

    def _write(self, key, pending, reason):
        record = {
            "version": STATE_VERSION,
            "dataset": key,
            "pending": pending,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        fd, temporary = tempfile.mkstemp(prefix=".maintenance-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path(key))
            # POSIX needs the directory entry flushed as well as file contents.
            # Windows does not support opening a directory this way.
            if os.name != "nt":
                dir_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def ensure(self, dataset, force=False):
        """Validate or bootstrap a record, optionally durably forcing pending."""
        self._require_lock()
        if type(force) is not bool:
            raise TypeError("force must be a bool")
        key = dataset_key(dataset)
        record = self._read(key)
        if record is None:
            self._write(key, True, "forced" if force else "bootstrap")
            return True
        if force:
            self._write(key, True, "forced")
            return True
        return record["pending"]

    def is_pending(self, dataset):
        return self.ensure(dataset)

    def mark_pending(self, dataset, reason):
        """Persist pending before any operation that can mutate the mosaic."""
        self._require_lock()
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        key = dataset_key(dataset)
        self._read(key)  # Do not overwrite invalid existing records.
        self._write(key, True, reason)

    def clear(self, dataset):
        """Persist clean only after the caller completes all maintenance."""
        self._require_lock()
        key = dataset_key(dataset)
        if self._read(key) is None:
            raise StateError("Cannot clear a dataset without an existing state record")
        self._write(key, False, "maintenance succeeded")