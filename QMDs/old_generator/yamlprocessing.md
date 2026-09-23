# YAML Processing Notes

> **Retraction, and why this document is now mostly about one real bug.**
>
> This note was written against transcriptions of `inputSimple.yaml`,
> `examplecos.sql` and `exampleproj.sql` that carried OCR damage. Comparing
> them against the real files showed that nearly every "mismatch" recorded
> here was a transcription artifact, not generator behavior. The fixtures have
> since been corrected. Retracted findings:
>
> | Was recorded as | Actually |
> | --- | --- |
> | `USE COSMOS` vs `cosmos_db: COSMOS_Sneak` | YAML says `COSMOS`. No mismatch. |
> | `TOP (500)` vs `stop_at_for_pk_table: 50` | YAML says `500`. No mismatch. |
> | `min_date_key` 20210101 rendered as 20200101 | YAML says `20200101`. Substitution is correct. |
> | YAML `IsDeleted` rewritten to `_IsDeleted` | YAML says `_IsDeleted`. No column rewriting exists. |
> | `p.Type` silently corrected to `dt.Type`, `ICD-10-CA` added | YAML says `dt.Type` with all three codes. No correction. |
> | Exact `K50` dropped; `K51` became a wildcard-less `LIKE` | The full four-term group is preserved verbatim. |
> | `print_md` as a legacy option name | The option is `printout_md`. The legacy-name problem does not exist. |
> | `#UVM_` as a possible legacy temp prefix | OCR damage. `##JVM_` is the only prefix. |
> | Runtime server discovery commented out | `SELECT @@SERVERNAME` is live in the real script. |
> | `exampleproj.sql` unrelated to this YAML | Replaced with the real matching script. |
>
> What survives is the dedup finding below, which is real and confirmed in two
> places in the generated SQL. The renderer analysis (grouped `WHERE` handling,
> non-null injection, placeholder substitution, staging pattern, dual row
> counts) also stands, and is now better supported: the generator turned out to
> be faithful, so its output is a usable specification.
>
> Current contracts live in `pullmanager_contracts.md`.


This note analyzes `inputSimple.yaml` and the currently available example SQL files in `QMDs/pullmanager/`.

At the time of the first read, both `examplecos.sql` and `exampleproj.sql` were empty. Both have now been populated/analyzed, with `exampleproj.sql` treated as a structural specimen from another project rather than a matching pair for `inputSimple.yaml`.

## File status

- `inputSimple.yaml`: parseable YAML.
- `examplecos.sql`: populated Cosmos-side SQL.
- `exampleproj.sql`: populated Projects-side transfer snippet from another project.

Because the Projects SQL is from another project, it should not be compared field-by-field against `inputSimple.yaml`. It is useful for:

- YAML columns to local table schema.
- Projects-side `OPENQUERY`.
- Destination table creation and insert behavior.
- Runtime Cosmos instance handling.

## What the example YAML represents

The simplified YAML defines:

- A Projects database: `PROJECTD93A5E7`.
- A Cosmos database: `COSMOS_Sneak`.
- A date-key window from `20210101` through `20260601`.
- Test options requesting a small PK pull with `stop_at_for_pk_table: 50`.
- A run output folder named `Example Run`.
- A parquet storage folder named `data`.
- Two cohorts, both named `PKTable`, with different destination tables.

The two cohorts appear to form a chained PK workflow:

1. `PKTable2`
   - Type: `PK`.
   - Key: `PatientDurableKey`.
   - Source: `PatientDim AS p`.
   - Filters current, undeleted, Cosmos-analytics-usable patients.
   - Output intent: a patient key set.

2. `PKTable`
   - Type: `PK`.
   - Key: `DiagnosisEventKey`.
   - Source: `DiagnosisEventFact AS def`.
   - Joins `DiagnosisTerminologyDim AS dt`.
   - Joins the previous patient key set as `p`.
   - Filters diagnosis event rows to the date window and ICD-10 K50/K51 diagnosis codes.
   - Output intent: diagnosis-event-level PK rows for IBD events among the previously filtered patient population.

Conceptually, this is not just "one PK table." It is a staged cohort definition:

```text
PatientDim filter -> patient key table -> diagnosis event filter -> diagnosis-event PK table
```

That staged dependency is important for the refactor. The new split/manifest model needs a way to represent "this phase/table depends on a previous generated table in the same session."

## Notable YAML details

### Duplicate cohort names

Both cohorts use `name: PKTable`, while their `dest_table` values differ (`PKTable2` and `PKTable`).

The old system likely keyed much practical behavior off `dest_table`, not `name`. In the new design, `name` collisions should be considered:

- Either allowed but non-identifying.
- Or warned/error if the UI expects names to be stable display IDs.

For manifest/session IDs, `dest_table` or explicit stable IDs are safer than `name` alone.

### `dedup_key` versus `dedup_keys` (CONFIRMED REAL)

The example uses:

```yaml
dedup_key: [DiagnosisEventKey]
```

The old-generator analysis describes newer logic expecting:

```yaml
dedup_keys:
  - [DiagnosisEventKey]
```

This may be OCR drift, older schema syntax, or a real legacy shorthand. It is exactly the sort of compatibility trap worth preserving intentionally.

Recommended behavior:

- Accept `dedup_key` as a legacy shorthand if it is common in old templates.
- Normalize it internally to `dedup_keys: [[...]]`.
- Emit a warning or migration note.
- Keep canonical output using `dedup_keys`.

### `printout_md` (retracted finding)

The example uses:

```yaml
test_options:
  print_md: true
```

The old analysis says the generator read:

```yaml
test_options.printout_md
```

This is another likely legacy/schema mismatch.

Because markdown run reports are now less central, this may not matter operationally. But if old templates are imported, YAML Manager should either normalize both names or clearly report that `print_md` is legacy/ignored.

### `##JVM_PKTable2` join target (retracted finding)

The second cohort joins:

```sql
INNER JOIN #UVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey
```

The old-generator analysis says global temp tables were generated as:

```text
##JVM_<base>
```

So `#UVM_PKTable2` is suspicious. Possibilities:

- OCR error from `##JVM_PKTable2`.
- A legacy temp-table naming convention.
- A local/session temp table used before the later global-temp convention.
- A deliberate different phase/table namespace.

This should be checked against the real generated SQL once `examplecos.sql` is populated.

Refactor implication: table references should not be hand-maintained strings if possible. If a cohort depends on another generated cohort/table, the dependency should be declared structurally and rendered through the same temp-name function.

### Date placeholders

The second cohort uses:

```yaml
def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}
```

The old generator replaced these placeholders centrally. This behavior should remain. It is a clean authoring convenience and reduces copy/paste date mistakes.

### WHERE list as line fragments

The diagnosis-code filter is split across YAML lines:

```yaml
- "( dt.Value LIKE 'K50.%'"
- "  OR dt.Value = 'K50'"
- "  OR dt.Value LIKE 'K51.%'"
- "  OR dt.Value = 'K51'"
- ")"
```

This demonstrates why the old auto-`AND` logic was more complicated than it first appears. The renderer must preserve grouped boolean fragments and avoid turning this into invalid SQL by inserting `AND` before each line.

This is a high-priority test case for the new SQL renderer.

### Empty join list

The first cohort has:

```yaml
join: []
```

The renderer should treat this as normal and emit no join lines. This should be distinct from a missing `join` key, which should probably also default to no joins.

### `filter.from` as a string

The old analysis says the renderer also tolerated `filter.from` as a list and used the first item. This example uses a string. The refactor should decide whether list support is still useful or only a legacy import behavior.

## What SQL generation would likely do

Assuming the old generator behavior, the first cohort would likely render to:

- A global temp table for `PKTable2`, probably `##JVM_PKTable2`.
- A schema with one non-null `BIGINT` column: `PatientDurableKey`.
- A `SELECT TOP (50)` because:
  - cohort type is `PK`,
  - `test_options.smallest` is true,
  - `stop_at_for_pk_table` is 50.
- `WHERE` conditions:
  - `p.IsDeleted = 0`
  - `p.IsCurrent = 1`
  - `p.UseInCosmosAnalytics_X = 1`
  - plus generated `p.DurableKey IS NOT NULL` because `PatientDurableKey` is non-nullable.

The second cohort would likely render to:

- A global temp table for `PKTable`, probably `##JVM_PKTable`.
- A schema with six columns.
- No `TOP` limit if the old logic treats `stop_at_for_non_pk_tables` only for non-PK tables and this is also `type: PK`; however, because this second cohort is also typed `PK`, old logic may also apply `stop_at_for_pk_table: 50`.
- A join to diagnosis terminology.
- A join to the staged patient key table.
- Date placeholder replacement.
- Added non-null filters for:
  - `def.DiagnosisEventKey`
  - `def.PatientDurableKey`
  - `def.EncounterKey`
  - `def.StartDateKey`
- Optional dedup by `DiagnosisEventKey`, if `dedup_key` is recognized or normalized.

That last point matters: if the real generator only recognizes `dedup_keys`, this example may silently run without dedup. That should be tested.

## What `examplecos.sql` actually does

The populated `examplecos.sql` is a single Cosmos-side script with:

- `USE COSMOS`.
- Commented-out `SELECT @@SERVERNAME AS CosmosServerName`.
- No `GO` batch separators.
- No upload cohorts or upload switches.
- Pull switches:
  - `@PullPKTable2 = 1`
  - `@PullPKTable = 1`
- Schema creation for:
  - `##JVM_PKTable2`
  - `##JVM_PKTable`
- One execution block per destination table.
- Source raw row-count telemetry for each cohort.
- Final row-count/duration telemetry for each cohort.
- A final reference SELECT listing `##JVM_PKTable2, ##JVM_PKTable`.

This confirms that the actual temp table convention in this example is `##JVM_`, not `#UVM_`.

## Concrete YAML to Cosmos SQL comparison

### Database selection (retracted finding)

YAML says:

```yaml
cosmos_db: COSMOS_Sneak
```

SQL says:

```sql
USE COSMOS;
```

This is a mismatch. It may be OCR drift, generator defaulting, or manual SQL editing. For the refactor, database selection should be a directly testable rendering rule because running against the wrong Cosmos database is high-impact.

### Runtime server discovery

The SQL contains:

```sql
-- SELECT @@SERVERNAME AS CosmosServerName;
```

The old generator analysis says runtime server discovery mattered for Projects-side `OPENQUERY`. In this example it is commented out, which means the script itself would not emit that value unless the runner adds its own query.

Pullmanager should decide where server identity is captured:

- Either server setup always records the connection/server identity.
- Or generated setup SQL emits it as structured telemetry.

It should not be an accidental commented line.

### Row limits (retracted finding)

YAML says:

```yaml
smallest: true
stop_at_for_pk_table: 50
stop_at_for_non_pk_tables: 0
```

SQL uses:

```sql
SELECT TOP (500)
```

for both `PKTable2` and `PKTable`.

This is a concrete mismatch. It could be manual editing, an older fixture, or a generator default. The important lesson is that row-limit behavior needs tests covering:

- PK table limit.
- Non-PK table limit.
- Multiple PK-like cohorts in a single YAML.
- Whether support PK tables and canonical PK tables are both limited.

### Date placeholders

YAML says:

```yaml
min_date_key: 20210101
max_date_key: 20260601
```

and:

```yaml
def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}
```

SQL renders:

```sql
def.StartDateKey BETWEEN 20200101 AND 20260601
```

The max date matches; the min date does not. This again points to either manual edits or stale generator state. Date placeholder substitution should be validated by tests because it is easy to miss in visual review.

### Deletion column names (retracted finding)

YAML uses:

```yaml
p.IsDeleted = 0
def.IsDeleted = 0
dt.IsDeleted = 0
```

SQL uses:

```sql
p._IsDeleted = 0
def._IsDeleted = 0
dt._IsDeleted = 0
```

This may be an intentional schema correction, but it is not a direct rendering of the YAML. If this kind of column-name normalization exists, it should be explicit. If not, this is a sign the SQL example was manually adjusted.

### Diagnosis terminology type filter (retracted finding)

YAML says:

```yaml
p.Type IN ('ICD-10-AM', 'ICD-10-CM')
```

SQL says:

```sql
dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM')
```

This looks like the SQL corrected a likely YAML typo (`p.Type` should probably be `dt.Type`) and added `ICD-10-CA`. That is a useful real-world flexibility issue: users may need recipe defaults or schema-aware validation that catches "alias exists but column probably belongs to another joined table" style mistakes.

The refactor should not silently rewrite arbitrary aliases. It should either:

- Render exactly what the YAML says.
- Or perform explicit recipe/schema validation and report a correction.

### Diagnosis code value filter (retracted finding)

YAML says:

```yaml
(
  dt.Value LIKE 'K50.%'
  OR dt.Value = 'K50'
  OR dt.Value LIKE 'K51.%'
  OR dt.Value = 'K51'
)
```

SQL says:

```sql
AND (
    dt.Value LIKE 'K50.%'
    OR dt.Value LIKE 'K51.%'
    OR dt.Value LIKE 'K51'
)
```

Differences:

- Exact `K50` was dropped.
- Exact `K51` became `LIKE 'K51'`, which is equivalent to `= 'K51'` without wildcards but stylistically odd.
- The parenthesized structure was preserved correctly.

This confirms the old concern about grouped WHERE fragments, but it also shows that generation/manual editing can alter filter semantics. Code-list predicates should be especially carefully tested.

### Non-null filters

The generated SQL adds non-null checks as expected:

```sql
p.DurableKey IS NOT NULL
def.DiagnosisEventKey IS NOT NULL
def.PatientDurableKey IS NOT NULL
def.EncounterKey IS NOT NULL
def.StartDateKey IS NOT NULL
```

This matches the old generator behavior for `nullable: false` columns and should be retained.

### Dependency temp table naming (retracted finding)

YAML references:

```sql
INNER JOIN #UVM_PKTable2 AS p
```

SQL renders:

```sql
INNER JOIN ##JVM_PKTable2 AS p
```

This confirms that the YAML dependency text was not carried through literally, or that the SQL was manually corrected. The new design should avoid requiring authors to hand-type generated temp table names. Dependencies between generated cohorts should be structural.

Suggested model:

```yaml
join_generated:
  cohort: PKTable2
  alias: p
  on: p.PatientDurableKey = def.PatientDurableKey
```

The exact syntax can differ, but the core idea is that the renderer should own generated temp-table names.

### Dedup behavior (CONFIRMED REAL)

YAML contains:

```yaml
dedup_key: [DiagnosisEventKey]
```

SQL says:

```sql
-- No dedup keys configured; using raw staged cohort rows as-is.
```

There is no dedup staging. This strongly suggests `dedup_key` was not recognized by the generator that produced this SQL, or dedup was manually removed.

This is a high-value migration issue. If old templates use `dedup_key`, the new system should normalize it or fail loudly. Silent "no dedup" can change output row counts and downstream analysis.

### Telemetry

The SQL emits two forms of telemetry:

- Source raw counts with `CountType = 'source raw'`.
- Final cohort row counts plus start/end/duration fields.

It does not emit stage count or dedup count telemetry because no dedup path is used.

The row-count aliases are destination-specific:

```sql
[PKTable2RowCount]
[PKTableRowCount]
```

The final count uses:

```sql
COUNT(*) AS CohortRowCount
```

Pullmanager should prefer a structured telemetry contract over scraping arbitrary column aliases, but this example shows the useful minimum:

- `CohortName`
- `DestTable`
- `CountType`, when relevant
- row count
- start/end time
- duration

### Script structure

This SQL is not the older four-batch `GO`-separated form described for `makeCosmos.py`. It is closer to the later in-process generator output:

- switches first,
- schemas next,
- execution blocks next,
- final temp list last.

That supports the design decision to stop thinking of "Cosmos.sql" as a monolith and instead generate phase-specific SQL under Pullmanager control.

## What this implies for the new split model

This simple YAML is useful because it exposes a missing nuance in a simple setup/upload/PK/run story: some "PK work" can itself be multi-step.

The new Pullmanager flow says each session has:

1. `setup.yaml`
2. `upload_cohorts.yaml`
3. `pk.yaml`
4. one or more run YAMLs

This example suggests `pk.yaml` may need to support a small internal plan, or the split plan may need multiple PK/setup-support tables before the canonical session PK is registered.

Possible interpretations:

- `PKTable2` is a supporting server-side cohort used only to build the final PK.
- `PKTable` is the canonical PK table for the session.
- Both are PK-like outputs, but only one should become the manifest `pk_source`/canonical PK.

Recommended design direction:

- Allow a session PK phase to contain ordered PK/support cohorts.
- Mark exactly one output as the canonical PK table for batching/run phases.
- Treat earlier PK/support outputs as dependencies, not as separate user-facing runs unless desired.
- Make dependency edges explicit in the split plan/manifest.

## Compatibility and migration checks to add

This YAML suggests the import/validation layer should check for:

- Legacy `dedup_key` and canonical `dedup_keys`.
- Legacy `print_md` and canonical `printout_md`, or a new reporting option name.
- Duplicate cohort names.
- Multiple `type: PK` cohorts in one session.
- Which `type: PK` cohort is canonical.
- Joins that refer to generated temp tables by handwritten names.
- Temp-table names that use old prefixes such as `#UVM_`.
- `WHERE` fragments that form parenthesized boolean groups.
- Date placeholders in SQL strings.
- Non-nullable columns that need filter enforcement.

## Suggested tests from this example

1. YAML parsing preserves two cohorts and their order.
2. Duplicate cohort names do not break planning if `dest_table` is unique.
3. `dedup_key: [DiagnosisEventKey]` is normalized or warned.
4. `print_md` is normalized or warned.
5. First cohort renders with no joins.
6. First cohort adds non-null condition for `p.DurableKey`.
7. Second cohort preserves the grouped K50/K51 `WHERE` expression without bad `AND` insertion.
8. Date placeholders are replaced.
9. The dependency from `PKTable` to `PKTable2` is detected or declared.
10. Canonical PK selection is explicit when multiple PK cohorts exist.

## Projects SQL specimen analysis

`exampleproj.sql` is not a matching Projects file for `inputSimple.yaml`; it is a snippet from another project. Therefore this section ignores table/column/project-name differences and focuses on the transfer pattern.

The snippet is still useful because it shows the core Projects-side lifecycle:

1. Switch to the Projects database.
2. Drop the destination Projects table.
3. Recreate the destination Projects table with the generated schema.
4. Drop a local temp staging table.
5. Use `OPENQUERY` to pull rows from the Cosmos global temp table into the local temp table.
6. Insert rows from the local temp table into the final Projects table.
7. Query Cosmos-side row count through `OPENQUERY`.
8. Query Projects-side row count directly.
9. Select one sample Projects-side row.
10. Print end time and duration.

### Database and destination table

The snippet starts with:

```sql
USE PROJECTD33A929;
```

and then operates on a fully-qualified destination table:

```sql
PROJECTD33A929.dbo.blkCrohnsPatients_SP
```

This tells us the generator did not rely only on `USE`; it still qualified the destination table with database and schema. That is a useful safety pattern when generated SQL may be run from tools with ambiguous database context.

The destination table is dropped and recreated:

```sql
DROP TABLE IF EXISTS PROJECTD33A929.dbo.blkCrohnsPatients_SP;

CREATE TABLE PROJECTD33A929.dbo.blkCrohnsPatients_SP
(
    ...
);
```

This confirms the old Projects-side behavior: schema alignment is destructive. A run replaces the destination table rather than appending into an existing table.

Refactor implication: Pullmanager should make this lifecycle explicit. For each local table, the phase should know whether it is:

- drop-and-recreate,
- append,
- replace partition/batch,
- or resume/retry-safe merge.

The old behavior is replace-table.

### Local staging table

Rows are first pulled into a session-local temp table:

```sql
DROP TABLE IF EXISTS #Local_blkCrohnsPatients_SP;

SELECT ...
INTO #Local_blkCrohnsPatients_SP
FROM OPENQUERY(...);
```

Then inserted:

```sql
INSERT INTO PROJECTD33A929.dbo.blkCrohnsPatients_SP (...)
SELECT ...
FROM #Local_blkCrohnsPatients_SP;
```

This two-step pattern matters. The generator does not directly `INSERT INTO final SELECT ... FROM OPENQUERY`. It stages the result locally first, then inserts into the typed destination table.

Likely reasons this existed:

- Keeps the remote `OPENQUERY` pull separate from local insert.
- Makes row-count/sample/debugging easier.
- Allows Projects SQL Server to infer the local temp table result set from the remote query.
- Gives a place to inspect transfer contents before final insert if needed.

Refactor implication: this should remain available as a local transfer strategy, even if later optimized.

### Cosmos global temp table reference

The remote source is:

```sql
FROM ##JVM_blkCrohnsPatients_SP
```

inside the linked-server query string.

This matches the Cosmos-side `##JVM_<dest>` convention. The Projects script assumes the Cosmos global temp table still exists on the linked server at transfer time.

That is central to the new session design:

- Pullmanager must keep the server session/connection context alive for phases that depend on global temp tables.
- Projects transfer must happen before those global temps disappear.
- The manifest/session model should treat `##JVM_<dest>` as a session-scoped output, not a durable table.

### Linked server / runtime Cosmos instance

The `OPENQUERY` target is a concrete server name:

```sql
OPENQUERY(
    [et4003vpdsq1032],
    ' ... '
)
```

This is exactly why the old generator captured `@@SERVERNAME` from Cosmos and fed it into `makeProjects.py`: Projects needs the actual linked server name that can see the session's global temp tables.

Refactor implication:

- Do not hardcode this in YAML if the runtime server may vary.
- Pullmanager should record the runtime Cosmos linked server/instance in session state.
- Local SQL rendering should receive that runtime value from Pullmanager.
- The manifest may need a runtime field such as `server_instance` or `linked_server` after setup.

### Column contract

The same ordered column list appears in three places:

- Destination `CREATE TABLE`.
- Remote `SELECT ... FROM ##JVM_*`.
- Final `INSERT INTO ... SELECT ... FROM #Local_*`.

This repeated list is the transfer contract. Column order and names must match exactly across Cosmos global temp, local temp, and Projects destination.

Refactor implication:

- Column definitions should be represented once in the domain model.
- Server SQL and local SQL should render from the same normalized column list.
- Tests should compare column names/order across generated server and local scripts.

This is also why stringly independent generators are risky: a column rename or ordering difference can break transfer even when each script looks reasonable in isolation.

### Row-count telemetry

The snippet captures Cosmos-side count by querying the remote global temp:

```sql
SELECT
    'blkCrohnsPatients_SP' AS CohortName,
    COUNT(*) AS CosmosRowCount
FROM OPENQUERY(
    [et4003vpdsq1032],
    '
        SELECT
            1 AS dummy
        FROM ##JVM_blkCrohnsPatients_SP
    '
);
```

The inner query returns one dummy row per source row, and the outer query counts them. This avoids pulling all actual data again just to count rows.

It then captures Projects-side count:

```sql
SELECT
    'blkCrohnsPatients_SP' AS TableName,
    COUNT(*) AS blkCrohnsPatients_SPRowCount
FROM PROJECTD33A929.dbo.blkCrohnsPatients_SP;
```

This dual count is valuable:

- Cosmos count tells how many rows were available in the global temp.
- Projects count tells how many rows landed locally.
- A mismatch indicates transfer or insert trouble.

Refactor implication: Pullmanager should store both counts when possible and compare them.

### Sample row telemetry

The snippet selects:

```sql
SELECT TOP (1) *
FROM PROJECTD33A929.dbo.blkCrohnsPatients_SP;
```

This was likely a debugging convenience for markdown/log output. It should probably not become a core status requirement, but it can be useful in verbose diagnostics or a run report.

### Timing output

The snippet ends with:

```sql
DECLARE @EndTime_blkCrohnsPatients_SP DATETIME2 = SYSDATETIME();
PRINT 'Projects cohort end: ...';
...
PRINT 'Projects cohort duration ...';
```

It references `@StartTime_blkCrohnsPatients_SP`, which is not declared in the snippet. Because the user said this is a single snippet from another file, that is probably just above the copied section.

Refactor implication: timing should move out of ad hoc `PRINT` text and into manifest status fields:

- `started_at`
- `finished_at`
- `duration_seconds`
- `rows`
- optional raw messages/log path

SQL-level timing can still exist, but Pullmanager should own the authoritative phase/run status.

### No `GO` separators

The snippet contains no `GO`. It appears intended to run as one block for a single destination table inside a larger Projects script.

That lines up with the old analysis: `makeProjects.py` could generate a combined script, while `generator.py` found blocks by destination table and executed relevant blocks.

In the refactor, this should become naturally phase/run-scoped:

- One local transfer script/block per manifest run/table, or
- A structured list of executable SQL blocks with explicit table IDs.

Avoid substring matching against a giant SQL file.

### Core Projects transfer algorithm

Abstracted, the Projects transfer is:

```text
given project_db, dest_table, columns, cosmos_linked_server:
  final_table = project_db.dbo.dest_table
  local_temp = #Local_dest_table
  cosmos_temp = ##JVM_dest_table

  drop final_table
  create final_table from columns
  drop local_temp
  select columns into local_temp
    from openquery(cosmos_linked_server, "select columns from cosmos_temp")
  insert final_table(columns)
    select columns from local_temp
  count cosmos_temp through openquery
  count final_table locally
  optionally sample final_table
```

This is the important part to preserve.

### Additional tests suggested by the Projects snippet

11. Projects SQL uses the runtime linked server value, not a stale configured value.
12. Projects final table names are fully qualified as `<project_db>.dbo.<dest_table>`.
13. Local temp table name is derived predictably as `#Local_<dest_table>`.
14. Cosmos temp table name is derived predictably as `##JVM_<dest_table>`.
15. The same normalized column order is used in remote SELECT, local staging, and final INSERT.
16. Final table replacement behavior is explicit and tested.
17. Cosmos-side remote count and Projects-side local count are both captured.
18. Row-count mismatch is surfaced in manifest status or warnings.
19. Projects transfer blocks are selected by manifest IDs/phase structure, not substring table matching.
