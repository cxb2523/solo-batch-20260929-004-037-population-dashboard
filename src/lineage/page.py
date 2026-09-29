"""Self-contained HTML dashboard: filters recompute every metric in the page."""
from __future__ import annotations

import html
import json
from typing import Any


def _embed(payload):
    # json.dumps already escapes backslashes; neutralize the closing tag
    # so the inline <script> block cannot terminate early.
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return text.replace('</', '<\\/').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


CSS = """
body{font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#0f1115;color:#e8eaed}
header{padding:16px 24px;border-bottom:1px solid #2a2d33;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0}
main{padding:20px 24px;max-width:1240px;margin:0 auto}
.filters{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;align-items:end}
label{font-size:12px;color:#9aa0a6;display:flex;flex-direction:column;gap:4px}
select,input,button{background:#171a1f;color:#e8eaed;border:1px solid #33373d;border-radius:6px;padding:6px 8px;font-size:13px}
button{cursor:pointer}
.banner{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-weight:600}
.ok{background:#10301c;color:#7ee2a8;border:1px solid #1f5132}
.fail{background:#3a1414;color:#f2a3a3;border:1px solid #6e2222}
.meta{font-size:12px;color:#9aa0a6}
table{border-collapse:collapse;width:100%;font-size:13px;background:#14171c;border:1px solid #262a30;border-radius:8px}
th,td{padding:8px 10px;border-bottom:1px solid #24282e;text-align:left;white-space:nowrap}
th{background:#1b1f25;color:#c7ccd3;font-weight:600}
tr.clickable{cursor:pointer}
tr.clickable:hover{background:#1c2128}
.card{background:#14171c;border:1px solid #262a30;border-radius:10px;padding:14px 16px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 10px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;margin-left:8px}
.p1{background:#14331f;color:#7ee2a8}
.p2{background:#12313a;color:#7ed7e2}
.p3{background:#3a2c12;color:#e2c87e}
.num{font-variant-numeric:tabular-nums}
.warn{color:#f2b06a}
.bad{color:#f28b8b;font-weight:700}
.good{color:#7ee2a8}
details{border:1px solid #262a30;border-radius:8px;margin:8px 0;background:#161a20}
summary{cursor:pointer;padding:10px 14px;font-weight:600}
.detail-body{padding:0 14px 14px}
.kv{font-size:12px;color:#c7ccd3;margin:6px 0}
.reason{background:#101318;border-left:3px solid #4a86e8;padding:8px 10px;border-radius:4px;font-size:12px;margin:8px 0}
code{background:#0c0e12;padding:1px 5px;border-radius:4px}
.empty{color:#9aa0a6;font-style:italic;padding:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.metric{background:#101318;border:1px solid #24282e;border-radius:8px;padding:10px 12px}
.metric .v{font-size:20px;font-weight:700}
.metric .k{font-size:11px;color:#9aa0a6}
.sample td{white-space:normal}
"""


def render_failure(status: str, error: str) -> str:
    error_html = html.escape(status + "\n\n" + error).replace("\n", "<br>")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>逐列真源看板 · 失败</title>
<style>{CSS.strip()}</style></head>
<body><header><h1>❌ 逐列真源看板：显式失败</h1></header>
<main><div class="banner fail">{error_html}</div>
<p class="meta">报告 status=fail，进程退出码 2。</p></main></body></html>
"""


def render_page(payload: dict[str, Any]) -> str:
    dataset = _embed(payload)
    static_summary = _static_summary(payload)
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>逐列真源看板</title>
<style>{CSS.strip()}</style>
</head>
<body>
<header>
  <h1>🏂 逐列真源看板</h1>
  <div class="meta" id="meta"></div>
</header>
<main>
  <section class="filters">
    <label>州（不选=全部）<select id="fStates" multiple size="4"></select></label>
    <label>起始年份 <input id="fFrom" type="number"></label>
    <label>结束年份 <input id="fTo" type="number"></label>
    <button id="fReset" type="button">重置</button>
  </section>
  <div id="banner"></div>
  <div id="board"></div>
  <div id="ssr">{static_summary}</div>
  <noscript><div class="banner fail">看板统计由 JavaScript 在筛选变化时重算，请启用 JavaScript。</div></noscript>
</main>
<script id="dataset" type="application/json">{dataset}</script>
<script>
{JS}
</script>
</body>
</html>
"""


def _static_summary(payload: dict[str, Any]) -> str:
    parts = [
        '<div class="card" data-ssr="initial">'
        f'<h2>初始切片统计（改筛选即重算，分母 = {payload["denominator"]}）</h2>',
        "<table><thead><tr><th>表</th><th>读入列</th><th>丢行数</th>"
        "<th>切片行数</th><th>NaN id / code / pop</th></tr></thead><tbody>",
    ]
    for stat in payload["table_stats"]:
        read = " ".join(
            f"<code>{html.escape(field)}</code>"
            for field, column in stat["read_columns"].items()
            if column is not None
        )
        dropped = stat["dropped_rows"]
        nan = stat["nan_rows"]
        parts.append(
            "<tr>"
            f"<td><b>{html.escape(stat['label'])}</b></td>"
            f"<td>{read}</td>"
            f"<td class='num'>{dropped['total']}（非法 {dropped['invalid']} / 区间外 {dropped['out_of_slice']}）</td>"
            f"<td class='num'>{stat['rows_slice']}</td>"
            f"<td class='num'>{nan['id']} / {nan['states_code']} / {nan['population']}</td>"
            "</tr>"
        )
    parts.append("</tbody></table></div>")

    parts.append('<div class="card"><h2>列级裁决（点击页面列条展开分歧与理由）</h2>')
    for verdict in payload["verdicts"]:
        parts.append(
            "<details><summary>"
            f"{html.escape(verdict['field_label'])} <code>{html.escape(verdict['field'])}</code>"
            f"<span class='pill p{verdict['winner_priority']}'>真源：{html.escape(verdict['winner'])} "
            f"· 优先级 {verdict['winner_priority']}</span>"
            "</summary><div class='detail-body'>"
            f"<div class='reason'>{html.escape(verdict['reason'])}</div>"
            + _pair_summary_for_field(payload, verdict["field"])
            + "</div></details>"
        )
    parts.append("</div>")

    parts.append(
        '<div class="card"><h2>Unmapped 分组</h2>'
        f"<p class='meta'>初始切片共 {payload['unmapped_count']} 个 unmapped 键，未静默丢弃。</p></div>"
    )
    return "".join(parts)


def _pair_summary_for_field(payload: dict[str, Any], field: str) -> str:
    rows = []
    for pair in payload["pairs"]:
        metrics = pair["columns"].get(field)
        if metrics is None:
            continue
        rows.append(
            "<div class='kv'>"
            f"<b>{html.escape(pair['left'])} ↔ {html.escape(pair['right'])}</b> · "
            f"命中率 {metrics['hit_rate'] * 100:.2f}%（{metrics['hit']}/{pair['denominator']}）· "
            f"分歧 {metrics['disagreements']} · NaN 左 {metrics['nan_left']} / 右 {metrics['nan_right']}"
            "</div>"
        )
    return "".join(rows)


JS = r"""
"use strict";
const DATA = JSON.parse(document.getElementById("dataset").textContent);
const FIELDS = ["id", "states_code", "population"];
const FIELD_LABELS = {id: "州 ID", states_code: "州码", population: "人口"};
const PRIORITY = {reshaped: 1, states_code: 2, wide: 3};

const elStates = document.getElementById("fStates");
const elFrom = document.getElementById("fFrom");
const elTo = document.getElementById("fTo");

const allStates = Array.from(new Set(DATA.records.map(r => r.state))).sort();
const allYears = Array.from(new Set(DATA.records.map(r => r.year))).sort((a, b) => a - b);
elStates.innerHTML = allStates.map(s => `<option>${escapeHtml(s)}</option>`).join("");
elFrom.min = Math.min(...allYears);
elTo.max = Math.max(...allYears);

document.getElementById("fReset").addEventListener("click", () => {
  [...elStates.options].forEach(o => (o.selected = false));
  elFrom.value = "";
  elTo.value = "";
  render();
});
[elFrom, elTo].forEach(e => e.addEventListener("input", render));
elStates.addEventListener("change", render);

document.getElementById("meta").textContent =
  `峰值驻留 ${DATA.peak_rss_mb.toFixed(1)} MB · 命中率分母=切片后行数 · 仅列级裁决`;

function selectedStates() {
  const sel = [...elStates.selectedOptions].map(o => o.value);
  return new Set(sel.length ? sel : allStates);
}

function compute() {
  const states = selectedStates();
  const from = elFrom.value === "" ? -Infinity : Number(elFrom.value);
  const to = elTo.value === "" ? Infinity : Number(elTo.value);
  const tables = {};
  DATA.tables.forEach(t => {
    tables[t.label] = {
      label: t.label,
      kind: t.kind,
      fields: t.fields,
      physical: t.physical,
      invalid: t.invalid,
      dup: t.dup,
      out: 0,
      rows: new Map(),
      nan: Object.fromEntries(FIELDS.map(f => [f, 0]))
    };
  });
  for (const r of DATA.records) {
    const t = tables[r.t];
    if (!states.has(r.state) || r.year < from || r.year > to) {
      t.out++;
      continue;
    }
    t.rows.set(r.state + "\u0000" + r.year, r.v);
    for (const f of FIELDS) {
      if (t.fields[f] && (r.v[f] === null || r.v[f] === undefined)) t.nan[f]++;
    }
  }
  const denoms = new Set(Object.values(tables).map(t => t.rows.size));
  const denom = denoms.size === 1 ? denoms.values().next().value : null;
  return {tables, denom};
}

function pairsCompute(view) {
  const labels = Object.keys(view.tables).sort(
    (a, b) => (PRIORITY[a] || 9) - (PRIORITY[b] || 9) || a.localeCompare(b)
  );
  const pairs = [];
  for (let i = 0; i < labels.length; i++) {
    for (let j = i + 1; j < labels.length; j++) {
      const L = view.tables[labels[i]];
      const R = view.tables[labels[j]];
      const cols = {};
      const keys = new Set([...L.rows.keys(), ...R.rows.keys()]);
      for (const f of FIELDS) {
        if (!L.fields[f] || !R.fields[f]) continue;
        let hit = 0, dis = 0, nanL = 0, nanR = 0, onlyL = 0, onlyR = 0;
        const samples = [];
        for (const k of [...keys].sort()) {
          const inL = L.rows.has(k);
          const inR = R.rows.has(k);
          const lv = inL ? L.rows.get(k)[f] : undefined;
          const rv = inR ? R.rows.get(k)[f] : undefined;
          if (inL && (lv === null || lv === undefined)) nanL++;
          if (inR && (rv === null || rv === undefined)) nanR++;
          if (inL && !inR) onlyL++;
          if (inR && !inL) onlyR++;
          if (!(inL && inR)) continue;
          if (lv === rv) hit++;
          else {
            dis++;
            if (samples.length < 50) {
              const [state, year] = k.split("\u0000");
              samples.push({state, year, left: lv, right: rv});
            }
          }
        }
        cols[f] = {
          hit, miss: view.denom - hit, dis, nanL, nanR, onlyL, onlyR,
          rate: view.denom ? hit / view.denom : null, samples
        };
      }
      pairs.push({left: L.label, right: R.label, cols});
    }
  }
  return {labels, pairs};
}

function verdictsCompute(view, labels, pairs) {
  const disByField = {};
  pairs.forEach(p =>
    Object.entries(p.cols).forEach(([f, m]) => {
      disByField[f] = (disByField[f] || 0) + m.dis;
    })
  );
  return FIELDS.map(f => {
    const parts = labels.filter(l => view.tables[l].fields[f]);
    if (!parts.length) return null;
    const winner = parts[0];
    const dis = disByField[f] || 0;
    const reasons = [
      `逐列裁决，仅作用于列 ${FIELD_LABELS[f]}（${f}），不整表二选一`,
      `固定优先级：${winner} 排名 ${PRIORITY[winner] || 9}` +
        (parts[1] ? `，高于 ${parts[1]}` : "，高于其余表"),
      dis
        ? `该列在 ${dis} 个切片键上存在分歧，优先取高优先级表的值`
        : "该列各表值一致，裁决用于声明唯一真源，未发生覆盖",
      view.tables[winner].nan[f]
        ? `胜出表该列切片内仍有 ${view.tables[winner].nan[f]} 个 NaN，需回溯源数据`
        : null
    ].filter(Boolean);
    return {
      f, winner, rank: PRIORITY[winner] || 9,
      reason: reasons.join("；"), dis, parts
    };
  }).filter(Boolean);
}

function unmappedCompute(view) {
  const out = [];
  for (const t of Object.values(view.tables)) {
    if (!t.fields.states_code) continue;
    for (const [k, v] of t.rows) {
      const [state, year] = k.split("\u0000");
      if (!DATA.mapping.includes(state)) {
        out.push({state, year: Number(year), t: t.label, code: v.states_code});
      }
    }
  }
  out.sort((a, b) =>
    a.state.localeCompare(b.state) || a.year - b.year || a.t.localeCompare(b.t)
  );
  return out;
}

function pct(x) {
  return x === null ? "—" : (x * 100).toFixed(2) + "%";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])
  );
}
function fmt(v) {
  return v === null || v === undefined
    ? '<span class="warn">NaN</span>'
    : escapeHtml(v);
}

function render() {
  const view = compute();
  const banner = document.getElementById("banner");
  const board = document.getElementById("board");
  const ssr = document.getElementById("ssr");
  if (ssr) ssr.style.display = "none";
  if (view.denom === null) {
    banner.innerHTML =
      '<div class="banner fail">❌ 命中率分母与切片行数口径不一致：各表切片行数不同，页面显式失败（报告 status=fail，退出码 2）</div>';
    board.innerHTML = "";
    return;
  }
  if (view.denom === 0) {
    banner.innerHTML =
      '<div class="banner fail">❌ 筛选区间为空：切片后行数为 0，页面显式失败（报告 status=fail，退出码 2）</div>';
    board.innerHTML = "";
    return;
  }
  banner.innerHTML =
    `<div class="banner ok">✓ 切片后行数守恒，命中率分母 = ${view.denom}（所有表共用）</div>`;
  const {labels, pairs} = pairsCompute(view);
  const verdicts = verdictsCompute(view, labels, pairs);
  const unmapped = unmappedCompute(view);

  let html =
    '<div class="card"><h2>每表逐列统计</h2><table><thead><tr>' +
    "<th>表</th><th>读入列 id</th><th>读入列 states_code</th><th>读入列 population</th>" +
    "<th>丢行数（非法 / 区间外）</th><th>切片行数</th>" +
    "<th>NaN: id</th><th>NaN: states_code</th><th>NaN: population</th></tr></thead><tbody>";
  for (const t of Object.values(view.tables)) {
    html +=
      `<tr><td><b>${escapeHtml(t.label)}</b> <span class="meta">${escapeHtml(t.kind)}</span></td>` +
      `<td>${t.fields.id ? '<span class="good">读入</span>' : '<span class="meta">—</span>'}</td>` +
      `<td>${t.fields.states_code ? '<span class="good">读入</span>' : '<span class="meta">—</span>'}</td>` +
      `<td>${t.fields.population ? '<span class="good">读入</span>' : '<span class="meta">—</span>'}</td>` +
      `<td class="num">${t.invalid + t.out} <span class="meta">(${t.invalid}/${t.out})</span></td>` +
      `<td class="num">${t.rows.size}</td>` +
      `<td class="num">${t.fields.id ? t.nan.id : "—"}</td>` +
      `<td class="num">${t.fields.states_code ? t.nan.states_code : "—"}</td>` +
      `<td class="num">${t.fields.population ? t.nan.population : "—"}</td></tr>`;
  }
  html += "</tbody></table></div>";

  html += '<div class="card"><h2>Join 命中率（点击单列展开分歧与裁决）</h2>';
  for (const f of FIELDS) {
    const verdict = verdicts.find(v => v.f === f);
    if (!verdict) continue;
    html += `<details><summary>${FIELD_LABELS[f]} <code>${f}</code>` +
      `<span class="pill p${verdict.rank}">真源：${escapeHtml(verdict.winner)} · 优先级 ${verdict.rank}</span></summary>` +
      '<div class="detail-body">' +
      `<div class="reason">${escapeHtml(verdict.reason)}</div>`;
    for (const p of pairs) {
      const m = p.cols[f];
      if (!m) {
        html += `<div class="kv"><b>${escapeHtml(p.left)} ↔ ${escapeHtml(p.right)}</b>：该列未被两表共同读入</div>`;
        continue;
      }
      html +=
        `<div class="kv"><b>${escapeHtml(p.left)} ↔ ${escapeHtml(p.right)}</b> · ` +
        `命中率 <span class="num ${m.rate === 1 ? "good" : "bad"}">${pct(m.rate)}</span> ` +
        `（<span class="num">${m.hit}/${view.denom}</span>）· 分歧 ${m.dis} · ` +
        `NaN 左 ${m.nanL} / 右 ${m.nanR} · 仅左 ${m.onlyL} / 仅右 ${m.onlyR}</div>`;
      if (m.samples.length) {
        html += '<table class="sample"><thead><tr><th>州</th><th>年</th>' +
          `<th>${escapeHtml(p.left)}</th><th>${escapeHtml(p.right)}</th></tr></thead><tbody>`;
        m.samples.forEach(s => {
          html += `<tr><td>${escapeHtml(s.state)}</td><td>${s.year}</td>` +
            `<td>${fmt(s.left)}</td><td>${fmt(s.right)}</td></tr>`;
        });
        html += "</tbody></table>";
      }
    }
    html += "</div></details>";
  }
  html += "</div>";

  html += '<div class="card"><h2>Unmapped 分组（州码对不上映射表，不静默丢弃）</h2>';
  if (!unmapped.length) {
    html += '<div class="empty">当前切片没有 unmapped 行。</div>';
  } else {
    html += `<p class="meta">共 ${unmapped.length} 个 (州, 年, 表) 行被归入 unmapped。</p>` +
      "<table><thead><tr><th>州</th><th>年</th><th>来源表</th><th>携带州码</th></tr></thead><tbody>";
    unmapped.forEach(u => {
      html += `<tr><td>${escapeHtml(u.state)}</td><td>${u.year}</td>` +
        `<td>${escapeHtml(u.t)}</td><td>${fmt(u.code)}</td></tr>`;
    });
    html += "</tbody></table>";
  }
  html += "</div>";

  html += '<div class="card"><h2>行数守恒</h2><div class="grid">';
  for (const t of Object.values(view.tables)) {
    html +=
      `<div class="metric"><div class="k">${escapeHtml(t.label)}</div>` +
      `<div class="v num">${t.rows.size + t.out}</div>` +
      `<div class="k">有效键 = 切片 ${t.rows.size} + 区间外 ${t.out}；物理 ${t.physical} = 有效 ${t.rows.size + t.out} + 非法 ${t.invalid}（重复键 ${t.dup}）</div></div>`;
  }
  html += "</div></div>";

  board.innerHTML = html;
}

render();
"""
