"""Atomic on-disk publication of report artifacts.

Each refresh writes all new artifacts into uniquely named temp files and only
then swaps the pointer file via ``os.replace`` (atomic on the same volume).
Readers therefore never observe a half-written report: either the previous
manifest or the new complete set is visible. A process-wide write lock makes
concurrent refreshes serialize.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .pipeline import canonical_bytes

MANIFEST_NAME = "manifest.json"

_WINDOWS_REPLACE_RETRIES = 50

_IO_LOCK = threading.RLock()

# Monotonic generation number; content files embed it so stale leftovers from
# an interrupted older publish never become reachable through a fresh manifest.
_GENERATION = 0


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _IO_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _replace_with_retry(tmp_path: Path, path: Path) -> None:
    """Replace with retries for Windows sharing-violation windows.

    On Windows a reader that just opened the old file can make ``os.replace``
    raise PermissionError for a few milliseconds. The replacement itself stays
    atomic; we only retry the same rename.
    """
    last_error: Exception | None = None
    for attempt in range(_WINDOWS_REPLACE_RETRIES):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(min(0.02 * (attempt + 1), 0.2))
    raise last_error


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def publish(
    out_dir: Path,
    artifacts: dict[str, bytes],
    run_id: str,
    generated_at: str,
) -> Path:
    """Publish one coherent generation of artifacts.

    ``artifacts`` maps file name to bytes. Content files get a run-id suffix;
    the manifest records the active suffix and is replaced atomically last,
    which is the flip readers wait on.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    global _GENERATION
    with _IO_LOCK:
        _GENERATION += 1
        generation = _GENERATION
        old_manifest_path = out_dir / MANIFEST_NAME
        old_files: set[str] = set()
        if old_manifest_path.exists():
            try:
                old = json.loads(old_manifest_path.read_text(encoding="utf-8"))
                old_files = {
                    entry["file"]
                    for entry in old.get("artifacts", [])
                    if isinstance(entry, dict)
                }
            except (ValueError, OSError):
                old_files = set()

        entries = []
        for name, data in artifacts.items():
            stem, suffix = os.path.splitext(name)
            final_name = f"{stem}.g{generation}.{run_id}{suffix}"
            atomic_write_bytes(out_dir / final_name, data)
            entries.append({"name": name, "file": final_name, "bytes": len(data)})

        manifest = {
            "generation": generation,
            "run_id": run_id,
            "generated_at": generated_at,
            "artifacts": sorted(entries, key=lambda e: e["name"]),
        }
        manifest_bytes = (
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        atomic_write_bytes(old_manifest_path, manifest_bytes)

        current_files = {entry["file"] for entry in entries}
        for stale in old_files - current_files:
            _unlink_with_retry(out_dir / stale)

    return old_manifest_path


def _unlink_with_retry(path: Path) -> None:
    for attempt in range(_WINDOWS_REPLACE_RETRIES):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(min(0.02 * (attempt + 1), 0.2))


def active_manifest(out_dir: Path) -> dict | None:
    with _IO_LOCK:
        path = Path(out_dir) / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None


def active_artifact_bytes(out_dir: Path, logical_name: str) -> bytes | None:
    """Read the currently advertised artifact; never returns half-written data."""
    with _IO_LOCK:
        manifest = active_manifest(out_dir)
        if manifest is None:
            return None
        advertised = {
            entry.get("name"): entry.get("file")
            for entry in manifest.get("artifacts", [])
            if isinstance(entry, dict)
        }
        file_name = advertised.get(logical_name)
        if file_name is None:
            return None
        path = Path(out_dir) / file_name
        if not path.exists():
            return None
        return path.read_bytes()


def deterministic_payload_bytes(payload: dict) -> bytes:
    return canonical_bytes(payload)
