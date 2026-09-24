# Telescope Design

What the system is and the contracts each part keeps. Three documents, one job
each, so a fact lives in exactly one place:

| Document | Answers |
| --- | --- |
| `design.md` | What it is, and how it behaves. Describes the code as it stands. |
| `decisions.md` | Why, what was rejected, and what it cost. Append-only, numbered. |
| `roadmap.md` | What is unbuilt, unverified, or undecided. The only place status lives. |

When code and this document disagree, one of them is a bug. Fix whichever is
wrong in the same commit. Working conventions for this repo are in `CLAUDE.md`
at the root.

---

## The Pipeline

```text
Mac   template.yaml + recipes.yaml + datadictionary
        └─► makeYaml --export-transfer  (validate)
              └─► <project>_transfer.yaml        recipes written out, nothing applied
VM    <project>_transfer.yaml + datadictionary   no recipes file
        └─► makeYaml --export-split     (validate again, apply cosmos_db,
              │                          multipliers, batching)
              └─► split/pullmanifest.yaml, sessions/...
                    └─► Pullmanager --execute ──► <project_db>.dbo.<dest>
                                                  status back into the manifest
                                                  (artifacts: not built)
```

**YAML Manager** (`scripts/makeYaml.py`, UI `scripts/yamlmanager.py`) owns
authoring: templates, recipes, validation, and planning the pull into a split
folder plus `pullmanifest.yaml`. It never connects to a database. Recipes live
only on the Mac (D49): what crosses to the VM is a transfer YAML with every
recipe written out, which the VM validates and splits.

**Pullmanager** (`scripts/pullmanager_src/pullmanager/`) owns execution: it
renders its own SQL, holds connections, materializes batches, moves data from
Cosmos to Projects, and writes progress back into the manifest.

The boundary between them is files. YAML Manager writes a plan; Pullmanager
writes status into the same document. Neither imports the other.

**makeArtifacts** (parquet export plus a contents file describing what actually
landed) is planned, not built. See the roadmap.

---

## Where It Runs

Two machines, one codebase, updated one way.

- **Mac** (and the Linux dev box): development, and most YAML authoring, in
  the browser UI.
- **VM**: air-gapped Windows, the only place Cosmos and Projects are reachable.
  Runs pulls. Cannot pull from git. Cannot load a page served by Python on
  localhost, or a static HTML file opened from disk, and has no in-editor
  browser. Has tkinter, but no other Python desktop toolkit (the `shiny` and
  `tcltk` entries in its package list are R).

### Environments

| | VM | Mac | Linux dev box |
| --- | --- | --- | --- |
| OS | Windows | macOS | Bluefin (immutable Fedora Silverblue) |
| Python | 3.13.9 | `python3.13` (python.org 3.13.9, `/usr/local/bin`); brew's 3.14 `python3` has no tkinter | brew 3.14 (`/var/home/linuxbrew/.linuxbrew/bin/python3`); system `/usr/bin/python3` has no tkinter |
| tkinter | yes, likely Tk 8.6 | Tk 8.6, bundled with the python.org install | via `brew install python-tk@3.14`, Tk 9 |
| numpy | 2.1.3 | 2.1.3, installed for all users | not installed |
| pyarrow | 22.0.0 | 22.0.0 in `python3.13`; brew's `python3` has 25 | whatever is installed |
| Database | `pyodbc` 5.3.0, ODBC Driver 17 for SQL Server | none reachable, no driver | none reachable, no driver |
| YAML | `ruamel.yaml` 0.17.17, `pyyaml` 6.0.3 | the same in `python3.13`; brew's `python3` has both | whatever is installed; Ruby fallback |

On the Mac, use `python3.13` for anything run on the VM (the launcher, the
runtime tests) so it meets the VM's Python, Tk and packages, not brew's. It
has the VM's versions, installed for all users:

```bash
sudo /usr/local/bin/python3.13 -m pip install ruamel.yaml==0.17.17 pyyaml==6.0.3 pyarrow==22.0.0
```

Without a YAML package the runtime cannot read YAML at all (`No YAML backend
available`); `makeYaml` alone falls back to Ruby, and names the Python that
sent it there if Ruby then fails. Without `pyarrow`, nothing can read or write
a parquet upload, and the tests that need it skip. All four suites pass under
`python3.13` with nothing skipped.

`requirements-vm.txt` pins those versions, for any Python 3.10 to 3.13 or venv:
`python -m pip install -r requirements-vm.txt`. Every Python on the Mac has the
packages, so whichever one an editor picks can run the tools: the VM's
versions in `python3.13`, uv's 3.11 and 3.13 and brew's 3.11; the newest that
fit in brew's 3.14, Apple's 3.9 and the project venvs.

**`YAMLs/DSVM Plugins.yaml` is the VM's installed software and package list.**
Check it before depending on anything outside the standard library; if it is
not listed, the VM does not have it and cannot get it.

Cosmos permissions are narrow: `VIEW DATABASE PERFORMANCE STATE` is denied and
`sys.partitions` returns nothing, so row counts come from counting, never from
metadata (D33).

Dev box gotchas: an IDE Python console (Positron's `%run`) keeps a started
`yamlmanager` server alive and holding port 8765 after the script "finishes";
run the server from a terminal instead. To reach it from another machine over
Tailscale: `--public --browser-host <hostname> --no-open`. Real-Tk GUI testing
without a display uses Xvfb (`brew install xorg-server`).

### The Bundle

Everything reaches the VM as one self-extracting file,
`dist/pullmanager_bundle.py`, built by `scripts/bundle_pullmanager.py` from
`scripts/bundle_extractor.py` (the prelude) plus every file it carries.

It carries the whole unit, not just the runtime:

```text
telescope/                          # the extracted tree
  pullmanager.py                    Pullmanager entry point
  pullmanager/                      runtime package and its tests
  scripts/makeYaml.py               validator and splitter
  YAMLs/datadictionary.yaml
  .bundle-manifest.json
```

Recipes, the browser UI and a template to start from are not shipped (D49):
the VM works from transfer YAMLs, and cannot open the UI. Published paths
reproduce the repo's `scripts/` beside `YAMLs/` shape, so `makeYaml` finds its
dictionary with no flags and no knowledge that it was bundled.

Guarantees:

- **Deterministic.** No timestamp. Identity is `content_id`, a hash over the
  sorted `(path, sha256, size)` list, so rebuilding unchanged sources is
  byte-identical.
- **Readable.** Payload lines are comment-prefixed source (`# ` + line; a blank
  line is `#`), not base64, so a file can be read straight out of the bundle.
  A payload line that looks like a marker encodes to `# # === ...` and cannot
  match the anchored marker pattern.
- **Verified both ways.** Size and SHA-256 checked against the embedded
  manifest before writing, and re-hashed from disk after.
- **Refuses** absolute paths, `..`, drive letters, backslashes, duplicate or
  unlisted sections, unterminated sections, mismatched END markers. CRLF
  sources are rejected at build time.
- **All-or-nothing.** Extraction stages to a sibling temp directory and swaps
  only after everything verifies. It replaces a previous extraction (one with
  `.bundle-manifest.json`) but refuses any other directory without `--force`.

### Updating Never Destroys Work

The extracted tree is **wholly managed**: it is swapped, not merged, so a file
of yours placed inside it is gone after the next update. Templates, uploads and
split folders belong beside the tree.

Every bundled file is replaced on re-extraction. One that was edited on the VM
is first set aside as `<name>.local` and reported. "Edited" is judged against
the hash the previous extraction recorded in `.bundle-manifest.json`, so a file
that only changed between releases is replaced quietly. With no record (a first
or `--force` extraction), any difference counts.

A file the previous bundle shipped and this one does not is removed, unless it
was edited, in which case it too is kept as `.local`. `.local` copies are
carried through later updates until you delete them.

```text
replaced   YAMLs/datadictionary.yaml  (your previous copy saved as YAMLs/datadictionary.yaml.local)
no longer shipped   YAMLs/recipes.yaml  (your edited copy kept as YAMLs/recipes.yaml.local)
no longer shipped   scripts/yamlmanager.py  (removed; it had not been edited)
kept       YAMLs/datadictionary.yaml.local  (set aside by an earlier update; delete it when done)
```

Nothing a user authors is bundled.

### No Configuration

No `.env` ships and none is needed. Both hosts are DNS aliases with defaults
(`COSMOS`, `PROJECTS`), authentication is Windows-integrated, and database names
come from the manifest. `COSMOS` versus `COSMOS_SneakPeek` versus `Dual` is a
template setting. `PULLMANAGER_*` environment variables, or a `.env` passed with
`--env` or found in the working directory, override if a host ever changes.

### Delivering An Update

On the Mac:

```bash
python3 scripts/bundle_pullmanager.py --tdd     # optional: the bundle's own tests
python3 scripts/bundle_pullmanager.py           # writes dist/pullmanager_bundle.py
```

Copy that one file to the VM. Nothing else travels. The same sources always
produce the same `content_id`, so it tells you whether the VM has the latest.

### Setting Up The VM Folder

The extracted tree is replaced on every update, so everything you author sits
beside it:

```text
<project share>\
  data\                       big reference files; a dictionary if kept outside the bundle
  QueryGenerator\             where you work: the working directory for every command
    pullmanager_bundle.py     the copied file
    telescope\                extracted; managed; never put your own files in here
    IBD_transfer.yaml         a transfer YAML, exported on the Mac
    split\                    written by --export-split
    sql\                      written by a dry run
    .pullmanager-gui.json     the launcher's remembered paths
```

Typed paths resolve from the working directory, so the parent folder is plain
`..\data\datadictionary.yaml`. Upload `file_loc` values resolve relative to
the transfer YAML, so its upload files keep the same places relative to it as
on the Mac (the export lists them). `YAMLMANAGER_DATA_DICTIONARY` sets the
dictionary once.

A change made on the VM is an edit to the transfer YAML by hand. A recipe
change is made on the Mac and re-exported.

### The Whole Pathway On The VM

```bash
python pullmanager_bundle.py --verify-bundle
python pullmanager_bundle.py --extract ./telescope
python telescope/pullmanager.py --tdd                        # prove the delivery

python telescope/pullmanager.py --gui                        # desktop launcher

# or the same steps by hand
python telescope/scripts/makeYaml.py --template IBD_transfer.yaml --export-split --out-dir ./split
python telescope/pullmanager.py --dry-run split/pullmanifest.yaml --out-dir ./sql
python telescope/pullmanager.py --execute split/pullmanifest.yaml
```

The repair loop for a VM-side bug: read the file out of the bundle or the
extracted tree, bring the text back to the Mac, fix the source, rebuild.

---

## Authoring: YAML Manager

### Inputs

| File | Role |
| --- | --- |
| template | The pull: project metadata, `cosmos_vars`, `run_vars`, `project_vars`, `multipliers`, `batching`, `upload_cohorts`, `cohorts` |
| `YAMLs/recipes.yaml` | Reusable cohort and batching definitions, referenced by name. Mac only (D49) |
| `YAMLs/datadictionary.yaml` | Source of truth for Cosmos tables, columns and types |

Paths typed on the command line (`--template`, `--recipes`,
`--datadictionary`) resolve from the working directory, like any command-line
tool. Defaults resolve from the install. `--datadictionary` is honoured by every
route: validation, the UI, pre-YAML, transfer and split export. A template that
does not exist is a one-line error, not a traceback.

The recipes file is read only when the template refers to it: a cohort with
`recipe:`, or a batching item that names a batching recipe (`sex`,
`{state: {...}}`). A transfer YAML refers to none, so it needs no recipes file,
and a missing or broken one cannot stop it. A template that does refer to
recipes, with no recipes file, is refused with `recipes_not_found`, listing
every reference and pointing at `--export-transfer`.

### Recipes And Table Inputs

A recipe that reads another generated table names it through a variable:

```sql
INNER JOIN {{prefix}}_{{HospitalICDTable}} AS hic ON ...
```

YAML Manager infers from this that the recipe has a **table input**
`HospitalICDTable`, and which columns it reads through the alias (`hic.X`).
`{{prefix}}` is the project's temp prefix (Naming, below), filled in by YAML
Manager; `{{prefix}}_Patients` names a generated table directly.

**`PKTable` is the one input bound automatically**, to the session's root PK.
Every other table input must be bound on the cohort that uses the recipe:

```yaml
cohorts:
  - name: Diagnoses
    recipe: DiagnosesByHospitalICD
    vars:
      HospitalICDTable: HospitalICDCodes
```

An unbound table input is the error `unbound_table_input`. It never picks a
table, but it names the ones that could work, judged by whether their known
columns cover what the recipe reads:

```text
Cohort `Diagnoses` joins a table through `HospitalICDTable` (as `hic`) and reads
column(s) ICDCode from it, but nothing binds `HospitalICDTable`. Tables in this
template that fit: HospitalICDCodes (upload). Bind it on the cohort using
recipe `DiagnosesByHospitalICD`: `vars: {HospitalICDTable: HospitalICDCodes}`.
```

An upload whose schema cannot be known (a `dbtable` with no declared columns,
or a parquet where `pyarrow` is missing) is listed separately as a table that
*may* fit. An ordinary missing scalar variable is `missing_variable`.

### Upload Files

Parquet is the upload format, because it carries types (D54). An upload
declares the file and, optionally, types for some of its columns:

```yaml
upload_cohorts:
  - name: HospitalICDCodes
    dest_table: HospitalICDCodes
    file_type: csv              # parquet, csv (converted at split) or dbtable
    file_loc: "csv/HospitalICDCodes.csv"
    columns:                    # optional; the rest keep the file's types
      - name: PatientDurableKey
        type: BIGINT
```

- A **parquet** keeps its own types; a declared type converts that column (R
  often writes large IDs as doubles, which would otherwise land as `FLOAT`).
- A **CSV** is converted to parquet when the split is exported: declared
  columns get their type, the rest stay text. A value that does not fit stops
  the split, naming the column. `makeYaml.py --csv-to-parquet FILE.csv
  [--out FILE.parquet] [--column NAME=TYPE ...]` converts one file by hand.
- A **dbtable** is a table already in the project database (`source_table`,
  else `dest_table`).
- A `file_loc` ending `.csv` under `file_type: parquet`, or the reverse, is
  `upload_type_mismatch`.
- A **missing upload file** is a warning where the output is a plan that
  travels, the transfer YAML and the UI, since the file may only exist on the
  VM: the transfer is still written, and its listing marks the file "not here
  yet". It is an error at the split, which needs the file, so on the VM the
  launcher's Validate and Export split confirm every file is in place. Batching
  columns on an uploaded PK whose file is not here are left unchecked, with a
  warning (`batch_columns_unchecked`), until then.
- Declarable types: `BIGINT`, `INT`, `SMALLINT`, `TINYINT`, `BIT`, `FLOAT`,
  `REAL`, `DECIMAL(p,s)`, `DATE`, `DATETIME`, `DATETIME2`, `VARCHAR(n)`,
  `NVARCHAR(n)`, `CHAR(n)`. Anything else is `bad_upload_type`; a declared
  column the file lacks is `unknown_upload_column`.

With `pyarrow`, validation reads a parquet's own columns, so recipes bound to
it are checked like any other table. An upload marked `type: pk` is the
template's PK (only one PK per template); batching is checked against its
file's columns. `split_after_build` multipliers on an uploaded PK are refused
(`split_after_build_on_uploaded_pk`): batch by that column instead, which
puts each group in a batch of one table rather than a table of its own, or
split the list before uploading it and run one pull per group.

### Validation

Validation is static: it reads the template, recipes and dictionary, never a
database. Rendering is skipped once blocking errors exist, so each problem is
reported once rather than again as a rendering failure. Every error is
collected, not just the first.

Every error carries a **fix**: what to change, and where. On the VM the YAML is
edited by hand (D49), so an error that only says what is wrong leaves the
reader guessing. A test fails if any `result.error(...)` in `makeYaml.py` has no
`fix=`. The context is a field path, the cohort by position and name:

```text
ERROR [missing_variable] at cohorts[0] (Patients): filter.where[1]: Cohort `Patients` requires variable `ICD_Value`, but no value was provided.
      fix: Add `ICD_Value: <value>` under the top-level `vars`, or under this cohort's own `vars`.
```

Checks:

- Required variables and table inputs are bound (above).
- Recipe references resolve.
- Upload files exist; their columns (CSV header, parquet schema) carry what
  bound recipes read; declared upload columns exist and have a type an upload
  can take (Upload Files, above).
- Zero or one upload cohort is `type: pk`, and it declares `key_columns`.
- Multiplier definitions are well formed.
- Batching definitions, field by field, since a transfer YAML writes them out
  in full: a known `kind`; `column_values` has a `column` on the PK and a
  non-empty `values` list; `row_chunk` has a positive `rows_per_batch` (the
  shipped `chunk` recipe's `required` placeholder is refused). `values: all`
  warns that the pull will stop at it.
- Temps are named with `{{prefix}}_`: `##JVM_` anywhere in a cohort is
  `old_temp_marker`, whose fix is the line rewritten. `prefix` is a reserved
  variable, and `temp_prefix` must be letters, digits and underscores, at most
  30 (`bad_temp_prefix`).
- Every cohort column against the data dictionary (below).

Authoring rules applied on the way:

- `dedup_keys` is canonical, a list of lists (`[[DiagnosisEventKey]]`). Legacy
  `dedup_key` is accepted and normalized, with a warning.
- `stop_at_for_pk_table` limits the **root** PK cohort only: the one joining no
  other generated temp. Under `Dual` there is one root per database, and each is
  limited.
- `stop_at_for_non_pk_tables`, `print_md` and `printout_md` are dead and warn
  that they are ignored. (`makeYaml.py --report` is unrelated: it reports on the
  template, not a run.)
- `from` and `join` are schema-qualified alike, and only where no schema is
  present, so `dbo.dbo.` and a qualified temp are impossible.
- `sql_condition(column, var)` renders a scalar as `=`, a list as `IN`, a
  wildcard value as `LIKE`, and warns on `_` inside a `LIKE` value.

### Data Dictionary Validation

Columns arrive as `source: dt.DiagnosisKey`, so the alias is resolved to a table
through `filter.from` and `filter.join` first. That is what catches `p.Type`
written where `dt.Type` was meant: the alias exists and the column exists, on a
different table.

| Case | Result |
| --- | --- |
| Table absent from the dictionary | Error. Add the table to the dictionary. |
| Column absent from the table | Error. |
| Alias cannot be resolved | Error. |
| Type family mismatch | Error. |

The dictionary uses annotated abstract types; cohorts declare T-SQL. The
parenthetical is stripped and families compared, with widening accepted:

| Dictionary | Accepts |
| --- | --- |
| `bigint` | `BIGINT` |
| `integer` | `INT`, `SMALLINT`, `TINYINT`, `BIGINT` |
| `string` | `VARCHAR(n)`, `NVARCHAR(n)`, `CHAR(n)` |
| `boolean` | `BIT` |
| `numeric` | `DECIMAL`, `NUMERIC`, `FLOAT`, `REAL` |
| `datetime`, `date/datetime` | `DATE`, `DATETIME`, `DATETIME2(n)` |

Lengths are not compared: the dictionary records none. Nullability is not
cross-checked, because `nullable: false` on a nullable column is the documented
way to force an `IS NOT NULL` filter.

### Keys And Relationships In Cosmos

Validation checks that every column exists with the right type, not that a
join is right. `ON tc.TerminologyConceptKey = dt.DiagnosisKey` passes and is
wrong; a correct join can still multiply rows, if it meets a table with several
rows per key and no filter. Knowing each table's keys, and what each foreign
key points at, would let validation check joins. Three questions were put to
the VM's AI (`QMDs/keys_research/`: the brief, and its answer transcribed): are
keys declared where SQL can read them, do they hold in the data, and what does
the interactive data dictionary show. It explained its queries rather than
running them, so nothing below has been counted yet.

| Finding | Source | How sure |
| --- | --- | --- |
| Keys are not readable through SQL: `INFORMATION_SCHEMA` constraints, `sys.foreign_keys` and `sys.indexes` are hidden from analyst logins, so an empty result means "cannot see", not "no keys" | VM AI; fits our narrow permissions (D33) | Likely. The permission probe (`HAS_PERMS_BY_NAME`) has not been run |
| The dictionary cannot be exported; only Epic could supply it as a file | VM AI | Unverified |
| The interactive data dictionary does declare keys: each table's own key, each foreign key with the table and column it points at, and how many rows match on each side | The `DiagnosisEventFact` page | Seen, for one table |
| `DiagnosisTerminologyDim` holds a row per terminology, several per `DiagnosisKey`, so joining it on `DiagnosisKey` alone duplicates diagnoses; constrain `Type` (our recipes do) | VM AI | Likely; not counted |
| `PatientDim` keeps history. Its own key is `PatientKey`, one per version of a patient; `DurableKey`, what other tables point at, is one per patient, and `IsCurrent = 1` picks one row for it | The diagram; VM AI | The keys seen; the history likely |
| `-1` in a foreign key means unmapped or a mixture (`LabComponentKey`, `MedicationKey`), and fact tables carry placeholder rows with negative keys, so `-1` is not an orphan; `0` varies by column | VM AI, as Epic convention | Unverified |
| `create_date` is when a database was created, not when its data was loaded | VM AI | True in general. D51 holds only if a refresh recreates the database, which the date matching the last refresh suggests; confirm across the next one |

**Reading a dictionary page** (`DataDictionary DiagnosisEventFact example.png`):

- **Columns tab:** each column's type, and for a foreign key the *table* it
  points at, in blue. A `Partition key` badge (here `StartDateKey`) marks the
  column that lets SQL Server skip most of the table when filtered; our recipes
  filter it.
- **ER Diagram:** the table's own key (filled key icon, `DiagnosisEventKey`),
  then a "Foreign keys" list (outline key icons). A dashed line runs from each
  foreign key to the table it points at, ending on the *column* it lands on,
  which the Columns tab does not give. The ends give the cardinality: a crow's
  foot on this side (many rows here), a bar on the other (one row there).
- **For `DiagnosisEventFact`** that reads: `DiagnosisKey` → `DiagnosisDim.DiagnosisKey`;
  `PatientDurableKey` → `PatientDim.DurableKey`; `EncounterKey` →
  `EncounterFact.EncounterKey`; `AgeKey` → `DurationDim.DurationKey`;
  `StartDateKey`, `EndDateKey`, `NotedDateKey_X`, `UserEnteredDateKey` →
  `DateDim.DateKey`; `SourceComboKey` → `DiagnosisEventSourceBridge`, a bridge
  from one combination key to several `SourceDim` rows. Our dictionary has
  `DiagnosisKey` pointing at "DiagnosisDim/DiagnosisTerminologyDim"; the
  interactive dictionary says `DiagnosisDim`.

What this means for a join check, still to plan (roadmap, Needs Research):
relationships come from the interactive dictionary into
`datadictionary.yaml` by hand, since SQL cannot supply them. Each needs more
than a target table: the column it lands on, its cardinality, whatever filter
makes the other side one row (`IsCurrent = 1`, a `Type`), and its sentinel
values. Data checks then confirm it: the parent key unique under its filter,
and no child rows without a parent, sentinels aside.

### Choosing The Cosmos Database

`cosmos_vars.cosmos_db` in the template, never VM configuration:

| Setting | Connects to | Cohorts rendered |
| --- | --- | --- |
| `COSMOS` | `COSMOS` | once |
| `COSMOS_SneakPeek`, `sneakpeek`, `sp` | `COSMOS_SneakPeek` | once, each tagged |
| `Dual`, `both` | `COSMOS` | twice; `_sp` variants tagged `COSMOS_SneakPeek` |

A cohort tagged with its own database qualifies its tables three-part
(`COSMOS_SneakPeek.dbo.PatientDim`), because a two-part name resolves against
whichever database is connected. Global temps stay unqualified: tempdb does not
follow the connected database.

An `_sp` copy reads the `_sp` copies of the generated tables it joins:
`##tesrun_Patients` becomes `##tesrun_Patients_sp` in its SQL, so it pulls for
the SneakPeek population. Uploads are shared by both copies and keep their
names.

### Outputs

- **Transfer YAML** (`--export-transfer`, `--out` to choose the file): what the
  VM receives (D49). The template with cohort recipes merged into their cohorts
  and batching items replaced by their full definitions; multipliers and
  batching are declared, not applied. Written only if the template passes full
  validation. Named `<project_folder>_transfer.yaml`, beside the template by
  default. `file_loc` is never rewritten, since it is what the VM resolves.
  Written to another folder (`--out`), each upload is copied there at its
  `file_loc`, so that folder is the unit to carry across; one outside the
  template's folder (`..` or absolute) is left as written, with the warning
  `upload_not_copied`. The export lists every upload path to carry.
  It opens with a `transfer:` block: `from_template` (file name), and, when
  recipes were used, `recipes_sha256` (first 12 hex digits) and
  `recipes_used`. No timestamp, so the same inputs give the same file. The
  split drops the block from its YAMLs and records it as the manifest's
  `source.transfer`, with `source.recipes` null.
- **pre-YAML** (`--export-preyaml symbolic`): the template, portable, close to
  what was authored, recipe references left symbolic. `expanded-recipes` inlines
  cohort recipes only, for inspection. Upload paths stay relative to where it
  was written, so moving one means moving its uploads too.
- **Split folder** (`--export-split --out-dir`): below. Self-contained: upload
  files are copied into `split/uploads/` and `file_loc` repointed, so the folder
  is the unit to copy or archive. A CSV upload is written there as parquet
  (`file_type: parquet`), with its declared types. Parquet output is
  deterministic: the same inputs give the same bytes.
- **Report** (`--report`): markdown summary of the template.

### The Browser UI

`scripts/yamlmanager.py` serves an editing and preview dashboard, talking to the
compiler only through `scripts/yamlmanager_backend.py`
(`YAMLMANAGER_BACKEND_MODULE` can swap it). With no arguments it serves on
`127.0.0.1:8765` and opens a browser. Any serving flag (`--host`, `--port`,
`--public`, `--browser-host`, `--no-open`) implies serving; `--static` writes a
file instead. `--port 0` picks a free port.

It is the Mac's authoring tool, and is not bundled. The VM cannot load it; the
VM uses the launcher. Its Exports tab shows the pre-YAML, the transfer YAML
(named for the project, with a Download button) and the pull manifest. Every
message shows its fix beneath it.

The **Builder** tab assembles a template section by section: Project,
Uploads, Multipliers, Batching, Cohorts and the draft YAML. Each section's
title carries a one-line explanation taken from the comments in
`YAMLs/template.yaml` (the comment on the key's line, else the lines just above
it), with built-in text where a key has none; editing those comments changes
the page.

- **PK table.** One per template, marked with a checkbox on an upload or a
  custom table, or coming from a PK recipe (shown as a badge). Marking a
  second is refused, naming the one that exists. Uploads and Cohorts show the
  current PK table. An uploaded PK asks for its key columns.
- **Multipliers.** `during_build` levels take variables
  (`ICD_Value: K51.%, K52.%`, `;` between variables); `split_after_build`
  levels take a PK column, values, and an optional role and row mult.
- **Custom tables** are built from the data dictionary: name, destination and
  PK checkbox, with "Add Custom Table" (or "Save Changes" when editing a loaded
  one) ending that row; "Reset Form" sits by the heading. Under Joins, a note
  on what each join type does with rows that do not match.
- **Save as Recipe**, on an added custom table, writes it into `recipes.yaml`
  (D56). A name already there is refused. A page opened as a file, not served,
  cannot write, and downloads the recipe instead.

**Save & Refresh**, at the end of the tab bar, saves the Builder's draft and
reloads every tab from it, so Validation, Graph, Exports and YAML show what
was just built. It writes `<project_folder>_temp.yaml` beside the template the
page opened with, so upload paths relative to the template still resolve, and
the template itself is never overwritten; saving again from a `_temp.yaml`
overwrites that file. The draft is written with the YAML library and read back
before it replaces anything. The tab that was open stays open. A page opened
as a file, not served, cannot save.

The **Cohorts** tab opens with a read-only line each for the multipliers
(`IBDType: UC/Crohns, Race: black/white`) and the batching
(`state: LA/MS/GA/NC, sex: Female/Male, chunk: 2000`).

---

## The Split Folder

```text
split/
  pullmanifest.yaml
  uploads/                     copied upload files
  sessions/
    <session_id>/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        run.yaml               unbatched
        b1of4-LA-Female.yaml   or one per batch combination
```

A **session** is the scope in which one Cosmos connection stays open, because
global temps die with it: one PK, and everything pulled for it. A multiplier
produces one session per multiplied PK, and under `Dual` each has an `_sp`
twin; otherwise there is one.

Each cohort records the session that builds it as `session_pk`: its multiplier
group's PK (or the uploaded PK). A session's runs hold only its own cohorts, so
every cohort is built once, joined to its own session's PK. With IBDType x Race
x `Dual`, that is 8 sessions of one `OtherHospitalizations` each.

Every phase document carries `temp_prefix` (Naming), and the manifest's
`project` records it too.

Every session has the same routine, batched or not:

| Phase | Does |
| --- | --- |
| `setup` | Creates the Projects destination tables (drop and create; kept on a resume) |
| `upload_cohorts` | Uploads every upload cohort into `##<prefix>_<dest>`, once per session |
| `pk` | Builds the PK table and copies it to Projects, or registers an uploaded one |
| runs | Pull the remaining cohorts, one run per batch combination |

Each YAML is standalone-valid: it repeats the project metadata, drops the
`multipliers` and `batching` instructions so nothing expands twice, and carries
a `pull_context` saying where it belongs:

```yaml
pull_context:
  session_id: Patients
  phase: run
  cohort: Patients
  pk_table: Patients
  run_id: Patients__run
  pk_source: {kind: generated, table: Patients}
```

An upload cohort marked `type: pk` (at most one) becomes the session's PK
source: `upload_cohorts` uploads it, and `pk` registers it
(`pk_source: {kind: uploaded_cohort, upload_name, table, key_columns}`) instead
of building one. Its Projects copy is the upload's own (`upload_<dest>`,
What A Session Does), which its uniqueness check, batches and chunks read, as a
generated PK's do. Its session carries the template's batching.

### Batching

Batch dimensions **cross-multiply**. They partition the cohort; they are not
alternative slicings of it:

```yaml
batching:
  - state: {values: [LA, MS]}
  - sex                          # Female, Male
  - chunk: 2000
```

`state × sex` is four runs: `b1of4-LA-Female`, `b2of4-LA-Male`,
`b3of4-MS-Female`, `b4of4-MS-Male` (D53). The number makes every label unique,
so two combinations can never share one; `A B` and `A-B` both clean to `A-B`
and are told apart by it. A run with only `chunk:` is `b1of1`.

The unit of multiplication is the **bucket**. `include_other: true` adds a
catch-all bucket (`<dimension>-other`), so `values: [Female]` plus
`include_other` is two. The catch-all records the values it `excludes`, and its
predicate includes `IS NULL`, because `NOT IN` never matches NULL.

| Dimension | Buckets known | Expanded by | Recorded as |
| --- | --- | --- | --- |
| `column_values` with listed values | at plan time | YAML Manager | `batch.dimensions`, one run each, stable `run_id` |
| `column_values` with `values: all` | at run time | refused (roadmap) | `batch.runtime` |
| `row_chunk` | at run time | Pullmanager | `batch.runtime` |

So `chunk: 2000` subdivides each combination rather than joining the product.
The chunks run inside their run, not as manifest nodes (D53): Pullmanager
counts the batch's PK rows and pulls `ceil(rows / 2000)` chunks in turn,
showing progress as `c2of3` on the run.

Batch membership is deterministic. A values bucket is a predicate, and a run's
buckets combine with `AND`, so the order of the dimensions changes only the
label (`LA-Female` or `Female-LA`), never the rows. A chunk is `ORDER BY` the
PK's key columns, verified unique after the PK phase. Both read the Projects
copy of the PK (D19), which a Cosmos refresh does not move.

---

## The Manifest

`pullmanifest.yaml` is the plan and, once Pullmanager starts, the status
record. YAML Manager writes it with every status `pending`.

```yaml
manifest_version: 1              # Pullmanager refuses anything else
project:
  name: <str>
  project_folder: <str>
  project_db: <str>              # e.g. PROJECTD33A929
  temp_prefix: <str>             # e.g. tesrun (D50)
  created_by: yamlmanager
source:
  template: <path>
  recipes: <path|null>           # null for a transfer YAML, which adds transfer: {...}
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

```yaml
yaml: <path, relative to the manifest's directory>
status: <status>
started_at: <iso8601|null>
finished_at: <iso8601|null>
rows: <int|null>
outputs: {}                      # untyped; Pullmanager fills it
error: <null | {message, detail}>
```

A batched run adds:

```yaml
batch:
  name: b1of4-LA-Female
  dimensions:
    - {name: state, kind: column_values, column: StateOrProvinceAbbreviation, value: LA}
    - {name: sex,   kind: column_values, column: Sex, value: Female}
  runtime:
    - {name: chunk, kind: row_chunk, rows_per_batch: 2000, applies_to: PKTable}
```

A catch-all bucket carries `is_other: true` and `excludes: [...]` in place of
`value`. Absent optionals are omitted, not written as `null`.

### Fields Pullmanager Adds

```yaml
# on a node
duration: {seconds: 312, display: "5m 12s"}   # on finish or fail; cleared on retry
note: <str>                                   # why skipped or blocked; never in `error`
epoch: <str>                                  # the connection it completed under

# on a run
outputs: {chunk: c3of3, batch_pk_rows_total: 4500, batch_pk_rows: 500, batch: ...}

# on a session
runtime:
  epoch: "20260922T140000-3f2a9c11"
  opened_at: "2026-09-22T14:00:00-05:00"
  linked_server: et4003vpdsql032
  temp_prefix: tesrun                          # tesrun2 if another pull held tesrun (D50)
  cosmos_created: {Cosmos: "2026-09-17T19:34:56.450"}

# on the manifest (D51)
cosmos_refresh: {Cosmos: "2026-09-17T19:34:56.450", Cosmos_SneakPeek: "..."}
```

### Rules

- **Phase names are closed**: `setup`, `upload_cohorts`, `pk`, in that order,
  which is execution order. Any other key raises.
- **Order comes from the manifest**, never from folder names. `yaml` paths
  resolve against the manifest's directory.
- **Unknown keys survive.** The loaded mapping is mutated in place and written
  back whole, so a newer YAML Manager can add fields without breaking an older
  bundle.
- **Writes are atomic**: temp file, then rename.

### Status

| Status | Meaning |
| --- | --- |
| `pending` | Planned, not started |
| `running` | Executing, or interrupted while executing |
| `done` | Completed |
| `failed` | Attempted and failed |
| `skipped` | Deliberately not run, e.g. no upload cohorts |
| `blocked` | Not run because something upstream failed |

`done` and `skipped` are *settled*. Session status is recomputed from its
children on every save: any `failed` → `failed`; any `running` → `running`; any
`blocked` → `blocked`; all `skipped` → `skipped`; all settled → `done`; some
settled → `running`; otherwise `pending`.

---

## Execution: Pullmanager

### Naming

`naming.py`:

| Thing | Rule | Example |
| --- | --- | --- |
| Cosmos global temp | `##<prefix>_<dest>`; a `JVM_` prefix on the dest is stripped first, never doubled | `##tesrun_PKTable` |
| Projects staging | `#Local_<dest>` | `#Local_PKTable` |
| Projects copy of an upload | `<project_db>.dbo.upload_<dest>` (D54) | `PROJECTD33A929.dbo.upload_HospitalICDCodes` |
| Projects destination | `<project_db>.dbo.<dest>`, always fully qualified | `PROJECTD33A929.dbo.PKTable` |

The prefix is per project (D50): `temp_prefix` from the template, else the
first (up to) three letters of each word of `project_folder`, lower-cased
(`IBD Ancestry` is `ibdanc`, `Test Run` is `tesrun`, blank is `pull`). A phase
document with no `temp_prefix`, written before D50, keeps `JVM`.

Global temps are instance-wide, so two pulls that both make `Patients` would
collide. When a session opens it asks, for each temp it will create, whether
it already exists (`OBJECT_ID('tempdb..##tesrun_Patients')`). One that does
belongs to a pull running now, since a global temp lives only as long as its
connection. The session then **does not drop it**: it numbers its prefix
(`tesrun2`, `tesrun3`, ... up to 99) until none of its names are taken, and
rewrites every statement it sends to use that. The chosen prefix is recorded
in `session.runtime.temp_prefix`, with a warning when it changed. A dry run
shows the planned names. If the check itself cannot run, the session warns and
uses the planned prefix.

### Sessions, Epochs And Staleness

One Cosmos connection is held open from `setup` through the last run of a
session, because every global temp dies with it. Opening it mints an
**epoch** and captures `SELECT @@SERVERNAME` into `session.runtime`. That
instance name (`et4003vpdsql032`, not the `COSMOS` alias) is what Projects-side
`OPENQUERY` must target, and it **changes on every connection**, so it is always
overwritten, never cached or reused.

| Output | Lives in | Survives the connection |
| --- | --- | --- |
| `##<prefix>_<dest>`, uploaded temps | Cosmos | No |
| `#Local_<dest>` | Projects connection | No |
| `<project_db>.dbo.<dest>`, `upload_<dest>` | Projects database | Yes |

So `done` means "completed once", not "still exists". `is_stale()` is true for a
node completed under an earlier epoch: its **server-side** output is gone. A
manifest with no epochs is never stale. What runs next is decided per session
(Running Again), not from staleness.

### What A Session Does

When the session opens, before anything runs: capture `@@SERVERNAME`, check
the Cosmos refresh date (D51, Running Again), and choose the temp prefix
(Naming).

1. **Setup.** Drop and create every Projects destination, once; runs append.
   Resuming, keep them and create only missing ones (D52). Every destination a
   run fills has a `_batch NVARCHAR(200) NOT NULL` column holding the run's
   batch label (`all` for an unbatched run). The PK's own copy has none, since
   batches are selected from it.
2. **Uploads** (D54). Each lands in Projects first, as `upload_<dest>` with
   its types (Uploads, below), and is committed. Then its Cosmos temp is
   created with the copy's types, read back from `INFORMATION_SCHEMA`, and
   filled from the copy through the client (there is no linked server from
   Cosmos back to Projects). Resuming, the copies are kept and the files are
   not read; a copy that is missing stops the phase, pointing at `--repull`.
3. **PK.** Build `##<prefix>_<pk>` and copy it to Projects (an uploaded PK
   already has its `upload_` copy), then verify uniqueness against the copy
   (`COUNT(*)` against a count of `SELECT DISTINCT keys`; a PK with no key
   column warns instead). Not rerun on a resume.
4. **Runs.** For a batched run, the PK temp is emptied and refilled with that
   batch's whole PK rows, selected from the **Projects copy** with a
   parameterized predicate, then uploaded. The cohort SQL runs unchanged: it
   only ever joins the PK temp. On a resume an unbatched run refills it with
   the whole Projects copy the same way. Each run then:
   - deletes its own label's rows from each destination
     (`DELETE ... WHERE _batch = 'b2of4-LA-Male'`), a separate block, so a run
     that failed after landing some rows lands them exactly once when retried;
   - for each cohort in turn (D55): builds its Cosmos temp and commits, then
     transfers it (`OPENQUERY` into `#Local_<dest>`, then `INSERT` into the
     destination, adding the `_batch` label) and commits, before the next
     cohort is built. A failure loses at most the cohort in flight.

   A chunked run clears once, then for each chunk refills the PK temp with
   `ORDER BY <keys> OFFSET/FETCH` over the Projects copy, rebuilds the cohort
   temps and lands them. A failed chunk fails the run; a retry redoes all of it.

After each run the Cosmos and Projects row counts are compared, counting only
this run's `_batch` rows on the Projects side (a chunked run compares totals),
and a mismatch warns. Counts past 80,000,000 warn. The widest value of each
staged column is measured and reported, not applied (D34). A unit that fails
rolls both connections back. Nothing runs in parallel: sessions, runs, chunks
and cohorts go one after another.

### Uploads

Every upload lands in Projects as `upload_<dest>` before it goes near Cosmos
(D54):

| Upload | Lands in Projects by |
| --- | --- |
| parquet | Read with `pyarrow`, declared columns converted, then bound in with `fast_executemany` into a table created with its types |
| dbtable | `SELECT * INTO upload_<dest> FROM <source_table>`, server-side, types and all |
| csv | Refused: the split converts CSVs; one reaching Pullmanager came from an older split. Export it again |

Parquet types land as: 64-bit integers `BIGINT`, 32-bit `INT`, 8/16-bit
`SMALLINT`, doubles `FLOAT`, decimals `DECIMAL(p,s)`, booleans `BIT`, dates
`DATE`, timestamps `DATETIME2(7)` (a time zone is converted to UTC, with a
note), text `NVARCHAR(longest + 50)`, or `NVARCHAR(MAX)` past 4000. A binary
column is refused. A declared type overrides, and a value that does not fit
it fails, naming the column.

**The copy is the source.** Once landed, the Cosmos temp, an uploaded PK's
batches and every resume or retry read the copy, never the file. So a file
changed after a pull started is not seen until `--repull`, which lands every
upload from its file again; that is the step to take after someone sends a
corrected file.

Rows travel by parameter binding with `fast_executemany`, chunked:

```python
cursor.fast_executemany = True
cursor.executemany("INSERT INTO ##tesrun_ClientPK (PatientDurableKey) VALUES (?)", rows)
```

Not a literal `INSERT ... VALUES` list, which T-SQL caps at 1000 rows. Binding
also removes quote escaping and maps `None` to `NULL`. Chunked because the
driver allocates buffers from declared width times batch size.

### Failure Policy

| Failure | Effect |
| --- | --- |
| A run | Siblings continue: batches are disjoint appends |
| A phase | Everything after it in the session is `blocked` |
| A session | The next session still runs |

One night produces one list of every failure. A partial session still rolls up
to `failed`, so a partial table cannot read as complete.

### Running Again

`--execute` first connects to Cosmos and reads
`SELECT name, create_date FROM sys.databases WHERE name LIKE 'Cosmos%'` (D51).
The manifest records the value for each database its cohorts read, to the
millisecond. If one has changed since the last run, Cosmos was refreshed: it
says so, and **every session starts over**, finished ones included. If the
value cannot be read, it warns that a refresh cannot be detected and carries
on. A session that finds a different value when it opens, because Cosmos was
refreshed during the run, stops with a message to run again.

Then, per session (D52):

| Session | Next `--execute` |
| --- | --- |
| PK phase not done | Starts over: setup drops the destinations, everything runs |
| PK done, some runs not done | Resumes: destinations kept, uploads replayed, PK query not rerun, only unfinished runs pulled |
| PK done, every run done | Skipped without connecting: its tables are complete |

- `--retry-failed` reopens `failed` work. Without it, failed work is excluded
  and the output says so; a session with only failures left is skipped.
- A run left `running` by a crash or Stop is interrupted: it is pulled again,
  its rows cleared first.
- `--repull` starts every session over, finished work included, and lands
  every upload from its file again. A retry never re-reads an upload file
  (Uploads: the copy is the source), so after a changed file, `--repull`.
- The summary (`pullmanager.py <manifest>`) and the dry run say, per session,
  what the next `--execute` will do.

`--resume-partial` is gone (D52, replacing D46).

### Connections

```text
Driver={ODBC Driver 17 for SQL Server};Server=tcp:COSMOS;Database=COSMOS;Trusted_Connection=yes;
Driver={ODBC Driver 17 for SQL Server};Server=tcp:PROJECTS;Database=<project_db>;Trusted_Connection=yes;
```

- Driver 17 defaults to `Encrypt=no`, so no certificate handling. Login timeout
  10s; no query timeout by default.
- Scripts are split on lines equal to `GO`, and every result set is drained
  with `nextset()`.
- `cursor.messages` is read on success and failure alike. That is what surfaces
  the inner error of a failed `OPENQUERY`.
- `autocommit=False`, so the `BEGIN/COMMIT TRANSACTION` inside a transfer
  block nests inside the driver's own transaction rather than committing on
  its own. Pullmanager commits after every block (D55): each cohort's rows,
  each chunk's, and each upload's copy are saved before the next is pulled,
  and a transaction and its locks last one cohort. A unit that fails is rolled
  back.
- Telemetry is read from result sets with declared columns, tied to manifest
  ids. No SQL is ever selected by searching its text.

### Command Line

```bash
pullmanager.py split/pullmanifest.yaml                      # summarize
pullmanager.py --dry-run split/pullmanifest.yaml [--out-dir sql] [-v] [--all] [--retry-failed] [--repull]
pullmanager.py --execute split/pullmanifest.yaml [--retry-failed] [--repull] [--env FILE]
pullmanager.py --gui
pullmanager.py --tdd [module]
```

A dry run renders every SQL block without touching a database or the manifest,
listing why each unit is included and what was excluded, and what each session
will do next.

### The Launcher

`--gui` opens a tkinter window for **running** pulls: choose the transfer YAML,
data dictionary, split folder and SQL folder; then Validate, Export split, Dry
run, Execute, Stop, with "Retry failed" and "Re-pull everything" options
(`--retry-failed`, `--repull`). There is no recipes field and no `--recipes` is
ever passed (D49); settings saved by an older launcher that named one still
load. Output streams into a log tab; a status tab reads the manifest every
three seconds.

It is a front end, not a second implementation. Each button runs the same
command a person would type, as a subprocess, so a long pull cannot freeze the
window and Stop has a real process to end. All logic lives in `launcher.py`,
which has no tkinter in it; `gui.py` only wires widgets. Chosen paths are
remembered in `.pullmanager-gui.json` in the working directory, not inside the
extracted tree.

---

## Testing

Stdlib `unittest` everywhere, so every suite runs unchanged on the VM.

```bash
python3 scripts/makeYaml.py --tdd [group]                  # YAML Manager (110)
python3 scripts/pullmanager_src/pullmanager.py --tdd [mod]  # runtime (315)
python3 scripts/bundle_pullmanager.py --tdd [class]         # bundle (46)
python3 scripts/yamlmanager.py --tdd                        # browser UI (9), Mac only
```

Tests that read or write parquet need `pyarrow` and skip without it: they
cover CSV conversion, uploads, and every session test (the runtime fixture's
upload is parquet).

- **makeYaml** keeps its tests inline, one `TestCase` per `--tdd` group
  (`TEST_GROUPS`), so the file stays self-contained.
- **Runtime** tests live in `pullmanager/tests/test_*.py`, discovered by name,
  and ship in the bundle. After extraction, `--tdd` proves the delivery with no
  network and no repo. Tests needing repo fixtures skip cleanly there.
- **Bundle** tests cover tampering, determinism, extraction safety, `.local`
  preservation (including files a bundle stops shipping), and the VM pathway
  from one copied file: export a transfer YAML on the Mac, extract, split it
  with no recipes present, dry run.
- **Transfer** tests check the outcome: a transfer YAML split alone, with no
  recipes file, gives byte-identical session YAMLs and uploads, and the same
  manifest (bar `source`), as the template split with recipes, for the tiny
  template and for test cases `01` and `02`.
- **UI** tests cover saving a recipe (comments and layout kept, a duplicate
  refused with the file unchanged, the entry placed inside `recipes:`), Save &
  Refresh (named for the project, beside the template, never over it) and the
  section notes.

Fixtures:

| Location | What |
| --- | --- |
| `YAMLs/manager_test_cases/*.yaml` | Realistic templates: `01` and `02` pass, `03` warns, `00`, `04`, `05` fail on purpose |
| `scripts/pullmanager_src/fixtures/split/` | Real `--export-split` output of `01_valid_basic.yaml`, the runtime's specification; its upload is parquet |
| `scripts/pullmanager_src/fixtures/pullmanifest.in-flight.yaml` | That manifest mid-run, produced by driving the real transition API |

Regenerate the split fixture after a change to split output:

```bash
python3 scripts/makeYaml.py --template YAMLs/manager_test_cases/01_valid_basic.yaml \
  --recipes YAMLs/recipes.yaml --export-split --out-dir scripts/pullmanager_src/fixtures/split
```

The database layer is tested against a fake cursor, and the GUI against a fake
tkinter. The fake Projects connection keeps each destination's rows per
`_batch` label, carried across executions like the real database, so retry,
chunk and refresh tests check what landed where. Statements apply in order and
a failure stops at the one it matches, so a run can fail after landing rows.
By default a rollback undoes nothing, the worst case; a transactional mode,
where only committed work survives, checks D55's commit per cohort. It also
answers `INFORMATION_SCHEMA`, `OBJECT_ID` and `SELECT INTO` for uploads.
Neither fake proves the real thing: nothing here has run against Cosmos.
Real Tk is exercised by hand: Tk 9 under Xvfb on the dev box, and Tk 8.6 with
the Mac's `python3.13`, which the VM likely matches.

When adding a feature, add a passing case, a failing case, and a warning case
if it can warn. A bug fix gets a test that fails with the bug reintroduced.
