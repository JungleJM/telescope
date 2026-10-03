---
title: "Task list"
---

File for discussing ideas/improvements. Your notes, then Claude's reply in a box with a blue border, and yours in one with an orange border; type anywhere inside the orange box, between its `:::` lines (D174). New notes go above **Settled**; what is agreed moves there, then into the plan documents. Pasted images land in `images/` beside this file; `python3 scope.py images` deletes one once nothing mentions it (D132).

# Tasks

Nothing open here. What is left of the show-stopper tests (step 4 after each real pull, then the clean-up), the HaT PheWAS pull and the Infant_RSV re-pull are in the roadmap's **Next: On The VM, The Bundle `b822ab77…`**.

# Questions

## Batching seems to be doing cXof6 for each table instead of per table

![](images/paste-1.png)

I think the batching is doing all the c1of6, c2of6, etc. instead of going through each table. I'd rather go through each table because if there's a failure, it's much more likely that the system will have completed one if it goes by table.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: per table is already possible, through table groups; and the chunks themselves are the bigger cost**

**What the code does today.** A run's loop is chunk first, then every table (`session.py`, `_run_run`): refill the PK temp with chunk 1, build and land each table, then chunk 2. The **run** is what the manifest marks done or failed. A failed chunk fails the whole run, and Retry failed redoes the run from the start (its rows are cleared by `_batch` label first). So turning the loop around on its own would save nothing: a failure in Labs would still pull Encounters and Diagnoses again on the retry. To keep finished tables, each table has to be its own run.

**That already exists: table groups** (D134). Each group is its own run, with its own connections, and Retry failed pulls only the failed group. One table per group gives you exactly what you describe: every chunk of Encounters, then every chunk of Diagnoses, and so on, each finished table kept. The cost of a group is a new connection and a refill of the PK temp, which takes seconds. Tables that read each other must share a group, and validation already says so (`table_reads_another_group`). Once D177 is built, each group is also packaged and emptied as it finishes.

**The bigger finding, from your screenshot:** each chunk costs almost a whole table's query. Labs takes 22 to 23 minutes per chunk for about 2,000 rows; Diagnoses about 10 minutes; Encounters about 6. So Cosmos is scanning the table's date range on every pass, not seeking by patient. Six chunks cost about six times one pass: roughly 4.5 hours for this session, against about 45 minutes unchunked. That answers the roadmap's open question **What a batch costs**: fact tables are scanned per pass. HaT has about 6,000 patients, so it needs no chunks at all. Chunks are worth it only to keep a table within the Projects database's room, not for speed.

**Recommendation.**

1.  For HaT and its ctrl\_ pull: no `chunk` (or one at least as large as the PK). It's a blueprint edit, no code.
2.  For per-table safety: one table per group. You can do that by hand in Author today. If you want it as a default, a small addition: **Each table its own group**, a button in Table Groups that puts every ungrouped table in a group of its own name.
3.  Don't turn the loop around inside a run: without per-table completion in the manifest it saves nothing, and with it, it rebuilds what groups already do.
4.  The roadmap's "What a batch costs" becomes a finding in `design.md`: prefer few, large chunks.

**For you to decide:** whether to add **Each table its own group** (2), or just use groups by hand.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## Artifacts to "Make Deliverables"

![](images/paste-2.png)

We are no longer using 'artifacts' to make the artifacts. But if you look at "Artifacts" in design.md, there is a set of deliverables that the client will use - the two parquet folders, the contents.md, loading scripts, utils. I want this button to say "make deliverables" and when you click it it asks the folder you want it placed in. It will copy the deliverables of that project to that folder. \
\
The only other thing is 'load_parquets.r' is based on teh folder, and I want the person to choose what folder that they want to load. Specifically there will be a 'cosmos' a 'sneakpeek' and 'uploaded'. and there should be declarative var at the top of the script that says 'load\_' and each section, that is true or false. That way they can just turn on and off the ones they want. Default is Uploaded and Cosmos.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: Make deliverables copies the package to a folder you choose; the load scripts get switches**

**What the code does today.** Artifacts (the button, or `scope.py --artifacts <project>`) reads each finished table from Projects into parquet and writes the deliverable into the pull's own run folder: `cosmos_parquets\`, `sneakpeek_parquets\`, `uploads_parquets\`, `contents.md`, `load_parquets.R` and `.py`, `utils.py` with `utils\client\`, and `HOW_TO.md` (D72 to D75, D148). Execute already does this by itself after a clean pull (D141), then drops the pull's Projects tables (D165). So by hand the button only re-packages, and after the drop there is nothing in Projects to re-package from. The load scripts point at the run folder by its full path (`PARQUETS`), and load every `*_parquets` folder they find.

**Recommendation.**

- **Make deliverables** replaces the Artifacts button. It asks for a folder, then copies the loaded pull's deliverable into `<that folder>\<project>\`: the three parquet folders, `contents.md`, the load scripts, `utils.py` with `utils\client\`, and `HOW_TO.md`. Nothing from `pull_files\`, the manifest or the logs. It reads nothing from Projects. If `<project>\` already exists there, it stops and says so, rather than mixing two copies. It runs in the window with a line per folder copied, since parquets can be gigabytes.
- **The load scripts are written again in the copy**, with `PARQUETS` set to the copy. Otherwise a copied script would still load from `runs\`.
- **Re-packaging** from Projects stays as `scope.py --artifacts <project>`, for a rescue, with no button. Execute's own packaging is unchanged.
- **Switches at the top of `load_parquets.R`:**

``` r
load_cosmos    <- TRUE
load_sneakpeek <- FALSE
load_uploaded  <- TRUE
```

  each reading its folder (`cosmos_parquets`, `sneakpeek_parquets`, `uploads_parquets`). The same three in `load_parquets.py` (`LOAD_COSMOS = True`...), so the two scripts agree. A switch that is on, with its folder missing or empty, prints that it found nothing, rather than failing.

**For you to decide:**

1.  Does the deliverable include what made the pull: the blueprint copy (the template as sent)? I'd include it, since D148 says a deliverable carries what made it, and leave the manifest and logs out.
2.  Is `<project>\` inside the chosen folder right, or should the files go straight into the folder you pick?
3.  Should Make deliverables refuse a pull that has not packaged cleanly, or copy what there is with a warning? I'd copy with a warning naming what is missing, since `contents.md` already lists what was left out.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## 'LOINC by Keyword' utility

I'm looking for a name for a utility where I can search for a keyword or keyphrase, and a series of them, and it will return any LOINC, CPT, SNOMED, ICD-10 code with that name in it, in a parquet. I'd like it to be a SQL query-to-parquet that

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: a name, where the codes live, and your sentence finished**

Your note stops at "a SQL query-to-parquet that", so this is a first answer.

**Names:** **Code Finder** (plain, says what it does), **Codes by Keyword**, or **Term Search**. I'd pick **Code Finder**: it isn't tied to LOINC, since it will search four vocabularies.

**Where each vocabulary is, in the dictionary:**

- **LOINC**: `LabComponentDim` (`LoincCode`, `LoincName`, `Name`, `CommonName`, `BaseName`), as your tryptase query searched.
- **CPT**: `ProcedureDim` (`CptCode`, `Name`, `ShortName`; also `HcpcsCode`, `Code` with `CodeSet`).
- **ICD-10-CM and SNOMED**: `DiagnosisTerminologyDim` (`Value`, `DisplayString`, `NameAndCode`), told apart by `Type`; `DiagnosisDim.Name` for Epic's own diagnosis name.
- `TerminologyConceptDim` (`Name`, `StandardName`) as a further place to look.

**How it could work:** a small window in `utils\manager\`, like clear_projects_db. You type keywords, one per line; it runs one `LIKE '%keyword%'` query per vocabulary on Cosmos (dimension tables only, no patient data, so it is fast), and writes one parquet with a row per code found: vocabulary, code, name, the keyword that matched, and the table's key (`LabComponentKey`, `DiagnosisKey`...), which is what a pull's filter needs. It shows the count per vocabulary, and opens the parquet in the viewer. The keys could also go straight into a pull: the parquet as a supporting table, read with **In supporting table** (D119).

**For you to answer:** how did your sentence end? And do you want it to search names only, or codes too (so `D89.4` finds `D89.40` to `D89.49`)?
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

**Suggested order**

1.  **Batching**: decide before the ctrl\_ pull is written; without chunks, it takes about a sixth of the time.
2.  **Make deliverables**: three small choices, then a small build; it waits behind D177 in the roadmap's order unless you want it sooner.
3.  **Code Finder**: finish the note first; it's new, and nothing else waits on it.

## Settled

Nothing waiting.