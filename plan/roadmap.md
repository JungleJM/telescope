# Roadmap

What is unbuilt, unverified, or undecided. The only document that carries status: `design.md` describes what exists, `decisions.md` why.

When an item here is built, delete it from this file and describe the result in `design.md`. When it is decided, record the decision in `decisions.md`.

------------------------------------------------------------------------

## Where Things Stand

| Part | State |
|------------------------------------|------------------------------------|
| YAML Manager: validation, dictionary, table binding, pre-YAML, split, manifest | Built and tested |
| The app: model, tkinter Author and Run (D92–D131), opened by `scope.py` (D112, D123) | Built and tested on the Mac: the model's tests, and the Author view's on a real Tk 8.6, withdrawn. In daily use on the Mac and the VM; the six remade pulls were authored in it |
| Bundle: build, verify, extract, `.local` preservation, carried transfer YAMLs (D79), the queue (D91, D122), YAMLs only (D122), `scope.py` and `utils.py` (D123, D124), not committed (D106); the dictionary at `reference/` (D111, D117) | Built and tested; used on the VM for every update. The morning bundle of 29 September 2026 carried the six remade pulls through the queue |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; used on the VM |
| Pullmanager: connections, session execution, uploads, transfer (D50–D62) | **Proven live** on many pulls since the first IBD Ancestry run (25 September 2026): three at once (26 September), and on 29 September Infant_RSV to the end (exit code 0, PK 186,963 rows) and Crohns_PatientsFromUpload (a 1,284,756-row parquet upload through both legs, 13 chunks) |
| `--execute <project>`, the lock, the log, the session readout (D66–D70) | Used on the VM for every run since 25 September 2026, several pulls side by side, each in its own console |
| Launcher, now the app's Run half; its three dropdowns (D126, D140) | Opened on the VM; Validate, Export split, Preview and Execute used there, and the two dropdowns of D126. The third, and the pulls' finished and stopped words, not yet |
| On the VM in the bundle `e126f85b…` (29 September 2026), not yet checked there: progress lines and each table's rows (D136, D137); how Execute ended (D140); Artifacts after a clean pull (D141); the run folder (D142); QuickEdit off (D143, tested against a fake console only); the Pull Manifest tab (D144); the credit line (D145); the parquet viewer's buttons (D146); extraction refusing while a pull executes, and the bundle in every window (D147); the deliverable pull folder (D148); the backup (D149); the dictionary line out (D150) | Built and tested on the Mac. The bundle's `--tdd` failed two window tests on Windows only, fixed in the tests in `0f81dc63…`, which is extracted and passes `--tdd` |
| Errors hidden in a batch (D151), the run scan (D152), manifest saves that wait and readers that let them (D153, D154), the dictionary audit and `dictionary-fix` (D155), the column check (D156), rows per join key (D157), re-pulling chosen sessions (D158), unquoted CSV headers (D159) | On the VM since the bundle `048c3f29…` (30 September 2026). The scan and the audit have run there; the show-stopper tests are under way (below): manifest saves past a held manifest proven (D175) |
| One blueprint per project (D162), Projects tables named per pull (D163), a database per pull (D164, D170, D171), drops after packaging (D165), Run and the title (D166), parquets from Status (D167), the Filters layout (D168), messages at their fields (D169) | Built and tested on the Mac; on the VM since `c98c60b3…` (1 October 2026). Seen working there: blueprints in `YAMLs\temp`, the table prefix in the manifest and SQL, the database choice, the title, the dropdowns and bold line, the step in flight, the Filters layout, messages at their fields. The bundles since: `5754255e…` (the six databases, listed in the code only), then `b822ab77…` (2 October 2026) |
| Table groups (D134, D138), recipe sets (D135), the table-order check, the multiplied-read warning (D139) | Built and tested on the Mac; on the VM in the bundle `e126f85b…`. No pull uses a group yet: the first is the question of what a group costs (below) |
| The refresh margin (D183), the Projects connection per table group and the retry on an expired sign-in (D176, D188), the join check (D178), recipes shipped for a while (D187), DepartmentDim in the dictionary | Built and tested on the Mac (2 October 2026), in the bundle `b822ab77…` with the HaT PheWAS blueprint; not yet extracted on the VM |
| `utils/clear_projects_db.py`: a project database's tables and space, and dropping them (D133) | Built and tested against a fake database. A scratch version of it, run on the VM (29 September 2026), dropped all 71 tables and freed the data file; the utility itself is on the VM in the bundle `e126f85b…` |
| Artifacts: parquets, `contents.md`, load scripts, the stock list (D124), progress and per-table failures (D72–D75, D88, D89); the PK parquet at the PK phase (D87) | Built and tested against a fake Projects connection; the Python load script runs and the R one runs under R `arrow` 25. Run on the VM for Celiac; the PK parquet seen there (Infant_RSV); `stock.yaml` not yet |

## Known Bugs

- **Execute sometimes ends mid-pull with exit code 1** (the IBD template, September 2026: `CrohnsPatients`, its first Cosmos session, during `upload_cohorts`, after the SneakPeek sessions finished). No summary, and the step left `running`. Every step catches Python errors, so it was either killed (Stop, or anything else on the VM: Windows gives 1) or an error outside the steps, whose traceback reached only the closing window. The log now keeps the traceback, or where a native crash happened; the next occurrence says which. The next Execute resumes it.

------------------------------------------------------------------------

## Next: Fixes, In Order

Agreed 2 October 2026. One commit each, with its outcome test. The first three (D183, D176, D178) are built and in the bundle `b822ab77…`; the rest wait until HaT PheWAS and Infant_RSV have run on it, so a fault found there has one cause.

1.  **Package each table group as it finishes, then empty its tables** (D177). The largest; its own bundle.
2.  **The Diagnoses recipe set and HospitalizationsWithinICDCode** (D184).
3.  **Specify Project DB** (D179).
4.  **clear_projects_db over every database, by pull** (D185), then **View dbo tables and the multi-column view in Run** (D180).
5.  **Row key and deduplication in Author** (D186), one column picker.
6.  **Counts from `profile:`** (D181), on the same picker, in its own order: Count for the PK, then the after-PK profile and the report, then fact tables in Count.

------------------------------------------------------------------------

## Next: On The VM, The Bundle `b822ab77…`

Built 2 October 2026: the software of D151 to D171, the window tests' fix, recipes (D187), the refresh margin (D183), the Projects connection per group and the sign-in retry (D176, D188), the join check (D178), and the HaT PheWAS blueprint. Extract it once no pull is executing (the VM's last scan named `c98c60b3`, so `5754255e…` may never have gone over). If the VM's `datascope.json` names `projects_databases`, delete that line first: it is refused now (D171).

1.  **HaT PheWAS**, the bundle's blueprint (`YAMLs\temp\HaT_PheWAS_blueprint.yaml`): Start run, Execute. Four tables, `Dual`, 1,000-patient chunks; about 5,967 patients expected. What to check after it, and what to cull, is in `HaT Considerations.md` at the repository root.
2.  **Infant_RSV again**, with Re-pull everything (agreed 2 October 2026: folders named `Cosmos` and `Cosmos_SneakPeek`, which nothing in Scope makes, sat beside its parquets, so it is pulled again to be sure). Its parquet folders and those two are already moved to `runs\Infant_RSV\old\`; the manifest, `pull_files\` and logs stay. The fresh `cosmos_parquets` are the ones to keep.
3.  **Seen as these run**: a connection on another instance logs `another instance, same refresh (D183)` and nothing starts over; each table group says `new Projects connection`; a session past 10 hours either finishes or logs `sign-in refused (expired?)` and goes on (D176). None of these has been seen live.
4.  **The show-stopper tests** (D151, D153, D154). Done: `--tdd` (two window tests listed the VM's real pulls; fixed in the tests, in this bundle), the reader check, step 3 (D175) and step 5 (`test_over_zero` FAILED with `Divide by zero encountered (8134)`); the scan listed only what was expected. Left: step 4, checked after each real pull rather than overnight (no `exit code 1` so far): `Select-String -Path runs\*\execute-*.log -Pattern "FileBusy|Traceback|exit code 1"` prints nothing. Then the clean-up: in clear_projects_db drop the tables starting with ShowTest2's and test_over_zero's prefixes (each manifest's `table_prefix`), and delete `runs\ShowTest`, `runs\ShowTest2`, `runs\test_over_zero` and any of their blueprints in `YAMLs\temp`.
5.  **After the hand rescue of 1 October** (Crohns, UC: `save_landed.py`): once each pull packages, its emptied tables are empty parquets; copy `saved_parquets\cosmos_parquets\*` and `saved_parquets\sneakpeek_parquets\*` over them, then drop the pull's tables in clear_projects_db (the scan lists the emptied tables, so the automatic drop does not happen). UC's split was fixed by hand for `CrohnsPatientInfo`; its working blueprint on the VM needs the same five lines (`{{PKTable}}`), as the Mac's copies now have.
6.  **A pull's first clean finish under D162–D165**: its log ends with `Dropped its N table(s) from PROJECTD...` (or says why they were kept), clear_projects_db no longer lists them, and `Removed <project>_blueprint.yaml from YAMLs/temp`; Author then offers it as its run folder's copy, and Run, choosing it, says only Re-pull everything pulls it again. The DROP permission is untested.
7.  **A second new pull** takes a database the first does not use, and its manifest's `database_choice` says why. A pull split before D163, not yet executed, is split again first, so its tables are prefixed and dropped.
8.  **The dictionary's true-only fix**: once the runs of 1 October are done, `python3.13 scope.py dictionary-fix reference/Audit/dictionary_audit_true_only.yaml` on the Mac, the YAMLs it names checked, and a bundle. The `false` findings wait for the builder to leave new columns nullable (Smaller Open Items).
9.  **Re-pull IBD_Ancestry's white sessions** (D158): Re-pull sessions, `UCwhitePatients` and `CrohnswhitePatients`. They land, or fail loudly with the server's message (D151), which is the cause to report.
10. **Seen as it runs**: `setup` prints "columns checked" (D156); Status has Median per key, P90 and Max (D157); the log holds `server:` lines (D151).
11. **Crohns_DxHxSxRx's Meds group**, retried after `ReadyToDispenseDateKey` was taken out by hand (not in Cosmos): not yet reported.
12. **A supporting CSV with a quoted header** (D159) lands without its quotes.
13. **Small checks for later**: double-clicking a packaged table in Status opens the parquet viewer on Windows (D167).

------------------------------------------------------------------------

## Next: The Remade Pulls On The VM

The user stopped every run and cleared the VM's pulls to start over (29 September 2026). Six pulls were remade for the checked dictionary (D130): Celiac, IBD_Ancestry, Infant_RSV, Crohns_DxHxSxRx, Crohns_PatientsFromUpload and UC_Visits. Checked on the Mac: each validates, splits and dry-runs; Celiac's SQL reads only `K90.0`; every fact-table query joins its PK. Pulls split before `e126f85b…` are in the old layout (`runs\<project>\split\`), which the new runtime does not find (D142); each is split again, which starts it over. The bundle `0f81dc63…` is extracted, its `--tdd` passes, and every pull's upload files are in place. Still to see on the VM:

1.  **Celiac**: its PK should be celiac patients (`K90.0`). The earlier Celiac run pulled Crohn's: the transfer sent had `K50.%`.
2.  **The batching tests.** Crohns_PatientsFromUpload (the uploaded Crohn's PK, 1.2 million, in 100,000-row chunks) finished on the old bundle: upload 1,284,756 rows in 2m 31s, both legs together; its one run, 13 chunks, in 5m 43s. Still to check: that each chunk landed and the rows add up to the PK's. IBD_Ancestry is now all patients, batched by sex and in 30,000-patient chunks (24 runs, each chunked once its PK exists): the test of chunking a generated PK. Once it finishes (D58–D61):
    - `SELECT Sex, COUNT(*) FROM <white PK> GROUP BY Sex` is about `row_mult` (4) times the same on the black PK, and the PK phase's `control_sample` output agrees.
    - `SELECT PatientDurableKey, BillingCodeValue FROM <OtherDiagnoses> GROUP BY PatientDurableKey, BillingCodeValue HAVING COUNT(*) > 1` returns nothing.
    - A patient's `IndexDate` is the earliest `StartDateKey` among their disease-code events.
    - `upload_IBD_Meds` lands once, in the first session, and no session loads it into Cosmos (the upload phase's `uploads` output says so).
    - `SELECT _batch, COUNT(*) FROM <destination> GROUP BY _batch` shows each batch once, and Cosmos and Projects row counts agree per batch, with no false warnings.
3.  **Infant_RSV**, **Crohns_DxHxSxRx** and **UC_Visits**. Infant_RSV ran to the end on 29 September 2026 (exit code 0, both sessions, the PK 186,963 rows) after its first run failed at `setup` with error 1105, the project database full (Space In The Projects Database, in design.md). Crohns_DxHxSxRx and UC_Visits have run; what is still wrong with them is in the bundle `1b628fc3…`'s list, above.
4.  **The progress lines** (D136): the Pull Log tab shows each table as it starts and lands, with its rows and times; the closing summary and the Status tab list each table's rows under its run (D137). An upload's lines time its two legs, file to Projects and Projects to Cosmos: the cost each table group adds (D134).
5.  **Run** chooses from its three dropdowns (D140): a running pull is under Running pulls only; one that has run under Finished and stopped pulls with its word; Start run lists only pulls not yet run. Stop one, and it reads `(stopped by user)`.
6.  **The run folder** (D142): after Export split, `runs\<project>\` holds the manifest, the transfer YAML's copy and `pull_files\`; after Execute, its log beside the manifest, the earlier ones in `older_logs\`. A clean pull ends by packaging itself (D141): `cosmos_parquets\` and the rest appear without pressing Artifacts, and `load_parquets` opens them (`PatientDurableKey` should be `integer64` under the VM's R `arrow` 11; checked only on 25).
7.  **QuickEdit** (D143): click inside Execute's window while it runs. The title should not change to "Select", and the lines keep coming.
8.  **The Pull Manifest tab** (D144): on a pull with a failure, the tab shows the manifest with its error red; double-clicking the failed row in Status shows that error, highlighted. Then `--retry-failed` pulls only that run, and a second `--execute` says "nothing left to pull".
9.  **The parquet viewer** (D146): from `python utils.py`, the dropdown lists the pulls that have run, as Run does; a pull opens on its Cosmos tables; a third table replaces the older of two. The copy in a pull's folder opens on that pull alone. A large lab table opens quickly now that only the page shown is read into Python.
10. **The credit line** (D145) at the foot of each window, the viewer in a pull's folder included, with `· bundle` and the same 8 characters `bundle.py` showed as its `content_id` (D147); `python scope.py --version` says it too.
11. **Extraction refuses while a pull executes** (D147): with a pull running, `python bundle.py` names it and extracts nothing. Once nothing runs, it says it removed the previous `pullmanager_runtime`.
12. **The backup** (D149): in Run, Browse a backup folder on the other drive; Artifacts on a packaged pull says `backed up to <folder>\<project>`, and the old parquets are there. With the drive unplugged, it goes to `runs\backup` with a warning at the end. Back up all backs up each pull, skipping one executing.
13. **A pull's folder as a deliverable** (D148): after Artifacts, `utils.py` in it opens a window with the parquet and transcription viewers only; the viewer opens on that pull. Your own `python utils.py` shows Client and Manager.
14. **The dictionary line is gone from Run** (D150), and Validate still names `reference\datadictionary.yaml` in the extracted folder.

### Still Unchecked From The First Live Runs

Most of the first-live-run list was settled by use: Execute's own console and the Pull Log tab, pulls side by side, the PK parquet at the PK phase (D87), Artifacts on Celiac, uploads over 1,000 rows and an uploaded PK (Crohns_PatientsFromUpload), and `OPENQUERY` reaching the captured `@@SERVERNAME` (every upload's second leg). What no run has shown yet, cheap to read off any finished pull:

- **The numbered prefix** (D50): every pull uses the default `tesrun`, so pulls side by side must have taken `tesrun2` and on. A manifest's `session.runtime.temp_prefix` and its warning would confirm it.
- **What D50 and D51 rely on**: `SELECT OBJECT_ID('tempdb..##<a temp that exists>')` returns a number from our login, and `manifest.cosmos_refresh` is filled in. Note `create_date` either side of the next refresh to confirm a refresh changes it.
- **An upload's types**: `upload_<dest>` in the project database has the file's types, and the Cosmos temp the same (`BIGINT`, not `NVARCHAR`).
- **Blocking during a transfer**: whether other work on the Projects database waits on a large one. Pullmanager commits after every cohort (D55), but the driver runs with autocommit off, so one cohort's `OPENQUERY` into staging sits inside an open transaction until that cohort commits. If it blocks others, open the Projects connection with autocommit on.
- **A batch with no values** (`state`, D82): no current pull uses one. When one does, each run shows `values_found` and `v3of51 (LA)` as it goes, and the destination's PK counted by the column matches the whole PK.

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

Later, not now: a **Transfer** tab that queues transfer YAMLs. Each template added gets its transfer version (recipes written out; multipliers and batching still in their own sections, D49), and the queue is carried to the VM and run. Open: whether the tab lives in YAML Manager (building the queue on the Mac), in the launcher (running it on the VM), or both; and whether queued pulls run one after another or side by side (D57 allows either). Carrying several in one bundle is built (the queue, D91, D122: six pulls went over together on 29 September 2026), and pulls already run side by side, each started by hand. The user expects two or three queued at a time, with one `--execute` starting them all (today it takes one project, D66).

### The App, Later

- **Tabs to come across** (D96): the Cohorts cards with their connections, Graph and Recipes.
- **A Parquets tab** after Author and Run: the parquet viewer as a tab of its own (D180).
- **Dark mode, a nice-to-have.** The Mac's Tk follows the system's dark mode already; Windows' Tk 8.6 does not, and styling it by hand is not worth it unless the VM needs it.
- **A later web UI**, if one is wanted, is designed on the model (D92); the old one is retired (D112).
- **Joins to Cosmos tables** in the table builder are written out by hand; picking them from the dictionary would need its keys as data (A Join Check, below).
- **Typing in a column field re-checks the draft on each key**, since renames change what validation reads; a slower draft may lag. If it does, check only after a pause, as the other fields do.

### A Test Server

The user plans SQL Servers on their homelab (Bluefin) holding fake data, so a pull can be run end to end from the Mac. Choosing it would sit beside Cosmos in the app. The server names are already settings (`PULLMANAGER_COSMOS_SERVER`, `PULLMANAGER_PROJECTS_SERVER`), and the ODBC driver and `pyodbc` (5.3.0 on the VM's list) would be needed on the Mac. Not until the servers exist.

### Smaller Open Items

- **Recipes ship for a while** (D187). When the user says to stop: remove the `recipes` entry from `COMPANION_FILES` in `bundle_pullmanager.py`, put back the bundle tests' expectation that no recipes file travels, and the next extraction removes the VM's copy (an edited one kept as `.local`).
- **The builder leaves new columns nullable** (D172), so a table's nullability in Cosmos stops becoming a row filter (`IS NOT NULL`, which turns a LEFT JOIN into an INNER one), and the audit's 327 `false` findings can then be written into the dictionary as facts. Agreed, not yet built: `_column_from` in the model.
- **Name the column when Cosmos cannot convert.** Error 8114 (and 245, 8115) names no column. When a cohort fails with one, run `sys.dm_exec_describe_first_result_set` on its `SELECT` (it reads no data) and add each column whose source type differs from its declared one to the failure. Agreed in principle, not yet built.
- **Generated-table dependencies.** Cohorts reference other generated temps by handwritten name (`{{prefix}}_Patients`). It should be structural, so the renderer owns temp names. Under multipliers a fact table read this way names the base table, not its level's copy (`UCOrders`), and fails at Execute; validation warns meanwhile (D139).
- **What a batch costs (D87).** Batching builds the PK once; what repeats per batch is every fact-table query, joined to that batch's PK rows. Whether fifty passes cost about one pull or about fifty depends on whether SQL Server seeks each fact table by `PatientDurableKey` or scans its date range on every pass, which nobody has measured. IBD_Ancestry's 24 runs, now timed per table (D136), are the first evidence; a clean comparison is one template three ways on `COSMOS_SneakPeek`, unbatched, `chunk:` and `state`, comparing each run's `duration`. If the fact tables are scanned per pass, prefer fewer, larger batches (`chunk: 100000`).
- **What a table group costs.** From Crohns_PatientsFromUpload's progress lines (D136): how long its upload's second leg took, Projects to Cosmos. A group repeats about that, so it is what each group adds (D134).
- **An uploaded PK is sent to Cosmos whole** in the upload phase of every session, even when every run is batched and refills it from the copy (D61 left it so). To address later: whether a batched uploaded PK needs to go up at all, and once per session.
- **Matching controls.** A control is sampled at `row_mult` times its case per batch (D59), so it is matched on the batching columns only. Deeper matching (age, and so on) is to address later, as is a control with several case levels.
- **`split_after_build` on an uploaded PK** is refused (D54). It could be supported by splitting the rows as the copy lands, if a list ever needs it.
- **One PK per multiplier group.** Two `type: PK` cohorts in one group are an error (`multiple_pk_cohorts`), so each session has exactly one PK.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis events for those patients: `PKTable` built by joining `PKTable2`). The `pk` phase is one YAML; ordered PK cohorts inside it need a representation.
- **Space in the project database.** Pullmanager neither checks the data file's and log's room before a pull nor drops a finished pull's tables, so a full database is found only when `setup` fails (D133). It could say, at Validate or before `setup`, how full each file is, and name the tables of pulls already packaged as parquets.
- **Tests still owed**: duplicate output column names and blank `source` expressions in a cohort.

### A Join Check From The Dictionary's Keys

Held off (30 September 2026): the user chose to wait on it.

No longer research: the facts are gathered. Each checked table's page is transcribed into `datadictionary.yaml`, every foreign key as a note in its column's type (`bigint (foreign key to DiagnosisDim)`, about 195 of them), and design.md (Keys And Relationships In Cosmos) says how a page reads, including the ER diagram that gives the column a key lands on. SQL cannot supply relationships, so the pages were the only source, and they are in hand. The worst join faults are already caught another way: an undefined alias, and a fact table joined to nothing the pull makes (D118, D129).

What is left is a choice, not an unknown: whether to build a join check at all. If so:

1.  **Keys as data, not notes.** A structured entry beside each column (`references: PatientDim.DurableKey`), the table's own key, and whatever filter makes the other side one row (`PatientDim` with `IsCurrent = 1`, `DiagnosisTerminologyDim` by `Type`). Each entry keeps its grade: *seen* (read off a page), *counted* (a query below run and recorded) or *said* (the VM's AI or Epic convention alone, a question to check, never used by validation). The keys noted today are *seen*, bar the column each lands on where a page had no ER diagram (PatientDim, DiagnosisTerminologyDim); the history rules (`IsCurrent`, `Type`) and the `-1` sentinels are *said*.
2.  **What validation flags**, as an error or a warning: a join on columns that are not a declared relationship (deliberate non-key joins exist); a join to a table with several rows per key and no filter that makes it one; perhaps a large fact table read without its partition key. The table builder could then offer Cosmos joins from the keys (The App, Later).
3.  **Counts, only for what is *said***, on `COSMOS_SneakPeek` with a date window: whether this login can see declared keys at all (`HAS_PERMS_BY_NAME('dbo.PatientDim', 'OBJECT', 'VIEW DEFINITION')`); a parent key one row per key under its filter (`COUNT_BIG(*)` against `COUNT_BIG(DISTINCT DurableKey)` on `PatientDim WHERE IsCurrent = 1`, and without); orphans, sentinels aside (a `LEFT JOIN` from the child where the parent is null and the key is not `-1`); and a one-to-many dimension's rows per key (`DiagnosisTerminologyDim` grouped by `DiagnosisKey`). Record each with its date and database.