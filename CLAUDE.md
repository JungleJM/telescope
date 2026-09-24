# Telescope

YAML Manager authors and plans data pulls; Pullmanager executes them on an
air-gapped Windows VM against Cosmos and Projects SQL Servers.

Read before working:

- `QMDs/design.md`: what the system is, as built, including environments,
  delivery to the VM, and testing.
- `QMDs/roadmap.md`: status, known bugs, open problems. Check it first.
- `QMDs/decisions.md`: why things are the way they are (D1 onward). Read the
  relevant entry before reversing anything; append a new entry rather than
  editing an old one's reasoning.

## Keeping The Docs True

- A fact lives in one of those three documents only. Status lives only in the
  roadmap.
- When code changes behaviour, update `design.md` in the same commit. When an
  item is built, delete it from the roadmap. When something is decided, add a
  numbered decision.
- Do not add new design documents.

## Constraints

- Code reaches the VM only through `dist/pullmanager_bundle.py`. The bundle is
  committed and deterministic: rebuild it (`python3 scripts/bundle_pullmanager.py`)
  and commit it whenever a bundled file changes.
- Standard library only, unless the package appears in `YAMLs/DSVM Plugins.yaml`
  (the VM's installed list). The VM runs Python 3.13.9.
- Nothing here can reach a database. Database code is tested against fakes, and
  the user runs it on the VM and reports back, often with screenshots.

## Working Conventions

- Tests are stdlib `unittest`. Run all three suites before committing:
  `python3 scripts/makeYaml.py --tdd`,
  `python3 scripts/pullmanager_src/pullmanager.py --tdd`,
  `python3 scripts/bundle_pullmanager.py --tdd`.
- A bug fix gets a test of the **outcome** (what data ends up where), confirmed
  to fail with the bug reintroduced. D46 is what happens otherwise.
- Prefer a loud error that suggests a fix over inferring what the user meant.
  The user wants to make the choice (D28, D45).
- Branches: `main`, and the long-lived `pullmanager`, kept separate from main.
  The user asks for commits and pushes; end commit messages with the
  `Co-Authored-By` line.
- Never run destructive git commands on uncommitted work.
- Mac: use `python3.13` (python.org 3.13.9, Tk 8.6, numpy 2.1.3) to match the
  VM. Dev box: use brew's Python for anything needing tkinter
  (`/var/home/linuxbrew/.linuxbrew/bin/python3`).
