---
title: "Task list"
---

File for discussing ideas/improvements. Your notes, then Claude's reply in a box with a blue border, and yours in one with an orange border; type anywhere inside the orange box, between its `:::` lines (D174). New notes go above **Settled**; what is agreed moves there, then into the plan documents. Pasted images land in `images/` beside this file; `python3 scope.py images` deletes one once nothing mentions it (D132).

# Tasks

## Show-stopper tests: what is left to paste

Paste these into **PowerShell in the working folder on the VM**, the folder with `scope.py`.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: steps 1 to 3, read**

- **Step 1, `--tdd`: two failures, both a fault in the tests, not in Pullmanager.** Two window tests (`RunningPullTests`) list pulls, and they looked in the real `runs` folder beside `scope.py` as well as their own. They found your Test_Crohns and the running CrohnsDxVisitsMedsDiagnoses. Fixed: they now look only in their own folder (confirmed to fail here with a stray pull in place, and to pass once fixed). The fix reaches the VM with the next bundle. The 166 skips are expected: the repo's fixtures don't travel in the bundle.
- **Step 2, the reader check: `Access is denied`.** The share refuses to replace an open file whatever the sharing flag, so only the waits carry a save past a reader.
- **Step 3: passed.** `busy (Access is denied); trying again in 60s`, twice, and ShowTest2 finished and packaged itself. So the waits are enough, and the fallback isn't needed (D175). Its scan listed 3 empty tables; that's expected from a sample of 10.
:::

**4. The real-life case.** Leave Run open on a real pull overnight, with Status showing. In the morning:

```         
Select-String -Path runs\*\execute-*.log -Pattern "FileBusy|Traceback|exit code 1"
```

Pass: nothing printed.

Agreed (2026-10-02): steps 1 to 3 need nothing more. Step 4 is checked as the real pulls run, with this command after each; no `exit code 1` so far. The clean-up below waits until those pulls are done.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: steps 5 and 6, read**

- **Step 5: passed.** test_over_zero's run ended `FAILED` with `Divide by zero encountered (8134)`, and the pull wasn't packaged. Before the fix it would have read `done` with no rows. (Your note had landed inside my box; I've folded it in here.)
- **Step 6, the scan: nothing unexpected.** Crohns' and UC's four Diagnoses tables show as `no_count`: those are the ones `save_landed` saved and skipped, as the rescue said they would be. ShowTest2's and Test_Crohns' `empty` tables come from a 10-row sample.
- **One thing to check:** the scan's first line says `bundle c98c60b3`. The newest bundle is `5754255e` (the six databases in the code, D171). If c98c60b3 is what's extracted, extract 5754255e (or the next bundle, which will carry the test fix too) once nothing is executing.
:::

**Clean up afterwards.** The test pulls' tables stay in Projects: ShowTest2's because its scan found empty tables, test_over_zero's because its run fails. In clear_projects_db, drop the tables starting with each one's prefix (its manifest's `table_prefix`). Then delete `runs\ShowTest`, `runs\ShowTest2` and `runs\test_over_zero`, and their blueprints in `YAMLs\temp` if any are left.

# Questions

## save_landed puts things in \_parquets -\> does it move them?

(From `temp-tasklist.md`, 2026-10-02.)

![](images/paste-25.png)

I'm assuming the ones in Cosmos are eventually copies of the ones in cosmos_parquets?

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: nothing moves by itself; and Scope doesn't make a `Cosmos` folder**

**save_landed** only writes, to `runs\<pull>\saved_parquets\cosmos_parquets\` and `...\sneakpeek_parquets\`. It never moves or copies anything after that. **Artifacts** writes `cosmos_parquets\` and `sneakpeek_parquets\` at the top of the run folder, and empties them first each time it packages (D72). So the saved files have to be copied over by hand, once the pull has packaged: `saved_parquets\cosmos_parquets\*` into `cosmos_parquets\`, the same for SneakPeek. That's in the roadmap until D177 makes it automatic.

**Your screenshot shows folders named `Cosmos` and `Cosmos_SneakPeek`.** Nothing in today's Scope makes folders by those names. They're the database names, and the parquet viewer's button labels (Cosmos opens `cosmos_parquets`). Before D142, Artifacts wrote `parquets\Cosmos\` and `parquets\SneakPeek\`, but not `Cosmos_SneakPeek`. These tables (EDVisits, EDVitals, RSVPatients…) are Infant_RSV's. `Cosmos` has all ten, while `cosmos_parquets` lacks the three of the run that failed (EDVitals, EDLabTestComponents, RSVPatients), so `Cosmos` looks like a complete copy made some other way: by hand, or by a script of your own.

**For you to answer:** what folder are these in (the path above `Cosmos`), and did you or a script of yours make them? If they're a hand copy, the `cosmos_parquets` that Artifacts writes once Infant_RSV packages cleanly is the one to keep.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response: you'r esure nothing makes them, even in artifacts? I thought that artifacts makes them specifically. If tha isn't the case then I"m not sure what to do. It sounds like i just have to re-do the whol thing to be sure. (it's short so it's not so bad).**
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: sure, from the code; and the re-pull**

Artifacts writes only `cosmos_parquets`, `sneakpeek_parquets` and `uploads_parquets` (`artifacts.py`, `COSMOS_DIR` and the two beside it). Nothing in Scope names a folder `Cosmos` or `Cosmos_SneakPeek`.

Agreed (2026-10-02): Infant_RSV is pulled again. Done on the VM: its parquet folders (and the `Cosmos` ones) moved into `runs\Infant_RSV\old\`, keeping the manifest, `pull_files\` and logs, so Run's **Re-pull everything** has a pull to start over. To do: extract the next bundle once nothing is executing, then re-pull. The fresh `cosmos_parquets` are the ones to keep.
:::

**Suggested order**

1.  **Show-stopper tests, step 4**: the last check, then the clean-up, and extract the newest bundle if the VM is on `c98c60b3`.
2.  **save_landed and the `Cosmos` folders**: a question, so the copy-over after packaging goes to the right folder.

Everything agreed is in the roadmap's **Next: Fixes, In Order**: the one-day refresh margin (D183), the Projects connection per group (D176), the join check (D178), packaging each group (D177), the Diagnoses recipe set (D184), Specify Project DB (D179), clear_projects_db by pull (D185) and the Run buttons (D180), Row key and deduplication (D186), then Counts (D181).

## Settled

Nothing waiting.