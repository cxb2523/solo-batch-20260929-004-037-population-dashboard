# 🏂 US Population Dashboard

A dashboard web app template built in Python using Streamlit.

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

## 逐列真源看板（Column Lineage Dashboard）

`src/lineage/serve.py` 把首页看板换成按州/年份切片的逐列真源视图。

```bash
python -m pytest -q
python -m lineage.serve --data data --out reports
# 可选筛选（改筛选即重算）：
python -m lineage.serve --data data --out reports --state Alabama --year-from 2012 --year-to 2015
# 生成后只读预览：
python -m lineage.serve --data data --out reports --serve 8000
```

产物（均经临时文件 + os.replace 原子落盘，刷新期不会读到半写文件）：

- `reports/report.json`：每表读入列、丢行数（非法/区间外）、NaN 行数、两表 join 命中率与分歧样例、逐列裁决（优先级 + 理由，只落该列）、unmapped 分组、行数守恒与峰值驻留。
- `reports/index.html`：州/年份筛选在页面内即时重算全部统计；点单列展开两表分歧与列级裁决。

口径与失败语义：

- join 命中率分母 = 切片后行数，与行数守恒断言共用同一分母。
- 分母不一致或筛选区间为空时：页面显式失败、`report.json` 的 status 写 fail、进程退出码 2。
- 州码对不上映射表的行进入 unmapped 分组，绝不静默丢弃。
- CSV 一律按块迭代（`pandas.read_csv(chunksize=...)`）；单表超过 10 MiB 不整体载入，峰值驻留写入报告并在页面可见。
- 同一输入重跑两次，报告除 generated_at 时间戳外逐字节一致（峰值驻留按稳定桶取整），index.html 不含时间戳、完全一致。
