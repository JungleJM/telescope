# Proposed YAML Manager to Pullmanager Flowthrough

This document describes the intended end-to-end workflow for YAML Manager, currently evolving out of Telescope, and its handoff to Pullmanager on the VM side. The goal is to preserve the flexible template/recipe authoring experience while producing execution artifacts that match how server connections, global temp tables, batching, local tables, and final parquet artifacts actually work.

## Core Idea

YAML Manager should no longer think of its only final product as one large expanded YAML. Instead, it should support three related artifacts:

1. **pre-YAML**: the portable authoring YAML before multiplier and batch expansion.
2. **split pull folder**: a folder of standalone-valid YAMLs divided into setup, upload, PK, and run phases.
3. **pullmanifest.yaml**: the authoritative execution/status document consumed and updated by Pullmanager.

The pre-YAML is what can be moved between machines before any server-side data leaves the closed VM environment. The split pull folder is what Pullmanager uses to perform the actual data pulls. The manifest is the step-by-step process document and eventually becomes the status/progress document.

## Roles

### YAML Manager

YAML Manager owns:

- Template creation and editing.
- Recipe browsing and symbolic recipe references.
- YAML validation and table logic checks.
- Optional recipe expansion for inspection.
- pre-YAML export.
- Split pull planning.
- Split YAML generation.
- pullmanifest.yaml generation.
- Optional handoff to Pullmanager once validation passes and the user approves.

YAML Manager does **not** own:

- SQL generation details.
- Server connection management.
- Local database connection management.
- Global temp table lifetime management at runtime.
- Actual PK extraction.
- Actual batch materialization from pulled PKs.
- Final parquet writing.
- Final data dictionary row counts.

### Pullmanager

Pullmanager owns:

- Reading `pullmanifest.yaml`.
- Executing setup/upload/PK/run YAMLs in manifest order.
- Keeping the correct server connection open for a session group.
- Calling generator/makeServer/makeLocal as needed.
- Creating or reusing global temp tables.
- Managing local database tables.
- Pulling PKs.
- Materializing logical batches after PKs are available.
- Updating manifest status as work proceeds.
- Recovering, skipping, retrying, or resuming from manifest status.

### generator.py / makeServer / makeLocal

These remain responsible for turning a YAML into the server and local SQL scripts needed for a pull. YAML Manager should generate YAMLs that these tools can consume, but should not duplicate their SQL-generation logic.

### makeArtifacts

`makeArtifacts` owns final artifact export. It should inspect what was actually pulled and create parquet files plus `parquetcontents.yaml`.

`parquetcontents.yaml` should reflect real completed data, not merely planned data. This matters when a later batch fails but earlier batches completed successfully.

## pre-YAML

The pre-YAML should be very close to the current `template.yaml` shape. It should remain portable and copyable.

It should include:

- Project metadata.
- `cosmos_vars`.
- `run_vars`.
- `test_options`.
- `project_vars`.
- `multipliers`.
- `batching`.
- `upload_cohorts`.
- `cohorts`.
- Symbolic recipe references.

By default, pre-YAML should **not** normalize or aggressively rewrite the author's template. It should remain close to what the user created.

YAML Manager may also provide an optional expanded-recipes pre-YAML preview/export. In that mode, recipe references can be translated into explicit cohort definitions for inspection. This should be an option, not the default, because symbolic recipe references are useful and portable.

Example CLI shape:

```bash
python scripts/yamlmanager.py --export-preyaml symbolic --out dist/preyaml.yaml
python scripts/yamlmanager.py --export-preyaml expanded-recipes --out dist/preyaml.expanded.yaml
```

## Full Split Output

Full split output applies multiplier and batching logic into a folder of execution YAMLs. Once a YAML has been split, the original `multipliers` and `batching` sections should not remain as active expansion instructions. Otherwise Pullmanager or generator could accidentally interpret them twice.

Each split YAML should still be standalone-valid. It should preserve the same general metadata shape above the cohort sections so that each YAML can be understood as a single pull unit.

Each split YAML should also include a `pull_context` block describing its role.

Example:

```yaml
pull_context:
  session_id: MainCohort__UC
  run_id: MainCohort__UC__batch-sex-female
  phase: run
  cohort: MainCohort
  multiplier:
    name: UC
    values:
      condition: UC
  batch:
    name: sex-female
    logic:
      sex: Female
```

The exact contents can evolve, but the purpose is stable: a split YAML should explain where it belongs in the planned pull.

## Session Model

The manifest should distinguish **session groups** from **runs**.

A session group is the scope where the server connection must remain open because global temp tables disappear when that connection closes.

For a multiplied cohort, the multiplier-derived cohort is the natural session group. For example, if the main cohort is multiplied into UC and Crohns cohorts, then the UC cohort and all of its batches should run under one server session, and the Crohns cohort and all of its batches should run under another.

The multiplier metadata may be important at the session level, but the individual run YAMLs do not necessarily need to carry much multiplier detail beyond identifiers and enough context to be standalone-valid.

## Standard Session Routine

Every session should follow the same routine, even when there is no multiplier and/or no batching. Uniformity makes Pullmanager simpler and makes restart/resume behavior easier.

Each session should have:

1. `setup.yaml`
2. `upload_cohorts.yaml`
3. `pk.yaml`
4. one or more run YAMLs

This applies to:

- no multiplier, no batching
- multiplier only
- batching only
- multiplier plus batching

### setup.yaml

`setup.yaml` establishes the session. It should contain enough metadata and instructions for Pullmanager/generator to:

- Open or initialize the server-side session.
- Establish expected global temp table naming/context.
- Establish expected local table naming/context.
- Create empty local table shells if needed.
- Prepare for uploads and PK pull.

It should not pull fact tables.

### upload_cohorts.yaml

`upload_cohorts.yaml` handles upload cohorts for the session.

Uploads belong in this phase because uploaded/global temp resources are session-scoped. Once the session closes, they should be assumed gone. Therefore uploads should occur once per session, not once per batch run.

The upload YAML should be standalone-valid and should repeat the necessary metadata to make sense on its own.

### pk.yaml

`pk.yaml` pulls or constructs the full PK table for the session.

This is required before batch materialization. Even when there is no batching, the PK phase still exists so every session follows the same algorithm.

For batched pulls, Pullmanager uses the PK output to determine actual batch membership. YAML Manager keeps batch definitions logical; Pullmanager turns those logical definitions into actual key sets after PKs exist on the VM side.

### run YAMLs

Run YAMLs pull the non-PK cohort tables/fact tables for a specific logical unit.

With no batching, a session may have one run YAML:

```text
run.yaml
```

With batching, a session has one run YAML per batch:

```text
runs/batch-sex-female.yaml
runs/batch-sex-male.yaml
runs/batch-other.yaml
```

Each run YAML should assume setup, uploads, and PK have already happened for that session. It should still be standalone-valid as a document, but execution order comes from the manifest.

## Suggested Folder Shape

Example with multiplier plus batching:

```text
yamlmanager-output/
  preyaml.yaml
  preyaml.expanded.yaml
  split/
    pullmanifest.yaml
    sessions/
      MainCohort__UC/
        setup.yaml
        upload_cohorts.yaml
        pk.yaml
        runs/
          batch-sex-female.yaml
          batch-sex-male.yaml
          batch-other.yaml
      MainCohort__Crohns/
        setup.yaml
        upload_cohorts.yaml
        pk.yaml
        runs/
          batch-sex-female.yaml
          batch-sex-male.yaml
          batch-other.yaml
```

Example with multiplier only:

```text
split/
  pullmanifest.yaml
  sessions/
    MainCohort__UC/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        run.yaml
    MainCohort__Crohns/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        run.yaml
```

Example with no multiplier and no batching:

```text
split/
  pullmanifest.yaml
  sessions/
    MainCohort/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        run.yaml
```

## pullmanifest.yaml

`pullmanifest.yaml` is the document Pullmanager consumes. Pullmanager should not infer execution order from folder names. The manifest is authoritative.

The manifest should include:

- Manifest schema/version.
- Project metadata.
- Source pre-YAML path/hash if useful.
- Session list.
- Ordered phases per session.
- Ordered run list per session.
- Stable IDs for every session and run.
- Paths to YAML files.
- Initial statuses.
- Space for Pullmanager to write progress/status.

Example:

```yaml
manifest_version: 1
project:
  name: MyProject
  created_by: yamlmanager
source:
  preyaml: ../preyaml.yaml
sessions:
  - session_id: MainCohort__UC
    cohort: MainCohort
    multiplier:
      name: UC
    status: pending
    phases:
      setup:
        yaml: sessions/MainCohort__UC/setup.yaml
        status: pending
      upload_cohorts:
        yaml: sessions/MainCohort__UC/upload_cohorts.yaml
        status: pending
      pk:
        yaml: sessions/MainCohort__UC/pk.yaml
        status: pending
        rows: null
    runs:
      - run_id: MainCohort__UC__batch-sex-female
        yaml: sessions/MainCohort__UC/runs/batch-sex-female.yaml
        batch:
          name: sex-female
        status: pending
        rows: null
      - run_id: MainCohort__UC__batch-sex-male
        yaml: sessions/MainCohort__UC/runs/batch-sex-male.yaml
        batch:
          name: sex-male
        status: pending
        rows: null
```

## Manifest as Status Document

The manifest should become the status document once Pullmanager starts running. YAML Manager creates it with `pending` statuses. Pullmanager updates it over time.

Suggested statuses:

- `pending`: planned but not started.
- `running`: currently being executed.
- `done`: completed successfully.
- `failed`: attempted and failed.
- `skipped`: intentionally not run, often because a dependency failed.
- `blocked`: cannot run until an upstream problem is resolved.

Suggested runtime fields:

```yaml
status: done
started_at: "2026-09-20T14:03:00-05:00"
finished_at: "2026-09-20T14:08:32-05:00"
rows: 12345
outputs:
  local_table: my_local_table
  parquet: null
error:
  message: null
  detail: null
```

Pullmanager can update phase/run statuses as it goes. YAML Manager can later read the same manifest for progress display.

## Batch Ownership

YAML Manager should keep batches logical.

For example:

```yaml
batch:
  name: sex-female
  logic:
    sex: Female
```

YAML Manager should not try to pre-materialize actual PK values, because those keys are only available after `pk.yaml` runs inside the VM/server environment.

Pullmanager should:

1. Run setup.
2. Run uploads.
3. Run PK.
4. Use the pulled PK table plus logical batch instructions to determine batch key sets.
5. Run each batch-specific pull.

Depending on what is easiest for SQL generation, Pullmanager may create mini global temp tables per batch, use offset/window logic, or use logical filters directly.

## Authoring to Pulling Walkthrough

### 1. User Has an Idea

The user starts with a research/data pull idea: a cohort, supporting tables, optional upload cohorts, optional multiplier groups, and optional batching needs.

Example concept:

- Main cohort: patients with IBD.
- Multipliers: UC and Crohns.
- Batching: sex-based batches plus an all-other behavior.
- Fact tables: encounters, diagnoses, labs.
- Uploads: local list of codes or custom input table.

### 2. User Builds a Template

The user works in YAML Manager.

They can:

- Edit project metadata.
- Define variables.
- Reference recipes symbolically.
- Add custom cohorts/tables.
- Add upload cohorts.
- Add multipliers.
- Add batching.
- Preview inferred dependencies and table logic.

At this stage, the working document remains a pre-YAML-like template.

### 3. YAML Manager Validates

YAML Manager validates internal logic:

- Required variables.
- Recipe references.
- Upload cohort references.
- Table input/output compatibility.
- PK table expectations.
- Column requirements where known.
- Multiplier and batching definitions.

It does not test live server/local DB connections.

### 4. User Exports pre-YAML

Once the user is comfortable, YAML Manager exports:

```text
preyaml.yaml
```

Optionally it can also export:

```text
preyaml.expanded.yaml
```

The symbolic pre-YAML is the primary portable artifact. The expanded-recipes version is for inspection.

### 5. User Copies pre-YAML to VM

The pre-YAML can be copied to the closed server VM because it contains instructions and metadata, not pulled data.

On the VM, YAML Manager or its backend can validate the same pre-YAML again. This confirms the VM-side copy has the expected recipes, paths, and logic.

### 6. YAML Manager Builds Split Output

On approval, YAML Manager creates the split folder:

```text
split/
  pullmanifest.yaml
  sessions/
    ...
```

It applies multiplier and batching structure into planned sessions and runs.

It removes active multiplier/batching expansion instructions from individual split YAMLs and replaces them with resolved session/run context.

### 7. User Inspects Split Output

The user can inspect:

- The manifest.
- Session list.
- Setup/upload/PK/run YAMLs.
- Expected session boundaries.
- Expected batches.
- Readable IDs and file names.

YAML Manager UI should eventually provide a tab for this, but CLI output is fine first.

### 8. YAML Manager Hands Off to Pullmanager

Initially, handoff can be manual:

```bash
pullmanager split/pullmanifest.yaml
```

Later, YAML Manager can call Pullmanager directly after approval.

The handoff boundary remains file-based. Pullmanager consumes the manifest and YAML files.

### 9. Pullmanager Runs a Session

For each session in manifest order, Pullmanager:

1. Opens the server connection.
2. Runs `setup.yaml`.
3. Runs `upload_cohorts.yaml`.
4. Runs `pk.yaml`.
5. Materializes logical batch definitions from the pulled PK table.
6. Runs each run YAML in order.
7. Updates manifest statuses and row counts.
8. Closes the server connection only after the session is complete or terminally failed.

The server connection remains open for all phases/runs that depend on the session's global temp tables.

### 10. Pullmanager Handles Failures

If a run fails, Pullmanager updates the manifest.

Possible behavior:

- Mark the failed run as `failed`.
- Mark downstream dependent runs as `blocked` or leave independent runs `pending`.
- Preserve completed run statuses and row counts.
- Allow retry/resume from the manifest.

Because the manifest is the status document, YAML Manager can later show progress and failure points without directly managing database connections.

### 11. makeArtifacts Exports Final Data

After Pullmanager completes enough data pulls, `makeArtifacts` reads what actually exists in the local database.

It creates:

- Parquet files.
- `parquetcontents.yaml`.

`parquetcontents.yaml` should include:

- Cohort/table name.
- Description.
- Total rows actually exported.
- Columns.
- Column names.
- Column descriptions.
- Column types.

It should not rely solely on planned YAML because partial completion is possible.

## Implementation Direction

The practical refactor should happen in backend-first layers:

1. Rename Telescope-facing scripts/concepts to YAML Manager.
2. Keep a stable Python backend adapter for frontends.
3. Add `build_preyaml()` support.
4. Add optional expanded-recipes pre-YAML export.
5. Add split planning data structures.
6. Add manifest generation.
7. Add split YAML writing.
8. Add CLI commands.
9. Add UI inspection tabs.
10. Add optional Pullmanager handoff.

Suggested CLI shape:

```bash
python scripts/yamlmanager.py --export-preyaml symbolic --out dist/preyaml.yaml
python scripts/yamlmanager.py --export-preyaml expanded-recipes --out dist/preyaml.expanded.yaml
python scripts/yamlmanager.py --export-split --template dist/preyaml.yaml --out-dir dist/split
```

Eventually:

```bash
python scripts/yamlmanager.py --handoff dist/split/pullmanifest.yaml
```

## Design Defaults

Recommended defaults unless future implementation pressure says otherwise:

- pre-YAML symbolic by default.
- Expanded-recipes pre-YAML optional.
- Full split output always includes manifest.
- Pullmanager executes from manifest, not folder inference.
- Every session has setup, upload, PK, and run phases.
- Every split YAML is standalone-valid.
- Upload cohorts are session setup resources, not per-batch resources.
- Batches remain logical in YAML Manager.
- Pullmanager materializes actual PK batches.
- Manifest status is mutable and owned by Pullmanager once execution begins.
- `parquetcontents.yaml` is generated by `makeArtifacts` from actual outputs.
