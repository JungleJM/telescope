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
| Session membership, refresh detection, single-batch retry, chunking, temp prefixes (D50–D53) | Built and tested against fakes on the Mac. Not yet on the VM |
| Transfer YAML (D49): export, split with no recipes, fixes on every error | Built and tested on the Mac. Not yet used on the VM |
| Launcher (`--gui`) | Built; tested with a fake tkinter, built for real on Tk 9 and on Tk 8.6 (Mac `python3.13`). Not yet opened on the VM |
| Artifact handoff (parquets) | Not built |

## Known Bugs

Found by reading the code or in a dry run; none has been hit against a
database, because nothing has run against one yet.

### An uploaded CSV PK cannot run

An upload marked `type: pk` with `file_type: csv` is uploaded to Cosmos only.
No Projects copy is made, yet everything after it assumes one:

- The PK phase checks uniqueness against `<project_db>.dbo.<dest>`, which does
  not exist, so the phase fails and every run is blocked, batched or not.
- Batches and chunks are selected from that same missing table.
- In the split, `chunk:` is silently dropped: the uploaded PK's session gets no
  batching at all, so one run pulls everything.
- In validation, a values dimension (`sex`) is refused as `missing_batch_column`
  even when the CSV has the column: the PK's columns are taken only from
  generated PK cohorts. `split_after_build` multipliers are refused the same
  way.
- A resume re-reads the CSV rather than a durable copy, so a file edited
  between runs would mix populations.

A `dbtable` PK works only while `source_table` is unset: with it set, the
checks read `<dest>`, not the source. Fixed by D54 (fixes 2 and 3 below);
until then use a generated PK.

### Uniqueness check is invalid for a multi-column key

`COUNT_BIG(DISTINCT [a], [b])` is not T-SQL: SQL Server allows one expression
in `COUNT(DISTINCT ...)`. Any PK with more than one key column fails its PK
phase. Count `SELECT DISTINCT a, b` in a derived table instead.

---

## Next: Fixes, In Order

Agreed order (D54, D55, and UI notes from testing on the Mac). Each is its own
commit.

1. **Small runtime fixes.** The multi-column uniqueness query (Known Bugs), and
   a commit after every SQL block (D55).
2. **Uploads land in Projects (D54), runtime.** Parquet read with `pyarrow`
   into typed `upload_<dest>`, `dbtable` copied server-side, Cosmos temps
   loaded from the copy with the same types. Resume keeps the copies and
   refuses a missing one. An uploaded PK's uniqueness, batches and chunks read
   its copy. A CSV reaching the runtime is refused with a pointer to the split.
3. **Uploads, YAML Manager (D54).** CSV converted to parquet at split with the
   declared `columns:` types, plus a command to convert by hand. Validation
   reads a parquet's columns and checks declared ones exist and parse. Batching
   on an uploaded PK validated against its file and carried into its session;
   `split_after_build` on one refused.
4. **Builder UI.**
   - Uploads: a "PK" checkbox per upload, refusing a second PK and naming the
     existing one; the current PK shown.
   - A Multipliers section, like Batching.
   - Each section's title followed by a one-line explanation, taken from the
     comments in `YAMLs/template.yaml`.
   - Joins: a short note on what each join type does with rows that do not
     match.
   - Custom table form: "Add Custom Table" (renamed) at the end of the form's
     first row; "Reset" outside the form, by its heading; the Type dropdown
     becomes the "PK table" toggle.
   - Added custom rows: the type box goes, replaced by the PK toggle; "Save as
     Recipe" between Load and Remove writes the table into `recipes.yaml`
     (refusing a name already there), replacing "Copy/Download Custom Recipe".
   - Cohorts tab: a read-only line summarising multipliers and batching, e.g.
     `[multipliers] Race: black/white, IBDType: UC/Crohns` and
     `[batching] sex: Female/Male, state: LA/MS/GA/NC/other, chunk: 2000`.

---

## Next: The First Live Run

Everything below the dry run is unproven until it meets Cosmos. On the VM:

1. Copy the new bundle, `--verify-bundle`, extract, then `pullmanager.py --tdd`.
   The extraction should report `recipes.yaml`, the UI and the template
   example as no longer shipped, with a `.local` for any that were edited
   there.
2. `pullmanager.py --gui`. Checked on Tk 8.6 on the Mac, but the VM's exact
   Tk is unconfirmed, so watch for option or layout errors.
3. On the Mac, export a small template with `--export-transfer`: a generated
   PK, an upload and a batched run (explicit `values:`, and a `chunk:`). Copy
   it and its listed uploads over, then Validate, Export split, Dry run and
   Execute. Check:
   - `@@SERVERNAME` is captured and `OPENQUERY` reaches that instance.
   - Cosmos and Projects row counts agree, per batch, with no false warnings.
   - `SELECT _batch, COUNT(*) FROM <destination> GROUP BY _batch` shows each
     batch once, and a chunked batch's rows all present.
   - An upload over 1000 rows succeeds.
   - A deliberately broken run fails alone while its siblings finish; then
     `--retry-failed` pulls only that run, and its `_batch` count is right.
   - A second `--execute` says "nothing left to pull".
   - The manifest reads correctly afterwards, in the launcher's status tab too.
4. Run two pulls with the same prefix at the same time: the second should
   report a numbered prefix (`tesrun2`) and both should finish.
5. Check what D50 and D51 rely on: `SELECT OBJECT_ID('tempdb..##<a temp that
   exists>')` returns a number from our login, and `manifest.cosmos_refresh`
   is filled in. Note `create_date` either side of the next refresh to confirm
   a refresh changes it.
6. An upload of around 250,000 rows, timed.
7. During a large transfer, check whether other work on the Projects database
   waits on it. The driver runs with autocommit off, so the `OPENQUERY` into
   staging sits inside an open transaction until the run commits, although
   `local_sql.render_transfer` was written to keep it outside one. If it
   blocks others, open the Projects connection with autocommit on.

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
- An upload needs no export: its parquet already exists (D54), so copy it.
- Parquet export per cohort, honouring the `separate_parquets` batching flag.
  Each destination row carries its batch label in `_batch` (D52), which is
  what a per-batch export splits on.
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
them inferred from `{{prefix}}_{{Var}}`.

The browser UI also does not yet show the uploaded PK source selection, and has
no button to hand a split folder to Pullmanager.

### Smaller Open Items

- **Generated-table dependencies.** Cohorts reference other generated temps by
  handwritten name (`{{prefix}}_Patients`). It should be structural, so the
  renderer owns temp names.
- **One PK per multiplier group.** Two `type: PK` cohorts in one group are an
  error (`multiple_pk_cohorts`), so each session has exactly one PK.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis
  events for those patients: `PKTable` built by joining `PKTable2`). The `pk`
  phase is one YAML; ordered PK cohorts inside it need a representation.
- **`values: all` batching.** Warned at validation, refused when the pull
  reaches it. Resolving it at run time would change the manifest's run set
  after planning.
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
