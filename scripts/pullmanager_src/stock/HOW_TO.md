# How To Use These Files: {project}

<!-- stock HOW_TO.md: Artifacts copies this file into every pull's folder,
     without this note, filling in the pull's name where "project" is written
     in braces, and the parquets folder where "parquets" is. Edit it on the Mac
     in scripts/pullmanager_src/stock/, then rebuild the bundle
     (python3 makebundle.py) to take it to the VM. -->

Everything this pull produced is in this folder:

| File | What it is |
| --- | --- |
| `contents.md` | Every table and column: what it holds, one row per what, and its type in SQL, Python and R. Start here. |
| `parquets/SneakPeek/` | Tables pulled from COSMOS_SneakPeek. Their names end in `_sp`. |
| `parquets/Cosmos/` | Tables pulled from COSMOS. |
| `parquets/uploads/` | Lists that were uploaded to make the pull, as supplied. |
| `viewparquets.py` | A window for looking at the tables: open one or several, page through, sort. |
| `load_parquets.R`, `load_parquets.py` | Open every table in R or Python without reading it into memory. |

## Looking At The Data

Run `python viewparquets.py` in this folder (or open it in VSCodium and run
it). Press Open, choose one or more parquets, and each opens in its own tab.
It needs nothing but Python.

## Working With The Data

`load_parquets` opens each table without reading it. Nothing reaches memory
until you ask for it, so you can filter, select columns or count first, and
read only the result. Keep the language you use and delete the other script
if you like.

- R: `Patients |> filter(Sex == "Female") |> select(PatientDurableKey) |> collect()`
- Python: `Patients.to_table(filter=ds.field("Sex") == "Female", columns=["PatientDurableKey"])`

Each names a table after its file: `Patients`, `OtherDiagnoses_sp`.

- **RStudio:** open `load_parquets.R` and press Source, or `source("load_parquets.R")`.
- **VSCodium:** open `load_parquets.py` and run it in the interactive window,
  or `%run load_parquets.py`.

Each script names the parquets folder in `PARQUETS` near its top
(`{parquets}`). If the folder is moved, change that line.

## Joining Tables

Tables join on the key columns `contents.md` lists, such as
`PatientDurableKey`. 64-bit keys are read as `int64` in Python and
`integer64` in R in every table, so they match exactly; do not convert them
to decimals (`double`), which can lose digits.
