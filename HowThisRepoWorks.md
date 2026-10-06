# How This Repo Works

Scope turns a description of a data pull, written as YAML, into the SQL that builds it in Cosmos and copies it into a Projects database. It then writes the result out as parquet files. Everything below runs on this machine, from `python scope.py`, which opens one window with two halves:

- **Author** (YAML Manager) writes and checks the description. It never touches a database.
- **Run** (Pullmanager) splits the description into SQL, runs it, and packages the results.

The two halves share no code at run time. Author writes files, and Run reads them.

File paths below are inside `pullmanager_runtime/`.

------------------------------------------------------------------------

## The Layers

Each layer is an abstraction over the one before it.

**1. SQL.** Every table in a pull is one `SELECT` against Cosmos, written into a global temp table (`##<prefix>_<table>`). It is then copied across the linked server into Projects (`<project_db>.dbo.<prefix>_<table>`).

**2. A table as YAML.** The same `SELECT`, written as data. Its `columns` are `source` → `name` pairs, and its `filter` has `from`, `join` and `where` lines. `{{variables}}` are filled in later. `dedup_keys` keeps one row per key.

``` yaml
- name: Patients
  type: PK                          # the pull's patient list; other tables join to it
  dedup_keys: [[PatientDurableKey]]
  dedup_order_by: [IndexDate]       # keep each patient's earliest row
  columns:
    - {source: dxf.PatientDurableKey, name: PatientDurableKey, nullable: false}
    - {source: dxf.StartDateKey,      name: IndexDate,         nullable: true}
  filter:
    from: [DiagnosisEventFact as dxf]
    join: ["INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey"]
    where:
      - "dxf.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
      - "{{sql_condition('dt.Value', ICD_Value)}}"
```

**3. Recipes.** Tables used again and again live in `reference/recipes.yaml`. A pull names one (`recipe: PatientWithDx`) and overrides what it needs to.

**4. The template (an intake).** A whole pull in one file: - project settings (`project_db`, `cosmos_db`, the date window); - upload files; - `multipliers`, which make one copy of the pull per level, such as one per disease; - `batching`, which splits each table into pieces by value or by row count; - `cohorts`, the tables.

`reference/template.yaml` holds the defaults a new one starts from.

**5. YAML Manager.** The Author window edits a template without hand-writing YAML. It checks every column against `reference/datadictionary.yaml`, the Cosmos tables and their column types.

**6. Pullmanager.** The Run window splits a template into SQL, runs it one step at a time, records progress, and resumes after a failure.

**7. Scope.** `scope.py` is the front door to both halves, plus the utilities. `reference/DSVM Plugins.yaml` lists the Python and R packages installed here. The code uses only the standard library and packages on that list.

------------------------------------------------------------------------

## From A Template To A Pull, Step By Step

### 1. Author: write the intake

The Author window keeps a draft, the template as a plain mapping, in `scripts/yamlmanager_model.py` (class `Draft`). Every button calls a `Draft` method, such as `add_prefab`, `add_multiplier`, `add_level` or `set_dedup`. The window itself (`scripts/yamlmanager_tk.py`) only shows the draft and passes edits to it. **Save** (`Draft.save`) writes the draft to `YAMLs/temp/<project>_blueprint.yaml`, the project's one working file.

### 2. Validate: compile the template

Validation is `makeYaml.compile_yaml()` (`scripts/makeYaml.py`); `Draft.validate` calls it. It runs the template through these steps in order:

| Step | Function | What it does |
|------------------------|------------------------|------------------------|
| Normalize | `normalize_template` | Lifts grouped settings (`cosmos_vars`, `run_vars`) to the top level. |
| Recipes | `load_recipes`, `import_recipes` | Merges each named recipe into its cohort, and fills each column's SQL type from the data dictionary. |
| Shape checks | `apply_table_groups`, `check_output_columns`, `check_dedup` | Checks table groups, column names and dedup keys. |
| Multipliers | `expand_multipliers` | Copies each cohort once per level and gives each copy that level's variables. Level `GERD` makes `GERDPtsWithDx` with `ICD_Value: [...]`. |
| Variables and inputs | `validate_and_resolve` | Decides where each `{{variable}}` comes from and binds each table input. Checks upload files. |
| Sessions | `assign_sessions` | Records on each table the PK whose session builds it. |
| Render | `render_cohorts`, `render_string`, `render_sql_condition` | Fills in every `{{variable}}`. A list of codes becomes `IN (...)`, or `LIKE ... OR LIKE ...` when any code has a `%`. |
| SQL checks | `validate_data_dictionary`, `check_sql_references`, `check_table_order` | Checks that every column exists in its Cosmos table, every alias is declared, and every table is built before it is read. |
| Batching | `expand_batching` (`normalize_batching`) | Attaches each batching definition to the tables. |
| Database | `expand_cosmos` (`with_cosmos_suffix`) | `cosmos_db: Dual` copies every table for the SneakPeek database (1% of patients) with a `_sp` suffix. Those copies read each other's temps. |

It returns a `CompileResult`: the finished YAML, plus errors and warnings. Each message carries a fix. An error blocks every later step.

### 3. Blueprint: hand the template to Run

**Transfer to Run** (`Draft.export_transfer`) hands the saved blueprint to the Run window. Multipliers and batching in it are still declarations; nothing is expanded until the split.

A blueprint can name recipes (`recipe: PatientWithDx`), which are read from `reference/recipes.yaml` at each compile. `makeYaml.build_transfer()` (`--export-transfer`) writes a version that stands alone instead: every recipe is written out in full, so the file needs no `recipes.yaml`. It is written only if the template validates.

### 4. Split: plan the sessions

Run's **Export split** calls `makeYaml.write_split_artifacts()`, which compiles the blueprint again (`plan_split_runs` → `compile_yaml`). Then `build_split_plan_from_finished()` turns the expanded tables into a plan:

- **A session** is one PK and every table joined to it. There is one per multiplier level per database. For example, 28 diseases × (Cosmos + SneakPeek) gives 56 sessions. SneakPeek sessions are ordered first.
- **Each session has three phases**: `setup`, `upload_cohorts` and `pk`.
- **Each session has runs**, made by `session_runs()`: one per combination of batch values (`b1of4-LA-Female`), or one per row chunk. `grouped_runs()` repeats every batch once for each table group.

`split_phase_document()` writes one small YAML per phase and run, holding only that step's tables, already rendered. The plan itself is written as `pullmanifest.yaml`: every session, phase and run, each with a `status`.

### 5. Render: YAML to SQL

Pullmanager turns each phase or run into SQL blocks. Nothing searches SQL text: each block is addressed by its place in the manifest.

- **Order of work**: `executor.plan()` → `plan_session()` → `plan_unit()`. `should_execute()` decides what runs, and on a resume what is skipped.
- **Cosmos side** (`pullmanager/server_sql.py`): `render_phase()` → `render_cohort()` writes one table as `DROP` / `CREATE ##temp` / `INSERT ... SELECT`.
  - The `SELECT` comes from `render_select()`, or from `render_dedup_select()`, which wraps it in `ROW_NUMBER() OVER (PARTITION BY <keys> ORDER BY ...)` and keeps row 1.
  - `top_clause()` adds `TOP (n)` for a test sample.
  - `sql.render_source_clause()` and `sql.render_where()` write the `FROM`, `JOIN` and `WHERE`.
- **Projects side** (`pullmanager/local_sql.py`):
  - `render_table_shell()` creates the destination table, with a `_batch` column.
  - `render_delete_batch()` deletes this run's earlier rows, so a retry can never double them.
  - `render_transfer()` copies the temp across: `OPENQUERY` into a local `#Local_` table, then one `INSERT` inside a transaction.

**Preview SQL** (`--dry-run`, `executor.write_sql()`) writes all of it to files without connecting.

### 6. Execute: run it

`pullmanager/session.py`, `SessionRunner.execute()`, runs one session on one held-open Cosmos connection, because a global temp dies with its connection:

1.  `_choose_prefix`, then `_run_setup`. `_check_columns` confirms every column exists in Cosmos, and the destination tables are created.
2.  `_run_uploads`. Upload files go into Projects, then into a Cosmos temp (`pullmanager/uploads.py`).
3.  `_run_pk`. This builds the PK, copies it to Projects, runs `_verify_pk_uniqueness`, takes a control sample if there is one (`_sample_control`), and writes the PK to parquet (`_write_pk_parquet`).
4.  `_run_run` for each run:
    - `_materialize_batch` refills the PK temp with just this batch's patients, selected from the Projects copy (`batches.select_batch_rows`, `batches.chunk_clause`).
    - Each table is then built and landed in turn (`_execute_unit`, `_land`).
    - Row counts are compared (`_check_counts`), and rows per patient are measured (`_measure_run`).

Every step writes its status into `pullmanifest.yaml` as it starts and ends (`manifest.Node.start`, `finish`, `fail`). The Run window's Status tab reads that file, and a re-run picks up where the last one stopped.

### 7. Artifacts: package the results

`pullmanager/artifacts.py`: `plan()` chooses the finished tables, and `package()` reads each from Projects in chunks and writes parquet with pyarrow. `contents.py` writes `contents.md`, describing every table and column, beside the parquets.

------------------------------------------------------------------------

## One Table, End To End

The PatientWithDx recipe, under a `Disease` multiplier level `GERD: ICD_Value: ["530.81", "530.11", "K21%"]`, in a pull whose temp prefix is `gicon`:

``` text
template   cohorts: [{recipe: PatientWithDx, name: PtsWithDx}]
compile    import_recipes → expand_multipliers (GERDPtsWithDx) → render_sql_condition
split      session GERDPtsWithDx (and GERDPtsWithDx_sp), phases setup / upload_cohorts / pk
render     server_sql.render_cohort → render_dedup_select
```

``` sql
CREATE TABLE ##gicon_GERDPtsWithDx ( [PatientDurableKey] BIGINT NOT NULL, ... );

INSERT INTO ##gicon_GERDPtsWithDx (...)
SELECT ... FROM (
    SELECT ROW_NUMBER() OVER (PARTITION BY dxf.PatientDurableKey ORDER BY dxf.StartDateKey) AS [_dedup_rn],
           dxf.PatientDurableKey AS [PatientDurableKey], ...
    FROM dbo.DiagnosisEventFact as dxf
    INNER JOIN dbo.DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey
    ...
    WHERE dxf.StartDateKey BETWEEN 19900101 AND 20260601
      AND dt.Type IN ('ICD-10-AM','ICD-10-CA','ICD-10-CM', 'ICD-9-CM')
      AND (dt.Value LIKE '530.81' OR dt.Value LIKE '530.11' OR dt.Value LIKE 'K21%')
) AS [_deduped]
WHERE [_deduped].[_dedup_rn] = 1;
```

Then `local_sql.render_transfer` lands it in `PROJECTD93A5E7.dbo.gicon_GERDPtsWithDx`, and Artifacts writes `GERDPtsWithDx.parquet`.

------------------------------------------------------------------------

## The Same Steps From A Terminal

``` bash
python pullmanager_runtime/scripts/makeYaml.py --template YAMLs/temp/<project>_blueprint.yaml --validate
python pullmanager_runtime/scripts/makeYaml.py --template YAMLs/temp/<project>_blueprint.yaml --export-split --out-dir runs/<project>
python scope.py --dry-run runs/<project>/pullmanifest.yaml --out-dir runs/<project>/pull_files/sql
python scope.py --execute <project>
python scope.py --artifacts <project>
```