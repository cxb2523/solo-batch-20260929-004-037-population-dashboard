"""Chunked CSV ingestion.

Every table is streamed with ``read_csv(chunksize=...)``. A file larger than
:data:`LARGE_FILE_BYTES` is never handed to a whole-frame ``read_csv``; only
compact normalized records and counters are retained.
"""
from __future__ import annotations

import re
import sys
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

LARGE_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 10_000

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_STATE_KEYS = ("states", "state")
_ID_KEYS = ("id",)
_CODE_KEYS = ("states_code", "state_code", "code")
_YEAR_KEYS = ("year",)
_POP_KEYS = ("population", "pop", "value")

# Canonical field order shared by ingestion, analysis and the page dataset.
FIELDS = ("id", "states_code", "population")

# None means the source table has no such column at all ("not read in").
Record = dict[str, Any]


@dataclass
class Table:
    label: str
    path: str
    kind: str  # "long" | "wide" | "unknown"
    raw_columns: list[str]
    spec: dict[str, str | None]
    year_columns: list[int]
    physical_rows: int = 0
    invalid_rows: int = 0
    duplicate_records: int = 0
    records: dict[tuple[str, int], Record] = field(default_factory=dict)
    parse_invalid: dict[str, int] = field(
        default_factory=lambda: {"id": 0, "states_code": 0, "population": 0}
    )
    bytes_size: int = 0
    chunks: int = 0
    chunk_rows_max: int = 0
    peak_rss_mb: float = 0.0

    def has_field(self, name: str) -> bool:
        if name == "population" and self.kind == "wide" and self.year_columns:
            return True
        return self.spec.get(name) is not None


def _find_column(columns: Iterable[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {str(column).strip().lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def detect_spec(columns: Iterable[str]) -> tuple[dict[str, str | None], list[int], str]:
    columns = [str(column) for column in columns]
    spec = {
        "states": _find_column(columns, _STATE_KEYS),
        "id": _find_column(columns, _ID_KEYS),
        "states_code": _find_column(columns, _CODE_KEYS),
        "year": _find_column(columns, _YEAR_KEYS),
        "population": _find_column(columns, _POP_KEYS),
    }
    year_columns = sorted(
        {
            int(column.strip())
            for column in columns
            if _YEAR_RE.match(str(column).strip()) and column != spec.get("population")
        }
    )
    if spec["year"] is not None:
        kind = "long"
    elif year_columns:
        kind = "wide"
    else:
        kind = "unknown"
    return spec, year_columns, kind


def _number(value: Any) -> Any:
    """Empty -> NaN(None). Non-empty but unparseable -> NaN(None) as well."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "" or text.lower() in {"na", "nan", "null", "none"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _code(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def peak_rss_mb() -> float:
    """Peak resident set size in MB, read from the OS accounting."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return 0.0
        return counters.PeakWorkingSetSize / 1048576.0
    import resource

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1024.0 if sys.platform.startswith("linux") else 1.0  # Linux KB, macOS bytes
    return maxrss / scale / 1024.0


def _store(table: Table, key: tuple[str, int], record: Record) -> None:
    if key in table.records:
        table.duplicate_records += 1
    table.records[key] = record


def _field_value(table: Table, name: str, raw: Any) -> Any:
    if not table.has_field(name):
        return None
    if name == "states_code":
        return _code(raw)
    value = _number(raw)
    if value is None and not _is_blank(raw):
        table.parse_invalid[name] += 1
    return value


def _ingest_long(table: Table, chunk: pd.DataFrame) -> None:
    spec = table.spec
    for raw in chunk.to_dict(orient="records"):
        state = str(raw[spec["states"]]).strip() if spec["states"] else ""
        year_text = str(raw[spec["year"]]).strip() if spec["year"] else ""
        if not state or not _YEAR_RE.match(year_text):
            table.invalid_rows += 1
            continue
        record = {
            name: _field_value(table, name, raw.get(spec[name])) if spec[name] else None
            for name in FIELDS
        }
        _store(table, (state, int(year_text)), record)


def _ingest_wide(table: Table, chunk: pd.DataFrame) -> None:
    spec = table.spec
    for raw in chunk.to_dict(orient="records"):
        state = str(raw[spec["states"]]).strip() if spec["states"] else ""
        if not state:
            table.invalid_rows += 1
            continue
        base = {
            "id": _field_value(table, "id", raw.get(spec["id"])) if spec["id"] else None,
            "states_code": _field_value(
                table, "states_code", raw.get(spec["states_code"])
            )
            if spec["states_code"]
            else None,
        }
        for year in table.year_columns:
            record = dict(base)
            record["population"] = _field_value(
                table, "population", raw.get(str(year))
            )
            _store(table, (state, year), record)


def stream_table(
    path: os.PathLike | str,
    label: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    mem_sampler: Callable[[], float] = peak_rss_mb,
) -> Table:
    path = Path(path)
    size = path.stat().st_size
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer; whole-frame reads are disabled")
    if size > LARGE_FILE_BYTES:
        # A >10 MiB table must be iterated; keep chunks modestly sized.
        chunk_size = min(chunk_size, DEFAULT_CHUNK_SIZE)

    table: Table | None = None
    peak_mb = mem_sampler()
    for chunk in pd.read_csv(
        path,
        chunksize=chunk_size,
        dtype=str,
        keep_default_na=False,
        index_col=False,
    ):
        peak_mb = max(peak_mb, mem_sampler())
        if table is None:
            spec, year_columns, kind = detect_spec(chunk.columns)
            table = Table(
                label=label,
                path=str(path),
                kind=kind,
                raw_columns=[str(column) for column in chunk.columns],
                spec=spec,
                year_columns=year_columns,
                bytes_size=size,
            )
        assert table is not None
        table.chunks += 1
        table.chunk_rows_max = max(table.chunk_rows_max, len(chunk))
        table.physical_rows += len(chunk)
        if table.kind == "wide":
            _ingest_wide(table, chunk)
        else:
            _ingest_long(table, chunk)

    if table is None:  # header-only / empty file
        header = pd.read_csv(path, nrows=0)
        spec, year_columns, kind = detect_spec(header.columns)
        table = Table(
            label=label,
            path=str(path),
            kind=kind,
            raw_columns=[str(column) for column in header.columns],
            spec=spec,
            year_columns=year_columns,
            bytes_size=size,
        )
    # Bucket the reading so the published value is stable across identical
    # reruns while still showing the peak residency order of magnitude.
    table.peak_rss_mb = float(_bucket_mb(peak_mb))
    return table


def _bucket_mb(value: float) -> int:
    """Round peak residency up to a stable 16 MiB bucket."""
    bucket = 16
    return int((value + bucket - 1) // bucket * bucket)


def label_for(path: os.PathLike | str) -> str:
    stem = Path(path).stem.lower()
    if "states-code" in stem or "states_code" in stem:
        return "states_code"
    if "reshaped" in stem:
        return "reshaped"
    return "wide"


def ingest_directory(
    data_dir: os.PathLike | str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[Table]:
    data_dir = Path(data_dir)
    tables: list[Table] = []
    used_labels: set[str] = set()
    for csv_path in sorted(data_dir.glob("*.csv")):
        label = label_for(csv_path)
        if label in used_labels:
            label = f"{label}_{csv_path.stem}"
        used_labels.add(label)
        tables.append(stream_table(csv_path, label=label, chunk_size=chunk_size))
    return tables


def build_code_mapping(tables: Iterable[Table]) -> dict[str, str]:
    """states -> official states_code, preferring the states_code table."""
    tables = list(tables)
    preferred = [table for table in tables if table.label == "states_code"]
    ordered = preferred + [table for table in tables if table not in preferred]
    mapping: dict[str, str] = {}
    for table in ordered:
        if not table.has_field("states_code"):
            continue
        for (state, _year), record in table.records.items():
            code = record.get("states_code")
            if code and state not in mapping:
                mapping[state] = code
    return mapping
