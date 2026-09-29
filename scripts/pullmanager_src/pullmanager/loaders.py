"""`load_parquets` in R and Python (D75), `viewparquets.py` and HOW_TO.md (D89).

Written at the root of `runs/<project>/`, beside `contents.md`. The client
keeps whichever language it uses and deletes the rest. Each names every
parquet by its table (`Patients`, `OtherDiagnoses_sp`), so they load side by
side without confusion. They use only what the VM has: R `arrow` and `dplyr`;
Python `pyarrow`, and tkinter for the viewer.

What else goes in every run folder is listed in `stock/stock.yaml` (D124),
beside this package, which the user edits and the bundle carries: HOW_TO.md,
and `viewparquets.py` from `utils/`, to begin with.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .yaml_io import load_yaml

STOCK_DIR = Path(__file__).resolve().parent.parent / "stock"
STOCK_LIST = "stock.yaml"
# The one stock file with the pull filled in; the rest are copied as they are.
HOW_TO = "HOW_TO.md"

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
    """Every parquet in `folder`'s *_parquets folders, by table name, opened
    but not read. pull_files/ is left alone: it holds the uploads as sent."""
    return {{path.stem: ds.dataset(path, format="parquet")
            for path in sorted(p for d in sorted(Path(folder).glob("*_parquets"))
                               for p in d.rglob("*.parquet"))}}


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
  # Only the *_parquets folders: pull_files/ holds the uploads as sent.
  folders <- list.dirs(folder, recursive = FALSE)
  folders <- folders[grepl("_parquets$", folders)]
  files <- sort(list.files(folders, pattern = "\\\\.parquet$", recursive = TRUE, full.names = TRUE))
  names <- sub("\\\\.parquet$", "", basename(files))
  for (i in seq_along(files)) assign(names[i], open_dataset(files[i]), envir = envir)
  invisible(names)
}}

loaded <- load_parquets()
message("Opened ", length(loaded), " table(s): ", paste(loaded, collapse = ", "))
'''

def stock_entries(stock_dir: Path | None = None) -> tuple[list[tuple[Path, Path]], list[str]]:
    """What stock.yaml lists: (the file, where it goes in the run folder), and
    what is wrong with any entry, so the rest are still copied."""
    folder = stock_dir or STOCK_DIR
    try:
        doc = load_yaml(folder / STOCK_LIST) or {}
    except FileNotFoundError:
        return [], [f"No {STOCK_LIST} in {folder}: no stock files copied. Put back the list of files."]
    entries, problems = [], []
    for entry in (doc.get("files") if isinstance(doc, dict) else None) or []:
        if isinstance(entry, dict):
            name, into = str(entry.get("file") or ""), str(entry.get("into") or "")
        else:
            name, into = str(entry), ""
        source = (folder / name).resolve() if name else None
        if source is None or not source.is_file():
            problems.append(f"{STOCK_LIST} lists {name or entry!r}, which is not in {folder}: not copied.")
            continue
        entries.append((source, Path(into) / source.name if into else Path(source.name)))
    return entries, problems


def stock_how_to(project: str, parquets: Path, source: Path | None = None) -> str:
    """The stock HOW_TO.md for one pull: its editing note dropped, its name and
    folder filled in. Plain replacement, not format(): other braces stay."""
    text = (source or STOCK_DIR / HOW_TO).read_text(encoding="utf-8")
    start, end = text.find("<!-- stock HOW_TO.md"), text.find("-->")
    if start != -1 and end > start:
        text = text[:start] + text[end + 3:].lstrip("\n")
    return text.replace("{project}", project).replace("{parquets}", parquets.as_posix())


def write_loaders(run_dir: Path, parquets: Path, project: str) -> list[Path]:
    """Write the load scripts, and copy what stock.yaml lists (D124); returns
    what was written. An entry that cannot be copied is said, not fatal."""
    location = parquets.resolve()
    files = {
        "load_parquets.py": LOAD_PY.format(project=project, parquets=location),
        # R reads forward slashes on Windows too, and a backslash would escape.
        "load_parquets.R": LOAD_R.format(project=project, parquets=location.as_posix()),
    }
    written = []
    for name, text in files.items():
        path = run_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    entries, problems = stock_entries()
    for source, destination in entries:
        path = run_dir / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        if source.name == HOW_TO:
            path.write_text(stock_how_to(project, location, source), encoding="utf-8")
        else:
            shutil.copyfile(source, path)
        written.append(path)
    for problem in problems:
        print(f"  stock: {problem}", file=sys.stderr)
    return written
