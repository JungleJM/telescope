# Keys In Cosmos: Questions For The VM

Temporary. Put this to the VM's AI, run what it suggests, and bring the answers
back (text or screenshots). Once they are folded into `roadmap.md` and
`decisions.md`, delete this file.

## Why We Are Asking

`YAMLs/datadictionary.yaml` records key relationships only as prose inside a
column's type:

```yaml
PatientDurableKey:
  type: bigint (foreign key to PatientDim.DurableKey)
```

If the relationships were known for certain, validation could check a
cohort's **joins**, not just its columns. Today
`ON tc.TerminologyConceptKey = dt.DiagnosisKey` passes validation: both columns
exist, and the join is wrong. Before building that, we need to know three
things:

1. **Where are keys declared?** In SQL Server metadata, in Epic's documentation,
   or nowhere?
2. **Do they hold in the data?** Is a key unique where we assume it is, and does
   every child row find its parent?
3. **What does the interactive data dictionary show** about keys and links?

## 1. Are Keys Declared In SQL Server?

Run from the Cosmos connection. An empty result may mean "not declared" **or**
"we cannot see it": our permissions are narrow (`sys.partitions` already
returns nothing). So first check what we can see.

```sql
-- What can this login see on a table we know exists?
SELECT HAS_PERMS_BY_NAME('dbo.PatientDim', 'OBJECT', 'VIEW DEFINITION') AS can_view_definition;
SELECT * FROM fn_my_permissions('dbo.PatientDim', 'OBJECT');

-- Declared primary keys and unique constraints
SELECT tc.TABLE_NAME, tc.CONSTRAINT_TYPE, tc.CONSTRAINT_NAME, kcu.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS kcu
  ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
WHERE tc.TABLE_NAME IN ('PatientDim', 'DiagnosisEventFact', 'DiagnosisTerminologyDim', 'EncounterFact')
ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_TYPE;

-- Declared foreign keys, anywhere
SELECT COUNT(*) AS foreign_keys FROM sys.foreign_keys;
SELECT TOP (50)
    OBJECT_NAME(fk.parent_object_id)     AS child_table,
    COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS child_column,
    OBJECT_NAME(fk.referenced_object_id) AS parent_table,
    COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS parent_column,
    fk.is_disabled, fk.is_not_trusted
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id;

-- Unique indexes, which often stand in for undeclared keys
SELECT OBJECT_NAME(i.object_id) AS table_name, i.name AS index_name,
       i.is_primary_key, i.is_unique, COL_NAME(ic.object_id, ic.column_id) AS column_name
FROM sys.indexes AS i
JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
WHERE (i.is_unique = 1 OR i.is_primary_key = 1)
  AND OBJECT_NAME(i.object_id) IN ('PatientDim', 'DiagnosisEventFact', 'DiagnosisTerminologyDim', 'EncounterFact');
```

Ask the AI: if these come back empty, is that because nothing is declared, or
because this login cannot see it? Is there another catalog view or Epic-provided
table that lists relationships?

## 2. Do The Keys Hold In The Data?

These matter whatever the metadata says, because they decide whether a join
multiplies rows. Run each on a sample first if they are slow (`TOP`, or a date
window on the fact table).

```sql
-- Is DurableKey unique in PatientDim? The dictionary says it is the patient
-- identifier, but PatientDim has IsCurrent, which suggests history rows.
SELECT COUNT_BIG(*) AS rows_total, COUNT_BIG(DISTINCT DurableKey) AS durable_keys
FROM dbo.PatientDim;
SELECT COUNT_BIG(*) AS rows_total, COUNT_BIG(DISTINCT DurableKey) AS durable_keys
FROM dbo.PatientDim WHERE IsCurrent = 1;

-- Is DiagnosisKey unique in DiagnosisTerminologyDim? It holds one row per code
-- per terminology, so one DiagnosisKey may have an ICD-10 row and a SNOMED row.
-- If so, joining it without a Type filter duplicates diagnoses.
SELECT TOP (20) DiagnosisKey, COUNT(*) AS n, STRING_AGG(Type, ', ') AS types
FROM dbo.DiagnosisTerminologyDim
GROUP BY DiagnosisKey
HAVING COUNT(*) > 1
ORDER BY n DESC;

-- Orphans: diagnosis events whose patient is not in PatientDim
SELECT COUNT_BIG(*) AS orphan_events
FROM dbo.DiagnosisEventFact AS dxf
LEFT JOIN dbo.PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey AND p.IsCurrent = 1
WHERE p.DurableKey IS NULL;

-- Orphans: diagnosis events whose encounter is not in EncounterFact
SELECT COUNT_BIG(*) AS orphan_events
FROM dbo.DiagnosisEventFact AS dxf
LEFT JOIN dbo.EncounterFact AS e ON e.EncounterKey = dxf.EncounterKey
WHERE dxf.EncounterKey IS NOT NULL AND e.EncounterKey IS NULL;
```

Ask the AI to suggest more checks of the same shape for other
`foreign key to ...` annotations in the dictionary, and whether negative or
sentinel keys (`-1`, `0`) mean "unknown" in Cosmos, since those would look
like orphans.

## 3. The Interactive Data Dictionary

Screenshot one page for each of `PatientDim`, `DiagnosisEventFact` and
`DiagnosisTerminologyDim`, showing:

- any column marked as a key (primary, foreign, durable, surrogate);
- any "links to", "references" or "joins to" information;
- anything saying whether a table keeps history (`IsCurrent`, effective dates).

Also ask the AI whether the dictionary can be exported (CSV, Excel, an API),
since reading keys from an export would beat transcribing them.

## Also While You Are There

Two checks other decisions rely on (D50, D51):

```sql
-- D50: can we see a global temp another connection made? Create one in one
-- query window, then run this from a second window.
CREATE TABLE ##keys_probe (x INT);           -- window 1
SELECT OBJECT_ID('tempdb..##keys_probe');    -- window 2: a number means yes
DROP TABLE ##keys_probe;                     -- window 1, afterwards

-- D51: the refresh stamp, from the Cosmos connection
SELECT name, create_date FROM sys.databases WHERE name LIKE 'Cosmos%';
```
