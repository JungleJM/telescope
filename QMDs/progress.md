# YAML Manager Progress

This is the active progress tracker for the next YAML Manager iteration. The older phase-by-phase build history remains in `QMDs/completed/buildprogress.qmd`.

## Current Direction

The active design is `QMDs/yamlmanagerDesign.qmd`.

YAML Manager is moving from "compile one large finished YAML" toward a planner/exporter workflow:

1. Validate and inspect the authoring template.
2. Export a portable pre-YAML.
3. Optionally export an expanded-recipes pre-YAML for inspection.
4. Generate a split pull folder.
5. Generate `pullmanifest.yaml`.
6. Hand that manifest/folder to Pullmanager.

Pullmanager will own VM/server/local execution and will update the manifest as a status document.

## Completed Backend Work

Implemented in `scripts/makeYaml.py`:

- YAML loading/writing abstraction with `ruamel.yaml`, `pyyaml`, and local Ruby fallback.
- `CompileResult` and structured errors/warnings.
- Recipe import into concrete cohorts.
- Top-level variable normalization.
- Required variable inference.
- Table input inference from `##JVM_{{TableVar}} AS alias`.
- Required input-column inference from alias usage.
- Output-column discovery from cohort `columns`.
- Group-specific `PKTable` inference.
- Upload registration and CSV header validation.
- `sql_condition` rendering.
- `IN` versus `LIKE` inference.
- Warning on `_` in `LIKE` values.
- `during_build` and `split_after_build` multiplier support.
- Batching recipe normalization and batching metadata.
- `COSMOS`, `COSMOS_SneakPeek`, and `Dual`/`both` expansion.
- Markdown report generation.
- Embedded `--tdd` test suite.
- Fixture templates under `YAMLs/manager_test_cases/`.
- Public `dump_yaml_text()` helper for frontend/backend adapters.
- `build_preyaml(mode="symbolic")`.
- `build_preyaml(mode="expanded-recipes")`.
- `plan_split_runs(...)` with in-memory split plan structures.
- CLI pre-YAML export through `scripts/yamlmanager.py --export-preyaml ...` and `scripts/makeYaml.py --export-preyaml ...`.

## Completed UI/Adapter Work

Implemented:

- The original `managerUI.py` simple UI was renamed through `scripts/telescope.py` and now lives at `scripts/yamlmanager.py`.
- Serving behavior now supports local, remote, VM, SSH tunnel, and public-bind use cases with configurable host/port behavior.
- UI no longer imports `makeYaml.py` directly.
- `scripts/yamlmanager_backend.py` provides a stable Python adapter surface around the compiler.
- `YAMLMANAGER_BACKEND_MODULE` can point the UI at an alternate backend module.
- `scripts/telescope.py` and `scripts/telescope_backend.py` remain as compatibility wrappers.

## Current Known Validation State

The current local template still reports the expected local validation blocker unless the relevant upload file/path is supplied:

```text
ERROR missing_upload_file
WARN upload_schema_unknown
```

This is input/template state, not a server/UI failure.

## New Design Decisions

Settled for the next iteration:

- pre-YAML defaults to symbolic recipe references and should stay close to the authored template.
- Expanded-recipes pre-YAML is optional and for inspection.
- Full split output always includes `pullmanifest.yaml`.
- Pullmanager executes from the manifest, not folder-name inference.
- Every session has the same phase routine:
  1. `setup.yaml`
  2. `upload_cohorts.yaml`
  3. `pk.yaml`
  4. one or more run YAMLs
- Every split YAML should be standalone-valid.
- Upload cohorts are session setup resources, not per-batch resources.
- Zero or one upload cohort may be marked `type: pk`; if present, `pk.yaml` registers that uploaded table as the canonical PK source.
- Batches remain logical in YAML Manager.
- Pullmanager materializes actual PK/batch key sets on the VM side.
- Pullmanager mutates `pullmanifest.yaml` into the status/progress document.
- `parquetcontents.yaml` belongs to `makeArtifacts` and should reflect actual completed outputs.

## Not Yet Implemented

Backend/API:

- Add uploaded-PK validation and planning.
- Add `pullmanifest.yaml` generation.
- Add split YAML generation.
- Add manifest status schema defaults.
- Add CLI commands for split export.

UI:

- Add pre-YAML preview/export controls.
- Add expanded-recipes preview/export controls.
- Add split manifest/session preview.
- Add validation display for uploaded PK source selection.
- Add optional handoff button/command once Pullmanager contract exists.

Testing:

- Add TDD coverage for split planning without batching.
- Add TDD coverage for split planning with batching.
- Add TDD coverage for uploaded PK cohort behavior.
- Add fixture templates for split output and manifest shape.

## Phase Status

Status markers:

- `[done]`: implemented and committed.
- `[in-progress]`: currently being built.
- `[blocked]`: cannot proceed until a dependency or design question is resolved.
- `[planned]`: agreed but not started.

Current phase:

- Phase 3: Split Plan Model `[done]`
- Next: Phase 4: Manifest Generation `[planned]`

Production sequence:

1. Phase 0: Planning Baseline `[done]`
2. Phase 1: Naming And Compatibility `[done]`
3. Phase 2: pre-YAML Export `[done]`
4. Phase 3: Split Plan Model `[done]`
5. Phase 4: Manifest Generation `[planned]`
6. Phase 5: Split YAML Writing `[planned]`
7. Phase 6: Multipliers And Logical Batches `[planned]`
8. Phase 7: Uploaded PK Cohorts `[planned]`
9. Phase 8: CLI And Tests `[planned]`
10. Phase 9: UI Preview And Handoff `[planned]`

After each phase:

- Update this status table.
- Commit the code/docs.
- Push the branch.

## Phase Notes

### Phase 0: Planning Baseline `[done]`

Completed:

- Consolidated the active design into `QMDs/yamlmanagerDesign.qmd`.
- Added this progress tracker.
- Marked older active plans as pointers/historical notes.
- Added uploaded-PK behavior to the design.
- Defined the manifest-centered Pullmanager handoff contract.

### Phase 1: Naming And Compatibility `[done]`

Completed:

- Rename `telescope.py` and `telescope_backend.py` to `yamlmanager.py` and `yamlmanager_backend.py`.
- Keep wrappers for old entry points.
- Update environment-variable naming with backward-compatible fallbacks.
- Verify the simple UI still runs through the new and compatibility entry points.

### Phase 2: pre-YAML Export `[done]`

Completed:

- Add backend pre-YAML export functions.
- Add symbolic export mode.
- Add expanded-recipes export mode.
- Add CLI flags and tests.

### Phase 3: Split Plan Model `[done]`

Completed:

- Define internal split plan structures.
- Generate session/run IDs.
- Represent setup/upload/PK/run phases.
- Represent uploaded PK source metadata.

### Phase 4: Manifest Generation `[planned]`

Planned:

- Generate `pullmanifest.yaml` from split plans.
- Include phase/run paths and initial statuses.
- Include mutable status fields for Pullmanager.

### Phase 5: Split YAML Writing `[planned]`

Planned:

- Write standalone-valid setup, upload, PK, and run YAMLs.
- Add `pull_context`.
- Remove active multiplier/batching instructions from split YAMLs.

### Phase 6: Multipliers And Logical Batches `[planned]`

Planned:

- Create sessions from multiplier-derived cohort groups.
- Create logical batch runs.
- Keep PK batch materialization out of YAML Manager.

### Phase 7: Uploaded PK Cohorts `[planned]`

Planned:

- Validate zero-or-one uploaded PK cohort.
- Generate upload/PK artifacts for uploaded PK source.
- Reflect uploaded PK in manifest and split YAML context.

### Phase 8: CLI And Tests `[planned]`

Planned:

- Expose pre-YAML and split export through CLI.
- Add embedded TDD coverage and fixture coverage.

### Phase 9: UI Preview And Handoff `[planned]`

Planned:

- Add UI previews/exports for pre-YAML and split output.
- Add optional Pullmanager handoff once Pullmanager CLI contract exists.

## Historical Docs

Use these for context, not as active implementation targets:

- `QMDs/completed/buildprogress.qmd`: detailed completed phase history.
- `QMDs/splittingVMtasks.qmd`: original brainstorming notes that led to the current design.
