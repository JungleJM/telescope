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

## Artifacts to "Make Deliverables"

![](images/paste-2.png)

We are no longer using 'artifacts' to make the artifacts. But if you look at "Artifacts" in design.md, there is a set of deliverables that the client will use - the two parquet folders, the contents.md, loading scripts, utils. I want this button to say "make deliverables" and when you click it it asks the folder you want it placed in. It will copy the deliverables of that project to that folder. \
\
The only other thing is 'load_parquets.r' is based on teh folder, and I want the person to choose what folder that they want to load. Specifically there will be a 'cosmos' a 'sneakpeek' and 'uploaded'. and there should be declarative var at the top of the script that says 'load\_' and each section, that is true or false. That way they can just turn on and off the ones they want. Default is Uploaded and Cosmos.

## Settled

Nothing waiting.