---
title: "Task list"
---

File for discussing ideas/improvements. Your notes, then Claude's reply in a box with a blue border, and yours in one with an orange border; type anywhere inside the orange box, between its `:::` lines (D174). New notes go above **Settled**; what is agreed moves there, then into the plan documents. Pasted images land in `images/` beside this file; `python3 scope.py images` deletes one once nothing mentions it (D132).

# Tasks

Nothing open here. What is left to see on the VM (the show-stopper tests' step 4 and clean-up, HaT PheWAS's checks, the Infant_RSV and UC re-pulls, the columns check) is in the roadmap's **Next: On The VM** sections.

# Explorations

## Returning to SP first

\
To that end, funny enough the system isn't actually running all the sneakpeek runs first anymore. I think it's because the logic thinks i'm batching by table so do 'patients_sp' then 'patients' then 'diagnosis_sp' then diagnosis'... But no that defeats the purpose. For any future run it should always do all the SPs first for the purpose of testing then go forth and do the regulars.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: agreed, and the code already does it; I need to see where it didn't**

**What the code does today.**

- **The split puts every SneakPeek session first** (D65): makeYaml sorts the sessions SneakPeek first, then cases before controls.
- **Execute runs the sessions in the manifest's order.**
- **A session runs all its tables before the next starts.** Batches and table groups (D190) are inside a session, never across two.

So the order is `ctrl_Patients_sp` (PK, Encounters, Diagnoses, Labs), then `ctrl_Patients` (the same). Patients_sp, Patients, Diagnoses_sp, Diagnoses never happens.

**Your two runs agree.**

- **The control pull's Status above:** `ctrl_Patients_sp` is done (its run took 12m 58s) before `ctrl_Patients` began.
- **HaT PheWAS (2 October):** SneakPeek's whole run finished at 18:28, and the Cosmos PK started at 18:28.

**Where it might have looked otherwise:**

- **A manifest split before D65** keeps its old order, Cosmos first. D65 applies from the next split.
- **Retry failed** pulls a failed Cosmos session without re-running a SneakPeek one that is done. That is correct, but it looks like Cosmos first.
- **The Pull Log of a session on its own**, from Re-pull sessions (D158).

**For you to decide:** nothing to change unless one of those doesn't explain it. Which pull, and where did you see the order (Pull Log, Status, the console)? A screenshot of the lines would settle it.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## Does the PheWAS need every encounter and every diagnosis?

(in the chat, 5 Oct, with the barebones control pull running) For the sake of argument: does the PheWAS really need *every* encounter or *every* diagnosis? What about collecting only encounters within x years of the index date they're tied to, and only the first instance of each diagnosis (deduplication)?

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: no to first-instance diagnoses; yes to windows, and to encounters as a few summary rows. The biggest saving is pulling diagnoses for the matched controls only**

**What the analysis reads** (pheauxWAS design.md and decisions):

- **Diagnoses.** A patient counts as a phecode case only if the code is on **2 or more distinct dates**, inside the window. The primary window is the 3 years before index; the sensitivity window is everything after (D12). Ruled-out diagnoses are dropped in Python before dates are counted (D17).
- **Encounters.** Encounters only feed patient-level numbers. Those are the first and last completed encounter (`YearsBeforeIndex`, `YearsAfterIndex`), and clinic-visit days, ED visits and admissions within 365 days of index. No analysis reads an encounter row itself.
- **Every control's index is known at pull time.** It's the sampled clinic visit in `ctrl_Patients`, so a window relative to index can go in the SQL. D191 forbade date windows as a way to split a pull. A window that's part of what the study measures is a different thing.

**Diagnoses: first instance only would break the case rule.** The first instance alone can't tell 1 date from 2, and it's taken before ruled-out rows are dropped. What's safe:

1.  **A window:** `DiagnosisDate >= IndexDate - 30000` (index minus 3 years, as YYYYMMDD) plus a few months' margin. Python applies the exact window, as now. This drops the decades of older history that no window reads. The cost: the `all` window is gone, and a longer lookback later means pulling again.
2.  **Fewer rows per date (optional):** drop ruled-out rows in the SQL, then dedup to one row per patient, code and date. That's the same result Python computes from D17's rows. Only worth it if diagnoses are still large after the window.

**Encounters: three small tables instead of every row.** Each works with what Telescope does today (dedup and where lines):

- **First encounter:** completed encounters, deduplicated by `PatientDurableKey`, keeping the earliest `DateKey`. One row per patient.
- **Last encounter:** the same, keeping the latest.
- **Encounters around index:** completed Office Visit, Follow-Up, ED and admission encounters with `DateKey` within about a year of `IndexDate` (±10000 as YYYYMMDD, slightly wide). Python counts the exact 365 days.

Each still reads the whole date range in Cosmos, since a filter relative to each patient's index can't skip partitions. The saving is in rows landed, and at 300,000 patients that's most of the cost.

**Bigger than both: diagnoses and labs for the matched only.** D16 had this, and D30 dropped it because Telescope couldn't sample controls inside Cosmos. It doesn't need to:

- **Stage 1:** `ctrl_Patients` plus the three encounter tables, for the 300,000-patient pool. Then MatchIt.
- **Stage 2:** upload the matched keys, about 30,000 at 5×, as the PK (an uploaded PK works today). Pull diagnoses and labs for those patients only.

That's about a tenth of the diagnosis rows, before any window, and it doesn't touch how anything is analysed. The cost is two pulls and a match in between.

**For you to decide:**

- Whether to propose these to pheauxWAS as decisions there. Its D12, D16/D30 and D23 change.
- Which ones to take:
  - Stage 2 alone is the largest saving, and it changes no analysis.
  - The window drops the `all` option.
  - The encounter summaries change `build_group_parquet.py`'s inputs.

None of this needs a Telescope change. A `GROUP BY` table (one row per patient with counts) would be cleaner than three encounter tables, but it's a new feature.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## A code library in YAML: `reference/codes/ICDs.yaml`

(in the chat, 6 Oct) Moved the GI conditions to `reference/codes/ICDs.yaml`, per the Code Finder idea. I like it as YAML more than as parquet: YAML for people to read, maybe a YAML-to-parquet step after each update, since I won't add to a parquet by hand. Or keep it YAML from the start and have `scope.py` copy it over. Grouped by specialty (GI, Cardiology, Allergy...) and then disease type (Upper GI, pancreatic, autoimmune, heart failure...), so I can add sections as I go, as if recreating the ICDs for the GI multiplier.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: keep YAML as the only source; pick conditions from it in Author, which writes them into the template; make a parquet only when something reads one**

**Done in the chat.** `ICDs.yaml` is now `specialties > GI > <disease type> > <condition>`. The disease types are UpperGI, GIBleeding, FunctionalBowel, Diverticular, AutoimmuneInflammatory, Colorectal, Pancreatic, Biliary, Liver, CirrhosisComplications and ViralHepatitis. Each condition has `name`, `icd9`, `icd10` and an optional `note`. A condition's key (`GERD`, `PepticUlcer`) is its multiplier level name, so its tables come out as `GERDPtsWithDx`.

There are 47 conditions. The 28 in `GI_Conditions_intake.yaml` match it code for code. The rest are the items that intake left out (screening, history, cirrhosis complications, EPI, hepatitis C...), kept under their own disease types.

**What the code does today.** Nothing reads `reference/codes/`. It is not in `datascope.json`, and no bundle carries it. The intake's 28 levels were written by hand. The roadmap's Code Finder plans a parquet per vocabulary, with categories, read later as a supporting table (In supporting table, D119).

**Ways a pull could use the library:**

1.  **Picked in Author, written into the template.** Under Multipliers: "From the code library", where you tick a specialty, disease types or single conditions. It writes one level per condition, with `ICD_Value` set to its codes, as the GI intake has now. The template and blueprint show every code (D45). Nothing new travels to the VM, and editing the library later never changes a pull already written.
2.  **Named in the template, expanded at Validate** (`codes: GI/Pancreatic`). The template is shorter, but editing the library silently changes the next export of an old intake. I'd not do this.
3.  **A parquet made from the YAML** (`scope.py codes`, pyarrow on both machines), one row per code: Vocabulary, Code, Condition, DiseaseType, Specialty, Name. It is only needed once a table reads codes from a supporting table rather than from a level's `ICD_Value`, or once the Code Finder exists. Generate it then, never by hand. The YAML stays the source.

**Recommendation:** 1 now, with: - a `codes` entry in `datascope.json` (D111); - a check of the library on load. Condition keys must be unique across all specialties, since they become table names. ICD-10 codes must start with a letter, and ICD-9 codes must be digits, V or E. A clash is a loud error naming both places.

3 waits for the Code Finder.

**For you to decide:**

- 1, 2 or 3 (or 1 now and 3 later).
- Should condition keys be unique across the whole library (as now), or only within a specialty, with the specialty added to the table name when two specialties clash?
- Should other vocabularies (SNOMED, CPT, LOINC) get their own files beside `ICDs.yaml`, as the Code Finder's one-parquet-per-vocabulary has it?

**Suggested order**

1.  The library check and the `datascope.json` entry: small, and they stop a bad edit before any pull reads the file.
2.  "From the code library" in Author's Multipliers: what makes the YAML pay off.
3.  The parquet, with the Code Finder: nothing reads one yet.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## Author's bundle-building code in what ships (option 3)

(in the chat, 7 Oct) Bundle 2 works on the VM; from now on nothing that ships says how it got there (D203).

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: the one place code still names bundling**

Author's Exports tab has the bundle queue and the Bundle With Manager and Bundle YAMLs only buttons. They only appear on the Mac, but their code ships: 25 lines in `yamlmanager_model.py` and `yamlmanager_tk.py` (`make_bundle`, `queue_add`, the `bundle_pullmanager` import). A developer reading Author's code on the VM would see them.

Taking them out of what ships means moving that code into a Mac-only module that Author loads when it is there. It's a refactor of the two Author files, with no change to what either machine shows.

**For you to decide:** whether and when. Nothing a VM user sees depends on it.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## Pull and clear \_sp files when batch done

(from `tasklist-temp.md`) When the \_sp of a project is done, archive them to parquets and then clear the drive space. It should also be happening for each table as discussed before. It does not seem to be happening. Confirm current status of that feature.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: not built yet; D177 is the feature, and it covers SneakPeek's tables too**

**What the code does today.** A pull packages only once, at the end of a clean Execute (D141), and then drops its tables (D165). Nothing packages or empties a table mid-pull. A pull that fails, as GI_Conditions did, packages nothing and drops nothing, so SneakPeek's finished tables stay in the database while Cosmos runs out of room beside them.

**What was agreed and isn't built:** D177, packaging each table group as it finishes, then emptying its tables. With D190 every ungrouped table is its own group, so that is each table, SneakPeek's included: under `Dual` SneakPeek's sessions run first, so their tables would be parquets and gone before Cosmos starts. It is in the roadmap's Fixes, In Order, after the Status colours (D198, D204), alone in its bundle because it empties tables of real data.

**Until then, by hand**, on a pull that has stopped: Artifacts packages every finished table (it refuses while the pull executes), then clear_projects_db drops them; Artifacts by hand drops nothing itself (D165). Check the parquets before dropping. Then Retry failed.

**For you to decide:** nothing new; D177 is already agreed. Whether to move it ahead of the Status colours, since it is what frees the room.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## Size issue with GI_Conditions

(from `tasklist-temp.md`) I ran GI Conditions and it seemed to go fine with all the SP ones. Then it stopped at the first Cosmos run. It says that all the \_sp tables total were about 1.2 GB and the database had 15 GB. Yet when doing just the PKs and index diagnoses for that first GI condition it told me it ran out of space. It seems improbable. I'm re-running, but see if there's a reason this could be happening.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: not improbable; Cosmos is about a hundred times SneakPeek, and a landing needs its room more than once while it lands**

**Why it can be.**

- **SneakPeek is about 1% of Cosmos** (HaT: 49 patients against 5,969). So 1.2 GB of SneakPeek tables means roughly 100 GB or more for the same tables in Cosmos, over all 28 diseases. The first Cosmos session is GERD, among the commonest of them, so its PK alone may be several GB, maybe more than 15.
- **The window is wide.** `min_date_key` is 19900101, so the PK and IndexDiagnosis read every GERD diagnosis since 1990.
- **A landing holds its rows twice, and logs them once.** Each table first lands in a staging table, `#Local_<dest>`, in the Projects server's tempdb, outside any transaction. Then one `INSERT` copies it into the destination, in a single transaction (`local_sql.py`, `render_transfer`). So while it runs, the rows sit in tempdb and in the data file, and the whole insert sits in the log, which stops at 20,000 MB separately and can't reuse space until the transaction ends. A 9 GB PK can fail with 15 GB free in the data file.
- **The PK can't be chunked.** `chunk` splits runs, not the PK phase, so the PK always lands whole.

**What would settle which one.**

1.  **The error's text** (Status, double-click the failed row). `1105` naming the project database means the data file. `9002` means a log, and it names which: the project database's log, or `tempdb`'s. A tempdb error is the staging table.
2.  **GERD's SneakPeek size.** Rows and MB of `GERDPtsWithDx_sp` and `GERDIndexDiagnosis_sp`, from clear_projects_db or Status. Times about 120 gives what Cosmos needed.

**Before the re-run reaches GERD again** (it will fail the same way):

- **Narrow the window**, if the study allows (2015 on, as other pulls use). This is the biggest lever and safe for accuracy: the window is the study's definition, not a split (D191).
- **Fewer PK columns.** `ICDName` (NVARCHAR 850) and five race columns are wide; the PK needs the key, the index date and what the study reads.
- **Free the room first**: Artifacts, then drop SneakPeek's tables (the topic above).

**For the code, later:** the size estimate (roadmap) would have warned at stage 2, after SneakPeek, before GERD started. And a PK landed in chunks, or landed without the full staging copy, would need room for one piece at a time; that is new work, and I'd decide it after seeing the error.

**For you to decide:** the window and PK columns for the re-run. And send the error text and GERD's `_sp` sizes, so we know which limit it hit.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:** (in the chat, 7 Oct) The error is lost. Restarting GI_Conditions from 2015 (11 years is plenty) as a smaller pull, and retrying. Nothing for Claude to do.
:::

**Suggested order: what to tackle next**

1.  **GI_Conditions' room**: re-running from 2015 (7 October). If GERD fails again, keep the error text this time (Status, double-click the failed row), and GERD's `_sp` sizes.
2.  **The Status colours, D198 and D204**: agreed (7 October), small, Run's window only, and first in the roadmap's Fixes, In Order. Then a packaged table is clearly safe to double-click. Being built while GI_Conditions runs.
3.  **D177, packaging each group as it finishes**: what frees the room mid-pull, SneakPeek's tables included. Already agreed; GI_Conditions is the real pull on the current software it waited for. Built after the colours, in a bundle of its own.
4.  **Does the PheWAS need every encounter and every diagnosis?**: the barebones control pull is running, and pulling diagnoses for the matched controls only would cut its largest table to about a tenth. A pheauxWAS decision; no Telescope change.
5.  **Returning to SP first**: needs a screenshot of where the order looked wrong; the code and both runs say SneakPeek goes first.
6.  **The code library**: its check and `datascope.json` entry are small, and stop a bad edit before anything reads the file; nothing reads it yet.
7.  **Author's bundle-building code**: nothing a VM user sees depends on it.

Moved out on 6 October: the batching thread (**one table per group** D190, **no date windows** D191, **the Meds filter** D192, **SneakPeek per patient** D193; the measurements in `design.md`, Batching; the UC and Crohns re-pulls and the columns check in the roadmap's **Next: On The VM, 4 October 2026**); **Guessing/smart chunking** and **Dynamic ordering** into the roadmap's **Estimate Size And Packaging By Chunk**; the **HaT control pull** (stopped, pulled again with the barebones intake; its timings in `design.md`, What a batch costs; green tables D198); **Barebones blueprints** into the roadmap's **Choosing A Study's Columns**, study-specific, how still open.

Moved out on 7 October: **Status fixing** is D204 (a packaged table purple), first in the roadmap's Fixes, In Order, with D198.

## Settled

Nothing waiting.