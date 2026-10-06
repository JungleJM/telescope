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
| `cosmos_parquets/` | Tables pulled from COSMOS. |
| `sneakpeek_parquets/` | Tables pulled from COSMOS_SneakPeek. Their names end in `_sp`. |
| `uploads_parquets/` | Lists that were uploaded to make the pull, as supplied. |
| `pullmanifest.yaml`, `execute-*.log` | The pull's record and its latest log; earlier logs are in `older_logs/`. |
| `pull_files/` | What ran the pull: the split and its SQL. Not needed to use the data. |
| `utils.py`, `utils/client/` | A small window of tools, among them the parquet viewer, to look at the tables. |
| `load_parquets.R`, `load_parquets.py` | Open every table in R or Python without reading it into memory. |

## Looking At The Data

Run `python utils.py` in this folder (or open it in VSCodium and run it), then
**viewparquets**. It opens on this pull: press **Cosmos** or
**Cosmos_SneakPeek**, then a table's button. A second table opens beside the
first; a third replaces the older of the two. **Browse...** opens any parquet.

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

Each script names this folder in `PARQUETS` near its top (`{parquets}`)
and opens what is in its `*_parquets` folders. If the folder is moved, change
that line.

## Joining Tables

Tables join on the key columns `contents.md` lists, such as
`PatientDurableKey`. 64-bit keys are read as `int64` in Python and
`integer64` in R in every table, so they match exactly; do not convert them
to decimals (`double`), which can lose digits.
