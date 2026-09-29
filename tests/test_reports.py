import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from lineage import pipeline, reportio
from lineage.serve import ReportService, make_handler


def _strip_timestamp(data: bytes) -> bytes:
    return re.sub(
        rb'("generated_at": ")[^"]*(")', rb"\g<1>TS\g<2>", data
    )


def test_reruns_byte_identical_except_timestamp(real_data_dir, tmp_path):
    out1 = tmp_path / "r1"
    out2 = tmp_path / "r2"
    service1 = ReportService(real_data_dir, out1)
    service2 = ReportService(real_data_dir, out2)
    service1.build(None)
    time.sleep(0.01)
    service2.build(None)

    for logical in ["report.json", "index.html", "resolved.csv", "resource-usage.json"]:
        a = reportio.active_artifact_bytes(out1, logical)
        b = reportio.active_artifact_bytes(out2, logical)
        assert a is not None and b is not None
        assert _strip_timestamp(a) == _strip_timestamp(b), logical


def test_failure_is_written_to_report_status(real_data_dir, tmp_path):
    out = tmp_path / "failed"
    service = ReportService(real_data_dir, out)
    result = service.build({"year_from": 2019, "year_to": 2010})
    assert result["failed"] is True
    assert result["status"] == "EMPTY_SLICE"
    report = json.loads(
        reportio.active_artifact_bytes(out, "report.json").decode("utf-8")
    )
    assert report["status"] == "EMPTY_SLICE"
    assert "generated_at" in report


def test_atomic_publish_never_exposes_half_files(real_data_dir, tmp_path):
    out = tmp_path / "atomic"
    service = ReportService(real_data_dir, out)
    service.build(None)

    errors = []
    stop = threading.Event()

    def refresher():
        year = 2010
        while not stop.is_set():
            try:
                service.build({"year_from": year, "year_to": 2019})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            year = 2010 if year == 2011 else year + 1

    def reader():
        while not stop.is_set():
            for logical in ("report.json", "index.html", "resolved.csv"):
                try:
                        data = reportio.active_artifact_bytes(out, logical)
                        if data is None:
                            continue
                        if logical.endswith(".json"):
                            json.loads(data)
                        elif logical.endswith(".html"):
                            assert b"<!doctype html>" in data
                        else:
                            assert b"states,id,year" in data
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

    workers = [threading.Thread(target=refresher) for _ in range(2)]
    workers += [threading.Thread(target=reader) for _ in range(4)]
    for worker in workers:
        worker.start()
    time.sleep(1.5)
    stop.set()
    for worker in workers:
        worker.join()
    assert errors == []


@pytest.fixture
def http_server(real_data_dir, tmp_path):
    out = tmp_path / "http"
    service = ReportService(real_data_dir, out)
    service.build(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", service
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_http_page_has_nonempty_stats_and_expanders(http_server):
    base, _ = http_server
    with urllib.request.urlopen(base + "/") as response:
        page = response.read().decode("utf-8")
    assert response.status == 200
    payload_match = re.search(
        r'<script id="lineage-payload" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    payload = json.loads(payload_match.group(1))
    assert payload["tables"]["coded_wide"]["sliced_rows"] == 520
    assert page.count('details class="col"') >= 15
    assert "Recompute slice" in page
    assert "unmapped" in page


def test_http_filter_recomputes_and_failure_is_explicit(http_server):
    base, _ = http_server
    url = base + "/?year_from=2012&year_to=2012&states=Alaska"
    with urllib.request.urlopen(url) as response:
        page = response.read().decode("utf-8")
    payload = json.loads(
        re.search(
            r'<script id="lineage-payload" type="application/json">(.*?)</script>',
            page,
            re.S,
        ).group(1)
    )
    assert payload["tables"]["coded_wide"]["sliced_rows"] == 1

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(base + "/?year_from=2019&year_to=2010")
    fail = exc_info.value
    assert fail.code == 422
    body = fail.read().decode("utf-8")
    assert "PIPELINE FAILED" in body
    assert "EMPTY_SLICE" in body


def test_resource_endpoint_shows_peak_resident(http_server):
    base, _ = http_server
    with urllib.request.urlopen(base + "/resource-usage.json") as response:
        resource = json.loads(response.read().decode("utf-8"))
    assert resource["resident_peak_bytes"] > 0
    assert resource["samples"] >= 1


def test_once_exit_code_two_for_empty_slice(real_data_dir, tmp_path):
    from lineage.serve import run_once

    code = run_once(
        real_data_dir,
        tmp_path / "once-fail",
        {"year_from": 2019, "year_to": 2010},
    )
    assert code == 2
