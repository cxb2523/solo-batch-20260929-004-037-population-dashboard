"""HTTP service and CLI for the column-level lineage board.

Usage::

    python -m lineage.serve --data data --out reports

A report generation is atomic end to end (see :mod:`lineage.reportio`) and
deterministic: rerunning the same inputs produces byte-identical reports apart
from the timestamp. Filter changes recompute the full pipeline; explicit
failures land in the report ``status`` and make the process exit with code 2
in ``--once`` mode.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from . import pipeline, reportio
from .memory import ResidentMonitor
from .render import render_page

REPORT_LOGICAL = "report.json"
SNAPSHOT_LOGICAL = "index.html"
RESOLVED_LOGICAL = "resolved.csv"
RESOURCE_LOGICAL = "resource-usage.json"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReportService:
    """Owns the (cached) loaded tables and a single-writer build pipeline."""

    def __init__(self, data_dir: Path, out_dir: Path):
        self.data_dir = Path(data_dir)
        self.out_dir = Path(out_dir)
        self._load_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._tables = None
        self._fingerprint = None
        self._last_build: dict | None = None

    def _fingerprint_now(self) -> tuple:
        entries = []
        for path in sorted(self.data_dir.glob("*.csv")):
            stat = path.stat()
            entries.append((path.name, stat.st_size, stat.st_mtime_ns))
        return tuple(entries)

    def get_tables(self, monitor: ResidentMonitor | None = None):
        sample = monitor.sample if monitor is not None else None
        fingerprint = self._fingerprint_now()
        with self._load_lock:
            if self._tables is None or fingerprint != self._fingerprint:
                self._tables = pipeline.load_tables(self.data_dir, sample)
                self._fingerprint = fingerprint
            return self._tables

    def build(self, filters: dict | None = None, persist: bool = True) -> dict:
        with self._build_lock:
            monitor = ResidentMonitor()
            try:
                tables = self.get_tables(monitor)
                payload = pipeline.build_payload(
                    self.data_dir,
                    filters=filters,
                    sample_resident=monitor.sample,
                    tables=tables,
                )
            except pipeline.LineageError as error:
                payload = pipeline.failure_payload(
                    self.data_dir, filters or {}, error
                )
                result = self._finalize(payload, monitor, persist, failed=True)
                return result

            result = self._finalize(payload, monitor, persist, failed=False)
            return result

    def _finalize(
        self,
        payload: dict,
        monitor: ResidentMonitor,
        persist: bool,
        failed: bool,
    ) -> dict:
        monitor.sample()
        resource = monitor.as_dict()
        run_id = pipeline.payload_run_id(payload)
        payload["run_id"] = run_id

        if failed:
            resolved_csv = None
        else:
            resolved = pd.DataFrame(payload["resolved_rows"])
            ordered = payload["resolved_columns"] + [
                f"{column}_source" for column in pipeline.CANON_COLUMNS
            ] + ["mapped"]
            ordered = [c for c in ordered if c in resolved.columns]
            buffer = io.StringIO()
            resolved[ordered].to_csv(buffer, index=False, lineterminator="\n")
            resolved_csv = buffer.getvalue().encode("utf-8")

        timed = {
            "generated_at": _now_iso(),
            "run_id": run_id,
            **payload,
        }
        report_bytes = (
            json.dumps(timed, sort_keys=True, indent=2, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        page = render_page(payload, resource=resource)
        resource_doc = {
            "generated_at": timed["generated_at"],
            "run_id": run_id,
            "status": payload["status"],
            "resident_peak_bytes": 0,
            "resident_peak_mib": 0.0,
            "samples": 0,
            "note": (
                "volatile peak RSS is served live from memory and shown on the "
                "page; it is intentionally not persisted so reruns stay "
                "byte-identical apart from generated_at"
            ),
        }
        resource_bytes = (
            json.dumps(resource_doc, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

        artifacts = {
            REPORT_LOGICAL: report_bytes,
            SNAPSHOT_LOGICAL: page.encode("utf-8"),
            RESOURCE_LOGICAL: resource_bytes,
        }
        if resolved_csv is not None:
            artifacts[RESOLVED_LOGICAL] = resolved_csv

        if persist:
            reportio.publish(
                self.out_dir,
                artifacts,
                run_id=run_id,
                generated_at=timed["generated_at"],
            )

        result = {
            "payload": payload,
            "resource": resource,
            "page": page,
            "artifacts": artifacts,
            "failed": failed,
            "status": payload["status"],
        }
        self._last_build = result
        return result

    def parse_filters(self, query: dict) -> dict:
        filters = {}
        if query.get("year_from"):
            filters["year_from"] = int(query["year_from"][0])
        if query.get("year_to"):
            filters["year_to"] = int(query["year_to"][0])
        states = query.get("states")
        if states:
            picked = states
            if len(states) == 1:
                picked = states[0].split(",")
            filters["states"] = sorted({s.strip() for s in picked if s.strip()})
        return filters

    def last_build(self) -> dict | None:
        return self._last_build


def make_handler(service: ReportService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LineageBoard/1.0"

        def log_message(self, fmt, *args):  # quiet default access log
            return

        def _send(self, body: bytes, status: int, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if parsed.path in ("/", "/index.html"):
                    filters = service.parse_filters(query)
                    result = service.build(filters, persist=True)
                    self._send(
                        result["page"].encode("utf-8"),
                        200 if not result["failed"] else 422,
                        "text/html; charset=utf-8",
                    )
                    return
                if parsed.path == "/report.json":
                    data = reportio.active_artifact_bytes(
                        service.out_dir, REPORT_LOGICAL
                    )
                    if data is None:
                        self._send(b'{"status":"NO_REPORT"}\n', 404, "application/json")
                    else:
                        self._send(data, 200, "application/json")
                    return
                if parsed.path == "/resolved.csv":
                    data = reportio.active_artifact_bytes(
                        service.out_dir, RESOLVED_LOGICAL
                    )
                    if data is None:
                        self._send(b"status: NO_REPORT\n", 404, "text/csv")
                    else:
                        self._send(data, 200, "text/csv; charset=utf-8")
                    return
                if parsed.path == "/resource-usage.json":
                    last = service.last_build()
                    if last is None:
                        self._send(b"{}\n", 404, "application/json")
                    else:
                        doc = {
                            "status": last["status"],
                            "run_id": last["payload"].get("run_id"),
                            **last["resource"],
                        }
                        self._send(
                            (
                                json.dumps(doc, sort_keys=True, indent=2) + "\n"
                            ).encode("utf-8"),
                            200,
                            "application/json",
                        )
                    return
                if parsed.path == "/healthz":
                    self._send(b"ok\n", 200, "text/plain")
                    return
                self._send(b"not found\n", 404, "text/plain")
            except pipeline.LineageError as error:
                payload = pipeline.failure_payload(
                    service.data_dir, service.parse_filters(query), error
                )
                self._send(
                    render_page(payload).encode("utf-8"),
                    400,
                    "text/html; charset=utf-8",
                )

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/refresh":
                self._send(b"not found\n", 404, "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            query = parse_qs(raw or parsed.query, keep_blank_values=True)
            try:
                filters = service.parse_filters(query)
                result = service.build(filters, persist=True)
                status = 200 if not result["failed"] else 422
                self._send(
                    json.dumps(
                        {"status": result["status"], "run_id": result["payload"].get("run_id")},
                        sort_keys=True,
                    ).encode("utf-8"),
                    status,
                    "application/json",
                )
            except pipeline.LineageError as error:
                payload = pipeline.failure_payload(service.data_dir, {}, error)
                self._send(
                    json.dumps(
                        {"status": payload["status"], "error": payload["error"]}
                    ).encode("utf-8"),
                    400,
                    "application/json",
                )

    return Handler


def run_server(data_dir: Path, out_dir: Path, port: int) -> int:
    service = ReportService(data_dir, out_dir)
    initial = service.build(None, persist=True)
    if initial["failed"]:
        print(f"lineage build failed: {initial['payload']['error']}", flush=True)
        return 2

    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(service))
    actual_port = httpd.server_address[1]
    print(
        f"column lineage board: http://127.0.0.1:{actual_port}/ "
        f"(reports under {out_dir})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def run_once(data_dir: Path, out_dir: Path, filters: dict | None) -> int:
    service = ReportService(data_dir, out_dir)
    result = service.build(filters, persist=True)
    payload = result["payload"]
    print(
        f"status={payload['status']} run_id={payload.get('run_id')} "
        f"peak_rss={result['resource']['resident_peak_mib']} MiB",
        flush=True,
    )
    return 0 if not result["failed"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lineage.serve",
        description="Column-level source-of-truth lineage board.",
    )
    parser.add_argument("--data", default="data", help="input CSV directory")
    parser.add_argument("--out", default="reports", help="report output directory")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument(
        "--once",
        action="store_true",
        help="build reports once and exit (exit code 2 on pipeline failure)",
    )
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        help="state name filter, repeatable (default: all mapped states)",
    )
    args = parser.parse_args(argv)

    filters = None
    if args.year_from or args.year_to or args.state:
        filters = {
            "year_from": args.year_from,
            "year_to": args.year_to,
            "states": args.state or [],
        }

    if args.once:
        return run_once(Path(args.data), Path(args.out), filters)
    return run_server(Path(args.data), Path(args.out), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
