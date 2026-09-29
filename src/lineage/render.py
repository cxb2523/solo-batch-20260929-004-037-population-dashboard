"""HTML rendering for the column-level source-of-truth board."""

from __future__ import annotations

import html
import json

from .pipeline import CANON_COLUMNS, TABLE_PRIORITY


def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _pct(rate) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.2f}%"


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; font-family: 'Segoe UI', system-ui, sans-serif;
       background:#111; color:#fafafa; }
a { color:#6ec6ff; }
.topbar { background:#1d1d1d; padding:14px 24px; border-bottom:1px solid #333;
          display:flex; justify-content:space-between; align-items:center;
          flex-wrap:wrap; gap:8px; }
.topbar h1 { font-size:18px; margin:0; }
.wrap { padding:20px 24px 60px; }
.grid { display:grid; grid-template-columns:300px 1fr; gap:20px; }
@media (max-width: 900px){ .grid{ grid-template-columns:1fr; } }
.card { background:#1d1d1d; border:1px solid #333; border-radius:10px;
        padding:16px; margin-bottom:16px; }
.card h2 { font-size:15px; margin:0 0 10px; }
.filters label { display:block; font-size:12px; color:#bdbdbd; margin:8px 0 4px; }
.filters select, .filters input { width:100%; background:#111; color:#fafafa;
        border:1px solid #444; border-radius:6px; padding:6px; }
.states { height:220px; }
.btn { background:#f63366; border:0; color:white; padding:8px 18px;
       border-radius:6px; cursor:pointer; margin-top:12px; width:100%;
       font-weight:600; }
.muted { color:#9e9e9e; font-size:12px; }
.ok { color:#27AE60; font-weight:700; }
.fail { color:#E74C3C; font-weight:700; }
.warn { color:#F39C12; font-weight:700; }
table.stats { border-collapse:collapse; width:100%; font-size:12px; }
table.stats th, table.stats td { border:1px solid #333; padding:6px 8px;
       text-align:right; }
table.stats th:first-child, table.stats td:first-child { text-align:left; }
table.stats th { background:#222; }
details.col { border:1px solid #333; border-radius:8px; margin:10px 0;
       background:#191919; }
details.col > summary { cursor:pointer; padding:10px 14px; font-weight:600;
       list-style:none; display:flex; justify-content:space-between; gap:10px;
       flex-wrap:wrap; }
details.col > summary::-webkit-details-marker { display:none; }
.colbody { padding:0 14px 14px; }
.chips span { display:inline-block; background:#2a2a2a; border:1px solid #444;
       border-radius:20px; padding:2px 10px; font-size:11px; margin:2px 4px 2px 0; }
table.ex { border-collapse:collapse; width:100%; font-size:12px; margin-top:8px; }
table.ex th, table.ex td { border:1px solid #333; padding:4px 8px; text-align:left; }
table.ex th { background:#222; }
.banner { border-radius:10px; padding:16px 20px; margin-bottom:16px; }
.banner.fail { background:#3a1714; border:1px solid #E74C3C; }
.banner.ok { background:#14301c; border:1px solid #27AE60; }
code { background:#111; border:1px solid #333; padding:1px 5px; border-radius:4px;
       font-size:11px; }
.metrics { display:flex; gap:18px; flex-wrap:wrap; margin:8px 0; }
.metric { background:#111; border:1px solid #333; border-radius:8px;
          padding:8px 12px; min-width:120px; }
.metric b { display:block; font-size:18px; }
"""


def render_page(payload: dict, resource: dict | None = None) -> str:
    resource = resource or {}
    if payload.get("status", "OK") != "OK":
        body = _failure_body(payload)
    else:
        body = _board_body(payload)

    resource_line = (
        '<span class="muted" id="rss-line">'
        "peak resident RSS: shown live when served over HTTP</span>"
    )

    embedded_payload = dict(payload)
    embedded_payload.pop("generated_at", None)
    island = json.dumps(embedded_payload, sort_keys=True, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Column Lineage Board</title>
<style>{CSS}</style></head>
<body>
<script id="lineage-payload" type="application/json">{island}</script>
<script>
fetch('resource-usage.json', {{cache: 'no-store'}}).then(function (r) {{
  if (!r.ok) throw new Error('no resource data');
  return r.json();
}}).then(function (d) {{
  var el = document.getElementById('rss-line');
  el.textContent = 'peak resident RSS: ' + d.resident_peak_mib + ' MiB ('
    + d.resident_peak_bytes + ' bytes, ' + d.samples + ' samples)';
}}).catch(function () {{}});
</script>
<div class="topbar">
  <h1>Column-wise Source-of-Truth Board — US Population</h1>
  <div>{resource_line}</div>
</div>
<div class="wrap">{body}</div>
</body></html>"""


def _failure_body(payload: dict) -> str:
    details = payload.get("details") or {}
    checks = details.get("checks")
    check_html = ""
    if checks:
        rows = "".join(
            "<tr>"
            f"<td>{esc(c['table'])}</td>"
            f"<td>{esc(c['canonical_rows'])}</td>"
            f"<td>{esc(c['dropped_rows'])}</td>"
            f"<td>{esc(c['excluded_rows'])}</td>"
            f"<td>{esc(c['sliced_rows'])}</td>"
            f"<td>{esc(c['partition_sum'])}</td>"
            f"<td>{esc(c['join_denominator'])}</td>"
            "<td class='fail'>FAIL</td></tr>"
            for c in checks
        )
        check_html = (
            "<table class='ex'><tr><th>table</th><th>canonical</th>"
            "<th>dropped</th><th>excluded</th><th>sliced</th>"
            "<th>partition sum</th><th>join denominator</th><th></th></tr>"
            f"{rows}</table>"
        )
    filters = details.get("filters") or payload.get("filters") or {}
    return f"""
<div class="banner fail">
  <div class="fail">PIPELINE FAILED — status {esc(payload.get('status'))}</div>
  <div>{esc(payload.get('error'))}</div>
  <div class="muted">filters: {esc(json.dumps(filters, sort_keys=True))}</div>
  <div class="muted">This failure is written into report status and the
  process exits with code 2.</div>
</div>
{check_html}
"""


def _filter_card(payload: dict) -> str:
    filters = payload["filters"]
    years = payload.get("available_years") or []
    states = payload.get("available_states") or []
    selected = set(filters.get("states") or [])

    year_opts = "".join(
        f'<option value="{esc(v)}" '
        f'{"selected" if v == filters.get("year_from") else ""}>{esc(v)}</option>'
        for v in years
    )
    year_opts_to = "".join(
        f'<option value="{esc(v)}" '
        f'{"selected" if v == filters.get("year_to") else ""}>{esc(v)}</option>'
        for v in years
    )
    state_opts = "".join(
        f'<option value="{esc(s)}" {"selected" if s in selected else ""}>'
        f"{esc(s)}</option>"
        for s in states
    )
    return f"""
<form class="card filters" method="get" action="/">
  <h2>Slice filters</h2>
  <label for="yf">Year from</label>
  <select id="yf" name="year_from">{year_opts}</select>
  <label for="yt">Year to</label>
  <select id="yt" name="year_to">{year_opts_to}</select>
  <label for="st">States (none = all; unmapped rows are always retained)</label>
  <select class="states" id="st" name="states" multiple>{state_opts}</select>
  <button class="btn" type="submit">Recompute slice</button>
  <p class="muted">Changing any filter recomputes every per-column statistic.
  Rows missing from the states-code mapping table land in the
  <code>unmapped</code> group and are never dropped.</p>
</form>"""


def _board_body(payload: dict) -> str:
    parts = [_filter_card(payload), "<div>"]
    checks = payload["conservation_checks"]
    all_ok = all(c["ok"] for c in checks)
    banner_class = "ok" if all_ok else "fail"
    parts.append(
        f'<div class="banner {banner_class}">'
        f'<span class="{banner_class}">'
        f"{'CONSERVATION + JOIN DENOMINATOR CHECKS PASSED' if all_ok else 'CHECKS FAILED'}"
        "</span> "
        '<span class="muted">hit-rate denominator == sliced rows for every table; '
        f"run {esc(payload.get('run_id', 'n/a'))}</span></div>"
    )
    parts.append(_unmapped_card(payload.get("unmapped") or {}))
    for kind in sorted(payload["tables"], key=lambda k: TABLE_PRIORITY[k]):
        parts.append(_table_card(payload, kind))
    parts.append(_adjudication_card(payload))
    parts.append("</div>")
    return f'<div class="grid">{"".join(parts)}</div>'


def _unmapped_card(unmapped: dict) -> str:
    total = unmapped.get("total_unmapped_rows", 0)
    cls = "ok" if total == 0 else "warn"
    rows = []
    for table in unmapped.get("by_table", []):
        members = ", ".join(
            f"{esc(m['state'] or '<blank>')} ({m['rows']})"
            for m in table.get("members", [])
        ) or "—"
        rows.append(
            f"<tr><td>{esc(table['table'])}</td>"
            f"<td>{table['unmapped_rows']}</td><td>{members}</td></tr>"
        )
    return f"""
<div class="card">
  <h2>Unmapped group <span class="{cls}">{total} row(s) retained</span></h2>
  <p class="muted">Rows whose state does not resolve against the states-code
  mapping table are grouped here, not silently dropped.</p>
  <table class="stats"><tr><th>table</th><th>unmapped rows</th><th>members</th></tr>
  {''.join(rows)}</table>
</div>"""


def _table_card(payload: dict, kind: str) -> str:
    table = payload["tables"][kind]
    cols = table["columns"]

    stat_rows = []
    for label, mode in (
        ("read-in rows (sliced)", "read"),
        ("dropped rows attributed", "dropped"),
        ("NaN rows", "nan"),
        ("join hits / denominator", "hits"),
        ("join hit rate", "rate"),
    ):
        cells = []
        for column in CANON_COLUMNS:
            entry = cols[column]
            if not entry["present"]:
                value = '<span class="muted">absent</span>'
            elif mode == "read":
                value = esc(entry["read_rows"])
            elif mode == "dropped":
                value = esc(entry["dropped_rows"])
            elif mode == "nan":
                value = esc(entry["nan_rows"])
            elif mode == "hits":
                value = (
                    f"{esc(entry['join_hits'])} / {esc(entry['read_rows'])}"
                    if entry["join_hits"] is not None
                    else "—"
                )
            else:
                value = _pct(entry["join_rate"])
            cells.append(f"<td>{value}</td>")
        stat_rows.append(f"<tr><td>{label}</td>{''.join(cells)}</tr>")

    head = "".join(f"<th>{esc(c)}</th>" for c in CANON_COLUMNS)
    details = "".join(_column_detail(payload, kind, column) for column in CANON_COLUMNS)
    chunk_note = (
        '<span class="warn">streamed in chunks (&gt;10 MiB, never loaded whole)</span>'
        if table["chunked"]
        else '<span class="muted">streamed via chunk iterator</span>'
    )
    return f"""
<div class="card">
  <h2>{esc(table['label'])} <code>{esc(table['name'])}</code>
    <span class="muted">(priority rank {TABLE_PRIORITY[kind]})</span></h2>
  <div class="metrics">
    <div class="metric"><span class="muted">file rows</span><b>{table['file_rows']}</b></div>
    <div class="metric"><span class="muted">dropped</span><b>{table['dropped_rows']}</b></div>
    <div class="metric"><span class="muted">filter-excluded</span><b>{table['excluded_rows']}</b></div>
    <div class="metric"><span class="muted">sliced (denominator)</span><b>{table['sliced_rows']}</b></div>
    <div class="metric"><span class="muted">key join rate</span><b>{_pct(table['key_join_rate'])}</b></div>
    <div class="metric"><span class="muted">NaN population rows</span><b>{cols['population']['nan_rows']}</b></div>
    <div class="metric"><span class="muted">unmapped</span><b>{table['unmapped_rows']}</b></div>
  </div>
  <p class="muted">read-in columns ({len(table['read_columns'])}):
  {esc(', '.join(table['read_columns']))} · {table['size_bytes']} bytes · {chunk_note}</p>
  <table class="stats"><tr><th>per column</th>{head}</tr>{''.join(stat_rows)}</table>
  <p class="muted" style="margin-top:10px">Click a column to expand two-table
  divergence, priority and the per-column ruling.</p>
  {details}
</div>"""


def _column_detail(payload: dict, kind: str, column: str) -> str:
    entry = payload["tables"][kind]["columns"][column]
    if not entry["present"]:
        return (
            f'<details class="col" inert><summary>{esc(column)} '
            '<span class="muted">not present in this table</span></summary></details>'
        )
    summary = payload["columns"].get(column, {})
    examples = [
        ex for ex in summary.get("examples", []) if ex.get("disagree")
    ]
    providers = {
        name: present for name, present in summary.get("present_by_table", {}).items()
    }
    chips = "".join(
        f"<span>{esc(name)}: {count} present</span>"
        for name, count in providers.items()
    )
    priority = "".join(
        f"<li><b>{esc(item['table'])}</b> (rank {item['rank']}): "
        f"{esc(item['reason'])}</li>"
        for item in summary.get("priority", [])
    )

    ex_rows = ""
    for ex in examples[:50]:
        values = "".join(
            f"<td>{esc(value)}</td>"
            for value in ex["values"].values()
        )
        value_heads = "".join(
            f"<th>{esc(name)}</th>" for name in ex["values"]
        )
        ex_rows += (
            f"<tr><td>{esc(ex['states'])}</td><td>{esc(ex['id'])}</td>"
            f"<td>{esc(ex['year'])}</td>{values}"
            f"<td>{esc(ex['winner'])}</td><td>{esc(ex['reason'])}</td></tr>"
        )
    examples_html = ""
    if examples:
        examples_html = (
            "<table class='ex'><tr><th>state</th><th>id</th><th>year</th>"
            f"{value_heads}<th>column winner</th><th>reason</th></tr>{ex_rows}</table>"
        )
    else:
        examples_html = (
            '<p class="muted">No divergent value between the providing tables '
            "inside this slice; the column still records its own priority, the "
            "ruling is never applied table-wide.</p>"
        )

    return f"""
<details class="col">
  <summary><span>{esc(column)}</span>
    <span class="chips">
      <span>join {_pct(entry['join_rate'])}</span>
      <span>NaN {entry['nan_rows']}</span>
      <span>dropped {entry['dropped_rows']}</span>
      <span>{esc(entry['join_kind'])}</span>
    </span>
  </summary>
  <div class="colbody">
    <p class="muted">Adjudication for <code>{esc(column)}</code> lands on this
    column only.</p>
    <div class="chips">{chips}
      <span>disagreement rows: {summary.get('disagreement_rows', 0)}</span>
      <span>missing rows: {summary.get('missing_rows', 0)}</span>
    </div>
    <ol class="muted" style="margin-top:8px">{priority}</ol>
    {examples_html}
  </div>
</details>"""


def _adjudication_card(payload: dict) -> str:
    rows = []
    for column in CANON_COLUMNS:
        summary = payload["columns"][column]
        winners = summary.get("winner_rows", {})
        winner_text = "; ".join(f"{esc(k)}: {v}" for k, v in winners.items())
        top = next(
            (p for p in summary.get("priority", []) if p["rank"] == 1),
            None,
        )
        rows.append(
            f"<tr><td>{esc(column)}</td>"
            f"<td>{summary.get('disagreement_rows', 0)}</td>"
            f"<td>{summary.get('missing_rows', 0)}</td>"
            f"<td>{winner_text}</td>"
            f"<td>{esc(top['reason']) if top else '—'}</td></tr>"
        )
    return f"""
<div class="card">
  <h2>Per-column adjudication (no table chosen wholesale)</h2>
  <table class="ex">
    <tr><th>column</th><th>divergent rows</th><th>missing rows</th>
    <th>rows won per table</th><th>top-priority reason</th></tr>
    {''.join(rows)}
  </table>
</div>"""
