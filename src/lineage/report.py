"""Report serialization with atomic, crash-safe replacement.

A reader (the dashboard, a concurrent refresh, ``--serve``) can only ever see
the previous complete file or the new complete file: writers publish through a
temp file + ``fsync`` + ``os.replace`` and never mutate in place.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_OK = "ok"
STATUS_FAIL = "fail"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(report: dict[str, Any]) -> bytes:
    """Stable serialization: identical inputs -> identical bytes."""
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def atomic_write_bytes(path: os.PathLike | str, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # On Windows a reader briefly holding an open handle can make the
        # rename return Access Denied. Retrying keeps the publish atomic while
        # tolerating that short window; a reader still only ever sees one
        # complete file or the other.
        last_error: OSError | None = None
        for _attempt in range(20):
            try:
                os.replace(tmp_name, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.01)
        if last_error is not None:
            raise last_error
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: os.PathLike | str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def load_report(path: os.PathLike | str) -> dict[str, Any]:
    """Read a published report; atomic replacement guarantees a whole document."""
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))
