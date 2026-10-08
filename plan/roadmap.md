# Roadmap

What is still to be built or decided: the future, and nothing else. What is built and still to be seen working on the VM is in `status.md`; why things are as they are, in `decisions.md`; what exists, in `design.md`.

When an item is agreed, put it under Next, in order. When it is built, delete it here: describe it in `design.md`, and if it has to be seen working on the VM, add that check to `status.md`.

------------------------------------------------------------------------

## Known Bugs

- **Execute sometimes ends mid-pull with exit code 1** (the IBD template, September 2026: `CrohnsPatients`, its first Cosmos session, during `upload_cohorts`, after the SneakPeek sessions finished). No summary, and the step left `running`. Every step catches Python errors, so it was either killed (Stop, or anything else on the VM: Windows gives 1) or an error outside the steps, whose traceback reached only the closing window. The log now keeps the traceback, or where a native crash happened; the next occurrence says which. The next Execute resumes it.

------------------------------------------------------------------------

## Next

Nothing agreed and unbuilt (8 October 2026). Waiting to be ordered:

- **Make deliverables** (D189); **Specify Project DB** (D179); **Counts from `profile:`** (D181), on D186's picker: Count for the PK, then the after-PK profile and the report, then fact tables in Count.
- **A single table can outgrow the database on its own** (UC's unfiltered MedAdminHistory, about 150 GB), which packaging by group does not bound; packaging by chunk would (Open Problems, Estimate Size And Packaging By Chunk).

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

### Code Finder (Future)

Low priority (the user, 2 October 2026). A utility that searches Cosmos's code tables for keywords and keeps a growing library of codes, one parquet per vocabulary (`ICD-10.parquet`, `SNOMED.parquet`, `CPT.parquet`, `LOINC.parquet`), each row with every column of its source table and the categories it is filed under. It grew from a query run in SSMS for HaT: `SELECT lcd.LabComponentKey, lcd.Name, lcd.CommonName, lcd.BaseName, lcd.LoincCode, lcd.LoincName, lcd.Type, lcd.Subtype, lcd.DefaultUnit FROM LabComponentDim AS lcd WHERE lcd.Name LIKE '%tryptase%' OR lcd.CommonName LIKE '%tryptase%' OR lcd.BaseName LIKE '%tryptase%' OR lcd.LoincName LIKE '%tryptase%'`, which found 8 components (2287, 8166, 16740, 51583, 59082, 75051, 86072, 91367; one of them `INACTIVE Tryptase`), the list HaT PheWAS's Labs table reads. A find keeps every column of its source table, as that query's result did.

- **The search.** A dropdown of the four vocabularies and a comma-separated list of keywords (`tryptase, potassium, magnesium`), each searched alone. Where each vocabulary is in the dictionary: LOINC in `LabComponentDim` (`LoincCode`, `LoincName`, `Name`, `CommonName`, `BaseName`, as the tryptase query searched); CPT in `ProcedureDim` (`CptCode`, `Name`, `ShortName`; `HcpcsCode`, `Code` with `CodeSet`); ICD-10-CM and SNOMED in `DiagnosisTerminologyDim` (`Value`, `DisplayString`, `NameAndCode`, told apart by `Type`), with `DiagnosisDim.Name` and `TerminologyConceptDim` beside them. Dimension tables only, so fast. Whether CPT and SNOMED search as cleanly as LOINC needs looking at on the VM.
- **Filing.** It asks for a category to file the finds under (`HaT`); a sidebar of the user's categories filters the parquet viewer below (none ticked shows all). A selected row can be marked incorrect, or moved to another category, which also undoes a mark.
- **Later, in a pull.** A supporting table drawn from the library by vocabulary, keywords and categories (`GI, autoimmune`), copied into a parquet of its own at run time and read with In supporting table (D119). Validate warns of a keyword or category not in the library, with a shortcut to the utility; one left unsearched is searched before the pull starts, its finds added to the pull's parquet and to the library as `uncategorized`.

A code is matched as written, and `%` widens it (`D89.4%`), D197.

### Estimate Size And Packaging By Chunk (Future)

The user's next step after the fixes (4 October 2026). Today chunk sizes are guessed. The user wants them chosen: a pull whose rows fit the project database's 20 GB in one pass is not chunked (6,000 patients in 1,000-patient chunks is six passes for nothing, since each chunk reads the fact tables' whole window again), and one that does not fit gets the fewest chunks that do (above about 10 GB, say). An equation, not an AI: the lowest mix of passes and room. Size = rows × bytes per row (design.md, What a pull will hold), in three stages, each better than the last:

1.  **At Validate**: bytes per row from the dictionary's types (text at its declared width, or a measured average: `AVG(DATALENGTH(col))` beside the widest value Execute already measures, D34), times rows per patient from earlier pulls (D157's median and 90th percentile), times the PK. A table never pulled shows its bytes per row only; a new filter makes an old figure an overestimate, the safe direction. Counting rows beforehand is no cheaper than pulling them (a count is a pass), so rows come from what pulls already learn.
2.  **After the SneakPeek sessions**, before Cosmos: SneakPeek's rows per patient (D193: over the PK's patients SneakPeek holds, a new count for an uploaded PK, which is how most pulls will be made), times the Cosmos PK, with a margin of about 25% for the extreme patients a 1% sample misses. On HaT PheWAS it came within 20% of every table, at about a two-hundredth of the time.
3.  **After the first chunk**: its rows and bytes times the number of chunks. Past the room left in the project database, stop loudly with the numbers; UC would have stopped after chunk 1 of 34 instead of failing at chunk 3 after 38 minutes. Chunks 1 and 2's times, times the chunks left, give the time to finish.

With it:

- **Packaging by chunk** (D177 made finer): each chunk written to parquet as it lands, its rows checked against what the run recorded, its table emptied; the parquets stacked, which is exact since each holds other patients (pyarrow streams them into one, or a folder that arrow reads as one table). The database then needs room for one chunk of one table, not the whole pull; a single table can outgrow it otherwise (UC's unfiltered MedAdminHistory, about 150 GB).
- **A chunk size per group**, the fewest chunks that fit, since chunks cost a pass each; a table where chunking gains nothing is not chunked.
- **Dynamic ordering** (the user, 4 October 2026): the PK, then the tables others read, then the small tables, then the large ones, chunked by patient and packaged chunk by chunk. What is core is landed first and the small tables are out of the way; only the large tables pay for repeated passes, which date windows cannot avoid (D191).

Open: whether packaging by chunk replaces D177's by group; a table another in its group reads is emptied only once every table of that chunk has landed; the run scan must expect emptied tables.

### Choosing A Study's Columns (Future)

Every table takes every column of its source today, and columns are removed one at a time in the table builder. Since Cosmos reads by column (design.md, Batching), columns cost time as well as room; the HaT control pull was trimmed by hand for this (12 of 45 encounter columns, 7 of 30 diagnosis columns). The user wants the choice study-specific (5 October 2026), so not a "core" set marked in the dictionary, which would have left out the department and site columns that study needed. How is not decided; the user will come back to it. The candidates: **Keep only these columns** on a table, a list pasted or loaded from the analysis's own code (as `build_group_parquet.py`'s `ENCOUNTER_COLS`), every other column removed but its keys, Validate warning of a listed column not in the table and of a dedup key removed; and later, if studies repeat, **saved column sets** reused by name, as recipes are. The size estimate (above) would show what trimming saves.

### A Test Server

The user plans SQL Servers on their homelab (Bluefin) holding fake data, so a pull can be run end to end from the Mac. Choosing it would sit beside Cosmos in the app. The server names are already settings (`PULLMANAGER_COSMOS_SERVER`, `PULLMANAGER_PROJECTS_SERVER`), and the ODBC driver and `pyodbc` (5.3.0 on the VM's list) would be needed on the Mac. Not until the servers exist.

### Smaller Open Items

- **Recipes ship for a while** (D187). When the user says to stop: remove the `recipes` entry from `COMPANION_FILES` in `bundle_pullmanager.py`, put back the bundle tests' expectation that no recipes file travels, and the next extraction removes the VM's copy (an edited one kept as `.local`).
- **The builder leaves new columns nullable** (D172), so a table's nullability in Cosmos stops becoming a row filter (`IS NOT NULL`, which turns a LEFT JOIN into an INNER one), and the audit's 327 `false` findings can then be written into the dictionary as facts. Agreed, not yet built: `_column_from` in the model.
- **Name the column when Cosmos cannot convert.** Error 8114 (and 245, 8115) names no column. When a cohort fails with one, run `sys.dm_exec_describe_first_result_set` on its `SELECT` (it reads no data) and add each column whose source type differs from its declared one to the failure. Agreed in principle, not yet built.
- **Generated-table dependencies.** Cohorts reference other generated temps by handwritten name (`{{prefix}}_Patients`). It should be structural, so the renderer owns temp names. Under multipliers a fact table read this way names the base table, not its level's copy (`UCOrders`), and fails at Execute; validation warns meanwhile (D139).
- **An uploaded PK is sent to Cosmos whole** in the upload phase of every session, even when every run is batched and refills it from the copy (D61 left it so). To address later: whether a batched uploaded PK needs to go up at all, and once per session.
- **Matching controls.** A control is sampled at `row_mult` times its case per batch (D59), so it is matched on the batching columns only. Deeper matching (age, and so on) is to address later, as is a control with several case levels.
- **`split_after_build` on an uploaded PK** is refused (D54). It could be supported by splitting the rows as the copy lands, if a list ever needs it.
- **One PK per multiplier group.** Two `type: PK` cohorts in one group are an error (`multiple_pk_cohorts`), so each session has exactly one PK.
- **Multi-step PK.** A PK built from a prior PK (a patient list, then diagnosis events for those patients: `PKTable` built by joining `PKTable2`). The `pk` phase is one YAML; ordered PK cohorts inside it need a representation.
- **Space in the project database.** Pullmanager does not check the data file's and log's room before a pull, so a full database is found only when a landing fails (D133). A clean pull drops its tables once packaged (D165), and clear_projects_db shows each database's GB free (D185), but neither runs before `setup`. It could say there how full each file is (the size estimate, above, would compare it with what the pull needs).

### A Join Check From The Dictionary's Keys

Held off (30 September 2026): the user chose to wait on it.

No longer research: the facts are gathered. Each checked table's page is transcribed into `datadictionary.yaml`, every foreign key as a note in its column's type (`bigint (foreign key to DiagnosisDim)`, about 195 of them), and design.md (Keys And Relationships In Cosmos) says how a page reads, including the ER diagram that gives the column a key lands on. SQL cannot supply relationships, so the pages were the only source, and they are in hand. The worst join faults are already caught another way: an undefined alias, and a fact table joined to nothing the pull makes (D118, D129).

What is left is a choice, not an unknown: whether to build a join check at all. If so:

1.  **Keys as data, not notes.** A structured entry beside each column (`references: PatientDim.DurableKey`), the table's own key, and whatever filter makes the other side one row (`PatientDim` with `IsCurrent = 1`, `DiagnosisTerminologyDim` by `Type`). Each entry keeps its grade: *seen* (read off a page), *counted* (a query below run and recorded) or *said* (the VM's AI or Epic convention alone, a question to check, never used by validation). The keys noted today are *seen*, bar the column each lands on where a page had no ER diagram (PatientDim, DiagnosisTerminologyDim); the history rules (`IsCurrent`, `Type`) and the `-1` sentinels are *said*.
2.  **What validation flags**, as an error or a warning: a join on columns that are not a declared relationship (deliberate non-key joins exist); a join to a table with several rows per key and no filter that makes it one; perhaps a large fact table read without its partition key. The table builder could then offer Cosmos joins from the keys (The App, Later).
3.  **Counts, only for what is *said***, on `COSMOS_SneakPeek` with a date window: whether this login can see declared keys at all (`HAS_PERMS_BY_NAME('dbo.PatientDim', 'OBJECT', 'VIEW DEFINITION')`); a parent key one row per key under its filter (`COUNT_BIG(*)` against `COUNT_BIG(DISTINCT DurableKey)` on `PatientDim WHERE IsCurrent = 1`, and without); orphans, sentinels aside (a `LEFT JOIN` from the child where the parent is null and the key is not `-1`); and a one-to-many dimension's rows per key (`DiagnosisTerminologyDim` grouped by `DiagnosisKey`). Record each with its date and database.
