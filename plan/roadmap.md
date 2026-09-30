# Roadmap

What is unbuilt, unverified, or undecided. The only document that carries status: `design.md` describes what exists, `decisions.md` why.

When an item here is built, delete it from this file and describe the result in `design.md`. When it is decided, record the decision in `decisions.md`.

------------------------------------------------------------------------

## Where Things Stand

| Part | State |
|------------------------------------|------------------------------------|
| YAML Manager: validation, dictionary, table binding, pre-YAML, split, manifest | Built and tested |
| The app: model, tkinter Author and Run (D92–D127), opened by `scope.py` (D112, D123) | Built and tested on the Mac: the model's tests, and the Author view's on a real Tk 8.6, withdrawn. Used on the Mac and on the VM up to D110; D117–D131 not yet on the VM |
| Bundle: build, verify, extract, `.local` preservation, carried transfer YAMLs (D79), the queue (D91, D122), YAMLs only (D122), `scope.py` and `utils.py` (D123, D124), not committed (D106); the dictionary at `reference/` (D111, D117) | Built and tested. `bundle_with_yamls.py` was used once; the D122 bundle is next (above) |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; used on the VM |
| Pullmanager: connections, session execution, uploads, transfer (D50–D62) | **Proven live**: the first IBD Ancestry pull ran to the end from the terminal. It is being run again on the artifacts bundle |
| `--execute <project>`, the lock, the log, the session readout (D66–D70) | Built and tested on the Mac. On the VM from the second IBD Ancestry run; not yet reported |
| Execute's progress lines: each step's start and end, timed, as it happens (D136); each table's rows in the summary, the manifest and the Status tab (D137) | Built and tested on the Mac; in the bundle `77742a9a…` (29 September 2026), not yet on the VM |
| Table groups (D134, D138), recipe sets (D135), the table-order check, the multiplied-read warning (D139) | Built and tested on the Mac; in the bundle `77742a9a…` (29 September 2026). Not yet run on the VM: the first live group is the question of what a group costs (below) |
| Launcher, now the app's Run half; its three dropdowns (D126, D140) | Opened on the VM; Validate, Export split, Preview and Execute used there, and the two dropdowns of D126. The third, and the pulls' finished and stopped words, not yet |
| How Execute ended, in the manifest (D140); Artifacts after a clean pull (D141); the run folder's layout (D142); QuickEdit off in Execute's window (D143) | Built and tested on the Mac; in the bundle `77742a9a…` (29 September 2026), not yet on the VM. QuickEdit tested against a fake console only |
| `utils/clear_projects_db.py`: a project database's tables and space, and dropping them (D133) | Built and tested against a fake database. A scratch version of it, run on the VM (29 September 2026), dropped all 71 tables and freed the data file; the utility itself is in the bundle `77742a9a…` (29 September 2026) |
| Artifacts: parquets, `contents.md`, load scripts, the stock list (D124), progress and per-table failures (D72–D75, D88, D89); the PK parquet at the PK phase (D87) | Built and tested against a fake Projects connection; the Python load script runs and the R one runs under R `arrow` 25. Run on the VM for Celiac; `stock.yaml` not yet |

## Known Bugs

- **Execute sometimes ends mid-pull with exit code 1** (the IBD template, September 2026: `CrohnsPatients`, its first Cosmos session, during `upload_cohorts`, after the SneakPeek sessions finished). No summary, and the step left `running`. Every step catches Python errors, so it was either killed (Stop, or anything else on the VM: Windows gives 1) or an error outside the steps, whose traceback reached only the closing window. The log now keeps the traceback, or where a native crash happened; the next occurrence says which. The next Execute resumes it.

------------------------------------------------------------------------

## Next: The Remade Pulls On The VM

The user stopped every run and cleared the VM's pulls to start over (29 September 2026). The bundle sent that morning carried D117 to D131 and six pulls, each remade for the checked dictionary (D130): Celiac, IBD_Ancestry, Infant_RSV, Crohns_DxHxSxRx, Crohns_PatientsFromUpload and UC_Visits. Checked on the Mac: each validates, splits and dry-runs; Celiac's SQL reads only `K90.0`; every fact-table query joins its PK. On the VM:

0.  **Before extracting the bundle `77742a9a…` (29 September 2026)** (59 files, the runtime alone). Pulls in the old layout (`runs\<project>\split\`) are not found by the new runtime (D142): let any pull that matters finish, and run Artifacts on it, first. After the update each pull is split again, which starts it over.
1.  **Delivery.** `python bundle.py`, then `python scope.py --tdd`: the three Windows-only failures and the error should be gone (two of the fixes show only on Windows). `pullmanager.py` beside the folder is removed; `scope.py` and `utils.py` are there, and `python utils.py` lists the viewer, the transcription viewer and `clear_projects_db`: Refresh shows the project database's tables and space.
2.  **Upload files beside the transfers**, at the paths the build names: IBD_Ancestry's `data\Meds\ibd\IBD_Meds.parquet`; Crohns_DxHxSxRx's and Crohns_PatientsFromUpload's `CrohnsPatients.parquet`; UC_Visits' `UCPatients.parquet`; `data\Codes\ICD-hosp.csv` (Crohns_DxHxSxRx, UC_Visits); Crohns_DxHxSxRx's `data\Meds\IBD_meds.parquet`; Celiac's `csv\HospitalICDCodes.csv`.
3.  **Celiac** first: its PK should be celiac patients (`K90.0`). The earlier Celiac run pulled Crohn's: the transfer sent had `K50.%`.
4.  **The batching tests.** Crohns_PatientsFromUpload finished on the old bundle (29 September 2026): its upload 1,284,756 rows in 2m 31s (both legs together, since the old bundle did not time them apart), the PK registered in 0s, and its one run, 13 chunks, in 5m 43s. Its window then sat at the session's name with the lock held, paused by QuickEdit (D143). Still to check: that the rows add up to the PK's. Crohns_PatientsFromUpload is every Crohn's patient's PatientDim row, the uploaded PK (1.2 million) in 100,000-row chunks: does each chunk land, and do the rows add up to the PK's? IBD_Ancestry is now all patients, not a sample, batched by sex and in 30,000-patient chunks (24 runs, each chunked once its PK exists): the test of chunking a generated PK.
5.  **Infant_RSV**, **Crohns_DxHxSxRx** and **UC_Visits**. Infant_RSV ran to the end on 29 September 2026 (exit code 0, both sessions, the PK 186,963 rows); it looked stopped after the PK only because the old bundle said nothing per table and only the PK's parquet is written before Artifacts (D137). Its first run failed at `setup` with error 1105: the project database was full (Space In The Projects Database, in design.md). Its tables were dropped and three pulls restarted (29 September 2026); not yet reported. The log stood at 12.6 of its 20,000 MB, held by an open transaction: it is to be ended (an SSMS tab's `COMMIT`, or closing SSMS) and checked with `clear_projects_db`'s Refresh.
6.  **The progress lines** (D136), once the bundle `77742a9a…` (29 September 2026) is in: the Pull Log tab shows each table as it starts and lands, with its rows and times; the closing summary and the Status tab list each table's rows under its run (D137). Crohns_PatientsFromUpload's upload lines time its two legs, file to Projects and Projects to Cosmos: the cost each table group adds (D134).
7.  **Run** chooses from its three dropdowns (D140): a running pull is under Running pulls only; one that has run under Finished and stopped pulls with its word; Start run lists only pulls not yet run. Stop one, and it reads `(stopped by user)`. The dictionary line names `reference\datadictionary.yaml` in the extracted folder.
8.  **The run folder** (D142): after Export split, `runs\<project>\` holds the manifest, the transfer YAML's copy and `pull_files\`; after Execute, its log beside the manifest, the earlier ones in `older_logs\`. A clean pull ends by packaging itself (D141): `cosmos_parquets\` and the rest appear without pressing Artifacts, and `load_parquets` opens them.
9.  **QuickEdit** (D143): click inside Execute's window while it runs. The title should not change to "Select", and the lines keep coming.

------------------------------------------------------------------------

## Next: Fixes, In Order

Agreed on 29 September 2026. One commit each, with its outcome tests.

1.  **The credit line** (D145): every window says who made it. Smallest.
2.  **The Pull Manifest tab** (D144): Run's window only.
3.  **The parquet viewer** (task list, Parquet Viewer): waits on the user's answers to its questions 1 to 3. The copy Artifacts puts in each pull's folder stays, so a client can open the parquets there.

------------------------------------------------------------------------

## Next: The First Live Run

Where it stands: the first IBD Ancestry pull, split on the D64 bundle, finished from VSCodium's terminal (September 2026). A second run of it, on the bundle with artifacts, is under way. Then: package it with Artifacts, and run two pulls side by side (Celiac and IBD, below). The checks, on the VM:

1.  `python scope.py --tdd`: see The Remade Pulls, above.
2.  On the new bundle (D65–D81):
    - The launcher's Execute opens a console window, the Pull Log tab follows it, and the window stays until `exit` is typed. If it does not open, the message and its terminal command.
    - While it runs, Export split, Execute and Artifacts are grey, and `python scope.py --running` says it is executing.
    - Each session ends with its warnings, then one column-width table.
    - A batch with no values (`state`, D82): each run shows `values_found` and `v3of51 (LA)` as it goes, and `SELECT StateOrProvinceAbbreviation, COUNT(*)` on a destination's PK matches the whole PK.
    - `--artifacts IBD_Ancestry` once it finishes: each file says when it starts, then its rows, size and time, and the run ends with the list of files; the parquets open with `load_parquets.R` and `.py`, and in `viewparquets.py` under the VM's Tk; `PatientDurableKey` is `integer64` in R `arrow` 11 (checked only on 25); `contents.md` and `HOW_TO.md` read right.
    - The PK's parquet appears in `runs/<project>/cosmos_parquets/` (or `sneakpeek_parquets/`) as soon as its PK phase is done, before the first run (D87), and the pk phase's `pk_parquet` output gives its rows.
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
11. **The app, on the new bundle (D93, D103–D131).** `python scope.py` opens Author and Run under the VM's Tk. The extraction places `reference\datadictionary.yaml` (21 tables, D130). Open a transfer YAML in Author: no Pending transfer offered, a missing upload file an error; Browse writes a relative path; the HospitalICDCodes rename survives Save and Run's Validate passes; Transfer to Run writes the transfer and an intake in `YAMLs\temp\`. Try the medication codes: a CSV as a supporting table, then In supporting table on the medication table, and check the rows. Check the layout and fonts under Windows, and the mouse wheel. `python utils.py` opens the utilities, and each opens from there (D124).
12. **What a batch costs (D87).** Batching builds the PK once; what repeats per batch is every fact-table query, joined to that batch's PK rows. Whether fifty passes cost about one pull or about fifty depends on whether SQL Server seeks each fact table by `PatientDurableKey` or scans its date range on every pass, which nobody has measured. On `COSMOS_SneakPeek`, run one template three ways, unbatched, `chunk:` and `state` (every value), and compare each run's `duration` in the manifest. If the fact tables are scanned per pass, prefer fewer, larger batches (`chunk: 100000`) to many small ones.

------------------------------------------------------------------------

## Open Problems

### Project Folder Layout

The layout is built (D142). Still open:

- Packaging SneakPeek as soon as its sessions finish, before the Cosmos ones run. Artifacts packages only finished tables, so running it mid-pull would do this, except that it refuses while the pull is executing (D67). Execute packages at the end of a clean pull (D141); a `--artifacts` that waits on the lock, or runs from Execute when the last SneakPeek session ends, would package earlier.
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
- **Finish checking the dictionary against the pages.** Most tables are checked (D130). The dictionary's foot lists the rest (ProblemListFact, TerminologyConceptDim, VitalsFact, CoverageDim, and the parts only partly seen), and the tables a checked foreign key points at that it lacks (DateDim, DiagnosisDim, the bridges...): screenshot one before a pull joins it. Until a table is checked, its unsized strings are `NVARCHAR(900)`.
- **Generated-table dependencies.** Cohorts reference other generated temps by handwritten name (`{{prefix}}_Patients`). It should be structural, so the renderer owns temp names. Under multipliers a fact table read this way names the base table, not its level's copy (`UCOrders`), and fails at Execute; validation warns meanwhile (D139).
- **What a table group costs.** From Crohns_PatientsFromUpload's progress lines (D136): how long its upload's second leg took, Projects to Cosmos. A group repeats about that, so it is what each group adds (D134).
- **An uploaded PK is sent to Cosmos whole** in the upload phase of every session, even when every run is batched and refills it from the copy (D61 left it so). To address later: whether a batched uploaded PK needs to go up at all, and once per session.
- **Matching controls.** A control is sampled at `row_mult` times its case per batch (D59), so it is matched on the batching columns only. Deeper matching (age, and so on) is to address later, as is a control with several case levels.
- **`split_after_build` on an uploaded PK** is refused (D54). It could be supported by splitting the rows as the copy lands, if a list ever needs it.
- **One PK per multiplier group.** Two `type: PK` cohorts in one group are an error (`multiple_pk_cohorts`), so each session has exactly one PK.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis events for those patients: `PKTable` built by joining `PKTable2`). The `pk` phase is one YAML; ordered PK cohorts inside it need a representation.
- **Space in the project database.** Pullmanager neither checks the data file's and log's room before a pull nor drops a finished pull's tables, so a full database is found only when `setup` fails (D133). It could say, at Validate or before `setup`, how full each file is, and name the tables of pulls already packaged as parquets.
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