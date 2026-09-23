# Telescope Design

What the system is and the contracts each part keeps. Three documents, one job
each, so a fact lives in exactly one place:

| Document | Answers |
| --- | --- |
| `design.md` | What it is, and how it behaves. Describes the code as it stands. |
| `decisions.md` | Why, what was rejected, and what it cost. Append-only, numbered. |
| `roadmap.md` | What is unbuilt, unverified, or undecided. The only place status lives. |

When code and this document disagree, one of them is a bug. Fix whichever is
wrong in the same commit. `old_generator/` is history and can be deleted.

---

## The Pipeline

```text
template.yaml ─┐
recipes.yaml ──┼─► makeYaml ──► split/               ──► Pullmanager ──► <project_db>.dbo.<dest>
datadictionary ┘   validate      pullmanifest.yaml       execute          (artifacts: not built)
                   split         sessions/...            update manifest
```

**YAML Manager** (`scripts/makeYaml.py`, UI `scripts/yamlmanager.py`) owns
authoring: templates, recipes, validation, and planning the pull into a split
folder plus `pullmanifest.yaml`. It never connects to a database.

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

- **Mac**: development, and most YAML authoring, in the browser UI.
- **VM**: air-gapped Windows, the only place Cosmos and Projects are reachable.
  Runs pulls. Cannot pull from git, cannot load a browser page served by Python
  or opened from disk, and has tkinter but no other Python desktop toolkit.

### The Bundle

Everything reaches the VM as one self-extracting file,
`dist/pullmanager_bundle.py`, built by `scripts/bundle_pullmanager.py` from
`scripts/bundle_extractor.py` (the prelude) plus every file it carries.

It carries the whole unit, not just the runtime:

```text
telescope/                          # the extracted tree
  pullmanager.py                    Pullmanager entry point
  pullmanager/                      runtime package and its tests
  scripts/makeYaml.py               compiler, validator, splitter
  scripts/yamlmanager.py            the browser UI
  scripts/yamlmanager_backend.py
  YAMLs/recipes.yaml
  YAMLs/datadictionary.yaml
  YAMLs/template.yaml.example       copy and rename to start a pull
  .bundle-manifest.json
```

Published paths reproduce the repo's `scripts/` beside `YAMLs/` shape, so
`makeYaml` and `yamlmanager` find their own recipes and dictionary with no
flags and no knowledge that they were bundled.

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

Every bundled file is replaced on re-extraction. One that was modified locally
is first set aside as `<name>.local` and reported:

```text
replaced   YAMLs/datadictionary.yaml  (your previous copy saved as YAMLs/datadictionary.yaml.local)
```

Nothing a user authors is bundled. `template.yaml` ships as
`template.yaml.example` so improvements keep arriving without any chance of
overwriting a real template.

### No Configuration

No `.env` ships and none is needed. Both hosts are DNS aliases with defaults
(`COSMOS`, `PROJECTS`), authentication is Windows-integrated, and database names
come from the manifest. `COSMOS` versus `COSMOS_SneakPeek` versus `Dual` is a
template setting. `PULLMANAGER_*` environment variables, or a `.env` passed with
`--env` or found in the working directory, override if a host ever changes.

### The Whole Pathway On The VM

```bash
python pullmanager_bundle.py --verify-bundle
python pullmanager_bundle.py --extract ./telescope
python telescope/pullmanager.py --tdd                        # prove the delivery

python telescope/pullmanager.py --gui                        # desktop launcher

# or the same steps by hand
python telescope/scripts/makeYaml.py --template mypull.yaml --export-split --out-dir ./split
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
| `YAMLs/recipes.yaml` | Reusable cohort definitions, referenced by name |
| `YAMLs/datadictionary.yaml` | Source of truth for Cosmos tables, columns and types |

Paths typed on the command line (`--template`, `--recipes`,
`--datadictionary`) resolve from the working directory, like any command-line
tool. Defaults resolve from the install. `--datadictionary` is honoured by every
route: validation, the UI, pre-YAML and split export. A template that does not
exist is a one-line error, not a traceback.

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

Checks:

- Required variables and table inputs are bound (above).
- Recipe references resolve.
- Upload files exist; CSV headers carry the required columns.
- Zero or one upload cohort is `type: pk`, and it declares `key_columns`.
- Multiplier and batching definitions are well formed; dimension labels do not
  collide.
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

- **pre-YAML** (`--export-preyaml symbolic`): the template, portable, close to
  what was authored, recipe references left symbolic. `expanded-recipes` inlines
  the recipes, for inspection only. Upload paths stay relative to where it was
  written, so moving one means moving its uploads too.
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

It is the Mac's authoring tool. The VM cannot load it; the VM uses the launcher.

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
Two dimensions that would produce the same run name are an error, since a lost
combination means patients silently not pulled.

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
`dest_table` collide; see the roadmap.

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

Running `--execute` again on a manifest replays each unfinished session **in
full**: a new connection makes every server-side phase stale, setup drops the
destinations, and every run pulls again. Always correct; costs time, which is
cheap overnight.

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

`--gui` opens a tkinter window for **running** pulls: choose template, recipes,
data dictionary, split folder and SQL folder; then Validate, Export split, Dry
run, Execute, Stop. Output streams into a log tab; a status tab reads the
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
python3 scripts/makeYaml.py --tdd [group]                  # YAML Manager (63)
python3 scripts/pullmanager_src/pullmanager.py --tdd [mod]  # runtime (291)
python3 scripts/bundle_pullmanager.py --tdd [class]         # bundle (41)
```

- **makeYaml** keeps its tests inline, one `TestCase` per `--tdd` group
  (`TEST_GROUPS`), so the file stays self-contained.
- **Runtime** tests live in `pullmanager/tests/test_*.py`, discovered by name,
  and ship in the bundle. After extraction, `--tdd` proves the delivery with no
  network and no repo. Tests needing repo fixtures skip cleanly there.
- **Bundle** tests cover tampering, determinism, extraction safety, `.local`
  preservation, and the whole pathway from one copied file: extract, export a
  split, dry run.

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
tkinter. Neither proves the real thing: nothing here has run against Cosmos,
and Tk itself is exercised by hand (Tk 9 under Xvfb on the dev box; the VM
likely has Tk 8.6).

When adding a feature, add a passing case, a failing case, and a warning case
if it can warn. A bug fix gets a test that fails with the bug reintroduced.
