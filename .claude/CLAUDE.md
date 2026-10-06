# Telescope

YAML Manager authors and plans data pulls; Pullmanager executes them on an air-gapped Windows VM against Cosmos and Projects SQL Servers.

Read before working:

- `plan/design.md`: what the system is, as built, including environments, delivery to the VM, and testing.
- `plan/roadmap.md`: status, known bugs, open problems. Check it first.
- `plan/decisions.md`: why things are the way they are (D1 onward). Read the relevant entry before reversing anything; append a new entry rather than editing an old one's reasoning.

## Keeping The Docs True

- A fact lives in one of those three documents only. Status lives only in the roadmap.
- When code changes behaviour, update `design.md` in the same commit, except during a planned run of fixes (below), where the docs catch up after the user has discussed the results. When an item is built, delete it from the roadmap. When something is decided, add a numbered decision.
- `HowThisRepoWorks.md` (root; in every bundle, D199) walks a developer on the VM from template to SQL to parquet, naming the functions. When a step or a function it names changes, update it in the same commit. It is written as if everything runs on the VM: no Mac, no copying over, no history.
- Do not add new design documents. A temporary brief for the VM (questions to put to its AI) is the exception; delete it once its answers are folded in. `plan/tasklist.md` is not a design document: it holds only what is still under discussion (D128).
- **"Update docs"** means all four: bring `design.md`, `decisions.md` and `roadmap.md` up to date with the code by the rules above, then extend `plan/commemorating/thoroughhistory.qmd` from its stated cut-off to the latest commit (timeline, numbers, defects, decision index, open questions) and move the cut-off. Last, run `python3 scope.py images`, which deletes every pasted image (`paste-*`) in an `images/` folder under `plan/` that no document mentions (D132). The history is a record, not a design document: it may repeat facts, and it keeps what the other three have since deleted.

## Planning And Doing Work

The user works in this cycle; follow it for any change bigger than a small fix.

0.  **A new chat** is usually started with a task-list section's heading: read that section, the plan documents above, and the code behind it, then answer under it. Before a chat ends, write anything it settled or learned that lives only in the chat into the task list (or, if agreed, the plan documents).
1.  **Respond topic by topic, in `plan/tasklist.md`.** When the user brings research, notes or ideas, read the code behind each topic first. Under each, in a blue-bordered box headed `**🟦 Claude: <topic>**`, say what the code does today, give a recommendation, and end with "For you to decide" where the choice is theirs; follow it with an empty orange-bordered box headed `**🟧 Your response:**`, with a blank line inside for them to type on. Close with a numbered **Suggested order**: one line per item, most urgent first, saying why it sits where it does. Agreed items move to the `# Settled` section at the bottom; new notes go above it.
    **Reply style (D174, option 5 in `plan/tasklist format tests/format_test.md`).** Each box opens with `::: {style="border:2px solid #4a90e2; border-radius:6px; padding:8px 12px; margin:8px 0;"}` (orange `#e2904a` for theirs) and closes with `:::` on its own line. Coloured text labels (option 6, D173) are retired: the colour was lost when the user typed beside a label. Do not use Quarto callouts, GitHub alerts, `<details>`, tables or headings to mark replies.
2.  **Document before code.** Once the user agrees, write the numbered decisions, put the order in `roadmap.md` as "Next: Fixes, In Order", and move each settled item out of the task list into the three documents, then delete it there: Settled should not become a graveyard. Commit and push, so there is a clean slate to revert to.
3.  **Build in that order.** One commit per item (small ones may share), each with its outcome tests and a rebuilt bundle if a bundled file changed. Push at the end.
4.  **Report, discuss, then document.** Report what was built, what was chosen along the way, and what the user needs to do or decide. Do not edit the plan documents with the results until the user has discussed them.

## Layout

- `python3 scope.py` opens the app (Author and Run); `scope.py test` runs the suites (D112, D123). On the VM, extraction writes a `scope.py` that opens the same app.
- The core files (data dictionary, recipes, template, the VM's package list and `requirements-vm.txt`) are in `reference/`; `datascope.json` at the root says where each is and where runs go, and is the one place to change if they move (D111).
- `YAMLs/` holds the pulls: intakes in `YAMLs/temp/`, test templates in `YAMLs/manager_test_cases/`. Runs and the Python cache go to `cleanup/`, which is disposable (D113).

## Constraints

- Code reaches the VM only through a bundle in `dist/`: `bundle.py`, the software with the queued pulls' transfer YAMLs, or `yamls_to_transfer.py`, the YAMLs alone (D122). Bundles are deterministic build products and `dist/` is not committed (D106). A build carries and then empties `YAMLs/temp/bundle_queue.txt`, so build only when a bundle is wanted; to check a build without touching the queue, `python3 makebundle.py --no-queue --out <scratch file>`. No test may reach the real queue.
- Standard library only, unless the package appears in `reference/DSVM Plugins.yaml` (the VM's installed list). The VM runs Python 3.13.9.
- A test that needs a package this machine lacks: look it up in `reference/DSVM Plugins.yaml` and install exactly that version, with no need to ask: `python3.13 -m pip install --user <package>==<version>` (an R package at its listed version the same way). Not listed means the VM does not have it: do not install it, and do not depend on it.
- Nothing here can reach a database. Database code is tested against fakes, and the user runs it on the VM and reports back, often with screenshots.

## Working Conventions

- Tests are stdlib `unittest`. Run every suite before committing: `python3.13 scope.py test` runs all six and says which passed (`scripts/makeYaml.py`, `scripts/pullmanager_src/pullmanager.py`, `scripts/bundle_pullmanager.py`, `scripts/yamlmanager_model.py`, `scripts/yamlmanager_tk.py`, the app's Author view on a real Tk, which skips without a display, and `scripts/tidy_images.py`; each also runs alone with `--tdd`). Upload tests need `pyarrow` and skip without it.
- A bug fix gets a test of the **outcome** (what data ends up where), confirmed to fail with the bug reintroduced. D46 is what happens otherwise.
- Prefer a loud error that suggests a fix over inferring what the user meant. The user wants to make the choice (D28, D45).
- Work on `main`. The long-lived `pullmanager` branch was merged into it (September 2026) and work continues on `main`. The user asks for commits and pushes; end commit messages with the `Co-Authored-By` line.
- Never run destructive git commands on uncommitted work.
- Mac: use `python3.13` (python.org 3.13.9, Tk 8.6, and the VM's numpy 2.1.3, pyarrow 22.0.0, ruamel.yaml 0.17.17, pyyaml 6.0.3) to match the VM. Dev box: use brew's Python for anything needing tkinter (`/var/home/linuxbrew/.linuxbrew/bin/python3`).