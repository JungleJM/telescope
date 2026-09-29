# Roadmap

What is unbuilt, unverified, or undecided. The only document that carries status: `design.md` describes what exists, `decisions.md` why.

When an item here is built, delete it from this file and describe the result in `design.md`. When it is decided, record the decision in `decisions.md`.

------------------------------------------------------------------------

## Where Things Stand

| Part | State |
|------------------------------------|------------------------------------|
| YAML Manager: validation, dictionary, table binding, pre-YAML, split, manifest | Built and tested |
| The app: model, tkinter Author and Run (D92–D110), opened by `datascope.py` (D112) | Built and tested on the Mac: the model's tests, and the Author view's on a real Tk 8.6, withdrawn. Used by the user on the Mac and once on the VM, where two bugs stopped a pull (fixed, D103); the fixes and D104–D110 since are not yet on the VM |
| Bundle: build, verify, extract, `.local` preservation, carried transfer YAMLs (D79), the queue (D91), the app and `utils/` (D93, D102); `bundle.py` and `bundle_with_yamls.py`, not committed (D106); the dictionary at `reference/` (D111) | Built and tested. `bundle_with_yamls.py` and Make bundle not yet used; the move of the dictionary not yet extracted on the VM |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; used on the VM |
| Pullmanager: connections, session execution, uploads, transfer (D50–D62) | **Proven live**: the first IBD Ancestry pull ran to the end from the terminal. It is being run again on the artifacts bundle |
| `--execute <project>`, the lock, the log, the session readout (D66–D70) | Built and tested on the Mac. On the VM from the second IBD Ancestry run; not yet reported |
| Launcher, now the app's Run half | Opened on the VM; Validate, Export split and Preview work. Execute in its own console window (D68) is not yet tried there |
| Artifacts: parquets, `contents.md`, load scripts, viewer, stock HOW_TO.md, progress and per-table failures (D72–D75, D88, D89); the PK parquet at the PK phase (D87) | Built and tested against a fake Projects connection; the Python load script runs and the R one runs under R `arrow` 25. Not yet run on the VM |

## Known Bugs

- **The join row's match label goes stale.** It refreshes only when the other table or its column changes, so changing this table's column left "number match" beside a VARCHAR-to-BIGINT join (Add join then refused it, correctly).
- **"Validate points here" stays after its message is gone.** It clears only when another section opens, not when a later check is clean.
- **`--tdd` on the VM: 3 failures and 1 error, all Windows-only** (September 2026). The log follower returns `\r\n` as read, so the Pull Log tab shows `\r` and three tests fail; `test_execute_greys_them…` opens a real console whose folder Windows cannot delete (use the fake console); `test_a_window_that_ends…` compares a short (`SHDW_0~1`) path with a long one (compare resolved paths).
- **Execute sometimes ends mid-pull with exit code 1** (the IBD template, September 2026: `CrohnsPatients`, its first Cosmos session, during `upload_cohorts`, after the SneakPeek sessions finished). No summary, and the step left `running`. Every step catches Python errors, so it was either killed (Stop, or anything else on the VM: Windows gives 1) or an error outside the steps, whose traceback reached only the closing window. The log now keeps the traceback, or where a native crash happened; the next occurrence says which. The next Execute resumes it.

------------------------------------------------------------------------

## Next: Fixes, In Order

Agreed with the user (D117 to D128). One commit per item, each with outcome tests.

1.  **The written-SQL checks (D118):** an undefined alias is an error in Validate; the builder checks a line as it is added; renaming an alias rewrites its lines. First, because it let a broken Infant_RSV reach Execute.
2.  **The `--tdd` fixes (Known Bugs):** `\r\n` normalised as the log is read; the fake console; resolved paths. Small, and the VM's own test run becomes a clean signal.
3.  **The operator dropdown (D119) and standard where lines (D120).** BETWEEN was the one Infant_RSV fault the app caused.
4.  **The two stale messages (Known Bugs):** the join match label, and "Validate points here".
5.  **Bundling and names (D122, D123):** one `makebundle.py` carrying the queue, Bundle YAMLs only and its extraction, the queue emptied, projects listed in Exports, `scope.py` on both machines. Together, since each changes the bundle or the extractor.
6.  **The builder's layout (D121, D125):** join row order, Duplicate, collapsed columns, imported description and granularity.
7.  **Run's two dropdowns (D126).**
8.  **`utils.py` and `stock/stock.yaml` (D124).**
9.  **Pasting a CSV (D127).** New; nothing waits on it.

------------------------------------------------------------------------

## Next: The First Live Run

Where it stands: the first IBD Ancestry pull, split on the D64 bundle, finished from VSCodium's terminal (September 2026). A second run of it, on the bundle with artifacts, is under way. Then: package it with Artifacts, and run two pulls side by side (Celiac and IBD, below). The checks, on the VM:

1.  `python pullmanager.py --tdd`: not yet reported from the VM.
2.  On the new bundle (D65–D81):
    - The launcher's Execute opens a console window, the Pull Log tab follows it, and the window stays until `exit` is typed. If it does not open, the message and its terminal command.
    - While it runs, Export split, Execute and Artifacts are grey, and `python pullmanager.py --running` says it is executing.
    - Each session ends with its warnings, then one column-width table.
    - A batch with no values (`state`, D82): each run shows `values_found` and `v3of51 (LA)` as it goes, and `SELECT StateOrProvinceAbbreviation, COUNT(*)` on a destination's PK matches the whole PK.
    - `--artifacts IBD_Ancestry` once it finishes: each file says when it starts, then its rows, size and time, and the run ends with the list of files; the parquets open with `load_parquets.R` and `.py`, and in `viewparquets.py` under the VM's Tk; `PatientDurableKey` is `integer64` in R `arrow` 11 (checked only on 25); `contents.md` and `HOW_TO.md` read right.
    - The PK's parquet appears in `runs/<project>/parquets/` as soon as its PK phase is done, before the first run (D87), and the pk phase's `pk_parquet` output gives its rows.
3.  Two pulls at once: Celiac and an IBD template, exported from the Mac, validated, split, previewed and executed side by side. Each keeps its own run folder, lock, log and temp prefix; the launcher greys only the loaded one's buttons; `--running` lists both.
4.  The IBD Ancestry pull, once it finishes (D58–D61):
    - `SELECT Sex, COUNT(*) FROM <white PK> GROUP BY Sex` is about `row_mult` times the same on the black PK, and the PK phase's `control_sample` output agrees.
    - `SELECT PatientDurableKey, BillingCodeValue FROM <OtherDiagnoses> GROUP BY PatientDurableKey, BillingCodeValue HAVING COUNT(*) > 1` returns nothing.
    - A patient's `IndexDate` is the earliest `StartDateKey` among their disease-code events.
    - `upload_IBD_Meds` lands once, in the first session, and no session loads it into Cosmos (the upload phase's `uploads` output says so).
    - Cosmos and Projects row counts agree, with no false warnings, and the status tab reads the finished manifest correctly.
5.  On the Mac, export a small template with `--export-transfer`: a generated PK, a parquet upload with a declared `BIGINT` column, and a batched run (explicit `values:`, and a `chunk:`). Copy it and its listed uploads over, then Validate, Export split, Preview SQL and Execute. Check:
    - `upload_<dest>` exists in the project database with the file's types, and the Cosmos temp has the same types (`BIGINT`, not `NVARCHAR`).
    - `@@SERVERNAME` is captured and `OPENQUERY` reaches that instance.
    - Cosmos and Projects row counts agree, per batch, with no false warnings.
    - `SELECT _batch, COUNT(*) FROM <destination> GROUP BY _batch` shows each batch once, and a chunked batch's rows all present.
    - An upload over 1000 rows succeeds.
    - A deliberately broken run fails alone while its siblings finish; then `--retry-failed` pulls only that run, and its `_batch` count is right.
    - A second `--execute` says "nothing left to pull".
    - The manifest reads correctly afterwards, in the launcher's status tab too.
6.  Run two pulls with the same prefix at the same time: the second should report a numbered prefix (`tesrun2`) and both should finish.
7.  Check what D50 and D51 rely on: `SELECT OBJECT_ID('tempdb..##<a temp that exists>')` returns a number from our login, and `manifest.cosmos_refresh` is filled in. Note `create_date` either side of the next refresh to confirm a refresh changes it.
8.  An upload of around 250,000 rows, timed: it now travels twice, file to Projects, then Projects to Cosmos.
9.  During a large transfer, check whether other work on the Projects database waits on it. Pullmanager commits after every cohort (D55), but the driver runs with autocommit off, so one cohort's `OPENQUERY` into staging still sits inside an open transaction until that cohort commits. If it blocks others, open the Projects connection with autocommit on.
10. An uploaded PK: a parquet list marked `type: pk`, batched by a column it carries. Its uniqueness check and batches should read `upload_<dest>`.
11. **The app, on the new bundle (D93, D103–D111).** `python pullmanager.py` opens Author and Run under the VM's Tk. The extraction removes the old `YAMLs\datadictionary.yaml` and places `reference\datadictionary.yaml` (20 tables). Open a transfer YAML in Author: no Pending transfer offered, a missing upload file an error; Browse writes a relative path; the HospitalICDCodes rename survives Save and Run's Validate passes; Transfer to Run writes the transfer and an intake in `YAMLs\temp\`. Try the medication codes: a CSV as a supporting table, then In supporting table on the medication table, and check the rows. Check the layout and fonts under Windows, and the mouse wheel. `python utils\transcription_viewer.py` (in the extracted folder) opens.
12. **What a batch costs (D87).** Batching builds the PK once; what repeats per batch is every fact-table query, joined to that batch's PK rows. Whether fifty passes cost about one pull or about fifty depends on whether SQL Server seeks each fact table by `PatientDurableKey` or scans its date range on every pass, which nobody has measured. On `COSMOS_SneakPeek`, run one template three ways, unbatched, `chunk:` and `state` (every value), and compare each run's `duration` in the manifest. If the fact tables are scanned per pass, prefer fewer, larger batches (`chunk: 100000`) to many small ones.

------------------------------------------------------------------------

## Open Problems

### Project Folder Layout

Pulls will run several at a time, so each should get its own folder, named by `project_folder`. The user's sketch:

``` text
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

Most of it is built inside `runs/<project>/` (D57, D72 to D75): `split/`, `sql/`, `logs/`, `parquets/SneakPeek|Cosmos|uploads/`, `contents.md` and the load scripts. What remains of the sketch:

- The renames (`server/`, `Manifest.yaml`, `server/yamls/`): cheap, since manifest paths are already relative to the manifest. Worth doing only if the flat layout gets in the way.
- Packaging SneakPeek as soon as its sessions finish, before the Cosmos ones run. Artifacts already packages only finished tables, so running it mid-pull would do this, except that it refuses while the pull is executing (D67). A `--artifacts` that waits on the lock, or runs from Execute when the last SneakPeek session ends, would.
- Big reference files live in a `data/` folder in the parent directory; templates reference them relative to the template.

### After Artifacts

- **Descriptions without a new split.** `contents.md` reads descriptions from the split (D73), so improving one means splitting again, which resets the pull. Reading them from the transfer YAML instead would need a way to match a multiplied table (`whitePatients_sp`) back to its template cohort.
- **Measured column widths** go only to the console and the log (D70); they could be written to the manifest, so templates can be tuned from data.

### Queueing Transfers

Later, not now: a **Transfer** tab that queues transfer YAMLs. Each template added gets its transfer version (recipes written out; multipliers and batching still in their own sections, D49), and the queue is carried to the VM and run. Open: whether the tab lives in YAML Manager (building the queue on the Mac), in the launcher (running it on the VM), or both; and whether queued pulls run one after another or side by side (D57 allows either). Carrying several transfer YAMLs in one bundle (`makebundle.py yaml=A,B`, D79) is the first piece. The user expects two or three queued at a time, with one `--execute` starting them all (today it takes one project, D66).

### The App, Later

- **Tabs to come across** (D96): the Cohorts cards with their connections, Graph and Recipes.
- **Dark mode, a nice-to-have.** The Mac's Tk follows the system's dark mode already; Windows' Tk 8.6 does not, and styling it by hand is not worth it unless the VM needs it.
- **A later web UI**, if one is wanted, is designed on the model (D92); the old one is retired (D112).
- **Joins to Cosmos tables** in the table builder are written out by hand; picking them from the dictionary would need its keys (Needs Research, below).
- **Typing in a column field re-checks the draft on each key**, since renames change what validation reads; a slower draft may lag. If it does, check only after a pause, as the other fields do.

### A Test Server

The user plans SQL Servers on their homelab (Bluefin) holding fake data, so a pull can be run end to end from the Mac. Choosing it would sit beside Cosmos in the app. The server names are already settings (`PULLMANAGER_COSMOS_SERVER`, `PULLMANAGER_PROJECTS_SERVER`), and the ODBC driver and `pyodbc` (5.3.0 on the VM's list) would be needed on the Mac. Not until the servers exist.

### Smaller Open Items

- **Name the column when Cosmos cannot convert.** Error 8114 (and 245, 8115) names no column. When a cohort fails with one, run `sys.dm_exec_describe_first_result_set` on its `SELECT` (it reads no data) and add each column whose source type differs from its declared one to the failure. Agreed in principle, not yet built.
- **Check the dictionary against the pages.** Only `LabComponentResultFact` has been checked against its dictionary page (D114). The user has screenshotted the other tables (`reference/DDict image refs/`), to record each column's real type and drop SD-only columns (D115, D116). Until a table is checked, its unsized strings are `NVARCHAR(900)`.
- **Generated-table dependencies.** Cohorts reference other generated temps by handwritten name (`{{prefix}}_Patients`). It should be structural, so the renderer owns temp names.
- **An uploaded PK is sent to Cosmos whole** in the upload phase of every session, even when every run is batched and refills it from the copy (D61 left it so). To address later: whether a batched uploaded PK needs to go up at all, and once per session.
- **Matching controls.** A control is sampled at `row_mult` times its case per batch (D59), so it is matched on the batching columns only. Deeper matching (age, and so on) is to address later, as is a control with several case levels.
- **`split_after_build` on an uploaded PK** is refused (D54). It could be supported by splitting the rows as the copy lands, if a list ever needs it.
- **One PK per multiplier group.** Two `type: PK` cohorts in one group are an error (`multiple_pk_cohorts`), so each session has exactly one PK.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis events for those patients: `PKTable` built by joining `PKTable2`). The `pk` phase is one YAML; ordered PK cohorts inside it need a representation.
- **Tests still owed**: duplicate output column names and blank `source` expressions in a cohort.

------------------------------------------------------------------------

## Needs Research

### Primary And Foreign Keys In The Data Dictionary

What is known so far, and how sure, is in design.md (Keys And Relationships In Cosmos), with how to read a dictionary page. SQL cannot supply relationships; the interactive data dictionary shows them, one table at a time.

**How sure a fact is, and what may use it.** Every relationship carries one of three grades:

| Grade | Means | May be used |
|------------------------|------------------------|------------------------|
| Seen | Read off a dictionary page (a screenshot, or its text transcribed) | In `datadictionary.yaml`, and by validation |
| Counted | A query below was run and its result recorded | In `datadictionary.yaml`, and by validation |
| Said | The VM's AI, or Epic convention, with neither of the above | Only as a question to check; never by validation |

The VM's AI answered without running anything and paraphrased the dictionary, so all it said is *Said* until a page or a count backs it. Asked again, it is to transcribe a page's text, write "not shown" rather than guess, and report query results, not what they would mean.

What is left:

1.  **Gather them** for the tables in `datadictionary.yaml` (the ones recipes use), from each table's dictionary page, read as design.md describes: by hand, by screenshot, or by the AI transcribing a page it is given.

2.  **Decide the shape.** A structured entry beside the prose, for example:

    ``` yaml
    DiagnosisEventFact:
      primary_key: DiagnosisEventKey
      partition_key: StartDateKey
      columns:
        PatientDurableKey:
          type: bigint (foreign key to PatientDim.DurableKey)
          references: PatientDim.DurableKey
          grade: seen            # DiagnosisEventFact page, ER diagram
    PatientDim:
      primary_key: PatientKey
      one_row_per: [DurableKey]
      when: IsCurrent = 1
      grade: said               # VM AI; counted once the query below is run
    DiagnosisTerminologyDim:
      one_row_per: [DiagnosisKey, Type]
      grade: said
    LabComponentResultFact:
      columns:
        LabComponentKey:
          references: LabComponentDim.LabComponentKey
          sentinels: {-1: unmapped}
          grade: said
    ```

    Every entry records its grade (`seen`, `counted` or `said`), with a comment saying where it came from, and validation reads only `seen` and `counted`. A `said` entry is a question waiting for its page or its count. Open: these names; whether to replace or keep the prose annotation; how to write annotations that name a table and no column, or two alternatives.

3.  **Confirm with data** on the VM, `COSMOS_SneakPeek` first, then `COSMOS` if cheap; a date window on a large fact table. Four query shapes:

    ``` sql
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

    Run the parent check for each parent a `foreign key to ...` annotation names, and the orphan check for each child column; how many `-1` (and negative) keys each fact table holds is the sentinel count. Record the results with their date and database.

4.  **Decide what validation flags**, and whether as an error or a warning: a join on columns that are not a declared relationship (deliberate non-key joins exist); a join to a table with several rows per key and no filter that makes it one (`DiagnosisTerminologyDim` without `Type`, `PatientDim` without `IsCurrent = 1`); perhaps a large fact table read without its partition key.

One correction is already known: our dictionary has `DiagnosisEventFact.DiagnosisKey` pointing at "DiagnosisDim/DiagnosisTerminologyDim"; the interactive dictionary says `DiagnosisDim.DiagnosisKey`.

The example page is kept in `reference/DDict image refs/`, with the user's screenshots of the other tables; design.md reads it. The brief and the AI's answer are summarized above and in design.md.