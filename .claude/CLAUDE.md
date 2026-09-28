# Telescope

YAML Manager authors and plans data pulls; Pullmanager executes them on an air-gapped Windows VM against Cosmos and Projects SQL Servers.

Read before working:

- `plan/design.md`: what the system is, as built, including environments, delivery to the VM, and testing.
- `plan/roadmap.md`: status, known bugs, open problems. Check it first.
- `plan/decisions.md`: why things are the way they are (D1 onward). Read the relevant entry before reversing anything; append a new entry rather than editing an old one's reasoning.

## Keeping The Docs True

- A fact lives in one of those three documents only. Status lives only in the roadmap.
- When code changes behaviour, update `design.md` in the same commit, except during a planned run of fixes (below), where the docs catch up after the user has discussed the results. When an item is built, delete it from the roadmap. When something is decided, add a numbered decision.
- Do not add new design documents. A temporary brief for the VM (questions to put to its AI) is the exception; delete it once its answers are folded in.
- **"Update docs"** means all four: bring `design.md`, `decisions.md` and `roadmap.md` up to date with the code by the rules above, then extend `plan/commemorating/thoroughhistory.qmd` from its stated cut-off to the latest commit (timeline, numbers, defects, decision index, open questions) and move the cut-off. The history is a record, not a design document: it may repeat facts, and it keeps what the other three have since deleted.

## Planning And Doing Work

The user works in this cycle; follow it for any change bigger than a small fix.

1.  **Respond topic by topic.** When the user brings research, notes or ideas, read the code behind each topic first. For each, say what the code does today, give a recommendation, and end with "For you to decide" where the choice is theirs. Close with a numbered **Suggested order**: one line per item, most urgent first, saying why it sits where it does.
2.  **Document before code.** Once the user agrees, write the numbered decisions, put the order in `roadmap.md` as "Next: Fixes, In Order", and fold any notes file into the three documents before deleting it. Commit and push, so there is a clean slate to revert to.
3.  **Build in that order.** One commit per item (small ones may share), each with its outcome tests and a rebuilt bundle if a bundled file changed. Push at the end.
4.  **Report, discuss, then document.** Report what was built, what was chosen along the way, and what the user needs to do or decide. Do not edit the plan documents with the results until the user has discussed them.

## Layout

- `python3 datascope.py` opens the app (Author and Run); `datascope.py test` runs the suites (D112).
- The core files (data dictionary, recipes, template, the VM's package list and `requirements-vm.txt`) are in `recipes/`; `datascope.json` at the root says where each is and where runs go, and is the one place to change if they move (D111).
- `YAMLs/` holds the pulls: intakes in `YAMLs/temp/`, test templates in `YAMLs/manager_test_cases/`. Runs and the Python cache go to `cleanup/`, which is disposable (D113).

## Constraints

- Code reaches the VM only through a bundle in `dist/`: `bundle.py` (the runtime), or `bundle_with_yamls.py` (with the queued transfer YAMLs). Bundles are deterministic build products and `dist/` is not committed (D106): rebuild (`python3 makebundle.py`) after a bundled file changes.
- Standard library only, unless the package appears in `recipes/DSVM Plugins.yaml` (the VM's installed list). The VM runs Python 3.13.9.
- A test that needs a package this machine lacks: look it up in `recipes/DSVM Plugins.yaml` and install exactly that version, with no need to ask: `python3.13 -m pip install --user <package>==<version>` (an R package at its listed version the same way). Not listed means the VM does not have it: do not install it, and do not depend on it.
- Nothing here can reach a database. Database code is tested against fakes, and the user runs it on the VM and reports back, often with screenshots.

## Working Conventions

- Tests are stdlib `unittest`. Run every suite before committing: `python3.13 datascope.py test` runs all five and says which passed (`scripts/makeYaml.py`, `scripts/pullmanager_src/pullmanager.py`, `scripts/bundle_pullmanager.py`, `scripts/yamlmanager_model.py`, and `scripts/yamlmanager_tk.py`, the app's Author view on a real Tk, which skips without a display; each also runs alone with `--tdd`). Upload tests need `pyarrow` and skip without it.
- A bug fix gets a test of the **outcome** (what data ends up where), confirmed to fail with the bug reintroduced. D46 is what happens otherwise.
- Prefer a loud error that suggests a fix over inferring what the user meant. The user wants to make the choice (D28, D45).
- Work on `main`. The long-lived `pullmanager` branch was merged into it (September 2026) and work continues on `main`. The user asks for commits and pushes; end commit messages with the `Co-Authored-By` line.
- Never run destructive git commands on uncommitted work.
- Mac: use `python3.13` (python.org 3.13.9, Tk 8.6, and the VM's numpy 2.1.3, pyarrow 22.0.0, ruamel.yaml 0.17.17, pyyaml 6.0.3) to match the VM. Dev box: use brew's Python for anything needing tkinter (`/var/home/linuxbrew/.linuxbrew/bin/python3`).