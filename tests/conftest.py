import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest

from lineage.pipeline import KIND_CODED, KIND_LONG, KIND_WIDE


@pytest.fixture
def real_data_dir():
    return ROOT / "data"


@pytest.fixture
def disagreement_dir(tmp_path):
    """Three tiny tables exercising unmapped rows and per-column disagreement.

    - ``Newstate`` exists only in the wide/long inputs, never in the mapping
      table, so it must land in ``unmapped`` rather than vanish.
    - long-table Wyoming 2010 population deliberately disagrees with both raw
      tables; ``states_code`` disagrees for Wyoming 2010 too.
    - long is the only provider for a Nevada 2010 population, so that column
      ruling falls back independently of the population rows elsewhere.
    """
    coded = tmp_path / "coded.csv"
    coded.write_text(
        "states,states_code,id,2010,2011\n"
        "Alabama,AL,1,100,110\n"
        "Wyoming,WY,56,200,210\n"
        "Nevada,NV,32,300,310\n",
        encoding="utf-8",
    )
    wide = tmp_path / "wide.csv"
    wide.write_text(
        "states,id,2010,2011\n"
        "Alabama,1,100,110\n"
        "Wyoming,56,200,210\n"
        "Nevada,32,300,310\n"
        "Newstate,99,400,410\n",
        encoding="utf-8",
    )
    long = tmp_path / "long.csv"
    long.write_text(
        "states,states_code,id,year,population\n"
        "Alabama,AL,1,2010,100\n"
        "Alabama,AL,1,2011,110\n"
        "Wyoming,XX,56,2010,999\n"
        "Wyoming,WY,56,2011,210\n"
        "Nevada,NV,32,2010,\n"
        "Nevada,NV,32,2011,310\n"
        "Newstate,NS,99,2010,400\n"
        "Newstate,NS,99,2011,410\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def large_csv_dir(tmp_path):
    """A coded mapping CSV and a long-form CSV larger than 10 MiB."""
    coded = tmp_path / "coded.csv"
    coded.write_text(
        "states,states_code,id,2010,2011\n"
        "Alabama,AL,1,100,110\n",
        encoding="utf-8",
    )
    long = tmp_path / "long.csv"
    rows = 500_000
    with open(long, "w", encoding="utf-8", newline="") as fh:
        fh.write("states,states_code,id,year,population\n")
        for index in range(rows):
            year = 2010 + (index % 2)
            fh.write(f"Alabama,AL,1,{year},{100 + index}\n")
    assert long.stat().st_size > 10 * 1024 * 1024
    return tmp_path
