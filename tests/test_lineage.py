from __future__ import annotations

import csv
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lineage.ingest import LARGE_FILE_BYTES, ingest_directory
from lineage.report import atomic_write_bytes, load_report
from lineage.serve import REPORT_NAME, PAGE_NAME, run

REPO = Path(__file__).resolve().parent.parent
REAL_DATA = REPO / "data"

WIDE_HEADER = ["states", "id", "2010", "2011"]


def _write_csv(path: Path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


@pytest.fixture()
def data_dir(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    _write_csv(
        directory / "us-wide.csv",
        WIDE_HEADER,
        [
            ["Alabama", "1", "4785437", "4799069"],
            ["Alaska", "2", "713910", "722128"],
            ["Atlantis", "99", "10", "11"],
        ],
    )
    _write_csv(
        directory / "us-reshaped.csv",
        ["states", "states_code", "id", "year", "population"],
        [
            ["Alabama", "AL", "1", "2010", "4785437"],
            ["Alabama", "AL", "1", "2011", "4799070"],
            ["Alaska", "AK", "2", "2010", "713910"],
            ["Alaska", "AK", "2", "2011", "722128"],
            ["Atlantis", "", "99", "2010", "10"],
            ["Atlantis", "", "99", "2011", "11"],
            ["", "??", "3", "2010", "5"],
            ["BadYear", "??", "4", "xxxx", "6"],
        ],
    )
    _write_csv(
        directory / "us-states-code.csv",
        ["states", "states_code", "id", "year"],
        [
            ["Alabama", "AL", "1", "2010"],
            ["Alabama", "AL", "1", "2011"],
            ["Alaska", "AK", "2", "2010"],
            ["Alaska", "AK", "2", "2011"],
            ["Atlantis", "", "99", "2010"],
            ["Atlantis", "", "99", "2011"],
        ],
    )
    return directory


def _report(out_dir: Path) -> dict:
    return load_report(out_dir / REPORT_NAME)


def test_real_data_end_to_end(tmp_path):
    out_dir = tmp_path / "reports"
    code = run(REAL_DATA, out_dir, None, (None, None), chunk_size=100)
    assert code == 0
    report = _report(out_dir)
    assert report["status"] == "ok"
    assert report["denominator"] == 520
    labels = {table["label"] for table in report["tables"]}
    assert {"reshaped", "states_code", "wide"} <= labels
    page = (out_dir / PAGE_NAME).read_text(encoding="utf-8")
    assert "每表逐列统计" in page
    assert "Join 命中率" in page
    assert "真源：reshaped" in page
    assert "逐列裁决，仅作用于列" in page


def test_unmapped_is_bucketed_not_dropped(data_dir, tmp_path):
    out_dir = tmp_path / "reports"
    code = run(data_dir, out_dir, None, (None, None), chunk_size=2)
    assert code == 0
    report = _report(out_dir)
    unmapped_states = {row["state"] for row in report["unmapped"]}
    assert "Atlantis" in unmapped_states
    # Conservation buckets still add up for every table.
    for row in report["conservation"]:
        assert row["valid_keys"] == row["out_of_slice"] + row["mapped"] + row["unmapped"]


def test_dropped_and_nan_counts(data_dir, tmp_path):
    out_dir = tmp_path / "reports"
    run(data_dir, out_dir, None, (None, None), chunk_size=100)
    report = _report(out_dir)
    reshaped = next(table for table in report["tables"] if table["label"] == "reshaped")
    # Two physically invalid rows (empty state, non-numeric year).
    assert reshaped["dropped_rows"]["invalid"] == 2
    # Atlantis rows carry an empty states_code -> NaN rows for that column.
    assert reshaped["nan_rows"]["states_code"] == 2


def test_empty_slice_fails_with_exit_2(data_dir, tmp_path):
    out_dir = tmp_path / "reports"
    code = run(data_dir, out_dir, ["Nowhere"], (None, None), chunk_size=100)
    assert code == 2
    report = _report(out_dir)
    assert report["status"] == "fail"
    assert "区间为空" in report["error"]
    page = (out_dir / PAGE_NAME).read_text(encoding="utf-8")
    assert "显式失败" in page


def test_year_filter_recomputes_denominator(data_dir, tmp_path):
    out_dir = tmp_path / "reports"
    code = run(data_dir, out_dir, None, (2011, 2011), chunk_size=100)
    assert code == 0
    report = _report(out_dir)
    # 3 states x 1 year for long tables; wide tables slice the single year too.
    assert report["denominator"] == 3
    for row in report["conservation"]:
        assert row["mapped"] + row["unmapped"] == 3


def test_deterministic_report_except_timestamp(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    run(REAL_DATA, first, None, (None, None), chunk_size=77)
    time.sleep(1.1)
    run(REAL_DATA, second, None, (None, None), chunk_size=77)
    report_a = json.loads((first / REPORT_NAME).read_text(encoding="utf-8"))
    report_b = json.loads((second / REPORT_NAME).read_text(encoding="utf-8"))
    assert report_a.pop("generated_at") != report_b.pop("generated_at")
    assert report_a == report_b
    # The HTML dashboard carries no timestamp and is byte-identical.
    assert (first / PAGE_NAME).read_bytes() == (second / PAGE_NAME).read_bytes()


def test_large_file_is_streamed_in_chunks(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    big = directory / "big.csv"
    with open(big, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["states", "states_code", "id", "year", "population"])
        state = "X"
        year = 2010
        while big.stat().st_size <= LARGE_FILE_BYTES + 1024:
            writer.writerow([state, "XX", "1", year, "12345678"])
            state += "X"
            if len(state) > 200:
                state = "X"
                year += 1
    tables = ingest_directory(directory, chunk_size=1000)
    table = tables[0]
    assert table.bytes_size > LARGE_FILE_BYTES
    assert table.chunks > 1
    assert table.chunk_rows_max <= 1000


def test_concurrent_readers_never_see_half_write(tmp_path):
    target = tmp_path / "report.json"
    payload = {"status": "ok", "n": list(range(1000))}
    stop = threading.Event()
    errors: list[Exception] = []

    def reader() -> None:
        while not stop.is_set():
            if target.exists():
                try:
                    data = load_report(target)
                    assert "n" in data
                except json.JSONDecodeError as exc:
                    # The only forbidden outcome: a half-written document.
                    errors.append(exc)
                    return
                except PermissionError:
                    # Windows may briefly deny opening during the atomic swap.
                    time.sleep(0.001)
            time.sleep(0.0005)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(60):
            atomic_write_bytes(target, (json.dumps({**payload, "i": i}) + "\n").encode())
    finally:
        stop.set()
        thread.join()
    assert errors == []


def test_module_invocation_exit_code(tmp_path):
    out_dir = tmp_path / "reports"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "lineage.serve",
            "--data",
            str(REAL_DATA),
            "--out",
            str(out_dir),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / REPORT_NAME).exists()
    assert (out_dir / PAGE_NAME).exists()
