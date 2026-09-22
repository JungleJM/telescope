# Pullmanager Refactor Plan

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
- Keep server/session resources alive while global temp tables are needed.
- Run setup, uploads, PK, and run phases.
- Generate/render server SQL and local transfer SQL from the phase YAML.
- Execute SQL through adapters.
- Update manifest status after each phase/table/run.
- Support retry/resume from manifest state.

YAML Manager remains the planner and authoring tool. Pullmanager is the VM-side executor.

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

### Phase 0: Contracts And Fixtures

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

### Phase 1: Bundle Infrastructure

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

### Phase 2: Core Models And Manifest I/O

Implement manifest/session/phase/run models and safe YAML read/write.

Outputs:

- Manifest loader.
- Manifest writer that preserves/updates status fields.
- Status transition helpers.
- Duration formatting helpers.

Exit criteria:

- Pullmanager can load a manifest, mark a phase running/done/failed, and write it back.
- Timing fields are written consistently.

### Phase 3: Normalization And Naming

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

### Phase 4: SQL Rendering Without Database Execution

Build server-side and local-side SQL renderers.

Outputs:

- Server/Cosmos SQL for setup/upload/PK/run phases.
- Projects/local transfer SQL using `OPENQUERY`.
- Structured SQL blocks keyed by manifest phase/table IDs.
- Rendered SQL files saved under manifest-defined output paths.

Exit criteria:

- Given fixtures, Pullmanager renders predictable SQL.
- No substring matching is needed to select executable blocks.

### Phase 5: Dry-Run Pullmanager

Implement CLI orchestration without live database execution.

Outputs:

- `pullmanager --dry-run split/pullmanifest.yaml`
- Ordered session/phase/run traversal.
- Rendered SQL artifact output.
- Manifest status can remain unchanged or record dry-run metadata.

Exit criteria:

- The planned execution order is visible and testable.
- The generated SQL can be manually inspected before pyodbc exists.

### Phase 6: PyODBC Execution Adapter

Bring in the old database lessons narrowly.

Outputs:

- Connection management.
- Cursor message capture.
- `nextset()` handling.
- Runtime server identity capture.
- SQL error reporting.
- Transaction/commit policy.
- Timeout policy.

Exit criteria:

- A small known SQL command can run and update manifest telemetry.
- Errors preserve useful server messages.

### Phase 7: Server Session Execution

Execute setup/upload/PK/run server SQL while preserving session-scoped global temps.

Outputs:

- Session connection lifecycle.
- Global temp table creation.
- Per-table/per-cohort timing.
- Server row-count telemetry.

Exit criteria:

- Pullmanager can create global temps and record timing/rows in the manifest.

### Phase 8: Local Projects Transfer

Execute local transfer SQL.

Outputs:

- Runtime linked-server value passed to local SQL rendering.
- `#Local_<dest>` staging.
- Final table drop/recreate or configured write mode.
- Cosmos count and Projects count comparison.

Exit criteria:

- Rows transfer from `##JVM_<dest>` to `<project_db>.dbo.<dest>`.
- Manifest records both counts and duration.

### Phase 9: Resume, Retry, And Failure Behavior

Make manifest status operational.

Outputs:

- Skip done phases by default.
- Retry failed phases when requested.
- Mark dependent work blocked/skipped when needed.
- Preserve partial completion.

Exit criteria:

- Pullmanager can restart from a partially completed manifest without redoing successful work unless requested.

### Phase 10: Artifact Handoff

Prepare clean handoff to `makeArtifacts`.

Outputs:

- Manifest fields describing actual completed local tables.
- Optional summary command.
- Clear contract for parquet export based on actual data.

Exit criteria:

- `makeArtifacts` can inspect completed outputs without trusting planned-but-failed work.

## When To Use The Old Code

Do not start by porting the old code.

Use the old code later for narrow questions:

- pyodbc connection strings.
- cursor message behavior.
- `OPENQUERY` error handling.
- transaction/commit behavior.
- timeout handling.
- CSV upload implementation details if needed.

The old code should be a reference quarry, not the architecture.
