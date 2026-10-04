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
**🟧 Your response:** (in the chat, 2 Oct) This is fascinating. Is there a specific rule of chunking for each type? I mean, I'm not sure how to effectively chunk then.
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: a rule per purpose; the default is no splitting at all**

Each way of splitting a pull answers one question. Pick by the problem, not by habit; with none of these problems, don't split.

| Your problem | What to use | Why | Cost |
|---|---|---|---|
| **A table failing makes you re-pull the others** | One table per group (D190, now the default) | Each table is its own run; Retry failed pulls only the one that failed | Seconds per table |
| **The pull takes too long** | Chunks by **date window** (proposed, below) | Each pass scans only its own years, so the passes divide the time | One pass per window, all adding up to about one scan |
| **One pass runs out of tempdb, or its landing is too big** (an error naming `tempdb` or the log, 9002 or 1105 in tempdb) | Chunks by **patient** (`chunk:`), as few as fit, e.g. `chunk: 100000` | Each pass builds and lands a smaller temp | Every chunk scans the whole date window again: N chunks, about N times the time |
| **The project database fills** | Not chunks: D177, packaging each group and emptying it | The destination holds every row whatever the chunks | (D177) |
| **Separate files per value, or matched controls** (by sex, state) | Batching **by value** (`sex`, `state`, D58, D59) | One run and parquet per value; controls sampled per value | Every value scans the window again, as patient chunks do |

So, for each kind of pull:

- **A small cohort** (HaT, about 6,000 patients): nothing. One group per table, no chunks.
- **A large cohort, finishing in a night**: still nothing.
- **A large cohort that takes too long**: date windows, once built. Yearly windows over 2015 to 2026 is 12 runs, each about a twelfth of the time.
- **A huge cohort that fails for tempdb**: patient chunks, large, and accept the time. We have not yet seen a tempdb failure; Crohns' and UC's failures were the project database filling, which chunks do not fix.

**Chunks by date window, in short:** a splitter that cuts `min_date_key` to `max_date_key` into windows (a year, say), each a run of its own, filling the dates its tables filter on. It only helps a table filtered on its partition key, which is every fact table's `standard_where`; a table that isn't would scan everything in every window. The PK isn't split; each window joins the whole PK.

**For you to decide:** put **chunks by date window** on the roadmap after D177? It would be its own decision first: what the window is (yearly, or a number of months), and what Author shows.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## Make a 'utils' tab in scope.py

after Run. that way I can just open it easily.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: a Utils tab after Run, the utilities window's buttons inside the app**

**What the code does today.** The utilities are a window of their own: `python utils.py` opens a button per script in `utils/`, under **Client** (the parquet viewer, the transcription viewer) and **Manager** (clear_projects_db), each opening its own window (D124, D148). The app (`python scope.py`) has two tabs, Author and Run. D180 adds two of the utilities to Run as buttons (View dbo tables, and a multi-column view of a text tab), not yet built; a Parquets tab was left for later.

**Recommendation.** A third tab, **Utils**, after Run, with the same buttons as `utils.py`, under the same headings, read from the same folders, so a script put in `utils/client/` or `utils/manager/` shows in both. Each button opens its utility's window, as it does now. The tab is drawn with the window's own code, so the two can't drift. D180's two buttons stay where they are, since they open a utility on what Run has loaded. The Parquets tab stays for later.

**For you to decide:** nothing, unless you want the utilities to open inside the tab rather than in their own windows. That would be a larger change, one utility at a time.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

**Suggested order**

1.  **Chunks by date window**: on the roadmap or not; it decides how the ctrl\_ pull is split if it is large.
2.  **The Utils tab**: a yes is enough; it's small.

Moved out: **Make deliverables** is D189 and **one table per group** D190, both in the roadmap's order; the finding that patient chunks repeat the scan is in `design.md`; **Code Finder** is in the roadmap's future items. The profile screenshot was for your HaT repository, so it is gone from here and from `HaT Considerations.md`.

## Settled

Nothing waiting.