"""`load_parquets` in R and Python (D75), `viewparquets.py` and HOW_TO.md (D89).

Written at the root of `runs/<project>/`, beside `contents.md`. The client
keeps whichever language it uses and deletes the rest. Each names every
parquet by its table (`Patients`, `OtherDiagnoses_sp`), so they load side by
side without confusion. They use only what the VM has: R `arrow` and `dplyr`;
Python `pyarrow`, and tkinter for the viewer.

`viewparquets.py` and `HOW_TO.md` are copied from `stock/`, beside this
package, which the user edits and the bundle carries.
"""

from __future__ import annotations

import shutil
from pathlib import Path

STOCK_DIR = Path(__file__).resolve().parent.parent / "stock"

LOAD_PY = '''"""Open every parquet of {project} without reading it into memory.

Run it (in VSCodium: Run Python File in Interactive Window, or `%run`), and
each table becomes a variable named for it, a pyarrow Dataset. Nothing is read
until you ask, so a table far larger than memory can still be filtered first:

    import pandas as pd
    female = Patients.to_table(filter=ds.field("Sex") == "Female").to_pandas(types_mapper=pd.ArrowDtype)

`tables` holds them all by name. contents.md describes every table and column.
If you move this folder, change PARQUETS below.
"""

from pathlib import Path

import pyarrow.dataset as ds

PARQUETS = Path(r"{parquets}")


def load_parquets(folder=PARQUETS):
    """Every parquet under `folder`, by table name, opened but not read."""
    return {{path.stem: ds.dataset(path, format="parquet")
            for path in sorted(Path(folder).rglob("*.parquet"))}}


tables = load_parquets()
globals().update(tables)
print(f"Opened {{len(tables)}} table(s): {{', '.join(tables)}}")
'''

LOAD_R = '''# Open every parquet of {project} without reading it into memory.
#
# source() it (in RStudio: Source), and each table becomes a variable named
# for it, an Arrow Dataset. Nothing is read until you collect(), so a table far
# larger than memory can still be filtered first:
#
#   female <- Patients |> filter(Sex == "Female") |> collect()
#
# contents.md describes every table and column. If you move this folder,
# change PARQUETS below.

options(arrow.int64_downcast = FALSE)  # 64-bit keys stay integer64 in every table
library(arrow)
library(dplyr)

PARQUETS <- "{parquets}"

load_parquets <- function(folder = PARQUETS, envir = globalenv()) {{
  files <- sort(list.files(folder, pattern = "\\\\.parquet$", recursive = TRUE, full.names = TRUE))
  names <- sub("\\\\.parquet$", "", basename(files))
  for (i in seq_along(files)) assign(names[i], open_dataset(files[i]), envir = envir)
  invisible(names)
}}

loaded <- load_parquets()
message("Opened ", length(loaded), " table(s): ", paste(loaded, collapse = ", "))
'''

def stock_how_to(project: str, parquets: Path) -> str:
    """The stock HOW_TO.md for one pull: its editing note dropped, its name and
    folder filled in. Plain replacement, not format(): other braces stay."""
    text = (STOCK_DIR / "HOW_TO.md").read_text(encoding="utf-8")
    start, end = text.find("<!-- stock HOW_TO.md"), text.find("-->")
    if start != -1 and end > start:
        text = text[:start] + text[end + 3:].lstrip("\n")
    return text.replace("{project}", project).replace("{parquets}", parquets.as_posix())


def write_loaders(run_dir: Path, parquets: Path, project: str) -> list[Path]:
    """Write the load scripts, and copy the viewer and HOW_TO.md; returns what was written."""
    location = parquets.resolve()
    files = {
        "load_parquets.py": LOAD_PY.format(project=project, parquets=location),
        # R reads forward slashes on Windows too, and a backslash would escape.
        "load_parquets.R": LOAD_R.format(project=project, parquets=location.as_posix()),
        "HOW_TO.md": stock_how_to(project, location),
    }
    written = []
    for name, text in files.items():
        path = run_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    viewer = run_dir / "viewparquets.py"
    shutil.copyfile(STOCK_DIR / "viewparquets.py", viewer)
    written.append(viewer)
    return written
