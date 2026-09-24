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
| Database | `pyodbc` 5.3.0, ODBC Driver 17 for SQL Server | none reachable, no driver | none reachable, no driver |
| YAML | `ruamel.yaml` 0.17.17, `pyyaml` 6.0.3 | brew's `python3` has both; `python3.13` needs them installed (below) | whatever is installed; Ruby fallback |

On the Mac, use `python3.13` for anything run on the VM (the launcher, the
runtime tests) so it meets the VM's Python and Tk, not brew's. It needs the
VM's YAML packages, for all users:

```bash
sudo /usr/local/bin/python3.13 -m pip install ruamel.yaml==0.17.17 pyyaml==6.0.3
```

Without them the runtime cannot read YAML at all (`No YAML backend available`);
`makeYaml` alone falls back to Ruby.

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
INNER JOIN ##JVM_{{HospitalICDTable}} AS hic ON ...
```

YAML Manager infers from this that the recipe has a **table input**
`HospitalICDTable`, and which columns it reads through the alias (`hic.X`).

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

An upload whose schema cannot be known (`dbtable`, or `parquet` with no declared
columns) is listed separately as a table that *may* fit. An ordinary missing
scalar variable is `missing_variable`.

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
- Upload files exist; CSV headers carry the required columns.
- Zero or one upload cohort is `type: pk`, and it declares `key_columns`.
- Multiplier definitions are well formed; dimension labels do not collide.
- Batching definitions, field by field, since a transfer YAML writes them out
  in full: a known `kind`; `column_values` has a `column` on the PK and a
  non-empty `values` list; `row_chunk` has a positive `rows_per_batch` (the
  shipped `chunk` recipe's `required` placeholder is refused). `values: all`
  warns that the pull will stop at it, and any `row_chunk` warns that it pulls
  only the first chunk (roadmap, Known Bugs).
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
  is the unit to copy or archive.
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
        LA-Female.yaml         or one per batch combination
```

A **session** is the scope in which one Cosmos connection stays open, because
global temps die with it. A multiplier produces one session per multiplied
cohort; otherwise there is one.

Every session has the same routine, batched or not:

| Phase | Does |
| --- | --- |
| `setup` | Creates the Projects destination tables (drop and create) |
| `upload_cohorts` | Uploads every upload cohort into `##JVM_<dest>`, once per session |
| `pk` | Builds the PK table, or registers an uploaded one, and copies it to Projects |
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
of building one.

### Batching

Batch dimensions **cross-multiply**. They partition the cohort; they are not
alternative slicings of it:

```yaml
batching:
  - state: {values: [LA, MS]}
  - sex                          # Female, Male
  - chunk: 2000
```

`state × sex` is four runs: `LA-Female`, `LA-Male`, `MS-Female`, `MS-Male`.

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
**Known bug:** Pullmanager currently pulls only the first chunk of each run and
drops the rest silently. Do not use `chunk:` until it is fixed (roadmap).
Two dimensions that would produce the same run name are an error, since a lost
combination means patients silently not pulled.

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
  created_by: yamlmanager
source:
  template: <path>
  recipes: <path>
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
  name: LA-Female
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

# on a session
runtime:
  epoch: "20260922T140000-3f2a9c11"
  opened_at: "2026-09-22T14:00:00-05:00"
  linked_server: et4003vpdsql032
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

Invariants, not preferences (`naming.py`):

| Thing | Rule | Example |
| --- | --- | --- |
| Cosmos global temp | `##JVM_<dest>`; a `JVM_` prefix is stripped first, never doubled | `##JVM_PKTable` |
| Projects staging | `#Local_<dest>` | `#Local_PKTable` |
| Projects destination | `<project_db>.dbo.<dest>`, always fully qualified | `PROJECTD33A929.dbo.PKTable` |

Global temps are instance-wide. Two pulls running at once with the same
`dest_table` collide. D50 replaces `##JVM_` with a per-project prefix (roadmap,
fix 4).

### Sessions, Epochs And Staleness

One Cosmos connection is held open from `setup` through the last run of a
session, because every `##JVM_` table dies with it. Opening it mints an
**epoch** and captures `SELECT @@SERVERNAME` into `session.runtime`. That
instance name (`et4003vpdsql032`, not the `COSMOS` alias) is what Projects-side
`OPENQUERY` must target, and it **changes on every connection**, so it is always
overwritten, never cached or reused.

| Output | Lives in | Survives the connection |
| --- | --- | --- |
| `##JVM_<dest>`, uploaded temps | Cosmos | No |
| `#Local_<dest>` | Projects connection | No |
| `<project_db>.dbo.<dest>` | Projects database | Yes |

So `done` means "completed once", not "still exists". `is_stale()` is true for a
node completed under an earlier epoch: its **server-side** output is gone. A
manifest with no epochs is never stale.

### What A Session Does

1. **Setup.** Drop and create every Projects destination, once. Runs append.
2. **Uploads.** CSV and `dbtable` cohorts go up through the client (there is no
   linked server from Cosmos back to Projects). `parquet` is refused: Cosmos
   cannot read it. Column widths are measured from the data, plus 50.
3. **PK.** Build `##JVM_<pk>` (or register the uploaded one), verify uniqueness
   (`COUNT(*)` against `COUNT(DISTINCT keys)`; a PK with no key column warns
   instead), and copy it to Projects.
4. **Runs.** For a batched run, `##JVM_<pk>` is emptied and refilled with that
   batch's whole PK rows, selected from the **Projects copy** with a
   parameterized predicate and `ORDER BY <keys> OFFSET/FETCH`, then uploaded.
   The cohort SQL runs unchanged: it only ever joins `##JVM_<pk>`. Each run
   then transfers: `OPENQUERY` into `#Local_<dest>`, then `INSERT` into the
   destination inside a transaction.

After each transfer the Cosmos and Projects row counts are compared, and a
mismatch warns. Counts past 80,000,000 warn. The widest value of each staged
column is measured and reported, not applied (D34).

### Uploads

Parameter binding with `fast_executemany`, chunked:

```python
cursor.fast_executemany = True
cursor.executemany("INSERT INTO ##JVM_ClientPK (PatientDurableKey) VALUES (?)", rows)
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

Running `--execute` again on a manifest replays **every session in full,
finished ones included**: each session opens a new connection, which makes
every `done` node stale, so setup drops the destinations and every run pulls
again. Always correct; costs time. To re-pull only some sessions today, run a
manifest containing only those. Skipping finished sessions is on the roadmap.

- `--retry-failed` reopens `failed` work. Without it, failed work is excluded
  and the output says so, because rebuilding a session around a failed unit
  would transfer nothing.
- A node left `running` by a crash or Stop is treated as interrupted and
  replayed. SQL Server rolls back the open transaction when the connection
  drops.
- `--resume-partial` is **refused**. As built it would have lost data (D46).

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
- `autocommit=False`, so each block is one transaction.
- Telemetry is read from result sets with declared columns, tied to manifest
  ids. No SQL is ever selected by searching its text.

### Command Line

```bash
pullmanager.py split/pullmanifest.yaml                      # summarize
pullmanager.py --dry-run split/pullmanifest.yaml [--out-dir sql] [-v] [--all]
pullmanager.py --execute split/pullmanifest.yaml [--retry-failed] [--env FILE]
pullmanager.py --gui
pullmanager.py --tdd [module]
```

A dry run renders every SQL block without touching a database or the manifest,
listing why each unit is included and what was excluded.

### The Launcher

`--gui` opens a tkinter window for **running** pulls: choose the transfer YAML,
data dictionary, split folder and SQL folder; then Validate, Export split, Dry
run, Execute, Stop. There is no recipes field and no `--recipes` is ever passed
(D49); settings saved by an older launcher that named one still load. Output streams into a log tab; a status tab reads the
manifest every three seconds.

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
python3 scripts/makeYaml.py --tdd [group]                  # YAML Manager (85)
python3 scripts/pullmanager_src/pullmanager.py --tdd [mod]  # runtime (293)
python3 scripts/bundle_pullmanager.py --tdd [class]         # bundle (46)
```

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
  recipes file, gives byte-identical session YAMLs and the same manifest (bar
  `source`) as the template split with recipes, for the tiny template and for
  test cases `01` and `02`.

Fixtures:

| Location | What |
| --- | --- |
| `YAMLs/manager_test_cases/*.yaml` | Realistic templates: `01` and `02` pass, `03` warns, `00`, `04`, `05` fail on purpose |
| `scripts/pullmanager_src/fixtures/split/` | Real `--export-split` output of `01_valid_basic.yaml`, the runtime's specification |
| `scripts/pullmanager_src/fixtures/pullmanifest.in-flight.yaml` | That manifest mid-run, produced by driving the real transition API |

Regenerate the split fixture after a change to split output:

```bash
python3 scripts/makeYaml.py --template YAMLs/manager_test_cases/01_valid_basic.yaml \
  --recipes YAMLs/recipes.yaml --export-split --out-dir scripts/pullmanager_src/fixtures/split
```

The database layer is tested against a fake cursor, and the GUI against a fake
tkinter. Neither proves the real thing: nothing here has run against Cosmos.
Real Tk is exercised by hand: Tk 9 under Xvfb on the dev box, and Tk 8.6 with
the Mac's `python3.13`, which the VM likely matches.

When adding a feature, add a passing case, a failing case, and a warning case
if it can warn. A bug fix gets a test that fails with the bug reintroduced.
