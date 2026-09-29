import pandas as pd
import pytest

from lineage import pipeline
from lineage.pipeline import (
    KIND_CODED,
    KIND_LONG,
    KIND_WIDE,
    LARGE_TABLE_BYTES,
    LineageError,
)


def test_real_data_full_slice_stats_and_conservation(real_data_dir):
    tables = pipeline.load_tables(real_data_dir)
    payload = pipeline.build_payload(real_data_dir, tables=tables)

    assert payload["status"] == "OK"
    assert payload["available_years"] == list(range(2010, 2020))
    assert len(payload["available_states"]) == 52

    for kind, table in payload["tables"].items():
        assert table["sliced_rows"] == 520
        assert table["denominator"] == 520
        assert table["key_join_rate"] == 1.0
        assert table["unmapped_rows"] == 0
        for column, entry in table["columns"].items():
            if entry["present"]:
                assert entry["read_rows"] == 520
                assert entry["join_rate"] == 1.0
                assert entry["join_hits"] == 520
                assert entry["nan_rows"] == 0

    assert payload["unmapped"]["total_unmapped_rows"] == 0
    for check in payload["conservation_checks"]:
        assert check["ok"]
        assert check["canonical_rows"] == check["partition_sum"]
        assert check["join_denominator"] == check["sliced_rows"]


def test_filter_change_recomputes_slice(real_data_dir):
    tables = pipeline.load_tables(real_data_dir)
    payload = pipeline.build_payload(
        real_data_dir,
        filters={
            "year_from": 2015,
            "year_to": 2015,
            "states": ["Alaska", "Wyoming"],
        },
        tables=tables,
    )
    for table in payload["tables"].values():
        assert table["sliced_rows"] == 2
        assert table["excluded_rows"] == 518
        assert table["dropped_rows"] == 0
        assert table["denominator"] == 2
        check = next(
            c for c in payload["conservation_checks"] if c["table"] == table["name"]
        )
        assert check["sliced_rows"] == 2
        assert check["partition_sum"] == 520
    assert len(payload["resolved_rows"]) == 2


def test_unmapped_states_are_retained_not_dropped(disagreement_dir):
    payload = pipeline.build_payload(disagreement_dir)
    assert payload["status"] == "OK"

    totals = {
        kind: table["unmapped_rows"]
        for kind, table in payload["tables"].items()
    }
    # Newstate has 2 years and is absent from the coded mapping table.
    assert totals[KIND_WIDE] == 2
    assert totals[KIND_LONG] == 2
    assert totals[KIND_CODED] == 0

    unmapped = payload["unmapped"]
    assert unmapped["total_unmapped_rows"] == 4
    members = {
        group["kind"]: {m["state"]: m["rows"] for m in group["members"]}
        for group in unmapped["by_table"]
    }
    assert members[KIND_WIDE]["Newstate"] == 2

    resolved_states = {row["states"] for row in payload["resolved_rows"]}
    assert "Newstate" in resolved_states
    newstate_rows = [
        row for row in payload["resolved_rows"] if row["states"] == "Newstate"
    ]
    assert all(row["mapped"] is False for row in newstate_rows)


def test_per_column_adjudication_is_column_local(disagreement_dir):
    payload = pipeline.build_payload(disagreement_dir)
    columns = payload["columns"]

    pop = columns["population"]
    assert pop["disagreement_rows"] == 1  # Wyoming 2010: 200 vs 999
    winner_files = pop["winner_rows"]
    coded_file = payload["tables"][KIND_CODED]["name"]
    long_file = payload["tables"][KIND_LONG]["name"]
    # Coded table wins Wyoming 2010; long table still keeps Nevada 2011/... it
    # only uniquely provides Nevada 2010? That is missing everywhere -> see below.
    assert winner_files[coded_file] >= 1

    # Wyoming 2010 disagreement example records both values and a reason that
    # explicitly limits the ruling to this column.
    example = next(
        ex
        for ex in pop["examples"]
        if ex["states"] == "Wyoming" and ex["year"] == "2010"
    )
    assert example["disagree"] is True
    assert example["winner"] == coded_file
    assert "column only" in example["reason"]

    # states_code: wide table cannot provide it; long's "XX" loses to "WY".
    code = columns["states_code"]
    code_example = next(
        ex
        for ex in code["examples"]
        if ex["states"] == "Wyoming" and ex["year"] == "2010"
    )
    assert code_example["disagree"] is True
    assert code_example["winner"] == coded_file
    providers = [item["table"] for item in code["priority"]]
    assert long_file in providers
    assert all(item["reason"] for item in code["priority"])

    # No table chooses wholesale: winner source varies by resolved column.
    wyoming_2010 = next(
        row
        for row in payload["resolved_rows"]
        if row["states"] == "Wyoming" and row["year"] == "2010"
    )
    assert wyoming_2010["population"] == "200"
    assert wyoming_2010["population_source"] == coded_file
    assert wyoming_2010["states_code"] == "WY"
    assert wyoming_2010["states_code_source"] == coded_file


def test_missing_value_kept_missing_per_column(disagreement_dir):
    payload = pipeline.build_payload(disagreement_dir)
    nevada_2010 = next(
        row
        for row in payload["resolved_rows"]
        if row["states"] == "Nevada" and row["year"] == "2010"
    )
    # The long row has a blank population; coded table provides 300, so
    # adjudication picks coded for this column independently.
    assert nevada_2010["population"] == "300"
    assert payload["columns"]["population"]["missing_rows"] == 0


def test_empty_year_interval_failure_payload(disagreement_dir):
    with pytest.raises(LineageError) as exc_info:
        pipeline.build_payload(
            disagreement_dir,
            filters={"year_from": 2011, "year_to": 2010},
        )
    assert exc_info.value.status == "EMPTY_SLICE"


def test_empty_slice_within_bounds_fails(disagreement_dir):
    with pytest.raises(LineageError) as exc_info:
        pipeline.build_payload(
            disagreement_dir,
            filters={"year_from": 2030, "year_to": 2031},
        )
    assert exc_info.value.status == "EMPTY_SLICE"


def test_unknown_state_is_explicit_error(disagreement_dir):
    with pytest.raises(LineageError) as exc_info:
        pipeline.build_payload(disagreement_dir, filters={"states": ["Atlantis"]})
    assert exc_info.value.status == "UNKNOWN_STATE"


def test_conservation_violation_detected(monkeypatch, disagreement_dir):
    original = pipeline.column_stats

    def broken_stats(slice_data):
        stats = original(slice_data)
        for table in stats.values():
            table["denominator"] = table["denominator"] + 1
        return stats

    monkeypatch.setattr(pipeline, "column_stats", broken_stats)
    with pytest.raises(LineageError) as exc_info:
        pipeline.build_payload(disagreement_dir)
    assert exc_info.value.status == "CONSERVATION_MISMATCH"


def test_chunked_read_for_large_table(large_csv_dir):
    monitor_calls = []
    tables = pipeline.load_tables(
        large_csv_dir, sample_resident=lambda: monitor_calls.append(1)
    )
    long_table = tables[KIND_LONG]
    assert long_table.size_bytes > LARGE_TABLE_BYTES
    assert long_table.chunked is True
    assert len(monitor_calls) >= 2  # multiple chunks sampled
    assert long_table.file_rows == 500_000
    payload = pipeline.build_payload(
        large_csv_dir, tables=tables
    )
    # Rows for 2 years but coded mapping only has years 2010/2011 -> all join.
    assert payload["tables"][KIND_LONG]["sliced_rows"] == 500_000


def test_dropped_rows_attributed_per_column(tmp_path):
    coded = tmp_path / "coded.csv"
    coded.write_text(
        "states,states_code,id,2010\n"
        "Alabama,AL,1,100\n",
        encoding="utf-8",
    )
    long = tmp_path / "long.csv"
    long.write_text(
        "states,states_code,id,year,population\n"
        "Alabama,AL,1,2010,100\n"
        ",AL,1,2010,101\n"
        "Alabama,AL,,2010,102\n",
        encoding="utf-8",
    )
    payload = pipeline.build_payload(tmp_path)
    long_stats = payload["tables"][KIND_LONG]
    assert long_stats["dropped_rows"] == 2
    assert long_stats["columns"]["states"]["dropped_rows"] == 1
    assert long_stats["columns"]["id"]["dropped_rows"] == 1
    assert long_stats["sliced_rows"] == 1
