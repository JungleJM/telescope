# Roadmap

What is unbuilt, unverified, or undecided. The only document that carries
status: `design.md` describes what exists, `decisions.md` why.

When an item here is built, delete it from this file and describe the result in
`design.md`. When it is decided, record the decision in `decisions.md`.

---

## Where Things Stand

| Part | State |
| --- | --- |
| YAML Manager: validation, dictionary, table binding, pre-YAML, split, manifest | Built and tested |
| YAML Manager browser UI | Built; Mac only |
| Bundle: build, verify, extract, `.local` preservation | Built, tested, delivered to the VM |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; dry run proven end to end from one copied bundle |
| Pullmanager: connections, session execution, uploads, transfer | Written and tested against a fake cursor. **Never run against a database** |
| Launcher (`--gui`) | Built; tested with a fake tkinter and by hand on Tk 9. Not yet opened on the VM |
| Artifact handoff (parquets) | Not built |

## Known Bugs

Found by reading the code; none has been hit yet, because nothing has run
against a database.

### `chunk:` pulls only the first chunk

`SessionRunner._materialize_batch` calls `select_batch_rows(...)` without a
`chunk_index`, so every run with a `row_chunk` dimension narrows the PK to rows
`0..rows_per_batch` and stops. The rest of the batch is never pulled, with no
error. `count_batch_rows` exists to size the fan-out but nothing calls it.

The fix: count the batch's PK rows, then loop `chunk_index` over
`ceil(count / rows_per_batch)`, each chunk refilling `##JVM_<pk>` and appending.
Decide whether chunks are recorded in the manifest as they are discovered (the
design always intended that for resume and retry) or executed inside the one
run node. The test must check the **outcome**: every PK row's data lands
exactly once across the chunks. Until fixed, do not use `chunk:`.

### A second `--execute` re-pulls finished sessions

Every session opens a new connection, which makes every `done` node stale, so
re-running a manifest to retry one failed session re-pulls every session that
had already finished. Correct, but it can double an overnight job. A session
whose status is `done` has complete Projects tables and could be skipped
outright, with a flag to force it.

---

## Next: The Transfer YAML (D49)

Recipes stay on the Mac; the VM gets `<project>_transfer.yaml` with them
written out in full. Already there: `--export-preyaml expanded-recipes` inlines
**cohort** recipes without applying multipliers or batching. Tested on
`02_valid_multipliers_batching.yaml`: split with an empty recipes file, the
cohorts resolve, but the batching fails
(`bad_batching: Could not understand batching item`), because `sex`, `state`
and `chunk` are still bare names pointing into `recipes.yaml`.

To build, in order:

1. **`--export-transfer`** in `makeYaml.py`. Runs full validation, then writes
   the template with cohort recipes inlined and each batching item replaced by
   its full definition (`name`, `kind`, `applies_to`, `column`, `values` or
   `rows_per_batch`, with the template's overrides applied). Adds the
   `transfer:` block (source template file name, recipes SHA-256). Written
   beside the template by default, `--out` to choose; written elsewhere,
   relative `file_loc` values are rebased so they still point at the same
   files, and the upload files that must travel with it are listed.
2. **Recipes only when referenced.** Compile, split and validation load the
   recipes file only if something says `recipe:` or names a batching recipe.
   No recipes file plus a reference is an error naming the cohort and pointing
   at `--export-transfer`.
3. **Hand-written batching is checked.** A full batching definition is
   validated field by field (known `kind`; `column_values` needs `column` and
   `values`; `row_chunk` needs a positive `rows_per_batch`), since on the VM it
   is edited by hand. A preset `chunk` left at `rows_per_batch: required` is an
   error. Any `row_chunk` warns that `chunk:` pulls only the first chunk (Known
   Bugs).
4. **Every error carries a fix.** `Message` gains a `fix` field, printed on its
   own line. Contexts become field paths (`cohorts[1] (Patients)`,
   `upload_cohorts[0].file_loc`, `batching[2]`). A test reads `makeYaml.py`
   and fails if any `result.error(...)` call has no `fix=`.
5. **Extraction keeps edited files the bundle drops**, as `<name>.local`,
   using the previous extraction's `.bundle-manifest.json` hashes to tell an
   edit from an untouched copy. Must land in the same bundle that drops files.
6. **Bundle contents.** Drop `recipes.yaml`, `yamlmanager.py`,
   `yamlmanager_backend.py` and `template.yaml.example`. The missing-template
   message points at `--export-transfer` instead of the example.
7. **Launcher.** "Template" becomes "Transfer YAML"; the recipes field goes.
   Old settings files still load (unknown keys are ignored).
8. **Browser UI.** The Exports tab shows the transfer YAML with a Download
   button.

Tests of the **outcome**: a transfer YAML split on its own, with no recipes
file, gives the same split folder (manifest and every session YAML) as the
original template split with recipes; and an edited `recipes.yaml` on the VM
survives an update that stops shipping it.

---

## Next: The First Live Run

Everything below the dry run is unproven until it meets Cosmos. On the VM:

1. Extract, then `pullmanager.py --tdd`.
2. `pullmanager.py --gui`. The VM likely has Tk 8.6, not the 9 it was checked
   against, so watch for option or layout errors.
3. Execute a small template with an upload and a batched run (explicit
   `values:` dimensions, no `chunk:`). Check:
   - `@@SERVERNAME` is captured and `OPENQUERY` reaches that instance.
   - Cosmos and Projects row counts agree.
   - An upload over 1000 rows succeeds.
   - A deliberately broken run fails alone while its siblings finish.
   - The manifest reads correctly afterwards, in the launcher's status tab too.
4. Run two sessions at the same time and see whether they interfere (next
   section).

---

## Open Problems

### Concurrent Pulls Collide On Temp Names

Global temps are instance-wide, not connection-private: `##JVM_Patients` is
visible to every session on the Cosmos instance. Two pulls running at once that
both produce `Patients` collide, and generated SQL opens with
`DROP TABLE IF EXISTS ##JVM_Patients`, so pull B drops pull A's table mid-run.
A then fails confusingly, or transfers partial rows.

Per-project folders do not help; the collision is in SQL Server's namespace.
The fix is to namespace the temp, `##JVM_<project>_<dest>` or a short session
token. Contained to `naming.py` and the naming contract, but it changes an
invariant inherited from the old generator, so it needs a decision first.
Blocking for running several pulls at once, which is the intended way of
working.

### A Resume That Keeps Completed Batches

`--resume-partial` is refused (D46). A correct version needs all of:

- Setup must not drop a destination that holds completed batches; today it
  always does.
- The completed runs' rows stay; only failed and unrun batches replay.
- The PK must be the same population the completed batches were drawn from.
  Batch selection already reads the Projects copy of the PK (D19), which does
  not move under a Cosmos refresh, so the remaining check is that the rebuilt
  server-side PK matches the row count recorded when it was first built, and
  a refusal when it does not.
- A test that checks the **outcome** (every batch's rows present exactly once
  after resume), not just which nodes were skipped. The original test checked
  the latter, which is how the data loss went unnoticed.

Until then a full replay is always correct.

### Project Folder Layout

Pulls will run several at a time, so each should get its own folder, named by
`project_folder`. The user's sketch:

```text
<project_folder>/
  load_parquets.py        load every parquet into R or Python, out of memory
  contents.yaml           each cohort pulled, with its description, and its
                          columns with type and description
  server/
    <pull>.yaml           the template that was run, renamed for the project
    Manifest.yaml         the live status document
    yamls/                the split YAMLs
    sql/                  Cosmos and Projects SQL, grouped by session
  parquets/
    SneakPeek/            exported as soon as the SneakPeek cohorts finish
    Cosmos/
```

Observations for when it is built:

- One root derived from `project_folder`, fixed shape, no routing table. Too
  many path knobs is what made paths painful before.
- The SneakPeek/Cosmos split needs no configuration: under `Dual` cohorts come
  out as `X` and `X_sp`. Session status already says when a variant is done, so
  SneakPeek parquets can export before the Cosmos cohorts run.
- The renames (`Manifest.yaml`, `server/yamls/`) are cheap: manifest paths are
  already relative to the manifest.
- Big reference files live in a `data/` folder in the parent directory;
  templates reference them relative to the template.

### Artifact Handoff

The old plan's Phase 10.

- Manifest fields describing the local tables that actually completed, so
  export never trusts planned-but-failed work.
- Parquet export per cohort, honouring the `separate_parquets` batching flag.
- `contents.yaml` and `load_parquets.py`, above.
- A summary command.
- Surface measured column widths so templates can be tuned from data. Today
  they, the row-count comparisons and large-count warnings only go to the
  console; they could be written to the manifest.

### Table-Input Binding In The UI

The rule today (D45): an unbound table input is an error naming the tables that
could fit, and binding happens on the cohort. When the UI grows, revisit how
binding is offered there: picking from the suggested tables, and whether a
recipe should be able to declare its table inputs explicitly rather than having
them inferred from `##JVM_{{Var}}`.

The browser UI also does not yet show the uploaded PK source selection, and has
no button to hand a split folder to Pullmanager.

### Smaller Open Items

- **Generated-table dependencies.** Cohorts reference other generated temps by
  handwritten name. It should be structural, so the renderer owns temp names.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis
  events for those patients: `PKTable` built by joining `PKTable2`). The `pk`
  phase is one YAML; ordered PK cohorts inside it need a representation.
- **`values: all` batching.** Refused with an explanation. Resolving it at run
  time would change the manifest's run set after planning.
- **Tests still owed**: duplicate output column names and blank `source`
  expressions in a cohort.

---

## Needs Research

### Questions For The Cosmos Developers

These decide whether a partial resume can ever be trusted across a refresh.
Refreshes are every few weeks.

1. Is `PatientDurableKey` stable across refreshes? (Expected yes: "durable".)
2. **Is there a snapshot or version identifier that can be `SELECT`ed?** If so,
   record it beside the epoch, and "did Cosmos refresh between these two
   connections" becomes a check instead of a judgment.
3. Do `IsCurrent`, `IsDeleted` and `UseInCosmosAnalytics_X` change for existing
   patients between refreshes? Those flags are why a re-run PK query can return
   a different set even with fixed date windows.

### Primary And Foreign Keys In The Data Dictionary

The dictionary already records key relationships, but as prose in the type:

```yaml
PatientDurableKey:
  type: bigint (foreign key to PatientDim.DurableKey)
```

Structured, it would let validation check **joins**, not just columns:
`ON tc.TerminologyConceptKey = dt.DiagnosisKey` binds fine and is wrong.

Before it is worth building:

- Structured fields (`primary_key: true`, `references: PatientDim.DurableKey`)
  or parse the annotation? Parsing prose is brittle; restructuring touches every
  entry.
- Some annotations name a table with no column (`foreign key to
  EncounterFact`), some name alternatives (`DiagnosisDim/DiagnosisTerminologyDim`).
- Is a join off a declared relationship an error, a warning, or fine?
  Deliberate non-key joins exist.
- Does Cosmos enforce these relationships, or only document them?
