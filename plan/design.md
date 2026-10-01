# Datascope Design

Datascope is the product; Telescope is its edition for Epic Cosmos (D123).

What the system is and the contracts each part keeps. Three documents, one job each, so a fact lives in exactly one place:

| Document | Answers |
|------------------------------------|------------------------------------|
| `design.md` | What it is, and how it behaves. Describes the code as it stands. |
| `decisions.md` | Why, what was rejected, and what it cost. Append-only, numbered. |
| `roadmap.md` | What is unbuilt, unverified, or undecided. The only place status lives. |

When code and this document disagree, one of them is a bug. Fix whichever is wrong in the same commit. Working conventions for this repo are in `.claude/CLAUDE.md`. What is still being discussed, before it becomes a decision, is in `plan/tasklist.qmd` (D128).

------------------------------------------------------------------------

## The Pipeline

``` text
Mac   template + reference/recipes.yaml + reference/datadictionary.yaml
        └─► makeYaml --export-transfer  (validate)
              └─► <project>_transfer.yaml        recipes written out, nothing applied
VM    <project>_transfer.yaml + datadictionary   no recipes file
        └─► makeYaml --export-split     (validate again, apply cosmos_db,
              │                          multipliers, batching)
              └─► runs/<project>/pullmanifest.yaml, pull_files/split/sessions/...
                    └─► Pullmanager --execute ──► <project_db>.dbo.<dest>
                          │                       status back into the manifest
                          └─► Pullmanager --artifacts ──► runs/<project>/*_parquets,
                                                          contents.md, load scripts
```

**YAML Manager** (`scripts/makeYaml.py`, with the app's Author half, `scripts/yamlmanager_tk.py` over `scripts/yamlmanager_model.py`) owns authoring: templates, recipes, validation, and planning the pull into a split folder plus `pullmanifest.yaml`. It never connects to a database. Recipes live only on the Mac (D49): what crosses to the VM is a transfer YAML with every recipe written out, which the VM validates and splits.

**Pullmanager** (`scripts/pullmanager_src/pullmanager/`) owns execution: it renders its own SQL, holds connections, materializes batches, moves data from Cosmos to Projects, and writes progress back into the manifest.

The boundary between them is files. YAML Manager writes a plan; Pullmanager writes status into the same document. Neither imports the other.

**Artifacts** (`--artifacts`, D72 to D75) turn a finished pull into files in its run folder: a parquet per finished table, `contents.md` describing them, and scripts that load them in R and Python. See Artifacts, below.

------------------------------------------------------------------------

## Where It Runs

Two machines, one codebase, updated one way.

- **Mac** (and the Linux dev box): development, and authoring, in the app's Author half (`python3 scope.py`, D112, D123).
- **VM**: air-gapped Windows, the only place Cosmos and Projects are reachable. Runs pulls. Cannot pull from git. Cannot load a page served by Python on localhost, or a static HTML file opened from disk, and has no in-editor browser. Has tkinter, but no other Python desktop toolkit (the `shiny` and `tcltk` entries in its package list are R). Some programs are blocked: `ipconfig` in Windows PowerShell 5.1 fails with "Access is denied". Python runs, from VSCodium's terminal (PowerShell 7.6.5) and from the launcher. Cannot launch webUI pages, in general.

### Environments

|   | VM | Mac | Linux dev box |
|------------------|------------------|------------------|------------------|
| OS | Windows | macOS | Bluefin (immutable Fedora Silverblue) |
| Python | 3.13.9 | `python3.13` (python.org 3.13.9, `/usr/local/bin`); brew's 3.14 `python3` has no tkinter | brew 3.14 (`/var/home/linuxbrew/.linuxbrew/bin/python3`); system `/usr/bin/python3` has no tkinter |
| tkinter | yes, likely Tk 8.6 | Tk 8.6, bundled with the python.org install | via `brew install python-tk@3.14`, Tk 9 |
| numpy | 2.1.3 | 2.1.3, installed for all users | not installed |
| pyarrow | 22.0.0 | 22.0.0 in `python3.13`; brew's `python3` has 25 | whatever is installed |
| pandas | 2.2.3 | 2.2.3 in `python3.13`; brew's `python3` has 3.0 | not installed |
| R | `arrow` 11.0.0.3, `dplyr`, `bit64`, `tibble` | R 4.6 with `arrow` 25.0.1, `dplyr`, `bit64`, `tibble` | not installed |
| Database | `pyodbc` 5.3.0, ODBC Driver 17 for SQL Server | none reachable, no driver | none reachable, no driver |
| YAML | `ruamel.yaml` 0.17.17, `pyyaml` 6.0.3 | the same in `python3.13`; brew's `python3` has both | whatever is installed; Ruby fallback |

On the Mac, use `python3.13` for anything run on the VM (the launcher, the runtime tests) so it meets the VM's Python, Tk and packages, not brew's. It has the VM's versions, installed for all users:

``` bash
sudo /usr/local/bin/python3.13 -m pip install ruamel.yaml==0.17.17 pyyaml==6.0.3 pyarrow==22.0.0 pandas==2.2.3
```

The artifact tests run the generated load scripts: the Python one under the running interpreter, the R one through `Rscript` when R has `arrow` (they skip otherwise). The Mac's R `arrow` is newer than the VM's, so the R check is close, not exact.

Without a YAML package the runtime cannot read YAML at all (`No YAML backend available`); `makeYaml` alone falls back to Ruby, and names the Python that sent it there if Ruby then fails. Without `pyarrow`, nothing can read or write a parquet upload, and the tests that need it skip. All six suites pass under `python3.13` with nothing skipped (`python3.13 scope.py test`).

`reference/requirements-vm.txt` pins those versions, for any Python 3.10 to 3.13 or venv: `python -m pip install -r reference/requirements-vm.txt`. Every Python on the Mac has the packages, so whichever one an editor picks can run the tools: the VM's versions in `python3.13`, uv's 3.11 and 3.13 and brew's 3.11; the newest that fit in brew's 3.14, Apple's 3.9 and the project venvs.

**`reference/DSVM Plugins.yaml` is the VM's installed software and package list.** Check it before depending on anything outside the standard library; if it is not listed, the VM does not have it and cannot get it.

Cosmos permissions are narrow: `VIEW DATABASE PERFORMANCE STATE` is denied and `sys.partitions` returns nothing, so row counts come from counting, never from metadata (D33).

Dev box gotchas: an IDE Python console (Positron's `%run`) keeps a started `yamlmanager` server alive and holding port 8765 after the script "finishes"; run the server from a terminal instead. To reach it from another machine over Tailscale: `--public --browser-host <hostname> --no-open`. Real-Tk GUI testing without a display uses Xvfb (`brew install xorg-server`).

### The Repository And `datascope.json`

``` text
scope.py            the front door: the app, `test`, `images` (D132), `dictionary-fix` (D155), or a Pullmanager command (D112, D123)
utils.py            the utilities window: a button per script in utils/, client/ and manager/ under their headings (D124, D148)
datascope.json      where the core files are, and where runs go (D111)
makebundle.py       builds dist/bundle.py, or dist/yamls_to_transfer.py (D122)
reference/          the core files: datadictionary.yaml, recipes.yaml, template.yaml,
                    DSVM Plugins.yaml, requirements-vm.txt; DDict image refs/, the
                    dictionary pages' screenshots, one folder per table (D117)
YAMLs/              the pulls: temp/ (intakes, their csv/, their exported blueprints), manager_test_cases/
plan/               design, decisions, roadmap, tasklist.qmd; commemorating/ (the history);
                    a pasted image in images/ beside its document (D132)
scripts/            makeYaml, the app's model and view, the bundler, dictionary_fix.py (D155), pullmanager_src/
cleanup/            disposable: the Python cache, runs/ (D113)
dist/               bundles and content_id.txt; not committed (D106)
.claude/CLAUDE.md   how to work in this repository
```

`datascope.json` says, relative to itself, where each core file is (`recipes`, `datadictionary`, `template`, `vm_plugins`), where runs go (`runs`: `cleanup/runs` on the Mac), and where pulls are backed up (`backup`, which may be absolute, on another drive; written by Run's Backup folder row, D149). Moving a core file means editing its line. makeYaml (`core_path`, `runs_root`) and Pullmanager (`pullmanager/config.py`) each read it from the working folder; neither imports the other, and a test holds their defaults together. Without it, as on the VM, the core files are beside the code (`reference/...` in the extracted tree) and runs are in the working folder's `runs/`. A name it cannot mean, or a file it cannot read, is `ConfigError`, saying what it may set. The Projects databases a pull may be given are not listed here but in the code (D164, D171): `DEFAULT_PROJECTS_DATABASES` in `pullmanager/config.py`, the six the login opens (D170), PROJECTD93A5E7 first; one the login gains is added there and carried by the next bundle, and PROJECTD52274F, a training database, is never listed. A `datascope.json` that still names `projects_databases` is refused as any unknown key is.

### The Bundle

Everything reaches the VM as one self-extracting file (D63), `dist/bundle.py`, which carries the queued pulls' blueprints with the software (D122, D162), or `dist/yamls_to_transfer.py`, the blueprints alone; built by `scripts/bundle_pullmanager.py` from `scripts/bundle_extractor.py` (the prelude) plus every file it carries.

It carries the whole unit, not just the runtime:

``` text
pullmanager_runtime/                # the extracted tree (any name; this is --extract's default)
  pullmanager.py                    Pullmanager entry point
  utilities.py                      the utilities window (D124)
  pullmanager/                      runtime package and its tests
  stock/                            stock.yaml and HOW_TO.md: what goes in each run folder (D89, D124)
  utils/                            utilities (D102, D124, D148): client/ (viewparquets.py, transcription_viewer.py), manager/ (clear_projects_db.py), yours
  scripts/makeYaml.py               validator and splitter
  scripts/yamlmanager_model.py      the app's model (D92)
  scripts/yamlmanager_tk.py         the app's Author half (D93)
  reference/datadictionary.yaml     where makeYaml's default finds it (D111, D117)
  .bundle-manifest.json
```

Every file under `scripts/pullmanager_src/` ships, not only its Python: `stock/`, and `utils/`, where your own utilities go (D102). The app's model and Author view ship beside `makeYaml.py`, which they use. Recipes, the browser UI and a template to start from are not shipped (D49): the VM works from blueprints, and the app's Author half adjusts them there (D94, D162). `dist/` is not committed (D106): a bundle is a build product, built from the working tree, so a file under `scripts/pullmanager_src/` travels as it is on disk, committed or not. Blueprints travel too, the queued pulls' and any named with `yaml=`, under `root/` in the bundle: verified like every file and counted in the `content_id`, but written into the working folder rather than the extracted tree (D79, D122). A blueprint (`<project>_blueprint.yaml`) goes to `YAMLs/temp/`, its project's one working copy on the VM (D162). Its relative `file_loc`s are shipped with `../../` before them, so they reach the same files beside `scope.py` as before (D103). The copy it replaces, if different, and any older copy of the same project (a `_transfer.yaml` or `_blueprint.yaml` beside `scope.py`, an `_intake.yaml` in `YAMLs/temp/`) are moved to `YAMLs/temp/replaced/`, never over a different copy there (a second one gets the time in its name); extraction names each. Any other carried YAML (an older `_transfer.yaml`) is written beside `scope.py`, a different copy there kept as `.local`. Published paths reproduce the repo's `scripts/` beside `YAMLs/` shape, so `makeYaml` finds its dictionary with no flags and no knowledge that it was bundled.

Guarantees:

- **Deterministic.** No timestamp. Identity is `content_id`, a hash over the sorted `(path, sha256, size)` list and the bundle's own code (the prelude above its manifest, `prelude_sha256`, D64), so rebuilding unchanged sources is byte-identical, and a change to how the bundle verifies or extracts is a new id.
- **Readable.** Payload lines are comment-prefixed source (`#` + line; a blank line is `#`), not base64, so a file can be read straight out of the bundle. A payload line that looks like a marker encodes to `# # === ...` and cannot match the anchored marker pattern.
- **Verified both ways.** Size and SHA-256 checked against the embedded manifest before writing, and re-hashed from disk after. The prelude is checked against `prelude_sha256` too.
- **Refuses** absolute paths, `..`, drive letters, backslashes, duplicate or unlisted sections, unterminated sections, mismatched END markers. CRLF sources are rejected at build time.
- **All-or-nothing.** Extraction stages to a sibling temp directory and swaps only after everything verifies. It replaces a previous extraction (one with `.bundle-manifest.json`) but refuses any other directory without `--force`. The previous extraction is removed whole, and extraction says so: "Removed the previous pullmanager_runtime (N files)".
- **Not while a pull executes** (D147). A software bundle refuses, before asking `y`, while any pull under the runs folder holds a live lock (D67), in either run-folder layout, naming it: a pull started from the old software would keep running it. Before asking, it says to close the app and the utilities, since a window opened before the update keeps the code it loaded. A bundle of YAMLs alone is not refused. The lock rule is Pullmanager's, copied, since the bundle's prelude cannot import it; a test holds the two together.

After extracting, `--extract` writes `scope.py` beside the extracted folder (D63, D123): a few lines that run that folder's `pullmanager.py` with the same arguments. It is rewritten by every extraction, whatever is there, so it always points at the folder just extracted; a missing folder makes it say to extract again. `utils.py` is written beside it the same way, opening the folder's `utilities.py` (D124). A `pullmanager.py` an earlier bundle wrote there is removed, known by its generated header; one of your own is left alone.

A bundle of YAMLs alone (`yamls_to_transfer.py`, every file under `root/`) extracts differently: it places its YAMLs as above, and leaves the extracted tree, `scope.py` and `utils.py` as they are (D122).

### Updating Never Destroys Work

The extracted tree is **wholly managed**: it is swapped, not merged, so a file of yours placed inside it is gone after the next update. Templates, uploads and split folders belong beside the tree.

Every bundled file is replaced on re-extraction. One that was edited on the VM is first set aside as `<name>.local` and reported. "Edited" is judged against the hash the previous extraction recorded in `.bundle-manifest.json`, so a file that only changed between releases is replaced quietly. With no record (a first or `--force` extraction), any difference counts.

A file the previous bundle shipped and this one does not is removed, unless it was edited, in which case it too is kept as `.local`. `.local` copies are carried through later updates until you delete them.

``` text
replaced   reference/datadictionary.yaml  (your previous copy saved as reference/datadictionary.yaml.local)
no longer shipped   YAMLs/datadictionary.yaml  (removed; it had not been edited)
no longer shipped   YAMLs/recipes.yaml  (your edited copy kept as YAMLs/recipes.yaml.local)
kept       reference/datadictionary.yaml.local  (set aside by an earlier update; delete it when done)
```

Nothing a user authors is bundled.

### No Configuration

No `.env` ships and none is needed. Both hosts are DNS aliases with defaults (`COSMOS`, `PROJECTS`), authentication is Windows-integrated, and database names come from the manifest. `COSMOS` versus `COSMOS_SneakPeek` versus `Dual` is a template setting. `PULLMANAGER_*` environment variables, or a `.env` passed with `--env` or found in the working directory, override if a host ever changes.

### Delivering An Update

On the Mac:

``` bash
python3 makebundle.py            # dist/bundle.py: the software and every queued pull (D122);
                                 # prints its content_id (D64) and empties the queue
python3 makebundle.py --yamls-only   # dist/yamls_to_transfer.py: the queued pulls alone
python3 makebundle.py --no-queue     # the software alone, the queue left as it is
python3 makebundle.py yaml=IBD_Ancestry  # also carry a blueprint (or older transfer YAML) already at the root
python3 makebundle.py --tdd      # optional: the bundle's own tests
```

Copy it to the VM, with any upload files the blueprints read that are not there already (the build names them, at the paths they need beside `scope.py`). The same sources and transfer YAMLs always produce the same `content_id`, so it tells you whether the VM has the latest. Each build records its bundle's `content_id` in `dist/content_id.txt`, one line per bundle file, and removes a `bundle_with_yamls.py` an older build left there (D122).

**The bundle queue** (D91) is `YAMLs/temp/bundle_queue.txt`, one intake's file name per line, in the order queued, each once. Saving an intake in the app adds it, and the app's Exports tab removes and adds (The App, below). Every build carries it (D122): `makebundle.py`, **Bundle With Manager** and **Bundle YAMLs only** in the app's Exports export each queued intake's blueprint beside it in `YAMLs/temp/`, as `--export-transfer` does, carry them, and then empty the queue. With nothing queued, `bundle.py` is the software alone and says so; `--yamls-only` with nothing to carry is refused. A queued intake that fails validation, or is no longer there, stops the build, with every such intake named (the first error of each). Upload files are not carried (D90): the build names each one to copy.

On the Mac, `python3 scope.py` opens the app, as `python scope.py` does on the VM (D93, D112, D123); it runs from the repository root wherever it is started, and passes anything else it is given to `pullmanager.py`. `python3 utils.py` opens the utilities window (D124).

### Setting Up The VM Folder

The extracted tree is replaced on every update, so everything you author sits beside it:

``` text
<project share>\
  data\                       big reference files; a dictionary if kept outside the bundle
  QueryGenerator\             where you work: the working directory for every command
    bundle.py                 the copied file (or yamls_to_transfer.py)
    scope.py                  written by --extract: python scope.py opens the app (D123)
    utils.py                  written by --extract: python utils.py opens the utilities (D124)
    pullmanager_runtime\      extracted; managed; never put your own files in here
    YAMLs\temp\                 each project's one working blueprint, IBD_Ancestry_blueprint.yaml,
                              placed by bundle.py, saved by Author, run from here (D162)
      replaced\              copies a newer blueprint replaced (D162)
    data\                     its upload files, at the paths the export listed
    runs\                     one folder per project (D57), so projects run side by side
      IBD_Ancestry\           from IBD_Ancestry_blueprint.yaml
        pullmanifest.yaml     written by Export split; pullmanifest.lock beside it while executing (D67, D142)
        IBD_Ancestry_blueprint.yaml  the copy the split was made from, its record (D142, D162)
        execute-<date>-<time>.log    the latest Execute's log (D68)
        older_logs\           every earlier log, moved there as an Execute starts
        cosmos_parquets\, sneakpeek_parquets\, uploads_parquets\   written by Artifacts (D72)
        contents.md           what each table and column is (D73)
        load_parquets.R/.py, HOW_TO.md   (D75, D89)
        utils.py, utils\client\   the client's utilities window: the parquet and transcription viewers (D148)
        pull_files\
          split\              sessions\ and uploads\, written by Export split
          sql\                written by Preview SQL
      backup\                where pulls are backed up when the backup folder can't be reached, or none is set (D149)
      .pullmanager-gui.json   the launcher's remembered blueprint (D103); on the Mac in cleanup/runs/
```

Typed paths resolve from the working directory, so the parent folder is plain `..\data\datadictionary.yaml`. Upload `file_loc` values resolve relative to the blueprint, which reaches them with `../../` from `YAMLs\temp\`, so upload files sit beside `scope.py` at the paths the export lists. The data dictionary is the bundle's (D150): nothing remembers another, and only `--datadictionary` on a typed command, or `datascope.json`'s `datadictionary`, names one.

Each project has one working blueprint on the VM, in `YAMLs\temp\` (D162). A change made on the VM is made to it in the app's Author half (open, adjust, Save, Transfer to Run), or by hand; there is no second file to fall behind. A new version from the Mac replaces it, the old one kept in `replaced\`; VM changes are copied to the Mac by hand. Once a pull is packaged, its working blueprint is removed if unchanged since the split, and its record is the copy in its run folder, which Author opens. A `_transfer.yaml` beside `scope.py`, from before D162, is still read. A recipe change is made on the Mac and re-exported.

### The Whole Pathway On The VM

``` bash
python bundle.py                       # verifies, shows the content_id, y extracts (D64)
python scope.py --tdd                  # prove the delivery

python scope.py                        # the app: Author and Run (D93, D123)
python utils.py                        # the utilities (D124)

# or the same steps by hand
python pullmanager_runtime/scripts/makeYaml.py --template YAMLs/temp/IBD_Ancestry_blueprint.yaml --export-split --out-dir runs/IBD_Ancestry
python scope.py --dry-run runs/IBD_Ancestry/pullmanifest.yaml --out-dir runs/IBD_Ancestry/pull_files/sql
python scope.py --execute IBD_Ancestry     # by the project's name (D66)
python scope.py --artifacts IBD_Ancestry   # by hand; a clean pull packages itself (D72, D141)
python scope.py --running                  # every pull, and which are executing (D67)
python scope.py --backup                   # back up every pull (D149); Run's Back up all
```

The repair loop for a VM-side bug: read the file out of the bundle or the extracted tree, bring the text back to the Mac, fix the source, rebuild.

------------------------------------------------------------------------

## Authoring: YAML Manager

### Inputs

| File | Role |
|------------------------------------|------------------------------------|
| template | The pull: project metadata, `cosmos_vars`, `run_vars`, `project_vars`, `multipliers`, `batching`, `upload_cohorts`, `cohorts` |
| `reference/recipes.yaml` | Reusable cohort and batching definitions, referenced by name. Mac only (D49) |
| `reference/datadictionary.yaml` | Source of truth for Cosmos tables, columns, types and standard where lines: 21 tables, most checked against their pages |

Paths typed on the command line (`--template`, `--recipes`, `--datadictionary`) resolve from the working directory, like any command-line tool. Defaults are where `datascope.json` says, else beside the install (D111). `--datadictionary` is honoured by every route: validation, the UI, pre-YAML, transfer and split export. A template that does not exist is a one-line error, not a traceback.

The recipes file is read only when the template refers to it: a cohort with `recipe:`, or a batching item that names a batching recipe (`sex`, `{state: {...}}`). A transfer YAML refers to none, so it needs no recipes file, and a missing or broken one cannot stop it. A template that does refer to recipes, with no recipes file, is refused with `recipes_not_found`, listing every reference and pointing at `--export-transfer`.

### Recipes And Table Inputs

A recipe that reads another generated table names it through a variable:

``` sql
INNER JOIN {{prefix}}_{{HospitalICDTable}} AS hic ON ...
```

YAML Manager infers from this that the recipe has a **table input** `HospitalICDTable`, and which columns it reads through the alias (`hic.X`). `{{prefix}}` is the project's temp prefix (Naming, below), filled in by YAML Manager; `{{prefix}}_Patients` names a generated table directly.

**`PKTable` is the one input bound automatically**, to the session's root PK. Every other table input must be bound on the cohort that uses the recipe:

``` yaml
cohorts:
  - name: Diagnoses
    recipe: DiagnosesByHospitalICD
    vars:
      HospitalICDTable: HospitalICDCodes
```

An unbound table input is the error `unbound_table_input`. It never picks a table, but it names the ones that could work, judged by whether their known columns cover what the recipe reads:

``` text
Cohort `Diagnoses` joins a table through `HospitalICDTable` (as `hic`) and reads
column(s) ICDCode from it, but nothing binds `HospitalICDTable`. Tables in this
template that fit: HospitalICDCodes (upload). Bind it on the cohort using
recipe `DiagnosesByHospitalICD`: `vars: {HospitalICDTable: HospitalICDCodes}`.
```

An upload whose schema cannot be known (a `dbtable` with no declared columns, or a parquet where `pyarrow` is missing) is listed separately as a table that *may* fit. An ordinary missing scalar variable is `missing_variable`.

**Recipe sets** (D135). `recipes.yaml` has a `recipe_sets:` list: each set names recipes in the order they run, a chain's first table first, with their table inputs to each other bound. Adding a set puts its recipes in the template as ordinary fact tables, bound, in a table group named for the set; after that nothing about them is special. A set has no variables of its own.

**A recipe's text adds to the dictionary's** (D121). When recipes are imported, a recipe table's description becomes the dictionary's description of its source table (its first `from` entry) followed by the recipe's, and a recipe column's description the dictionary column's followed by the recipe's; text already containing the dictionary's is kept as it is. A description the template writes itself is the whole text, and so are columns the template lists itself. Granularity is the recipe's. A recipe's description therefore says only what it adds. The transfer YAML carries the collated text as the table's own, so it is never collated twice.

### Upload Files

Parquet is the upload format, because it carries types (D54). An upload declares the file and, optionally, types for some of its columns:

``` yaml
upload_cohorts:
  - name: HospitalICDCodes
    dest_table: HospitalICDCodes
    file_type: csv              # parquet, csv (converted at split) or dbtable
    file_loc: "csv/HospitalICDCodes.csv"
    columns:                    # optional; the rest land as they are
      - name: PatientDurableKey
        type: BIGINT
      - name: ICDCode           # the table's name for it (D98)
        from: DiagnosisCode     # the file's
      - name: Notes
        drop: true              # left out as it lands
    pending_transfer: true      # the file will only exist on the VM (D97)
```

- A **parquet** keeps its own types; a declared type converts that column (R often writes large IDs as doubles, which would otherwise land as `FLOAT`).
- A **CSV** is converted to parquet when the split is exported: declared columns get their type, the rest stay text. A value that does not fit stops the split, naming the column. `makeYaml.py --csv-to-parquet FILE.csv [--out FILE.parquet] [--column NAME=TYPE ...]` converts one file by hand.
- A **dbtable** is a table already in the project database (`source_table`, else `dest_table`).
- A `file_loc` ending `.csv` under `file_type: parquet`, or the reverse, is `upload_type_mismatch`.
- A **missing upload file** is a warning where the output is a plan that travels, the transfer YAML and the app, since the file may only exist on the VM: the transfer is still written, and its listing marks the file "not here yet". It is an error at the split, which needs the file, so on the VM Validate and Export split confirm every file is in place.
- **`pending_transfer: true`** says a missing file is expected (D97): it is reported as pending (`upload_pending_transfer`), a third kind of message beside errors and warnings (D99), not as a warning. Only a `csv` or `parquet` with a `file_loc` can be pending, and only `true` or `false` (`bad_pending_transfer`). The split still needs the file.
- **Typed-in columns.** For a table nothing here can read (a `dbtable`, or a file not here), the names under `columns:`, with or without types, stand in for its schema (D97), so batching and bindings are checked against them. Without them its columns are unknown: batching is left unchecked (`batch_columns_unchecked`) and a binding lists it as a table that may fit. A missing file's declared types are still checked.
- **Renaming and dropping** (D98). An entry with `from:` names the file's column, and `name` is what it lands as; `drop: true` leaves a column out; unlisted columns land as they are. Pullmanager applies them as the file lands in Projects, so the copy and its Cosmos temp have the new names, and validation reads the table as it will land: bindings, suggestions, batching and the uploaded PK's key all use the new names. A rename or drop of a column the file lacks is `unknown_upload_column`; two columns ending with one name, `duplicate_upload_column`; a dropped or renamed key, `dropped_key_column` or `renamed_key_column`; any of them on a `dbtable`, `upload_rename_on_dbtable` (D100). A declared type names the file's column.
- A column whose name has quote characters in it (a CSV header written `'DiagnosisCode'`) is `quoted_column_name`, a warning whose fix is to rename it (D98, D109); one already renamed or dropped is not warned about. In the app, choosing a supporting table's file writes that rename by itself, to the name without quotes, shown in Lands as (D159); it happens only when the file is chosen, so a rename back to the quoted name stays.
- Declarable types: `BIGINT`, `INT`, `SMALLINT`, `TINYINT`, `BIT`, `FLOAT`, `REAL`, `DECIMAL(p,s)`, `DATE`, `DATETIME`, `DATETIME2`, `VARCHAR(n)`, `NVARCHAR(n)`, `CHAR(n)`. Anything else is `bad_upload_type`; a declared column the file lacks is `unknown_upload_column`.

With `pyarrow`, validation reads a parquet's own columns, so recipes bound to it are checked like any other table. An upload marked `type: pk` is the template's PK (only one PK per template); batching is checked against its file's columns. `split_after_build` multipliers on an uploaded PK are refused (`split_after_build_on_uploaded_pk`): batch by that column instead, which puts each group in a batch of one table rather than a table of its own, or split the list before uploading it and run one pull per group.

### Validation

Validation is static: it reads the template, recipes and dictionary, never a database. Rendering is skipped once blocking errors exist, so each problem is reported once rather than again as a rendering failure. Every error is collected, not just the first.

Messages are errors, warnings, or pending (a file marked `pending_transfer` that is not here, D99), printed `ERROR`, `WARN` and `PEND`. Every error carries a **fix**: what to change, and where. On the VM the YAML is edited by hand (D49), so an error that only says what is wrong leaves the reader guessing. A test fails if any `result.error(...)` in `makeYaml.py` has no `fix=`. The context is a field path, the cohort by position and name:

``` text
ERROR [missing_variable] at cohorts[0] (Patients): filter.where[1]: Cohort `Patients` requires variable `ICD_Value`, but no value was provided.
      fix: Add `ICD_Value: <value>` under the top-level `vars`, or under this cohort's own `vars`.
```

Checks:

- Required variables and table inputs are bound (above).
- Recipe references resolve. A table's `filter` may list `add_where` and `add_join`, lines appended to its own `where` and `join` (its recipe's, for a prefabricated table) when the template is read, so a template can narrow a recipe without replacing its lines (D105); anything but lines is `bad_added_lines`. A transfer YAML writes the recipe out with them appended.
- Upload files exist, or are marked pending; their columns (CSV header, parquet schema, or the columns typed in), as renamed and dropped, carry what bound recipes read; declared upload columns exist and have a type an upload can take (Upload Files, above).
- Zero or one upload cohort is `type: pk`, and it declares `key_columns`.
- Every multiplier has at least one level (`multiplier_without_levels`, naming it): with none, the product of levels is empty and every cohort would vanish without a word (D84).
- Multiplier definitions are well formed. Each `split_after_build` level says which PK rows are its own (`column` and `values`, or `where`; `split_level_without_condition`) and splits the PK only (`split_after_build_target`). `role` is `control` or absent; `row_mult` needs `role: control`, is a positive number, and needs exactly one case level beside it; a sampled control's PK needs a key (D59). `role` and `row_mult` on a `during_build` level are refused.
- Dedup names the cohort's own columns: every `dedup_keys` and `dedup_order_by` name is one of its `columns` (`bad_dedup_column`, whose fix lists them; a list written inside `dedup_order_by` is told it takes plain names). `dedup_order` and `order_by` are refused (`old_dedup_order`) (D58).
- `random_pk_sample` under `smallset` needs the PK's key, from `dedup_keys` or `key_column` (`random_sample_without_key`) (D60).
- A retired option (`stop_at_for_non_pk_tables`, `print_md`, `printout_md`) warns, naming itself (`retired_option`) (D62).
- Batching definitions, field by field, since a transfer YAML writes them out in full: a known `kind`; `column_values` has a `column` on the PK and a non-empty `values` list; `row_chunk` has a positive `rows_per_batch` (the shipped `chunk` recipe's `required` placeholder is refused). `values: all` warns that the pull will stop at it.
- `project_db` is set (`project_db_missing`: a warning while writing, an error at the split, which needs it) and shaped like `PROJECTD<code>` (`project_db_unexpected`, a warning). Whether the database exists and can be opened is only known when Execute connects.
- Temps are named with `{{prefix}}_`: `##JVM_` anywhere in a cohort is `old_temp_marker`, whose fix is the line rewritten. `prefix` is a reserved variable, and `temp_prefix` must be letters, digits and underscores, at most 30 (`bad_temp_prefix`).
- Every cohort column against the data dictionary (below).
- **Table order** (D134). A run builds its tables in Fact Tables order, so a table that reads another fact table (`{{prefix}}_<table>`) must come after it, else `table_read_before_built`, naming both and which to move. Nothing is reordered for you (D45). Each multiplier level's copy reports once.
- **Table groups** (D134). `table_groups` is a list of groups, each a name and its tables; a table in two groups, the PK in a group, a name that is not a table, two groups of one name, or a group called `run` (the tables in no group run under that name) is an error, and an empty group a warning. A table that reads a table in another group, or in no group while it is in one, is `table_reads_another_group`: its temp is gone once its group's connection closes, so the fix is one group for both.
- **Multiplied tables read by their written name** (D139). Under multipliers each level's copy of a table is named for its level (`UCOrders`), but SQL written as `{{prefix}}_Orders` still names `##<prefix>_Orders`, which is never made, so the run would fail at Execute. It is a warning (`multiplied_table_read_by_name`), once per pair: for the PK, whose fix is `{{prefix}}_{{PKTable}}`; for a fact table, which has no fix yet but a template per level (roadmap: Generated-table dependencies).
- **Every table's written SQL** (D118, D129): each `alias.Column` in its `from`, `join` and `where` lines (after variables are filled; strings and table names aside) names an alias the table's own `from` or joins define, else `undefined_sql_alias`, naming the aliases it has. A fact table that joins nothing the pull makes (no `{{prefix}}_` or `#` table anywhere in its lines, a subquery in a where line included) would read the whole Cosmos table, and is `fact_table_not_joined`, whose fix is the join to the PK. A column the dictionary does not list for that table warns (`sql_column_not_in_dictionary`), as does a PK joined to a `...Fact` table without `dedup_keys` (`pk_join_without_dedup`). This is what would have stopped Infant_RSV's `pk.` and the UC pulls' whole-table reads on the Mac.

A passing `--validate` writes nothing, and says what it found and what it checked (D62):

``` text
OK: IBD_Ancestry_transfer.yaml is valid: 16 cohort(s) in 8 session(s), 24 run(s); 0 warning(s) above.
Checked: recipes written out in it; every variable and table binding; each column against the data dictionary (...); 1 upload file(s) present, with their declared columns; multipliers and their levels; batching; dedup columns.
Nothing was written. Export split writes the pull.
```

Authoring rules applied on the way:

- `dedup_keys` is canonical, a list of lists (`[[PatientDurableKey, BillingCodeValue]]`); legacy `dedup_key` is accepted and normalized, with a warning. `dedup_order_by`, a plain list (`[StartDateKey]`, an entry may end in `DESC`), chooses which duplicate survives. Both name the cohort's columns and render as their sources, since `ROW_NUMBER` sits in the SELECT that defines those names (D58): `PARTITION BY def.PatientDurableKey, dt.Value ORDER BY def.StartDateKey`. Without `dedup_order_by`, which duplicate survives is arbitrary and may differ between runs, and a dry run says so.
- `stop_at_for_pk_table` limits the **root** PK cohort only: the one joining no other generated temp. Under `Dual` there is one root per database, and each is limited. A sampled control's limit is `row_mult` times it (D59).
- With `random_pk_sample`, the limited PK is ordered by `HASHBYTES('SHA2_256', <key>)`, the key being its first dedup key set or its `key_column`: a spread-out sample, the same on every run (D60). Without `smallset` there is no limit, and nothing to sample.
- `stop_at_for_non_pk_tables`, `print_md` and `printout_md` are retired: gone from the Builder and the example template, and a template that still has one gets a warning (D62). (`makeYaml.py --report` is unrelated: it reports on the template, not a run.)
- `from` and `join` are schema-qualified alike, and only where no schema is present, so `dbo.dbo.` and a qualified temp are impossible.
- `sql_condition(column, var)` renders a scalar as `=`, a list as `IN`, a wildcard value as `LIKE`, and warns on `_` inside a `LIKE` value.

### Data Dictionary Validation

Columns arrive as `source: dt.DiagnosisKey`, so the alias is resolved to a table through `filter.from` and `filter.join` first. That is what catches `p.Type` written where `dt.Type` was meant: the alias exists and the column exists, on a different table.

| Case                             | Result                                  |
|------------------------------------|------------------------------------|
| Table absent from the dictionary | Error. Add the table to the dictionary. |
| Column absent from the table     | Error.                                  |
| Alias cannot be resolved         | Error.                                  |
| A column nobody types            | Error (`column_type_missing`), after the three above. |

#### Column Types Come From The Dictionary

A template declares no types (D115). When recipes are imported, the step every output shares (validation, the transfer YAML, Export split, Preview), each column whose source is a plain `alias.Column` on a dictionary table takes its type from the dictionary. The dictionary wins silently: a type a template still declares on such a column is replaced without a message. The transfer YAML carries the filled types, so the split and the dry run on the VM read them from it. Pre-YAML export (`--export-preyaml`) is not filled; it never travels.

| Dictionary `type` | Filled in as |
|------------------------------------|------------------------------------|
| A page's type with a size: `nvarchar(300)`, `varchar(50)`, `decimal(10,2)` | As written: `NVARCHAR(300)` |
| A page's type: `bigint`, `int`, `tinyint`, `smallint`, `bit`, `float`, `real`, `date`, `time`, `datetime2`, `smalldatetime`, `money`, `uniqueidentifier` | As written: `TINYINT` |
| `string`, `text`, or a text type with no size | `NVARCHAR(900)` |
| `integer` | `INT` |
| `boolean` | `BIT` |
| `numeric` | `FLOAT` |
| `datetime`, `date/datetime` | `DATETIME2(7)` |

A parenthetical annotation is ignored: `bigint (foreign key to EncounterFact)` is `BIGINT`. A page's size is the widest the source can hold, so nothing is cut off and nothing is wider than it needs to be. A string with no recorded size is Unicode, as Cosmos text is (`VARCHAR` would turn other scripts to `?`), and 900 wide, under the 1000 the user keeps to. Declared widths also set SQL Server's memory grant for a sort, so a cohort with many unrecorded strings under `dedup_keys` asks for more than it uses until its sizes are recorded.

A column the dictionary cannot type keeps its declared type, and without one is `column_type_missing`: an expression, a column of a generated temp or an upload, or a dictionary type the fill-in does not know (which also warns, `dd_unknown_type`, and says to write the type as the page shows it). A missing dictionary types nothing, so every column is then an error.

The table builder writes no types, but knows each column's for its join checks, from the same fill-in (D115).

#### What The Dictionary Records

Each table's columns as its Cosmos dictionary page shows them, and only those with the database icon in its Available column: a column marked SD alone is left out (D116). Types are written as the page writes them (`nvarchar(300)`, `tinyint`, `float`), with any foreign key after them (`bigint (foreign key to LabDim)`). A comment above a table says what was checked against its page, and when; the file's foot lists what is only partly seen, any screenshotted table left out, and the tables a checked foreign key points at that the dictionary lacks (D130, D160). Every table in the dictionary has been checked against a page.

`standard_where`, at the end of a table, lists the conditions a pull on it normally carries, written without an alias: a live row (`_IsDeleted = 0`; on PatientDim also `IsCurrent`, `IsValid` and `UseInCosmosAnalytics_X`) and, for a checked fact table, the partition key's date window (`ArrivalDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}`) (D120, D130). The table builder adds them, under the table's alias, when the table is chosen; nothing else reads them. A test holds each line's column to its table.

Validation is only as right as the dictionary. `LabComponentResultFact.ReferenceValueHigh_X` and `ReferenceValueLow_X` were recorded as `numeric` and are `nvarchar(300)`, so a `FLOAT` went to Cosmos, where the insert failed with error 8114, which names no column (D114). The tables have since been checked against their pages (D130); a column the pages lack is gone from the dictionary, so a pull naming it is stopped on the Mac (`MedicationOrderFact.OrderedDateKey` was one). Many flags (`_IsDeleted`, `_IsInferred`) are `tinyint` where the page shows it: a `BIT` would turn a stored 2 into 1 without an error.

Validation does not cross-check a template's nullability, because `nullable: false` on a nullable column is the documented way to force an `IS NOT NULL` filter. The dictionary's own `nullable` is checked against Cosmos by the audit (D161); the Author view copies it onto each column it adds.

### Keys And Relationships In Cosmos

Validation checks that every column exists with the right type, not that a join is right. `ON tc.TerminologyConceptKey = dt.DiagnosisKey` passes and is wrong; a correct join can still multiply rows, if it meets a table with several rows per key and no filter. Knowing each table's keys, and what each foreign key points at, would let validation check joins. Three questions were put to the VM's AI: are keys declared where SQL can read them, do they hold in the data, and what does the interactive data dictionary show. It explained its queries rather than running them, and said its dictionary answers were "paraphrased based on Epic's conventions, not exact text", so nothing below has been counted, and what it alone said is unverified. The brief and its answer are summarized here and in the roadmap (A Join Check From The Dictionary's Keys); the first page seen is `reference/DDict image refs/DiagnosisEventFact/DiagnosisEventFact 3 columns and ER diagram.png`, beside the user's screenshots of the others, one folder per table.

| Finding | Source | How sure |
|------------------------|------------------------|------------------------|
| Keys are not readable through SQL: `INFORMATION_SCHEMA` constraints, `sys.foreign_keys` and `sys.indexes` are hidden from analyst logins, so an empty result means "cannot see", not "no keys" | VM AI; fits our narrow permissions (D33) | Likely. The permission probe (`HAS_PERMS_BY_NAME`) has not been run |
| The dictionary cannot be exported; only Epic could supply it as a file | VM AI | Unverified |
| The interactive data dictionary does declare keys: each table's own key, each foreign key with the table and column it points at, and how many rows match on each side | The `DiagnosisEventFact` page, then each checked table's (D130) | Seen, for the checked tables; noted in each foreign key column's type |
| `DiagnosisTerminologyDim` holds a row per terminology, several per `DiagnosisKey`, so joining it on `DiagnosisKey` alone duplicates diagnoses; constrain `Type` (our recipes do) | VM AI | Likely; not counted |
| `PatientDim` keeps history. Its own key is `PatientKey`, one per version of a patient; `DurableKey`, what other tables point at, is one per patient, and `IsCurrent = 1` picks one row for it | The diagram; VM AI | The keys seen; the history likely |
| `-1` in a foreign key means unmapped or a mixture (`LabComponentKey`, `MedicationKey`), and fact tables carry placeholder rows with negative keys, so `-1` is not an orphan; `0` varies by column | VM AI, as Epic convention | Unverified |
| `create_date` is when a database was created, not when its data was loaded | VM AI | True in general. D51 holds only if a refresh recreates the database, which the date matching the last refresh suggests; confirm across the next one |

**Reading a dictionary page** (`DiagnosisEventFact 3 columns and ER diagram.png`):

- **Columns tab:** each column's type, and for a foreign key the *table* it points at, in blue. An Available column shows where the column exists: an SD icon, a database icon, or both. Only the database icon's columns can be pulled, so only they go in our dictionary (D116). Expanding a row shows its description, whether it allows null, and its de-identification method. A `Partition key` badge (here `StartDateKey`) marks the column that lets SQL Server skip most of the table when filtered; our recipes filter it.
- **ER Diagram:** the table's own key (filled key icon, `DiagnosisEventKey`), then a "Foreign keys" list (outline key icons). A dashed line runs from each foreign key to the table it points at, ending on the *column* it lands on, which the Columns tab does not give. The ends give the cardinality: a crow's foot on this side (many rows here), a bar on the other (one row there).
- **For `DiagnosisEventFact`** that reads: `DiagnosisKey` → `DiagnosisDim.DiagnosisKey`; `PatientDurableKey` → `PatientDim.DurableKey`; `EncounterKey` → `EncounterFact.EncounterKey`; `AgeKey` → `DurationDim.DurationKey`; `StartDateKey`, `EndDateKey`, `NotedDateKey_X`, `UserEnteredDateKey` → `DateDim.DateKey`; `SourceComboKey` → `DiagnosisEventSourceBridge`, a bridge from one combination key to several `SourceDim` rows. Our dictionary had `DiagnosisKey` pointing at "DiagnosisDim/DiagnosisTerminologyDim"; the interactive dictionary says `DiagnosisDim`, which the checked entry now has.

#### Checking Screenshots Into The Dictionary

New dictionary-page screenshots go in `reference/DDict image refs/Unordered/`, in any order. To check them in:

1.  **Sort.** Read each page's table name and move its screenshots into a folder named for it, beside the others. If the heading is cut off, the ER diagram's centre box or the reporting table name gives it. A table cut across several screenshots, or taken twice, goes in one folder. `Unordered/` ends empty.
2.  **Check each table column by column**, as above (What The Dictionary Records, Reading a dictionary page). List only the database icon's columns (D116), with types as the page writes them. The table's own key is `bigint (primary key)`. A foreign key goes after the type, naming the column it lands on where an ER diagram shows it (`bigint (DateKey; foreign key to DateDim)`, `bigint (foreign key to PatientDim.DurableKey)`). The partition key is noted. An existing column keeps its nullability and its description, unless the screenshot shows the page's description whole, which then replaces it. A column the page lacks is removed.
3.  **`standard_where`**: `_IsDeleted = 0` where the table has it; for a fact table, the partition key's date window; for a Snapshot (Type 2) dimension, `IsCurrent = 1` as well. A page with no `_IsDeleted` gets none. A partitioned table whose partition key is not seen gets no window.
4.  **Every table with a screenshot goes in**, whether or not a pull reads it; the dictionary audit (D155) says what is wrong. A page whose every column is SD only stays out, since it has nothing to pull (D160).
5.  **A comment above each table**: that it was checked against its page, and when; where its screenshots are; whether its keys come from an ER diagram or only the Columns tab's links; and what was not seen (descriptions cut off at the right, no Overview, no partition key).
6.  **The foot**: what is only partly seen, the screenshotted tables left out and why, and the tables a checked foreign key points at that the dictionary lacks.
7.  **Then**: run the suites. Validate every intake, and compare each transfer YAML's types with what the new dictionary fills in. Export again each one that changed; only its `type:` lines should differ. List what a removed column breaks (every YAML under `YAMLs/` that names it). Build a full bundle, `makebundle.py` with the re-exported intakes queued, not YAMLs only, which does not carry the dictionary, and run the audit with it.
8.  **Report** each screenshot that hides something (columns past the bottom, a partition key not seen), for the user to take again.

What this means for a join check, still to decide (roadmap, A Join Check From The Dictionary's Keys): relationships come from the interactive dictionary into `datadictionary.yaml` by hand, since SQL cannot supply them. Each needs more than a target table: the column it lands on, its cardinality, whatever filter makes the other side one row (`IsCurrent = 1`, a `Type`), and its sentinel values. Data checks then confirm it: the parent key unique under its filter, and no child rows without a parent, sentinels aside.

### Choosing The Cosmos Database

`cosmos_vars.cosmos_db` in the template, never VM configuration. A template that names none pulls from both, as `Dual` (D86); the project database and dates have no such default (D83).

| Setting | Connects to | Cohorts rendered |
|------------------------|------------------------|------------------------|
| `COSMOS` | `COSMOS` | once |
| `COSMOS_SneakPeek`, `sneakpeek`, `sp` | `COSMOS_SneakPeek` | once, each tagged |
| `Dual`, `both` | `COSMOS` | twice; `_sp` variants tagged `COSMOS_SneakPeek` |

A cohort tagged with its own database qualifies its tables three-part (`COSMOS_SneakPeek.dbo.PatientDim`), because a two-part name resolves against whichever database is connected. Global temps stay unqualified: tempdb does not follow the connected database.

An `_sp` copy reads the `_sp` copies of the generated tables it joins: `##tesrun_Patients` becomes `##tesrun_Patients_sp` in its SQL, so it pulls for the SneakPeek population. Uploads are shared by both copies and keep their names.

### Outputs

- **Blueprint** (`--export-transfer`, `--out` to choose the file): what the VM receives (D49), called the transfer YAML before D162. The template as written, with cohort recipes merged into their cohorts and batching items replaced by their full definitions; multipliers and batching are declared, not applied. Grouped settings stay in their groups (`cosmos_vars.project_db`), each once, so editing one by hand on the VM takes effect. Written only if the template passes full validation. Named `<project_folder>_blueprint.yaml` (D162), written beside the template, or where `--out` (or Author's Transfer to Run, the working folder) says. Written to another folder than the template's, each upload inside the template's folder keeps its `file_loc` and is copied there at it, so that folder is the unit to carry across; one that leaves the template's folder (`..`) is rewritten to reach the same file from the transfer's folder, and one still outside it, or absolute, is not copied, with the warning `upload_not_copied` (D103). The export lists every upload path to carry. It opens with a `transfer:` block: `from_template` (file name), and, when recipes were used, `recipes_sha256` (first 12 hex digits) and `recipes_used`. No timestamp, so the same inputs give the same file. The split drops the block from its YAMLs and records it as the manifest's `source.transfer`, with `source.recipes` null.
- **pre-YAML** (`--export-preyaml symbolic`): the template, portable, close to what was authored, recipe references left symbolic. `expanded-recipes` inlines cohort recipes only, for inspection. Upload paths stay relative to where it was written, so moving one means moving its uploads too.
- **Split** (`--export-split --out-dir`, the run folder; without it `runs/<project>`, D57, D142): below. The manifest and a copy of the template it was made from at the folder's top, the rest under `pull_files/split/`. The copy's relative `file_loc`s are repointed to reach the same files from the run folder, so Author opens it as it opened the template (D162), and the manifest records the template's `source.template_sha256`. The split fixes the pull's **table prefix** (D163): the template's temp prefix, numbered 2, 3 and on if another pull beside it under the runs folder has it (and never `jvm` or `local`, which naming strips), or the one it had if split before; written as the manifest's `project.table_prefix` and into every phase document. Self-contained: upload files are copied into `pull_files/split/uploads/` and `file_loc` repointed, relative to the manifest, so the run folder is the unit to copy or archive. A CSV upload is written there as parquet (`file_type: parquet`), with its declared types. Parquet output is deterministic: the same inputs give the same bytes.
- **Report** (`--report`): markdown summary of the template.

### The App: Author

Every window (the app, the utilities window, each utility, the parquet viewer copied into each pull's folder included) says "Designed and built by Jason Mathias" at its foot, small and grey, packed first so a small window squeezes its contents and never that line (D145). Beside it is the bundle it runs from, `· bundle ca0fa906`, the first 8 characters of the `content_id` in the `.bundle-manifest.json` above it; run from source on the Mac, there is none and nothing is shown. The viewer copied into a pull's folder has no bundle above it, so Artifacts writes the bundle into the copy's `BUNDLE = ""` line as it copies it (D147). `python scope.py --version` prints the same.

One window with two halves, **Author** and **Run**, the same on the Mac and the VM (D93): `python scope.py` on the VM, `python3 scope.py` on the Mac (D112, D123). Run is the launcher (below). If Author cannot open, Run still does, and says why in Author's tab. Standard library, tkinter, and makeYaml only.

**The model and its views** (D92). `scripts/yamlmanager_model.py` holds the draft (the template itself, a plain mapping) and every edit to it, and answers what a view shows; it has no tkinter and no HTML in it. `scripts/yamlmanager_tk.py` is the tkinter view: every value it shows is read from the model and every edit is a call to it, so another view (a web UI, say) would share every rule. An edit the model refuses raises `DraftError`, and the view shows its message in the status line.

- **Workspace.** The working folder is makeYaml's `transfer_home`: the repository root on the Mac, the folder beside the extracted bundle on the VM. On the Mac, intakes live in its `YAMLs/temp/`; on the VM, each project's one working blueprint lives there (D162). Recipes and the dictionary are where `datascope.json` says (D111); with no recipes file (the VM) Prefabricated lists nothing and Save as Recipe says recipes are kept on the Mac.
- **The VM side** is where the app runs from an extracted bundle (D108), known from the `.bundle-manifest.json` beside the code, not from the system. There Pending transfer is not offered (the model refuses it), and a missing upload file is an error in Validate, as at the split; on the Mac it is a warning, or pending if marked so.
- **Checking.** The model compiles the draft in process, as a transfer is checked (a missing file a warning, or pending), without saving it (`compile_yaml(template_data=...)`), and turns each message into its kind and the Builder field it points to, parsed from its context (`cohorts[2]` is a Fact Table or the PK, `upload_cohorts[1]` a Supporting Table or the PK, and so on). A crash in the check is an error, `compile_crashed`, and the draft is kept. The view checks 0.4 s after the last edit.
- **makeYaml's analysis** records, for the view, every table's columns as it will land (`table_schemas`) and each upload file's own columns (`upload_file_columns`).

The top bar: **Project name**, a list of projects, each once with the file it opens (`Infant_RSV  ·  working blueprint`; D162): on the Mac its intake; on the VM its working blueprint, else an older copy (a `_transfer.yaml` beside `scope.py`, an intake in `YAMLs/temp/`), else the copy in its run folder (`finished: its run folder's copy`). The list is read again each time it opens. Choosing one opens it; typing names the draft. Then **Browse**, **New**, **Save** and **Transfer to Run**, with where Save writes. The window's title names both halves, `Telescope · Author: <project> · Run: <pull>`, with `*` after the project while unsaved (D166). A draft with unsaved changes asks before another is opened.

The **Builder** (D96) has its sections down the left, each marked when a message points into it, coloured by the worst. Each message is also shown at the entry it points to, at the top of that entry's box, in its colour with its fix; one for a section and no entry at the top of the section (D169). The background check fills these in place, without redrawing the page, so typing is not interrupted:

- **Project.** Pull from: Cosmos and Cosmos_SneakPeek, both on by default (both is `Dual`; one is that database; neither writes `cosmos_db: none` and is an error, D99). Project DB, and the dates as `YYYYMMDD`, filled from `template.yaml` where a draft has none (D83, D86). "Collect all patients matching criteria", on unless `smallset`: off enables Sample size (`stop_at_for_pk_table`) and Random sample (`random_pk_sample`).
- **PK Table.** The one PK, from a prefabricated PK recipe, a table built from the dictionary, a parquet, a CSV, or a Projects table (`dbtable`). Choosing another replaces it, after asking. The PK is edited in place: its name; a file's location; its **Row key** (the columns that make each row one of its own, which the uniqueness check, chunk order, the random sample and controls follow), prefilled with `PatientDurableKey` when the file, or the columns typed in for it, has that column (D107); Pending transfer (Mac only); and its columns as a Supporting Table's. A recipe or built PK has its variables, and a recipe PK its filters, as a Fact Table's. A PK from the dictionary is chosen by its table and built in place.
- **Supporting Tables.** Every upload except the PK: name, destination, type (parquet first), file or Projects table, Pending transfer (Mac only), and its columns. A file that can be read lists each column with what it lands as, an optional type and Drop (D98); one that cannot takes its column names typed in (D97), saying why. **Or paste a CSV** (D127): rows with their header, comma- or tab-separated (as a spreadsheet copies them), summarised as they are pasted (columns, rows, the first row); a blank or repeated header, or a row of another width, is refused naming the line. **Add pasted table** needs a name, writes `YAMLs/temp/csv/<name>.csv` (never over a file already there) and adds a supporting table reading it, like any CSV.
- **Multipliers.** `during_build` only: a name, and levels, each a strat and its variables (`ICD_Value: K51.%, K52.%`, `;` between variables).
- **Splitters.** Each says which it is. **Separate PK per level** (D134) is a `split_after_build` multiplier: levels with a PK column (chosen from the PK's columns), values, and an optional role and row mult. **Pieces of one table** is batching: a PK column with values (blank: every value, D82) and Separate parquets, or a number of rows. A splitter by column needs the PK's columns; before there is a PK, or when its columns are unknown, the section says so and only rows can be added. Editing a batch writes it out in full, as a transfer YAML does, so one written as `sex` becomes its definition.
- **Fact Tables.** At the top, set apart by a coloured frame, **Add a fact table** (D110): Prefabricated (a non-PK recipe) or From data dictionary (a table), each with a Name; choosing a dictionary table opens its whole form in place. Below, each table, prefabricated or built from the dictionary, with its buttons in order: Edit (a built table's form, inside its entry), **Duplicate** (a copy after it, `<name>_copy` with its own destination, opened for editing when built, D125), Remove, and Save as Recipe (a built table, D56); then its position (a number and Enter moves it; the PK keeps its place), its variables, and its table inputs. A variable left blank says where its value comes from: "set by multiplier IBDType", "from the PK (K50.%)" or "from the PK's value"; one nothing supplies is marked required. A table input is bound from a list of the template's tables, those whose columns cover what it reads first, each with a ✓ and the columns it has, a ✗ and the ones it lacks, or a ? where its columns are unknown. Nothing is picked for you (D45). A prefabricated table shows its **Description** and **Granularity** as imported (the collated text, above), to keep, clear or add to; only a change is written as its own (D121). It has **Filters added to this table** (D105): a where line by column, with an operator (D119): `=`, `<>`, `<`, `<=`, `>`, `>=`, `IN` (values separated by commas), `LIKE` (a pattern with `%`), `BETWEEN` (a lower and a higher box), or **In supporting table** (`x.MedicationKey IN (SELECT [Code] FROM {{prefix}}_MedCodes)`: `IN`, not a join, so a code listed twice cannot duplicate rows). Numbers and `{{Variables}}` are written as they are, text quoted; a `%` or a comma list under `=` is refused, naming LIKE or IN. And a join by column to another table of the template, laid out as the SQL reads (type, JOIN, the other table and its column, the operator, "by column", this table's column; D125), refused unless the two columns' types match, with the match said beside it as any of its choices changes. They are written as the table's `add_where` and `add_join`, each removable, and each checked for its aliases as it is added (D118). The box shows **JOIN** then **WHERE**, each a heading with its lines and a Remove beside each; under each, **Add JOIN** or **Add WHERE** opens its form in place with **Add** and **Cancel**, and adding closes it, a refusal keeping it open with why. A form's explanation shows only while it is open (D168).

- **Table Groups** (D134), after Fact Tables: **Add group** (a name), and under each group its tables with **Remove table from group**, **Add table to group** (offering only tables in no group; a table in another group is removed from there first), **Remove group**, and **Save as recipe set** (D135). The note says tables in no group run together, as one group, after the groups. Each fact table says its group. Renaming or removing a table keeps the groups true; **Duplicate** puts the copy in the original's group, right after it (D138).
- **Recipe sets** (D135). Add a fact table offers each set in `recipes.yaml`; adding one checks it whole, then puts in its tables and a group named for it. **Save as recipe set** writes the group's tables to `recipe_sets:` in Fact Tables order: a prefabricated table as the template has it (its bindings to its siblings, its variables and filters), a table built from the dictionary as a new recipe in the same write, since a set names recipes. A name already in `recipes.yaml` is refused, and the file is left unchanged.

The **table builder**, drawn in place (D110), builds a PK or fact table from the dictionary: name, destination, description, granularity, Pull this cycle, and the table its rows come from, which brings every column under an alias of its initials, the table's description and granularity (D121), and its `standard_where` lines as ordinary, removable where lines (D120); the builder knows the columns' types from the dictionary but writes none (D115). Columns sit under a **Columns (n)** header that starts closed and stays as it was left for that table while the draft is open (D125); they are removed, added back, renamed, described and moved by number. A join to another table of the template is picked from lists, with its type and operator, and refused unless the two columns' types match (and says why); a join to a Cosmos table is written out, and refused if it names an alias the table does not define (D118). Where lines are written by column, as a Fact Table's filters are, or typed as SQL, each typed line saying beside it, as it is typed, when it names an alias the table lacks. Joins and where lines sit in one box laid out as a Fact Table's Filters (D168), the open forms also taking a written join or condition. Renaming the alias rewrites the table's own columns, joins and where lines (strings left alone), and refuses an alias a join already has. Add Table (or Save Changes) puts it into the draft, refusing a table with a bad line; Cancel closes the form. A loaded table is edited in place, keeping what the builder does not show (`dedup_keys`, ...).

**Validate** shows the pipeline's steps (Load, Recipes, Variables, Uploads, Columns, Ready for the VM: passed, failed, or pending), then every message: errors red, warnings yellow, pending transfers blue. Choosing one shows its fix; double-clicking it opens the Builder at its section with the entry marked "Validate points here", until a check finds no message pointing there. The status line says how many of each kind, each shown at its field. **Exports** lists the queued projects (Mac only), each as its intake → transfer YAML, with Remove and Add, and **Bundle With Manager** (`dist/bundle.py`) and **Bundle YAMLs only** (`dist/yamls_to_transfer.py`), which build from the queue, empty it, and show the `content_id` until the next build (D122); then the saved intake's pre-YAML, transfer YAML and manifest, or a queued intake's when chosen; an unsaved draft is told to save first. **YAML** shows the draft as it would be saved.

**Save** writes `YAMLs/temp/<project>_intake.yaml` on the Mac (D95), and `YAMLs/temp/<project>_blueprint.yaml` on the VM (D162), never over a file this draft did not open (D85). On the VM, a draft opened from an older copy (a `_transfer.yaml` beside `scope.py`, an intake in `YAMLs/temp/`) moves that copy to `YAMLs/temp/replaced/`; one opened from a run folder's copy leaves it. Relative upload paths are rewritten to reach the same file from there, with `..` where they must (D103), so a transfer YAML opened at the root keeps its uploads. Browse writes a file relative to the draft's folder the same way (D104); a full path only where no relative one exists (another drive). Retired test options are dropped. The draft is read back before it replaces anything, and on the Mac the saved intake joins the bundle queue, once. **Transfer to Run** saves; on the Mac it exports the blueprint to the working folder, on the VM the saved blueprint is the one, and Run loads it and is shown (D94, D162).

The browser UI that came before it (`scripts/yamlmanager.py`) was retired (D112).

------------------------------------------------------------------------

## The Split Folder

``` text
runs/<project>/                the run folder (D142)
  pullmanifest.yaml            its paths are relative to it: pull_files/split/sessions/...
  <project>_transfer.yaml      the template the split was made from
  pull_files/split/
  uploads/                     copied upload files
  sessions/
    <session_id>/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        run.yaml               unbatched
        b1of9-LA-Female.yaml   or one per batch combination
```

A **session** is one PK and everything pulled for it. Its first Cosmos connection does setup, uploads and the PK; each table group then has a connection of its own (Table Groups, below), because global temps die with the connection that made them. A multiplier produces one session per multiplied PK, and under `Dual` each has an `_sp` twin; otherwise there is one. Under `Dual` every SneakPeek session runs first, then every Cosmos one, cases before controls within each (D65), so the smaller database gives a quick round through every phase first.

Each cohort records the session that builds it as `session_pk`: its multiplier group's PK (or the uploaded PK). A session's runs hold only its own cohorts, so every cohort is built once, joined to its own session's PK. With IBDType x Race x `Dual`, that is 8 sessions of one `OtherHospitalizations` each.

Every phase document carries `temp_prefix` (Naming), and the manifest's `project` records it too.

Every session has the same routine, batched or not:

| Phase | Does |
|------------------------------------|------------------------------------|
| `setup` | Creates the Projects destination tables (drop and create; kept on a resume) |
| `upload_cohorts` | Lands each upload in Projects once per pull, and loads its Cosmos temp where the session reads it (D61) |
| `pk` | Builds the PK table and copies it to Projects, or registers an uploaded one |
| runs | Pull the remaining cohorts, one run per batch combination |

Each YAML is standalone-valid: it repeats the project metadata, drops the `multipliers` and `batching` instructions so nothing expands twice, and carries a `pull_context` saying where it belongs. The metadata appears once, flat at the top level (`project_db`, `smallset`, …; `run_vars` merged into `vars`): the groups a template may use (`cosmos_vars`, `run_vars`, `test_options`, `project_vars`) are lifted out, not kept beside their copies. The runtime reads the top level, and falls back to a `test_options` group for splits made before this.

``` yaml
pull_context:
  session_id: Patients
  phase: run
  cohort: Patients
  pk_table: Patients
  run_id: Patients__run
  pk_source: {kind: generated, table: Patients}
```

An upload cohort marked `type: pk` (at most one) becomes the session's PK source: `upload_cohorts` uploads it, and `pk` registers it (`pk_source: {kind: uploaded_cohort, upload_name, table, key_columns}`) instead of building one. Its Projects copy is the upload's own (`upload_<dest>`, What A Session Does), which its uniqueness check, batches and chunks read, as a generated PK's do. Its session carries the template's batching.

### Table Groups

A **table group** (D134) is a named set of a template's fact tables, written as `table_groups: [{name: Meds, tables: [MedicationOrders, MedicationAdministrations]}]`. The levels are session (one PK) → group (one Cosmos connection) → batch → chunk. The split makes each batch once per group: a run per group and batch, holding only that group's tables and recording its `group` in the manifest and its `pull_context`, groups in the order `table_groups` lists them, then the tables in no group, which run as one group under the plain run name. A template without groups splits as before. Groups are outside batches: every chunk of Meds, then every chunk of Visits.

At Execute, the first group shares the connection that built the PK. When the group changes, the connection closes and a new one opens, mints its own epoch and captures `@@SERVERNAME`, refills the PK temp from the PK's Projects copy, and loads the uploads the group's tables read; the session's temp prefix is kept (D138). Progress names the group (`run Meds Female started`). A failed group is a failed run: `--retry-failed` pulls it alone, and finished groups are skipped.

### Multipliers

A `during_build` level sets variables (`ICD_Value: [K50.%]`), so each level builds its own cohorts. Each multiplied table records its levels as `multiplier_levels` (multiplier, strat, stage, and a `during_build` level's vars), which `contents.md` reads (D73).

**Variables.** A table's variables come from, last winning: the template's `vars`, the uploads, the automatic ones (`prefix`, `PKTable`), its PK's own `vars` (the PK of its multiplier group), then its own `vars`, which include its multiplier levels' (D78). So a table that does not set `ICD_Value` takes the one its patients were chosen by; one it sets itself wins. A `split_after_build` level splits the PK by one of its columns (D59): its condition joins that PK's `where`, written on the PK's own source for the column (`p.FirstRace LIKE 'Black%'`), so each level still builds its own PK in its own session. Levels multiply: IBDType × Race × `Dual` is 8 sessions.

A level with `role: control` and `row_mult: n` is a control, sampled against its case: the multiplier's one other level, in the same group and database (`whitePatients` against `blackPatients`, `_sp` against `_sp`). The PK records it as `split_after_build[].matched_to`, and the manifest puts every case session before any control. Once the control's PK lands in Projects, each batch keeps n times its case's rows in that batch, the first in hash order of the key (D60), and the rest are deleted, so the Projects PK is the sample and every run is drawn from it. The control is matched on the batching columns and nothing else. A batch with too few controls keeps them all, with a warning.

### Batching

Batch dimensions **cross-multiply**. They partition the cohort; they are not alternative slicings of it:

``` yaml
batching:
  - state: {values: [LA, MS]}
  - sex                          # Female, Male
  - chunk: 2000
```

The unit of multiplication is the **bucket**. Listing values always adds a catch-all bucket for every other value (`<dimension>-other`), so the batches together are the whole PK and batching never drops a row (D76). The catch-all records the values it `excludes`, and its predicate includes `IS NULL`, because `NOT IN` never matches NULL. `include_other` is retired: it warns and does nothing.

So `state × sex` is nine runs, `b1of9-LA-Female` through `b9of9-state-other-sex-other` (D53). The number makes every label unique, so two combinations can never share one; `A B` and `A-B` both clean to `A-B` and are told apart by it. A run with only `chunk:` is `b1of1`.

A `column_values` batch names the PK column it splits on as `required_column` (D80): a PK without it is an error (`missing_batch_column`) naming the PK's columns, and the old `column` is refused with the fix. Inside the split, and in the manifest's `batch.dimensions`, it is still `column`.

`separate_parquets: true` on a batch makes Artifacts write one parquet per value of it instead of one per table (D72). It is off unless set (D77): no batching recipe sets it. The split records it on each dimension as `separate: true`.

| Dimension | Buckets known | Expanded by | Recorded as |
|------------------|------------------|------------------|------------------|
| `column_values` with listed values | at plan time | YAML Manager | `batch.dimensions`, one run each, stable `run_id` |
| `column_values` with no values (or `values: all`) | at run time | Pullmanager | `batch.runtime` |
| `row_chunk` | at run time | Pullmanager | `batch.runtime` |

So `chunk: 2000` subdivides each combination rather than joining the product. The chunks run inside their run, not as manifest nodes (D53): Pullmanager counts the batch's PK rows and pulls `ceil(rows / 2000)` chunks in turn, showing progress as `c2of3` on the run.

A batch that lists no values batches by every value the PK has (D82), found the same way, inside the run: once the PK is in Projects, the run selects the distinct values of the column among its own PK rows (`SELECT DISTINCT` under the run's other dimensions), NULL included as a value of its own (`IS NULL`), and pulls each in turn, chunked if the batch is, all under the run's own `_batch` label. Progress shows as `v3of51 (LA)`, and `values_found` on the run. As with chunks, a failure fails the run and a retry redoes all of it. The preview notes it. Separate parquets cannot apply to such a batch (`separate_values_all`): the tables other than the PK do not carry the column to split by.

Batch membership is deterministic. A values bucket is a predicate, and a run's buckets combine with `AND`, so the order of the dimensions changes only the label (`LA-Female` or `Female-LA`), never the rows. A chunk is `ORDER BY` the PK's key, verified unique after the PK phase; the PK's key is its first `dedup_keys` set, else its `key_column(s)`, everywhere it is used (D69). Both read the Projects copy of the PK (D19), which a Cosmos refresh does not move.

------------------------------------------------------------------------

## The Manifest

`pullmanifest.yaml` is the plan and, once Pullmanager starts, the status record. YAML Manager writes it with every status `pending`.

``` yaml
manifest_version: 1              # Pullmanager refuses anything else
project:
  name: <str>
  project_folder: <str>
  project_db: <str>              # e.g. PROJECTD33A929; rewritten by the first Execute (D164)
  temp_prefix: <str>             # e.g. tesrun (D50)
  created_by: yamlmanager
  table_prefix: <str>            # e.g. infrsv; every Projects table's (D163)
source:
  template: <path>
  template_sha256: <hex>         # the blueprint as split (D162)
  recipes: <path|null>           # null for a blueprint, which adds transfer: {...}
sessions:
  - session_id: <stable id>
    cohort: <str>
    pk_table: <str|null>
    multiplier: {...}            # omitted when not multiplied
    status: <status>             # derived, see below
    phases:
      setup:          <node>
      upload_cohorts: <node>
      pk:             <node + pk_source>
    runs:                        # always at least one
      - <node + run_id, and batch when batched>
```

Every node:

``` yaml
yaml: <path, relative to the manifest's directory>
status: <status>
started_at: <iso8601|null>
finished_at: <iso8601|null>
rows: <int|null>
outputs: {}                      # untyped; Pullmanager fills it
error: <null | {message, detail}>
```

A batched run adds:

``` yaml
batch:
  name: b1of4-LA-Female
  dimensions:
    - {name: state, kind: column_values, column: StateOrProvinceAbbreviation, value: LA}
    - {name: sex,   kind: column_values, column: Sex, value: Female}
  runtime:
    - {name: chunk, kind: row_chunk, rows_per_batch: 2000, applies_to: PKTable}
```

A catch-all bucket carries `is_other: true` and `excludes: [...]` in place of `value`. Absent optionals are omitted, not written as `null`.

### Fields Pullmanager Adds

``` yaml
# on a node
duration: {seconds: 312, display: "5m 12s"}   # on finish or fail; cleared on retry
note: <str>                                   # why skipped or blocked; never in `error`
epoch: <str>                                  # the connection it completed under

# on a run
outputs: {chunk: c3of3, batch_pk_rows_total: 4500, batch_pk_rows: 500, batch: ...}

# on a step while it runs (D166): the table it is building; cleared when it ends
outputs: {in_flight: {table: OtherHospitalizations, since: "11:01"}}

# on a run, and the pk phase (D137): each table's rows as Cosmos built them,
# added across chunks and values; saved as each table lands, so a failed run
# keeps the ones it finished. A run has no `rows` of its own.
outputs: {table_rows: {HospitalAdmissionFact: 5000, RSVPatients: 5000}}

# on a session
runtime:
  epoch: "20260922T140000-3f2a9c11"
  opened_at: "2026-09-22T14:00:00-05:00"
  linked_server: et4003vpdsql032
  temp_prefix: tesrun                          # tesrun2 if another pull held tesrun (D50)
  cosmos_created: {Cosmos: "2026-09-17T19:34:56.450"}

# on the pk phase of a generated PK (D87)
outputs: {pk_parquet: {file: cosmos_parquets/Patients.parquet, rows: 4242}}

# on the pk phase: the rows Cosmos built, before a control is sampled (D152)
outputs: {cosmos_rows: 1232900}

# on a run, and the pk phase (D157): each table's rows per join key, over
# what that step landed; the PK by its own key
outputs: {per_key: {OtherDiagnoses_sp: {key: PatientDurableKey, keys: 9275,
          median: 93, p90: 269, max: 1160}}}

# on the pk phase of a sampled control (D59)
outputs: {control_sample: {matched_to: CrohnsblackPatients, row_mult: 4.0,
          per_batch: {b1of3-Male: {cases: 812, controls: 3248}}}}

# on an upload phase (D61)
outputs: {uploads: {IBD_Meds: {projects: landed, cosmos: not read in this session}}}

# on the manifest
cosmos_refresh: {Cosmos: "2026-09-17T19:34:56.450", Cosmos_SneakPeek: "..."}   # D51
uploads_landed: {IBD_Meds: {table: PROJECTD93A5E7.dbo.upload_IBD_Meds, rows: 1204,
                 landed_at: "...", by_session: CrohnsblackPatients}}            # D61
last_execute: {started_at: "...", ended_at: "...", exit_code: 0,
               how: finished}   # or finished with errors, stopped by user,
                                # stopped with errors; ended_at empty if killed (D140)
database_choice: {database: PROJECTD125423, why: "...", chosen_at: "...",
                  free: {PROJECTD93A5E7: 0.0 GB free, ...}}                    # D164
tables_dropped: {at: "...", database: PROJECTD125423, tables: [infrsv_EDVisits, ...]}  # D165
```

### Rules

- **Phase names are closed**: `setup`, `upload_cohorts`, `pk`, in that order, which is execution order. Any other key raises.
- **Order comes from the manifest**, never from folder names. `yaml` paths resolve against the manifest's directory.
- **Unknown keys survive.** The loaded mapping is mutated in place and written back whole, so a newer YAML Manager can add fields without breaking an older bundle.
- **Writes are atomic**: temp file, then rename. Windows refuses the rename while another program has the manifest open, so it is tried again, quickly (0.1, 0.25, 0.5, 1 and 2 seconds), then once a minute for five minutes, each wait printed (`pullmanifest.yaml is busy (Access is denied); trying again in 60s, 2 of 5`), then the save fails, naming the file and the likely holders (D153). The backup's copy waits the same way.
- **Readers let it be replaced** (D154): on Windows every YAML the runtime reads is opened with `FILE_SHARE_DELETE` (through `ctypes`), so reading a manifest never blocks its save; elsewhere, a plain read.

### Status

| Status    | Meaning                                      |
|-----------|----------------------------------------------|
| `pending` | Planned, not started                         |
| `running` | Executing, or interrupted while executing    |
| `done`    | Completed                                    |
| `failed`  | Attempted and failed                         |
| `skipped` | Deliberately not run, e.g. no upload cohorts |
| `blocked` | Not run because something upstream failed    |

`done` and `skipped` are *settled*. Session status is recomputed from its children on every save: any `failed` → `failed`; any `running` → `running`; any `blocked` → `blocked`; all `skipped` → `skipped`; all settled → `done`; some settled → `running`; otherwise `pending`.

`running` cannot say whether a pull is running now. The lock can (D67): while `--execute` runs it holds `pullmanifest.lock` beside the manifest (process id, machine, start time, the log it writes, and a heartbeat a background thread rewrites every 30 seconds). It is removed when the pull ends; one with no heartbeat for 2 minutes is stale, and the next Execute takes it over, saying so. The process id is shown, never checked: on Windows `os.kill(pid, 0)` would end the process. While a lock is live, a second `--execute` of that pull, `--export-split` of its split, and `--artifacts` all refuse, before touching anything.

------------------------------------------------------------------------

## Execution: Pullmanager

### Naming

`naming.py`:

| Thing | Rule | Example |
|------------------------|------------------------|------------------------|
| Cosmos global temp | `##<prefix>_<dest>`; a `JVM_` prefix on the dest is stripped first, never doubled | `##tesrun_PKTable` |
| Projects staging | `#Local_<dest>` | `#Local_PKTable` |
| Projects copy of an upload | `<project_db>.dbo.<table prefix>_upload_<dest>` (D54, D163) | `PROJECTD33A929.dbo.ibdanc_upload_HospitalICDCodes` |
| Projects destination | `<project_db>.dbo.<table prefix>_<dest>`, always fully qualified (D163) | `PROJECTD33A929.dbo.ibdanc_PKTable` |

The prefix is per project (D50): `temp_prefix` from the template, else the first (up to) three letters of each word of `project_folder`, lower-cased (`IBD Ancestry` is `ibdanc`, `Test Run` is `tesrun`, blank is `pull`). A phase document with no `temp_prefix`, written before D50, keeps `JVM`.

Every table a pull makes in Projects carries its **table prefix** (D163): each destination, the PK's copy, each upload's copy, the case a control is sampled against, and what Artifacts reads back (`naming.projects_table`). It is fixed at the split and never changes, unlike the temp prefix, which a session renumbers. Parquets, `contents.md` and the manifest's table names keep the plain name. A pull split before D163 has no table prefix and keeps unprefixed names.

Global temps are instance-wide, so two pulls that both make `Patients` would collide. When a session opens it asks, for each temp it will create, whether it already exists (`OBJECT_ID('tempdb..##tesrun_Patients')`). One that does belongs to a pull running now, since a global temp lives only as long as its connection. The session then **does not drop it**: it numbers its prefix (`tesrun2`, `tesrun3`, ... up to 99) until none of its names are taken, and rewrites every statement it sends to use that. The chosen prefix is recorded in `session.runtime.temp_prefix`, with a warning when it changed. A dry run shows the planned names. If the check itself cannot run, the session warns and uses the planned prefix.

### Sessions, Epochs And Staleness

One Cosmos connection is held open from `setup` through the last run of a session, because every global temp dies with it. Opening it mints an **epoch** and captures `SELECT @@SERVERNAME` into `session.runtime`. That instance name (`et4003vpdsql032`, not the `COSMOS` alias) is what Projects-side `OPENQUERY` must target, and it **changes on every connection**, so it is always overwritten, never cached or reused.

| Output | Lives in | Survives the connection |
|----|----|----|
| `##<prefix>_<dest>`, uploaded temps | Cosmos | No |
| `#Local_<dest>` | Projects connection | No |
| `<project_db>.dbo.<dest>`, `upload_<dest>` | Projects database | Yes |

So `done` means "completed once", not "still exists". `is_stale()` is true for a node completed under an earlier epoch: its **server-side** output is gone. A manifest with no epochs is never stale. What runs next is decided per session (Running Again), not from staleness.

### What A Session Does

When the session opens, before anything runs: capture `@@SERVERNAME`, check the Cosmos refresh date (D51, Running Again), and choose the temp prefix (Naming).

1.  **Setup.** First the column check (D156): every plain `alias.Column` source in the session's cohorts is matched to the Cosmos table its alias names in `filter.from` and `filter.join` (in the database the cohort reads, `COSMOS_SneakPeek` for a SneakPeek cohort), and each table's columns are read once (`sys.columns`). A source not there fails `setup`, naming each column, its cohort and table, and near matches (`AdmissionDateKey_X`), so the session is blocked before anything is built and the other sessions go on. A table whose columns cannot be read (not found, or not visible to the login) is a warning. Expressions and temps are not checked. Then drop and create every Projects destination, once; runs append. Resuming, keep them and create only missing ones (D52). Every destination a run fills has a `_batch NVARCHAR(200) NOT NULL` column holding the run's batch label (`all` for an unbatched run). The PK's own copy has none, since batches are selected from it.

2.  **Uploads** (D54, D61). A non-PK upload lands in Projects once per pull, as `upload_<dest>` with its types (Uploads, below), committed, by the first session to reach it; the manifest records it (`uploads_landed`) and later sessions use that copy. Its Cosmos temp is created with the copy's types, read back from `INFORMATION_SCHEMA`, and filled from the copy through the client (there is no linked server from Cosmos back to Projects), but only in a session whose cohorts read it. An uploaded PK lands and goes up in every session. Resuming, the copies are kept and the files are not read; a copy that is missing stops the phase, pointing at `--repull`.

3.  **PK.** Build `##<prefix>_<pk>` and copy it to Projects (an uploaded PK already has its `upload_` copy), then verify uniqueness against the copy (`COUNT(*)` against a count of `SELECT DISTINCT keys`, the key by D69; a PK with no key warns instead, naming `dedup_keys` and `key_column`). The rows Cosmos built are kept as `cosmos_rows` (D152). A sampled control is cut to its sample first (Multipliers). Then a generated PK is written whole to parquet, where Artifacts puts it (`runs/<project>/<cosmos|sneakpeek>_parquets/<pk>.parquet`), before any run starts (D87); a failure to write it warns and the pull goes on. An uploaded PK is a file already and is not written. Not rerun on a resume.

4.  **Runs.** For a batched run, the PK temp is emptied and refilled with that batch's whole PK rows, selected from the **Projects copy** with a parameterized predicate, then uploaded. The cohort SQL runs unchanged: it only ever joins the PK temp. On a resume an unbatched run refills it with the whole Projects copy the same way, as does an unbatched sampled control, whose temp still holds every row the PK query built. Each run then:

    - deletes its own label's rows from each destination (`DELETE ... WHERE _batch = 'b2of4-LA-Male'`), a separate block, so a run that failed after landing some rows lands them exactly once when retried;
    - for each cohort in turn (D55): builds its Cosmos temp and commits, then transfers it (`OPENQUERY` into `#Local_<dest>`, then `INSERT` into the destination, adding the `_batch` label) and commits, before the next cohort is built. A failure loses at most the cohort in flight.

    A chunked run clears once, then for each chunk refills the PK temp with `ORDER BY <keys> OFFSET/FETCH` over the Projects copy, rebuilds the cohort temps and lands them. A failed chunk fails the run; a retry redoes all of it.

**Progress** (D136). Each step prints a line when it starts and when it ends, flushed at once, so the console, the log and the Pull Log tab show it while it runs. Every line starts with the time:

``` text
=== Patients ===
  14:34:02  connected: Cosmos on et4003vpdsql032, Projects PROJECTD33A929
  14:34:02  uploads started
  14:34:02    HospitalICDCodes: into Projects started
  14:34:02    HospitalICDCodes: 2 rows into Projects in 0s
  14:34:02    HospitalICDCodes: into Cosmos started
  14:34:02    HospitalICDCodes: 3 rows into Cosmos in 0s
  14:34:02  uploads done in 0s, 3 rows
  14:34:03  run Female started
  14:34:03    c1of3 Patients: 2,000 PK rows into Cosmos in 0s
  14:34:03    c1of3 OtherHospitalizations started
  14:34:03    c1of3 OtherHospitalizations: 10 rows (Cosmos 0s, into Projects 0s)
  14:34:03  run Female done in 0s, 10 rows
```

A phase says started, then done (its time and rows); a run says done with its time and how many tables it landed, never one row total, since each table has said its own (D137). Either can say FAILED (its time and error), blocked or skipped (why). An upload times its two legs apart; a table gives its rows, the time Cosmos took to build it and the time it took to land. A chunk or a value (`v2of5 (LA)`) names itself at the start of its lines. A table's start line with nothing after it is the table in flight.

The summary at the session's end lists, under each finished phase and run, every table it landed with its rows, and under a failed run the tables it landed before failing (a retry pulls them again). The summary command (`pullmanager.py <manifest>`) and the Status tab list them the same way, from the manifest's `table_rows` (D137).

**Rows per join key** (D157). When a run finishes, each table it landed is grouped by its join column, over the run's own `_batch` rows, and the number of keys, the median, the 90th percentile and the maximum rows per key are recorded as the run's `per_key`; the PK the same, by its own key, once it lands. The join column is the landed column whose source is this table's side of the first equality in the cohort's first `JOIN` (`pk.PatientDurableKey = def.PatientDurableKey` gives the column sourced from `def.PatientDurableKey`). A table deduplicated to one row per key shows 1, 1, 1. A failed measurement is a warning. The session's summary lists them as a note after the widths; Status shows them as columns.

**A failed statement fails its step** (D151). A block is several statements in one batch, and the driver raises an error in any statement after the first only when the next result is asked for; every result is asked for, and nothing there is caught, so a failed `INSERT` fails its step with the server's message. Every landing ends by reporting its Projects row count, and one that reports none is an error, not a comparison skipped. Before this, IBD_Ancestry's white controls (1,232,900 built) landed nothing and finished `done` with 0 rows and no warning. Every server message, from every statement, goes to the execute log only, as `server: [<block>] <message>`.

After each run the Cosmos and Projects row counts are compared, counting only this run's `_batch` rows on the Projects side (a chunked run compares totals), and a mismatch warns. Counts past 80,000,000 warn. The widest value of each staged column is measured and reported, not applied (D34): at the end of each session, after its warnings, one table of each text column's declared type and widest value across all the session's batches, as a note (D70). A unit that fails rolls both connections back. Nothing runs in parallel: sessions, runs, chunks and cohorts go one after another.

### Uploads

Every upload lands in Projects as `upload_<dest>` before it goes near Cosmos (D54):

| Upload | Lands in Projects by |
|------------------------------------|------------------------------------|
| parquet | Read with `pyarrow`, declared columns converted, renamed and dropped columns applied (D98), then bound in with `fast_executemany` into a table created with its types |
| dbtable | `SELECT * INTO upload_<dest> FROM <source_table>`, server-side, types and all |
| csv | Refused: the split converts CSVs; one reaching Pullmanager came from an older split. Export it again |

Parquet types land as: 64-bit integers `BIGINT`, 32-bit `INT`, 8/16-bit `SMALLINT`, doubles `FLOAT`, decimals `DECIMAL(p,s)`, booleans `BIT`, dates `DATE`, timestamps `DATETIME2(7)` (a time zone is converted to UTC, with a note), text `NVARCHAR(longest + 50)`, or `NVARCHAR(MAX)` past 4000. A binary column is refused. A declared type overrides, and a value that does not fit it fails, naming the column.

**The copy is the source.** Once landed, the Cosmos temp, an uploaded PK's batches and every resume or retry read the copy, never the file. So a file changed after a pull started is not seen until `--repull`, which clears `uploads_landed` and lands every upload from its file again; that is the step to take after someone sends a corrected file.

Rows travel by parameter binding with `fast_executemany`, chunked:

``` python
cursor.fast_executemany = True
cursor.executemany("INSERT INTO ##tesrun_ClientPK (PatientDurableKey) VALUES (?)", rows)
```

Not a literal `INSERT ... VALUES` list, which T-SQL caps at 1000 rows. Binding also removes quote escaping and maps `None` to `NULL`. Chunked because the driver allocates buffers from declared width times batch size.

### Failure Policy

| Failure   | Effect                                          |
|-----------|-------------------------------------------------|
| A run     | Siblings continue: batches are disjoint appends |
| A phase   | Everything after it in the session is `blocked` |
| A session | The next session still runs                     |

One night produces one list of every failure. A partial session still rolls up to `failed`, so a partial table cannot read as complete.

### Running Again

`--execute` first connects to Cosmos and reads `SELECT name, create_date FROM sys.databases WHERE name LIKE 'Cosmos%'` (D51). The manifest records the value for each database its cohorts read, to the millisecond. If one has changed since the last run, Cosmos was refreshed: it says so, and **every session starts over**, finished ones included. If the value cannot be read, it warns that a refresh cannot be detected and carries on. A session that finds a different value when it opens, because Cosmos was refreshed during the run, stops with a message to run again.

Then, per session (D52):

| Session | Next `--execute` |
|------------------------------------|------------------------------------|
| PK phase not done | Starts over: setup drops the destinations, everything runs |
| PK done, some runs not done | Resumes: destinations kept, uploads replayed, PK query not rerun, only unfinished runs pulled |
| PK done, every run done | Skipped without connecting: its tables are complete |

- `--retry-failed` reopens `failed` work. Without it, failed work is excluded and the output says so; a session with only failures left is skipped.
- A run left `running` by a crash or Stop is interrupted: it is pulled again, its rows cleared first.
- `--repull-session <name>`, repeatable, starts only those sessions over, as a new pull would: their steps pending and outputs cleared, their tables dropped and made again by `setup`; the other sessions, and uploads already landed (D61), are kept (D158). A case brings its controls, which were sampled against its counts, and says so. `all` means `--repull`; a name the pull does not have is an error listing its sessions, before anything changes.
- `--repull` starts every session over, finished work included, and lands every upload from its file again. A retry never re-reads an upload file (Uploads: the copy is the source), so after a changed file, `--repull`.
- The summary (`pullmanager.py <manifest>`) and the dry run say, per session, what the next `--execute` will do.

`--resume-partial` is gone (D52, replacing D46).

### Connections

``` text
Driver={ODBC Driver 17 for SQL Server};Server=tcp:COSMOS;Database=COSMOS;Trusted_Connection=yes;
Driver={ODBC Driver 17 for SQL Server};Server=tcp:PROJECTS;Database=<project_db>;Trusted_Connection=yes;
```

- Driver 17 defaults to `Encrypt=no`, so no certificate handling. Login timeout 10s; no query timeout by default.
- Scripts are split on lines equal to `GO`, and every result set is drained with `nextset()`. An error `nextset()` or a fetch raises fails the batch (D151): the driver reports a later statement's error only there.
- A connection that is refused is reported in one line, naming the server and database, with a hint (for "cannot open database" or "login failed": check `project_db`), and the next session still gets its chance. It used to escape as a traceback.
- `cursor.messages` is read after every statement, on success and failure alike. That is what surfaces the inner error of a failed `OPENQUERY`; on success they go to the execute log (D151).
- `autocommit=False`, so the `BEGIN/COMMIT TRANSACTION` inside a transfer block nests inside the driver's own transaction rather than committing on its own. Pullmanager commits after every block (D55): each cohort's rows, each chunk's, and each upload's copy are saved before the next is pulled, and a transaction and its locks last one cohort. A unit that fails is rolled back.
- Telemetry is read from result sets with declared columns, tied to manifest ids. No SQL is ever selected by searching its text.

### Space In The Projects Database

A project database has a size cap: PROJECTD93A5E7's data file and its log each stop at 20,000 MB, separately. A pull split before D163 is never dropped by itself, so its tables gather until the data file is full, and then a pull fails at `setup`, before anything lands, with error 1105 ("the 'PRIMARY' filegroup is full"); the next Execute, after space is freed, starts it cleanly. A dropped table's space is free for reuse, but the file does not shrink. The log is under `SIMPLE` recovery, so it reuses its space by itself, except behind a transaction left open (`log_reuse_wait_desc` `ACTIVE_TRANSACTION`), such as an SSMS tab's, until that transaction ends.

**A database per pull** (D164). On a pull's first Execute (no database chosen yet, every step pending), before anything is built, each database in `DEFAULT_PROJECTS_DATABASES` (D171) is asked for its data files' room to their cap (`sys.database_files`; a file with no cap counts as room). One that refuses the login is skipped and said. Among those with more than 6 GB free it takes the blueprint's own `project_db`, if no other unfinished pull uses it; else the roomiest no other unfinished pull uses; else, stacking, the roomiest. An unfinished pull is another under the runs folder not finished and packaged, that has run or been given a database. With none over 6 GB, Execute stops, building nothing, listing each. The choice is written into the manifest (`project.project_db`, and `database_choice` with why, when, and each database's space) and every phase document, and never changes (D52).

**Drops after packaging** (D165). When a clean Execute has packaged its pull (D141), it drops every table the pull made (each destination, the PK's copy, each upload's copy, by their prefixed names, read from the phase documents, never one without the prefix), provided the pull has a table prefix and the run scan (D152) finds nothing wrong with it. Otherwise nothing is dropped and the log says why. The manifest records `tables_dropped` (when, the database, the tables), and Run, choosing such a pull, says only Re-pull everything pulls it again. Artifacts run by hand drops nothing.

`utils/manager/clear_projects_db.py` (D133, D148) is a window onto one project database: every table with its rows and size, largest first; each file's use against its cap; and what the log is waiting on, with what to do. It drops one table (double-click), the selected ones, or all of them (after the database's name is typed), each committed alone and the view refreshed after; a foreign key on or pointing at a chosen table is dropped first. It takes only a name starting `PROJECTD`. A table another session has locked is reported as blocked after 30 seconds, naming SSMS and a running pull as the usual holders, and the rest still drop. Open transactions runs `DBCC OPENTRAN` (db_owner only; otherwise it says to try `SELECT @@TRANCOUNT` in each SSMS tab), and Free log a `CHECKPOINT`. Server, database and driver start from the `PULLMANAGER_*` settings, as Pullmanager's connections do.

### The Run Scan

`--scan-runs`, and **Scan runs** in Run (D152), reads every pull under the runs folder, without a database, and compares what each manifest says it built with what it packaged. It writes `runs/run_scan.yaml` with only what did not check out, per pull, short enough to copy off the VM by screenshot:

``` yaml
# run scan, 2026-10-01 09:12, bundle 1b628fc3: 6 pulls, 4 problems
IBD_Ancestry:
  short_controls:
    UCwhitePatients:
      - b1of3-Female kept 0 for 88,776 cases (4x)
  empty:
    - UCwhitePatients
  lost_rows:
    OtherDiagnoses: built 1,150,870, parquets 1,000,000
```

- **lost_rows**: a table whose parquets hold fewer rows than the manifest's `table_rows` add up to; or a PK that landed fewer than its `cosmos_rows` (D152's own record, so only pulls since).
- **empty**: a finished table with 0 rows.
- **short_controls**: a sampled control that kept fewer than `row_mult` times its case in a batch.
- **no_count**: a finished run that recorded no row count for a table its document makes.
- **no_parquet**: a finished table's parquet missing from a packaged pull (Artifacts has written `contents.md`). Before Artifacts, `not_packaged` says so once and only the parquets already there (the PK's, D87) are compared.

With nothing wrong the file is one line, `every pull checks out`. It exits 1 when something did not check out.

### The Dictionary Audit

`--audit-dictionary`, and **Audit dictionary** in Run (D155), asks Cosmos (the default database, `COSMOS`) for every dictionary table's columns, with each one's type and nullability, one read-only `sys.columns` query each. It writes `runs/dictionary_audit.yaml` with only what is wrong: what the dictionary lists and Cosmos lacks, and each column whose type or nullability differs from Cosmos's (D161):

``` yaml
# dictionary audit, 2026-10-01 09:12, bundle 1b628fc3, database COSMOS: 30 tables, 2 wrong
tables_not_found:  # the dictionary names them; Cosmos has no such table, or this login cannot see it
  - SomeTable
columns_not_in_cosmos:  # remove these from the dictionary
  MedicationDispenseFact: [ReadyToDispenseDateKey]
  EncounterFact: [FooKey]  # near: FooKey -> FooKey_X
types_wrong:  # Cosmos's type; dictionary-fix writes it in
  MedicationDispenseFact: {FillNumber: nvarchar(50), MinimumDose_X: "numeric(19,4)"}
nullable_wrong:  # Cosmos's nullability; dictionary-fix writes it in
  VitalsFact: {DateKey: false, Temperature: true}
```

Names compare without case. Columns Cosmos has that the dictionary lacks need no fix and are left out. Cosmos's type is written as a page writes it: `nvarchar(300)` (from its 600 bytes), `numeric(19,4)`, `nvarchar(max)`, and `datetime2` without its default scale. A dictionary type matches when its own words, before any annotation, are Cosmos's (`bigint (DateKey; foreign key to DateDim)` is `bigint`); an abstract type (`string`, `integer`) never matches, so the audit names every one left. A column with no `nullable` counts as nullable, as a pull reads it. Each table's wrong columns are one line, with a value holding a comma quoted, so the file stays short enough to screenshot. With nothing wrong the file is one line, `the dictionary matches Cosmos`.

On the Mac, `python3 scope.py dictionary-fix <file>` takes that file as transcribed and changes `reference/datadictionary.yaml` by its lines, so every other line and comment stays as written. It removes each listed column and table (a table's own comment lines above it go with it). It writes in each wrong type, keeping the annotation (`integer (DateKey)` becomes `bigint (DateKey)`), and each wrong `nullable:`, adding the line where a column has none. Then it lists every YAML under `YAMLs/` that names a changed column (`alias.Column`) or a removed table, by what to do: a removed one to fix and export again; a retyped one to export again, since its types come from the dictionary at export; a column whose nullability changed, to check the YAML's own `nullable:`, which the Author view copied when the column was added (a `false` there filters out the rows where it is null). A same-named column of another table is listed too. A name the dictionary lacks is said, not an error.

### Command Line

On the VM each command is typed as `python scope.py ...` in the working folder (D123); `scope.py` passes it to the runtime's `pullmanager.py`, which is what is written below.

``` bash
pullmanager.py runs/<project>/pullmanifest.yaml                    # summarize
pullmanager.py --dry-run runs/<project>/pullmanifest.yaml [--out-dir runs/<project>/pull_files/sql] [-v] [--all] [--retry-failed] [--repull]
pullmanager.py --execute <project> [--retry-failed] [--repull] [--env FILE] [--keep-open]
pullmanager.py --execute                                           # lists the pulls; runs nothing
pullmanager.py --artifacts <project>                               # package a finished pull (D72)
pullmanager.py --running                                           # every pull, and which are executing (D67)
pullmanager.py --backup                                            # back up every pull, one executing skipped (D149)
pullmanager.py --scan-runs                                         # what every pull built against what it packaged (D152)
pullmanager.py --audit-dictionary [--env FILE]                     # the dictionary against Cosmos's columns (D155)
pullmanager.py --execute <project> --repull-session <name> [...]   # start only these sessions over (D158)
pullmanager.py                                                     # the app: Author and Run (D63, D93)
pullmanager.py --gui                                               # the same
pullmanager.py --tdd [module]
```

`--execute` and `--artifacts` take a project's name (D66): `IBD_Ancestry`, `"IBD Ancestry"` or `IBD_Ancestry_transfer.yaml` all mean `runs/IBD_Ancestry/pullmanifest.yaml`, looked for under the working directory, then beside `scope.py`; the folder's own spelling is used whatever the case typed. A manifest path still works; one that is not there says so. A name with no pull lists the pulls there are. With no name, each lists every pull with its state (not started, executing, finished, or finished with errors, stopped by user or stopped with errors, with its sessions done of the total; D140) and the command for it, and runs nothing.

A dry run (the launcher's **Preview SQL**) renders every SQL block without touching a database or the manifest, listing why each unit is included and what was excluded, and what each session will do next. It ends with one statement (D71):

``` text
Preview finished: 48 unit(s), 112 SQL block(s), 0 errors, 3 note(s). Nothing was pulled.
SQL written to runs\IBD_Ancestry\sql for reading; Execute does not need it.
To pull it: press Execute, or in a terminal in <working folder> run:
    python scope.py --execute IBD_Ancestry
```

**Execute** writes everything it prints to `runs/<project>/execute-<date>-<time>.log` as well, flushed line by line, however it is started (D68); a refused Execute writes one too, saying why. As it starts, the logs already there move into `older_logs/`, so the one beside the manifest is the latest (D142). In a Windows console it turns QuickEdit off, so a click in the window cannot pause the pull at its next line (D143). It writes `last_execute` into the manifest as it starts and ends (D140). When it pulled something and ends with every session done and nothing failed, it runs Artifacts itself, in the same window and log, still holding the lock; a pull with a failure says it was not packaged and why. If Artifacts fails, the exit code stays the pull's, with a warning naming what was not written (D141). Ctrl+C stops it cleanly (exit 130), releasing its lock; what it was working on stays `running`, and the next Execute pulls it again. Each step records its own failures in the manifest; an error no step catches is written to the log with its traceback, and a crash in native code (the ODBC driver, pyarrow) writes where it was (`faulthandler`), so the reason outlives the window. `--keep-open` holds the window at the end: "Safe to close: the pull has finished (exit code N). Type exit and press Enter to close this window." Only `exit` closes it.

### The Launcher: Run

The launcher is the app's **Run** half (D93), in the same window as Author, which hands it the transfer YAML it exports. It is for **running** pulls, chosen from three dropdowns (D126, D140), each filled as it opens: **Running pulls**, every pull executing now with how far it has got (`IBD_Ancestry: executing since 14:03, heartbeat 20s ago (2 of 8 sessions done)`); **Finished and stopped pulls**, every pull that has run and is not executing, as `(finished)`, or `(finished with errors)`, `(stopped by user)` or `(stopped with errors)` with its sessions done (`Infant_RSV  (finished with errors (1 of 2 sessions done, 1 failed))`); and **Start run**, the working blueprints by project that have not run (`Celiac  (not run yet)`, or `(not started)` once split), with Browse beside it: `YAMLs/temp/*_blueprint.yaml`, then a blueprint or `_transfer.yaml` beside `scope.py`, each project once, the first found (D162). A pull is finished once every session is done, however its process ended; stopped by user once Execute ended on Ctrl+C, or on Stop, which the window that pressed it records, since the process it ends cannot; stopped with errors when Execute ended on an error it did not expect, left a step running, or never wrote its end (killed); finished with errors otherwise. Choosing any loads the pull: Pull Log, Status and Stop are then its. A finished pull with no working blueprint loads its run folder's copy (D162). Choosing from one dropdown empties the other two, and a bold line above them names the loaded pull and its state in the dropdowns' words, `Loaded: Infant_RSV  (finished)` (D166). A line under them names the loaded file, its run folder and `pull_files/` (D57, D142), so there are no folder fields. The **Backup folder** row names the folder pulls are backed up to (D149), or where they go instead (`runs\backup`, when none is set or it can't be reached), with Browse, Clear and **Back up all**, which runs `--backup` in the window. There is no data dictionary line (D150): the dictionary is the bundle's, a dictionary saved by an older Run is neither passed nor kept, and nothing reads `YAMLMANAGER_DATA_DICTIONARY`. Then Validate, Export split, Preview SQL, Execute, Artifacts, **Scan runs** (D152) and **Audit dictionary** (D155), which run in the window, and Stop, with "Retry failed", "Re-pull everything" and "Re-pull sessions" options (`--retry-failed`, `--repull`, `--repull-session`). Ticking Re-pull sessions shows a dropdown of the loaded pull's finished sessions, `all` first, with Add and Remove and the list added so far; Execute passes each, or `--repull` for `all` (D158). There is no recipes field and no `--recipes` is ever passed (D49); settings saved by an older launcher that named one still load.

Four tabs (D71, D144):

- **Validation Output**: what Validate, Export split, Preview SQL and Artifacts print, which run inside the window.
- **Pull Log**: the loaded pull's Execute log, followed every second: the live Execute's (named in its lock), else the newest. A pull started from a terminal shows there too. Windows' `\r\n` is shown as a newline, a `\r` that ends one read waiting for its `\n` in the next.
- **Pull Manifest** (D144): the loaded pull's `pullmanifest.yaml` as it is, with line numbers, read-only, since the running pull rewrites it and an edit here would be lost or overwrite its record. Refreshed with Status, keeping where it was scrolled to, and redrawn only when the file changed. Every `status:` line is coloured as Status colours it; every `error:` that holds a message is red, with its message and detail. Double-clicking a row in Status shows that entry's lines, highlighted (a table whose parquet exists opens in the viewer instead, below): its whole error when it has one, a table's line in its step's `table_rows`, else the entry's first line. The entry is found by its own id (`session_id`, the phase's name, `run_id`), wherever in its list item the id is written, never by searching for the error's text.
- **Status**: the manifest as a tree, each phase and run with the tables it landed under it, each with its own rows (D137) and then, after Duration, its rows per join key: **Median per key**, **P90**, **Max** (D157). A running step's row says what is in flight, `c2of13 · OtherHospitalizations, since 11:01`: the session records the table it is building (`outputs.in_flight`) as it starts it and clears it when the step ends (D166). Double-clicking a table whose parquet is in the run folder opens it in the bundled viewer (`utils/client/viewparquets.py`), in a window of its own: the SneakPeek folder for a name ending `_sp`, else the Cosmos folder, else uploads; one packaged per value opens its first two files. Any other row, or a table not yet packaged, opens Pull Manifest (D167). Refresh and the manifest's path are at the top left. Refreshed when the window opens, on Refresh, every three seconds while the window's own command runs, and every three seconds while the loaded pull's lock is live, when it says "Executing since 14:03, last heartbeat 20s ago". Each refresh reads the manifest once, for this tab and Pull Manifest, and not at all when its size and modified time have not changed; Refresh always reads it (D154).

**Execute** opens a console window of its own (`CREATE_NEW_CONSOLE`) in the working folder, running the runtime's `pullmanager.py --execute <project> --keep-open` with the same Python and no shell between (D68): started from the window with its output piped back, it died on the VM before printing a line (`0xC0000142`). If the console cannot be opened, or its process ends before writing its log, the window says so, with the exit code (in hex for a Windows failure), and gives the terminal command (`python scope.py --execute <project>`, D123). If it ends with a non-zero code before its log has the pull's summary, Pull Log says it ended before finishing: what it was working on stays `running`, and the reason, if Python gave one, is just above. On Windows a killed process, Stop included, ends with 1. Stop ends a pull the window started and removes the lock it could not remove itself; a pull started from a terminal is stopped there. Closing the window leaves a pull in its own console running. On the Mac, with no console to open, Execute runs unseen and is read from its log.

While the loaded pull's lock is live (checked every second, whoever started it), Export split, Execute and Artifacts are greyed; pressing Execute greys them at once. Validate and Preview SQL stay available: they write nothing a pull reads (D67).

It tells the app the loaded pull's name, for the title (D166). It is a front end, not a second implementation. Each button runs the same command a person would type, as a subprocess, so a long pull cannot freeze the window and Stop has a real process to end. All logic lives in `launcher.py` (finding pulls in `pulls.py`, the lock in `lock.py`), which has no tkinter in it; `gui.py` only wires widgets, into a frame the app gives it (`pullmanager/app.py`), leaving the window's title and closing to the app. Chosen paths are remembered in `.pullmanager-gui.json` in the runs folder (`runs/` in the working directory, or where `datascope.json` says: `cleanup/runs/` on the Mac), not inside the extracted tree; one at the working folder's top, where it used to be, is moved there (D103, D111). Only the blueprint and a chosen dictionary are remembered. Closing the app asks first if Author has unsaved changes or Run has a command running.

------------------------------------------------------------------------

## Artifacts

`python scope.py --artifacts <project>`, or the launcher's Artifacts button, turns a pull into files for R and Python on the VM (the parquets cannot leave it). Execute runs it by itself after a clean pull (D141), then drops the pull's Projects tables (D165) and removes its working blueprint if unchanged (D162). By hand it refuses while the pull is executing (D67). Each run replaces what the last wrote in the three parquet folders, and clears nothing else in the run folder (D142).

``` text
runs/<project>/
  sneakpeek_parquets/   tables from COSMOS_SneakPeek; names end in _sp
  cosmos_parquets/      tables from COSMOS
  uploads_parquets/     the uploads, copied from the split's parquet
  contents.md     every table and column (D73)
  load_parquets.R, load_parquets.py
  utils.py, utils/client/   the client's utilities window: the parquet and transcription viewers (D148)
  HOW_TO.md       what each file is, and how to use it (D89)
```

A pull's folder without `pull_files/` is its deliverable (D148): what a client needs to use the data, and what made it.

**Parquets** (`artifacts.py`, D72). The manifest decides what is finished, never what exists in Projects: a PK table once its PK phase is done, a run's table once every run that fills it is, so a finished table group's tables are packaged while another group is unfinished (D138). The rest are listed as not packaged, with why. Each table is read from Projects, by its prefixed name (D163), in 50,000-row chunks and written with pyarrow, typed from its own `INFORMATION_SCHEMA` columns (BIGINT is `int64`, DATE `date32`, DATETIME2 `timestamp[us]`, text `string`; a type the driver cannot hand back, such as `DATETIMEOFFSET`, is read as text). `_batch` is dropped. A dimension with `separate_parquets` gives one file per value (`OtherDiagnoses_LA.parquet`, `OtherDiagnoses_LA_sp.parquet`): a run's table chosen by its `_batch` labels, the PK, which has no `_batch`, by the dimension's own predicate. An upload with no parquet in the split (a `dbtable`) is listed as not packaged; it is in Projects as its prefixed `upload_<dest>` copy.

It reports as it goes (D88): `writing <file> ...` as each parquet starts, then `wrote <file> (rows, size, seconds)`; an upload is `copied`. A table that fails is recorded with its error (`FAILED <table>: ...`), its partial file removed, and the next table packaged on a fresh cursor; a file is written to `.tmp` and renamed only when whole. The run ends with every file written, relative to the run folder (rows and size for each parquet), the failures, and `Artifacts finished in <time>: N table(s) in M parquet file(s), R rows, K left out, F failed`. It exits 1 if any table failed, saying to run it again once the cause is fixed.

**contents.md** (`contents.py`, D73) opens with the pull: its project database, which Cosmos databases, when it last finished, the Cosmos refresh dates, whether it was a test sample (`smallset`, its limit, and whether the sample was hashed), each file's rows, and what was left out. Then per table:

- **Granularity:** its own `granularity`; else "One row per" its `dedup_keys`; else "No granularity given".
- **Specific to:** from its session PK's `multiplier_levels` and split conditions, as the SQL says them, and a control's sampling: "Crohns (IBDType): ICD_Value K50.%; white (Race): p.FirstRace LIKE 'White%', sampled at 4 times CrohnsblackPatients_sp per batch"; a separated file adds its batch.
- **Description**, then each column as `` `PatientDurableKey`: BIGINT (py: int64, r: integer64): description ``. The types are the SQL type as held and the type once loaded, for writing code against the files. A column's description is its own; else the data dictionary's for its source (`p.Sex` is `PatientDim.Sex`, the aliases read from `from` and `join`); else "No description". Nothing is guessed.

Descriptions are read from the split, so a changed description reaches `contents.md` through a new split.

**Load scripts** (`loaders.py`, D75), at the run folder's root. `load_parquets.R` and `.py` open every parquet in the `*_parquets` folders (not `pull_files/`, which holds the uploads as sent; D142) without reading it (`arrow::open_dataset()`, `pyarrow.dataset`). Each table becomes a variable named for its file. R sets `arrow.int64_downcast = FALSE`, so 64-bit keys are `integer64` in every table and joins match. Each script names the run folder in `PARQUETS`; moved, that line changes.

**The stock files** (D89, D124, D148) are what `scripts/pullmanager_src/stock/stock.yaml` lists, by path from that folder: a plain entry lands at the run folder's top, `{file: ..., into: R}` in a folder of its own there, `{file: ..., as: name}` under another name, and `{folder: ..., into: ...}` every script in a folder. It lists `HOW_TO.md`, the utilities window (`../utilities.py` as `utils.py`) and the folder `../utils/client` into `utils/client`, so the client's window is the user's own with only the client's scripts, and a script put in `client/` reaches every pull with no new entry. A script's `BUNDLE = ""` line gets the bundle (D147). A `viewparquets.py` an earlier Artifacts put at the folder's top is removed. An entry that is not there is said, and the rest are still copied. The user edits them; the bundle carries them, so an edit reaches the VM with the next bundle. `viewparquets.py` (D146), in `utils/client/` and so in both utilities windows, is a tkinter window for a pull's tables. Opened from the utilities window it lists the pulls under `runs/` that have run or are running, with the words Run shows, read with Run's own `pulls.find_pulls`; the copy in a pull's folder, for a client who has only that folder, finds no runtime above it and opens on the pull whose folder it is in, with no dropdown. A pull's **Cosmos** and **Cosmos_SneakPeek** buttons (large) and **Uploads** (smaller) are greyed where their folders are empty, and the first with tables opens; under them, a button per parquet with its rows, read from the file's metadata. One table fills the window, a second splits it into two columns, and a third replaces the older of the two; each column has a close button and pages 1,000 rows and sorts on its own, and the shown tables' buttons are marked ●. **Browse...** opens any parquet anywhere by the same rule. With pyarrow a table is kept in Arrow: only the page on screen becomes Python values, and sorting is done in Arrow, empty values last. Without it, duckdb or pandas read it whole. `HOW_TO.md` says what each file is, how to open the utilities window and look at the data with the viewer, how to load it in RStudio and VSCodium, and how tables join. Its leading `<!-- stock HOW_TO.md ... -->` note is dropped from the copy, and `{project}` and `{parquets}` are filled in by plain replacement, so other braces stay as written.

### Backups

Before Artifacts replaces a pull's parquets (by hand, or at the end of a clean pull, D141), it backs the pull up (`backup.py`, D149): the run folder, without `pull_files/`, a lock, `.tmp` files and Python's cache, mirrored to `<backup folder>/<project>/`. A file already there with the same size and time (within 2 seconds) is left; one gone from the run folder is removed, so the backup is an exact copy and a second backup copies only what changed. Each file is copied to a `.tmp` and renamed, whole or not at all. So the backup holds the packaging before the latest: what a wrong pull noticed late needs. A pull never packaged has nothing to back up.

The backup folder is `datascope.json`'s `backup`, set in Run, so Execute, Artifacts and Run read one setting however a pull was started. When it can't be reached, can't be written, or none is set, the copy goes to `backup/` in the runs folder instead, packaging goes on, and Artifacts ends with a warning naming where the copy went. Only if neither can be written does Artifacts stop, replacing nothing. **Back up all** in Run (`--backup`) backs up every pull under the runs folder the same way, skipping, and naming, one that is executing.

------------------------------------------------------------------------

## Testing

Stdlib `unittest` everywhere, so every suite runs unchanged on the VM.

``` bash
python3 scope.py test                                      # all seven, and which passed
python3 scripts/makeYaml.py --tdd [group]                  # YAML Manager (204)
python3 scripts/pullmanager_src/pullmanager.py --tdd [mod]  # runtime (584)
python3 scripts/bundle_pullmanager.py --tdd [class]         # bundle (84)
python3 scripts/yamlmanager_model.py --tdd                  # the app's model (86)
python3 scripts/yamlmanager_tk.py --tdd                     # the app's Author view (32), needs a display
python3 scripts/tidy_images.py --tdd                        # the pasted-image cleanup (6)
python3 scripts/dictionary_fix.py --tdd                     # applying the dictionary audit (4)
```

Tests that read or write parquet need `pyarrow` and skip without it: they cover CSV conversion, uploads, and every session test (the runtime fixture's upload is parquet).

- **makeYaml** keeps its tests inline, including that a saved draft's empty lists and mappings read back empty, not null, one `TestCase` per `--tdd` group (`TEST_GROUPS`), so the file stays self-contained.
- **Runtime** tests live in `pullmanager/tests/test_*.py`, discovered by name, and ship in the bundle. After extraction, `--tdd` proves the delivery with no network and no repo. Tests needing repo fixtures skip cleanly there.
- **Bundle** tests cover the queue (every queued temp exported and carried in `bundle.py`, the queue emptied after and an old `bundle_with_yamls.py` removed; nothing queued, the software alone; a temp that does not validate, or is gone, stops the build naming it), YAMLs only (no software carried; extracting it places the YAMLs and leaves the runtime, `scope.py` and a file of yours inside the tree untouched; nothing to carry refused), `scope.py` and `utils.py` written beside the tree and an old generated `pullmanager.py` removed but one of yours kept, the stock files carried, tampering, determinism, extraction safety, `.local` preservation (including files a bundle stops shipping), transfer YAMLs carried with `yaml=` (placed beside `scope.py`, a different copy kept as `.local`, verified, in the content_id), and the VM pathway from one copied file: export a transfer YAML on the Mac, extract, split it with no recipes present, dry run.
- **Artifacts** tests package the runtime fixture against a fake Projects connection and read the parquets back (types, `_batch` dropped, left-out tables, separated files, SneakPeek folders), check `contents.md`, check the progress lines and the summary, package every other table when one fails (exit 1, no partial file), copy the viewer and the stock `HOW_TO.md` with the pull filled in, and run the generated load scripts: Python for real, R through `Rscript` when R has `arrow`. The session tests check the PK parquet: written where Artifacts puts it, before any run, and a failure to write it only warns. They check Execute's progress lines too (D136): every line timed and in order; a table saying it started before its query is sent, and a failing one leaving its start line with nothing after it; blocked and skipped steps saying so; each chunk naming itself; and the lines printed between the session's heading and its summary.
- **The model** is tested on its own, in a scratch workspace with its own recipes and dictionary: the Project settings (Pull from as `cosmos_db`, neither being an error, flat or grouped settings edited where they are), one PK of any kind, column renames and drops reaching validation, pending files, splitters of both kinds writing the keys makeYaml reads and refusing a column before there is a PK, each variable's source, binding candidates (fits first, never picked), order by number, messages pointing at their section, a crash kept as a message, saving (never over another file) and the transfer to the working folder, then the same transfer adjusted as on the VM with no recipes; the table builder (columns from the dictionary, removed, restored, renamed and moved; joins refused on differing or unknown types; editing in place; as the PK) and Save as Recipe.
- **The Author view** is tested on a real Tk, withdrawn, and skips without a display (D101), on intakes of its own in `scripts/yamlmanager_fixtures/temp/`, not the pulls in `YAMLs/temp/`, which change: every section built for every intake there, typing into a field edits the draft, a column renamed in the view is renamed in the template, the check marks the section, a refused edit is said, the table builder adds a table, Transfer saves and hands the file on, splitters by whether there is a PK, the binding picker binds what is chosen, a message goes to its field, and the exports and queue. The runtime's GUI tests check the app's wiring against the fake tkinter: the launcher built inside Run without touching the window, a transfer from Author loaded into Run, and Run opening without Author.
- **Bundle** tests also adjust a transfer YAML with the model inside an extracted tree, with no recipes: open, change, save an intake, export again beside `scope.py`.
- **Config** tests read a `datascope.json` that moves a core file and the runs folder, refuse a name nothing reads, check the repository's own finds every moved file, and hold makeYaml's and Pullmanager's defaults together; pulls are found, and settings saved, under a configured runs folder.
- **The fixes of D151 to D159**: the fake connection can fail a statement only at `nextset()`, as pyodbc does, and answer without a landing's count, so a late `INSERT` failure and a missing count each fail the PK phase (both confirmed to fail on the old `drain`); server messages reach the log and not the console; a save refused three times lands and one refused for five minutes fails saying why, with `sleep` recorded, not slept; Status reads the manifest once per refresh and not when unchanged; the run scan on a finished fixture pull, then with rows lost, a control kept short, a count missing; the audit against a fake Cosmos and its report read back as YAML; `dictionary-fix` removing a column and a table and nothing else; the column check failing `setup` with nothing built, and a table it cannot read only warning; rows per key recorded per run over its `_batch`, the PK by its key, a failed measurement a warning; re-pulling a control alone, a case with its control, `all`, an unknown name; a quoted CSV header renamed when added, and a rename back kept.
- **Pasted images** (D132): an image a document names by its path stays and an orphan goes; a deleted document takes its pictures; the same `paste-1.png` beside another document does not keep it; a path from the root, URL-quoted or in HTML counts; nothing outside an `images/` folder under `plan/`, or not named `paste-`, is touched; a mention under `cleanup/` does not count.
- **Transfer** tests check the outcome: a transfer YAML split alone, with no recipes file, gives byte-identical session YAMLs and uploads, and the same manifest (bar `source`), as the template split with recipes, for the tiny template and for test cases `01` and `02`.
- **Window tests on a real Tk** (the credit line, the parquet viewer) run the window in a process of its own, hidden, and read what it prints in UTF-8, forced both ways, since on Windows a child's piped output is cp1252. Every suite passes run with `PYTHONIOENCODING=cp1252`.
- **The app's later tests** cover the VM side (no pending, a missing file an error), Browse's relative paths, where lines by column and In supporting table reaching the transfer after the recipe's own, joins refused on differing or unknown types, the inline add panel and PK form, the Row key prefilled, the bundle's `content_id` kept on screen, and the quoted-name warning. Since D118 to D127: a written line naming an alias the table lacks refused (and said as it is typed), an alias rename rewriting the table's lines, where lines by every operator (BETWEEN's two values unquoted when they are variables; a `%` or a list under `=` refused), standard where lines arriving with a table, the join match following either side's column, the Validate mark going once nothing points there, imported table text written only when changed, Duplicate, the collapsed column list, Bundle YAMLs only emptying the queue, and a pasted CSV read, named and kept. The **makeYaml** SQL checks are tested on Infant_RSV's faults (an alias from a recipe, a where line's alias), UC_Visits' (a fact table joined to nothing), a subquery counting as joined, and dotted values in strings; the **runtime** tests add the Run dropdowns (a running pull left out of Start run), the backup line (D149), a dictionary an older Run saved neither passed nor kept (D150), the backup mirror and where it goes, `\r\n` in the log, the stock list, the utilities window, and `clear_projects_db`'s drops against a fake database (what is left; a key dropped first; a locked table skipped and the rest dropped).

Fixtures:

| Location | What |
|------------------------------------|------------------------------------|
| `YAMLs/manager_test_cases/*.yaml` | Realistic templates: `01` and `02` pass, `03` warns, `00`, `04`, `05` fail on purpose |
| `scripts/pullmanager_src/fixtures/split/` | Real `--export-split` output of `01_valid_basic.yaml`, the runtime's specification; its upload is parquet. Made before D142, so `sessions/` and `uploads/` sit beside its manifest; the runtime reads paths relative to the manifest, so the tests copy it into a run folder as it is. Regenerated with the command below, it would take the new layout, and the tests that name `sessions/...` would change |
| `scripts/pullmanager_src/fixtures/pullmanifest.in-flight.yaml` | That manifest mid-run, produced by driving the real transition API |
| `scripts/yamlmanager_fixtures/temp/` | The intakes the Author view's tests open; `IBD_Ancestry_intake.yaml` there lacks a variable on purpose |

Regenerate the split fixture after a change to split output:

``` bash
python3 scripts/makeYaml.py --template YAMLs/manager_test_cases/01_valid_basic.yaml \
  --recipes reference/recipes.yaml --export-split --out-dir scripts/pullmanager_src/fixtures/split
```

The database layer is tested against a fake cursor, and the GUI against a fake tkinter. The fake Projects connection keeps each destination's rows per `_batch` label, carried across executions like the real database, so retry, chunk and refresh tests check what landed where. Statements apply in order and a failure stops at the one it matches, so a run can fail after landing rows. By default a rollback undoes nothing, the worst case; a transactional mode, where only committed work survives, checks D55's commit per cohort. It also answers `INFORMATION_SCHEMA`, `OBJECT_ID` and `SELECT INTO` for uploads. Neither fake proves the real thing: nothing here has run against Cosmos. Real Tk is exercised by the Author view's tests (Tk 8.6 with the Mac's `python3.13`, which the VM likely matches) and by hand: Tk 9 under Xvfb on the dev box.

When adding a feature, add a passing case, a failing case, and a warning case if it can warn. A bug fix gets a test that fails with the bug reintroduced.