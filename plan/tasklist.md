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
**🟧 Your response: shit - I guess we do need to have a per-table group, then. In that case the table groups are just usable as a shorthand for the user to add a bunch of groups, which is OK.**

**\
bigger question is, how many chunks should I be doing? I mean, is there any value to chunks at all? if I split into two chunks am I always taking twice the length? Is there any way I can have it actually split up the time instead?**
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: every table its own group; chunks by patient multiply the time, chunks by date would divide it**

**Per-table groups, as I read your answer:** a table in no group runs as a group of its own, one run per table, instead of all ungrouped tables together in one run. A group you name keeps its tables together, as a shorthand for tables that must share a run. Today the tables in no group run together, as one group named `run` (D134); this changes that default. Consequences:

- Retry failed pulls only the table that failed. Once D177 is built, each table is packaged and emptied as it finishes.
- Two ungrouped tables where one reads the other are now in different groups, so validation refuses them (`table_reads_another_group`), and the fix is a named group for both. Your HaT tables read only the PK, so nothing to change there.
- A new connection and a PK refill per table: seconds each.

**How many chunks, and are they worth anything?** With chunks by patient, as today, two chunks take about twice as long, for the scan-heavy tables. Cosmos's fact tables are partitioned on their date key (`StartDateKey`, `DateKey`, `PrioritizedDateKey`...), and the HaT times show each query scanning the whole date window whatever the patients: Labs takes 23 minutes for 2,000 patients' rows, as it would for all 6,000. A chunk by patient narrows only the join, not the scan. So:

- **Time:** chunks by patient never save time; N chunks cost about N scans.
- **What they are for:** keeping each query's temp and each landing small, when a pull is so large that one pass would fill tempdb or the Projects log, or run for many hours in one transaction. Only for pulls of hundreds of thousands of patients; HaT needs none.
- **How many:** as few as fit. Start with none. Add chunks only after a pull fails for space, and then large ones (`chunk: 100000` or more).

**Splitting the time instead: chunks by date window.** Each pass reads only its own years of the partition key, so Cosmos skips the other partitions, and N windows cost about one whole scan, divided. For example, `min_date_key` to `max_date_key` cut into yearly windows, each run filling `{{min_date_key}}` and `{{max_date_key}}` with its own year. It suits tables filtered on their partition key, which is every fact table's `standard_where`. It doesn't change the PK; each window is a run of its own, so a failed year retries alone. Rows per window vary by year.

**Recommendation:** decide the per-table default now (it's small). Record "chunks by patient never save time" as a finding, which closes the roadmap's **What a batch costs**. Put **chunks by date window** on the roadmap after D177, as its own decision, since it touches the split and the renderer.

**For you to decide:** is my reading of per-table groups right? And do you want chunks by date window on the roadmap?
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

bigg

## Artifacts to "Make Deliverables"

![](images/paste-2.png)

We are no longer using 'artifacts' to make the artifacts. But if you look at "Artifacts" in design.md, there is a set of deliverables that the client will use - the two parquet folders, the contents.md, loading scripts, utils. I want this button to say "make deliverables" and when you click it it asks the folder you want it placed in. It will copy the deliverables of that project to that folder.\
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

1.  **add the blueprint.**

2.  **Actually do /data inside the folder.**

3.  yes it should refuse,l with a question - something like a popup saying "warning: incomplete pull, See 'status' (or whatever) for explanation/what's missing. Still make deliverable of what's been pulled?" with a Continue and Cancel.
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: the layout, as I read "/data inside the folder"**

The folder you choose holds the deliverable directly, with the data under `data\`:

``` text
<chosen folder>\
  data\
    cosmos\        (from cosmos_parquets)
    sneakpeek\     (from sneakpeek_parquets)
    uploaded\      (from uploads_parquets)
  load_parquets.R, load_parquets.py   PARQUETS is data\, load_cosmos / load_sneakpeek / load_uploaded
  contents.md, HOW_TO.md
  <project>_blueprint.yaml
  utils.py, utils\client\
```

- The folder names under `data\` are yours: `cosmos`, `sneakpeek`, `uploaded`, matching the switches. The run folder keeps its own names.
- The load scripts find `data\` beside themselves, so the folder can be moved or zipped without editing them. Python reads its own location; R takes the folder from where `source()` was run, else the path written in at copy time, and says which it used.
- A chosen folder that already has a `data\` stops it, naming what is there.
- An incomplete pull asks: *Warning: incomplete pull. See Status for what is missing. Still make deliverables of what has been pulled?*, with **Continue** and **Cancel**. Continue copies what is packaged, and `contents.md` lists what was left out, as it does now.

**For you to decide:** is that the layout you meant? If so, it's ready to write as a decision and put in the roadmap's order.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## 7-10 for that prior query

In case we still need that

![](images/paste-5.png)

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: kept in HaT Considerations, with two things they show**

Recorded under **Profile Results** in `HaT Considerations.md`, with the screenshot kept. Two things worth your attention:

- **Query 7** shows only its last row: 2017, 12 patients. So D89.44 rows go back before 2018, and the pull starts at 2015-01-01. A screenshot of its earlier rows would say how many patients have a first D89.44 before 2015 and so get a later index.
- **Query 11**: 5,974 patients, 3,696 of them on 2+ dates. Of the 2,278 seen once, 586 fall in the window's last 6 months, too recent for a second code.

Nothing for you to decide here; this section can go once you've read it.
:::

**Suggested order**

1.  **Batching**: confirm per-table groups (small, and it changes how every pull runs), and whether chunks by date window go on the roadmap; decide before the ctrl\_ pull is written.
2.  **Make deliverables**: confirm the `data\` layout, then it's a decision and goes in the roadmap's order.
3.  **The profile screenshot**: read and remove.

Moved out: **Code Finder** is in the roadmap under **Code Finder (Future)**, with `temp-tasklist.md` as its full description.

## Settled

Nothing waiting.