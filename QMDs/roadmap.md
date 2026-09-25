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
| YAML Manager browser UI | Built; Mac only. The Builder changes from testing (PK marking, Multipliers, section notes, Save as Recipe) are tested in code but not yet clicked through |
| Bundle: build, verify, extract, `.local` preservation | Built and tested. On the VM (D64, content_id `1af88f77`) |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; dry run proven end to end from one copied bundle |
| Pullmanager: connections, session execution, uploads, transfer | Tested against a fake cursor. **First live run under way** (IBD Ancestry, below): its first session ran every phase against the databases |
| Session membership, refresh detection, single-batch retry, chunking, temp prefixes (D50–D53) | Built and tested against fakes on the Mac. Executing for the first time in that run |
| Uploads through Projects, typed; CSV to parquet at split; commit per cohort (D54, D55) | Built and tested against fakes, with real `pyarrow`, on the Mac. Executing for the first time in that run |
| Dedup through sources, split levels filtering their PK, control sampling, hash samples, uploads once per pull (D58–D62) | Built and tested against fakes on the Mac; dry-run on the VM. Executing for the first time in that run |
| Transfer YAML (D49): export, split with no recipes, fixes on every error | Built and tested; used on the VM (IBD Ancestry) |
| Launcher (`pullmanager.py`) | Opened on the VM. Validate, Export split and Dry run work from it; Execute does not (Known Bugs) |
| Artifact handoff (parquets) | Not built |

## Known Bugs

Found in the first live run (IBD Ancestry, September 2026), all in the fixes
below:

- Under `Dual`, each Cosmos session runs before its SneakPeek twin, so the
  quick round comes last (1).
- The status tab's Refresh button sits alone at the bottom right (1).
- A PK that names its key only in `dedup_keys` is never checked for
  uniqueness: every session warns "PK declares no key_column", and a `chunk:`
  on it would fail its run (1).
- Execute started from the launcher ends at once with exit code 3221225794
  (`0xC0000142`), before printing anything. The same command typed into
  VSCodium's terminal runs (4).

---

## Next: Fixes, In Order

From the first live run. The IBD Ancestry run in progress keeps its split and
its order; these apply from the next one.

1. **SneakPeek first (D65), the Refresh button and the PK's key (D69).** The
   Refresh button moves to the top left of the status tab, beside the manifest
   path. All three are small, and the order and the uniqueness check matter
   from the next split.
2. **`--execute <project>` (D66), the new names and the preview's closing
   statement (D71; the Pull Log tab waits for fix 4).** They make the terminal
   route easy at once, even if the launcher's Execute never works on the VM,
   and say what the preview is for.
3. **The running-pull lock (D67):** `--execute` and `--export-split` refuse to
   clash, the launcher greys Export split and Execute and follows the pull,
   and `--running`. Needed before two pulls run at once, and fix 4 relies on it.
4. **Execute in its own console window, its log and the Pull Log tab (D68,
   D71).** The least certain to work on the VM, so late; if it cannot start,
   it falls back to fix 2's command.
5. **The session readout (D70):** warnings first, then one table of column
   widths per session, as notes. Polish, and it reads best once the log
   exists.

Then rebuild the bundle.

---

## Next: The First Live Run

Where it stands: the D64 bundle is extracted on the VM and the launcher opens
there (its Tk is fine, bar the Refresh button). The IBD Ancestry pull was
validated, split and dry-run from the launcher, and is executing from
VSCodium's terminal (`python pullmanager.py --execute
runs\IBD_Ancestry\split\pullmanifest.yaml`). Its first session,
`CrohnsblackPatients` on Cosmos, finished every phase on instance
`et4003vpdsql032`: setup, uploads, the PK, and all three Sex batches
(`sex-other` matched no PK rows, as a catch-all should), with no warning that
Cosmos and Projects counts disagreed. The next session had started. The checks,
on the VM:

1. `python pullmanager.py --tdd`: not yet reported from the VM.
2. The IBD Ancestry pull, once it finishes (D58–D61):
   - `SELECT Sex, COUNT(*) FROM <white PK> GROUP BY Sex` is about `row_mult`
     times the same on the black PK, and the PK phase's `control_sample`
     output agrees.
   - `SELECT PatientDurableKey, BillingCodeValue FROM <OtherDiagnoses> GROUP BY
     PatientDurableKey, BillingCodeValue HAVING COUNT(*) > 1` returns nothing.
   - A patient's `IndexDate` is the earliest `StartDateKey` among their
     disease-code events.
   - `upload_IBD_Meds` lands once, in the first session, and no session loads
     it into Cosmos (the upload phase's `uploads` output says so).
   - Cosmos and Projects row counts agree, with no false warnings, and the
     status tab reads the finished manifest correctly.
3. On the Mac, export a small template with `--export-transfer`: a generated
   PK, a parquet upload with a declared `BIGINT` column, and a batched run
   (explicit `values:`, and a `chunk:`). Copy it and its listed uploads over,
   then Validate, Export split, Dry run and Execute. Check:
   - `upload_<dest>` exists in the project database with the file's types,
     and the Cosmos temp has the same types (`BIGINT`, not `NVARCHAR`).
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
6. An upload of around 250,000 rows, timed: it now travels twice, file to
   Projects, then Projects to Cosmos.
7. During a large transfer, check whether other work on the Projects database
   waits on it. Pullmanager commits after every cohort (D55), but the driver
   runs with autocommit off, so one cohort's `OPENQUERY` into staging still
   sits inside an open transaction until that cohort commits. If it blocks
   others, open the Projects connection with autocommit on.
8. An uploaded PK: a parquet list marked `type: pk`, batched by a column it
   carries. Its uniqueness check and batches should read `upload_<dest>`.

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

First step taken (D57): each project's split and SQL now go to
`runs/<project>/split` and `runs/<project>/sql`, `<project>` from the transfer
YAML's file name (itself from `project_folder`), so projects already run side by
side. The rest of the sketch below would grow inside that folder.

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

### Queueing Transfers

Later, not now: a **Transfer** tab that queues transfer YAMLs. Each template
added gets its transfer version (recipes written out; multipliers and
batching still in their own sections, D49), and the queue is carried to the
VM and run. Open: whether the tab lives in YAML Manager (building the queue on
the Mac), in the launcher (running it on the VM), or both; and whether queued
pulls run one after another or side by side (D57 allows either). The user
expects two or three queued at a time, with one `--execute` starting them all
(today it takes one project, D66).

### Smaller Open Items

- **Generated-table dependencies.** Cohorts reference other generated temps by
  handwritten name (`{{prefix}}_Patients`). It should be structural, so the
  renderer owns temp names.
- **Declaring upload column types in the Builder.** `columns:` with types
  (D54) has no field in the Uploads section yet; add it in the YAML.
- **An uploaded PK is sent to Cosmos whole** in the upload phase of every
  session, even when every run is batched and refills it from the copy
  (D61 left it so). To address later: whether a batched uploaded PK needs to
  go up at all, and once per session.
- **Matching controls.** A control is sampled at `row_mult` times its case per
  batch (D59), so it is matched on the batching columns only. Deeper matching
  (age, and so on) is to address later, as is a control with several case
  levels.
- **`split_after_build` on an uploaded PK** is refused (D54). It could be
  supported by splitting the rows as the copy lands, if a list ever needs it.
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

What is known so far, and how sure, is in design.md (Keys And Relationships In
Cosmos), with how to read a dictionary page. SQL cannot supply relationships;
the interactive data dictionary shows them, one table at a time.

**How sure a fact is, and what may use it.** Every relationship carries one of
three grades:

| Grade | Means | May be used |
| --- | --- | --- |
| Seen | Read off a dictionary page (a screenshot, or its text transcribed) | In `datadictionary.yaml`, and by validation |
| Counted | A query below was run and its result recorded | In `datadictionary.yaml`, and by validation |
| Said | The VM's AI, or Epic convention, with neither of the above | Only as a question to check; never by validation |

The VM's AI answered without running anything and paraphrased the dictionary,
so all it said is *Said* until a page or a count backs it. Asked again, it is
to transcribe a page's text, write "not shown" rather than guess, and report
query results, not what they would mean.

What is left:

1. **Gather them** for the tables in `datadictionary.yaml` (the ones recipes
   use), from each table's dictionary page, read as design.md describes: by
   hand, by screenshot, or by the AI transcribing a page it is given.
2. **Decide the shape.** A structured entry beside the prose, for example:

   ```yaml
   DiagnosisEventFact:
     primary_key: DiagnosisEventKey
     partition_key: StartDateKey
     columns:
       PatientDurableKey:
         type: bigint (foreign key to PatientDim.DurableKey)
         references: PatientDim.DurableKey
   PatientDim:
     primary_key: PatientKey
     one_row_per: [DurableKey]
     when: IsCurrent = 1
   DiagnosisTerminologyDim:
     one_row_per: [DiagnosisKey, Type]
   LabComponentResultFact:
     columns:
       LabComponentKey:
         references: LabComponentDim.LabComponentKey
         sentinels: {-1: unmapped}
   ```

   Open: these names; whether to replace or keep the prose annotation; how to
   write annotations that name a table and no column, or two alternatives; and
   where each entry records its grade.
3. **Confirm with data** on the VM, `COSMOS_SneakPeek` first, then `COSMOS`
   if cheap; a date window on a large fact table. Four query shapes:

   ```sql
   -- Can this login see keys at all? 0 means an empty sys.foreign_keys is
   -- "cannot see", not "none declared".
   SELECT HAS_PERMS_BY_NAME('dbo.PatientDim', 'OBJECT', 'VIEW DEFINITION');

   -- A parent key is one row per key (under its filter, if it keeps history).
   SELECT COUNT_BIG(*) AS rows_total, COUNT_BIG(DISTINCT DurableKey) AS keys
   FROM dbo.PatientDim WHERE IsCurrent = 1;       -- and again without the filter

   -- Child rows with no parent, sentinels aside.
   SELECT COUNT_BIG(*) AS orphans
   FROM dbo.LabComponentResultFact AS c
   LEFT JOIN dbo.LabComponentDim AS p ON p.LabComponentKey = c.LabComponentKey
   WHERE c.LabComponentKey IS NOT NULL AND c.LabComponentKey <> -1
     AND p.LabComponentKey IS NULL;

   -- A one-to-many dimension: how many rows per key, and of which Type.
   SELECT TOP (20) DiagnosisKey, COUNT(*) AS n, STRING_AGG(Type, ', ') AS types
   FROM dbo.DiagnosisTerminologyDim
   GROUP BY DiagnosisKey HAVING COUNT(*) > 1 ORDER BY n DESC;
   ```

   Run the parent check for each parent a `foreign key to ...` annotation
   names, and the orphan check for each child column; how many `-1` (and
   negative) keys each fact table holds is the sentinel count. Record the
   results with their date and database.
4. **Decide what validation flags**, and whether as an error or a warning:
   a join on columns that are not a declared relationship (deliberate non-key
   joins exist); a join to a table with several rows per key and no filter
   that makes it one (`DiagnosisTerminologyDim` without `Type`, `PatientDim`
   without `IsCurrent = 1`); perhaps a large fact table read without its
   partition key.

One correction is already known: our dictionary has `DiagnosisEventFact.DiagnosisKey`
pointing at "DiagnosisDim/DiagnosisTerminologyDim"; the interactive dictionary
says `DiagnosisDim.DiagnosisKey`.

`QMDs/keys_research/` keeps only the example page, which design.md reads. The
brief and the AI's answer are summarized above and in design.md.
