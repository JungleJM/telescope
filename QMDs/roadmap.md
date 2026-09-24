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
| Bundle: build, verify, extract, `.local` preservation | Built and tested. The D49 bundle (no recipes or UI) is not yet on the VM |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; dry run proven end to end from one copied bundle |
| Pullmanager: connections, session execution, uploads, transfer | Written and tested against a fake cursor. **Never run against a database** |
| Transfer YAML (D49): export, split with no recipes, fixes on every error | Built and tested on the Mac. Not yet used on the VM |
| Launcher (`--gui`) | Built; tested with a fake tkinter, built for real on Tk 9 and on Tk 8.6 (Mac `python3.13`). Not yet opened on the VM |
| Artifact handoff (parquets) | Not built |

## Known Bugs

Found by reading the code or in a dry run; none has been hit against a
database, because nothing has run against one yet.

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

### Every session runs every multiplied fact cohort, some against another session's PK

Found splitting `YAMLs/template.yaml` (IBDType x Race multipliers, `Dual`), and
the same before D49. There are 8 sessions (`UCblackPatients`,
`CrohnswhitePatients_sp`, ...), and each session's runs carry all 8
`OtherHospitalizations` variants rather than the one for its own group. In
session `UCblackPatients_sp`, the run for `CrohnsblackOtherHospitalizations_sp`
joins `##JVM_CrohnsblackPatients`, a different session's PK, and not the
SneakPeek one. So each fact cohort is pulled 8 times, the copies overwrite one
another's `##JVM_` temp, and whichever finishes last decides which population
lands in Projects.

Two faults, probably: the split does not filter a session's fact cohorts to
its multiplier group and Cosmos variant, and `PKTable` is bound before
`expand_cosmos` adds `_sp`, so an `_sp` cohort points at the non-`_sp` PK. The
test must check the **outcome**: each session's runs hold exactly its own
group's cohorts, each joining its own session's PK. Until fixed, do not run a
template that combines multipliers with fact cohorts.

### A second `--execute` re-pulls finished sessions

Every session opens a new connection, which makes every `done` node stale, so
re-running a manifest to retry one failed session re-pulls every session that
had already finished. Correct, but it can double an overnight job. Fixed by
D52 (fix 2 below).

### A failed run can leave some of its rows behind

Each cohort's transfer commits on its own, so a run that fails at its third
cohort has landed the first two in Projects. A replay today is safe only
because setup drops every destination first. Fixed by D52's `_batch` column
and delete-before-run (fix 2 below).

### The row-count check warns falsely from the second batch on

The Projects-side count is `COUNT_BIG(1)` over the whole destination, while
the Cosmos count is this batch alone, so every batch after the first reports
"the transfer did not carry everything". Fixed by counting
`WHERE _batch = <label>` (D52, fix 2 below).

---

## Next: Fixes, In Order

Agreed order. Each is its own commit.

1. **Every session runs every multiplied fact cohort** (Known Bugs). Filter a
   session's runs to its own multiplier group and Cosmos variant, and bind an
   `_sp` cohort's `PKTable` to the `_sp` PK. Outcome test on a template with
   multipliers and `Dual`.
2. **Refresh detection, then single-batch retry** (D51, D52). Record
   `create_date` per Cosmos database; a change re-pulls everything. Skip
   finished sessions (`--repull` forces them). Keep destinations, rebuild the
   PK temp from its Projects copy, delete a run's `_batch` rows before it runs.
   Remove `--resume-partial` (D46) entirely. Fixes the three bugs above.
3. **`chunk:`** (Known Bugs, D53). Count the batch's PK rows and loop the
   chunks inside the run. Wanted soon: PKs of 250k to 1M patients should run
   in a few large batches, not hundreds. Uploads are already uncapped
   (parameter arrays, 20,000 rows a call; design, Uploads), so the chunk loop
   is the only limit.
4. **Per-project temp prefix and numbered batch labels** (D50, D53).
   `{{prefix}}_{{Var}}` in recipes, `##JVM_` refused with a fix, the clash
   check that adds a number, and `b<i>of<n>-<values>` labels, which retire
   `duplicate_batch_name`.
5. **Keys in the data dictionary** (Needs Research): after the VM's AI has
   worked through `QMDs/keys_research_for_vm.md`.

---

## Next: The First Live Run

Everything below the dry run is unproven until it meets Cosmos. On the VM:

1. Copy the new bundle, `--verify-bundle`, extract, then `pullmanager.py --tdd`.
   The extraction should report `recipes.yaml`, the UI and the template
   example as no longer shipped, with a `.local` for any that were edited
   there.
2. `pullmanager.py --gui`. Checked on Tk 8.6 on the Mac, but the VM's exact
   Tk is unconfirmed, so watch for option or layout errors.
3. On the Mac, export a small template with `--export-transfer`: an upload and
   a batched run (explicit `values:` dimensions, no `chunk:`). Copy it and its
   listed uploads over, then Validate, Export split, Dry run and Execute. Check:
   - `@@SERVERNAME` is captured and `OPENQUERY` reaches that instance.
   - Cosmos and Projects row counts agree.
   - An upload over 1000 rows succeeds.
   - A deliberately broken run fails alone while its siblings finish.
   - The manifest reads correctly afterwards, in the launcher's status tab too.
4. Run two sessions at the same time and see whether they interfere.
5. Check what D50 and D51 rely on: `SELECT OBJECT_ID('tempdb..##<a temp that
   exists>')` returns a number from our login, and the `sys.databases` query
   runs from the Cosmos connection. Note `create_date` either side of the next
   refresh to confirm a refresh changes it.
6. An upload of around 250,000 rows, timed.

---

## Open Problems

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

What to find out on the VM is in `QMDs/keys_research_for_vm.md`, a temporary
brief to put to the VM's AI: where keys are declared, whether they hold in the
data, and what the interactive data dictionary shows. Delete it once its
answers are folded in here and into `decisions.md`.
