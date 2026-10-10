# Status

What is built, and what is still to be seen working on the VM. Only the user can confirm a check there, so each waits here until they say it passed, newest software first. When a check passes, delete it; when a section is empty, delete it. What is still to be built is in `roadmap.md`.

------------------------------------------------------------------------

## Where Things Stand

| Part | State |
|------------------------------------|------------------------------------|
| YAML Manager: validation, dictionary, table binding, pre-YAML, split, manifest | Built and tested |
| The app: model, tkinter Author and Run (D92–D131), opened by `scope.py` (D112, D123) | Built and tested on the Mac: the model's tests, and the Author view's on a real Tk 8.6, withdrawn. In daily use on the Mac and the VM; the six remade pulls were authored in it |
| Bundle: build, verify, extract, `.local` preservation, carried transfer YAMLs (D79), the queue (D91, D122), YAMLs only (D122), `scope.py` and `utils.py` (D123, D124), not committed (D106); the dictionary at `reference/` (D111, D117) | Built and tested; used on the VM for every update. The morning bundle of 29 September 2026 carried the six remade pulls through the queue |
| What the bundle carries says nothing of how it got there (D200–D203): the transcription viewer held back, the prose scrubbed, the code names saying `version` (D202); the walkthrough `HowThisRepoWorks.md`, the template and the package list carried (D199) | Bundle 2, `11036600…`, installed on the VM over the earlier folder on 7 October 2026: `--tdd` passes, the windows' foot says `version`, and Author validates, splits and previews there. Still saying it: Author's Mac-only bundle-building code (task list, option 3) |
| Pullmanager: manifest, naming, rendering, dry run | Built and tested; used on the VM |
| Pullmanager: connections, session execution, uploads, transfer (D50–D62) | **Proven live** on many pulls since the first IBD Ancestry run (25 September 2026): three at once (26 September), and on 29 September Infant_RSV to the end (exit code 0, PK 186,963 rows) and Crohns_PatientsFromUpload (a 1,284,756-row parquet upload through both legs, 13 chunks) |
| `--execute <project>`, the lock, the log, the session readout (D66–D70) | Used on the VM for every run since 25 September 2026, several pulls side by side, each in its own console |
| Launcher, now the app's Run half; its three dropdowns (D126, D140) | Opened on the VM; Validate, Export split, Preview and Execute used there, and the two dropdowns of D126. The third, and the pulls' finished and stopped words, not yet |
| On the VM in the bundle `e126f85b…` (29 September 2026), not yet checked there: progress lines and each table's rows (D136, D137); how Execute ended (D140); Artifacts after a clean pull (D141); the run folder (D142); QuickEdit off (D143, tested against a fake console only); the Pull Manifest tab (D144); the credit line (D145); the parquet viewer's buttons (D146); extraction refusing while a pull executes, and the bundle in every window (D147); the deliverable pull folder (D148); the backup (D149); the dictionary line out (D150) | Built and tested on the Mac. The bundle's `--tdd` failed two window tests on Windows only, fixed in the tests in `0f81dc63…`, which is extracted and passes `--tdd` |
| Errors hidden in a batch (D151), the run scan (D152), manifest saves that wait and readers that let them (D153, D154), the dictionary audit and `dictionary-fix` (D155), the column check (D156), rows per join key (D157), re-pulling chosen sessions (D158), unquoted CSV headers (D159) | On the VM since the bundle `048c3f29…` (30 September 2026). The scan and the audit have run there; the show-stopper tests are under way (below): manifest saves past a held manifest proven (D175) |
| One blueprint per project (D162), Projects tables named per pull (D163), a database per pull (D164, D170, D171), drops after packaging (D165), Run and the title (D166), parquets from Status (D167), the Filters layout (D168), messages at their fields (D169) | Built and tested on the Mac; on the VM since `c98c60b3…` (1 October 2026). Seen working there: blueprints in `YAMLs\temp`, the table prefix in the manifest and SQL, the database choice, the title, the dropdowns and bold line, the step in flight, the Filters layout, messages at their fields. The bundles since: `5754255e…` (the six databases, listed in the code only), then `b822ab77…` (2 October 2026) |
| Table groups (D134, D138), recipe sets (D135), the table-order check, the multiplied-read warning (D139) | Built and tested on the Mac; on the VM in the bundle `e126f85b…`. Used on the VM by UC_VisitsMedsDiagnoses (three groups); what a group costs, a PK refill per run, is measured (design.md, Batching) |
| The refresh margin (D183), the Projects connection per table group and the retry on an expired sign-in (D176, D188), the join check (D178), recipes shipped for a while (D187), DepartmentDim in the dictionary | Built and tested on the Mac (2 October 2026), first in the bundle `b822ab77…`; on the VM since bundle 2 (`11036600…`, 7 October 2026), not yet seen working there |
| The upload-not-read warning (D195) and the chunk note (D196), duplicate and blank columns refused, the Diagnoses recipe set (D184), Row key and deduplication pickers (D186), the Utils tab (D194), clear_projects_db over every database (D185), Run's View dbo tables and multi-column view (D180), one table per group (D190) | Built and tested on the Mac (5 October 2026), first in the bundle `9e742fd1…`; on the VM since bundle 2 (`11036600…`, 7 October 2026), not yet seen working there |
| `utils/clear_projects_db.py`: a project database's tables and space, and dropping them (D133) | Built and tested against a fake database. A scratch version of it, run on the VM (29 September 2026), dropped all 71 tables and freed the data file; the utility itself is on the VM in the bundle `e126f85b…` |
| Artifacts: parquets, `contents.md`, load scripts, the stock list (D124), progress and per-table failures (D72–D75, D88, D89); the PK parquet at the PK phase (D87) | Built and tested against a fake Projects connection; the Python load script runs and the R one runs under R `arrow` 25. Run on the VM for Celiac; the PK parquet seen there (Infant_RSV); `stock.yaml` not yet |

------------------------------------------------------------------------

## To Check On The VM

### Project DB: Auto, Or Chosen With Its Free Space (D218), In The Next Bundle

Built 9 October.

1.  **Author's Project DB** opens ticked **Auto** for a new draft. Unticked, its dropdown lists each Projects database with its free space within a few seconds. Choosing one marks the draft unsaved, and the exported blueprint carries it.
2.  **A pull with `project_db: auto`**: its first Execute says "Choosing a Projects database" and takes the roomiest no other unfinished pull uses, as before.
3.  **A pull naming a database**: its first Execute says "The blueprint names …", measures only that one, and uses it even where another unfinished pull is. Named and too full (6 GB or less), it stops with nothing built, naming it.

### Infant RSV: Bundle `ae095e1f…` And The RSV Bundle `8bef2f7c` (9 October 2026)

Built 8 and 9 October (D209 to D214).

1.  **One of the two Infant_RSV pulls** (D213, D214): Redo everything (`Infant_RSV_blueprint.yaml`) or the follow-up (`Infant_RSV_Followup_blueprint.yaml`, after `python rsv keys`). No table lands empty; ED labs come with names, ED medications with theirs, and admissions with their departments' specialties.
2.  **The RSV bundle** (D209): `rsv_bundle.py` checks every file and installs `rsv`, keeping an edited `settings.yaml`.
3.  **`rsv build`, `report`, `compare` and `admission`** (D209 to D212) on the new pull's parquets: the build page lists the venous pH's real lab code once lab names arrive, and VBG is filled in once ED labs exist.
4.  **The transcription viewer**, copied into `utils/` by hand, opens at three columns and 16 point, and goes no smaller (D211).

### The Software Of 7 October 2026

Built from the fixes agreed 7 October (D198, D204 to D208, D177), for GI_Conditions' restart.

1.  **Status colours** (D198, D204): a table's row turns green once its run is done; a packaged table is purple; double-clicking an unpackaged table says why it opened Pull Manifest.
2.  **Scope, blueprints, no Exports tab** (D205): the window's title begins `Scope`; Author has no Exports tab; Run's field is **Blueprint** and Author's button **Open in Run**; a blueprint saved by Author starts with `blueprint:`.
3.  **Tables leave Projects as soon as nothing reads them** (D177, D206): each table group is packaged and emptied as it finishes; each PK once its session's runs (and any control sampled against it) are settled. clear_projects_db shows the database's room coming back during the pull.
4.  **A failed landing gives back its room** (D207), and **a run with no tables finishes at once** (D208).

### The Software Of `9e742fd1…`

Built 5 October 2026; on the VM since bundle 2 (`11036600…`, 7 October 2026), which changed only its wording and code names (D201, D202). The files that run, land and package pulls are unchanged from `b822ab77…` (session, executor, uploads, artifacts, manifest, the command line); what changed is Author, validation and the split (makeYaml), Run's window, the Utils tab and two utilities. To see:

1.  **Validate** on the old UC intake (in `YAMLs\temp\replaced\`, or any template with an upload nothing reads) warns `upload_not_read` (D195); a chunked one prints a `NOTE [chunk_passes]` line with its passes (D196).
2.  **Author**: a built or recipe PK has **Row key**, a dropdown of its columns, several allowed; each fact table has **Deduplicate by** and **Keep** (D186). Tick and untick, then Save and look at the YAML.
3.  **The Utils tab** after Run, with the utilities window's buttons (D194).
4.  **Run**: **View dbo tables** opens clear_projects_db on the loaded pull's database (D180).
5.  **clear_projects_db** (from Utils or View dbo tables): every listed database with its GB free, or why it could not be opened; opening one shows its tables grouped by pull; selecting a database shows its files; Drop selected on a pull's row drops only its tables, and on a database's row asks for its name (D185). Try it on a database with nothing you need.
6.  **One table per group** (D190): a new pull's Export split and Preview SQL show one run per ungrouped table (`<session>__<table>__b1of1`). A pull already split keeps its runs. The old `Infant_RSV_transfer.yaml` beside `scope.py`, if still there, is now refused (its tables read each other in no group); the working blueprint groups them and passes.
7.  **The Diagnoses recipe set** (D184): Add a fact table offers it; HospitalizationsWithinICDCode replaces OtherHospitalizations in the recipe list. The UC and Crohns intakes keep their table `OtherHospitalizations`.

------------------------------------------------------------------------

### Pulls And Questions Of 4 October 2026

1.  **Whether time grows with columns.** The HaT control pull, pulled again with Encounters at 12 of 45 columns and Diagnoses at 7 of 30 (`ctrl_PheWAS_barebones_intake.yaml` in pheauxWAS, `chunk: 100000`, so 3 passes), answers it: compare each table's time a pass with the full-column pull's (design.md, What a batch costs). Labs kept all 40 columns, so it is the control. Only if that is unclear, the SSMS version: in one SSMS window on `COSMOS`, with `#pk` made as before: `SELECT ef.PatientDurableKey, ef.EncounterKey, ef.DateKey INTO #few` and then `SELECT ef.* INTO #all`, each `FROM EncounterFact AS ef INNER JOIN #pk AS pk ON pk.PatientDurableKey = ef.PatientDurableKey WHERE ef._IsDeleted = 0 AND ef.DateKey BETWEEN 20250101 AND 20251231`, timed as before. `#all` several times `#few` means trimming columns saves time, and the table builder should say so.
2.  **UC_VisitsMedsDiagnoses again**, from `yamls_to_transfer.py` (`76c813d5…`, replacing `1c208cd6…`): its blueprint has the `IBD_Meds` filter (D192), the five tables already saved turned off (OtherDiagnoses, IndexDiagnosis, OtherHospitalizations, EDVisitHistory, HospitalAdmissions) and 1,000,000-patient chunks, two passes, since every chunk reads each table's whole window again (design.md, Batching). `1c208cd6…`'s Preview on the VM gave 0 errors; if UC is not yet executing, extract `76c813d5…` and Export split again; if it is, let it run. Before Execute: Back up all; copy `cosmos_parquets\`, `sneakpeek_parquets\` and `uploads_parquets\` out of the run folder (packaging replaces them); drop the remaining `ucvis_` tables in PROJECTD52219B (the 9 GB is the unfiltered MedAdminHistory). While it runs: when the SneakPeek Meds finishes, its MedAdminHistory_sp rows over the old 564,163 times about 150 GB is roughly what Cosmos's MedAdminHistory will need; past about 12 GB, Stop. After: copy the saved Visits and Diagnoses parquets back over the empty ones.
3.  **Crohns_VisitsMedsDiagnoses and Crohns_DxHxSxRx**: their intakes carry the filter (D192); their blueprints wait for each pull's state on the VM (its manifest or Status, and a listing of its parquet folders and `saved_parquets\`), to know which tables to turn off and whether their Meds were pulled unfiltered. Crohns_DxHxSxRx's Meds group (item 11 below) was unfiltered.
4.  **UC's MedOrderHistory and MedDispenseHistory** (Cosmos) had 0 rows in the failed Meds run of 2 October, their `_sp` copies 310,672 and 181,478: turned off for that retry, or nothing returned? The re-pull answers it.

------------------------------------------------------------------------

### The Software Of `b822ab77…`

Built 2 October 2026: the software of D151 to D171, the window tests' fix, recipes (D187), the refresh margin (D183), the Projects connection per group and the sign-in retry (D176, D188), the join check (D178). On the VM since bundle 2 (7 October 2026). HaT PheWAS, its blueprint, ran on 2 October 2026 (5,969 patients; its timings are in design.md, What a batch costs).

1.  **HaT PheWAS's checks and cull**, in `HaT Considerations.md` at the repository root.
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
13. **Small checks for later**: double-clicking a packaged table in Status opens the parquet viewer on Windows (D167). Every double-click so far opened Pull Manifest (7 October 2026), which is what an unpackaged table does; D204 will show which tables are packaged, so try one of those.

------------------------------------------------------------------------

### The Remade Pulls

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
10. **The credit line** (D145) at the foot of each window, the viewer in a pull's folder included, with `· version` (D202; `· bundle` before) and the same 8 characters `bundle.py` showed as its `content_id` (D147); `python scope.py --version` says it too.
11. **Extraction refuses while a pull executes** (D147): with a pull running, `python bundle.py` names it and extracts nothing. Once nothing runs, it says it removed the previous `pullmanager_runtime`.
12. **The backup** (D149): in Run, Browse a backup folder on the other drive; Artifacts on a packaged pull says `backed up to <folder>\<project>`, and the old parquets are there. With the drive unplugged, it goes to `runs\backup` with a warning at the end. Back up all backs up each pull, skipping one executing.
13. **A pull's folder as a deliverable** (D148): after Artifacts, `utils.py` in it opens a window with the parquet viewer only; the viewer opens on that pull. Your own `python utils.py` shows Client and Manager.
14. **The dictionary line is gone from Run** (D150), and Validate still names `reference\datadictionary.yaml` in the extracted folder.

#### Still Unchecked From The First Live Runs

Most of the first-live-run list was settled by use: Execute's own console and the Pull Log tab, pulls side by side, the PK parquet at the PK phase (D87), Artifacts on Celiac, uploads over 1,000 rows and an uploaded PK (Crohns_PatientsFromUpload), and `OPENQUERY` reaching the captured `@@SERVERNAME` (every upload's second leg). What no run has shown yet, cheap to read off any finished pull:

- **The numbered prefix** (D50): every pull uses the default `tesrun`, so pulls side by side must have taken `tesrun2` and on. A manifest's `session.runtime.temp_prefix` and its warning would confirm it.
- **What D50 and D51 rely on**: `SELECT OBJECT_ID('tempdb..##<a temp that exists>')` returns a number from our login, and `manifest.cosmos_refresh` is filled in. Note `create_date` either side of the next refresh to confirm a refresh changes it.
- **An upload's types**: `upload_<dest>` in the project database has the file's types, and the Cosmos temp the same (`BIGINT`, not `NVARCHAR`).
- **Blocking during a transfer**: whether other work on the Projects database waits on a large one. Pullmanager commits after every cohort (D55), but the driver runs with autocommit off, so one cohort's `OPENQUERY` into staging sits inside an open transaction until that cohort commits. If it blocks others, open the Projects connection with autocommit on.
- **A batch with no values** (`state`, D82): no current pull uses one. When one does, each run shows `values_found` and `v3of51 (LA)` as it goes, and the destination's PK counted by the column matches the whole PK.
