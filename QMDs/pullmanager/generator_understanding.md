# Old Generator Understanding

This note is a first-pass digestion of `old_Generator_analysis.qmd` in light of the newer `QMDs/yamlmanager/yamlmanagerDesign.qmd`. The OCR text may have small transcription errors, so this should be treated as a conceptual map rather than exact line-level documentation.

## What the old stack was doing

The old generator stack had grown into a full supervisor for a pull, not just a SQL renderer. Its responsibilities included:

- Loading and validating the YAML.
- Building `Cosmos.sql`, eventually in-process inside `generator.py`.
- Running `Cosmos.sql` through `pyodbc`.
- Capturing server messages, result sets, row counts, timings, and errors.
- Inferring per-cohort success/failure from Cosmos telemetry.
- Running `makeProjects.py` with the actual Cosmos server name discovered at runtime.
- Running only the relevant pieces of `Projects.sql` for cohorts that were enabled and succeeded on Cosmos.
- Optionally running `makeR`.
- Writing a markdown run report.
- Mirroring generated SQL and R scripts into run-specific folders.

So the old `generator.py` was not a clean generator. It was a CLI parser, config normalizer, SQL builder, database executor, telemetry parser, output packager, and downstream script coordinator all in one.

## High-level pipeline

The old run looked roughly like this:

1. Parse CLI arguments such as YAML path, output paths, dry-run flags, and verbosity.
2. Load YAML with `yaml.safe_load`.
3. Resolve project/run/output paths.
4. Normalize upload cohorts and pull cohorts.
5. Build one large `Cosmos.sql`.
6. Write `Cosmos.sql` into a `QueryScripts` location, usually under a run output folder.
7. Execute `Cosmos.sql` batch by batch, splitting on `GO`.
8. Capture Cosmos telemetry, including the actual `@@SERVERNAME`.
9. Decide which cohorts succeeded.
10. Run `makeProjects.py`, passing the runtime Cosmos instance so `OPENQUERY` points at the right server.
11. Execute `Projects.sql` one destination table at a time, gated by YAML `pull_this_cycle` and Cosmos success.
12. Summarize Projects-side tables and upload tables.
13. Optionally run R script generation for successful cohorts.
14. Optionally write `generator_output.md`.

The key old assumption was: one pull produces a combined Cosmos script and a combined Projects script, then Python tries to orchestrate and partially execute those scripts safely.

The new design breaks that assumption. YAML Manager now plans split artifacts, and Pullmanager should execute manifest phases. The old "one whole Cosmos.sql and one whole Projects.sql, then split execution by table" behavior should become obsolete.

## SQL generation model

The old stack generated two sides of SQL.

### Cosmos side

Cosmos SQL created global temp tables named with the `##JVM_<base>` convention. If the YAML destination table already started with `JVM_`, the prefix was stripped before adding `##JVM_`, avoiding names like `##JVM_JVM_Foo`.

For each cohort, Cosmos generation:

- Built a table schema from YAML columns.
- Built a select projection from YAML source expressions.
- Built the `FROM`/`JOIN`/`WHERE` fragment.
- Substituted placeholders such as `{cosmos_server}`, `{cosmos_db}`, `{cosmos_schema}`, and date keys.
- Added `IS NOT NULL` filters for columns declared `nullable: false`.
- Optionally applied `TOP` limits for small/test pulls.
- Optionally randomized PK sampling with `ORDER BY NEWID()`.
- Optionally deduplicated rows through staging tables.
- Inserted final rows into the global temp table.
- Emitted telemetry SELECTs.

Earlier `makeCosmos.py` wrote a more vertical, `GO`-separated script:

- Header and server-name discovery.
- Upload switch/upload blocks.
- Pull switch/schema blocks.
- Execution blocks/final temp list.

The later in-process generator version added more defensive validation and more telemetry.

### Projects side

Projects SQL was mostly transfer SQL:

- Drop and recreate the destination table in the Projects database.
- Pull rows from the Cosmos global temp through `OPENQUERY([cosmos_instance], ...)`.
- Stage into a local temp table.
- Insert into the final Projects table.
- Print counts, sample rows, and timing messages.

Important detail: `makeProjects.py` generated a combined `Projects.sql`, but the old Python supervisor did not simply execute the whole file. It searched for SQL blocks mentioning `dbo.<dest_table>` or `[dbo].[<dest_table>]` and executed only the blocks for enabled/successful cohorts. This table-name substring matching was useful but fragile.

## Behavior that is obsolete under the new design

These old behaviors seem like good candidates to remove or avoid recreating:

- Global `--verbosity` / `-vv` plumbing as a core design feature.
- Large markdown run reports as the primary status artifact.
- Hard-coded fixed run telemetry as the main progress model.
- One giant `Cosmos.sql` and one giant `Projects.sql` per run.
- Python post-processing that splits a combined Projects file by destination table.
- Treating generator as the overall pipeline supervisor.
- Optional run-folder mirroring as a substitute for a manifest-defined artifact folder.
- `makeR` as a step embedded inside the core pull execution path, unless it has a clear new phase.
- Time reporting that is only printed/logged rather than written into a mutable status document.

In the new design, manifest status should replace much of the old console/report telemetry. Pullmanager should own start/end timestamps, row counts, failure states, retry/resume state, and execution order.

## Behaviors worth preserving

Some of the old logic looks like it represents lessons learned the hard way. These are worth intentionally preserving in the refactor, even if implemented in a cleaner place.

### Path handling

- Accept absolute or relative YAML paths.
- Resolve relative paths from a predictable root.
- Produce explicit errors for missing files.
- Normalize odd Unicode separators/control characters in user-provided path fragments.
- Keep output folders deterministic and inspectable.

### YAML validation

- PK cohorts must have enough filter/source information to build a PK table, unless the session uses an uploaded PK.
- Cohort column names must be present and unique.
- `dedup_keys` should be list-of-lists and non-empty when supplied.
- Invalid dedup config should produce a clear error or an explicit fallback, not silent broken SQL.
- Boolean-ish fields like `pull_this_cycle` and `push_this_cycle` should accept common YAML/user forms (`true`, `yes`, `1`, etc.).

### Filter construction

The old `WHERE` builder handled awkward but important cases:

- Automatically adding `AND` where appropriate.
- Not adding `AND` before lines that already begin with `AND`/`OR`.
- Not corrupting parenthesized expressions, comments, or YAML-coded literal/list-like fragments.
- Replacing `{{min_date_key}}` and `{{max_date_key}}`.
- Applying placeholder substitution in `from`, `join`, and `where`.
- Adding `source_expr IS NOT NULL` for required columns.

This is an edge-case-heavy area and should be tested directly.

### Dedup handling

The old dedup path used staging tables:

- `#Keys_<dest>`
- `#Cohort_<dest>`
- `#Cohort_<dest>_Dedup`

It selected distinct key combinations, chose one candidate row per key set, and inserted deduplicated rows into the final global temp. The later generator also checked whether dedup keys actually matched column names and could fall back to non-dedup staging with a clear comment.

The exact SQL may change, but the behaviors matter:

- Dedup must be explicit.
- Dedup keys must be validated against generated columns/source aliases.
- Order rules must be deterministic when supplied.
- Fallback should be visible.

### Upload cohorts

Upload support had several important modes:

- `dbtable`: read an existing Projects table into a Cosmos global temp through `OPENQUERY(PROJECTS, ...)`.
- `csv`: later generator version inferred columns from the header, normalized SQL identifiers, and inserted inline SQL rows.
- `parquet`: documented as unsupported directly by Cosmos, with guidance to load into Projects or otherwise materialize it.

CSV handling in the newer generator is especially worth keeping:

- Hard fail if the CSV is missing.
- Resolve paths relative to project root if not absolute.
- Read BOM-safe UTF-8.
- Normalize headers into SQL-safe identifiers.
- Handle empty files/headers intentionally.
- Escape string values and convert empty cells to `NULL`.

Older `makeCosmos.py` treated CSV as a one-column diagnosis-code list. That may still matter as a recipe-specific shorthand, but it should not be confused with general CSV upload support.

### Runtime Cosmos instance discovery

The old pipeline queried `SELECT @@SERVERNAME AS CosmosServerName` after running Cosmos SQL and passed that value into `makeProjects.py`. This mattered because the actual linked server name could differ from a YAML/default name.

In the new split/session model, Pullmanager probably still needs a version of this behavior. Any server-side session that creates global temps should record the actual server/connection identity used for later phase SQL.

### Execution gating

The old stack had two levels of gating:

- SQL variables such as `@Pull<dest>` and `@Push<dest>`.
- Python-side gating based on `pull_this_cycle`, `push_this_cycle`, and observed Cosmos success.

The new design should avoid duplicated gating if possible, but the semantics still matter:

- Disabled pulls should not run.
- Disabled uploads should not run.
- Downstream transfer should not run if upstream global temp creation failed.
- Failure should mark dependent steps as failed/skipped/blocked in the manifest.

### Telemetry and row counts

Old telemetry captured:

- Source raw counts.
- Stage counts.
- Dedup counts.
- Final cohort row counts.
- Cosmos per-table durations.
- Projects per-table row counts.
- Projects table columns.
- Upload table row counts/columns.
- Very large row-count warnings around an 80 million row cutoff.

The new manifest does not need to preserve all printed messages, but Pullmanager should preserve enough structured telemetry to answer:

- Did this phase run?
- What table did it create?
- How many rows landed?
- Were rows filtered/deduped?
- Did the row count exceed a risk threshold?
- What failed and where?

- How long did it take to finish each table?

### Error handling

Old guardrails worth carrying forward:

- Fail fast on Cosmos SQL execution errors.
- Include server messages in error details, especially for nested `OPENQUERY` failures.
- Hard fail on missing required SQL/YAML/CSV files.
- Put reasonable timeouts on local Projects connections.
- Close database connections explicitly.
- Treat downstream optional artifact generation failures as non-fatal only if that is a deliberate phase policy.

## How this maps to the new Pullmanager design

The new `QMDs/yamlmanager/yamlmanagerDesign.qmd` changes the ownership model:

- YAML Manager owns authoring, validation, pre-YAML export, split planning, split YAML writing, and manifest creation.
- Pullmanager owns reading the manifest, keeping session connections open, executing setup/upload/PK/run YAMLs, materializing batches, updating status, and resuming/retrying.
- `makeServer`/`makeLocal`/generator-like pieces should become SQL renderers or execution helpers, not the overall supervisor.
- `makeArtifacts` owns parquet export and `parquetcontents.yaml`, based on actual completed data.

The old generator should therefore be decomposed into smaller concepts:

- Config/domain validation.
- SQL rendering for server phases.
- SQL rendering for local transfer phases.
- Upload materialization helpers.
- Execution adapters.
- Telemetry parsers.
- Manifest status updater.
- Artifact/report writer.

The most important conceptual replacement is:

Old model:

```text
YAML -> giant Cosmos.sql -> run all Cosmos -> giant Projects.sql -> selectively run table blocks -> markdown report
```

New model:

```text
pre-YAML -> split folder + pullmanifest.yaml -> Pullmanager executes setup/upload/PK/run phases -> manifest status + real local tables -> makeArtifacts
```

## Likely edge cases to remember during refactor

- Destination tables with and without the `JVM_` prefix.
- Cohorts with `pull_this_cycle: false`.
- Upload cohorts with `push_this_cycle: false`.
- Missing or blank `dest_table`.
- Duplicate column names.
- Columns with blank `source` expressions.
- Non-null columns whose source expression must be added to the `WHERE`.
- `WHERE` lines that already include `AND`/`OR`.
- `WHERE` comments.
- Parenthesized/list-like `WHERE` fragments that should not receive automatic `AND`.
- Date-key placeholders in filters.
- Placeholder substitution in joins.
- `filter.from` supplied as a list.
- Dedup keys that do not match generated columns.
- Dedup keys with ordering rules.
- PK cohort without a valid filter.
- Uploaded PK cohort replacing generated PK behavior.
- Multiple uploaded PK cohorts marked `type: pk`.
- CSV upload with BOM.
- CSV upload with empty file, empty header, or no data rows.
- CSV headers with spaces/punctuation/leading digits.
- SQL string escaping for CSV values.
- Parquet upload requested even though Cosmos cannot directly read it.
- Very large result sets that should produce warnings.
- Runtime Cosmos server name differing from the configured name.
- Table names that are substrings of other table names, which made old Projects block matching fragile.

## Refactor stance

The legacy code contains a lot of behavior that should not survive as architecture, but some of it should survive as tests and contracts. The main challenge is to avoid accidentally deleting battle-tested flexibility while stripping the old supervisor shape.

The safest approach is to convert the old lessons into explicit unit/fixture tests around:

- YAML normalization.
- Filter rendering.
- Dedup validation/rendering.
- Upload cohort handling.
- Split session/run planning.
- Manifest status transitions.
- SQL execution telemetry parsing.

Once those behaviors are captured, the old combined-script pipeline can be replaced without needing to keep its coupling.
