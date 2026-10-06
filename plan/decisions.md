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

**Amended by D93:** the launcher becomes the Run half of one app that also authors.

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

### D58. Dedup names the cohort's own columns; the SQL uses their sources

**Context.** The first dry run on the VM showed dedup rendered inside the query
that defines the cohort's columns, where SQL Server sees only source columns:
`PARTITION BY [BillingCodeValue]` names the alias of `dt.Value`, so every
OtherDiagnoses run would have failed as an invalid column. The PK worked only
because `PatientDurableKey` is also a column of `dxf`. And the ordering every
recipe writes, `dedup_order_by`, was never read (the runtime read `dedup_order`
or `order_by`, which nothing writes), so "the first diagnosis" was any one.

**Decision.**

- `dedup_keys` and `dedup_order_by` name the cohort's output columns
  (`columns[].name`); the SQL uses each one's `source`. An ordering entry may
  end in ` DESC` or ` ASC`.
- `dedup_order_by` is the one spelling. `dedup_order` and `order_by` are
  refused, with a fix naming it.
- YAML Manager checks both on the Mac: a name that is not one of the cohort's
  columns is an error, so it cannot reach the VM.
- Without `dedup_order_by`, which duplicate survives is arbitrary and may
  differ between runs; the note says so (it said "stable", which SQL Server
  does not promise). Ties in the ordering, two diagnoses on one day, are
  arbitrary too.

### D59. A `split_after_build` level filters its own PK; a control is sampled against its case

**Context.** The Race multiplier (black, white) made one session per level, but
nothing applied the level: both PKs selected the same patients, of every race.
`role: control` and `row_mult` were carried along and ignored.

**Decision.**

- Each level's condition is added to its PK's `where`: `column` and `values`
  rendered as `sql_condition` does, through the PK's own source for that
  column (`p.FirstRace LIKE 'Black%'`), or a level's `where` as written. Each
  level still builds its own PK in its own session. A level with neither is
  an error.
- `role: control` with `row_mult: n` makes that level's PK a reproducible
  random sample (D60's ordering) of n times its case, per batch: for each
  batch combination (sex, say), n times the case's rows in that batch. The
  control is matched on the batching columns and nothing else. Its case is
  the other level of the same multiplier, in the same group and Cosmos
  database; a control with `row_mult` needs exactly one such level. A `role`
  other than `control`, or `row_mult` without it, is an error.
- The sample is taken when the control's PK lands in Projects: rows beyond n
  times the case's count are removed from that copy, batch by batch, so the
  Projects PK is the sample and every run is drawn from it. Where there are
  fewer than n times, all are kept, with a warning.
- The manifest puts each case session before its control. A control whose
  case has no PK in Projects fails its PK phase, saying so.
- Under `smallset`, a control's `stop_at_for_pk_table` is multiplied by n, so
  the sample has enough to draw from.

**Consequences.** Matching on more than the batching columns (age, say) is
for later.

### D60. `random_pk_sample` orders the limited PK by a hash of its key

**Context.** The option was offered in the Builder and ignored: `TOP (n)`
returns whichever rows the server reaches first, often clustered by site or
period.

**Decision.** With `smallset` and `random_pk_sample`, the root PK's `TOP (n)`
is ordered by `HASHBYTES('SHA2_256', <key>)`: a pseudo-random sample that is
the same on every run, so a result can be looked at again. The key is the PK's
first `dedup_keys` set, else its `key_column(s)`; with neither, validation
errors. `NEWID()` was rejected because it differs every run. The same ordering
draws a control's sample (D59). Without `smallset` there is no limit, so
nothing to sample.

**Consequences.** The whole candidate population is hashed and sorted before
the limit; acceptable for test runs.

### D61. A non-PK upload lands in Projects once per pull

**Context.** Every session landed every upload from its file into Projects
and loaded it into Cosmos. The IBD Ancestry pull would have landed IBD_Meds
eight times, for no cohort that reads it.

**Decision.**

- The file lands in Projects (`upload_<dest>`) once per pull, by the first
  session that runs its upload phase; the manifest records it, and later
  sessions use that copy. `--repull` or a Cosmos refresh clears the record and
  the file lands again.
- Its Cosmos temp is loaded only in a session whose cohorts read it (their
  SQL names `##<prefix>_<dest>`). A temp lives only as long as its session's
  connection, so each such session still loads its own.
- An uploaded PK is unchanged: every session loads it. Whether it has to,
  batched, is for later.

### D62. Retired test options are removed; Validate says what it checked

**Decision.**

- `stop_at_for_non_pk_tables`, `print_md` and `printout_md` are gone from the
  Builder and the example template. A template that still has one gets a
  warning naming it; design.md said so already, but nothing emitted it.
- `--validate` no longer says "finished YAML ready at" a path: nothing is
  written, and the path was inside the extracted bundle. It says the file is
  valid, how many cohorts, sessions and runs it makes, and what was checked.

### D63. The VM folder: `bundle.py`, a root `pullmanager.py`, transfer YAMLs beside them

**Amended by D106:** `dist/` is not committed, and a build with transfer YAMLs is `bundle_with_yamls.py`.

**Context.** The working folder on the VM held `pullmanager_bundle.py`, the
extracted folder and the transfer YAMLs. Starting the launcher meant
`python pullmanager_runtime\pullmanager.py --gui`: a path into the managed
folder, and a flag for the thing done most often.

**Decision.**

- The bundle is `bundle.py` (`dist/bundle.py` on the Mac).
- `bundle.py --extract <folder>` also writes `pullmanager.py` into the folder
  it extracts beside, the working folder: a few lines that run the extracted
  copy. It is rewritten on every extraction, without checking what is there;
  it is not for editing. The code itself stays in the extracted folder, which
  every update replaces. `--extract` with no folder uses
  `pullmanager_runtime`.
- `pullmanager.py` with no arguments opens the launcher. Every other command
  is unchanged (`--dry-run`, `--execute`, `--tdd`, a manifest to summarize),
  and `--gui` still works.
- Transfer YAMLs sit at the root of the working folder, with their upload
  files at the paths they list, ready to run. A run folder holds only what a
  pull makes (`runs/<project>/split`, `sql`); nothing is copied into it.

**Consequences.** The root holds `bundle.py`, `pullmanager.py`, the transfer
YAMLs and their `data\`, `runs\`, the extracted folder and
`.pullmanager-gui.json`. Queueing several transfer YAMLs is for later
(roadmap).

### D64. One command each side: `makebundle.py` on the Mac, `python bundle.py` on the VM

**Amended by D106:** the bundle is a build product in `dist/`, not committed.

**Context.** Building meant `python3 scripts/bundle_pullmanager.py`; on the VM,
`--verify-bundle` and then `--extract`, two steps to compare one number. And
the content_id covered only the files carried, not the bundle's own verify and
extract code, so a change there kept the same number.

**Decision.**

- `python3 makebundle.py`, at the top of the repo, builds `dist/bundle.py` and
  prints its content_id. It runs `scripts/bundle_pullmanager.py` with the same
  arguments.
- `python bundle.py` alone verifies, shows the content_id, and asks before
  extracting into `pullmanager_runtime` beside itself, writing
  `pullmanager.py` beside that (D63). Only `y` extracts; anything else, or no
  one to answer, extracts nothing. The step-by-step options still work.
- With no folder named, extraction goes beside the bundle, wherever it was run
  from.
- The content_id covers the bundle's own code (the prelude above its
  manifest), recorded as `prelude_sha256`; a bundle whose code was changed
  after building fails verification.

**Consequences.** Every bundle built from now on has a different id from the
ones before, even where the carried files are the same.

### D65. Under `Dual`, every SneakPeek session runs before any Cosmos session

**Context.** `Dual` pulls every cohort from `COSMOS_SneakPeek` and from
`COSMOS`. SneakPeek is the smaller database, so pulling it too is meant to give
a quick first round through every phase before the long one. The split ordered
sessions by D59's rule alone, cases before controls, and otherwise listed each
Cosmos session before its SneakPeek twin. So the first live run (IBD
Ancestry) started on Cosmos.

**Decision.** The manifest runs every `COSMOS_SneakPeek` session first, then
every `COSMOS` session. Within each, cases come before controls (D59), and
otherwise the template's order holds. A control's case is in the control's own
database, so it still runs first.

**Consequences.** A problem in any phase shows up on SneakPeek, before a Cosmos
session starts. It applies from the next split; a manifest already made keeps
its order.

### D66. `--execute` takes a project name; the dry run ends with that command

**Context.** Execute runs from a terminal on the VM (D68), and typing
`runs\IBD_Ancestry\split\pullmanifest.yaml` is slow and easy to get wrong. The
dry run ended with "Nothing was executed and the manifest was not modified",
leaving the next step to memory.

**Decision.**

- `--execute` takes the project's name as well as a manifest path:
  `IBD_Ancestry`, `"IBD Ancestry"` (a space for an underscore) or the transfer
  file's name (`IBD_Ancestry_transfer.yaml`) all mean
  `runs/IBD_Ancestry/split/pullmanifest.yaml` (D57). It looks under the working
  directory, then beside `pullmanager.py`, so it works from another folder
  too. A name with no pull says so and lists the pulls there are.
- `--execute` with nothing after it runs nothing. It lists each pull under
  `runs/`: its state from the manifest (not started, sessions done of the
  total, failed), whether it is executing (D67), and the command that runs
  it. The user picks; it never guesses the newest.
- The dry run ends with its counts and the exact command:

  ```text
  Dry run finished: 48 unit(s), 112 SQL block(s), 0 errors, 3 note(s). Nothing was pulled.
  To pull it: press Execute, or in a terminal in <working folder> run:
      python pullmanager.py --execute IBD_Ancestry
  ```

  It counts notes, not warnings: a dry run produces no warnings, and anything
  wrong stops it as an error.

**Consequences.** Queueing several pulls for one `--execute` is for later
(roadmap).

### D67. A running Execute holds a lock, and the commands and the launcher respect it

**Context.**

- Pulls from two transfer YAMLs cannot overwrite each other. Each has its own
  run folder and manifest (D57), temp prefix (D50) and destinations. The
  exception is two pulls landing the same table in the same `project_db`
  (D57).
- One project can overwrite itself. Export split replaces the manifest a
  running Execute writes to, and a second Execute pulls the same sessions at
  the same time. Nothing stops either, because nothing knows a pull is
  running. `running` in the manifest cannot tell, since a pull that crashed
  or was stopped leaves it too (Status).
- The launcher reads the status only while a command it started is running,
  so a pull started from a terminal shows no progress there.
- On Windows the standard library cannot safely ask whether a process is
  alive: `os.kill(pid, 0)` ends it.

**Decision.**

- `--execute` writes `pullmanifest.lock` beside the manifest. The lock holds
  the process id, the machine, the start time and a heartbeat. A background
  thread rewrites the heartbeat every 30 seconds, so a long query does not
  stop it. The lock is removed when the pull ends. One whose heartbeat is more
  than 2 minutes old is stale: its process stopped without cleaning up, so it
  is ignored and replaced.
- `--execute` refuses to start on a manifest with a live lock, and
  `--export-split` refuses to replace one. Each says who holds the lock, since
  when, and when it would count as stopped. The check is in the commands, so
  it protects a pull started from a terminal as much as one started from the
  launcher.
- The launcher checks the loaded transfer YAML's lock every few seconds,
  whoever started the pull. While it is live:
  - Export split and Execute are greyed out. Pressing Execute greys them at
    once.
  - The status tab says "Executing since 14:03, last heartbeat 20s ago" and
    keeps refreshing.
  - Validate and Dry run stay available. They write nothing a pull reads.
- `python pullmanager.py --running` lists every pull under `runs/` and whether
  it is executing. It is the same list `--execute` alone prints (D66).

**Consequences.** A pull killed without cleanup (its window closed, the
machine restarted) holds its project for up to 2 minutes. The lock says only
whether a process is alive; the manifest still says what it has done.

### D68. Execute from the launcher runs in its own console window, and writes a log

**Context.** On the VM, Execute started from the launcher ended at once with
exit code 3221225794 (`0xC0000142`: a DLL failed to initialize). It stopped
before printing its first line, so before any of Pullmanager's own work.
Validate, Export split and Dry run, started the same way, work. The same
command typed into VSCodium's terminal runs. The launcher starts `python.exe`
directly, with its output piped into the window. The cause is unknown. Some
programs are blocked on the VM (design.md, Where It Runs), so a fix cannot
count on starting `cmd.exe` or `powershell.exe`.

**Decision.**

- The launcher's Execute opens a new console window (`CREATE_NEW_CONSOLE`) in
  the working folder. It runs `python pullmanager.py --execute <project>`
  with the same Python and no shell in between, the situation that works from
  a terminal. The window's title and first line name the pull.
- When the pull ends, the console stays open, saying
  `Safe to close: the pull has finished (exit code 0). Type exit and press Enter to close this window.`
  Only `exit` closes it, so an Enter pressed by accident does not lose the
  output. The lock (D67) is released when the pull ends, not when the window
  closes.
- Execute also writes everything it prints to
  `runs/<project>/logs/execute-<date>-<time>.log`, however it was started.
  The launcher's Output tab follows the newest log of the loaded pull while
  its lock is live, so a pull started from a terminal shows there too.
- If the console cannot be started, or its process ends before writing its
  log, the launcher says so, with the exit code, and prints the terminal
  command to run instead (D66).
- Stop ends a pull the launcher started. A pull started from a terminal is
  stopped there, with Ctrl+C.

**Consequences.** A pull's output shows in its console and in the Output tab,
and the log keeps it after both are closed. Validate, Export split and Dry run
still run inside the launcher.

### D69. The PK's key is one rule everywhere

**Context.** The PK's key was read two ways. The hash sample and the control
sample (D59, D60) take its first `dedup_keys` set, else its `key_column(s)`.
The uniqueness check after the PK phase, chunk ordering and batch selection
read only `key_column(s)`. The IBD Ancestry PK names its key only through
`dedup_keys: [[PatientDurableKey]]`. So in the first live run every session
warned "PK declares no key_column" and its uniqueness was never checked, and a
`chunk:` on that PK would have failed its run ("Row chunking needs the PK key
columns to order by").

**Decision.** The PK's key is its first `dedup_keys` set, else its
`key_column(s)`, wherever it is used: the sample, the uniqueness check, chunk
ordering and batch selection. One function gives it. A PK with neither still
warns, naming both ways to declare a key.

**Consequences.** A PK deduplicated on its key always passes the check, since
the dedup makes it unique; the check still catches a `key_column` that is not
unique.

### D70. The pull's readout: a plain-text log, and widths once per session as notes

**Context.** The user wants to see what Execute prints (warnings about the PK,
column widths, the instance) in the launcher, not only in a terminal. It went
only to the terminal, after each session. The column widths were printed as
warnings, once per batch: in the first live run, OtherDiagnoses' nine columns
appeared twice in one session, for Male and for Female. They are information,
since widths are measured and never applied (D34), and the real warnings among
them were easy to miss.

**Decision.**

- The log Execute writes (D68) is plain text, the same lines as the terminal.
  Markdown was considered; the launcher cannot render it, and plain text reads
  the same in both places.
- At the end of each session its warnings come first, then one table of column
  widths: each destination's string columns, with the declared type and the
  widest value across all of the session's batches. These are notes, not
  warnings.
- Warnings are kept for what needs a look: Cosmos and Projects counts that
  disagree, a count past the threshold, a batch matching no PK rows, too few
  controls, a PK with no key.

### D71. The launcher's names: Validation Output, Pull Log, Preview SQL; the preview ends in one statement

**Context.** The launcher had one Output tab, for Validate, Export split and
Dry run, and D68 would have put Execute's log there too. "Dry run" did not say
that it writes the SQL, nor that Execute does not need it: Execute renders the
same SQL itself as it goes. The user took it to be a required step.

**Decision.**

- The Output tab is **Validation Output**, for Validate, Export split and
  Preview SQL. A new **Pull Log** tab follows Execute's log, in place of the
  Output tab D68 named.
- The Dry run button is **Preview SQL**. The terminal option stays
  `--dry-run`.
- The preview ends with one statement holding everything needed next, in
  place of D66's closing lines:

  ```text
  Preview finished: 48 unit(s), 112 SQL block(s), 0 errors, 3 note(s). Nothing was pulled.
  SQL written to runs\IBD_Ancestry\sql for reading; Execute does not need it.
  To pull it: press Execute, or in a terminal in <working folder> run:
      python pullmanager.py --execute IBD_Ancestry
  ```

  With no SQL folder given (a terminal `--dry-run` without `--out-dir`), the
  second line says no SQL was written and how to write it.

### D72. `--artifacts` packages a pull's finished tables as parquets

**Context.** The first live pull finished, and its tables sit in Projects. The
user wants them as files in the project, for R and Python on the VM: the
parquets cannot leave it.

**Decision.**

- `python pullmanager.py --artifacts IBD_Ancestry` (a project's name, as
  `--execute` takes, D66), and an **Artifacts** button in the launcher.
  "Package" and "extract" were rejected as too close to other commands.
- It writes `runs/<project>/parquets/`, with `SneakPeek/`, `Cosmos/` and
  `uploads/` inside. Files keep the table's name, so SneakPeek's keep `_sp`
  and nothing is confused when all are loaded together.
- **Only finished tables.** A PK table once its PK phase is done; a run's
  table once every run that fills it is done. Anything else is listed as not
  packaged, with why. The manifest decides, never what exists in Projects.
- A table is read from Projects in chunks and written with pyarrow, typed
  from the table's own column types. `_batch` is dropped: it is internal.
- A batching dimension with `separate_parquets: true` writes one file per
  value (`OtherDiagnoses_LA.parquet`), chosen by `_batch` and the manifest's
  batch list; the split now records the flag on each batch dimension.
  Dimensions without it stay combined.
- Uploads are copied from the split's parquet, not read back from Projects.
- Each run replaces what the last wrote; there is no use for older copies.
- It refuses while the pull is executing (D67).

### D73. `contents.md` describes every packaged table, for people and for the VM's AI

**Decision.** `runs/<project>/contents.md` holds:

- **A pull summary:** the project, the Cosmos database, when it was pulled,
  the Cosmos refresh date, whether it was a test sample (`smallset`, and its
  limit), and each table's row count.
- **Per table:** granularity, "specific to", description, then its columns.
  - Granularity is the table's own `granularity`; else "One row per" its
    `dedup_keys`; else "No granularity given".
  - "Specific to" is written from the table's multiplier levels and variables
    as the SQL says them (`p.FirstRace LIKE 'White%'`, `ICD_Value K50.%`,
    sampled at 4 times its case per batch), so it also shows how the rows were
    chosen.
  - Each column is its name, its SQL type as the table holds it, and its type
    once loaded in each language: `PatientDurableKey: BIGINT (py: int64,
    r: integer64): description`. The types are for writing code against the
    files, including by the VM's AI, so they are the programmatic ones.
  - A column's description is its own `description`; else the data
    dictionary's for its source column (`p.Sex` is `PatientDim.Sex`, through
    the table's `from` and `join` aliases); else "No description". Nothing is
    guessed, and borrowed text is not marked as borrowed.
  - **Keys and joins (testing; may be removed):** the table's key, and the
    other tables sharing each of its key columns.
- Separated batches are tables of their own; uploads are listed too.
- Descriptions are read from the split, so a changed description reaches
  `contents.md` through a new split.

### D74. Recipes carry `granularity` per table and `description` per column

**Decision.** A cohort (recipe or custom table) may have `granularity:`, and
each of its columns `description:`, beside the `description:` a cohort
already has. They travel through the transfer YAML and the split unchanged,
and nothing but `contents.md` reads them. The Builder's custom-table editor
has fields for all three. The user writes them for recipes; blanks fall back
as D73 says.

### D75. `load_parquets` and `examine_parquets`, in R and in Python

**Decision.** At the root of `runs/<project>/`, beside `contents.md`:

- `load_parquets.R` and `load_parquets.py` open every parquet without reading
  it into memory (`arrow::open_dataset()`, `pyarrow.dataset`), named by
  table, for filtering before anything is loaded.
- `examine_parquets.R` and `examine_parquets.py` read every table into memory
  (R data frames; pandas with Arrow types, so a nullable integer stays an
  integer), for browsing small tables.
- R keeps 64-bit integers as `integer64` (`arrow.int64_downcast = FALSE`), so
  a key's type is the same in every table and joins match.
- `HOW_TO.md` says which to use when. The client keeps whichever language it
  wants and deletes the rest. All use only what the VM has: R `arrow`, Python
  `pyarrow` and `pandas`.

### D76. `include_other` is retired: listed batch values always get a batch of the rest

**Context.** `include_other: true` added a catch-all batch for values a batch
did not list. Turned off, it did not lump those rows together: it left them
out of the pull entirely. The user read it as lumping, and a setting whose
misreading silently loses data is dangerous. It was briefly made to default
to on, then removed.

**Decision.** Listing values always adds a catch-all batch (`sex-other`), so
the batches together are always the whole PK and batching never drops a row.
`include_other` in a template or batching recipe warns (`retired_option`) and
is ignored. The Builder has no toggle for it.

**Consequences.** `sex: [Female, Male]` is three batches, and batches
cross-multiply with their catch-alls: `state [LA, MS] × sex` is nine. A pull
that really wants only some values filters its PK instead.

### D77. Batches stay in one parquet per table unless `separate_parquets` is set

**Context.** The batching recipes set `separate_parquets: true`, so every
batched pull packaged each table as a file per value. Separating is the
unusual case.

**Decision.** No batching recipe sets it; it is off unless a template turns it
on for a batch. The Builder's checkbox is off by default and shows what the
split will do, reading a recipe's own setting, and unticking a recipe's `true`
writes `separate_parquets: false`.

### D78. A table takes its PK's variables when it does not set them

**Context.** IndexDiagnosis filters on the same `ICD_Value` as the PK that
chose its patients. Without a multiplier setting it, the variable had to be
written on both, and the Builder offered no field for either: a template
failed with `missing_variable` and no way to fill it in.

**Decision.**

- A table's variables are, last winning: the template's `vars`, the
  uploads, the automatic ones, its PK's own `vars` (the PK of its multiplier
  group), then its own `vars`. So a table that does not set a variable takes
  its PK's; one it sets itself wins. `PKTable` is not taken from the PK.
- The Builder gives each recipe row an input per variable its SQL uses that
  the template does not already supply, saying where a blank one comes from
  ("set by multiplier IBDType", "from the PK: K50.%") and marking one nothing
  supplies.
- Rejected: joining IndexDiagnosis to the PK's own event (`DiagnosisEventKey`,
  or the PK's code). It would return only the index event the PK already
  holds, where IndexDiagnosis is each code's first date in the disease
  family.

**Consequences.** How a recipe should declare that a variable is inherited or
optional, rather than the rule applying to every variable, is open (roadmap).

### D79. Transfer YAMLs live at the repository root, and the bundle can carry them

**Amended by D106 and D112:** transfer YAMLs travel in `bundle_with_yamls.py`; `yamlmgr.py` is gone, `datascope.py` opens the app.

**Decision.**

- `--export-transfer` writes `<project>_transfer.yaml` at the repository
  root (in an extracted bundle, the working folder beside it, never inside
  it), with its upload files copied under it at the same relative paths.
- `python3 makebundle.py yaml=IBD_Ancestry,Celiac` carries those root
  transfer YAMLs in the bundle. A name may be the project, the project with
  `.yaml`, or the full file name, in any case; the space in `yaml=A, B.yaml`
  is allowed; a name with no file is refused, listing those there are. They
  are verified like every file and count in the `content_id`.
  `python bundle.py` writes each beside `pullmanager.py`, not into the
  extracted tree, keeping a different copy already there as `<name>.local`.
  Upload files are not carried; the build names each one to copy.
- `python3 yamlmgr.py` at the root opens YAML Manager.

**Consequences.** The committed `dist/bundle.py` is built without transfer
YAMLs; a build with `yaml=` overwrites it locally until the next plain build.

### D80. A batch names its column `required_column`

**Context.** A batch's `column` is the PK column it splits on, and the PK must
have it; the name did not say so.

**Decision.** Batching recipes and definitions write `required_column`. A PK
without it is an error naming the PK's columns (`missing_batch_column`, as
before). The old `column` is refused (`batching_column_renamed`) with the
exact replacement, not accepted and guessed at. Inside the split and in the
manifest it stays `column`, so manifests and the runtime are unchanged.
Multiplier levels keep `column`.

### D81. `contents.md` drops the key line; the Builder shows exports, not a draft

**Decision.**

- The "Key (testing)" line D73 tried is removed: nearly every table shares
  `PatientDurableKey`, so it listed most of the pull for each table.
- The Builder's Draft YAML section is removed; it did nothing a user needed.
  In its place, Exports shows the saved template's pre-YAML, transfer YAML and
  manifest, as the Exports tab does.

### D82. A batch that lists no values batches by every value the PK has

**Context.** `values: all` was warned about and refused when a pull reached
it: the values are not known until the PK exists, and resolving them then
would change the manifest's runs after planning. The user wants a batch with
no values, `state` say, to batch by every state there is.

**Decision.**

- A `column_values` batch with no `values` (or `values: all`) is resolved
  inside its run, as chunks are (D53), so the manifest's runs do not change:
  the run selects the column's distinct values among its own PK rows in the
  Projects copy, NULL included, and pulls each in turn under its own label.
- A failure fails the run; a retry redoes all its values. Progress and the
  number found are recorded on the run.
- Separate parquets on such a batch is refused (`separate_values_all`): the
  tables other than the PK do not carry the column, so their rows cannot be
  split by it.

**Consequences.** Every value is one pass through the run's cohorts, so fifty
states is fifty passes: slower than listing a few, and a failure late in the
list redoes the early ones.

### D83. Inherited variables stay a rule for all; the Builder defaults the project

**Context.** The roadmap asked whether a recipe should declare which
variables it inherits (D78). The user kept the rule as it is: the Builder
already says where a value comes from and lets it be overridden.

**Decision.**

- No declaration. A table that sets no value takes its PK's, for every
  variable. The Builder no longer marks such a table red before the PK has
  a value; it says the value will come from the PK. Only the PK, or a table
  with no PK, is marked.
- The Builder fills in `PROJECTD93A5E7`, `19900101` and `20260601` where a
  template has no project database or dates, so a mistyped or missing one is
  less often the error. makeYaml has no such default: a template written by
  hand still says what it means.

### D84. A saved template keeps its empty lists; a multiplier needs levels; a failed refresh keeps the page

**Context.** Save & Refresh crashed with `'NoneType' object is not iterable`
and replaced the page with one that knew neither the template nor the error's
place. The cause: the UI's YAML writer (`dump_yaml_text`) wrote `[]` and `{}`
as blanks, which read back as null, so a multiplier added with no levels yet
saved as `levels:` and `expand_multipliers` failed on it. The browser's own
writer had been fixed for this; the Python one had not.

**Decision.**

- The writer writes an empty list as `[]` and an empty mapping as `{}`.
- A multiplier with no levels is an error, `multiplier_without_levels`,
  naming it, with the fix. Even read correctly it would have made no cohorts,
  silently.
- A template that fails to compile or render still opens the dashboard: the
  Builder holds the saved draft, and the failure is shown as an error saying
  where it happened, not replaced by a page that starts over.

### D85. Temps live in `YAMLs/temp/`, named for the project, and never overwrite another file

**Amended by D95:** the file is `<project>_intake.yaml`.

**Context.** New Blank Template swapped the Builder's draft but left the old
path in the header, and saving from a `_temp.yaml` overwrote that file, so a
blank draft could replace a real one.

**Decision.**

- Save & Refresh writes `YAMLs/temp/<project_folder>_temp.yaml`, whichever
  template the page opened with.
- New Blank shows `YAMLs/temp/_temp.yaml` in the header, and the name follows
  the Project Folder as it is typed.
- A save never replaces a different file: a target that exists and is not the
  file the page opened is refused, saying to load it or pick another name.
  Changing a loaded temp's folder saves a new file and leaves the old one.
- Upload files for temps live in `YAMLs/temp/csv/`, so `file_loc` stays
  `csv/<file>` relative to the temp, and the transfer export still copies it.
- The existing temps move there; `YAMLs/Celiac_temp.yaml` is the current
  Celiac.

**Consequences.** Replaces the rule that a draft is saved beside the template
it came from, which kept relative upload paths working; keeping uploads under
`YAMLs/temp/csv/` does that now.

### D86. A missing `cosmos_db` means `Dual`; the Builder's defaults come from `template.yaml`

**Decision.**

- `cosmos_db` left out of a template means `Dual`, in makeYaml as in the
  Builder, since both databases is the usual pull. This is the one default
  makeYaml supplies; the project database and dates are still the author's to
  write (D83).
- A new template in the Builder takes `cosmos_vars`, `run_vars` and
  `test_options` from `YAMLs/template.yaml`, so that file is the one place to
  edit the defaults. The built-in fallback is `PROJECTD93A5E7`, `19900101`,
  `20260601`, `Dual`, and small set off.
- Uploads default to parquet.

### D87. The PK is written to parquet as soon as it lands; batching is unchanged

**Context.** The user read batching as rebuilding a small PK per batch. It
does not: the PK is built once and copied to Projects, and each batch is
selected from that copy and uploaded to the Cosmos temp (D18, D19). What
repeats per batch is the fact-table queries, whose cost per pass is
unmeasured. The user's proposal, the whole PK landed first and batches drawn
from it, is what is built.

**Decision.**

- Batching stays as it is. Its cost is measured on the VM before anything
  changes (roadmap).
- Once the PK phase has landed and checked the PK, Execute writes it to
  `runs/<project>/parquets/<database>/<PK>.parquet`, where Artifacts would, so
  the whole PK is in hand before any run starts. A failure to write it warns
  and the pull continues. Artifacts later replaces it with everything else.

### D88. Artifacts reports each file as it goes, and carries on past a failed table

**Decision.** Artifacts says when it starts each file, then its rows and
seconds; a table that fails is recorded with its error and the rest are still
packaged. It ends with every file it wrote (rows, size), the errors, and the
total time, and exits non-zero if any table failed.

### D89. `viewparquets.py` replaces `examine_parquets`; `HOW_TO.md` is copied from an editable stock file

**Context.** D75 wrote `examine_parquets.R/.py` to read every table into
memory. The user wrote `viewparquets.py`, a tkinter window that opens
parquets, which suits the client better.

**Decision.**

- Artifacts no longer writes `examine_parquets.R` or `.py`. It copies
  `viewparquets.py` into the run folder. `load_parquets.R` and `.py` stay.
- `HOW_TO.md` is copied from `scripts/pullmanager_src/stock/HOW_TO.md`, which
  the user edits; `{project}` and `{parquets}` in it are filled in.
  `viewparquets.py` lives beside it. Both travel in the bundle.

**Consequences.** Supersedes D75's examine scripts.

### D90. Upload files are not carried in the bundle

**Context.** A CSV upload could have been ticked "add to bundle" and carried
in `bundle.py`. The bundle takes UTF-8 text with Unix line endings, and CSVs
from Excel are often neither, and a parquet cannot travel as text at all.

**Decision.** Not built. Upload files are copied to the VM by hand; the build
already names each one and where it goes.

### D91. Save & Refresh queues a temp for the bundle; `makebundle.py queue` carries the queue

**Amended by D122:** the queue is emptied after each bundle, and `makebundle.py` always carries it.

**Amended by D95:** the queue lists intakes.

**Decision.**

- Saving a temp adds it, once, to `YAMLs/temp/bundle_queue.txt`.
- Builder → Exports lists the queue, each with Remove; choosing one shows its
  pre-YAML, transfer YAML and manifest. A dropdown adds a temp from
  `YAMLs/temp/` that is not queued, such as an old pull a client wants
  refreshed.
- `python3 makebundle.py queue` exports each queued temp's transfer YAML to
  the repository root and carries them all, as `yaml=` does (D79). A queued
  temp that fails validation stops the build, naming it. The export is
  labelled "Transfer YAML (to be bundled with bundle.py)".
- Plain `makebundle.py` is unchanged, so the committed bundle still carries no
  transfer YAMLs (D79).

---

## The App

### D92. The Builder's logic moves into a Python model; every UI is a view of it

**Retired by D112:** the browser UI is removed; the app is the one UI.

**Context.** The browser UI is not decoupled from the engine. Python compiles
and draws read-only panels, but the page's JavaScript (about 1,500 lines) holds
the draft and every edit to it: the PK rules, where a variable comes from (a
second copy of D78's rules), type matching against the dictionary, the custom
table builder and a YAML writer of its own. It is checked only under node. The
user wants several UIs over time (tkinter now; a web UI, React or a native app
later) and a web UI redesigned only once it is easy to change.

**Decision.**

- A model module beside makeYaml, with no tkinter and no HTML in it, holds
  the draft and every edit as functions. It reads the defaults, recipes and
  dictionary, validates in-process through makeYaml, and answers what a view
  asks: where a variable comes from, which tables fit a binding and which of
  their columns match, a PK's columns, and each message's kind and the field
  it points to. It is tested with `unittest`. It takes over from
  `yamlmanager_backend.py` as the one thing a UI imports.
- A view only wires widgets to it, as `gui.py` does to `launcher.py` (D44).
- The browser UI is frozen from now: no fixes, and not moved onto the model.
  It is retired once the tkinter app satisfies the user; a later web UI is
  designed on the model.

**Consequences.** Standard library and the VM's packages only, since the
model travels with makeYaml (D93). Rules that exist twice today, in makeYaml
and in the page, exist once.

### D93. One tkinter app, on the Mac and the VM: Author and Run

**Amends D44**, which kept authoring on the Mac because the VM cannot open
the browser UI.

**Context.** The user likes tkinter's file dialogs, fields and buttons, wants
one app for opening a transfer YAML, adjusting it and running it, and wants
the Mac to look and behave exactly as the VM does, for testing.

**Decision.**

- One tkinter window with two halves. **Author**: the Builder (D96),
  Validate, Exports and YAML. **Run**: today's launcher (Validation Output,
  Pull Log, Status), whose logic stays in `launcher.py`.
- The same app on both sides, needing nothing installed: the standard
  library, tkinter, and packages on the VM's list. Being VM-capable is a
  design constraint; whether and when it ships is the user's choice.
- On the Mac, Run works as on the VM up to the database connection. Execute
  cannot connect there until a test server exists (roadmap).
- Tested like the launcher: the model with `unittest`, the view against the
  fake tkinter, and by hand with the Mac's Tk 8.6. Dark mode comes later.

### D94. The flow is the same on both sides: intake, transfer, Pullmanager

**Upholds D49.** Recipes stay on the Mac.

**Context.** The user does not want the core of the program left on the VM
where it could be copied. Every VM session is reached from the Mac, so
authoring happens there. On the VM the user makes small changes to a mostly
made YAML (names, a value) before running it.

**Decision.**

- On both sides, Author saves an intake (D95) and exports its transfer YAML,
  which Run takes. There is no VM-only path.
- On the VM the file opened is a transfer YAML. It names no recipes, so it
  needs no recipes file, and exporting it again gives the same file with its
  provenance kept (checked: byte-identical, `transfer:` block included).
- Where there is no recipes file, Prefabricated lists nothing and Save as
  Recipe is unavailable, each saying why. That follows from the file being
  absent, not from which machine it is.

### D95. A draft is an intake: `YAMLs/temp/<project>_intake.yaml`

**Amends D85 and D91**: the file's suffix, not its folder.

**Decision.**

- Save writes `YAMLs/temp/<project_folder>_intake.yaml`. New Blank shows
  `YAMLs/temp/_intake.yaml`. The folder keeps its name, and D85's
  never-overwrite rule stands.
- The existing `_temp.yaml` files and the bundle queue's entries are renamed.
- `_intake` joins the suffixes a run folder's name drops, in makeYaml and in
  `pulls.py` alike. `_temp` stays in that list, so older files name the same
  run folder.
- `project_folder` is shown as **Project name**. Its dropdown lists the
  intakes in `YAMLs/temp/` and the transfer YAMLs in the working folder, with
  Browse and New beside it.

### D96. The Builder's layout: the PK first, and splitting made explicit

**Context.** From the user's redesign. It maps onto the template's existing
keys, so the core logic is unchanged. The difference between a
`split_after_build` multiplier and a batch was obscure enough that the user
had not seen it.

**Decision.** Sections, in order:

- **Project.** Project name (D95). "Pull from" toggles Cosmos and
  Cosmos_SneakPeek, both on by default: both means `Dual`, one means that
  database, neither is an error. Project DB, defaulting to `PROJECTD93A5E7`
  (D86). The dates. "Collect all patients matching criteria", on by default:
  on is `smallset: false`; off enables a sample size (`stop_at_for_pk_table`)
  and "Random sample" (`random_pk_sample`).
- **PK Table.** Exactly one source: a prefabricated recipe, a table built
  from the dictionary, a parquet, a CSV, or a `dbtable`. It replaces the PK
  checkbox spread across uploads and custom tables.
- **Supporting Tables** (formerly Uploads): every upload except the PK. Each
  shows its columns, which can be renamed and dropped (D98).
- **Multipliers**: `during_build` only, explained as "each level becomes its
  own set of tables".
- **Splitters**: one section in which each splitter states its kind.
  "**Separate tables**" is a `split_after_build` multiplier: each level gets
  its own PK and session, with an optional control and row mult (D59).
  "**Pieces of one table**" is batching: a chunk, or a PK column with values,
  and Separate parquets. The column is picked from the PK's known columns.
  Before a PK is chosen, the column controls are greyed and say "Choose a PK
  Table first"; chunk still works. The YAML is unchanged: they are written as
  today's `multipliers` and `batching`.
- **Fact Tables** (formerly Cohorts): prefabricated recipes, and tables built
  from the dictionary with the same builder the PK uses, columns ordered by
  number. A variable left unset is labelled with its source, "from the PK
  (K50.%)" or "set by multiplier IBDType", as a label only. A table input is
  bound with a picker listing the tables that fit first, with a ✓ or ✗ for
  each column it needs. It never picks, even when only one fits (D45 stands;
  the user will try it).
- **Validate** merges Validation and Pipeline. Errors are red, warnings
  yellow, pending transfers blue (D97), passes green. Clicking a message goes
  to its field. It runs a moment after each edit; Save stays explicit, and
  the title shows unsaved changes.
- **Exports** (the queue, D91, and each intake's three exports) and **YAML**
  come across. The Cohorts cards, Graph and Recipes tabs follow later.
- Looking codes up (ICD, CPT) through an outside service is dropped.

### D97. `pending_transfer: true` marks a file that will exist only on the VM

**Decision.**

- A PK or supporting table read from a file may say `pending_transfer:
  true`. On the Mac, its missing file is reported as pending (blue), not as a
  warning. A missing file not so marked stays a warning, so a real mistake
  stays loud.
- It changes nothing at the split: a file the split needs and cannot find is
  an error there, as now.
- A table whose columns cannot be read (a `dbtable`, or a file not here yet)
  takes its column names, and optionally types, typed in under `columns:`, so
  the Splitters dropdown and binding checks work. Without them its column
  controls stay empty, with a warning saying why.

### D98. A supporting table's columns can be renamed and dropped

**Decision.**

- An entry under an upload's `columns:` may add `from:`, the file's column
  name, making `name` the table's column. `drop: true` leaves a column out.
  Columns not listed are kept as they are.
- The change is made as the file lands in Projects, so the Projects copy,
  its Cosmos temp, validation and every recipe bound to it see the new names.
- Validation refuses a `from:` the file lacks (when the file can be read),
  two columns ending with one name, and dropping a column the table's key or
  a binding needs, each with its fix.

### D99. Pending is a third kind of message; no database is `none`

**Context.** Building D96 and D97 needed two small choices the decisions did
not make: how makeYaml says "pending", and what "Pull from" with neither
database writes.

**Decision.**

- makeYaml's result has a third list beside errors and warnings, `pending`
  (printed `PEND`), for a file marked `pending_transfer` that is not here.
  It is not a warning, so a draft whose only notes are pending files is clean.
- With both "Pull from" toggles off, the app writes `cosmos_db: none`. Left
  blank it would read as `Dual` (D86) and pull both, silently. makeYaml
  refuses `none` as it refuses any other unknown value, and the app says "no
  database is chosen" in place of that message.

### D100. A dbtable's columns are not renamed; keys use the names a table lands with

**Context.** D98's renames happen as a file lands in Projects. A `dbtable` is
copied inside the database with `SELECT *`, where nothing on the Mac or in
Pullmanager sees its columns.

**Decision.**

- `from:` or `drop:` on a `dbtable` is an error (`upload_rename_on_dbtable`)
  whose fix is to rename in the table itself, or upload it as a parquet.
- `key_columns`, bindings and batching name columns as they land. A key named
  by its old name is `renamed_key_column`; a dropped key is
  `dropped_key_column`. Both errors, with the fix.
- A declared type still names the file's column, since the CSV is converted
  under its own names at the split and renamed only as it lands.

### D101. The Author view is tested on a real Tk, withdrawn

**Context.** The launcher is tested against a fake tkinter (D44), which checks
wiring and nothing of Tk. The Author view has far more widgets, and the Mac's
`python3.13` has Tk 8.6, likely the VM's.

**Decision.** `yamlmanager_tk.py --tdd` builds the view on a real Tk with its
window withdrawn, types into its fields and reads the model back. Without a
display the tests skip. One Tk serves every test: on the Mac a second Tk in
one process can hang. The app's wiring (Author's transfer reaching Run, Run
opening without Author) is tested with the launcher's fake tkinter.

**Consequences.** Layout and look are still only checked by eye. Building the
view this way found two hangs no fake would have: a re-render destroying a
combobox inside its own event (re-renders now wait until Tk is idle), and the
second Tk.

### D102. The committed bundle is built from the commit; `utils/` carries your utilities

**Amended by D106:** `dist/` is not committed, so a bundle is built from the working tree; the `utils/` rule stands.

**Context.** A bundle built from the working tree swept in a file that was
not yet committed, so the committed bundle could not be rebuilt from the
repository. The file, `utils/transcription_viewer.py`, was meant to ship:
everything under `scripts/pullmanager_src/` is bundled.

**Decision.**

- The committed `dist/bundle.py` is built from a clean checkout of the
  commit it goes with, so what it carries is exactly what is committed.
- `scripts/pullmanager_src/utils/` is where your own VM utilities go, and it
  ships in every bundle. A file there that is not committed is committed
  (after asking), never left out.

### D103. An upload path always reaches the same file; a string always reads back as itself

**Amends the transfer export's rule that `file_loc` is never rewritten
(Outputs, in design.md) for paths that leave the template's folder.**

**Context.** On the VM, a transfer YAML opened at the root and saved as an
intake in `YAMLs/temp/` stopped the pull twice over. Its uploads, beside the
transfer, were written relative to the root; the save kept any path needing
`..` as written, so `IBD_meds.parquet` was then looked for in `YAMLs/temp/`.
And a CSV header with quotes in it, `'DiagnosisCode'`, renamed in the app,
was written plain by makeYaml's own YAML writer and read back without its
quotes, so the split found no such column. Author's Validate, checking the
draft in memory, said both were fine.

**Decision.**

- Saving an intake rewrites every relative `file_loc` to reach the same file
  from `YAMLs/temp/`, with `..` where it must (`../../IBD_meds.parquet`).
- The transfer export, written to another folder, rewrites a `file_loc` that
  leaves the template's folder to reach the same file from the transfer's
  folder (`IBD_meds.parquet` again, at the root). Kept as written it would
  point somewhere else. One inside the template's folder is still kept and
  its file copied beside the transfer; one still outside, or absolute, is
  not copied, with the warning `upload_not_copied`.
- makeYaml's writer writes a string plain only when it reads back as the
  same string, and quotes it otherwise: quotes, `true`, `null`, numbers,
  leading or trailing spaces, and YAML's special first characters.
- The launcher's remembered paths move from the working folder's top to
  `runs/.pullmanager-gui.json`, where everything else the app makes lives; a
  file at the old place is moved there when it is first read.

**Consequences.** A transfer written elsewhere may now differ from the
template in an upload's `file_loc`, where before it could differ from the
file it meant.

### D104. Upload paths are written relative, never hardcoded

**Context.** Browse wrote a full path (`\\epic-nas\...\IBD_meds.parquet`)
when the file was not under the draft's folder. Upload files mostly stay on
the VM, beside the transfer YAML, so a path should say where the file is
relative to the YAML, not on which machine.

**Decision.** Browse writes the chosen file relative to the draft's folder,
with `..` where it must (`../../IBD_meds.parquet` from `YAMLs/temp/`), as
save and the transfer export keep it (D103). A full path is written only
where no relative one exists (another drive), or where you type one.

### D105. Extra lines on any table: `add_where` and `add_join`; where lines by column

**Amended by D119:** Value mode becomes an operator dropdown, with BETWEEN.

**Context.** A prefabricated table cannot take a filter of its own: a
template's `filter.where` on a recipe cohort replaces the recipe's whole
list. The user needs, for one, medication tables limited to an uploaded list
of medication codes.

**Decision.**

- Beside `where:` and `join:`, a table's `filter:` may list `add_where:` and
  `add_join:`, lines of the same kind, appended to the table's own (its
  recipe's, for a prefabricated one) when the template is read. A transfer
  YAML writes the recipe out with them already appended.
- The Builder writes a where line by column: this table's column, then
  either **Value** (a value typed in) or **In supporting table** (a
  supporting table and one of its columns), which writes
  `x.Column IN (SELECT [Col] FROM {{prefix}}_Table)`: `IN`, not a join, so a
  code listed twice cannot duplicate rows. A prefabricated table's lines go
  under `add_where` and `add_join`; a built table's under `where` and `join`.
- Joins to the template's tables choose their operator (`=`, `<>`, ...) as
  well as their type, with what each type does beside it.

### D106. `dist/` is not committed; two bundles, one with the queued YAMLs

**Amended by D122:** one `makebundle.py` carries the queue; `bundle_with_yamls.py` is retired; YAMLs can travel alone.

**Amends D63 and D64** (the committed bundle), **D79** (its consequence) and
**D102** (the committed bundle built from the commit).

**Context.** `dist/bundle.py` was committed and rebuilt with every bundled
change, but it is a build product: the Mac builds it and it is copied to the
VM. D79 kept transfer YAMLs out of the committed bundle, which kept project
pulls out of the repository's generic build; a build with them then
overwrote the committed file locally.

**Decision.**

- `dist/` is ignored by git. `python3 makebundle.py` writes `dist/bundle.py`,
  the runtime alone; `python3 makebundle.py queue` writes
  `dist/bundle_with_yamls.py`, carrying every queued intake's transfer YAML.
  You choose which to copy.
- Each build writes its content_id to `dist/content_id.txt` beside it (one
  line per bundle file).
- The app's Exports has **Make bundle**, beside the queue (Mac only): it
  replaces `dist/bundle_with_yamls.py` and shows its content_id until the
  next build.

**Consequences.** A checkout has no bundle until one is built. A bundle is
built from the working tree again, so an uncommitted file under
`scripts/pullmanager_src/` is carried (D102's `utils/` rule stands).

### D107. The PK's key is a "Row key", prefilled

**Decision.** Key columns are labelled **Row key**, "the columns that make
each row one of its own", with what relies on them: the uniqueness check
(D20), chunk order (D53), the random sample (D60) and control sampling
(D59). A new uploaded PK, or one without a key, is given
`PatientDurableKey` when its file has that column.

### D108. The VM side is where the app runs from an extracted bundle

**Context.** Pending transfer was offered on the VM, where it means
nothing, and confused. Detecting the VM must not probe the network or the
system.

**Decision.** The app is on the VM side when it runs from an extracted
bundle (a `.bundle-manifest.json` beside the code), which is also how it
finds the working folder. There, Pending transfer is not offered, and a
missing upload file is an error in Validate, as it is at the split. On the
Mac, which runs from the repository, nothing changes.

### D109. A column name with quotes in it is a warning

**Decision.** A CSV or parquet column whose name has a quote character
(`'DiagnosisCode'`) is `quoted_column_name`, a warning whose fix is to rename
it in the app (D98) or save the file without them.

### D110. A fact table is added inline, at the top of Fact Tables

**Decision.** "Add a fact table" moves to the top of Fact Tables, shaded
apart from the tables below. It offers **Prefabricated** (a recipe) and
**From data dictionary** (a table), each with a Name. Choosing a dictionary
table opens the whole form in place (name, destination, description,
granularity, columns, joins, where), not in a window of its own. The PK
Table's "Table from the dictionary" opens the same way.

### D111. The core files live in `recipes/`, found through `datascope.json`

**Amended by D117:** the folder is `reference/`.

**Context.** `YAMLs/` held two kinds of file: the core ones the tools read
(the data dictionary, recipes, the template, the VM's package list) and the
pulls' own (intakes, test templates). The user wants `YAMLs/` for the pulls,
and the core files somewhere they can be moved without editing code.

**Decision.**

- `datadictionary.yaml`, `recipes.yaml`, `template.yaml` and
  `DSVM Plugins.yaml` move to `recipes/` at the root, with
  `requirements-vm.txt`, which pins the same list.
- `datascope.json`, at the root, says where each is, relative to itself, and
  where runs go: `recipes`, `datadictionary`, `template`, `vm_plugins`,
  `runs`. Moving a file means editing that one line. makeYaml and Pullmanager
  each read it (neither imports the other; a test holds their defaults
  together). Without it, the defaults are the `recipes/` paths, beside the
  code, and `runs/` in the working folder: the extracted bundle's layout.
- The bundle publishes the dictionary at `recipes/datadictionary.yaml`. An
  extracted tree's old `YAMLs/datadictionary.yaml` is removed by the next
  extraction, or kept as `.local` if it was edited (D42).

### D112. The browser UI is retired; `datascope.py` opens the app

**Amended by D123:** the entry script is `scope.py`.

**Retires the browser UI (D92), amends D79** (`yamlmgr.py`).

**Decision.**

- `scripts/yamlmanager.py`, `yamlmanager_backend.py`, `yamlmgr.py` and
  `UI/` are removed. The app (D93) is the one UI.
- `python3 datascope.py`, at the root, opens the app; `python3 datascope.py
  test` runs every suite; anything else goes to `pullmanager.py` as typed.

### D113. The repository's top level: `plan/`, `.claude/CLAUDE.md`, runs in `cleanup/`

**Decision.**

- `QMDs/` is renamed `plan/`.
- `CLAUDE.md` moves to `.claude/CLAUDE.md`, which Claude Code reads there.
- On the Mac, `datascope.json` sends runs to `cleanup/runs/`, beside the
  Python cache, since everything in `cleanup/` is disposable output. The VM
  has no `datascope.json` and keeps `runs/` in its working folder.
- `TESTING_UPLOADS_AND_LABS.md` moves beside the scripts it describes, in
  `scripts/`.

### D114. The dictionary records Cosmos's real types; a dictionary page outranks the VM's AI

**Amended by D130:** the checked dictionary replaced the old one.

**Decision.**

- The dictionary's `type` is what the Cosmos dictionary page shows, not what
  a column's name suggests. A flag stored as `tinyint` is `tinyint (flag)`,
  and a `float` is `float`, each with its own family: `tinyint` accepts
  `TINYINT` and wider integers, `float` accepts `FLOAT` only.
- `BIT` is refused for a `tinyint` column: it would turn a stored 2 into 1
  without an error. Guessing that a tinyint is only ever 0 or 1 is left out.
- A table's entry counts as checked only against the page itself (a
  screenshot), with a comment above the table saying what was checked and
  when. `LabComponentResultFact` is the first.

**Why.** The Infant_RSV pull failed on Cosmos with error 8114 (nvarchar to
float), naming no column. `ReferenceValueHigh_X` and `ReferenceValueLow_X`
are `nvarchar(300)`; the dictionary, transcribed by the VM's AI and
confirmed against that transcription, said `numeric`, so validation passed a
`FLOAT`. Validation already compared types; the dictionary was wrong. The
same page showed `PrioritizedDateKey` as `bigint`, `Count` and the flags as
`tinyint`, five columns missing, and one foreign key attached to the wrong
column.

**Consequences.** Other tables' `boolean` flags stay until their pages are
checked; each check may turn a template's `BIT` into an error that says to
use `TINYINT`. The user will screenshot the tables the recipes use.

### D115. Column types come from the dictionary, filled in at export

**Amends D114** (a `BIT` on a `tinyint` column no longer arises: templates
stop declaring types).

**Decision.**

- A column whose source is `alias.Column` on a dictionary table takes its
  type from `recipes/datadictionary.yaml`, filled in when recipes are
  imported, the step every output shares: validation, the transfer YAML,
  Export split and Preview. The transfer YAML carries the filled types, so
  the VM's split and dry run need nothing more.
- The dictionary wins, silently. A type a template or recipe still declares
  on such a column is replaced without a message. The table builder stops
  writing types, and the types already in `recipes.yaml` and the intakes are
  removed.
- A column the dictionary cannot type (an expression, a generated temp, an
  upload) keeps a declared type, and an error says so if it has none. Every
  column in the recipes and intakes today is a dictionary column.
- The fill-in writes the page's real type where the dictionary records one
  (`nvarchar(300)`, `tinyint`, `float`): the widest the source can hold, so
  nothing is cut off, and no wider. A string with no recorded length becomes
  `NVARCHAR(900)`: Cosmos text is Unicode, which `VARCHAR` would turn to
  `?`, and the user keeps widths under 1000. A page type of `nvarchar(max)`
  or over 900 is left for when one appears.

**Why.** Every template held its own copy of each type, taken from the
dictionary when the table was added. Correcting the dictionary (D114) left
the copies stale, so each correction broke the templates that had copied the
old type, though the user never wrote a type. With one copy, a correction
reaches every template on its next export.

**As built** (`e0789f8` to `ffa01e6`). The fill-in sits in `import_recipes`,
which every output calls. The type-family check (D114's families) is removed:
with every type filled, it could no longer disagree. The error for a column
nobody types comes after the dictionary's own errors, which say why better,
and a missing dictionary makes every column one. 784 types were removed from
`recipes.yaml` and the intakes; `LabComponentResultFact`'s text columns went
to their page widths.

### D116. The dictionary lists only the columns a pull can read

**Decision.** A dictionary page's Available column marks each column with an
SD icon, a database icon, or both. Only columns with the database icon go in
`datadictionary.yaml`; a column marked SD alone is left out, and says so in
the table's comment.

**Why.** The user pulls only from the database, and wants the dictionary to
hold only what a pull can name. An SD-only column in it would pass
validation; whether Cosmos then refuses it is not tested, and need not be.
Five were added to `LabComponentResultFact` from its page (`IsFinal`,
`RawNumericValue_X`, `RawUnit_X`, `RawValue_X`, `SourceKey`) before the
icons were read, and are removed.


### D117. The core files' folder is `reference/`, not `recipes/`

**Amends D111.** `recipes/` is renamed `reference/`, and `datascope.json`,
the defaults in makeYaml and Pullmanager, and the bundle (which publishes
`reference/datadictionary.yaml`) follow. The folder holds more than recipes:
the dictionary, the template, the VM's package list, and the dictionary
pages' screenshots (`reference/DDict image refs/`). The extracted tree is
replaced whole (D42), so the VM's old `recipes/` goes on the next extraction.

### D118. Every table's written SQL is checked against its own aliases

**Amended by D129:** a fact table that joins nothing the pull makes is an error.

**Context.** Infant_RSV's first Execute stopped at its PK on `pk.EncounterKey`:
a join carried from a recipe named an alias the intake's table did not have.
Eight more of the kind were in the same intake. Validate, Export split and
Preview SQL all passed, because none reads join or where lines; SQL Server
was the first reader.

**Decision.**

- makeYaml checks each table's join and where lines, wherever Validate runs
  (Mac, app, VM). An alias used but not defined by the table's `from` or its
  joins is an **error** naming the aliases it has. A column the dictionary
  does not list for that table, a fact table joining no generated table, and
  a PK joining a many-rows table without `dedup_keys` are **warnings**.
- The builder runs the same check on a written line as it is added, and
  refuses one naming an alias the table lacks.
- Changing a table's alias rewrites its own join and where lines, as it
  already rewrites its columns.

### D119. A built where line picks its operator

**Amended by D131:** `=` refuses a `%` or a comma list.

**Amends D105.** The builder's Value mode wrote only `=` and quoted what it
was given, so `BETWEEN {{min_date_key}} AND {{max_date_key}}` became
`= 'BETWEEN …'`. It becomes an operator dropdown: `=`, `<>`, `<`, `<=`, `>`,
`>=`, `IN` (a comma list), `LIKE`, `BETWEEN` (with Lower and Higher fields),
and In supporting table as now. Numbers and `{{Variables}}` are written
unquoted, text quoted.

### D120. Each table's standard where lines live in the dictionary

**Amended by D130:** the lists are the checked dictionary's `standard_checks`, date windows included.

**Decision.** A table in `datadictionary.yaml` may carry `standard_where`, a
short list of conditions written without the alias. When the builder adds
the table, the lines arrive as ordinary where lines under its alias, which
the user may remove. The lists are seeded: `_IsDeleted = 0` on each of the
15 tables that have it, and `IsCurrent = 1`, `IsValid = 1` on PatientDim.
`_IsInferred = 0` is not standard. Each list is written out in full on its
table, never applied as a hidden rule, so the dictionary shows what is added.

**Why.** The user adds the same lines to nearly every table, and they make
the pulls smaller and faster.

### D121. A table's description and granularity are imported, and editable

**Amended by D131:** recipe columns collate too.

**Decision.**

- A table built from the dictionary starts with the dictionary's description
  and granularity.
- A recipe table's description is the dictionary's description of its source
  table followed by the recipe's own; its granularity is the recipe's.
- Either may be kept, cleared, or added to. Nothing is written as an override
  unless it differs from what was imported.
- Recipes follow a convention: a recipe's description says only what the
  recipe adds, and never repeats the dictionary's text.

**Why.** The user wants to see what is there and extend it, not write an
override blind. A recipe reshapes its source, so the source's granularity
would be wrong for it; its description, though, is an addition to the
source's.

### D122. One `makebundle.py`, which carries the queue; YAMLs can travel alone

**Amended by D131:** `--no-queue`.

**Amends D106** (two bundles) **and D91** (the queue).

- `python3 makebundle.py` builds the runtime, and carries the transfer YAML
  of every queued intake; with nothing queued, the runtime alone.
  `bundle_with_yamls.py` is retired.
- Exports gets **Bundle With Manager**, which does the same, and **Bundle
  YAMLs only**, which writes `dist/yamls_to_transfer.py`. Extracting that
  places its transfer YAMLs beside the entry script (keeping `.local` copies
  as now) and leaves the runtime alone.
- The queue is emptied after each bundle.
- Exports lists projects (`Infant_RSV`), each as "intake → transfer YAML",
  not file names.

**Why.** The user always meant transfer YAMLs to travel with the bundle (see
the roadmap's correction of D79 and D106), and wants to send a new pull
without replacing the software.

### D123. Datascope, Telescope, and `scope.py`

**Amends D112.** The product is **Datascope**. Editions for a particular
source get their own names: Epic Cosmos's is **Telescope**; others (census,
genetics) will be themed later. The entry script is `scope.py` on both
machines: on the Mac it replaces `datascope.py`, and on the VM extraction
writes it in place of `pullmanager.py` and deletes an older `pullmanager.py`
it had written, so there is one way in. The runtime package keeps its name.

### D124. `utils.py`, and a stock list

**Amended by D131:** a stock entry that is not there is said, not fatal.

**Decision.**

- `utils.py` opens a small window with a button for each script in `utils/`,
  each started as its own process. It sits at the Mac's root, and extraction
  writes one beside `scope.py` on the VM.
- `viewparquets.py` moves to `utils/`.
- `stock/stock.yaml` lists, by path, what Artifacts copies into every run
  folder (`HOW_TO.md`, `../utils/viewparquets.py`, R scripts later), grouped
  as the user likes. `HOW_TO.md` keeps its `{project}` fill-in.

### D125. The builder's layout: join rows, Duplicate, and collapsed columns

**Amended by D131:** a duplicate always gets its own destination.

- A join row reads as its SQL does: type, JOIN, the other table and its
  column, the operator, then "by column" and this table's column.
- A table's buttons are Edit, Duplicate, Remove, Save as Recipe. Duplicate
  copies the table as `<name>_copy` with its own destination, and opens a
  built table for editing.
- A table's column list sits under a "Columns (n)" header that starts
  collapsed, remembered per table while the draft is open.

### D126. Run chooses a pull from two dropdowns

**Amended by D131:** Start run lists `*_transfer.yaml`.

- **Running pulls**: every pull whose Execute is live, with how far it has
  got. Choosing one follows its log, shows its status, and lets Stop reach it.
- **Start run**: the transfer YAMLs in the working folder, by project name
  and with their last state, running ones left out; Browse beside it.
- The Split folder and SQL folder fields are removed: they are always
  `runs/<project>/split` and `/sql` (D57), said as a line of text.
- The data dictionary field shows the file it found, in full, with Browse.

### D127. A CSV can be pasted into a supporting table

In Supporting tables, **Paste** beside Browse opens a box for CSV text. As it
is pasted, the columns, row count and first rows show; a ragged row or a
missing header is an error naming the line. On adding, it asks for a name
and writes `YAMLs/temp/csv/<name>.csv` on the Mac, which the table then
reads as any upload. The rows are not written into the YAML, so the file can
be reused.

### D128. `plan/tasklist.qmd` holds what is under discussion

**Amends the rule that there are three plan documents** (`.claude/CLAUDE.md`).

- `plan/tasklist.qmd` is the standing list of the user's ideas and notes
  still being discussed. Under each, Claude answers in a blue callout
  (`🟦 Claude: …`) followed by an empty orange one (`🟧 Your response`).
- A `# Settled` section sits at the bottom. What is agreed moves there, and
  from there, promptly, into decisions, the roadmap or design, and is then
  deleted. New notes go above it.
- It holds no settled facts, so it is not a fourth design document.
- A pasted image goes in `plan/images/markdowns/<document name>/` (set in
  `.vscode/settings.json`), and "update docs" deletes the folder of any
  document that no longer exists.

### D129. A fact table that joins nothing the pull makes is an error

**Amends D118**, which made it a warning.

**Context.** Checking UC_Visits and UC_DxHxSxRx before they went to the VM,
their built fact tables (EDVisitHistory, HospitalAdmissions, three medication
tables) had no join and no where line. Rendered, each read `FROM
dbo.EdVisitFact` with nothing but NOT NULL checks: every row in Cosmos, not the
PK's patients. Batching does not restrict a fact table by itself. A Crohn's
pull that ran for hours had the same shape (CrohnsClinicalData's medication
tables).

**Decision.** `fact_table_not_joined` is an error. A fact table counts as joined
when any of its lines names a table the pull makes (`{{prefix}}_...` or `#`),
a subquery in a where line included. Its fix is the join to the PK, written
out with the table's alias.

**Why.** A warning is read after the fact; this one costs hours of Cosmos time
and a table no one wanted. The user prefers a loud stop (D28, D45).

### D130. The checked dictionary is the dictionary; its standard checks are D120's lists

**Amends D120** (the seeded lists) **and D114** (only LabComponentResultFact
checked).

**Decision.**

- `datadictionary_checked.yaml`, checked against the page screenshots in
  `reference/DDict image refs/`, replaces `datadictionary.yaml`. The foot of
  the file lists what is not yet checked or only partly seen, and the tables
  a checked foreign key points at that the dictionary lacks.
- Its per-table `standard_checks` are D120's `standard_where`, renamed: a
  live row (`_IsDeleted = 0`; PatientDim also `IsCurrent`, `IsValid`,
  `UseInCosmosAnalytics_X`) and, for a checked fact table, the partition
  key's date window. An unchecked table with `_IsDeleted` gets
  `_IsDeleted = 0`, as D120 seeded.
- The builder adds them all to a new table. Remaking the existing intakes
  added only the live-row lines a built table lacked: a date window on a
  mother's record or a pregnancy linked to a birth could drop rows, so it is
  the author's to add.

**Consequences.** A column the pages lack is gone, so a pull naming it stops
on the Mac: Crohns_DxHxSxRx's `MedicationOrderFact.OrderedDateKey`, which is
not on the page, was removed from it.

### D131. Smaller choices made building D118 to D127

- **Recipe columns collate too** (D121): a recipe column's description follows
  its dictionary column's, as the table's does; text already containing the
  dictionary's is kept as it is.
- **`=` refuses a `%` or a comma list** (D119), naming LIKE or IN, rather than
  switching silently as Value mode did.
- **A duplicate always gets its own destination** (D125): a recipe's default
  would land both tables in one.
- **Start run lists `*_transfer.yaml`** (D126); anything else is reached with
  Browse. Run remembers only the transfer YAML and a chosen dictionary.
- **A stock entry that is not there is said, not fatal** (D124): Artifacts
  runs at the end of a long pull.
- **`makebundle.py --no-queue`** builds the software alone and leaves the
  queue (D122); `queue`, D106's word, is accepted and changes nothing. No test
  may run a build that reaches the real queue: one did, and emptied it.
- **The Author view's tests open intakes of their own**
  (`scripts/yamlmanager_fixtures/temp/`): they used the pulls in
  `YAMLs/temp/`, and remaking those broke them.

### D132. Pasted images stay where Quarto puts them; `scope.py images` deletes the ones no document mentions

**Amends D128** (the paste folder).

**Context.** `.vscode/settings.json` sent a paste to
`plan/images/markdowns/<document name>/`, but only VS Code's Markdown view
reads it. Quarto's visual editor, where the task list is written, puts a paste
in `images/` beside the document, named `paste-<n>.png`, and has no setting to
move it. It also draws callouts as plain boxes and shows raw HTML as code, so no
CSS colours them there.

**Decision.**

- A pasted image stays in the visual editor's `images/` folder. The `.vscode`
  setting is removed.
- `python3 scope.py images` (`scripts/tidy_images.py`) deletes every `paste-*`
  image in an `images/` folder under `plan/` that no `.md` or `.qmd` in the
  repository names, by its path from that document or from the root (plain or
  URL-quoted; `cleanup/` and `dist/` not read). "Update docs" runs it last.
- Callout colours show only in Quarto Preview (a note blue, a warning orange);
  in the editor the 🟦 and 🟧 in the titles carry them.

**Why.** Deleting by mention lets a deleted document, or a settled task-list
section, take its pictures at the next update, at no cost in tokens, and never
touches an image not named `paste-`. A test holds it to the path: the same
`paste-1.png` beside another document does not keep this one.

### D133. A utility to see and clear a Projects database

**Context.** Infant_RSV's two sessions failed at `setup` with error 1105:
the project database's data file had reached its 20,000 MB cap, full of the
tables of earlier pulls, already packaged as parquets. A drop-everything
script in SSMS seemed to do nothing, most likely waiting on a lock another
session held. Its log, a separate 20,000 MB file, stood at 12.5 GB used,
held by an open transaction (`ACTIVE_TRANSACTION`, under `SIMPLE` recovery).

**Decision.**

- `utils/clear_projects_db.py`: a tkinter window onto one project database,
  showing each table's rows and size, each file's use against its cap, and
  what the log is waiting on. It drops one table, the selected ones, or all
  (all only after the database's name is typed), and refreshes after each.
- Each drop commits alone, with a 30-second lock timeout: a locked table is
  reported as blocked, naming the usual holders, and the rest still drop. A
  foreign key on or pointing at a chosen table is dropped first.
- It refuses any database whose name does not start `PROJECTD`.
- Pullmanager itself still drops nothing of a finished pull, and does not
  check space before it starts (roadmap).

**Why.** Dropping is the user's choice, table by table, once the parquets are
safe (D28, D45): a pull deleting its own tables would lose the copy
Artifacts reads. A lock timeout turns a silent hang into a message saying
what to close. The name check keeps it off Cosmos and anything that is not a
project database.

### D134. Table groups: one Cosmos connection each, inside the session

**Amends D36** (one connection for the whole session).

**Context.** The user uploaded all 1.2 million Crohn's patients (about ten
columns) in about two minutes, not the long block feared. The fear was why every
table of a cohort ran on one connection held open for hours, with no news until
it ended. A pull's tables tend to come in related blocks: a birth (BirthFact,
the mother, the pregnancy), medications (order, administration, dispense), visits
(ED visits, hospital admissions). The user wants to pull each block on its own
connection, and to see it named in the progress: "getting UC Meds", not "getting
all UC data".

**Decision.**

- A **table group** is a named set of a template's fact tables. The levels are
  session (one PK) → group (one Cosmos connection) → batch → chunk.
- The session's first connection does setup, uploads and the PK, as now; the
  PK is built once. Each group then opens its own connection, mints its own
  epoch and captures `@@SERVERNAME` (D37), refills the PK temp from the PK's
  Projects copy (D19), loads the supporting tables its tables read, runs every
  batch and chunk of its tables, and closes.
- Groups are outside batches: every chunk of UC Meds, then every chunk of UC
  Visits. Progress names the group (`UC · Meds · c3of12`).
- The tables in no group share one group, so a template without groups runs as
  today.
- A table that reads a table in another group is an error naming both groups,
  whose fix is to put them in one group. It is never reloaded from Projects.
- Inside a group, tables run in the Fact Tables order, which can be changed by
  number. A table that reads a table later in that order is an error naming both.
  It is not reordered for you (D45). This check applies to templates without
  groups too: today such a table fails in Cosmos.
- A failed group is retried alone by `--retry-failed`; finished groups are
  skipped. Under `Dual`, every SneakPeek session still runs first (D65).

**In the app.** Table groups get their own section after Fact Tables: **Add
group** (a name), **Add table to group**, **Remove table from group**, **Remove
group**, and the note "Tables in no group run together, as one group". Each fact
table says which group it is in. It is not in Splitters, which splits the PK. They
are "groups", not "batches": batching already means pieces of one table, and
every destination has a `_batch` column. Splitters' **Separate tables** is renamed
**Separate PK per level**.

**Why.** A group costs only a refill of the PK temp, which the upload suggests is
minutes. In return a stuck or failed group costs only itself, and progress names
what is being pulled.

### D135. Recipe sets: several recipes added as one table group

**Decision.**

- `recipes.yaml` gets a `recipe_sets:` list. A set names its recipes in order,
  which is the order they run: a chain's first table first (BirthFact before the
  tables that read it).
- Adding a set puts its recipes in the template as ordinary fact tables, with
  their table inputs to each other already bound, in a table group named for the
  set. After that there is nothing special: each table is edited, removed or
  moved out of the group as any prefabricated table is.
- A set and a group are two things. Groups can be made by hand, with no set.
- A set has no variables of its own. Its tables take the template's and the
  PK's, as every table does.
- In the app, a group has **Save as recipe set**, as a built table has Save as
  Recipe (D56).

**Why.** Birth, medications and visits are each two or three tables that are
always pulled together and read each other.

### D136. Execute says when each step starts and ends, as it happens

**Amends D70** (the log's readout).

**Context.** During Execute the log printed a session's name, then nothing
until the whole session had ended. Each table was landed and committed as it
went (D55), but no line said so. A table that took four hours looked the same as
a hung pull, and so did every table before it in the run.

**Decision.** Each step prints a line when it starts and when it ends, flushed at
once so the console, the log and the Pull Log tab show it while it runs. Every
line starts with the time (`HH:MM:SS`). Each line is one of:

- the session's connections once they are open;
- each phase and run: started, then done (with its time and rows), FAILED
  (with its time and error), blocked or skipped (with why);
- each upload: into Projects started, then its rows and time; into Cosmos
  started, then its rows and time (or not sent to Cosmos, or kept);
- each table: started, then its rows, with the time Cosmos took to build it and
  the time it took to land in Projects;
- a batch's PK rows going up to Cosmos, and the PK's parquet.

Inside a run, a chunk or a value names itself (`c3of12`, `v2of5 (LA)`) at the
start of its lines. The end-of-session summary is unchanged.

**Why.** A table's start line with nothing after it is the table in flight, so a
slow table and a hung one can be told apart, and the upload's two legs are timed
separately (the question behind D134's cost).

### D137. A run's rows are its tables', one count per table

**Amends D136** (the end-of-session summary) **and D70.**

**Context.** Infant_RSV finished with exit code 0, but nothing said which tables
it had pulled or how many rows each had: the summary named only the run
(`done EDVisits__run`), and only the PK's parquet appeared before Artifacts ran,
so the pull looked as if it had stopped after the PK. The manifest's `rows` on a
run was its first table's count alone, so a run of nine tables recorded one
number that described none of the others.

**Decision.** Each run records `outputs.table_rows`: every table it landed and
the rows Cosmos built for it, added across chunks and values, and saved as each
table lands. The PK phase records its one table the same way. A run has no
`rows` of its own, and nothing ever shows a total across tables: four tables of
5,000 are four lines of 5,000, not 20,000. The end-of-session summary, the
summary command and the Status tab list the tables under their phase or run; a
failed run lists what it landed before failing. A node that starts again drops
its last attempt's rows. With table groups (D134) each group is its own run, so
its tables are listed under it.

**Why.** Row counts are only meaningful per table; a total mixes visits with
lab results. The counts were already gathered for the Cosmos/Projects check and
thrown away.


### D138. Smaller choices made building D134 and D135

Confirmed by the user once built (29 September 2026):

1. **The first group shares the PK's connection.** A new connection opens only
   when the group changes, so a template with no groups runs exactly as before,
   and the first group saves one reload of the PK.
2. **Groups run in the order `table_groups` lists them**, then the tables in no
   group, last.
3. **Add table to group offers only tables in no group.** A table in another
   group is refused ("remove it from there first"), not moved.
4. **Duplicate puts the copy in the original's group**, right after it.
5. **Save as recipe set** saves a prefabricated table as the template has it
   (its bindings to its siblings, its variables and filters). A table built from
   the dictionary is saved as a recipe too, in the same write, because a set
   names recipes. A name already in `recipes.yaml` is refused, and the file is
   left unchanged.
6. **Artifacts packages a finished group's tables while another group is
   unfinished.** Before, every table waited for every run, and only the
   session's first run was read, which would have missed every group but the
   first.
7. **The temp prefix stays the session's** on every group connection.

### D139. A multiplied table read by its written name is a warning

**Context.** Building D134 found that under multipliers each level's copy of a
table is named for its level (`UCOrders`), while SQL written as
`{{prefix}}_Orders` still names `##<prefix>_Orders`, which is never made. The run
fails at Execute. The PK is reached through `{{PKTable}}`, which is the level's
own, unless it too is written by name. The real fix is the renderer owning temp
names (roadmap: Generated-table dependencies). None of the six queued pulls
combines multipliers with such a read.

**Decision.** Until then, validation warns (`multiplied_table_read_by_name`),
once per pair, naming the reader, what it reads and the level's real name; for
the PK the fix is `{{prefix}}_{{PKTable}}`. A warning, not an error: the user's
choice, so a template is never stopped by a check that may be wrong about it.

### D140. Run lists finished and stopped pulls in a dropdown of their own; Execute records how it ended

**Amends D126.**

**Context.** Start run listed every transfer YAML with how it last ran, so the
pulls to start and the pulls already run were mixed in one list, and a pull that
crashed (exit code 1), one the user stopped and one closed with its window read
alike: `stopped mid-run`.

**Decision.** Three dropdowns, in this order:

1. **Running pulls**, as before.
2. **Finished and stopped pulls**: every pull that has run and is not running,
   each with one of: `(finished)`, every session done and nothing failed;
   `(finished with errors)`, Execute ended by itself but a run, phase or session
   failed or was left pending; `(stopped by user)`, ended by Stop or Ctrl+C;
   `(stopped with errors)`, it did not end by itself (an unexpected error, a
   killed process, a closed window). A finished pull stays here; Re-pull
   everything runs it again.
3. **Start run**: the transfer YAMLs with no pull yet.

Choosing any loads the pull. To tell the stops apart, Execute writes
`last_execute: {started_at, ended_at, exit_code, how}` into the manifest as it
ends, on the unexpected-error path too. A hard kill writes nothing, and a
manifest still `running` with no lock held reads as stopped with errors.

### D141. Artifacts runs by itself after a clean pull

**Amends D72 and D67.**

**Decision.** When Execute ends with every session finished and nothing failed,
it runs Artifacts in the same process, still holding the lock, printing to the
same console, log and Pull Log tab. A pull with any failure is not packaged, and
says so, pointing at Retry failed or Artifacts. The Artifacts button and
`--artifacts` stay for packaging by hand. If Artifacts fails, Execute's exit code
is still 0, since the pull is safe in Projects, with a loud line naming the
tables not written and saying to run Artifacts again.

**Why.** Infant_RSV looked stopped because only the PK's parquet appeared until
Artifacts was run by hand.

### D142. The run folder: parquets, manifest and latest log at the top, the machinery in `pull_files/`

**Amends D57, D68, D72 and D87.**

**Decision.**

``` text
runs/<project>/
  cosmos_parquets/
  sneakpeek_parquets/
  uploads_parquets/
  pullmanifest.yaml
  execute-<date>-<time>.log     the latest Execute's log
  <project>_transfer.yaml       the copy the split was made from
  contents.md, HOW_TO.md, load_parquets.py, load_parquets.R
  older_logs/                   every earlier log, moved there when an Execute starts
  pull_files/
    split/                      sessions/, uploads/
    sql/
```

Names use underscores, not spaces. The manifest's paths stay relative to itself
(`pull_files/split/sessions/...`). The PK's parquet (D87) goes to its database's
parquet folder with the rest. Run folders in the old layout are not moved or
read specially: a pull that needs redoing is split again, which starts it over
(the user: few runs have succeeded so far).

### D143. Execute turns QuickEdit off in its console window

**Context.** Crohns_PatientsFromUpload finished every step (29 September 2026),
but its window sat at `=== CrohnsPatients ===` with the lock still held: its
title read "Select". A click in a Windows console with QuickEdit on starts a
selection, and every write to the console waits until it is cleared, so the
first line after the session, and everything after it, waited.

**Decision.** Execute turns QuickEdit off in its own console as it starts
(`SetConsoleMode`, standard library `ctypes`), keeping every other input mode.
Anywhere but a Windows console it does nothing. Selecting text with the mouse
in that window no longer works; the log holds everything the window shows.

### D144. Run shows the manifest in a tab of its own

**Context.** A failure's full record (its error's message and detail, and
everything else the manifest holds) was seen only by opening
`pullmanifest.yaml` in an editor; Status shows the first line of the error.

**Decision.** A **Pull Manifest** tab between Pull Log and Status shows the
loaded pull's `pullmanifest.yaml` as it is, with line numbers, refreshed with
Status and keeping its scroll position. Every `status:` line is coloured as the
Status tab colours it, and each `error:` block with a message is red.
Double-clicking a row in Status shows that entry's lines, highlighted, or its
`error:` lines when it has one; the entry is found by its own id, never by
searching for text. The tab is read-only: the running pull rewrites the file,
so an edit there would be lost or would overwrite the pull's record.

### D145. Every window says who made it

**Context.** The user built these tools for their own work on the VM. They
are not the VM company's, whose name should not be attached to their bugs.

**Decision.** Every window (the app, the utilities window and each utility,
the parquet viewer copied into each pull's folder included) says "Designed and
built by Jason Mathias" at its foot, small and grey.

### D146. The parquet viewer opens a pull's tables by buttons, two at a time

**Amends D89.**

**Context.** The viewer asked for a file through a dialog, and read the whole
of it into Python before showing a page, so a table of tens of millions of
rows would not open. The run folder (D142) and the pulls' states (D140) now
say where every pull's parquets are and how the pull stands.

**Decision.**

- **A pull chooser.** A dropdown of the pulls that have run or are running,
  each with the words Run shows, read with Run's own code (`pulls.find_pulls`).
  Pulls not yet run are left out. This needs the runtime beside the viewer,
  as it is when opened from the utilities window. The copy Artifacts puts in
  each pull's folder, for a client who has only that folder, stays (the user:
  "that way my client can see the parquets easily"); it opens on its own pull,
  with no dropdown and no status word.
- **Then its parquets.** Large **Cosmos** and **Cosmos_SneakPeek** buttons and a
  smaller **Uploads** one, each greyed when its folder is empty; under them a
  button per parquet with its rows, read from the file's metadata.
- **Two columns.** One table fills the window; a second splits it in two; a
  third replaces the older of the two on screen. The shown tables' buttons are
  marked, and each column pages and sorts on its own. The tabs and Add View
  go. **Browse...** opens any parquet anywhere by the same rule.
- **Only what is shown is Python.** A table is kept in Arrow and only the page
  on screen is turned into Python values; sorting is done in Arrow, whole-table.

### D147. Extraction refuses while a pull executes, and every window shows its bundle

**Context.** The user wanted to be sure an update leaves no old version to be
used by accident. Extraction already replaces the extracted folder whole and
rewrites `scope.py` and `utils.py` (D6, D63). What it could not prevent: a
window or pull started before the update keeps running the old code it has
loaded, and nothing on screen said which version a window was.

**Decision.**

- **Extraction refuses while a pull is executing** (a live lock under the runs
  folder, D67, in either run-folder layout), naming it. Before asking `y`, it
  says to close the app and the utilities first, since a window left open keeps
  running the old version. A bundle of YAMLs alone is not refused: it leaves the
  software as it is.
- **Every window shows its bundle** beside the credit line (D145): `Designed and
  built by Jason Mathias · bundle ca0fa906`, the first 8 characters of the
  `content_id`, read from `.bundle-manifest.json`. The viewer copied into a pull's
  folder shows the bundle it was packaged with, written into the copy by
  Artifacts. `python scope.py --version` prints it. Run from source on the Mac,
  there is no bundle, and no bundle is shown.
- **Extraction says what it removed**: the previous extracted folder and how
  many files it held, and that `scope.py` and `utils.py` were rewritten.
- **`.local` copies are kept**, as now (the user's choice).

### D148. Each pull's folder is a deliverable, with a small utilities window; utilities are sorted into client and manager

**Amends D89 and D124.**

**Context.** A pull's folder, without its split and SQL, is everything a
client needs: the parquets, `contents.md`, the load scripts, and what made the
pull. The user wants the client able to look at the parquets and send images
of errors, with the same tools the user has.

**Decision.** `utils/` has two subfolders: `client/` (the parquet viewer and the
transcription viewer, for now) and `manager/` (`clear_projects_db.py`). The
user's utilities window shows both, under their names; a script left at
`utils/`'s top still shows. Artifacts copies the utilities window into each
pull's folder as `utils.py`, with `utils/client/`, so the client's window is the
user's own, with only the client's scripts. `viewparquets.py` no longer sits at
a pull folder's top; one an earlier Artifacts put there is removed. The viewer
opens on the pull whose folder it is in.

### D149. Artifacts backs a pull up before replacing its parquets

**Context.** A wrong pull noticed late is lost when Artifacts replaces the
parquets (D72), and the Projects tables are dropped when a pull starts over
(D52). Parquets are too large for GitLab; the VM has Git but no Git LFS and no
backup tool. A "previous" folder inside the run folder was considered and
dropped: one backup folder, on another drive, is simpler.

**Decision.**

- **A backup folder**, set in Run (Browse, Clear) and kept as `backup` in the
  working folder's `datascope.json`, so Execute, Artifacts and Run all read the
  same one, however a pull was started. It may be on another drive.
- **Before Artifacts writes new parquets**, by hand or after a clean pull
  (D141), it copies the pull's run folder, without `pull_files/`, to
  `<backup>/<project>/`, replacing what is there. The backup therefore holds the
  packaging before the latest. It copies only what changed (a file of the same
  size and time is skipped) and removes what is gone from the run folder, so it
  stays an exact copy.
- **When the backup folder cannot be reached, or none is set**, the copy goes to
  `runs/backup/<project>/` instead, packaging goes on, and Artifacts ends with a
  warning naming where the copy went. Nothing is replaced without a copy
  somewhere.
- **Back up all**, a button in Run, copies every pull under the runs folder the
  same way; one executing is skipped and named.

### D150. The data dictionary is the bundle's; nothing quietly chooses another

**Amends D126.**

**Context.** Run's data dictionary line let a dictionary be chosen, and the
choice was saved and passed to every later split, even once the bundle's copy
was newer. `contents.md` read `YAMLMANAGER_DATA_DICTIONARY`, which makeYaml did
not, so the two could describe one pull by different dictionaries. Since D130
the checked dictionary the bundle carries is the dictionary.

**Decision.** Run's dictionary line goes, and a dictionary saved in Run's
settings is ignored and dropped from them; the Backup folder row takes its
place (D149). `YAMLMANAGER_DATA_DICTIONARY` is read nowhere. `--datadictionary`
stays for a single command typed by hand, and `datascope.json`'s
`datadictionary` for a moved core file (D111); both are deliberate, and
Validate and Export split name the dictionary they used.

### D151. A failed statement anywhere in a batch fails it; every server message goes to the log

**Context.** A generated script sends several statements in one batch (the
landing's `SELECT … INTO #Local FROM OPENQUERY`, the `INSERT`, the `COMMIT`,
then the telemetry `SELECT`s). pyodbc raises an error in any statement after
the first only when the next result is asked for (`nextset()`), and `drain`
caught every exception there as "no more results", since the first execution
adapter. On 30 September 2026 IBD_Ancestry's white controls (1,232,900 and
952,204 rows built in Cosmos) landed nothing in Projects, and the phases ended
`done` with 0 rows and no warning: the count comparison never ran, because the
telemetry after the failed statement was never read. A probe on the VM
(`SELECT 1; SELECT 1/0; SELECT 2;` through `execute_script`) returned the first
result and no error.

**Decision.**

- An exception from `nextset()`, or from fetching a result set, fails the batch
  like one from `execute()`: a `DatabaseError` with the server's messages. A
  statement with no result set is not fetched.
- A cohort's landing that does not report its Projects row count is an error,
  not a comparison skipped.
- Every server message, from every statement of every batch (row counts aside:
  they are not messages), goes to the execute log only, as `server: [<block>]
  <message>`: informational notices such as "Null value is eliminated by an
  aggregate" are kept to be looked at, and kept out of the console.

**Consequences.** What was silent is loud. Tables pulled before this may have
lost rows unseen; the run scan (D152) finds them.

### D152. The run scan compares what each pull built with what it packaged

**Context.** After D151, earlier pulls are suspect (IBD_Ancestry's white
controls; Infant_RSV's PK of 186,963 where about 400,000 were expected). The
parquets can go only by screenshot, so the report must be short.

**Decision.** `--scan-runs` (the command, on the VM) reads every pull under the
runs folder, without a database, and writes `runs/run_scan.yaml` listing only
what did not check out, per pull:

- **lost rows**: a table whose parquets hold fewer rows than the manifest says
  it built;
- **empty**: a table marked done with 0 rows;
- **short controls**: a sampled control (D59) that kept fewer than `row_mult`
  times its case in a batch, with the case's count;
- **no count**: a finished run that recorded no row count for a table it makes;
- **no parquet**: a finished table missing from a packaged pull. A pull never
  packaged is said once.

With nothing wrong the file is one line. The PK phase also records the rows
Cosmos built (`cosmos_rows`) before a control is sampled, so later scans
compare those too.

### D153. A manifest save waits for the file, then stops loudly

**Amends D10.**

**Context.** On Windows a file open for reading cannot be replaced, and the
share at the VM makes reads slow. The Run window, reading a running pull's
manifest every 3 seconds, made saves fail with `WinError 5`, stopping whole
pulls (IBD_Ancestry, Celiac, 29 and 30 September 2026). A probe on the VM
confirmed one open reader is enough.

**Decision.** The rename that ends a save is retried when refused:
quickly first (0.1, 0.25, 0.5, 1 and 2 seconds), then once a minute for up to
five minutes, each minute's wait printed. Then the save fails, naming the file
and the likely holders (the Run window, antivirus, a program with it open).
Trying the rename is the check: Windows cannot say who holds a file on a share.
The backup's copy (D149) waits the same way. The lock's heartbeat already
retries on its next beat.

### D154. Readers let the manifest be replaced while they read, and Run reads it less

**Context.** D153's cause, from the reader's side. Readers only read, but an
ordinary open on Windows forbids replacing the file until it is closed.

**Decision.**

- On Windows, the runtime opens the manifest (and every YAML it reads) with
  sharing that allows it to be deleted or replaced (`FILE_SHARE_DELETE`,
  through `ctypes`); elsewhere, a plain open.
- Run's Status refresh reads the manifest once, for both the tree and the Pull
  Manifest tab, and not at all when the file's size and modified time have not
  changed since the last read.

**Consequences.** The Windows open is tested only on the VM; the Mac tests the
fallback.

### D155. The dictionary audit lists what the dictionary says and Cosmos lacks

**Context.** `MedicationDispenseFact.ReadyToDispenseDateKey` was in the
dictionary and not in Cosmos, and a Crohns_DxHxSxRx run failed on it after an
11-minute query (30 September 2026). Nothing had checked the dictionary
against the real tables.

**Decision.**

- **Audit dictionary**, a button in Run, and `--audit-dictionary`: for every
  table in the bundle's dictionary, the columns Cosmos has (`sys.columns`, one
  query per table, read-only). It writes `runs/dictionary_audit.yaml` with only
  what did not check out: `tables_not_found`, and `columns_not_in_cosmos` by
  table, each with its near matches. Columns Cosmos has that the dictionary
  lacks need no fix and are left out. With nothing wrong, one line.
- **`python3 scope.py dictionary-fix <file>`** on the Mac takes that file, as
  transcribed, removes each listed column and table from
  `reference/datadictionary.yaml`, and lists every intake and template under
  `YAMLs/` that names a removed column, to be fixed and exported again.

### D156. Each session checks its columns exist before it pulls

**Context.** D155's error, caught per pull: a missing column costs one second
at the start instead of a run.

**Decision.** At the start of `setup`, the session gathers every plain
`alias.Column` source in its cohorts, maps each alias to the Cosmos table it
names (in the database the cohort reads), and reads each table's columns once.
A source not there fails `setup`, naming each column, its cohort and table,
and near matches, so the session is blocked and the other sessions go on
(`--retry-failed` redoes it). A table whose columns cannot be read at all
(not found, or not visible to the login) is a warning: the check cannot tell a
missing table from one it may not see, and the query itself will say.
Expressions and generated tables are not checked.

### D157. Every table records its rows per join key

**Context.** Celiac's other diagnoses, 124 per patient on average, looked like
failed deduplication until the spread was measured by hand (median 93, 90th
percentile 269, maximum 1,160; 30 September 2026). A table deduplicated to one
row per key shows 1, 1, 1, which confirms it.

**Decision.** When a run finishes, for each table it landed, and when the PK
lands, one query in Projects over what that step landed (a run's `_batch`
rows) groups by the table's **join column** and records the number of keys,
the median, the 90th percentile and the maximum rows per key, as the step's
`per_key` output. The join column is the landed column whose source is this
table's side of the first equality in the cohort's first `JOIN`; the PK uses
its own key. On by default; a failed measurement is a warning. Run's Status
shows them after Rows and Duration: Median per key, P90, Max; the session
summary lists them as a note, like the widths (D34).

### D158. Chosen sessions can be re-pulled; a case brings its controls

**Context.** IBD_Ancestry's white sessions finished `done` with 0 rows (D151).
Retry failed skips them; Re-pull everything redoes the black ones too.

**Decision.**

- `--repull-session <name>`, repeatable: each named session starts over as a
  new pull would (its steps pending, its tables dropped and made again by
  `setup`); every other session, and uploads already landed (D61), stay.
  `all` means `--repull`. An unknown name is an error listing the sessions.
- A case brings its controls: re-pulling a session whose PK a control is
  sampled against (D59) re-pulls that control too, and says so.
- In Run, **Re-pull sessions** beside Re-pull everything opens a dropdown of
  the loaded pull's finished sessions, `all` first, with Add, and the list of
  those added, each removable. Execute passes them.

### D159. A quoted column name lands without its quotes by itself

**Amends D109.**

**Context.** The VM's `HospitalICDCodes.csv` has the header `'DiagnosisCode'`,
quotes included (a CSV quotes with `"`, so single quotes are part of the
name). Validate warned (D109), and every new pull needed the rename typed by
hand before a recipe reading `DiagnosisCode` could bind it. The user chose the
automatic fill over a button (30 September 2026).

**Decision.** When a supporting table's file is chosen (added, or its file or
type changed), each file column whose name carries quote characters and is not
already renamed or dropped is written as a rename to the name without them
(`'DiagnosisCode'` lands as `DiagnosisCode`), shown in Lands as like any rename.
It happens only then, so a rename back to the quoted name stays. The warning
remains for templates written by hand.

### D160. Every table with a screenshot goes into the dictionary

**Amends D130.**

**Context.** D130 checked the dictionary's tables against their pages and
listed at its foot the tables a checked foreign key points at, to be
screenshotted "before a pull joins it". The second batch of screenshots (29
September 2026) covered several of those (DiagnosisDim, ProcedureDim,
LabTestFact, SourceDim, EncounterSourceBridge). The plan was to add one only
if a pull read it or a checked key pointed at it. The user chose otherwise (30
September 2026): the dictionary audit (D155) now says what the dictionary has
that Cosmos lacks, so a table added from its page is checked without a pull.

**Decision.** A table whose page is screenshotted goes into the dictionary,
checked like the others, whether or not a pull reads it. The exception is a
page whose every column is SD only (ProcedureSetDim): it has nothing to pull
(D116), so it stays out, named at the dictionary's foot. How a batch of
screenshots is checked in is in design.md (Checking Screenshots Into The
Dictionary).

### D161. The audit also reports each wrong type and nullability

**Amends D155.**

**Context.** The screenshots leave gaps: descriptions cut off, and no row
expanded, so almost no nullability comes from a page. Most `nullable` values
came from the VM's AI. They matter: the Author view copies a column's
`nullable` from the dictionary, and `nullable: false` becomes `IS NOT NULL` in
the pull, so a wrong `false` drops rows silently. Wrong types are what broke
pulls (D114; `MedicationDispenseFact.FillNumber` was INT, and is
`nvarchar(50)`). The same `sys.columns` row the audit already reads holds both.
The user chose to check the whole dictionary for both, reporting only what is
wrong, so the file stays short enough to screenshot (30 September 2026).
Descriptions stay with the pages: they are not in `sys.columns`, and could not
come back by screenshot anyway.

**Decision.**

- The audit's query also asks for each column's type (`TYPE_NAME`, size,
  precision, scale) and `is_nullable`. Cosmos's type is written as a page
  writes it (`nvarchar(300)` from 600 bytes, `numeric(19,4)`, `datetime2`
  without its default scale). A dictionary type matches when its own words,
  annotation aside, are Cosmos's; an abstract type (`string`) never matches.
  A column with no `nullable` counts as nullable, as a pull reads it.
- Two more sections, one line per table, only the columns that differ:
  `types_wrong` and `nullable_wrong`, each holding Cosmos's value.
- `dictionary-fix` writes each in, in its line, keeping a type's annotation
  (`integer (DateKey)` becomes `bigint (DateKey)`), and lists the YAMLs that
  name a retyped column (to export again) and those that name a column whose
  nullability changed (to check their own `nullable:`, which the Author view
  copied).

### D162. One blueprint per project; the transfer YAML is renamed the blueprint

**Amends D94, D95 and D79.**

**Context.** On the VM, Author's Save wrote `YAMLs/temp/<project>_intake.yaml`,
while Run read `<project>_transfer.yaml` beside `scope.py`, rewritten only by
Transfer to Run. Author's dropdown listed both. Edits to Infant_RSV's codes and
IBD_Ancestry's upload path were saved into the file Run does not read, so the
old one was pulled (30 September 2026). The user wants one source of truth per
project: a YAML changed on the VM stays one YAML; a new version from the Mac
replaces it, with no merging, since VM changes are copied to the Mac by hand.

**Decision.**

- **The name.** The file a split is made from is the **blueprint**,
  `<project>_blueprint.yaml`, everywhere: the Mac's export, the bundle, Run and
  the run-folder rule. A `_transfer.yaml` is still read, as before.
- **On the VM, one working copy:** `YAMLs/temp/<project>_blueprint.yaml`.
  The bundle delivers a blueprint there, not beside `scope.py`. Author opens and
  saves it in place; no intake is written on the VM. Run's Start run lists it.
  Transfer to Run, on the VM, saves and loads it in Run.
- **Its record is the run folder:** Export split copies the blueprint beside the
  manifest (D142). When the pull finishes and is packaged (D141), the working
  copy in `YAMLs/temp` is removed, and the log says so, unless it was changed
  after the split. To change a finished pull, Author opens its run-folder copy;
  Save puts a working copy back in `YAMLs/temp`.
- **A new version from the Mac replaces the VM's.** The file it replaces, and
  any older copy of the same project (a `_transfer.yaml` beside `scope.py`, an
  `_intake.yaml` in `YAMLs/temp`), goes to `YAMLs/temp/replaced/`, once, so a
  change not yet copied to the Mac is not lost. This replaces `.local` for
  blueprints. Saving a draft opened from such an older copy moves that copy
  there too.
- **Author's dropdown lists projects, not files**: `Infant RSV`. It opens the
  project's one file on this side: the working blueprint on the VM (else the
  run folder's), the intake on the Mac.
- **The Mac is unchanged in kind:** its intake in `YAMLs/temp` is the source,
  since it names recipes, and Transfer to Run exports its blueprint to the
  root, where the bundle takes it.

### D163. Every Projects table carries its pull's prefix

**Context.** Pulls in one Projects database with the same table names dropped
each other's tables at setup: UC_Visits and the Crohns pulls both write
`OtherDiagnoses`, and share `upload_HospitalICDCodes` (30 September 2026). The
Cosmos temps already carry a prefix (D50); the Projects tables did not. The
user chose the temp prefix's form, first three letters of each word, numbered
on a clash; upload copies get it too. The name matters only for
troubleshooting: parquets keep the plain name.

**Decision.**

- At Export split, the pull's **table prefix** is fixed: the template's temp
  prefix (D50), and if another pull under the runs folder has it, the same with
  2, 3 and on. A pull split again keeps the prefix it had. It is written into
  the manifest's `project.table_prefix` and into every phase document, and never
  changes, unlike the temp prefix, which is renumbered per run.
- Every table a pull makes in Projects is `<table prefix>_<name>`: each
  destination, the PK's copy, and each upload's copy
  (`ibdanc_upload_IBD_Meds`). Parquets, `contents.md` and the manifest's table
  names stay the plain name.
- A manifest without a table prefix (split before this) keeps unprefixed names,
  so a pull in progress still resumes and retries.

### D164. Each pull is given its own Projects database

**Context.** Two of three pulls stopped when PROJECTD93A5E7 filled (20 GB),
while the user's other project databases stood empty (30 September 2026). The
user chose: each pull on a database of its own where possible, a database
chosen when it has more than 6 GB free, PROJECTD93A5E7 the default; once set
for a pull it is permanent; with more pulls than databases they stack, which
matters less as finished pulls drop their tables (D165).

**Decision.**

- The databases are listed in `datascope.json` under `projects_databases`;
  without it, the user's ten: PROJECTD93A5E7 first, then PROJECTD33A929,
  PROJECTD723D95, PROJECTD52219B, PROJECTD52274F, PROJECTD125423,
  PROJECTD139081, PROJECTD338331, PROJECTD427046 and PROJECTD03DEC. A folder
  `Project D<code>` is the database `PROJECTD<code>`.
- When a pull executes for the first time (no step has run, no database chosen
  yet), Execute asks each listed database for its free space before anything
  is built: the data files' room to their cap. One it cannot open is skipped,
  and said.
- It takes, among databases with more than 6 GB free: the blueprint's own
  `project_db`, if no other unfinished pull uses it; else the one with the most
  free space among those no other unfinished pull uses; else, stacking, the one
  with the most free space. An unfinished pull is one under the runs folder not
  finished and packaged. If none has 6 GB, Execute stops, building nothing, and
  lists each database's free space.
- The choice is written into the manifest (`project.project_db`, and
  `database_choice` with each database's free space) and into every phase
  document, and never changes: resume, retry, re-pull and Artifacts all read it
  there (D52).

### D165. A pull's Projects tables are dropped once it is packaged

**Context.** A finished pull's tables fill its database, and its parquets hold
them. The user chose dropping them automatically once tables are named per pull
(D163), since before that an upload copy could be another pull's (30 September
2026).

**Decision.** After a clean pull is packaged (D141), Execute drops every
Projects table the pull made (each destination, the PK's copy and each upload's
copy, by their prefixed names), provided the pull has a table prefix and the
run scan (D152) finds nothing wrong with it. Otherwise nothing is dropped, and
the log says why. The manifest records `tables_dropped` and when, so Run says
the pull can only be re-pulled from the start. Artifacts run by hand drops
nothing.

### D166. Run and the title say which project each half has loaded

**Context.** The window title showed Author's file while Run followed another
pull; Run's three dropdowns each kept their last text, so a stale
`IBD_Ancestry (not run yet)` showed beside a loaded Infant_RSV; and Status gave
no sign of the chunk and table in flight, so a 44-minute first table looked
like a pull with one table (30 September 2026).

**Decision.**

- The title names both halves: `Telescope · Author: <project> · Run: <pull>`.
- Choosing a pull from one of Run's dropdowns clears the other two, and a bold
  line above them names the loaded pull and its state, in the dropdowns' words.
- A run's row in Status shows what is in flight while it runs:
  `c2of13 · OtherHospitalizations, since 11:01`. The session records the table
  it is building (`outputs.in_flight`) as it starts it, and clears it when the
  run ends.

### D167. Double-clicking a packaged table in Status opens its parquet

**Amends D144.**

**Decision.** Double-clicking a table row in Status whose parquet exists in the
pull's run folder opens it in the parquet viewer, in a window of its own (a
table packaged as one file per value opens its first two files). Any other row,
or a table with no parquet yet, opens Pull Manifest at its lines, as before.

### D168. Filters: joins first, each form opened by a button

**Context.** The Filters box listed `join:` and `where:` lines mixed, with both
forms always open, where first, and Remove off the right edge (30 September
2026). The user chose the same layout for the table builder's Joins and Where.

**Decision.** In the Filters box of a prefabricated table and in the table
builder alike: **JOIN** then **WHERE**, each a heading with its lines under it,
each line with Remove beside it. Under each, **Add JOIN** or **Add WHERE**
opens that form in place, with **Add** and **Cancel**; either closes it. A
form's explanation shows only while it is open. In the table builder the form
also offers a written line, beside by column.

### D169. The Builder shows each message at its field

**Context.** The Builder said only `1 error, 3 pending. See Validate.`; the
message itself was on the Validate tab, and reached its field by
double-clicking (30 September 2026).

**Decision.** Each message the background check finds (D96) is shown in the
Builder at the entry it points to, in its colour with its fix, and updated in
place when the check runs again, without redrawing the page (so typing is not
interrupted). A message pointing at a section but no entry shows at the top of
that section. Validate keeps the full list.

### D170. The Projects databases are the ones the login opens, never the training one

**Amends D164.**

**Context.** The first Execute under D164 (1 October 2026) measured the ten:
PROJECTD723D95, PROJECTD427046 and PROJECTD03DEC refused the login, and
PROJECTD52274F is a training database the user will not touch.

**Decision.** Without `projects_databases` in `datascope.json`, the list is
the six the login opens: PROJECTD93A5E7 (the default), PROJECTD33A929,
PROJECTD52219B, PROJECTD125423, PROJECTD139081 and PROJECTD338331. One the
user gains access to is added in `datascope.json` or here; PROJECTD52274F is
never listed.

### D171. The Projects databases are listed in the code alone

**Amends D164 and D170.**

**Context.** The user does not want the list in `datascope.json`, which the VM
keeps by hand; it belongs with the software, changed on the Mac and carried by
the next bundle (1 October 2026).

**Decision.** The list is `DEFAULT_PROJECTS_DATABASES` in
`pullmanager/config.py`, and only there. `datascope.json` no longer reads
`projects_databases`: a file that still names it is refused, as any unknown
key is, saying which key to remove.

### D172. Only the audit's `true` nullability findings are applied, until the builder stops filtering on it

**Context.** The dictionary audit (D155, D161) on the bundle `048c3f29` found
378 differences, all nullability; SSMS confirmed it reads Cosmos right
(`Sex` 0, `IsValid` 1, `_IsDeleted` 1; 30 September 2026). A column the
dictionary marks `nullable: false` becomes, when the builder copies it, a
`NOT NULL` destination column and an `IS NOT NULL` on the cohort's WHERE. Through
a LEFT JOIN that silently drops the unmatched rows, the very loss chased that
week.

**Decision.** Apply the 51 `true` findings now (`_IsDeleted` and `_IsInferred`
in 24 tables, `PatientDim.IsValid`, `PatientDim.UseInCosmosAnalytics_X`,
`EdVisitFact.ArrivalInstant`), from `reference/Audit/dictionary_audit_true_only.yaml`
through `dictionary-fix`: they only remove a predicate. Hold the `false`
findings until the builder leaves new columns nullable, since a column's
nullability in its table is not its nullability in the pull; the dictionary
can then record Cosmos's `false` as the fact it is.

### D173. The task list is Markdown, and replies are marked by coloured labels

**Amends D128.**

**Context.** Quarto callouts showed as plain framed boxes in the user's
editor, with colour only in Quarto Preview. The user tested 24 ways of setting
a reply apart (`plan/tasklist format tests/`): only 4–7 of the `.md` file
(a background-coloured box, a border-coloured box, coloured text, LaTeX
colour) and 21 and 23 of the `.qmd` file (a styled div, coloured text) showed
(1 October 2026).

**Decision.** The task list is `plan/tasklist.md`. Each reply starts with a
coloured label on a line of its own, Claude's blue
(`[**🟦 Claude: <topic>**]{style="color:#4a90e2"}`) and the user's orange
(`[**🟧 Your response:**]{style="color:#e2904a"}`), and runs until the next
label (option 6). Option 5, the same colours as a box's border, is the
alternative the user is open to; it would mark where a reply ends, at the cost
of a `:::` to close.

### D174. Task-list replies are boxes with a coloured border

**Amends D173.**

**Context.** The coloured labels of D173 (option 6) lost their colour when the
user typed on the label's line, so their responses ended up as plain bold
text (1 October 2026).

**Decision.** Each reply is a box with a coloured border (option 5 of
`plan/tasklist format tests/format_test.md`): Claude's blue (`#4a90e2`),
headed `**🟦 Claude: <topic>**`, and the user's orange (`#e2904a`), headed
`**🟧 Your response:**`, each opened by
`::: {style="border:2px solid <colour>; border-radius:6px; padding:8px 12px; margin:8px 0;"}`
and closed by `:::`. Claude leaves an empty line inside the orange box to type
on. The colour lives on the box, so nothing typed inside it can lose it, and
the closing `:::` marks where a reply ends.

### D175. On the share, the waits carry a manifest save past a reader; no fallback

**Follows D153 and D154.**

**Context.** The reader check on the VM printed `Access is denied`: the share
(`Z:`) refuses to replace a file that anything has open, whatever sharing flag
the reader used, so D154's flag does nothing there. Step 3 of the show-stopper
tests held a pull's manifest open for 90 seconds (2 October 2026): the log said
`pullmanifest.yaml is busy (Access is denied); trying again in 60s`, twice, and
the pull finished and packaged itself.

**Decision.** The waits of D153 are enough. The fallback that was held ready,
writing the new manifest into the existing file once the waits are spent, is
not built.

**Consequences.** A reader that holds the manifest for more than five minutes
would still stop a pull with `FileBusy`. Nothing in Scope reads for that long;
if it happens, the fallback is the answer.

### D176. The Projects connection is opened anew with each table group, and a landing refused for an expired sign-in is tried once more

**Amends D134** (one Cosmos connection per group; Projects kept for the session).

**Context.** Infant_RSV's EDVisits session failed at `EDLabTestComponents` with
`Login failed for user 'NT AUTHORITY\ANONYMOUS LOGON'` (18456), after landing
Hospitalizations and Birth. Every landing runs on Projects and reaches Cosmos
through the linked server (`OPENQUERY`), which passes on the user's Kerberos
ticket through the Projects connection. That connection was opened once, when
the session opened (02:24:34), and the failure came at 12:07:24. `klist` on the
VM shows tickets that last exactly 10 hours (2 October 2026). SneakPeek's
shorter sessions never reached it.

**Decision.**

- When the table group changes, the Projects connection is closed and opened
  again, as the Cosmos one is. Nothing on it outlives a landing: each landing's
  staging table is made and dropped in its own block, and each landing commits.
- A landing that fails with 18456 or `ANONYMOUS LOGON` reconnects Projects and
  is tried once more. It fails in `OPENQUERY`, before anything is inserted, so
  the retry cannot double rows. A second failure fails the run, with a message
  naming an expired sign-in as the likely cause and Retry failed as the way on.

**Consequences.** A single group longer than the ticket is covered by the retry.
The test is a fake Projects connection that refuses one landing with 18456: the
run reconnects, lands, and the rows arrive once.

### D177. A table group is packaged as soon as it finishes, then its tables are emptied

**Context.** Crohns' and UC's Diagnoses runs filled their project databases
(1 October 2026): OtherDiagnoses alone was 57 million rows, and the groups after
it failed for space. They were rescued by hand with a one-off script
(`save_landed.py`, in the task list's history): save each landed table to
parquet, check its rows, empty it, and leave it out of the retry. The user
wants this to happen by itself, by table group, since tables in a group can read
each other.

**Decision.** When every run of a table group is done, Execute writes the
group's tables to parquet, checks each file's rows against what the runs
recorded, and empties each table that matches (`TRUNCATE`: the space comes back
at once, and the table stays). The manifest records the group as packaged, so a
retry does not pull it again; Re-pull sessions and Re-pull everything do.
Dropping the tables waits for the whole pull's packaging (D165).

**Consequences.** A pull then needs room for its largest group, not its whole.
To settle while building: Artifacts replaces the parquet folders each time
(D72), so it must keep a packaged group's files and not overwrite them from an
emptied table; the run scan must take an emptied table as expected, not as rows
lost.

### D178. A join to a generated table the pull does not make is an error

**Context.** UC_VisitsMedsDiagnoses was copied from the Crohns template, and
five of its tables still joined `{{prefix}}_CrohnsPatientInfo`, the Crohns PK.
Validation said nothing; UC's Meds failed at Execute with
`Invalid object name '##ucvis_CrohnsPatientInfo'` (1 October 2026).

**Decision.** Validation reads every `{{prefix}}_<name>` a table joins. A name
that is not one of the pull's PK, fact tables or uploads is an error, naming the
table, the name, and the tables the pull does make (the PK first, with
`{{PKTable}}` as the fix when the name looks like a PK).

**Consequences.** The UC template on the Mac was fixed by hand (`{{PKTable}}`).

### D179. Specify Project DB: a tick box, off by default; ticked means that database

**Amends D164** (the blueprint's database is a preference).

**Context.** Author asks for a project database, and Execute usually chooses
another (D164), so the field reads as a setting and is not one.

**Decision.** Author's Project section has **Specify Project DB**, off by
default. Off, the blueprint carries `project_db: auto`, Export split accepts it,
and Execute chooses (D164). Ticked, a dropdown of the listed databases (D171),
and the choice is a **must**: Execute uses it even if another pull is there, and
stops if it has under 6 GB free. Once the pull has run, Author shows the
database as text, read from the run folder's manifest, with why it was chosen,
and no field. A blueprint that names a database reads as ticked.

### D180. Run opens the utilities: View dbo tables, and a multi-column view of a text tab

**Decision.**

- **View dbo tables**, a button in Run, opens `clear_projects_db` (D133).
- **Multi-column view**, a button on each tab that shows text (Validation
  Output, Pull Log, Pull Manifest), opens the transcription viewer on that
  tab's text.
- Each is opened through its module, imported, not by a file name typed in the
  launcher, so a renamed utility fails a test rather than a button.
- A **Parquets** tab after Author and Run, the parquet viewer as a tab of its
  own, is for later (roadmap, The App, Later).

### D181. Counts before, during and after a pull, from a `profile:` in the template

**Context.** A set of hand-written aggregate queries for the HaT PheWAS pull
(`plan/sql feedback formatting/profile_queries.sql`) answered quickly what a
pull would return: cohort size, cases per quarter, the codes before the index
code, Sex against ReliableSex (1 October 2026).

**Decision.**

- A PK or fact table may carry `profile:`, a list of aggregate questions in its
  own column names: `by:` (columns, with `top:`), `dates_per_key:`, and a
  calculated grouping as a `source` expression. Each renders as a `GROUP BY`
  over the table's own SELECT and returns counts only; there is no free SQL.
- Read at three moments, built in this order: a **Count** button in Run for the
  PK alone, sent to Cosmos, building nothing (it stays available while a pull
  runs, D67), with its SQL written to `pull_files/sql/profile.sql`; then the
  PK's profile after it lands, against the Projects copy, into the log and the
  manifest, and every table's profile and rows per key (D157) in `contents.md`;
  last, fact tables in Count, a tick per table, off by default.
- Counts are exact, small ones included: they are for checking a pull returns
  what was expected, not for publication.

### D182. No automatic restart on errors Scope recognises

**Context.** The user asked whether Execute could read a failure, recognise a
known cause (a full database, an expired sign-in) and clear and retry by itself,
and worried it might misread one, since every failure ends with exit code 1.

**Decision.** Not built. The two known causes are fixed at the source instead:
a full database by packaging each group as it finishes (D177), an expired
sign-in by reconnecting (D176).

### D183. A Cosmos refresh is a `create_date` that moved by more than a day

**Amends D51** (any change, to the millisecond, is a refresh).

**Context.** UC_VisitsMedsDiagnoses' retry (2 October 2026) said
`Cosmos was refreshed: created 2026-09-17T19:34:56.450 when last run,
2026-09-17T19:54:30.070 now`, the same for SneakPeek, and started every session
over. There was no refresh. Cosmos is served by several instances (D37), each
with its own copy of the database, restored minutes apart at a refresh. D51
recorded the first instance's value and compared to the millisecond, so a
connection on another instance read as a refresh. A table group's new Cosmos
connection (D134) runs the same check mid-pull, and would fail its session.

**Decision.** A database counts as refreshed when its `create_date` differs from
the recorded one by more than a day. Refreshes come about monthly and the copies
differ by minutes. Each instance's value is recorded as well (`create_date` per
`@@SERVERNAME`), so the log can say "another instance, same refresh".

**Consequences.** The test: a manifest recorded on one instance, a connection on
another 20 minutes newer: nothing starts over; a month newer: everything does.

### D184. Recipe sets: Meds, and Diagnoses with HospitalizationsWithinICDCode

**Follows D135.**

**Context.** Only the Meds set had been saved. In the UC and Crohns templates,
OtherHospitalizations joins OtherDiagnoses, so it is a hospitalization within
the pull's ICD codes, and can't sit in another group (D134).

**Decision.** `recipes.yaml` keeps **Meds** (MedOrderHistory,
MedDispenseHistory, MedAdminHistory) and gains **Diagnoses**: OtherDiagnoses,
then the hospitalization recipe, renamed from OtherHospitalizations to
**HospitalizationsWithinICDCode**, which reads it. No other sets for now.

**Consequences.** The rename reaches the recipe and the templates on the Mac
that use it; a pull already split keeps its old table names.

### D185. clear_projects_db shows every project database, its tables grouped by pull

**Amends D133** (one database, typed in).

**Decision.** The window is a tree, as Status is: one closed row per database in
`DEFAULT_PROJECTS_DATABASES` (D171) with its GB free, measured as Execute
measures it (D164), or why the login could not open it. Opening a database
lists its tables grouped by pull, by table prefix (D163), each with rows and
MB. The buttons act on what is selected: tables, a pull's group, or a database
for all its tables. The Database field goes; a database not listed can still be
typed. It is what Run's View dbo tables opens (D180).

### D186. Author picks a built PK's row key, and a fact table's deduplication, from its columns

**Context.** A random sample on a PK built from the dictionary failed
validation for want of a key (D60, D69), and Author has no field for one: Row
key shows only for an uploaded PK. Deduplication (`dedup_keys`,
`dedup_order_by`, D58) has no field either; both are typed by hand.

**Decision.**

- Every built PK has a **Row key**: a dropdown of its columns, several allowed,
  written as `key_column(s)`, the table's `...Key` columns offered first. With
  Random sample ticked and no key, the message points to it.
- Every fact table has **Deduplicate by** (its columns, several allowed: one key
  set) and **Keep**: a column and earliest (smallest value, `ORDER BY col`) or
  latest (largest, `col DESC`). Without Keep, the note says which duplicate
  survives is arbitrary. Several key sets stay possible by hand.
- One column picker serves both, and `profile:` (D181) after them.

### D187. Recipes ship to the VM for a while

**Context.** D49 kept `recipes.yaml` on the Mac: the VM works from blueprints
with their recipes written out, and two copies could disagree. The user wants
recipes on the VM for now (2 October 2026), so Author there can add them.

**Decision.** The bundle carries `recipes.yaml` as `reference/recipes.yaml`,
where makeYaml's default finds it, replaced on extraction like the dictionary
(an edited copy kept as `.local`). Temporary: it reverses only the recipes
part of D49, and comes out when the user says.

- On the VM, Prefabricated lists the Mac's recipes, and a template naming
  recipes validates without `--recipes`. A blueprint still names none, so
  nothing that already runs changes.
- The Mac stays the source. A recipe saved on the VM goes to its copy, which
  the next bundle replaces (the edit kept as `.local`, not merged back).
- Without a recipes file a template that names recipes is still refused with
  `recipes_not_found`; the tests check it by removing the shipped copy.

**Consequences.** Two copies again, D49's original worry, accepted for now
because the Mac's overwrites the VM's on every bundle. How to undo it is in
the roadmap.

### D188. A landing reads through the linked server only before it inserts

**Amends D176**, which said a landing refused for an expired sign-in "fails in
`OPENQUERY`, before anything is inserted".

**Context.** Building D176 (2 October 2026) showed that untrue: a landing
copied the Cosmos temp, inserted and committed, then counted the Cosmos temp
again through `OPENQUERY`. A sign-in refused at that count came after the
rows were in, and the retry landed them twice (the test showed 20 rows, not 10).

**Decision.** A landing counts the Cosmos temp first, then copies it into
staging, then inserts, then counts what landed and measures widths, which read
Projects alone. Every read through the linked server precedes the insert, so a
landing refused at any of them has inserted nothing and can be tried again.
A test holds the order.

**Consequences.** The counts are the same numbers as before, read in another
order. The refresh check's instance is read with `SERVERPROPERTY('ServerName')`
in the same query as the dates (D183): the same name `@@SERVERNAME` gives, kept
apart from the query OPENQUERY's target is captured with.

### D189. Make deliverables: a folder you choose, the data under `data\`, the load scripts with switches

**Amends D72, D75 and D148.**

**Context.** Execute packages a clean pull by itself (D141) and then drops its
Projects tables (D165), so the Artifacts button only re-packages, and after the
drop it has nothing to read. What the user needs is to hand a pull's
deliverable to a client, somewhere other than `runs\`. The load scripts point
at the run folder by its full path and load every parquet folder.

**Decision.** (2 October 2026, the user's answers in the task list.)

- **Make deliverables** replaces Run's Artifacts button. It asks for a folder
  and copies the loaded pull's deliverable into it: `data\cosmos\`,
  `data\sneakpeek\` and `data\uploaded\` (from the three parquet folders),
  `load_parquets.R` and `.py`, `contents.md`, `HOW_TO.md`, the blueprint, and
  `utils.py` with `utils\client\`. Nothing from `pull_files\`, the manifest or
  the logs. It reads nothing from Projects, and runs in the window with a line
  per folder copied.
- A chosen folder that already has a `data\` stops it, naming what is there.
- A pull not packaged whole asks first: *Warning: incomplete pull. See Status
  for what is missing. Still make deliverables of what has been pulled?*, with
  Continue and Cancel. Continue copies what is packaged; `contents.md` lists
  what was left out.
- The load scripts are written for the copy and find `data\` beside
  themselves, so the folder can be moved. At their top, `load_cosmos`,
  `load_sneakpeek` and `load_uploaded` (`LOAD_COSMOS`... in Python), each
  reading its folder; Cosmos and uploaded on by default. A switch that is on,
  with its folder empty or missing, says it found nothing.
- Re-packaging from Projects stays as `scope.py --artifacts <project>`, with no
  button. The run folder keeps its own layout (D142).

### D190. Every table is its own table group unless a named group holds it

**Amends D134**, under which the tables in no group ran together, as one group
named `run`.

**Context.** HaT PheWAS's chunked run built every table for chunk 1, then for
chunk 2, and a failure anywhere would have re-pulled every table, because the
run is the unit of retry. The user wants a failure to leave the finished tables
finished.

**Decision.** A fact table in no named group runs as a group of its own, named
for the table: one run per table (per batch), its own connections, retried
alone, and once D177 is built, packaged and emptied alone. A named group is the
shorthand for tables that must share a run, because one reads another's temp.
Two ungrouped tables where one reads the other are therefore in different
groups, and validation refuses them (`table_reads_another_group`), with a named
group for both as the fix.

**Consequences.** A pull of N ungrouped tables opens N connections and refills
the PK temp N times: seconds each. A split made before this keeps its runs; a
new split makes one run per table. Batches still repeat each table's scan
(design.md, Batching: what a batch costs).

### D191. Pulls are not split by date window: accuracy first

**Context.** Chunks by patient repeat each fact table's scan (design.md,
Batching: what a batch costs), so chunks by date window were proposed: each
pass would read only its own years of the partition key, dividing the time
instead of multiplying it (2 October 2026). But each window deduplicates on its
own, so a dedup group that spans windows ("the first time each code appears",
`PatientDurableKey, BillingCodeValue` ordered by date) keeps one row per window;
and a table that joins two fact tables, each windowed on its own date, loses the
rows either side of a window's edge. Both give wrong data with no error. The
user: accurate data matters most, and a function with a chance of introducing
error is not used (4 October 2026).

**Decision.** No chunks by date window. A table's rows are split only by
patient (`chunk:`) or by value (D58), which never split a patient: each
patient's whole history is in one batch, so a pull returns the same rows split
or not, wherever each row belongs to one patient, as every fact table joined to
the PK by `PatientDurableKey` does.

**Consequences.** Time is saved by fewer passes (as few chunks as the room
allows), not by windows; room is made by packaging (D177). A timing test showing
that windows would divide the time does not reopen this by itself; validation
that could prove a table safe to window would.

### D192. A supporting list filters with `IN`, never with a join

**Context.** UC_VisitsMedsDiagnoses uploaded `IBD_Meds` (715 rows) to restrict
its Meds tables, but no table read it: MedAdminHistory pulled every medication
for 1.69 million patients, 51 million rows (9 GB) in two chunks of 34, and the
project database filled (4 October 2026). The Crohns templates had the same gap.

**Decision.** The three Meds tables of UC_VisitsMedsDiagnoses,
Crohns_VisitsMedsDiagnoses and Crohns_DxHxSxRx keep only the listed medications:
`<alias>.MedicationKey IN (SELECT im.MedicationKey FROM {{prefix}}_IBD_Meds AS im)`.
A list that only filters is read with `IN` (or `EXISTS`), not `INNER JOIN`: a
join returns a row once per matching list row, so a key listed twice would
duplicate every row it matches, silently; `IN` keeps or drops each row once.

**Consequences.** UC's re-pull carries the filter (its blueprint, with the
tables already saved turned off). Nothing yet warns of an upload no table reads;
it is on the roadmap.

### D193. SneakPeek predicts a Cosmos pull per patient, over the PK's patients it holds

**Context.** HaT PheWAS (2 October 2026) pulled the same tables from both
databases: SneakPeek's rows per patient matched Cosmos's (medians 210/205,
530/535, 2/2), and SneakPeek's rows per patient times the Cosmos PK came within
20% of each table's actual rows, in 1 minute against 3 hours 54. The user will
mostly upload PKs from now on (4 October 2026). An uploaded PK is sent whole to
both databases, and only the patients SneakPeek holds match there.

**Decision.** When the size estimate is built (roadmap, Estimate Size), a
table's rows per patient from SneakPeek are its SneakPeek rows divided by **the
PK's patients found in SneakPeek**: the SneakPeek PK's size for a generated PK;
for an uploaded PK, the upload's keys present in SneakPeek's `PatientDim`
(`IsCurrent = 1`), counted once as the PK goes up and recorded beside the PK's
rows. Never a table's `per_key` `keys` (patients with at least one row in it),
which overstates: HaT's Labs by 68%, against 20% over all its patients.

**Consequences.** A 1% sample misses the rare extreme patient (Diagnoses'
maximum 4,448 there, 42,029 in Cosmos), so the estimate carries a margin. The
count is new work in the upload phase of a SneakPeek session.

### D194. A Utils tab after Run, its buttons the utilities window's

**Context.** The utilities open from a window of their own (`python utils.py`,
D124, D148); the user wants them a click away in the app (4 October 2026).

**Decision.** A third tab, **Utils**, after Run, with the same buttons as
`utils.py` under the same headings (**Client**, **Manager**), read from the
same folders and drawn by the window's own code, so a script put in
`utils/client/` or `utils/manager/` shows in both and the two cannot drift.
Each button opens its utility in its own window, as now. D180's two buttons stay
in Run, since they open a utility on what Run has loaded.

**Consequences.** Opening a utility inside the tab is not planned; it would be
a change per utility. The Parquets tab stays for later.

### D195. Validate warns of an upload no table reads

**Context.** UC's and the Crohns pulls' `IBD_Meds` went up on every run and
filtered nothing: no table read it, and nothing said so (#98, D192).

**Decision.** Validation warns, naming the upload, when a non-PK upload is read
by no table: no cohort's SQL (its filter, joins, conditions, or a variable that
names it) mentions `{{prefix}}_<dest>`. A warning, not an error: an upload kept
only to travel with the pull's parquets is allowed.

**Consequences.** The UC and Crohns templates before D192 would have warned.

### D196. Validate and Preview say what chunks cost

**Context.** Every chunk reads each table's whole date window again (design.md,
What a batch costs), so 34 chunks cost 34 passes. Chunk sizes were chosen as if
they cost nothing (HaT 1,000, UC 50,000).

**Decision.** A template with `chunk:` gets a note: how many passes it makes
where the PK's size is known (an uploaded PK's file: its row count), else that
the passes are the PK's rows divided by the chunk; that each pass reads every
table's whole date window again; and that chunks bound what one pass lands,
not the time, so fewer, larger chunks are faster. A note, not a warning: chunks
are sometimes needed.

**Consequences.** None to running; it is text. Built at Validate and Export split, both in makeYaml; Preview, which is the runtime's dry run, does not say it, so bundle 1 leaves the runtime as it was (5 October 2026).

### D197. The Code Finder matches a code as written; `%` widens it

**Context.** The Code Finder (roadmap, future) searches Cosmos's code tables
by keyword. A typed code such as `D89.4` could mean that code alone or
everything under it (`D89.40` to `D89.49`), and which is meant varies.

**Decision.** A code is matched exactly as written: `D89.4` finds `D89.4`,
`D89.40` finds `D89.40`. A `%` widens it, as SQL's `LIKE` does: `D89.4%` finds
`D89.4` and everything under it. Words (`tryptase`) are searched in the names
as before. The name of the utility does not matter to the user (5 October
2026).

**Consequences.** Nothing is widened for the user; they say so with `%`, as
in a template's own code lists (`K50.%`).

### D198. Status shows a table green once its run is done

**Context.** In Run's Status, a table's row stays black while its run and
session are green, so a finished SneakPeek session read as unfinished (the HaT
control pull, 5 October 2026).

**Decision.** A table row takes its run's colour once the run is done (green),
and the PK's table its phase's. A table under a run still running or failed is
black, as now, since its rows are what has landed so far.

**Consequences.** Run's window only; nothing a pull does changes.

### D199. The bundle carries the template, the VM's package list, and a walkthrough for developers

**Context.** A developer on the VM is to read the code there and see how a
template becomes SQL (6 October 2026). They see the VM as the whole system,
so what they read must not mention the Mac or anything copied from it. The
template and the VM's package list were not shipped (D49): the VM worked from
blueprints alone.

**Decision.** Every bundle carries `reference/template.yaml`, `reference/DSVM
Plugins.yaml` and `HowThisRepoWorks.md`, each replaced on extraction like the
dictionary. `HowThisRepoWorks.md` is brief. It explains the layers (SQL, a
table as YAML, recipes, the template, YAML Manager, Pullmanager, Scope), then
each step from template to SQL to parquet with the functions that do it. It
says nothing about the Mac, copying, or how the system came to be, and it is
kept true as the code changes.

**Consequences.**

- Reverses the template part of D49. In an extracted bundle, `makeYaml.py`
  without `--template` is refused and names the blueprint to pass. It used
  to say that because no template existed there; now it is checked.
- A bundled path may contain spaces (the package list's does). The
  extractor's file headers read a path up to ` SHA256:`. Each bundle carries
  its own extractor, so older bundles are unaffected.

### D200. For now the bundle says nothing of screenshots or of transcribing them

**Context.** The VM is to be shown to others, and nothing on it may mention
transcribing text from screenshots, nor carry a tool for it (6 October
2026). The transcription viewer (D148, D180), the dictionary's notes on the
screenshots it was checked against, and two docstrings did. The Mac keeps
the viewer; the user will say when it goes back in.

**Decision.**

- The bundle holds back `utils/client/transcription_viewer.py`
  (`HELD_BACK`). It stays on the Mac, where the utilities window and a
  pull's folder still offer it.
- Run's Multi-column view (D180) is removed from the code in a commit of its
  own, since it only opened the viewer. The shipped tests, `HOW_TO.md`, and
  the audit's and scan's docstrings no longer name the viewer or screenshots.
- The bundle's copy of the dictionary drops its comment lines. Every
  shipped YAML has its descriptions' notes reworded: "cut off in the
  screenshot" becomes "not recorded", and "not yet transcribed" becomes "not
  yet recorded" (`SCREENSHOT_REWORDS`). The Mac's dictionary keeps them.
- With `NO_SCREENSHOT_MENTIONS`, a build stops if any file it would carry
  still contains `transcri` or `screenshot`, naming each line, so a new
  mention cannot slip in.

**Consequences.**

- To ship the viewer again:
  - Remove it from `HELD_BACK`.
  - Set `NO_SCREENSHOT_MENTIONS` to False.
  - Revert the commit that removed Multi-column view.
- Pull folders made on the VM before this keep their copy of the viewer. The
  next extraction removes it from the runtime.
- Easy to reverse was chosen over keeping the button: hiding it while the
  viewer was absent would still have named the viewer in shipped code.

### D201. The bundle's prose says nothing of how the software arrives (bundle 1)

**Context.** The VM is to be shown as if the system were built and run
there. Beyond D200, the bundle described its own delivery: comments and
docstrings on bundling, extraction and the Mac. Every window's foot said
`bundle <id>`, and VM-visible messages pointed at the Mac (6 October 2026).
The user: if in doubt, say nothing of how updating is done or how code is
added. The Mac keeps all of it.

**Decision.**

- **Comments and docstrings.** At build time, every comment block and every
  docstring paragraph in shipped Python that mentions bundling, extraction,
  copying over or the Mac is dropped. The result must still compile, or the
  build stops. Shipped Markdown and YAML drop such comment blocks the same
  way.
- **Text a VM user sees** is reworded in the source:
  - the window foot, `scope.py --version`, and the audit's and scan's
    headers say `version <id>`;
  - the messages that send a VM user to the Mac no longer do.
- **The guard** (D200) also stops a build when a comment or docstring would
  still carry those words.
- **Code names stay** (`bundle_id`, `.bundle-manifest.json`), as does the
  Mac-only bundle-building code in Author, which never shows on the VM.
- Built as `dist/bundle1.py`, the fallback for D202.

**Consequences.** Shipped code keeps every comment and docstring paragraph
on other topics, so it still reads as documented code. The Mac's sources are
unchanged except for the reworded visible text.

### D202. The bundle's code names say nothing of bundling either (bundle 2)

**Context.** D201 leaves code names. A developer reading the shipped code
would still meet `bundle_id()`, `stamp_bundle`, `BUNDLE = ""` and
`.bundle-manifest.json`.

**Decision.** Rename them, on the Mac too:
- `bundle_id` → `version_id` and `stamp_bundle` → `stamp_version`;
- each utility's `BUNDLE = ""` → `VERSION = ""`;
- the extraction's record `.bundle-manifest.json` → `.runtime-manifest.json`;
- test names that say bundle or extracted.

The extractor still recognises a folder extracted with the old record name,
reads its hashes, and replaces it with the new one. Only the installer itself
(`bundle.py`, which is deleted after use) still names bundling, and what it
prints is reworded. Author's Mac-only bundle-building code (the Exports
tab's buttons and the queue) is not removed. Built as `dist/bundle2.py`.

**Consequences.**

- If bundle 2 fails on the VM, bundle 1 is the fallback. Extracting bundle 1
  over bundle 2 finds `.runtime-manifest.json`, which bundle 1's extractor
  does not know, so it is refused with a message. Extract bundle 1 into a
  fresh folder, or with `--force`.
- What remains: the Exports tab's code and the `bundle_pullmanager` import in
  Author, Mac-only; and the R package named `bundle` in the package list.
