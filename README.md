# 🏂 US Population Dashboard

A dashboard web app template built in Python using Streamlit.

## Column-level lineage board

The home page of the lineage service is a **column-wise source-of-truth
board** over the three population CSVs in `data/` (the raw wide census table,
the states-code wide table and the reshaped long table). After slicing by
state and year, every table reports, *per column*:

- read-in columns and rows that survived the slice,
- dropped rows attributed to the key columns,
- join hit rate (hits / sliced rows) and NaN rows,
- the `unmapped` group for state codes/names absent from the mapping table
  (rows are retained, never silently dropped).

Clicking a column expands the two-table divergence with explicit priority
and reasons; the ruling is applied to that column only — the service never
picks one table wholesale. Filters recompute the whole pipeline per request.

The hit-rate denominator and the post-slice row-conservation assertion share
one definition. On mismatch (or an empty year interval) the page fails
explicitly, the report `status` records the failure and the one-shot command
exits with code `2`. Inputs stream in 50k-row chunks; tables over 10 MiB are
never loaded whole, and the sampled peak resident memory is visible on the
page. Publication is atomic (temp files + manifest flip), and reruns produce
byte-identical artifacts apart from the timestamp.

```bash
python -m pytest -q
python -m lineage.serve --data data --out reports        # serves the board
python -m lineage.serve --data data --out reports --once # writes reports once
```

Outputs under `reports/`: `report.json` (status, per-column stats, rulings),
`resolved.csv` (column-local curated values + winning source per column),
`index.html` (snapshot of the board) and `resource-usage.json`.

## Demo App

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://population-dashboard.streamlit.app/)

## Colab notebook
[![Colab Notebook](https://colab.research.google.com/assets/colab-badge.svg)](https://github.com/dataprofessor/population-dashboard/blob/master/US_Population.ipynb)

## Prerequisite libraries
Here are the Python libraries used in the creation of this dashboard app

## Data source
US Population data spanning the duration of 2010-2019 was obtained from the [U.S. Census Bureau](https://www.census.gov/data/datasets/time-series/demo/popest/2010s-state-total.html).

## Reference
A talk entitled [_Crafting a Dashboard App in Python using Streamlit_](https://budapestbi.hu/2023/hu/program/speakers/chanin-nantasenamat/) showing how to build this app is given at the [Budapest BI Forum (Data Visualization track)](https://budapestbi.hu/2023/hu/en/program-data-visualization-track/) on November 22, 2023.
