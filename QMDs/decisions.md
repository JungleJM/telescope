# Architecture Decisions

Why things are the way they are. `design.md` says what the system *is*;
this says what was chosen, what was rejected, and what it cost. What is still
undecided lives in `roadmap.md`.

Entries are grouped, numbered stably, and never renumbered. A reversed decision
keeps its entry and gains a **Superseded by** line, because the reasoning that
led to the wrong answer is usually the reasoning that will lead there again.

---

## Method

### D1. Ground contracts in real generator output, not design prose

**Context.** The plan and the YAML Manager design document described the
manifest schema in prose. The obvious start was to build against that description.

**Decision.** Generate real `--export-split` output and build against it.

**Consequences.** Found six schema errors immediately, the largest being
`source: {preyaml}` which is really `source: {template, recipes}`. Every
correction is pinned by a test, so drift fails loudly rather than quietly.

Later it paid again, in the other direction: `yamlprocessing.md` recorded ten
mismatches between `inputSimple.yaml` and the SQL it produced, and comparing
against the real files showed nearly all of them were OCR damage in the
transcriptions. The old generator was faithful. That reversal is what made its
output usable as a specification for Phase 4 instead of a cautionary tale.
(Those files were deleted in `721ed36`; recover them from git history.)

**Cost.** The analysis documents had to be retracted in part, and three
fixtures replaced.

### D2. Treat the old code as a reference quarry, not a porting source

**Context.** The old generator exists but is described as needing heavy
refactoring, and the user deliberately worked without it.

**Decision.** Do not port. Extract narrow mechanical answers — connection
strings, `cursor.messages`, `GO` splitting, `nextset()`, transaction policy —
and write them down in the contracts. Architecture comes from the plan.

**Consequences.** Two old patterns are explicitly *not* reproduced, with
reasons recorded: selecting SQL blocks by substring-matching a table name, and
scraping row counts from result-set column names. See D31, D32.

---

## Bundle And Delivery

### D3. Ship one self-extracting file, not a directory

**Context.** The VM cannot pull from git or sync with the Mac. Code moves by
hand.

**Decision.** Develop as normal modules; bundle them into a single `.py` that
extracts itself.

**Consequences.** One file to copy, and no way to half-update a tree.

### D4. Comment-prefix the payload rather than base64 it

**Context.** A bundle is a `.py` file Python must parse, so raw embedded source
would be executed. Three options: base64, one giant triple-quoted string, or
prefixing every payload line with `# `.

**Decision.** Comment-prefix.

**Consequences.** The bundle stays readable, so a file can be read straight out
of it and copied back to the Mac for repair — which is the stated VM patch
workflow. Base64 would have broken that. A triple-quoted string breaks the
moment a source file contains the quote sequence.

An unplanned benefit: marker forgery becomes structurally impossible. A source
line that itself looks like a marker encodes to `# # === ...`, which no longer
matches the anchored pattern. No defence needed.

Two details make it byte-exact: a blank line encodes as `#` rather than `# `,
so trailing-whitespace stripping cannot corrupt it, and CRLF sources are
rejected at build time rather than silently normalized.

### D5. Make bundles deterministic by omitting a build timestamp

**Context.** A generated artifact in git churns on every rebuild.

**Decision.** No timestamp. Identity comes from `content_id`, a hash over the
sorted file list.

**Consequences.** Rebuilding unchanged sources leaves git clean, and two
bundles can be compared for equality. The build date is recoverable from git.

### D6. Extraction refuses by default and stages before swapping

**Decision.** Verify size and SHA-256 both before and after writing, stage into
a temp directory, and swap only once everything verifies. Refuse absolute
paths, `..`, drive letters and backslashes. Replace a previous extraction, but
refuse any other directory without `--force`.

**Consequences.** A bad bundle leaves the existing runtime untouched. The
Windows-specific path forms matter because the VM is Windows and
`os.path.isabs("C:\\evil")` is False on Linux.

---

## Testing

### D7. Use stdlib `unittest`, not a bespoke harness or pytest

**Context.** The repo convention was an embedded `--tdd` harness, justified by
VM portability. By the second suite it had been copied twice.

**Decision.** `unittest`. Stdlib, so portability holds; real assertion
introspection, so a failure shows a diff instead of a dumped JSON blob.

**Consequences.** `makeYaml.py` migrated too, losing 104 lines while keeping
the same 35 tests and the `--tdd <group>` interface. Test count rose in the
process, because `subTest` made table-driven cases cheap.

### D8. Ship the tests inside the bundle

**Context.** Tests are roughly half the shipped source.

**Decision.** Bundle them anyway.

**Consequences.** The VM can prove a delivered bundle is sound with no network
and no repo: `--extract` then `--tdd`. Worth the bytes. Tests that need repo
fixtures skip cleanly when extracted.

---

## Manifest And Status

### D9. Mutate the loaded mapping in place

**Decision.** Wrap the loaded dictionaries rather than parsing into typed
objects and re-serialising.

**Consequences.** Keys this version does not understand round-trip untouched,
so YAML Manager can add fields without breaking an older bundle on the VM.

### D10. Write the manifest atomically

**Context.** The manifest is the only record of what completed.

**Decision.** Write to a temp file and rename into place.

**Consequences.** An interrupted write cannot truncate the record that resume
depends on.

### D11. Derive session status from its children

**Decision.** Recompute on every save rather than setting it independently.

**Consequences.** A session's status cannot drift from its phases and runs.

### D12. Put skip and block reasons in `note`, not `error`

**Context.** The schema had one free-text slot, `error`.

**Decision.** Add a `note` field.

**Consequences.** A skipped phase no longer carries an `error` block that makes
it read like a failure. One extra additive field.

### D13. Phase names are a closed set

**Decision.** `setup`, `upload_cohorts`, `pk` only; anything else raises.

**Consequences.** A hand-edited or newer manifest fails loudly instead of
silently skipping work.

### D14. Add session epochs before they were needed

**Context.** Global temps die with the connection, so `done` does not mean the
output still exists. Resume policy was Phase 9 work.

**Decision.** Add the epoch fields during Phase 2 anyway.

**Consequences.** Cheap then, awkward to retrofit later. It turned out to be
needed much earlier than Phase 9 — see D24.

---

## Batching

### D15. Batch dimensions cross-multiply

**Context.** `makeYaml` emitted one run per batch *axis*: `state[LA,MS]`,
`sex[Female,Male]`, `chunk-2000`. Three runs each describing a different
slicing of the whole cohort.

**Decision.** They multiply. `state × sex` is four runs, each a disjoint slice.

**Consequences.** A generator defect, fixed in `session_runs()`. The old
behaviour would have pulled the full cohort three times over.

### D16. The unit of multiplication is the bucket, not the value

**Decision.** `include_other: true` contributes a catch-all bucket, so
`values: [Female] + include_other` is two buckets.

**Consequences.** The catch-all must record what it excludes; `is_other: true`
alone cannot build a predicate. Missed initially and found while writing Phase
7 — `excludes` was added then. Its predicate includes `IS NULL` explicitly,
because `NOT IN` never matches NULL and those rows would vanish silently.

### D17. Expand at plan time only what is knowable at plan time

**Context.** Three knowability classes, not two: explicit value lists,
`values: all`, and `row_chunk`.

**Decision.** Explicit lists expand into the manifest with stable `run_id`s.
`values: all` and `row_chunk` stay logical in `batch.runtime`.

**Consequences.** Resume and retry can address a run by id. `values: all` is
currently refused with an explanation rather than half-supported.

### D18. Batches narrow the PK by replacing the temp's contents

**Context.** Run YAML joins the PK temp by name. A batch could rewrite the SQL,
add a filter, or change what the temp holds.

**Decision.** Repopulate `##JVM_<pk>` with that batch's rows and run the cohort
SQL unchanged.

**Consequences.** The renderer needs no knowledge of batching at all. Whole PK
rows are uploaded, not just keys, because batching selects on PK attributes and
cohort joins may use them.

### D19. Select batches against the Projects copy, on one code path

**Context.** With no linked server back to Projects, keys travel through the
client. A fresh run could chunk the existing Cosmos temp instead and skip a
round trip.

**Decision.** Always read the durable Projects copy, fresh run and resume
alike.

**Consequences.** Resume is exercised every night rather than only when
something breaks, which is where recovery bugs hide. Costs a few small uploads
on overnight jobs. The Projects copy also does not move under a Cosmos refresh,
so a batch means the same rows on a resume.

### D20. Verify PK uniqueness before any batch runs

**Context.** Chunking orders by the key. Ties can come back in any order, so a
chunk would mean different rows each run.

**Decision.** Compare `COUNT(*)` against `COUNT(DISTINCT keys)` when the PK
completes, and fail if they differ.

**Consequences.** Also a useful check on dedup configuration. A PK declaring no
`key_column` cannot be checked, so that warns — `template.yaml` does not
declare one, `inputSimple.yaml` does.

---

## Write Mode And Failure

### D21. Drop and create once per session; runs append

**Context.** The old generator dropped inside every transfer block.

**Decision.** Move it to the setup phase.

**Consequences.** Batches accumulate into one complete table. The old placement
would have left only the last batch.

### D22. Wrap only the final insert in a transaction

**Decision.** The `OPENQUERY` pull into `#Local_<dest>` stays outside it.

**Consequences.** A run that fails midway cannot duplicate rows on retry, and
no lock is held while data crosses the linked server. `pyodbc` defaults to
`autocommit=False`, so this was already close to the old behaviour.

### D23. A failed run does not stop its siblings; a failed phase does

**Context.** Fail-fast was the original plan. But these run overnight, and
learning about four truncation errors takes four nights that way.

**Decision.** Runs continue past a failed sibling, because batches are disjoint
appends and independent once the PK exists. Phases block what follows, because
setup, uploads and PK are prerequisites. A session that fails does not stop the
next one.

**Consequences.** One night produces one list of every failure. The session
still rolls up to `failed`, so a partial table cannot read as complete.

### D24. Default resume replays the whole session

**Amended by D46:** the `--resume-partial` half of this was built wrongly and
is disabled. Full replay stands.

**Context.** The user's instinct, and the safe answer.

**Decision.** Default to full replay. `--resume-partial` opts into keeping
completed local transfers.

**Consequences.** Partial resume needs a guard, because Cosmos is a refreshing
snapshot and appending later batches onto earlier ones drawn from a different
population would stitch one table from two cohort definitions, silently. D25
largely removes that risk.

This decision landed in Phase 5, not Phase 9, because a planner that ignores
epoch staleness produces a *wrong* plan — it would claim completed server-side
work need not replay.

### D25. Restore the PK by re-uploading it, not by re-querying Cosmos

**Amended by D46:** the resume this served is disabled. The mechanism survives
in batch selection (D19), which always reads the Projects copy; the row-count
guard described in D24 was never built.

**Context.** On resume the PK temp is gone. Re-running the PK query risks a
different population if Cosmos refreshed in between.

**Decision.** Upload the saved Projects copy back up.

**Consequences.** Identical key set by construction, and chunk boundaries stay
reproducible. Needs no linked server, because it goes through the client like
every other upload.

Earlier framing of the risk was wrong and is recorded here to avoid repeating
it: the danger is not that a `PatientDurableKey` starts pointing at a different
patient — "durable" means stable across refreshes — but that the *set* the
query returns changes, since `IsCurrent`, `IsDeleted` and similar flags move.

---

## Validation

### D26. `datadictionary.yaml` is the source of truth for types

**Decision.** Check every cohort column against it by type family, resolving
aliases through `from` and `join`.

**Consequences.** Catches the `p.Type` versus `dt.Type` mistake, where the
alias exists and the column exists but on a different table. Found a real
defect on first run: the embedded test fixture selected `p.FirstRace` while
declaring only the `dxf` and `dt` aliases, so it described SQL that could not
bind.

Families compare abstract dictionary types to T-SQL. Widening is accepted, an
integer fits a BIGINT; narrowing is not. Lengths are not compared, because the
dictionary does not record them.

### D27. An unknown table is a hard error

**Context.** The softer default was to warn, since the dictionary covers only
part of the schema.

**Decision.** Error. Usually an invented or pseudocode table name, and the
dictionary is meant to stay complete.

**Consequences.** Adding a table to the dictionary becomes a required step
rather than optional. Turning it on cost exactly two additions.

### D28. Accept legacy `dedup_key`, with a warning

**Superseded by itself — originally decided as a hard error.**

**Context.** Canonical is `dedup_keys`, a list of lists. The old generator
recognised only that spelling and, given the singular, silently emitted no
deduplication at all.

**Decision.** Accept and normalize, warning loudly. Reversed from hard-erroring
because the outcome being guarded against is silent data loss, and accepting
the typo while saying so removes that outcome entirely. Refusing the file only
relocates the friction.

### D29. `stop_at_for_pk_table` limits the root PK only

**Context.** The old generator applied it to every PK-typed cohort, so a
chained PK became 500 patients and then 500 of their events.

**Decision.** Limit the root — the PK cohort joining no other PK's temp — which
is derivable structurally, so no new YAML field.

**Consequences.** Under `cosmos_db: Dual` there are two roots, one per
database. They are parallel chains, not competing ones, and each is limited.

### D30. Qualify `from` and `join` alike, and never twice

**Context.** The old generator qualified `FROM dbo.X` but left `JOIN X`
unqualified. Both work under a default schema; the inconsistency is arbitrary.

**Decision.** Qualify both, skipping anything already carrying a schema or
beginning with `#`.

**Consequences.** Writing the test caught a real `dbo.dbo.` bug: the identifier
pattern stopped at the dot, so a qualified name never looked qualified.

---

## Rejected Patterns

### D31. Never select SQL by searching its text

**Context.** The old runner chose which parts of a combined `Projects.sql` to
execute with `f"dbo.{dest_table}" in block_sql.lower()`.

**Decision.** Address every block by manifest id.

**Consequences.** The old approach is broken on prefix names and fires on this
project's own data: `dbo.PKTable` matches the `PKTable2` block, so `PKTable2`
was dropped, refilled and re-reported on every run. Correctness survived only
because the transfer happened to be idempotent.

### D32. Telemetry has a declared shape

**Decision.** Read telemetry from result sets with known columns tied to
manifest ids, rather than inspecting arbitrary column names.

### D33. Drop the unfiltered `source_raw` count

**Context.** A full `COUNT_BIG(1)` over a fact table for an approximate number.

**Decision.** Remove it. The cheap metadata alternatives
(`sys.partitions`, `sys.dm_db_partition_stats`) are not readable with the
permissions available on Cosmos — tested, `VIEW DATABASE PERFORMANCE STATE` is
denied and `sys.partitions` returns nothing.

### D34. Measure column widths, but only report them

**Context.** The ask was to size `VARCHAR` from `MAX(LEN(col)) + 50`.

**Decision.** Measure at the staging table and report. Do not apply to
destination tables.

**Consequences.** Applying it cannot work with batching: the destination is
created before any batch runs, so sizing from the first batch would truncate a
later one carrying a longer value. Uploads are the exception and *do* size from
data, because the whole file is in hand before the table exists.

Related: a blanket wide default is not free. Storage is unaffected — `VARCHAR`
stores actual length plus two bytes — but SQL Server sizes sort and hash memory
grants from *declared* width, and `fast_executemany` allocates buffers from
declared width times batch size.

---

## Connection

### D35. No credentials, and no `.env` required

**Context.** Both connections use `Trusted_Connection=yes`.

**Decision.** Settings hold host and database names only. Both hosts are DNS
aliases — `COSMOS` and `PROJECTS` — with defaults, and the database names come
from the manifest.

**Consequences.** Pullmanager reproduces both connection strings with no
configuration at all. `.env` overrides rather than enables, which removes a
setup step and a class of first-run failure. It is still gitignored.

A bug found by asking what the file needed: nothing read it. `Settings.from_env`
read `os.environ` only, so a correctly filled `.env` would have been ignored.

### D36. Hold the Cosmos connection open for the whole session

**Context.** Confirmed in the old code, which returns its connection rather
than closing it.

**Decision.** Same. Every `##JVM_` table dies with it.

**Consequences.** This single fact shapes the epoch, what a resume replays, and
why uploads go through the client.

### D37. Capture `@@SERVERNAME` per connection and never cache it

**Context.** The instance name — `et4003vpdsql032`, not the `COSMOS` alias —
changes on **every** connection, and local `OPENQUERY` needs it.

**Decision.** Capture on open, store on the epoch, and always overwrite.

**Consequences.** Found a live bug: `begin_epoch()` originally left the old
value when none was passed, so a stale name could have aimed `OPENQUERY` at a
server that was no longer ours.

### D38. Upload with parameter binding, not a literal VALUES list

**Context.** The old path built one enormous `INSERT ... VALUES`, and hit
"cannot do more than 1000".

**Decision.** `fast_executemany` with chunking.

**Consequences.** A table value constructor is capped at 1000 rows; parameter
arrays are not, because the statement stays single-row and only its bindings
repeat. Quote escaping and NULL handling stop being the caller's problem.
Chunked, because buffers are allocated from declared width times batch size.

### D39. `cosmos_db` is chosen in the template, and resolved carefully

**Context.** `COSMOS` versus `COSMOS_SneakPeek` versus `Both` belongs in
`cosmos_vars`, not in VM configuration.

**Decision.** Normalize the setting to a real database name for the connection,
and qualify a cohort's tables with *its own* database when it declares one.

**Consequences.** Fixes a bug that would have failed loudly in one form and
silently in another. `Dual` and `sp` are directives, so `Database=Dual` would
simply fail; worse, under `Dual` one session spans two databases while two-part
names resolve against whichever is connected, so the SneakPeek cohorts would
have read COSMOS and written results labelled SneakPeek with no error anywhere.

---

### D40. The split folder is the self-contained handoff unit

**Context.** `file_loc` on an upload cohort is written relative to the
template. `makeYaml` resolved it that way; Pullmanager resolved it relative to
the manifest. The two anchors disagreed, so every upload failed to open on the
far side of the handoff — found by running the whole pathway end to end.

**Decision.** `--export-split` copies upload files into `split/uploads/` and
repoints `file_loc`.

**Consequences.** The split folder is the unit to copy or archive. The pre-YAML
is deliberately *not* made self-contained the same way: it stays close to the
authored template, so moving one means moving its upload files too.

### D41. The bundle carries YAML Manager, not just the runtime

**Amended by D42 and D43:** `template.yaml` ships as `template.yaml.example`,
and `.env.example` no longer ships. The UI and its backend were added.

**Context.** The design has the split step running on the VM. The bundle
shipped only the Pullmanager runtime, so that step had nothing to run.

**Decision.** Bundle `makeYaml.py`, `recipes.yaml`, `datadictionary.yaml`,
`template.yaml` and `.env.example` alongside the runtime.

**Consequences.** One file delivers the whole pathway. Published paths are
chosen so `makeYaml` finds its own data with no flags: it resolves `YAMLs/` as
a sibling of `scripts/`, so the extracted tree reproduces that shape and the
code needs no knowledge that it was bundled.

The bundle grows from 211 KB to 451 KB, most of it the data dictionary. Worth
it: the alternative is a second delivery mechanism for the files without which
the first one cannot be used.

### D42. Re-extraction never destroys VM-side work

**Context.** The bundle now carries the authoring UI, and the user edits YAML
on the VM. Extraction replaces its target directory wholesale, so an update
would have silently deleted that work.

**Decision.** Every bundled file is updated, but a locally modified one is
first set aside as `<name>.local` and reported.

Nothing the user authors is bundled at all. `template.yaml` ships as
`template.yaml.example`, so improvements to it keep flowing without any chance
of landing on a real template. That removed the need for a second "write only
if absent" policy, which had existed only to protect the template.

The extracted tree is swapped rather than merged, so an unbundled file placed
inside it does not survive. Pinned by a test, because it is the thing most
likely to cost someone work.

**Consequences.** Updates stay one-way and recipes and the dictionary stay
authoritative on the Mac, without an edit made on the VM being lost. It also
suits the documented VM patch workflow, where a file is hand-fixed on the VM
and copied back: the fix survives the next update as `.local`.

Anything not bundled is untouched, so user templates and split folders belong
outside the extracted tree.

### D43. Do not ship a `.env` example

**Context.** `.env.example` was bundled alongside the runtime.

**Decision.** Removed.

**Consequences.** Nothing needs configuring — both hosts are DNS aliases with
defaults and the database names come from the manifest — so shipping an
example would only suggest otherwise. The `PULLMANAGER_*` variables still work
for an override.

### D44. The VM gets a desktop launcher over the CLI, not a port of the web UI

**Context.** The VM will not load a page from a Python-served localhost, and
opening a static HTML file is blocked too. It has no Python desktop toolkit
installed except tkinter; the `shiny` and `tcltk` entries in its package list
are R, not Python.

**Decision.** A tkinter launcher for *running* pulls: choose inputs, then
Validate, Export split, Dry run, Execute and Stop, with a live log and a status
table read from the manifest. Authoring stays on the Mac, where the browser
works.

It is a front end, not a second implementation. Every button runs the same
command a person would type, as a subprocess. That keeps database work out of
the UI thread, means a long pull cannot freeze the window, gives Stop something
real to terminate, and guarantees the GUI never behaves differently from the
CLI.

**Consequences.** All logic sits in `launcher.py`, which has no tkinter in it
and is tested against real subprocesses. `gui.py` only wires widgets. It is
tested against a fake tkinter, which checks its own wiring but not Tk itself —
the development machine has no tkinter and no display, so option names and
layout are first exercised on the VM.

Later the development box gained Tk 9 and Xvfb, and the launcher was driven by
hand there. The VM likely has Tk 8.6, so it is still the real test.

Stop is abrupt by design. The node it interrupts stays `running`, which resume
already treats as interrupted and replays, and SQL Server rolls back the open
transaction when the connection drops.

Chosen paths are remembered in the working directory, not the extracted
bundle, which is replaced on every update.

### D45. An unbound table input is an error that suggests, never picks

**Context.** A recipe reads another generated table through a variable
(`##JVM_{{HospitalICDTable}} AS hic`). A template that used the recipe without
binding it failed with a bare "missing variable", which read like a ghost from
an old template. Four options: infer the binding from the columns the recipe
reads (A's suggestions, applied automatically); give recipes default bindings;
require an explicit declaration on the recipe; or bind on the cohort.

**Decision.** Two of them together. The error, now `unbound_table_input`,
names the variable, the alias, the columns read through it, and the tables in
the template whose known columns cover them, and shows the exact line to add.
The binding itself is written on the cohort using the recipe
(`vars: {HospitalICDTable: HospitalICDCodes}`). `PKTable` remains the only
input bound automatically, to the session's root PK.

Rejected: picking the suggested table automatically, and recipe defaults. Both
introduce an assumption at exactly the point where the author should be made to
choose: the recipe should say plainly that it needs a table, and the author
connects it.

**Consequences.** A `dbtable` upload, or a `parquet` one with no declared
columns, used to register as having *no* columns, which would have ruled it out
of every suggestion. It now registers as unknown and is listed separately as a
table that may fit. To be revisited when binding is offered in the UI.

### D46. `--resume-partial` is disabled

**Context.** D24 promised a partial resume that kept completed local transfers.
Reviewing the pathway end to end found that as built it lost them: setup, which
replays because its server-side output is stale, drops and recreates every
destination table, while the completed runs, being settled, were skipped. The
result was a table holding only the batches that had not previously completed.
The test for it checked which nodes were skipped, not what the table held, so
it passed. The guard D24 and D25 described was never built either.

**Decision.** Refuse the flag everywhere: the CLI, the session runner, and the
launcher, which no longer offers it. Full replay is the only resume.

**Consequences.** Correct, and costs time only. A test now checks the outcome
of a full replay (every batch's rows restored), and was confirmed to fail with
the bug reintroduced. What a correct partial resume needs is in the roadmap.

### D47. Three design documents, one job each

**Context.** Design had spread across a YAML Manager design, a contracts
document, a refactor plan, a progress tracker, a testing guide, a completed
folder and a todo file, each partly superseding the others. Corrections were
being recorded as notes in one document about errors in another, and test
counts, phase statuses and open questions had already drifted.

**Decision.** `design.md` for what exists, `decisions.md` for why, `roadmap.md`
for what does not exist yet, and nowhere else. Status lives only in the
roadmap. Old-generator material was first gathered into `old_generator/`,
then deleted (`721ed36`): nothing current depends on it, and git keeps it. Split fixtures moved beside the code that tests against
them. The completed folder was deleted: its build history is in git, and its
one lasting lesson (report each problem once) is in the design.

**Consequences.** A fact has one home, so a change updates one place. The rule
that makes it hold: when code and `design.md` disagree, fix whichever is wrong
in the same commit.

### D48. Paths typed on the command line resolve from the working directory

**Context.** `makeYaml` took `--template` literally, but `yamlmanager`
resolved it against its own install directory, while `--out-dir` in the same
command meant the working directory. On the VM that forced
`--template ..\IBDTest.yaml` for a file sitting beside the user.

**Decision.** Every path typed on the command line (`--template`, `--recipes`,
`--datadictionary`, output paths) resolves from the working directory, like any
command-line tool. Defaults, when nothing is typed, still resolve from the
install, which is how the bundled recipes and dictionary are found.

**Consequences.** A behaviour change for anyone who had learned the old
workaround: `..\IBDTest.yaml` now means the parent of the working directory.
Found alongside two related bugs: `--export-split` ignored `--datadictionary`,
validating against the bundled copy, and a missing default template crashed
with a traceback instead of pointing at `template.yaml.example`.

### D49. Recipes stay on the Mac; the VM receives a transfer YAML with them inlined

**Context.** The VM cannot serve or open a web page, so YAML editing there was
never going to happen in the browser UI, and the launcher (tkinter) only runs
pulls. Shipping `recipes.yaml` to the VM meant two copies to keep in step, and a
recipe edited on one side silently disagreed with the other.

**Decision.** Recipes are maintained in one place, on the Mac. What travels to
the VM is a **transfer YAML**, `<project>_transfer.yaml`, written by
`makeYaml.py --export-transfer`: the template with every recipe reference
resolved and written out in full, cohort recipes and batching recipes alike.

- Multipliers and batching are **not** applied. They stay declared, and the
  split on the VM applies them, by `cosmos_db`, cohort, multiplier and batch,
  into per-session YAMLs and then SQL.
- It is only written if the template passes full validation on the Mac
  (dictionary, table binding, uploads), and it re-validates on the VM, so a
  broken file is caught on both sides.
- It carries a `transfer:` block naming the template it came from and a hash of
  the recipes file, so "which recipes made this" has an answer. No timestamp,
  so the same inputs give the same file.
- The VM side is a straight Pullmanager. A recipes file is needed only if the
  YAML still says `recipe:`, and a YAML that does, with no recipes file, is
  refused with a message pointing at `--export-transfer`.
- Changes on the VM are made by editing the YAML by hand, so **every error
  carries a fix**: which field, and what to change it to. A test enforces that
  no error is raised without one (extends D28, D45).
- `recipes.yaml`, the browser UI and `template.yaml.example` leave the bundle.
  The launcher takes a transfer YAML and has no recipes field.

**Consequences.** The VM needs no `recipes.yaml`, and a transfer YAML is
self-describing: what will run is what is in the file. A recipe fix reaches the
VM only by re-exporting on the Mac. The flag is `--export-transfer`, not
`--transferyaml`, to sit beside `--export-split` and `--export-preyaml`.
Dropping files from the bundle exposed a gap in "updating never destroys work":
re-extraction deleted a dropped file even if it had been edited on the VM. A
previously bundled file that was edited and is no longer shipped is now kept
as `<name>.local`, like a replaced one.

### D50. Temp tables carry a per-project prefix, and a clash adds a number

**Context.** Every Cosmos global temp was `##JVM_<dest>`, after the author's
initials. Global temps are instance-wide, so two pulls running at once that
both produce `Patients` collide, and each opens with
`DROP TABLE IF EXISTS ##JVM_Patients`: pull B drops pull A's table mid-run.
Running several pulls at once is the intended way of working.

**Decision.** The prefix is per project:

- Derived from `project_folder`: the first (up to) three letters of each word,
  lower-cased. `IBD Ancestry` is `ibdanc`, `Test Run` is `tesrun`. Optional
  `temp_prefix:` in the template overrides it.
- Recipes and cohorts write `{{prefix}}_{{Var}}` where they wrote
  `##JVM_{{Var}}`. `{{prefix}}` renders as `##<prefix>`, so the global-temp
  marker stays out of hand-written text. `##JVM_` in a template or recipe is an
  error whose fix names the new form.
- Pullmanager checks, when a session opens, whether any temp it is about to
  create already exists (another pull holds it: a global temp lives only as
  long as the connection that made it). If so it **does not fail**: it adds a
  number (`ibdanc2`, `ibdanc3`, ...) until none exists, and uses that for the
  whole session. These names are only temps, so looking tidy does not matter.

**Consequences.** Replaces an invariant inherited from the old generator
(`naming.py`). Every recipe changes once. Two pulls that derive the same prefix
still work, because the clash check numbers the second. The check depends on
`OBJECT_ID('tempdb..##name')` being visible to our login, which must be
confirmed on the VM. Projects destinations are unaffected: they live in each
project's own database. This does not fix the multiplier bug (roadmap), where
one pull's sessions share temps; that is a split fault.

### D51. A Cosmos refresh is detected from `sys.databases.create_date`, and re-pulls everything

**Context.** Cosmos and SneakPeek are refreshed about monthly; the database
goes down overnight and comes back rebuilt. Work pulled before a refresh cannot
be mixed with work pulled after it, and the question "did Cosmos refresh
between these connections" had been left to the Cosmos developers. On the VM:

```sql
SELECT name AS database_name, create_date
FROM sys.databases
WHERE name LIKE 'Cosmos%'
-- Cosmos            2026-09-17 19:34:56.450
-- Cosmos_SneakPeek  2026-09-17 19:34:59.300
```

The date matches the last refresh, so a refresh recreates the database.

**Decision.** Every session records its Cosmos database's `create_date`, the
full timestamp, when it opens, and the manifest keeps the value per database
(`COSMOS`, `COSMOS_SneakPeek`; a `Dual` pull has both). An `--execute` that
finds a different value from the one recorded says so loudly and re-pulls
everything, finished sessions included. No partial result survives a refresh.

**Consequences.** Answers the questions for the Cosmos developers without
them. Whether `PatientDurableKey` is stable, or whether `IsCurrent`-style flags
move between refreshes, no longer matters, because a refresh always means a
full re-pull. Relies on a refresh recreating the database; confirm by noting the
value either side of the next one. Makes D52 possible: with a refresh ruled
out, work finished earlier is still valid.

### D52. A failed batch is retried alone; `--resume-partial` is removed

**Context.** D46 refused `--resume-partial`, which would have lost completed
batches. Meanwhile any second `--execute` re-pulled everything: each session
opens a new connection, every `done` node looks stale, and setup drops every
destination. So one failed batch cost a whole session, and an overnight job
could run twice. Reading the code for this also found:

- A failed run can leave rows behind: each cohort's transfer commits on its
  own, so a run that fails at its third cohort has landed the first two.
- The Projects-side row count counts the whole destination, so from the second
  batch on it never matches the Cosmos count, and warns falsely.

**Decision.** `--resume-partial` goes. Instead, with Cosmos unrefreshed (D51):

- A session whose runs are all `done` is skipped: its Projects tables are
  complete. `--repull` forces everything to run again.
- A session with work left keeps its destinations (setup creates only missing
  tables), replays its uploads, and rebuilds the Cosmos PK temp from the
  **Projects copy** of the PK rather than re-running the PK query, so the
  population is the one the finished batches came from, by construction.
- Before a run executes, its rows are deleted from each of its destinations.
  To find them, every run destination gets a `_batch` column holding the run's
  batch label. The row-count check counts `WHERE _batch = <label>`.
- Then only the runs not `done` execute (`failed` ones only with
  `--retry-failed`, as before).

Batch membership is deterministic, so the rebuilt batch is the same rows:
values dimensions are predicates combined with `AND`, so their order changes
only the label; chunks are `ORDER BY` the PK's key columns, which are verified
unique. Both read the Projects copy of the PK (D19).

**Consequences.** Run destination tables gain a `_batch` column, which the
parquet export also needs to honour `separate_parquets`. The PK's own
destination does not get one: batches are selected from it. A session whose PK
phase never finished is replayed in full. The test checks the **outcome**:
after a failure and a retry, every batch's rows are present exactly once
(D46's lesson).

### D53. Batch labels are numbered, and chunks run inside their batch

**Context.** Run labels were the bucket values joined (`LA-Female`). Two
dimensions could produce the same label, which was an error, since a lost
combination means patients silently not pulled. Chunks (`row_chunk`) are only
known at run time, and it was undecided whether each becomes its own manifest
node.

**Decision.** A batch label is `b<i>of<n>` then the values:
`b1of4-LA-Female`. The number makes every label unique, so a collision is
impossible and the error goes. It also says how far through a session a run is.
Chunks execute inside their run, one after another, each refilling the PK temp
and appending. Progress is shown as `c<j>of<m>` on the run, not as new nodes. A
failed chunk fails its run, and a retry (D52) redoes the whole run.

**Consequences.** Run IDs, run YAML names and SQL file names change form.
Retry granularity is the batch, not the chunk, so a failure late in a heavily
chunked run redoes the earlier chunks. Correct and simple; revisit if chunked
runs get long enough for that to hurt.

### D54. Every upload lands in Projects, typed, and that copy is the source

**Context.** A CSV upload went to Cosmos only, as text. An uploaded PK
therefore had no Projects copy, although the uniqueness check, batching,
chunking and retries (D52) all read one, so it could not run. Every column was
NVARCHAR, so a list's `PatientDurableKey` joined Cosmos's BIGINT as text,
converting every row. Parquet, which carries types, was refused outright.

**Decision.**

- Parquet is the upload format. Every file upload is read on the VM with
  `pyarrow` (on the VM's package list) and lands in Projects first, as
  `<project_db>.dbo.upload_<dest_table>`, with the file's types. A `dbtable`
  upload is copied server-side into `upload_<dest_table>` the same way. Only
  then does it go up to Cosmos, from that copy, with the same types.
- A CSV is converted to parquet when the split is exported, on whichever
  machine exports it. Columns listed under the upload's `columns:` get the
  declared type; the rest stay text. The same declarations also convert a
  parquet file's columns (R often writes large IDs as doubles). A value that
  does not fit is an error naming the column. A standalone command converts a
  file by hand.
- **The copy is the source.** Once landed, a resume or retry sends the copy to
  Cosmos and never re-reads the file, so a file edited between runs cannot mix
  populations. To pick up a changed file, `--repull`. A copy missing on resume
  is an error pointing at `--repull`.
- An uploaded PK's copy is what its uniqueness check, batches and chunks read,
  exactly as for a generated PK. Batching on an uploaded PK is validated
  against its file's columns and reaches its session. `split_after_build`
  multipliers on an uploaded PK are refused with a clear error for now: a
  given list is usually split before it is uploaded.

**Consequences.** Uploads cost a second trip through the client (file to
Projects, then Projects to Cosmos, since there is no linked server from Cosmos
back to Projects). The `upload_` prefix says where a table came from and keeps
it apart from tables made by hand in the project database. The parquet export
(Artifact Handoff) can copy an upload's parquet rather than download it again.
Splitting or running with a file upload needs `pyarrow`; the Mac's
`python3.13` needs it installed to match the VM (22.0.0).

### D55. Each cohort's rows are committed before the next is pulled

**Context.** The driver runs with autocommit off, so a transfer's own
`COMMIT TRANSACTION` nests inside the driver's transaction, which was
committed once per run: a run pulling three cohorts saved nothing until all
three had landed, and held Projects locks throughout.

**Decision.** Commit after every SQL block, so each cohort's rows (and each
chunk's) are saved before the next is pulled: the bird in hand before the one
in the bush. Nothing runs in parallel within a pull; sessions, batches, chunks
and cohorts run one after another.

**Consequences.** A failure loses at most the cohort in flight. A batch that
fails part-way leaves its earlier cohorts committed, which D52's clear-before-
run already handles on retry. Transactions, and the locks they hold, are as
short as one cohort's transfer.

### D56. The Builder's one write is saving a custom table into `recipes.yaml`

**Context.** A custom table built in the browser UI could only be copied to
the clipboard or downloaded as a recipe file, then pasted into `recipes.yaml`
by hand. Recipes live in one place, on the Mac (D49), so a table worth keeping
belongs there, and a paste can break the file's indentation.

**Decision.** "Save as Recipe" on an added custom table asks the served page to
add it to `recipes.yaml`. It is the page's only write, and it is careful:

- A name already in the file is refused, and the file is untouched.
- The entry is inserted inside the `recipes:` list at that list's own
  indentation, so comments and layout elsewhere are kept.
- The result is parsed before it replaces the file; if it does not read back
  with the new recipe, nothing changes.

A page opened as a file, not served, cannot write, and downloads the recipe
instead. The copy and download buttons are gone.

**Consequences.** The UI now has a `POST /save-recipe` route, on the local
server only. Recipes saved this way reach the VM only through a transfer YAML,
like any other (D49).

### D57. Each project's split and SQL live in `runs/<project>/`

**Context.** The launcher and the command line wrote every split to `split/`
and every dry run to `sql/` in the working directory. Two projects run from
one folder at once share those: exporting B's split replaces A's
`pullmanifest.yaml` while A runs, and A's status is then written into B's plan.
Temp tables were made safe for this in D50; the folders were not.

**Decision.** A split goes to `runs/<project>/split` and a dry run's SQL to
`runs/<project>/sql`, under the working directory:

- `<project>` is the transfer YAML's file name without `.yaml` and without
  `_transfer` (or `_temp`): `IBD_Ancestry_transfer.yaml` runs in
  `runs/IBD_Ancestry/`. The transfer is named from `project_folder`, so that is
  normally the project's name. The file name, not `project_folder` read from
  inside the file, so that two transfer files never share a folder (a copy
  keeping the same `project_folder` included), and a hand edit of
  `project_folder` on the VM does not move a run halfway through.
- Nothing is taken from the working folder's own name (`Project D139081`), and
  `project_db` still comes only from the YAML.
- The launcher's split and SQL fields are blank by default, which means this;
  a typed folder still wins. Settings saved by an older launcher holding
  exactly its old defaults (`split`, `sql`) load as blank, since those were
  never chosen. An existing `split\` and `sql\` are left as they are.
- `makeYaml --export-split` without `--out-dir` uses the same rule, under the
  repository root.

**Consequences.** Two launchers can run two projects from one working folder.
They share `.pullmanager-gui.json`: each window keeps its own choices while
open, and the last one to save is what the next launch restores. Re-exporting
the same project still replaces its manifest, as before. In Projects, two
projects with different `project_db` are separate; two landing the same
`dest_table` in the same database still collide.
