# Pullmanager Contracts (Phase 0)

This is the contract Pullmanager builds against. Unlike the prose in
`pullmanager_refactor_plan.md`, every schema claim here was verified against
real `scripts/makeYaml.py --export-split` output, not inferred from design
notes. Where the design prose and the generator disagree, the generator wins
and the disagreement is called out.

Regenerate the evidence at any time:

```bash
python3 scripts/makeYaml.py \
  --template YAMLs/manager_test_cases/01_valid_basic.yaml \
  --recipes YAMLs/recipes.yaml \
  --export-split --out-dir QMDs/pullmanager/fixtures/split
```

## Corrections To The Design Prose

These were found by diffing `yamlmanagerDesign.qmd` against real output. Each
one is pinned by a test in `pullmanager/tdd.py` so drift surfaces loudly.

| Design prose says | Generator actually emits |
| --- | --- |
| `source: {preyaml: ...}` | `source: {template: ..., recipes: ...}` |
| Session has `cohort`, `multiplier` | Also `pk_table` |
| `pk_source: {kind: generated}` | Also `table`; uploaded adds `upload_name`, `key_columns` |
| `outputs: {local_table, parquet}` | `outputs: {}`, empty and untyped until Pullmanager fills it |
| Phase example shows `duration` | Not emitted by YAML Manager. Pullmanager adds it |
| Absent optionals shown as `null` | Omitted entirely (`multiplier`, `batch`, `pk_source`) |

## Manifest Schema As Emitted

Top level:

```yaml
manifest_version: 1          # Pullmanager refuses anything else
project:
  name: <str>
  project_folder: <str>
  project_db: <str>          # the Projects database, e.g. PROJECTD33A929
  created_by: yamlmanager
source:
  template: <path>
  recipes: <path>
sessions: [ <session>, ... ]
```

Session:

```yaml
session_id: <stable id>      # unique; used for retry/resume targeting
cohort: <str>
pk_table: <str|null>         # canonical PK table for this session
status: <status>
phases: { setup: <phase>, upload_cohorts: <phase>, pk: <phase> }
runs: [ <run>, ... ]         # always at least one
multiplier: {...}            # omitted when the session is not multiplied
```

Phase and run nodes share the same status/telemetry shape:

```yaml
yaml: <path relative to the manifest's own directory>
status: <status>
started_at: <iso8601|null>
finished_at: <iso8601|null>
rows: <int|null>
outputs: {}
error: <null | {message, detail}>
```

A `pk` phase additionally carries `pk_source`; a run additionally carries
`run_id` and (when batched) `batch`:

```yaml
batch:
  name: LA-Female
  dimensions:                # resolved at plan time, one per static dimension
    - name: state
      kind: column_values
      column: StateOrProvinceAbbreviation
      value: LA
    - {name: sex, kind: column_values, column: Sex, value: Female}
  runtime:                   # Pullmanager must expand these itself
    - {name: chunk, kind: row_chunk, rows_per_batch: 2000, applies_to: PKTable}
```

A bucket standing for "everything not listed" carries `is_other: true` in
place of `value`.

### Phase Names Are Closed

Exactly `setup`, `upload_cohorts`, `pk` — in that order, which is the execution
order. Pullmanager raises on any other phase key rather than silently ignoring
it, so a hand-edited or newer manifest fails loudly instead of skipping work.

### Paths Are Manifest-Relative

`yaml` values resolve against the manifest's own directory, not the process
working directory. Pullmanager never infers execution order from folder names.

## Fields Pullmanager Adds

YAML Manager writes the plan; Pullmanager writes the status. Two fields are
additive (YAML Manager never emits them, and it preserves them on re-read):

```yaml
duration:                    # written on finish/fail, cleared on retry
  seconds: 312
  display: "5m 12s"
note: <str>                  # why something was skipped or blocked
```

`note` exists because `error` would misrepresent a skip. A skipped phase is not
a failure, so its reason must not appear in an `error` block.

Everything else Pullmanager writes goes into fields YAML Manager already
declared: `status`, `started_at`, `finished_at`, `rows`, `outputs`, `error`.

### Unknown Keys Survive

Pullmanager mutates the loaded mapping in place and writes the whole document
back, so any key it does not understand round-trips untouched. This is what
lets YAML Manager add fields without breaking an older bundle on the VM.

### Writes Are Atomic

The manifest is the only record of what completed. It is written to a temp file
and renamed into place, so an interrupted write cannot truncate the record that
resume depends on.

## Status Vocabulary

| Status | Meaning |
| --- | --- |
| `pending` | Planned, not started |
| `running` | Currently executing |
| `done` | Completed successfully |
| `failed` | Attempted and failed |
| `skipped` | Deliberately not run (e.g. no upload cohorts) |
| `blocked` | Cannot run because something upstream failed |

`done` and `skipped` are *settled*: a plain resume does not re-run them.
`failed` and `blocked` are re-runnable once the cause is addressed.

### Session Status Is Derived

A session's status is recomputed from its phases and runs on every save, so it
can never drift from its children:

1. any `failed` → `failed`
2. any `running` → `running`
3. any `blocked` → `blocked`
4. all `skipped` → `skipped`
5. all settled → `done`
6. any settled, some not → `running` (partially complete)
7. otherwise → `pending`

## Naming Contract

Inherited from the old generator; these are invariants, not preferences.
Implementation lands in Phase 3 (`naming.py`), tested there.

| Thing | Rule | Example |
| --- | --- | --- |
| Cosmos global temp | `##JVM_<dest_table>` | `PKTable` → `##JVM_PKTable` |
| ...already prefixed | strip `JVM_` first, never double it | `JVM_PKTable` → `##JVM_PKTable` |
| Projects staging | `#Local_<dest_table>` | `PKTable` → `#Local_PKTable` |
| Projects destination | `<project_db>.dbo.<dest_table>`, always fully qualified | `PROJECTD33A929.dbo.PKTable` |

Destination tables are fully qualified even when the script also issues `USE`,
because generated SQL gets run from tools with ambiguous database context.

## Session Lifetime

Global temp tables live and die with the server connection. Therefore one
connection stays open across `setup` → `upload_cohorts` → `pk` → all runs for a
session, and closes only when the session completes or terminally fails.

The runtime linked-server identity (old `SELECT @@SERVERNAME`) is captured
during `setup` and recorded in that phase's `outputs`, because the real
instance name can differ from any configured value and local `OPENQUERY` SQL
needs the real one.

## Bundle Contract

The VM cannot pull from git, so the runtime ships as one file.

```bash
python3 scripts/bundle_pullmanager.py          # build dist/pullmanager_bundle.py
python  pullmanager_bundle.py --verify-bundle  # on the VM
python  pullmanager_bundle.py --extract ./pullmanager_runtime
python  ./pullmanager_runtime/pullmanager.py split/pullmanifest.yaml
```

Guarantees:

- **Deterministic.** Identical sources produce a byte-identical bundle, so a
  rebuild with no changes leaves git clean and `content_id` identifies the code.
- **Readable.** Payload files are stored as comment-prefixed source, not
  base64, so a file can be read straight out of the bundle and copied back to
  the Mac for repair.
- **Unforgeable markers.** Comment-prefixing means a source line that itself
  looks like a marker encodes to `# # === ...` and no longer matches.
- **Verified both ways.** Every file is checked against the embedded manifest
  for size and SHA-256, and re-hashed after being written to disk.
- **Refuses.** Absolute paths, `..` traversal, Windows drive letters and
  backslashes, duplicate sections, sections missing from the manifest, sections
  absent from the manifest, unterminated sections, and mismatched END markers.
- **All-or-nothing.** Extraction stages to a temp directory and swaps only
  after everything verifies, so a bad bundle leaves the existing runtime intact.
- **Non-destructive by default.** It will replace a previous extraction (one
  containing `.bundle-manifest.json`) but refuses any other directory without
  `--force`.

CRLF sources are rejected at build time rather than silently normalized,
because newline translation would invalidate the hashes on a Windows VM.

## Fixtures

| File | What it shows |
| --- | --- |
| `fixtures/split/` | Real `--export-split` output: manifest plus four phase YAMLs |
| `fixtures/pullmanifest.in-flight.yaml` | The same manifest after a partial run: setup done, uploads skipped, PK done with rows, run failed, session rolled up to `failed` |

The in-flight fixture was produced by driving the real transition API, not
hand-written, so it cannot describe a state the code will not actually write.

## Batch Expansion

Batch dimensions **cross-multiply**. They are not alternative slicings and not
a flat list. Given 200 patients:

```yaml
batching:
  - state:
      values: [LA, MS]
  - sex          # Female, Male
  - chunk: 2000
```

`state` x `sex` yields **4** batches (`LA-Female`, `LA-Male`, `MS-Female`,
`MS-Male`). With four states it would be 8. Every patient lands in exactly one
combination, so the batches partition the cohort rather than re-pulling it.

The unit of multiplication is the **bucket**, not the raw value. A dimension
declaring `include_other: true` contributes one bucket per listed value plus a
catch-all, so `values: [Female] + include_other` is 2 buckets, not 1.

### Two Kinds Of Dimension

| Kind | Buckets known at | Expanded by |
| --- | --- | --- |
| `column_values` | plan time (values are enumerated in the template) | YAML Manager |
| `row_chunk` | run time (depends on actual PK row count) | Pullmanager |

So `chunk: 2000` above is not a third dimension of the product. It subdivides
each of the 4 combinations, and how many chunks each yields is unknowable until
the PK table exists. A combination of 500 patients is one chunk; 5,000 is three.

This is the concrete meaning of "batches stay logical in YAML Manager,
Pullmanager materializes them": the `column_values` product is static and
belongs in the manifest with stable `run_id`s for resume and retry, while
`row_chunk` fan-out is recorded by Pullmanager as it discovers it.

### Generator Support

`scripts/makeYaml.py` implements this in `session_runs()`. `batch_buckets()`
decides whether a dimension can expand at plan time; the static ones go through
`itertools.product`, and the rest are copied into `batch.runtime` untouched.

```text
batching: [state[LA, MS], sex, chunk: 2000]

  ->  runs/LA-Female.yaml   dimensions: state=LA, sex=Female   runtime: [chunk]
      runs/LA-Male.yaml     dimensions: state=LA, sex=Male     runtime: [chunk]
      runs/MS-Female.yaml   dimensions: state=MS, sex=Female   runtime: [chunk]
      runs/MS-Male.yaml     dimensions: state=MS, sex=Male     runtime: [chunk]
```

Run names are the bucket labels joined with `-`. Two dimensions that would
produce the same label are a hard error rather than a silently dropped
combination, since a missing combination means silently unpulled patients.

Covered by `makeYaml.py --tdd batching`.

## Open Questions

Flagged rather than assumed. These need answers before Phases 4-8.
Batch semantics used to head this list; it is settled above.

1. **Multi-step PK.** `inputSimple.yaml` shows a PK built from a prior PK
   (`PKTable` depends on `PKTable2`). The current `pk` phase is a single YAML.
   Ordered PK/support cohorts inside one phase need a representation.
2. **Generated-table dependencies.** Cohorts currently reference other
   generated temps by handwritten name (`#UVM_PKTable2` in the example, which
   the real SQL rendered as `##JVM_PKTable2`). Dependencies should be
   structural so the renderer owns temp names.
3. **Write mode.** The old Projects behavior was drop-and-recreate. Append,
   partition-replace, and resume-safe merge are unspecified.
4. **Legacy `dedup_key`.** The example YAML uses `dedup_key`; the generator
   recognized only `dedup_keys` and silently produced no dedup. Normalize or
   fail loudly — silence changes row counts.

## Edge Cases Owed Tests

Carried forward from `yamlprocessing.md` and `generator_understanding.md`.
Phase-2 items are done; the rest are owed by the phase noted.

Naming and normalization (Phase 3):

- `dest_table` with and without a `JVM_` prefix.
- Missing or blank `dest_table`.
- Boolean-ish `pull_this_cycle` / `push_this_cycle` (`true`, `yes`, `1`).
- Legacy `dedup_key` versus canonical `dedup_keys`.
- Legacy `print_md` versus `printout_md`.
- Duplicate cohort names where `dest_table` differs.
- Table names that are substrings of other table names.

SQL rendering (Phase 4):

- `WHERE` lines already starting with `AND` / `OR`.
- Parenthesized boolean groups split across YAML list items.
- `WHERE` comments.
- Date placeholders `{{min_date_key}}` / `{{max_date_key}}`.
- Placeholder substitution in `from`, `join`, and `where`.
- `filter.from` supplied as a list rather than a string.
- Non-nullable columns forcing `IS NOT NULL` into the filter.
- Duplicate column names; blank `source` expressions.
- Dedup keys that match no generated column; dedup keys with ordering rules.
- Identical column name and order across global temp, local staging, and
  destination insert.

Uploads (Phase 4/7):

- CSV with BOM; empty file; header-only; headers with spaces, punctuation, or
  leading digits.
- SQL string escaping and empty-cell-to-`NULL` conversion.
- Parquet upload requested, which Cosmos cannot read directly.

Execution and telemetry (Phases 6–9):

- Runtime server name differing from the configured one.
- Cohorts disabled by `pull_this_cycle: false`.
- Downstream transfer attempted after upstream temp creation failed.
- Cosmos and Projects row counts captured separately and compared.
- Row counts past the ~80M risk threshold.
- Resume skipping settled work; retry re-running failed work.

## Phase Status

- Phase 0 — Contracts and fixtures: **done** (this document plus `fixtures/`).
- Phase 1 — Bundle infrastructure: **done** (`scripts/bundle_pullmanager.py`,
  `scripts/bundle_extractor.py`, `dist/pullmanager_bundle.py`).
- Phase 2 — Core models and manifest I/O: **done**
  (`pullmanager/models.py`, `pullmanager/manifest.py`).
- Phase 3 — Normalization and naming: next.

## Running The Tests

```bash
python3 scripts/pullmanager_src/pullmanager.py --tdd            # runtime (38)
python3 scripts/pullmanager_src/pullmanager.py --tdd manifest   # one module
python3 scripts/bundle_pullmanager.py --tdd                     # bundler (31)
python3 scripts/bundle_pullmanager.py --tdd tamper              # one class
python3 scripts/makeYaml.py --tdd                               # YAML Manager (35)
```

Pullmanager's suites use stdlib `unittest`, so they stay dependency-free and
run unchanged from an extracted bundle. That is how the VM verifies a delivery:

```bash
python pullmanager_bundle.py --verify-bundle
python pullmanager_bundle.py --extract ./pullmanager_runtime
python ./pullmanager_runtime/pullmanager.py --tdd
```

Test modules are discovered by filename (`pullmanager/tests/test_*.py`), so a
new module needs no registration. `makeYaml.py` keeps its own `--tdd` harness
for now; migrating it is optional and independent.

Tests are about 55% of the shipped source. They are bundled deliberately, so
the VM can prove a copied bundle is sound without network access or a repo.
