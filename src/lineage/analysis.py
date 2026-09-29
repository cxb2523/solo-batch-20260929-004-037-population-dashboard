"""Slice, row-conservation, join hit-rate and per-column adjudication."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ingest import FIELDS, Table

# Column priority is decided per column, never "pick one whole table".
COLUMN_PRIORITY = {
    "reshaped": 1,
    "states_code": 2,
    "wide": 3,
}
FIELD_LABELS = {
    "id": "州 ID",
    "states_code": "州码",
    "population": "人口",
}


class LineageError(Exception):
    """A condition that must surface as an explicit failure (exit code 2)."""


@dataclass
class SliceTable:
    table: Table
    rows: dict[tuple[str, int], dict[str, Any]]
    mapped_rows: dict[tuple[str, int], dict[str, Any]]
    unmapped_rows: dict[tuple[str, int], dict[str, Any]]
    out_of_slice: int
    invalid_rows: int
    duplicate_records: int
    emitted: int

    @property
    def denominator(self) -> int:
        return len(self.mapped_rows) + len(self.unmapped_rows)

    @property
    def dropped_total(self) -> int:
        return self.invalid_rows + self.out_of_slice

    def nan_rows(self, field: str) -> int:
        if not self.table.has_field(field):
            return 0
        return sum(1 for row in self.rows.values() if row.get(field) is None)


def _in_slice(
    state: str,
    year: int,
    states: set[str] | None,
    year_from: int | None,
    year_to: int | None,
) -> bool:
    if states is not None and state not in states:
        return False
    if year_from is not None and year < year_from:
        return False
    if year_to is not None and year > year_to:
        return False
    return True


def slice_tables(
    tables: list[Table],
    mapping: dict[str, str],
    states: list[str] | None,
    years: tuple[int | None, int | None],
) -> list[SliceTable]:
    states_set = set(states) if states is not None else None
    year_from, year_to = years
    result: list[SliceTable] = []
    for table in tables:
        rows: dict[tuple[str, int], dict[str, Any]] = {}
        mapped_rows: dict[tuple[str, int], dict[str, Any]] = {}
        unmapped_rows: dict[tuple[str, int], dict[str, Any]] = {}
        out_of_slice = 0
        for key, record in table.records.items():
            state, year = key
            if not _in_slice(state, year, states_set, year_from, year_to):
                out_of_slice += 1
                continue
            rows[key] = record
            if state in mapping:
                mapped_rows[key] = record
            else:
                # Unmapped codes are bucketed, never silently dropped.
                unmapped_rows[key] = record

        year_span = len(table.year_columns) if table.kind == "wide" else 1
        emitted = (table.physical_rows - table.invalid_rows) * year_span
        if emitted != len(table.records) + table.duplicate_records:
            raise LineageError(
                f"表 {table.label} 行数不守恒：物理有效行展开 {emitted} != "
                f"去重键 {len(table.records)} + 重复 {table.duplicate_records}"
            )
        bucket_total = out_of_slice + len(mapped_rows) + len(unmapped_rows)
        if len(table.records) != bucket_total:
            raise LineageError(
                f"表 {table.label} 切片后行数不守恒：有效键 {len(table.records)} != "
                f"区间外 {out_of_slice} + 命中映射 {len(mapped_rows)} + "
                f"unmapped {len(unmapped_rows)}"
            )
        result.append(
            SliceTable(
                table=table,
                rows=rows,
                mapped_rows=mapped_rows,
                unmapped_rows=unmapped_rows,
                out_of_slice=out_of_slice,
                invalid_rows=table.invalid_rows,
                duplicate_records=table.duplicate_records,
                emitted=emitted,
            )
        )
    return result


def _equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    return a == b


def pair_columns(
    left: SliceTable, right: SliceTable, denominator: int
) -> dict[str, dict[str, Any]]:
    """Per-column join metrics. Denominator is the shared post-slice row count."""
    columns: dict[str, dict[str, Any]] = {}
    keys = set(left.rows) | set(right.rows)
    for field in FIELDS:
        left_has = left.table.has_field(field)
        right_has = right.table.has_field(field)
        if not (left_has and right_has):
            continue
        hit = 0
        disagreements = 0
        nan_left = 0
        nan_right = 0
        only_left = 0
        only_right = 0
        samples: list[dict[str, Any]] = []
        for key in sorted(keys):
            in_left = key in left.rows
            in_right = key in right.rows
            left_value = left.rows[key].get(field) if in_left else None
            right_value = right.rows[key].get(field) if in_right else None
            if in_left and left_value is None:
                nan_left += 1
            if in_right and right_value is None:
                nan_right += 1
            if in_left and not in_right:
                only_left += 1
            if in_right and not in_left:
                only_right += 1
            if not (in_left and in_right):
                continue
            if _equal(left_value, right_value):
                hit += 1
            else:
                disagreements += 1
                if len(samples) < 50:
                    samples.append(
                        {
                            "state": key[0],
                            "year": key[1],
                            "left": left_value,
                            "right": right_value,
                        }
                    )
        miss = denominator - hit
        columns[field] = {
            "hit": hit,
            "miss": miss,
            "disagreements": disagreements,
            "nan_left": nan_left,
            "nan_right": nan_right,
            "only_left": only_left,
            "only_right": only_right,
            "hit_rate": round(hit / denominator, 6) if denominator else None,
            "samples": samples,
        }
    return columns


def priority_rank(label: str) -> int:
    return COLUMN_PRIORITY.get(label, 9)


def adjudicate_columns(
    slices: list[SliceTable], pairs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pair_disagreements: dict[str, int] = {}
    for pair in pairs:
        for field, metrics in pair["columns"].items():
            pair_disagreements[field] = (
                pair_disagreements.get(field, 0) + metrics["disagreements"]
            )

    verdicts: list[dict[str, Any]] = []
    for field in FIELDS:
        participants = [slice_ for slice_ in slices if slice_.table.has_field(field)]
        if not participants:
            continue

        def sort_key(slice_: SliceTable) -> tuple[int, int, str]:
            table = slice_.table
            return (priority_rank(table.label), -len(table.records), table.label)

        ordered = sorted(participants, key=sort_key)
        winner = ordered[0]
        runner_up = ordered[1] if len(ordered) > 1 else None
        disagreement = pair_disagreements.get(field, 0)
        reasons = [
            f"逐列裁决，仅作用于列 {FIELD_LABELS[field]}（{field}），不整表二选一",
            f"固定优先级：{winner.table.label} 排名 {priority_rank(winner.table.label)}"
            + ("，高于其余表" if runner_up is None else f"，高于 {runner_up.table.label}"),
        ]
        if disagreement:
            reasons.append(f"该列在 {disagreement} 个切片键上存在分歧，优先取高优先级表的值")
        else:
            reasons.append("该列各表值一致，裁决用于声明唯一真源，未发生覆盖")
        winner_nan = winner.nan_rows(field)
        if winner_nan:
            reasons.append(f"胜出表该列切片内仍有 {winner_nan} 个 NaN，需回溯源数据")
        verdicts.append(
            {
                "field": field,
                "field_label": FIELD_LABELS[field],
                "winner": winner.table.label,
                "winner_priority": priority_rank(winner.table.label),
                "reason": "；".join(reasons),
                "disagreement_keys": disagreement,
                "participants": [
                    {"label": slice_.table.label, "priority": priority_rank(slice_.table.label)}
                    for slice_ in ordered
                ],
            }
        )
    return verdicts


def collect_unmapped(slices: list[SliceTable]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for slice_ in slices:
        for key, record in slice_.unmapped_rows.items():
            entry = merged.setdefault(
                key,
                {"state": key[0], "year": key[1], "sources": {}, "tables": []},
            )
            entry["sources"][slice_.table.label] = record.get("states_code")
            if slice_.table.label not in entry["tables"]:
                entry["tables"].append(slice_.table.label)
    return [merged[key] for key in sorted(merged)]


def analyze(
    tables: list[Table],
    mapping: dict[str, str],
    states: list[str] | None,
    years: tuple[int | None, int | None],
) -> dict[str, Any]:
    if not tables:
        raise LineageError("data 目录下未发现任何 .csv 输入表")
    unknown = [table.label for table in tables if table.kind == "unknown"]
    if unknown:
        raise LineageError(f"无法识别表结构（既非长表也非按年份宽表）：{', '.join(unknown)}")

    slices = slice_tables(tables, mapping, states, years)

    # Shared denominator: post-slice row count, identical for every table.
    denominators = {slice_.denominator for slice_ in slices}
    if len(denominators) != 1:
        detail = ", ".join(
            f"{slice_.table.label}={slice_.denominator}" for slice_ in slices
        )
        raise LineageError(
            f"命中率分母与切片行数口径不一致（各表切片行数不同）：{detail}"
        )
    denominator = denominators.pop()
    if denominator == 0:
        raise LineageError("筛选区间为空：切片后行数为 0，无命中率可计算")

    pairs: list[dict[str, Any]] = []
    ordered = sorted(slices, key=lambda item: priority_rank(item.table.label))
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            pairs.append(
                {
                    "left": left.table.label,
                    "right": right.table.label,
                    "denominator": denominator,
                    "columns": pair_columns(left, right, denominator),
                }
            )

    verdicts = adjudicate_columns(slices, pairs)
    unmapped = collect_unmapped(slices)

    conservation = []
    table_stats = []
    for slice_ in slices:
        table = slice_.table
        conservation.append(
            {
                "label": table.label,
                "physical_rows": table.physical_rows,
                "invalid_rows": slice_.invalid_rows,
                "duplicate_records": slice_.duplicate_records,
                "emitted": slice_.emitted,
                "valid_keys": len(table.records),
                "out_of_slice": slice_.out_of_slice,
                "mapped": len(slice_.mapped_rows),
                "unmapped": len(slice_.unmapped_rows),
                "denominator": slice_.denominator,
            }
        )
        table_stats.append(
            {
                "label": table.label,
                "kind": table.kind,
                "read_columns": {
                    field: (
                        "2010..2019 (按年份列)"
                        if field == "population" and table.kind == "wide" and table.year_columns
                        else table.spec[field]
                    )
                    if table.has_field(field)
                    else None
                    for field in FIELDS
                },
                "rows_slice": len(slice_.rows),
                "nan_rows": {
                    field: slice_.nan_rows(field) if table.has_field(field) else None
                    for field in FIELDS
                },
                "dropped_rows": {
                    "invalid": slice_.invalid_rows,
                    "out_of_slice": slice_.out_of_slice,
                    "duplicates": slice_.duplicate_records,
                    "total": slice_.dropped_total,
                },
            }
        )

    return {
        "slices": slices,
        "denominator": denominator,
        "pairs": pairs,
        "verdicts": verdicts,
        "unmapped": unmapped,
        "conservation": conservation,
        "table_stats": table_stats,
    }
