# Pullmanager Architecture Decisions

Why things are the way they are. `pullmanager_contracts.md` says what the
contracts *are*; this says what was chosen instead, and what it cost.

Entries are grouped, numbered stably, and never renumbered. A reversed decision
keeps its entry and gains a **Superseded by** line, because the reasoning that
led to the wrong answer is usually the reasoning that will lead there again.

---

## Method

### D1. Ground contracts in real generator output, not design prose

**Context.** The plan and `yamlmanagerDesign.qmd` described the manifest schema
in prose. The obvious start was to build against that description.

**Decision.** Generate real `--export-split` output and build against it.

**Consequences.** Found six schema errors immediately, the largest being
`source: {preyaml}` which is really `source: {template, recipes}`. Every
correction is pinned by a test, so drift fails loudly rather than quietly.

Later it paid again, in the other direction: `yamlprocessing.md` recorded ten
mismatches between `inputSimple.yaml` and the SQL it produced, and comparing
against the real files showed nearly all of them were OCR damage in the
transcriptions. The old generator was faithful. That reversal is what made its
output usable as a specification for Phase 4 instead of a cautionary tale.

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

## Still Open

Recorded so the absence of a decision is visible.

- **Generated-table dependencies.** Cohorts reference other generated temps by
  handwritten name. Should be structural so the renderer owns the names.
- **Multi-step PK.** `inputSimple.yaml` builds a PK from a prior PK; the `pk`
  phase is a single YAML.
- **`values: all` batching.** Refused with an explanation rather than resolved
  at run time, which would change the manifest's run set.
- **Primary and foreign keys in the dictionary.** Relationships exist only as
  prose inside type annotations. See the To Do section of the plan.
- **Phases 6 to 8 have never run against a database.** Orchestration is proven
  against fakes; nothing has touched Cosmos.
