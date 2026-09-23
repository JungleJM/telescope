# Pullmanager Refactor Plan

> **Status:** Phases 0-3 are implemented. The verified schema, naming rules,
> and open questions now live in `pullmanager_contracts.md`, which corrects
> several details this plan and `yamlmanagerDesign.qmd` got wrong. Read that
> first; this document remains the phase roadmap.

This document turns the old generator lessons into a new Pullmanager build plan. The goal is not to port the old program line by line. The goal is to preserve the hard-earned behaviors while building a cleaner manifest-driven runner.

## Core Direction

Pullmanager should consume the split folder created by YAML Manager:

```text
split/
  pullmanifest.yaml
  sessions/
    <session>/
      setup.yaml
      upload_cohorts.yaml
      pk.yaml
      runs/
        <run>.yaml
```

Pullmanager owns execution and status:

- Read `pullmanifest.yaml`.
- Execute sessions in manifest order.
- Keep the Cosmos connection open for a whole session, because global temp
  tables die with it.
- Run setup, uploads, PK, and run phases.
- Render server SQL and local transfer SQL from the phase YAML. No separate
  generator exists to delegate this to; Pullmanager owns it.
- Execute SQL through adapters.
- Update manifest status after each phase/table/run.
- Support retry/resume from manifest state.

YAML Manager remains the planner and authoring tool. Pullmanager is the
VM-side executor.

Settled contracts, verified schema and open questions live in
`pullmanager_contracts.md`. This document is the phase roadmap.

## Portability Constraint

The VM cannot pull from git or synchronize with the Mac repo. Code is moved manually from Mac to VM.

Therefore the runtime artifact should be easy to copy and hard to partially update incorrectly.

The preferred approach is:

- Develop as clean source modules on the Mac.
- Bundle those modules into one portable VM file.
- Copy only the bundle to the VM.
- Extract the bundle deterministically on the VM.
- Run the extracted module tree.

This gives us modular development without requiring manual multi-file updates on the VM.

## Bundle Design

The bundle should be a self-extracting source archive, not a minified giant script.

Example command shape:

```bash
python pullmanager_bundle.py --verify-bundle
python pullmanager_bundle.py --extract ./pullmanager_runtime
python ./pullmanager_runtime/pullmanager.py split/pullmanifest.yaml
```

Possible later convenience:

```bash
python pullmanager_bundle.py --extract-and-run split/pullmanifest.yaml
```

### Bundle Contents

Development source can live in something like:

```text
scripts/pullmanager_src/
  pullmanager/
    __init__.py
    cli.py
    manifest.py
    models.py
    naming.py
    normalize.py
    server_sql.py
    local_sql.py
    telemetry.py
    executor.py
    yaml_io.py
  tests/
  bundle_pullmanager.py
```

The generated bundle can live at:

```text
dist/pullmanager_bundle.py
```

The VM extraction output can be:

```text
pullmanager_runtime/
  pullmanager.py
  pullmanager/
    ...
  .bundle-manifest.json
```

### Deterministic Cut Marks

The bundle should contain exact file markers:

```text
# === BEGIN FILE: pullmanager/models.py SHA256: <hash> SIZE: <bytes> ===
...
# === END FILE: pullmanager/models.py ===
```

The extractor should:

- Parse only exact begin/end markers.
- Refuse duplicate paths.
- Refuse missing files from the embedded manifest.
- Refuse extra embedded file sections not listed in the manifest.
- Refuse absolute paths.
- Refuse paths containing `..`.
- Verify file size.
- Verify SHA-256 hash.
- Extract to a temporary folder first.
- Replace the target folder only after all files verify.
- Write `.bundle-manifest.json`.
- Print every extracted file.

This is the lock-and-key system: the manifest says what the bundle should contain, and the cut marks plus hashes prove the extracted files are exactly those files.

### VM-Side Patch Workflow

If a VM-side bug is found in one extracted file, the user can manually copy that specific file's text back into the Mac repo context for repair. We then patch the source module, regenerate the bundle, and the fix is incorporated cleanly.

This avoids treating the bundle as the only source of truth.

## Telemetry Contract

Pullmanager should write structured telemetry into `pullmanifest.yaml`. Console output can exist, but the manifest is the authoritative status document.

Every phase/run should support:

```yaml
status: done
started_at: "2026-09-22T14:03:00-05:00"
finished_at: "2026-09-22T14:08:32-05:00"
duration:
  seconds: 332
  display: "5m 32s"
rows: 12345
error:
  message: null
  detail: null
```

For table-level work, Pullmanager should also support per-table telemetry:

```yaml
tables:
  - dest_table: PKTable
    global_temp: "##JVM_PKTable"
    local_table: "PROJECTD93A5E7.dbo.PKTable"
    status: done
    started_at: "2026-09-22T14:03:00-05:00"
    finished_at: "2026-09-22T14:05:12-05:00"
    duration:
      seconds: 132
      display: "2m 12s"
    rows:
      cosmos: 1000
      projects: 1000
```

This should answer:

- How long did each table take from start to saved in the global temp table?
- If execution is grouped by cohort, how long did that cohort group take?
- How long did the Projects/local transfer take?
- Did Cosmos and Projects row counts match?

This can be implemented in layers. Phase-level timing can come first; table-level timing can follow once the execution renderer exposes table boundaries cleanly.

## Old Lessons To Preserve

The new system should preserve these behaviors as explicit tests/contracts:

- `##JVM_<dest_table>` global temp naming.
- `#Local_<dest_table>` Projects staging naming.
- Fully qualified local destination tables: `<project_db>.dbo.<dest_table>`.
- Runtime linked-server/Cosmos instance discovery.
- Date placeholder substitution.
- Non-null source filters for non-null output columns.
- Grouped `WHERE` fragments without bad `AND` insertion.
- Legacy `dedup_key` normalization or loud failure.
- Canonical `dedup_keys` support.
- Explicit dedup staging/validation.
- CSV upload path handling and header normalization.
- Uploaded PK validation.
- Multiple PK/support tables with exactly one canonical PK.
- Dependencies between generated temp tables represented structurally, not by handwritten temp names.
- Cosmos and Projects row counts captured separately.
- Row-count mismatch warnings.
- Large row-count risk warnings.
- Fail-fast behavior on SQL execution errors with server messages preserved.

## What Not To Preserve

These old patterns should not drive the new architecture:

- One giant `Cosmos.sql` plus one giant `Projects.sql`.
- Substring matching against Projects SQL to decide what to execute.
- A single generator script supervising every stage.
- Verbosity as a major cross-cutting feature.
- Markdown reports as the main status artifact.
- Hardcoded output mirroring as a substitute for manifest paths.
- Hidden fallback behavior that silently changes SQL semantics.

## Project Phases

### Phase 0: Contracts And Fixtures `[done]`

Create the initial Pullmanager docs, sample manifests, and fixtures.

Outputs:

- Pullmanager manifest schema draft.
- Example session/run YAML fixture.
- Example telemetry/status fixture.
- Explicit naming contract.
- Test list derived from old edge cases.

Exit criteria:

- We can describe what Pullmanager expects before writing executor code.
- The bundle strategy is documented.

### Phase 1: Bundle Infrastructure `[done]`

Build the source layout and deterministic bundle/extractor.

Outputs:

- `scripts/pullmanager_src/...`
- `scripts/bundle_pullmanager.py`
- `dist/pullmanager_bundle.py`
- `--verify-bundle`
- `--extract`
- tests for extraction safety and hash verification.

Exit criteria:

- One generated file can be copied to the VM.
- Extraction is deterministic and refuses tampered/malformed bundles.

### Phase 2: Core Models And Manifest I/O `[done]`

Implement manifest/session/phase/run models and safe YAML read/write.

Outputs:

- Manifest loader.
- Manifest writer that preserves/updates status fields.
- Status transition helpers.
- Duration formatting helpers.

Exit criteria:

- Pullmanager can load a manifest, mark a phase running/done/failed, and write it back.
- Timing fields are written consistently.

### Phase 3: Normalization And Naming `[done]`

Implement old compatibility rules as explicit normalization.

Outputs:

- Boolean normalization.
- Legacy `dedup_key` handling.
- Temp table naming.
- Local table naming.
- Destination table naming.
- Canonical PK selection helpers.

Exit criteria:

- Edge cases from `yamlprocessing.md` have tests.

### Phase 4: SQL Rendering Without Database Execution `[next]`

Build the server-side and local-side renderers.

**There is no existing renderer to call.** `makeServer`, `makeLocal`,
`makeCosmos`, `makeProjects` and `generator.py` exist nowhere in this repo or
its history, only in the analysis documents. Earlier drafts of this plan said
Pullmanager would delegate SQL generation to them; it has to write it.

The corrected fixtures are the specification. `inputSimple.yaml`,
`examplecos.sql` and `exampleproj.sql` are now a verified matching triple, and
the old generator turned out to be faithful to its input, so its output is a
usable target rather than a cautionary tale.

Outputs:

- Cosmos SQL for the setup, PK and run phases, rendering from the Phase 3
  naming and normalization helpers rather than re-deriving names.
- Projects transfer SQL: `OPENQUERY` into `#Local_<dest>`, then insert into
  `<project_db>.dbo.<dest>`.
- Table shell DDL for the setup phase (see write mode below).
- Structured SQL blocks keyed by manifest phase and run ids.
- A declared telemetry contract, not result sets identified by column name.

Write mode, decided:

```text
setup           DROP + CREATE  <project_db>.dbo.<dest>   once per session
run LA-Female   INSERT
run LA-Male     INSERT
```

The old generator dropped and recreated inside every transfer block, which with
batching leaves only the last batch. Moving the drop into setup is what lets
batches accumulate into one complete cohort table.

Column widths are measured, not guessed. `#Local_<dest>` already holds the
transferred data before the destination table is created, so
`MAX(LEN(col)) + 50` is free there and no pre-scan is needed.

Not carried over:

- The unfiltered `source_raw` row count. It was a full scan of a fact table for
  an approximate number, and the cheap metadata alternative
  (`sys.partitions`, `sys.dm_db_partition_stats`) is not readable with the
  permissions available on Cosmos.
- `GO` batch separators in generated SQL. Pullmanager controls batching.
- Schema qualification applied to `from` but not `join`; Phase 3 applies it
  consistently and cannot produce `dbo.dbo.`.

Exit criteria:

- Given the fixture YAML, the renderer reproduces the structure of
  `examplecos.sql` and `exampleproj.sql`.
- Every executable block is addressed by manifest id. No SQL text is searched.

### Phase 5: Dry-Run Pullmanager `[planned]`

CLI orchestration with no database access.

Outputs:

- `pullmanager --dry-run split/pullmanifest.yaml`
- Ordered session, phase and run traversal.
- Rendered SQL written to inspectable files.
- The batch materialization plan made visible before anything runs.
- Manifest either untouched or annotated with dry-run metadata.

Exit criteria:

- Execution order is visible and testable.
- Every statement can be read before pyodbc exists.

### Phase 6: PyODBC Execution Adapter `[planned]`

The mechanical answers are already harvested; see "Connection And Execution
Facts" in `pullmanager_contracts.md`.

Known:

- `ODBC Driver 17 for SQL Server`, which defaults to `Encrypt=no`.
- `Trusted_Connection=yes` on both sides, so **there are no credentials**. The
  `.env` holds host and database names only.
- `cursor.messages` read on both the success and failure paths, which is what
  surfaces the inner error of a failed `OPENQUERY`.
- Scripts split on lines equal to `GO`, then every result set drained through
  `nextset()`.
- `autocommit=False` by default, so a block is already one transaction.

Outputs:

- Connection management for Cosmos (held open) and Projects (per table).
- Message capture, `nextset()` drain, error reporting with server messages.
- Runtime server identity capture.
- Parameterized bulk insert via `fast_executemany`, chunked, for every upload
  path.
- Timeout policy.

Exit criteria:

- A known statement runs and updates manifest telemetry.
- A failed `OPENQUERY` reports its inner error, not just the outer failure.

### Phase 7: Server Session Execution `[planned]`

Execute setup, uploads, PK and runs while the session's global temps live.

Outputs:

- Session connection lifecycle. The Cosmos connection is held open across every
  phase of a session, because `##JVM_*` dies with it.
- An epoch minted per connection, and `@@SERVERNAME` captured into it. The
  instance name **changes on every connection**, so it is never cached.
- Upload cohorts materialized through the client, since there is no linked
  server from Cosmos back to Projects.
- PK construction, then transfer of the PK table to Projects, which is what
  makes resume possible at all.
- PK uniqueness verification (`COUNT(*)` against `COUNT(DISTINCT keys)`).
  Non-unique keys make `ORDER BY` non-deterministic, so batch 3 would not be
  the same rows twice.
- Batch materialization against the **local** PK table, not Cosmos: the batch
  predicate and `OFFSET/FETCH` run inside the `OPENQUERY` string on the
  Projects side, so only the keys for the batch in hand move.
- Per-table timing and row-count telemetry.

Exit criteria:

- A session creates its global temps, records timing and rows, and keeps them
  alive across phases.
- Batch key sets are reproducible across runs given the same PK table.

### Phase 8: Local Projects Transfer `[planned]`

Outputs:

- The runtime linked-server value injected into local SQL from the current
  epoch.
- `OPENQUERY` into `#Local_<dest>`, then insert into the destination.
- Destination column widths measured from the staging table.
- The final insert wrapped in a transaction, so a failure partway through
  cannot duplicate rows when the run is retried.
- Cosmos and Projects row counts captured separately and compared.
- Large row-count warnings.

Exit criteria:

- Rows land in `<project_db>.dbo.<dest>` and the manifest records both counts
  and a duration.
- Re-running a failed batch does not duplicate rows.

### Phase 9: Resume, Retry, And Failure Behavior `[planned]`

Failure policy, decided:

| Failure | Effect |
| --- | --- |
| A run fails | Siblings continue. Batches are disjoint appends. |
| A phase fails | Everything downstream in that session is blocked. |
| A session fails | The next session still runs. |

One night therefore produces one list of every failure, rather than one failure
per night.

Outputs:

- Settled work (`done`, `skipped`) skipped on a plain resume.
- Stale work detected by epoch. `is_stale()` means the **server-side** output
  is gone, which is all there is for setup, uploads and PK; a run's durable
  result is rows in a Projects table and survives.
- Default resume replays the whole session and drops its Projects tables.
- `--resume-partial` keeps completed local transfers and replays only the
  server side, restoring the PK by uploading the saved Projects table rather
  than re-querying Cosmos. Guarded by comparing the restored PK against the row
  count recorded when it was first built: Cosmos is a refreshing snapshot, and
  appending later batches onto earlier ones drawn from a different population
  would stitch one table from two cohort definitions, silently.
- Retry of a single failed run without dropping the destination table.

Exit criteria:

- A partially completed manifest restarts without redoing successful work,
  unless asked.
- A resume across a Cosmos refresh is refused rather than silently mixed.

### Phase 10: Artifact Handoff `[planned]`

Outputs:

- Manifest fields describing the local tables that actually completed.
- A summary command.
- A contract for parquet export based on real data, including the
  `separate_parquets` batching flag.
- Measured column widths surfaced so templates can be tuned from data.

Exit criteria:

- `makeArtifacts` can inspect completed outputs without trusting
  planned-but-failed work.

## Adjacent Work: YAML Manager

Not part of these phases, and on the other side of the file boundary, but
agreed during planning:

- **Data dictionary validation.** `YAMLs/datadictionary.yaml` is the source of
  truth for column types. Cohort columns are checked against it by type family,
  with alias resolution; an unknown table is a hard stop so the dictionary stays
  complete. Belongs in `makeYaml.py`.
- `dedup_key` accepted and normalized with a warning at authoring time.
- `stop_at_for_pk_table` applied to the root PK cohort only.
- `stop_at_for_non_pk_tables`, `print_md` and `printout_md` warned as ignored.

The batch cross-product fix already landed in `makeYaml.py`.

## When To Use The Old Code

Do not start by porting the old code. It is a reference quarry, not the
architecture.

Most of what was wanted from it has now been extracted and written down in
`pullmanager_contracts.md`: connection strings, `cursor.messages` behavior,
`OPENQUERY` error surfacing, `GO` splitting, the `nextset()` drain, transaction
and timeout policy, and the shape of the CSV upload.

Still worth asking it about, if it surfaces:

- Whether any upload path other than literal `INSERT ... VALUES` was ever tried.
- How `parquet` upload cohorts were meant to behave, given Cosmos cannot read
  them directly.
- Anything about `makeR` or artifact export, for Phase 10.

Two patterns from it must not be reproduced, and both are documented with the
reasons: selecting SQL blocks by substring-matching a table name, which is
broken on prefix names and demonstrably mis-fires on this project's own
`PKTable` / `PKTable2` pair; and recovering row counts by inspecting result-set
column names instead of a declared contract.
