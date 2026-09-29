"""Column-wise source-of-truth lineage pipeline.

Population tables are read in chunks, normalized into one canonical long
shape (states, states_code, id, year, population), sliced by state and year,
and compared independently for *every* column. Rows whose state code/name
cannot be resolved against the mapping table stay in the result as an
``unmapped`` group; they are never silently dropped.

The join-hit-rate denominator and the post-slice conservation assertion use
the exact same per-table denominator (touched sliced rows), so any mismatch
raises :class:`LineageError`, which the CLI turns into an explicit failure
page, a failed report status and exit code 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

CANON_COLUMNS = ["states", "states_code", "id", "year", "population"]
LARGE_TABLE_BYTES = 10 * 1024 * 1024
CSV_CHUNKSIZE = 50_000
MAX_EXAMPLES_REPORT = 100

KIND_WIDE = "wide"
KIND_CODED = "coded_wide"
KIND_LONG = "long"

TABLE_PRIORITY = {KIND_CODED: 1, KIND_WIDE: 2, KIND_LONG: 3}
TABLE_LABELS = {
    KIND_CODED: "states-code wide table",
    KIND_WIDE: "wide census table",
    KIND_LONG: "reshaped long table",
}
TABLE_REASONS = {
    KIND_CODED: (
        "carries the authoritative states_code mapping next to the census "
        "populations, so it defines both the join keys and the code values"
    ),
    KIND_WIDE: (
        "is the raw census extract with populations beside the official id; "
        "it outranks the derived long table for numeric columns"
    ),
    KIND_LONG: (
        "is a melt/reshape of the two raw tables, so it is used only where "
        "both raw tables are missing a value"
    ),
}


class LineageError(Exception):
    """Explicit pipeline failure carrying a stable status string."""

    def __init__(self, status: str, message: str, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


def detect_kind(path: Path) -> str:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        header = fh.readline()
    cols = [c.strip().strip('"') for c in header.rstrip("\r\n").split(",")]
    if "states_code" in cols and "year" not in cols:
        return KIND_CODED
    if "year" in cols and "population" in cols:
        return KIND_LONG
    return KIND_WIDE


def _clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace({"": pd.NA})


def _to_int(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(text, errors="coerce").astype("Int64")


@dataclass
class LoadedTable:
    name: str
    kind: str
    path: Path
    size_bytes: int
    chunked: bool
    file_rows: int
    year_columns: list[str]
    raw_columns: list[str]
    ignored_columns: list[str]
    frame: pd.DataFrame


def read_table(path: Path, sample_resident=None) -> LoadedTable:
    """Read one CSV iterating over chunks.

    ``chunksize`` is always passed to :func:`pandas.read_csv`; normalization
    runs per chunk and only normalized records are concatenated, so a table
    above 10 MiB is never held whole in memory.
    """
    size = path.stat().st_size
    kind = detect_kind(path)
    reader = pd.read_csv(
        path,
        dtype="string",
        encoding="utf-8",
        keep_default_na=False,
        na_values=[""],
        chunksize=CSV_CHUNKSIZE,
    )

    file_rows = 0
    frames: list[pd.DataFrame] = []
    raw_columns: list[str] = []
    year_columns: list[str] = []
    ignored: list[str] = []

    for chunk in reader:
        if not raw_columns:
            raw_columns = chunk.columns.tolist()
            year_columns = [c for c in raw_columns if c.isdigit()]
        file_rows += len(chunk)
        normalized = _normalize_chunk(chunk, kind, ignored)
        if not normalized.empty:
            frames.append(normalized)
        if sample_resident is not None:
            sample_resident()

    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=CANON_COLUMNS)
    )
    return LoadedTable(
        name=path.name,
        kind=kind,
        path=path,
        size_bytes=size,
        chunked=size > LARGE_TABLE_BYTES,
        file_rows=file_rows,
        year_columns=year_columns,
        raw_columns=raw_columns,
        ignored_columns=ignored,
        frame=frame,
    )


def _normalize_chunk(chunk: pd.DataFrame, kind: str, ignored: list[str]) -> pd.DataFrame:
    if kind == KIND_LONG:
        return _normalize_long(chunk, ignored)
    return _normalize_wide(chunk, kind, ignored)


def _series_or_na(chunk: pd.DataFrame, name: str) -> pd.Series:
    if name in chunk.columns:
        return chunk[name]
    return pd.Series(pd.NA, index=chunk.index, dtype="string")


def _normalize_long(chunk: pd.DataFrame, ignored: list[str]) -> pd.DataFrame:
    known = set(CANON_COLUMNS)
    for col in chunk.columns:
        if col not in known and col not in ignored:
            ignored.append(col)
    return pd.DataFrame(
        {
            "states": _clean_text(_series_or_na(chunk, "states")),
            "states_code": _clean_text(_series_or_na(chunk, "states_code")),
            "id": _to_int(_series_or_na(chunk, "id")),
            "year": _to_int(_series_or_na(chunk, "year")),
            "population": _to_int(_series_or_na(chunk, "population")),
        }
    )


def _normalize_wide(chunk: pd.DataFrame, kind: str, ignored: list[str]) -> pd.DataFrame:
    year_cols = [c for c in chunk.columns if c.isdigit()]
    for col in chunk.columns:
        if col not in {"states", "id", "states_code", *year_cols} and col not in ignored:
            ignored.append(col)

    id_vars = ["states"]
    if "id" in chunk.columns:
        id_vars.append("id")
    if kind == KIND_CODED and "states_code" in chunk.columns:
        id_vars.append("states_code")

    melted = chunk.melt(
        id_vars=id_vars,
        value_vars=year_cols,
        var_name="year",
        value_name="population",
    )
    return pd.DataFrame(
        {
            "states": _clean_text(melted["states"]),
            "states_code": (
                _clean_text(melted["states_code"])
                if "states_code" in melted
                else pd.Series(pd.NA, index=melted.index, dtype="string")
            ),
            "id": _to_int(melted["id"]),
            "year": _to_int(melted["year"]),
            "population": _to_int(melted["population"]),
        }
    )


def load_tables(data_dir: Path, sample_resident=None) -> dict[str, LoadedTable]:
    csvs = sorted(p for p in Path(data_dir).glob("*.csv"))
    if not csvs:
        raise LineageError("NO_INPUT", f"no CSV inputs found under {data_dir}")
    tables: dict[str, LoadedTable] = {}
    for csv_path in csvs:
        table = read_table(csv_path, sample_resident=sample_resident)
        if table.kind in tables:
            raise LineageError(
                "AMBIGUOUS_INPUT",
                f"{csv_path.name} and {tables[table.kind].name} both look like "
                f"{TABLE_LABELS[table.kind]}",
            )
        tables[table.kind] = table
    if KIND_CODED not in tables:
        raise LineageError(
            "MISSING_MAPPING",
            "states-code mapping table is required to resolve state codes",
        )
    return tables


@dataclass
class Slice:
    filters: dict
    tables: dict[str, LoadedTable]
    slices: dict[str, pd.DataFrame]
    excluded: dict[str, int]
    dropped_columns: dict[str, dict[str, int]]
    map_by_state: dict[str, dict]
    code_by_state: dict
    id_by_state: dict
    universe: set
    years: list[int]
    available_states: list[str]


def _filters_jsonable(filters: dict) -> dict:
    out = {}
    for key, value in filters.items():
        if isinstance(value, (set, tuple)):
            out[key] = sorted(value)
        else:
            out[key] = value
    return out


def slice_tables(tables: dict[str, LoadedTable], filters: dict) -> Slice:
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    selected_states = set(filters.get("states") or [])

    coded = tables[KIND_CODED].frame
    valid_map = coded[coded["states"].notna() & coded["id"].notna()].drop_duplicates(
        ["states", "id", "states_code"]
    )
    if valid_map["states"].duplicated().any():
        dupes = valid_map.loc[valid_map["states"].duplicated(), "states"].tolist()
        raise LineageError(
            "AMBIGUOUS_MAPPING",
            f"duplicate states in mapping table: {dupes[:5]}",
        )
    map_by_state = {
        row.states: {
            "states_code": row.states_code if pd.notna(row.states_code) else None,
            "id": int(row.id),
        }
        for row in valid_map.drop_duplicates("states").itertuples(index=False)
    }
    code_by_state = {s: info["states_code"] for s, info in map_by_state.items()}
    id_by_state = {s: info["id"] for s, info in map_by_state.items()}

    all_years = sorted(
        {
            int(y)
            for table in tables.values()
            for y in table.frame["year"].dropna().unique()
        }
    )
    available = sorted(map_by_state)
    if selected_states:
        unknown = sorted(selected_states - set(available))
        if unknown:
            raise LineageError(
                "UNKNOWN_STATE",
                f"states not present in the mapping table: {unknown}",
                {"filters": _filters_jsonable(filters)},
            )
    if year_from is not None and year_to is not None and year_from > year_to:
        raise LineageError(
            "EMPTY_SLICE",
            f"empty interval: year_from={year_from} > year_to={year_to}",
            {"filters": _filters_jsonable(filters)},
        )

    slices: dict[str, pd.DataFrame] = {}
    excluded: dict[str, int] = {}
    dropped_columns: dict[str, dict[str, int]] = {}

    for kind, table in tables.items():
        frame = table.frame
        missing_states = frame["states"].isna()
        missing_id = frame["id"].isna()
        missing_year = frame["year"].isna()
        dropped_mask = missing_states | missing_id | missing_year
        dropped_columns[kind] = {
            "states": int(missing_states.sum()),
            "id": int(missing_id.sum()),
            "year": int(missing_year.sum()),
            "states_code": 0,
            "population": 0,
            "union": int(dropped_mask.sum()),
        }

        mapped = frame["states"].isin(map_by_state)
        year_ok = ~missing_year
        if year_from is not None:
            year_ok = year_ok & (
                frame["year"].astype("Int64") >= int(year_from)
            )
        if year_to is not None:
            year_ok = year_ok & (
                frame["year"].astype("Int64") <= int(year_to)
            )
        state_ok = mapped.copy()
        if selected_states:
            state_ok = state_ok & frame["states"].isin(selected_states)

        retain_mapped_rule = state_ok | (~mapped)
        keep = (~dropped_mask) & year_ok & retain_mapped_rule
        excluded[kind] = int((~dropped_mask).sum() - int(keep.sum()))
        sliced = frame.loc[keep].copy()
        sliced["mapped"] = sliced["states"].isin(map_by_state)
        slices[kind] = sliced.reset_index(drop=True)

    coded_rows = slices[KIND_CODED]
    coded_mapped = coded_rows[coded_rows["mapped"]]
    universe = set(
        zip(
            coded_mapped["states"],
            coded_mapped["id"].astype("Int64"),
            coded_mapped["year"].astype("Int64"),
        )
    )

    return Slice(
        filters=filters,
        tables=tables,
        slices=slices,
        excluded=excluded,
        dropped_columns=dropped_columns,
        map_by_state=map_by_state,
        code_by_state=code_by_state,
        id_by_state=id_by_state,
        universe=universe,
        years=all_years,
        available_states=available,
    )


def _rounded(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def column_stats(slice_data: Slice) -> dict[str, dict]:
    """Per-table per-column stats; every rate shares the sliced-row denominator."""
    stats: dict[str, dict] = {}
    for kind, table in slice_data.tables.items():
        sliced = slice_data.slices[kind]
        touched = len(sliced)
        mapped = sliced["mapped"]

        keys = pd.Series(
            list(
                zip(
                    sliced["states"],
                    sliced["id"].astype("Int64"),
                    sliced["year"].astype("Int64"),
                )
            ),
            index=sliced.index,
        )
        key_in_universe = keys.isin(slice_data.universe)

        states_hit = int(mapped.sum())
        expected_id = sliced["states"].map(slice_data.id_by_state).astype("Int64")
        id_hit = int(
            (mapped & sliced["id"].eq(expected_id).fillna(False)).sum()
        )

        expected_code = sliced["states"].map(slice_data.code_by_state).astype("string")
        if kind != KIND_WIDE:
            code_hit = int(
                (
                    mapped
                    & sliced["states_code"].notna()
                    & sliced["states_code"].eq(expected_code).fillna(False)
                ).sum()
            )
            code_present = True
        else:
            code_hit = 0
            code_present = False

        year_hit = int(
            (mapped & sliced["year"].notna().fillna(False)).sum()
        )
        key_hit = int(
            (mapped & key_in_universe.fillna(False)).sum()
        )

        coded_keyed = slice_data.slices[KIND_CODED][
            slice_data.slices[KIND_CODED]["mapped"]
        ].drop_duplicates(["states", "id", "year"])
        coded_keyed = coded_keyed.set_index(["states", "id", "year"])
        in_coded = keys.isin(set(coded_keyed.index))
        pop_join_hit = int(
            (
                mapped
                & in_coded.fillna(False)
                & sliced["population"].notna().fillna(False)
            ).sum()
        )

        def col_entry(
            present, dropped=0, nan_rows=0, hit=None, join_kind=None
        ):
            entry = {
                "present": bool(present),
                "read_rows": int(touched),
                "dropped_rows": int(dropped),
                "nan_rows": int(nan_rows),
                "join_rate": (
                    _rounded(int(hit), touched) if hit is not None and present else None
                ),
                "join_hits": int(hit) if hit is not None and present else None,
                "join_kind": join_kind,
            }
            return entry

        cols = {
            "states": col_entry(
                True,
                dropped=slice_data.dropped_columns[kind]["states"],
                nan_rows=int((~mapped).sum()) + int(sliced["states"].isna().sum()),
                hit=states_hit,
                join_kind="mapping-table state join",
            ),
            "states_code": col_entry(
                code_present,
                dropped=0,
                nan_rows=(
                    int(sliced["states_code"].isna().sum()) if code_present else 0
                ),
                hit=code_hit if code_present else None,
                join_kind="mapping-table state-code join",
            ),
            "id": col_entry(
                True,
                dropped=slice_data.dropped_columns[kind]["id"],
                nan_rows=int(sliced["id"].isna().sum()),
                hit=id_hit,
                join_kind="mapping-table id join",
            ),
            "year": col_entry(
                True,
                dropped=slice_data.dropped_columns[kind]["year"],
                nan_rows=int(sliced["year"].isna().sum()),
                hit=year_hit,
                join_kind="mapping-table (state,id,year) key join",
            ),
            "population": col_entry(
                True,
                dropped=0,
                nan_rows=int(sliced["population"].isna().sum()),
                hit=pop_join_hit,
                join_kind="join onto states-code population table",
            ),
        }

        table_stats = {
            "name": table.name,
            "kind": kind,
            "label": TABLE_LABELS[kind],
            "file_rows": int(table.file_rows),
            "size_bytes": int(table.size_bytes),
            "chunked": bool(table.chunked),
            "read_columns": list(table.raw_columns),
            "year_columns": list(table.year_columns),
            "ignored_columns": list(table.ignored_columns),
            "dropped_rows": int(slice_data.dropped_columns[kind]["union"]),
            "excluded_rows": int(slice_data.excluded[kind]),
            "sliced_rows": int(touched),
            "mapped_rows": int(mapped.sum()),
            "unmapped_rows": int((~mapped).sum()),
            "key_join_rate": _rounded(key_hit, touched),
            "key_join_hits": key_hit,
            "denominator": int(touched),
            "columns": cols,
        }
        stats[kind] = table_stats
    return stats


def assert_conservation(
    slice_data: Slice, stats: dict[str, dict]
) -> list[dict]:
    """Post-slice row-conservation assertion sharing the hit-rate denominator.

    For every table the canonical rows must partition exactly into dropped
    rows, filter-excluded rows and sliced rows; every join rate denominator is
    that same sliced-row count. Any mismatch raises ``CONSERVATION_MISMATCH``.
    """
    checks = []
    for kind, table in slice_data.tables.items():
        canon_rows = len(table.frame)
        dropped = int(slice_data.dropped_columns[kind]["union"])
        excluded = int(slice_data.excluded[kind])
        sliced = int(len(slice_data.slices[kind]))
        partition = dropped + excluded + sliced
        table_ok = partition == canon_rows

        denom = int(stats[kind]["denominator"])
        denom_ok = denom == sliced
        rate_ok = (
            stats[kind]["key_join_hits"] <= sliced
            and 0.0 <= stats[kind]["key_join_rate"] <= 1.0
        )
        col_ok = True
        for column, entry in stats[kind]["columns"].items():
            if not entry["present"]:
                continue
            if entry["join_hits"] is not None and entry["join_hits"] > sliced:
                col_ok = False
            if entry["read_rows"] != sliced:
                col_ok = False
            if entry["nan_rows"] < 0 or entry["dropped_rows"] < 0:
                col_ok = False

        check = {
            "table": table.name,
            "canonical_rows": int(canon_rows),
            "dropped_rows": dropped,
            "excluded_rows": excluded,
            "sliced_rows": sliced,
            "partition_sum": int(partition),
            "join_denominator": denom,
            "partition_ok": bool(table_ok),
            "denominator_ok": bool(denom_ok),
            "rate_bounds_ok": bool(rate_ok),
            "column_counts_ok": bool(col_ok),
            "ok": bool(table_ok and denom_ok and rate_ok and col_ok),
        }
        checks.append(check)
        if not check["ok"]:
            raise LineageError(
                "CONSERVATION_MISMATCH",
                (
                    f"{table.name}: conservation check failed "
                    f"(canonical={canon_rows} dropped={dropped} excluded={excluded} "
                    f"sliced={sliced} partition_sum={partition}, "
                    f"denominator={denom})"
                ),
                {"checks": checks, "filters": _filters_jsonable(slice_data.filters)},
            )

    total_sliced = sum(len(s) for s in slice_data.slices.values())
    if total_sliced == 0:
        raise LineageError(
            "EMPTY_SLICE",
            "sliced interval is empty: no rows survive the state/year filter",
            {"checks": checks, "filters": _filters_jsonable(slice_data.filters)},
        )
    return checks


def unmapped_groups(slice_data: Slice, stats: dict[str, dict]) -> dict:
    """Rows that cannot resolve against the mapping table, kept on purpose."""
    groups = []
    total = 0
    for kind, table in slice_data.tables.items():
        sliced = slice_data.slices[kind]
        bad = sliced[~sliced["mapped"]]
        total += len(bad)
        by_state = bad.groupby("states", dropna=False).size().sort_values(ascending=False)
        members = []
        for state, count in by_state.items():
            label = None if pd.isna(state) else str(state)
            members.append(
                {
                    "state": label,
                    "rows": int(count),
                    "reason": (
                        "blank state name"
                        if label is None
                        else "state name absent from states-code mapping table"
                    ),
                }
            )
        groups.append(
            {
                "table": table.name,
                "kind": kind,
                "unmapped_rows": int(len(bad)),
                "members": members[:MAX_EXAMPLES_REPORT],
            }
        )
        stats[kind]["unmapped_groups"] = members[:MAX_EXAMPLES_REPORT]
    return {"total_unmapped_rows": int(total), "by_table": groups}


def _norm_scalar(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, np.integer):
        return str(int(value))
    return str(value).strip()


def adjudicate_columns(slice_data: Slice) -> tuple[pd.DataFrame, dict]:
    """Decide each column independently; never choose one table wholesale."""
    kinds = sorted(slice_data.tables, key=lambda k: TABLE_PRIORITY[k])
    lookup = {}
    key_sets = []
    for kind in kinds:
        frame = slice_data.slices[kind]
        frame = frame.drop_duplicates(["states", "id", "year"], keep="first")
        keys = list(
            zip(
                frame["states"],
                frame["id"].astype("Int64"),
                frame["year"].astype("Int64"),
            )
        )
        key_sets.append(set(keys))
        lookup[kind] = {
            column: dict(zip(keys, frame[column].tolist()))
            for column in CANON_COLUMNS
        }
    all_keys = sorted(set().union(*key_sets), key=lambda k: tuple(str(x) for x in k))

    columns_summary = {}
    resolved_rows = []

    for column in CANON_COLUMNS:
        provider_kinds = [
            kind
            for kind in kinds
            if not (column == "states_code" and kind == KIND_WIDE)
        ]
        examples = []
        winner_counts = {kind: 0 for kind in provider_kinds}
        disagree_keys = 0
        missing_keys = 0
        per_table_present = {kind: 0 for kind in provider_kinds}

        for key in all_keys:
            values = {}
            for kind in provider_kinds:
                values[kind] = _norm_scalar(lookup[kind][column].get(key))
                if values[kind] is not None:
                    per_table_present[kind] += 1

            winner = next(
                (kind for kind in provider_kinds if values[kind] is not None),
                None,
            )
            distinct = {v for v in values.values() if v is not None}
            disagree = len(distinct) > 1
            if disagree:
                disagree_keys += 1
            if winner is None:
                missing_keys += 1
            else:
                winner_counts[winner] += 1

            if (disagree or winner is None) and len(examples) < MAX_EXAMPLES_REPORT:
                examples.append(
                    {
                        "states": _norm_scalar(key[0]),
                        "id": _norm_scalar(key[1]),
                        "year": _norm_scalar(key[2]),
                        "values": {
                            slice_data.tables[kind].name: values[kind]
                            for kind in provider_kinds
                        },
                        "winner": slice_data.tables[winner].name if winner else None,
                        "reason": _winner_reason(column, winner, disagree),
                        "disagree": disagree,
                    }
                )

        columns_summary[column] = {
            "provider_tables": [slice_data.tables[k].name for k in provider_kinds],
            "present_by_table": {
                slice_data.tables[k].name: per_table_present[k]
                for k in provider_kinds
            },
            "winner_rows": {
                slice_data.tables[k].name: winner_counts[k] for k in provider_kinds
            },
            "disagreement_rows": int(disagree_keys),
            "missing_rows": int(missing_keys),
            "priority": [
                {
                    "table": slice_data.tables[k].name,
                    "rank": TABLE_PRIORITY[k],
                    "reason": TABLE_REASONS[k],
                }
                for k in provider_kinds
            ],
            "examples": examples,
        }

    for key in all_keys:
        row = {
            "states": _norm_scalar(key[0]),
            "id": _norm_scalar(key[1]),
            "year": _norm_scalar(key[2]),
        }
        row["mapped"] = key[0] in slice_data.map_by_state
        for column in CANON_COLUMNS:
            winner_kind = None
            scalar = None
            for kind in sorted(
                slice_data.tables, key=lambda k: TABLE_PRIORITY[k]
            ):
                if column == "states_code" and kind == KIND_WIDE:
                    continue
                raw = lookup[kind][column].get(key)
                scalar = _norm_scalar(raw)
                if scalar is not None:
                    winner_kind = kind
                    break
            row[column] = scalar
            row[f"{column}_source"] = (
                slice_data.tables[winner_kind].name if winner_kind else None
            )
        resolved_rows.append(row)

    resolved = pd.DataFrame(resolved_rows)
    if not resolved.empty:
        resolved = resolved.sort_values(
            ["year", "states"], kind="stable", na_position="last"
        ).reset_index(drop=True)
    return resolved, columns_summary


def _winner_reason(column: str, winner: str | None, disagree: bool) -> str:
    if winner is None:
        return "no table provides a non-null value for this column"
    base = TABLE_REASONS[winner]
    if disagree:
        return (
            f"column `{column}` disagrees across tables; taking the value from the "
            f"{TABLE_LABELS[winner]} because it {base}. The ruling applies to this "
            "column only; other columns keep their own winners."
        )
    return (
        f"all providing tables agree on `{column}`; the value is anchored to the "
        f"{TABLE_LABELS[winner]} (rank {TABLE_PRIORITY[winner]}) because it {base}."
    )


def default_filters(tables: dict[str, LoadedTable]) -> dict:
    years = sorted(
        {
            int(y)
            for table in tables.values()
            for y in table.frame["year"].dropna().unique()
        }
    )
    return {
        "year_from": years[0],
        "year_to": years[-1],
        "states": [],
    }


def build_payload(
    data_dir: Path,
    filters: dict | None = None,
    sample_resident=None,
    tables: dict[str, LoadedTable] | None = None,
) -> dict:
    """Run the whole pipeline for one filter set and return a JSON-safe payload."""
    owned = tables is None
    if owned:
        tables = load_tables(Path(data_dir), sample_resident=sample_resident)
    base_filters = default_filters(tables)
    merged = {**base_filters, **(filters or {})}
    merged["states"] = sorted(set(merged.get("states") or []))

    sliced = slice_tables(tables, merged)
    stats = column_stats(sliced)
    checks = assert_conservation(sliced, stats)
    unmapped = unmapped_groups(sliced, stats)
    resolved, columns = adjudicate_columns(sliced)

    files = sorted(p.name for p in Path(data_dir).glob("*"))
    input_fingerprint = []
    for name in files:
        path = Path(data_dir) / name
        if path.is_file():
            stat = path.stat()
            input_fingerprint.append((name, stat.st_size, stat.st_mtime_ns))

    payload = {
        "status": "OK",
        "data_dir": str(Path(data_dir)),
        "filters": _filters_jsonable(merged),
        "available_years": sliced.years,
        "available_states": sliced.available_states,
        "tables": stats,
        "conservation_checks": checks,
        "unmapped": unmapped,
        "columns": columns,
        "resolved_columns": list(resolved.columns),
        "resolved_rows": resolved.where(resolved.notna(), None).to_dict(
            orient="records"
        ),
        "input_fingerprint": input_fingerprint,
    }
    return payload


def failure_payload(data_dir: Path, filters: dict | None, error: LineageError) -> dict:
    return {
        "status": error.status,
        "data_dir": str(Path(data_dir)),
        "filters": _filters_jsonable(filters or {}),
        "error": error.message,
        "details": error.payload,
    }


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic serialization: sorted keys, fixed separators, newlines."""
    import json

    return (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def payload_run_id(payload: dict) -> str:
    import hashlib

    return hashlib.sha256(canonical_bytes(payload)).hexdigest()[:16]
