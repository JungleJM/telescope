# The VM AI's Answer To The Keys Brief

Transcribed from six screenshots (the `Screenshot 2026-09-24 ...` files beside
this one) of the VM's AI answering `keys_research_for_vm.md`, which was saved
on the VM as `Notes/keysInDataDictionary.md`. The answer ran to 11 printed
pages, 682 lines; the screenshots cover all of them. Transcribed as written,
except: its links back to the brief (`[sql](Notes/keysInDataDictionary.md:31)`)
and the `-- written by generative AI` line opening each SQL block are left out,
and `?` where the print lost an arrow is written `→`.

**Did it run the queries? Almost certainly not.** It reports no counts, rows
or results anywhere. Every interpretation is conditional ("if the first query
shows duplicates...", "if window 2 returns `NULL`..."), and "you already have"
quotes the brief's own queries back. It explains what the queries would show;
none has been run. Some of what it states about Cosmos (sentinel keys,
"Galaxy / EDDI" documentation, no dictionary export) is presented as Epic
convention, and is unverified until checked against the data or the
interactive dictionary.

---

Answers are in three parts to match your sections.

---

## 1. Are keys declared in SQL Server?

### What's going on with the catalog views?

You already noted the key point: Cosmos logins have very constrained
permissions. Two consequences:

1. You may not be able to see:
   - `sys.foreign_keys`
   - `sys.foreign_key_columns`
   - `sys.indexes`
   - `sys.index_columns`
   - even `INFORMATION_SCHEMA.*` beyond a minimal subset
2. Empty results from your probes can mean:
   - "no keys are declared", **or**
   - "your login is blocked from seeing metadata".

Your test:

```sql
SELECT HAS_PERMS_BY_NAME('dbo.PatientDim', 'OBJECT', 'VIEW DEFINITION') AS can_view_definition;
SELECT * FROM fn_my_permissions('dbo.PatientDim', 'OBJECT');
```

interprets as:

- If `HAS_PERMS_BY_NAME = 0` and `fn_my_permissions` shows only basic rights,
  you will not see:
  - table definition
  - indexes
  - foreign keys
- If `HAS_PERMS_BY_NAME = 1` and you still get empty sets from
  `INFORMATION_SCHEMA` / `sys.*`, then for Cosmos specifically Epic is almost
  certainly restricting access to those catalog views.

Cosmos is designed around a published data model (Galaxy / EDDI docs) and the
interactive data dictionary, not around discoverable SQL Server metadata. In
practice:

- **Primary keys / foreign keys are not exposed to analysts via SQL Server
  metadata.**
- They are defined in Caboodle and Cosmos internally, but the Cosmos analytics
  login does not see those definitions.
- There is no Epic-provided "relationship catalog table" in Cosmos that you can
  query to recover all parent/child links. Relationship knowledge is in:
  - the interactive data dictionary
  - Galaxy documentation for each table
  - narrative descriptions like your `datadictionary.yaml`

So if your `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` and `sys.foreign_keys`
queries come back empty, that is permissions, not proof that keys are
undeclared in the underlying DB.

### Is there another catalog view/table with relationships?

Within Cosmos as provided to users, there is no supported, queryable
"relationship registry" that lists foreign keys the way `sys.foreign_keys`
would, across all tables. Epic's pattern is:

- Data model and relationship diagrams live in documentation and the
  interactive data dictionary.
- Keys are described there in prose (as you've mirrored into
  `datadictionary.yaml`).
- Users rely on those docs rather than introspecting `sys.*`.

So for your validator you should assume:

- You **cannot** infer relationships from SQL Server metadata.
- You **must** rely on:
  - The YAML (your curated version of Galaxy/interactive dictionary)
  - Data checks (section 2 below)
  - A small set of known rules (e.g. `PatientDurableKey → PatientDim.DurableKey`).

From the VM's point of view, "empty catalog views" will most often mean "login
cannot see them", not "no keys".

---

## 2. Do the keys hold in the data?

Your probes are exactly the right shape. You can extend them systematically off
every `type: ... (foreign key to ...)` in `datadictionary.yaml`.

### General pattern for checking foreign keys

For any annotation:

```yaml
SomeChildKey:
  type: bigint (foreign key to ParentTable.ParentKey)
```

use two checks:

1. **Parent uniqueness / key-ness**

   ```sql
   SELECT
       COUNT_BIG(*)                                 AS rows_total,
       COUNT_BIG(DISTINCT ParentKey)                AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT ParentKey) AS duplicate_rows
   FROM dbo.ParentTable;
   ```

   If `duplicate_rows > 0`, either:

   - The key is not unique in the parent, **or**
   - There is history across rows and some additional filter
     (`IsCurrent = 1`, effective dates) is required.

2. **Child orphans**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_rows
   FROM dbo.ChildTable AS c
   LEFT JOIN dbo.ParentTable AS p
     ON p.ParentKey = c.SomeChildKey
     -- add filters like p.IsCurrent = 1 if the documentation says history rows exist
   WHERE p.ParentKey IS NULL;
   ```

   If `orphan_rows` is non-zero you need to decide:

   - Are they genuine data quality problems?
   - Are they expected special cases (masked rows, sentinel keys, cross-system
     gaps)?

Always run these on `COSMOS_SneakPeek` first; if they're cheap enough, re-run on
`Cosmos` before you trust them.

### Concrete suggestions for other Cosmos keys

Using only columns you listed:

#### Patient keys

1. **PatientDim.DurableKey uniqueness, with and without history**

   You already have:

   ```sql
   SELECT COUNT_BIG(*) AS rows_total, COUNT_BIG(DISTINCT DurableKey) AS durable_keys
   FROM dbo.PatientDim;

   SELECT COUNT_BIG(*) AS rows_total, COUNT_BIG(DISTINCT DurableKey) AS durable_keys
   FROM dbo.PatientDim
   WHERE IsCurrent = 1;
   ```

   Interpretation:

   - If the first query shows duplicates and the second doesn't, then your
     validator should:
     - Treat `PatientDim.DurableKey` as a logical key only when `IsCurrent = 1`.
     - Never join to unfiltered `PatientDim` if your downstream logic assumes
       one row per patient.

2. **PatientDurableKey relationships**

   There are many tables with `PatientDurableKey`; examples from your list:

   - `DiagnosisEventFact.PatientDurableKey`
   - `EncounterFact.PatientDurableKey`
   - `MedicationOrderFact.PatientDurableKey`
   - `MedicationOrderComponentFact.PatientDurableKey`
   - `MedicationAdministrationFact.PatientDurableKey`
   - `MedicationDispenseFact.PatientDurableKey`
   - `LabComponentResultFact.PatientDurableKey`
   - `VitalsFact.PatientDurableKey`
   - `PregnancyFact.PatientDurableKey`
   - `BirthFact.MotherPatientDurableKey`, `BirthFact.BabyPatientDurableKey`
   - `CancerStagingFact.PatientDurableKey`
   - `ProblemListFact.PatientDurableKey`
   - `ResearchStudyPatientAssociationFact.PatientDurableKey`
   - `RiskScoreFact.PatientDurableKey`
   - `TransplantEpisodeFact.PatientDurableKey`
   - `VariantResultFact.PatientDurableKey`
   - `VariantResultPhenotypeFact.PatientDurableKey`
   - etc.

   Canonical orphan check (you already did it for diagnoses):

   ```sql
   SELECT COUNT_BIG(*) AS orphan_events
   FROM dbo.DiagnosisEventFact AS dxf
   LEFT JOIN dbo.PatientDim AS p
     ON p.DurableKey = dxf.PatientDurableKey
    AND p.IsCurrent = 1  -- history filter to match documentation
   WHERE p.DurableKey IS NULL;
   ```

   Repeat this shape for other fact tables, e.g.:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_encounters
   FROM dbo.EncounterFact AS e
   LEFT JOIN dbo.PatientDim AS p
     ON p.DurableKey = e.PatientDurableKey
    AND p.IsCurrent = 1
   WHERE p.DurableKey IS NULL;
   ```

   Use `DateKey` filters (e.g. `e.DateKey BETWEEN 19990101 AND 20251231`) in
   production-sized queries.

#### Encounter keys

Tables with `EncounterKey` include at least:

- `EncounterFact.EncounterKey`
- `DiagnosisEventFact.EncounterKey`
- `EdVisitFact.EncounterKey`
- `HospitalAdmissionFact.EncounterKey`
- `MedicationOrderComponentFact.EncounterKey`
- `MedicationAdministrationFact.EncounterKey`
- `LabComponentResultFact.EncounterKey`
- `VitalsFact.EncounterKey`
- `IcuStayRegistryDataMart.EncounterKey`
- `EyeExamTestFact.EncounterKey`
- `HomeCareEncounterFact.EncounterKey`
- `ProcedureEventFact.EncounterKey`
- `SurveyAnswerFact.EncounterKey`

Checks:

1. **EncounterFact.EncounterKey uniqueness**

   ```sql
   SELECT
       COUNT_BIG(*) AS rows_total,
       COUNT_BIG(DISTINCT EncounterKey) AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT EncounterKey) AS duplicate_rows
   FROM dbo.EncounterFact;
   ```

2. **Orphans from child tables to encounters**

   You already have diagnoses:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_events
   FROM dbo.DiagnosisEventFact AS dxf
   LEFT JOIN dbo.EncounterFact AS e
     ON e.EncounterKey = dxf.EncounterKey
   WHERE dxf.EncounterKey IS NOT NULL
     AND e.EncounterKey IS NULL;
   ```

   Same pattern for labs (expensive; run on `COSMOS_SneakPeek` or date-window):

   ```sql
   SELECT COUNT_BIG(*) AS orphan_lab_results
   FROM dbo.LabComponentResultFact AS lcr
   LEFT JOIN dbo.EncounterFact AS e
     ON e.EncounterKey = lcr.EncounterKey
   WHERE lcr.EncounterKey IS NOT NULL
     AND e.EncounterKey IS NULL;
   ```

   And vitals:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_vitals
   FROM dbo.VitalsFact AS vf
   LEFT JOIN dbo.EncounterFact AS e
     ON e.EncounterKey = vf.EncounterKey
   WHERE vf.EncounterKey IS NOT NULL
     AND e.EncounterKey IS NULL;
   ```

#### Diagnosis keys

1. **DiagnosisDim.DiagnosisKey uniqueness**

   ```sql
   SELECT
       COUNT_BIG(*) AS rows_total,
       COUNT_BIG(DISTINCT DiagnosisKey) AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT DiagnosisKey) AS duplicate_rows
   FROM dbo.DiagnosisDim;
   ```

2. **DiagnosisTerminologyDim by DiagnosisKey**

   You already have:

   ```sql
   SELECT TOP (20)
       DiagnosisKey,
       COUNT(*) AS n,
       STRING_AGG(Type, ', ') AS types
   FROM dbo.DiagnosisTerminologyDim
   GROUP BY DiagnosisKey
   HAVING COUNT(*) > 1
   ORDER BY n DESC;
   ```

   Interpretation:

   - If many `DiagnosisKey` values have multiple `Type`s, then joining
     `DiagnosisEventFact` to `DiagnosisTerminologyDim` only on `DiagnosisKey`
     will duplicate diagnoses across terminologies.
   - Your validator should treat `DiagnosisTerminologyDim` as one-to-many per
     `DiagnosisKey` and require a `Type` filter when joining.

3. **DiagnosisEventFact → DiagnosisDim**

   Orphans:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_dx_events
   FROM dbo.DiagnosisEventFact AS dxf
   LEFT JOIN dbo.DiagnosisDim AS dd
     ON dd.DiagnosisKey = dxf.DiagnosisKey
   WHERE dxf.DiagnosisKey IS NOT NULL
     AND dd.DiagnosisKey IS NULL;
   ```

   And specifically for terminology:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_dx_terminology
   FROM dbo.DiagnosisEventFact AS dxf
   LEFT JOIN dbo.DiagnosisTerminologyDim AS dt
     ON dt.DiagnosisKey = dxf.DiagnosisKey
   WHERE dxf.DiagnosisKey IS NOT NULL
     AND dt.DiagnosisKey IS NULL;
   ```

   If you see many orphans here, it suggests not all diagnosis codes are mapped
   into `DiagnosisTerminologyDim`.

#### Procedure keys

You have:

- `ProcedureDim.DurableKey`
- `ProcedureEventFact.ProcedureDurableKey`

Checks:

1. **DurableKey uniqueness in ProcedureDim**

   ```sql
   SELECT
       COUNT_BIG(*) AS rows_total,
       COUNT_BIG(DISTINCT DurableKey) AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT DurableKey) AS duplicate_rows
   FROM dbo.ProcedureDim;
   ```

2. **Orphans: events whose procedure code is not in ProcedureDim**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_procedure_events
   FROM dbo.ProcedureEventFact AS pef
   LEFT JOIN dbo.ProcedureDim AS pd
     ON pd.DurableKey = pef.ProcedureDurableKey
   WHERE pef.ProcedureDurableKey IS NOT NULL
     AND pd.DurableKey IS NULL;
   ```

#### Medication keys

Tables:

- `MedicationDim.MedicationKey`
- `MedicationOrderFact.MedicationKey`
- `MedicationOrderComponentFact.MedicationKey`
- `MedicationAdministrationFact.MedicationKey`
- `MedicationDispenseFact.MedicationKey`
- `MedicationCodeDim.MedicationKey`
- `MedicationSetDim.MedicationKey`

1. **MedicationDim key uniqueness**

   ```sql
   SELECT
       COUNT_BIG(*) AS rows_total,
       COUNT_BIG(DISTINCT MedicationKey) AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT MedicationKey) AS duplicate_rows
   FROM dbo.MedicationDim;
   ```

2. **Orders → MedicationDim**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_med_orders
   FROM dbo.MedicationOrderFact AS mof
   LEFT JOIN dbo.MedicationDim AS md
     ON md.MedicationKey = mof.MedicationKey
   WHERE mof.MedicationKey IS NOT NULL
     AND md.MedicationKey IS NULL;
   ```

3. **Components → MedicationDim**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_med_order_components
   FROM dbo.MedicationOrderComponentFact AS mocf
   LEFT JOIN dbo.MedicationDim AS md
     ON md.MedicationKey = mocf.MedicationKey
   WHERE mocf.MedicationKey IS NOT NULL
     AND md.MedicationKey IS NULL;
   ```

4. **Administrations → MedicationDim**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_med_admins
   FROM dbo.MedicationAdministrationFact AS maf
   LEFT JOIN dbo.MedicationDim AS md
     ON md.MedicationKey = maf.MedicationKey
   WHERE maf.MedicationKey IS NOT NULL
     AND md.MedicationKey IS NULL;
   ```

Note: mixture medications in `MedicationOrderFact` and
`MedicationAdministrationFact` carry `MedicationKey = -1`. These will look like
"orphans" unless you treat `-1` specially.

#### Lab keys

Tables:

- `LabComponentDim.LabComponentKey`
- `LabComponentResultFact.LabComponentKey`
- `LabComponentSetDim.LabComponentKey`
- `LabComponentMappingDim.LabComponentKey`

1. **LabComponentDim key uniqueness**

   ```sql
   SELECT
       COUNT_BIG(*) AS rows_total,
       COUNT_BIG(DISTINCT LabComponentKey) AS distinct_keys,
       COUNT_BIG(*) - COUNT_BIG(DISTINCT LabComponentKey) AS duplicate_rows
   FROM dbo.LabComponentDim;
   ```

2. **Results → LabComponentDim**

   ```sql
   SELECT COUNT_BIG(*) AS orphan_lab_components
   FROM dbo.LabComponentResultFact AS lcr
   LEFT JOIN dbo.LabComponentDim AS lcd
     ON lcd.LabComponentKey = lcr.LabComponentKey
   WHERE lcr.LabComponentKey IS NOT NULL
     AND lcd.LabComponentKey IS NULL;
   ```

   Here `LabComponentKey = -1` is the unmapped sentinel; see next section.

### Sentinel keys and negative values

Your question:

> whether negative or sentinel keys (`-1`, `0`) mean "unknown" in Cosmos, since
> those would look like orphans.

From Caboodle/Cosmos conventions and Epic documentation:

- `-1` is consistently used as an "unspecified / unmapped" sentinel for foreign
  keys:
  - `LabComponentResultFact.LabComponentKey = -1` → lab component not mapped.
  - `MedicationOrderFact.MedicationKey = -1` and
    `MedicationAdministrationFact.MedicationKey = -1` → mixture medications.
  - Fact tables carry placeholder rows with negative primary keys and null
    non-key columns, purely for referential integrity; these are not usable
    data.
- `0` can mean "unknown" or "not applicable" in some columns, but semantics are
  column-specific; you cannot treat all zero keys as orphans generically.

Implications:

1. **Exclude sentinel keys when counting orphans**

   Lab example:

   ```sql
   SELECT COUNT_BIG(*) AS orphan_lab_components
   FROM dbo.LabComponentResultFact AS lcr
   LEFT JOIN dbo.LabComponentDim AS lcd
     ON lcd.LabComponentKey = lcr.LabComponentKey
   WHERE lcr.LabComponentKey IS NOT NULL
     AND lcr.LabComponentKey <> -1      -- sentinel: unmapped component
     AND lcd.LabComponentKey IS NULL;
   ```

2. **Exclude negative primary keys in validation probes**

   Medication administration example:

   ```sql
   SELECT COUNT_BIG(*) AS real_admin_rows
   FROM dbo.MedicationAdministrationFact
   WHERE MedicationAdministrationKey > 0;
   ```

3. **Encode column-specific sentinel semantics in your YAML**

   ```yaml
   LabComponentResultFact.LabComponentKey:
     type: bigint (foreign key to LabComponentDim.LabComponentKey)
     sentinel values: [-1]
     meaning:
       - -1: Unmapped lab component; do not treat as orphan
   ```

   Then your validator:

   - Skips sentinel rows in orphan checks.
   - Optionally reports them separately as "unmapped / mixture / placeholder".

---

## 3. The interactive data dictionary

You asked for each of `PatientDim`, `DiagnosisEventFact`, and
`DiagnosisTerminologyDim`:

> - any column marked as a key (primary, foreign, durable, surrogate);
> - any "links to", "references" or "joins to" information;
> - anything saying whether a table keeps history (`IsCurrent`, effective dates).

For those tables, the interactive data dictionary is the authoritative source
for:

1. **Key columns**

   Examples in the UI (paraphrased based on Epic's conventions, not exact text):

   - `PatientDim.DurableKey` described as a durable surrogate key, unique per
     patient, commonly used as foreign key.
   - `DiagnosisEventFact.DiagnosisEventKey` described as a surrogate key for each
     diagnosis event row.
   - `DiagnosisTerminologyDim.DiagnosisTerminologyKey` described as a surrogate
     key per terminology/code row.

2. **Foreign key relationships**

   Column descriptions or "Links To"/"References" sections indicate:

   - `PatientDurableKey` → "Foreign key to PatientDim.DurableKey."
   - `EncounterKey` → "Foreign key to EncounterFact.EncounterKey."
   - `DiagnosisKey` in events → links to `DiagnosisDim.DiagnosisKey`, and when
     using terminology, typically with `DiagnosisTerminologyDim` on
     `DiagnosisKey` + `Type`.

   This is what you're already capturing in:

   ```yaml
   PatientDurableKey:
     type: bigint (foreign key to PatientDim.DurableKey)
   ```

3. **History semantics**

   Descriptions for:

   - `IsCurrent` in `PatientDim` ("this table keeps history; IsCurrent = 1
     marks the current row").
   - `StartDateKey` / `EndDateKey` or effective start/end columns in
     episode/problem tables.
   - `IsHistorical` flags in episodic tables.

   These details are essential because:

   - Keys may only be unique when filtered to current rows.
   - Joins can legitimately produce multiple rows per logical entity when
     history is involved.

### Can the dictionary be exported?

The Cosmos interactive data dictionary is not exposed as a self-service export
inside Cosmos:

- No documented button to export the full dictionary as CSV/Excel.
- No documented API inside Cosmos for programmatically pulling dictionary
  metadata.

Cosmos' export policy is focused on summary-level data and artifacts (like
tables of aggregate results), not on metadata. While dictionary metadata is not
patient data, getting a full schema export would require support outside the
normal UI:

- You would need to contact your Epic representative.
- Ask specifically whether an export of the Cosmos schema / interactive
  dictionary (or EDDI model) can be delivered as a file.
- If approved, they can provide a document or extract, but it is not something
  you can trigger from Cosmos SQL.

For your project, until such an export exists, the approach you're using is the
right one:

- Treat `data/reference/datadictionary.yaml` as your working export.
- Populate it from:
  - Interactive dictionary screen captures / manual transcription.
  - Galaxy docs.
  - Data-backed checks as above.

---

## 4. "Also While You Are There" probes (D50, D51)

### D50: Global temp visibility

```sql
CREATE TABLE ##keys_probe (x INT);            -- window 1
SELECT OBJECT_ID('tempdb..##keys_probe');     -- window 2: a number means yes
DROP TABLE ##keys_probe;                      -- window 1, afterwards
```

Interpretation:

- If window 2 returns a positive `OBJECT_ID`, then:
  - Your Cosmos login can see global temp tables created in other sessions.
  - That enables workflows where a DSVM or another connection stages data into
    `##` tables, and a separate Cosmos session reads them.
- If window 2 returns `NULL`, then:
  - Global temp tables are not visible cross-session to your login.
  - You must design the generator so each connection owns its own temp
    objects; no cross-session staging.

This directly affects patterns like "pre-compute a key set in one window and
then join to it in another".

### D51: Refresh stamp / database dates

```sql
SELECT name, create_date
FROM sys.databases
WHERE name LIKE 'Cosmos%';
```

Given your limited `sys.*` access, this query checks:

- Whether you see any metadata about the Cosmos databases.
- `create_date` is the SQL Server database creation date:
  - It tells you when that Cosmos db instance was created on the host.
  - It does **not** provide "last ETL refresh date" for data.

For exact refresh cadence and timestamps, you need documentation or an
Epic-provided artifact; they're not discoverable from Cosmos SQL.

---

## 5. How this feeds into your validator

Summarizing:

1. **Do not rely on SQL Server metadata to discover keys.**
   - Absence in `INFORMATION_SCHEMA` / `sys.*` is permissions, not proof of no
     keys.
   - Use your YAML + interactive dictionary + Galaxy docs as the relationship
     source of truth.
2. **Add systematic data checks for each `foreign key to ...` in YAML.**
   - Parent uniqueness (possibly filtered by `IsCurrent` / effective dates).
   - Child orphan counts, excluding sentinel keys (`-1`, occasionally `0`).
   - Multiplicity checks for one-to-many relationship tables like
     `DiagnosisTerminologyDim`.
3. **Encode sentinel semantics per column in YAML.**
   - `sentinel values` and meaning, e.g. `-1` = unmapped, mixture, placeholder.
   - Make the validator skip sentinels in "orphan" counts and optionally count
     them separately.
4. **Flag dangerous join patterns.**
   - Joins to `DiagnosisTerminologyDim` on `DiagnosisKey` without constraining
     `Type`.
   - Joins to parent tables whose keys aren't unique unless filtered (e.g.
     `PatientDim` without `IsCurrent = 1`).
   - Use row-vs-distinct-key counts to detect these patterns automatically.
5. **Use the global temp probe to decide architecture.**
   - If global temps are visible cross-session, you can design multi-step,
     multi-connection workflows with `##` tables.
   - If not, keep each generator script self-contained per session.

This covers the three questions in `Notes/keysInDataDictionary.md` and provides
concrete SQL shapes you can fold into your validator and `datadictionary.yaml`
process.
