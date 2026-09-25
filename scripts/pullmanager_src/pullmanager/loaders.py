"""`load_parquets` and `examine_parquets`, in R and Python, and HOW_TO.md (D75).

Written at the root of `runs/<project>/`, beside `contents.md`. The client
keeps whichever language it uses and deletes the rest. Each names every
parquet by its table (`Patients`, `OtherDiagnoses_sp`), so they load side by
side without confusion. They use only what the VM has: R `arrow`, `dplyr` and
`bit64`; Python `pyarrow` and `pandas`.
"""

from __future__ import annotations

from pathlib import Path

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

EXAMINE_PY = '''"""Read every parquet of {project} into memory, for browsing.

Run it, and each table becomes a variable named for it, a pandas DataFrame
with Arrow types, so a whole-number column with gaps stays whole numbers and
a 64-bit key keeps every digit. For large tables use load_parquets.py
instead: this reads everything at once. `tables` holds them all by name.
If you move this folder, change PARQUETS below.
"""

from pathlib import Path

import pandas as pd

PARQUETS = Path(r"{parquets}")


def examine_parquets(folder=PARQUETS):
    """Every parquet under `folder`, by table name, read into memory."""
    return {{path.stem: pd.read_parquet(path, dtype_backend="pyarrow")
            for path in sorted(Path(folder).rglob("*.parquet"))}}


tables = examine_parquets()
globals().update(tables)
for name, frame in tables.items():
    print(f"{{name}}: {{len(frame):,}} rows x {{frame.shape[1]}} columns")
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

EXAMINE_R = '''# Read every parquet of {project} into memory, for browsing.
#
# source() it, and each table becomes a variable named for it, a data frame
# (a tibble). 64-bit keys are integer64 (package bit64), so every digit is
# kept and joins match. For large tables use load_parquets.R instead: this
# reads everything at once. If you move this folder, change PARQUETS below.

options(arrow.int64_downcast = FALSE)
library(arrow)
library(bit64)

PARQUETS <- "{parquets}"

examine_parquets <- function(folder = PARQUETS, envir = globalenv()) {{
  files <- sort(list.files(folder, pattern = "\\\\.parquet$", recursive = TRUE, full.names = TRUE))
  names <- sub("\\\\.parquet$", "", basename(files))
  for (i in seq_along(files)) assign(names[i], read_parquet(files[i]), envir = envir)
  invisible(names)
}}

examined <- examine_parquets()
for (name in examined) message(name, ": ", nrow(get(name)), " rows x ", ncol(get(name)), " columns")
'''

HOW_TO = '''# How To Use These Files: {project}

Everything this pull produced is in this folder:

| File | What it is |
| --- | --- |
| `contents.md` | Every table and column: what it holds, one row per what, and its type in SQL, Python and R. Start here. |
| `parquets/SneakPeek/` | Tables pulled from COSMOS_SneakPeek. Their names end in `_sp`. |
| `parquets/Cosmos/` | Tables pulled from COSMOS. |
| `parquets/uploads/` | Lists that were uploaded to make the pull, as supplied. |
| `load_parquets.R`, `load_parquets.py` | Open every table without reading it into memory. |
| `examine_parquets.R`, `examine_parquets.py` | Read every table into memory. |

Keep the language you use and delete the other two scripts if you like.

## Load Or Examine?

**Load** (`load_parquets`) opens each table without reading it. Nothing
reaches memory until you ask for it, so you can filter, select columns or
count first, and read only the result. Use it for large tables, and whenever
you want only part of one.

- R: `Patients |> filter(Sex == "Female") |> select(PatientDurableKey) |> collect()`
- Python: `Patients.to_table(filter=ds.field("Sex") == "Female", columns=["PatientDurableKey"])`

**Examine** (`examine_parquets`) reads every table into memory as a data
frame, ready to browse, sort and view. Use it for small tables, or a test
pull. On a full pull it may need more memory than the machine has.

Both name each table after its file: `Patients`, `OtherDiagnoses_sp`.

## Running Them

- **RStudio:** open the `.R` file and press Source, or `source("load_parquets.R")`.
- **VSCodium:** open the `.py` file and run it in the interactive window, or
  `%run load_parquets.py`.

Each script names this folder in `PARQUETS` near its top. If the folder is
moved, change that line.

## Joining Tables

Tables join on the key columns `contents.md` lists, such as
`PatientDurableKey`. 64-bit keys are read as `int64` in Python and
`integer64` in R in every table, so they match exactly; do not convert them
to decimals (`double`), which can lose digits.
'''


def write_loaders(run_dir: Path, parquets: Path, project: str) -> list[Path]:
    """Write the four scripts and HOW_TO.md; returns what was written."""
    location = parquets.resolve()
    files = {
        "load_parquets.py": LOAD_PY.format(project=project, parquets=location),
        "examine_parquets.py": EXAMINE_PY.format(project=project, parquets=location),
        # R reads forward slashes on Windows too, and a backslash would escape.
        "load_parquets.R": LOAD_R.format(project=project, parquets=location.as_posix()),
        "examine_parquets.R": EXAMINE_R.format(project=project, parquets=location.as_posix()),
        "HOW_TO.md": HOW_TO.format(project=project),
    }
    written = []
    for name, text in files.items():
        path = run_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
