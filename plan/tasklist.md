---
title: "Task list"
---

File for discussing ideas/improvements. Your notes, then Claude's reply in a box with a blue border, and yours in one with an orange border; type anywhere inside the orange box, between its `:::` lines (D174). New notes go above **Settled**; what is agreed moves there, then into the plan documents. Pasted images land in `images/` beside this file; `python3 scope.py images` deletes one once nothing mentions it (D132).

# Tasks

## Cosmos "refreshed" when it was not

(From chat, 2026-10-02, your question under the size rescue: "Is it actually restarting just the failed ones?")

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: a false refresh started UC over**

**No, that time it restarted everything.** Your screenshot of UC's retry says `Cosmos was refreshed: created 2026-09-17T19:34:56.450 when last run, 2026-09-17T19:54:30.070 now`, the same for SneakPeek, then "every session starts over". There was no refresh. Both times are 17 September, 20 minutes apart.

**Why.** Cosmos is served by several instances; the one you land on can change from one connection to the next (D37; your logs show ET4003VPDSQL033 now, 032 earlier). Each instance has its own copy of the database, restored a few minutes apart at each refresh. D51 records `create_date` from whichever instance answered first, and compares it to the millisecond. So landing on a different instance looks like a refresh, and Execute re-pulls everything. Worse, a table group's new Cosmos connection (D134) runs the same check, so landing on another instance mid-pull fails the session.

Little was lost this time: UC's finished Diagnoses tables were saved and skipped, and its other groups had failed anyway. But any pull can hit this on any retry.

**Recommendation.** A refresh is a change of more than a day. Refreshes come about monthly and the copies differ by minutes, so a one-day margin can't miss a real one or mistake a copy for one. Record each instance's value too (`create_date` per `@@SERVERNAME`), so the log can say "another instance, same refresh" rather than nothing. Test: a manifest recorded on one instance, a connection on another 20 minutes newer: nothing starts over. A month newer: everything does.

**To see if it hit other pulls:** `Select-String -Path runs\*\execute-*.log -Pattern "was refreshed"`.

**For you to decide:** the one-day margin (recommended), or matching by instance only (an instance not seen before is recorded, not compared)? It goes first in the roadmap's fix list either way.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

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

**5. Force a late error.** Open `YAMLs\temp\ShowTest_blueprint.yaml` in Notepad. Find any fact table's `columns:` list, for example under EDVitals. Add this as a new line, indented exactly like the `- {source: ...}` lines around it:

```         
      - {source: 1/0, name: Boom, type: INT}
```

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:** Author seems to have caught it:

![](images/paste-18.png)![](images/paste-19.png)

![](images/paste-23.png)

I guess we won't know until we actually see that "divide zero" is truly not accepted.
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: step 5, so far right**

Author's note is a warning, not a refusal: it can't check `1/0`'s type against the dictionary, and that's all it says. So the split went through, as it should. Step 5 is about what happens at Execute: press **Execute on test_over_zero**. Its run must end FAILED with `Divide by zero`. Before the fix, it ended `done` with no rows.
:::

Afterwards:

```         
Select-String -Path runs\test_over_zero\execute-*.log -Pattern "Divide by zero|FAILED"
```

Pass: that run is FAILED, with "Divide by zero" in its message.

**6. The scan, after every pull:**

```         
python scope.py --scan-runs
```

**Clean up afterwards.** The test pulls' tables stay in Projects: ShowTest2's because its scan found empty tables, test_over_zero's because its run fails. In clear_projects_db, drop the tables starting with each one's prefix (its manifest's `table_prefix`). Then delete `runs\ShowTest`, `runs\ShowTest2` and `runs\test_over_zero`, and their blueprints in `YAMLs\temp` if any are left.

# Post-Urgent Solve ideas

## Table Group recipes

(FOr after this urgent solve)

I like the table groups as a further recipe (meds, for instance, makes things much easier to add instead of all 3). I can also start thinking about making R/Python functions to make new tables based on knowing everything within a tabel group is being pulled. So, I"d like to start making the table group as part of recipes. I thought we'd already did that, but it's not in the recipes now, and there's no 'add recipe' button for the groups. Let's go ahead and do that. I think it would be pulling from the data dictionaries just the table names, wouldnt' need to tpull the whole bit about each column. After all this is a grouping of standard tables, not making a custom large one with joins. It will probably still need to have the join/where logic for ones that are interdependent (if you need birthFact to get PregnancyFact and MotherPatientDim, for instance).\
\
Go ahead and make one of each table group in Crohns_HxDxetc. as below

![](images/paste-17.png)

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:** We have this now, no?
:::

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: recipe sets exist; only Meds is saved**

**Yes, the mechanism exists** (D135, recipe sets). In Author on the Mac, each table group has **Save as recipe set**, which writes the group to `recipes.yaml` under `recipe_sets:`. Add a fact table then offers each set, and adding one puts its tables in a group of that name. The sets hold table names and how the tables bind to each other, not the dictionary's columns, as you thought. But `recipes.yaml` has only one set, **Meds** (MedOrderHistory, MedDispenseHistory, MedAdminHistory). Your **Visits** and **Diagnosis** groups, and Infant_RSV's **Hospitalizations** and **Birth**, were never saved.

**One catch for Visits.** In the UC and Crohns templates, OtherHospitalizations joins OtherDiagnoses (`{{prefix}}_OtherDiagnoses`), and a table can't read one in another group (D134). Either OtherHospitalizations goes in the Diagnosis set, or the Visits set carries its own OtherDiagnoses.

**Recommendation:** I save the four sets from the intakes (Visits and Diagnosis from Crohns_VisitsMedsDiagnoses' groups, Hospitalizations and Birth from Infant_RSV), with OtherHospitalizations in Diagnosis, and they reach the VM with the next bundle.

**For you to decide:** those four, with OtherHospitalizations moved to Diagnosis? Or name the sets you want.
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## clear_projects_db: every database at once

(From chat, 2026-10-01.) clear_projects_db works and can delete things. I'd like it to have a dropdown of the available PROJECTD databases, or just a list of them, collapsed, similar to the Status view. It should have the name and GB available, as the pull log shows them. Clicking one opens the selection of what's found, so we can delete specific tables or all of them.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: clear_projects_db over every database**

**Today.** `utils/manager/clear_projects_db.py` opens one database, typed into a Database field (server and driver from `.env`). Refresh lists its tables with rows and MB, and a line of each file's use and cap; Drop selected, Drop all, Open transactions and Free log act on that one database.

**Recommendation, as you describe it.** A tree like Status: one row per database in `DEFAULT_PROJECTS_DATABASES` (D171), closed, with its GB free (measured as Execute measures it, D164) and, if one refuses the login, why. Opening a row lists its tables under it (name, rows, MB), and the prefix (D163) groups them by pull, so a finished pull's tables are one selection. The buttons act on what is selected: tables, or a database row for all of its tables. The Database field goes; free typing stays possible for one not listed. This also suits the "View dbo tables" button in Run (D180), which would open this window.

**For you to decide:** group a database's tables by pull prefix (recommended, since D165 drops by pull), or list them flat as today?
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**
:::

## Random Sample error:

![](images/paste-24.png)

![](images/paste-20.png)

![](images/paste-21.png)

So this is because we dont' ahve a pktable key_column. I can see how this is an issue. Maybe if we choose the randomize, there's a dropdown menu of "PKTable Key to set hash by" with a dropdown.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: a key picker for a built PK**

**Today.** The error is right, and its wording is the trouble. A random sample takes the PK's rows in the order of a hash of its key (D60), and the key is the PK's first `dedup_keys` set, else its `key_column` (D69). IBDComplicationProcedures is built from the dictionary (ProcedureEventFact), and Author gives a built PK no key field. **Row key** shows only for an uploaded PK. So the fix it names can only be typed into the YAML by hand.

**Recommendation, as you describe it.** A built PK gets a **Row key** dropdown of its own columns (several allowed), written as `key_column(s)`. It shows always, not only with Random sample, since chunking and the uniqueness check need the key too (D69). With Random sample ticked and no key, the message points to that field. For a table like ProcedureEventFact, the key that makes rows unique is the event's own key (`ProcedureEventKey`), not `PatientDurableKey`, which repeats. The dropdown would offer the table's `...Key` columns first.

**For you to decide:** Row key on every built PK (recommended), or only when Random sample is ticked?
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

## Add Dedup option to Fact Tables

Come to think of it, fact tables should all have a 'deduplicate by column:' and it should let you do a dropdown of the colunms to deduplicate by. You can add multiple. And do a "order by:" with a column. I don't know how we'd say 'earliest' or 'latest' depending on the value so unless there's a way, we just say 'order by' and explain how it does it.

::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟦 Claude: Dedup fields on each fact table**

**Today.** The template already does this (D58): `dedup_keys` (one or more sets of columns; a row is a duplicate when all of a set match) and `dedup_order_by` (which duplicate survives). Your Diagnoses templates use it: `[[PatientDurableKey, BillingCodeValue]]`, by `StartDateKey`. But Author has no field for either, so it's typed by hand.

**Recommendation.** On each fact table's card, beside Columns:

- **Deduplicate by**: a dropdown of the table's columns, several allowed. It writes one key set.
- **Keep**: a column dropdown and **earliest** or **latest**. Earliest is the smallest value, so the first date or the lowest key (`ORDER BY StartDateKey`); latest is the largest (`... DESC`). That answers "earliest or latest" for dates and keys alike. The note under it says so in a line.
- Without Keep, the note warns that which duplicate survives is arbitrary and may differ between runs, as the dry run already says.

The same column dropdown serves the Row key above and `profile:` (D181), so the three are built together.

**For you to decide:** one key set in Author, with several sets still possible by hand (recommended), or several sets in Author?
:::

::: {style="border:2px solid #e2904a; border-radius:6px; padding:8px 12px; margin:8px 0;"}
**🟧 Your response:**

:::

**Suggested order**

1.  **Cosmos "refreshed" when it was not**: it throws away finished work on any retry that lands on another Cosmos instance, and can fail a session mid-pull. First in the roadmap's fix list once you choose the rule.
2.  **Show-stopper tests, steps 4 and 5**: two checks left, then the clean-up.
3.  **clear_projects_db: every database at once**: freeing space a pull at a time is now routine, and View dbo tables (D180) opens it.
4.  **Random Sample error** and **Add Dedup option to Fact Tables**: one column picker serves both, and `profile:` (D181) after them. Build them together.
5.  **Table Group recipes**: small; saving the four sets once you say which.

The agreed fixes, in order, are in the roadmap (Next: Fixes, In Order): the Projects connection per group (D176), the check for a join to another pull's table (D178), packaging each group as it finishes (D177), Specify Project DB (D179), the utility buttons (D180), then Counts (D181).

## Settled

Nothing waiting.