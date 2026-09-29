"""``python -m lineage.serve --data data --out reports`` entry point.

Streams every input table in chunks, slices by state/year, asserts a single
shared denominator for join hit-rate and row conservation, writes
``report.json`` and ``index.html`` through atomic replacement, and exits with
code 2 on any consistency failure (including an empty slice).
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import ingest as ingest_mod
from .analysis import LineageError, analyze
from .ingest import FIELDS, _bucket_mb, build_code_mapping, ingest_directory, peak_rss_mb
from .page import render_failure, render_page
from .report import STATUS_FAIL, STATUS_OK, atomic_write_bytes, atomic_write_text, utc_now_iso

REPORT_NAME = "report.json"
PAGE_NAME = "index.html"


def _payload(
    result: dict[str, Any],
    mapping: dict[str, str],
    peak_rss: float,
    filters: dict[str, Any],
    table_stats: list[dict[str, Any]],
) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for slice_ in result["slices"]:
        table = slice_.table
        tables.append(
            {
                "label": table.label,
                "kind": table.kind,
                "cols": table.raw_columns,
                "fields": {field: table.has_field(field) for field in FIELDS},
                "physical": table.physical_rows,
                "invalid": table.invalid_rows,
                "dup": table.duplicate_records,
            }
        )
        for (state, year), record in table.records.items():
            records.append(
                {
                    "t": table.label,
                    "state": state,
                    "year": year,
                    "v": {field: record.get(field) for field in FIELDS},
                }
            )
    records.sort(key=lambda item: (item["t"], item["state"], item["year"]))
    return {
        "filters": filters,
        "mapping": sorted(mapping),
        "tables": tables,
        "records": records,
        "peak_rss_mb": peak_rss,
        "denominator": result["denominator"],
        "table_stats": table_stats,
        "pairs": result["pairs"],
        "verdicts": result["verdicts"],
        "unmapped_count": len(result["unmapped"]),
    }


def _success_report(
    result: dict[str, Any],
    peak_rss: float,
    filters: dict[str, Any],
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "status": STATUS_OK,
        "error": None,
        "filters": filters,
        "denominator": result["denominator"],
        "peak_rss_mb": peak_rss,
        "inputs": inputs,
        "tables": result["table_stats"],
        "conservation": result["conservation"],
        "pairs": result["pairs"],
        "verdicts": result["verdicts"],
        "unmapped": result["unmapped"],
    }


def _failure_report(error: str, peak_rss: float, filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "status": STATUS_FAIL,
        "error": error,
        "filters": filters,
        "denominator": None,
        "peak_rss_mb": peak_rss,
    }


def _input_summary(tables) -> list[dict[str, Any]]:
    return [
        {
            "label": table.label,
            "path": table.path,
            "kind": table.kind,
            "bytes": table.bytes_size,
            "chunks": table.chunks,
            "chunk_rows_max": table.chunk_rows_max,
            "raw_columns": table.raw_columns,
            "streamed": True,
        }
        for table in tables
    ]


def run(
    data_dir: Path,
    out_dir: Path,
    states: list[str] | None,
    years: tuple[int | None, int | None],
    chunk_size: int,
) -> int:
    filters = {"states": sorted(states) if states else None, "year_from": years[0], "year_to": years[1]}
    peak_rss = peak_rss_mb()
    tables = []
    try:
        tables = ingest_directory(data_dir, chunk_size=chunk_size)
        peak_rss = max(peak_rss, max((table.peak_rss_mb for table in tables), default=peak_rss))
        mapping = build_code_mapping(tables)
        result = analyze(tables, mapping, states, years)
        peak_rss = max(peak_rss, peak_rss_mb())
        stable_peak = float(_bucket_mb(peak_rss))
        report = _success_report(result, stable_peak, filters, _input_summary(tables))
        payload = _payload(result, mapping, stable_peak, filters, report["tables"])
        page = render_page(payload)
        status_code = 0
    except LineageError as exc:
        peak_rss = max(peak_rss, peak_rss_mb())
        message = str(exc)
        report = _failure_report(message, float(_bucket_mb(peak_rss)), filters)
        page = render_failure(STATUS_FAIL, message)
        status_code = 2
    except FileNotFoundError as exc:
        peak_rss = max(peak_rss, peak_rss_mb())
        message = f"输入路径不存在：{exc.filename or exc}"
        report = _failure_report(message, float(_bucket_mb(peak_rss)), filters)
        page = render_failure(STATUS_FAIL, message)
        status_code = 2

    out_dir.mkdir(parents=True, exist_ok=True)
    # Both artifacts are published atomically: concurrent readers never see a
    # half-written report during a refresh.
    atomic_write_bytes(
        out_dir / REPORT_NAME,
        (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )
    atomic_write_text(out_dir / PAGE_NAME, page)

    if status_code == 0:
        print(
            f"[lineage] status=ok denominator={report['denominator']} "
            f"tables={len(tables)} peak_rss_mb={report['peak_rss_mb']} "
            f"report={out_dir / REPORT_NAME}"
        )
    else:
        print(f"[lineage] status=fail: {report['error']}", file=sys.stderr)
    return status_code


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="逐列真源看板（列级数据血缘）")
    parser.add_argument("--data", default="data", help="输入 CSV 目录")
    parser.add_argument("--out", default="reports", help="报告与页面输出目录")
    parser.add_argument("--state", action="append", default=None, help="按州筛选，可重复")
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=ingest_mod.DEFAULT_CHUNK_SIZE,
        help="CSV 分块行数（>10MiB 的表强制按块迭代）",
    )
    parser.add_argument("--serve", type=int, default=None, metavar="PORT", help="生成后启动只读 HTTP 服务")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    code = run(
        Path(args.data),
        Path(args.out),
        args.state,
        (args.year_from, args.year_to),
        args.chunk_size,
    )
    if args.serve is not None:
        if code != 0:
            print("[lineage] 存在失败结论，仍已落盘失败页面；不启动服务", file=sys.stderr)
            return code
        out_dir = Path(args.out).resolve()
        handler = partial(SimpleHTTPRequestHandler, directory=str(out_dir))
        with ThreadingHTTPServer(("127.0.0.1", args.serve), handler) as httpd:
            print(f"[lineage] serving {out_dir} at http://127.0.0.1:{args.serve}")
            httpd.serve_forever()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
