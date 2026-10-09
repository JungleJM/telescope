"""Load every table in data/cosmos_parquets/ as a pandas DataFrame.

    from python.load_parquets import load
    tables = load()
    tables["EDVisits"].head()
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "cosmos_parquets"


def load(folder: Path = DATA) -> dict[str, pd.DataFrame]:
    """Each parquet, by its table name."""
    tables = {path.stem: pd.read_parquet(path) for path in sorted(folder.glob("*.parquet"))}
    if not tables:
        raise FileNotFoundError(f"No parquets in {folder}")
    return tables


if __name__ == "__main__":
    for name, frame in load().items():
        print(f"{name:<28}{len(frame):>10,} rows  {len(frame.columns):>3} columns")
