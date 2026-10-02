/* Profiling queries for the HaT PheWAS pull. Run each block on its own in SSMS.
   Each one is aggregated, so the result is a small table, not patient rows.
   Change the date range at the top of a block if your window differs. */


/* Note: queries 1-4 assume dt.Type = 'ICD-10-CM'. Run query 6 first and fix the
   spelling if it differs. */


/* 1. HaT cohort size and the case rule.
   How many patients ever had D89.44, and how many have it on 2+ distinct dates.
   Look for: n_any (everyone to exclude from controls) vs n_2plus (the cases).
   The gap between them is the "single D89.44 code" group: not cases, not controls. */
WITH hat_dates AS (
    SELECT def.PatientDurableKey,
           COUNT(DISTINCT def.StartDateKey) AS n_dates,
           MIN(def.StartDateKey)            AS first_date
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 20180101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
    GROUP BY def.PatientDurableKey
)
SELECT COUNT(*)                                       AS n_any,
       SUM(CASE WHEN n_dates >= 2 THEN 1 ELSE 0 END)  AS n_2plus,
       MIN(first_date)                                AS earliest_first_date
FROM hat_dates;


/* 2. Cases per calendar quarter (the control sampling targets).
   Quarter from a DateKey: year = key / 10000, month = (key / 100) % 100.
   Look for: quarters with very few cases (exact-quarter matching may fail there),
   and whether earliest_first_date in query 1 is around 2021-10 or later. */
WITH hat_dates AS (
    SELECT def.PatientDurableKey,
           COUNT(DISTINCT def.StartDateKey) AS n_dates,
           MIN(def.StartDateKey)            AS first_date
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 20180101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
    GROUP BY def.PatientDurableKey
)
SELECT first_date / 10000                              AS index_year,
       ((first_date / 100) % 100 - 1) / 3 + 1          AS index_quarter,
       COUNT(*)                                        AS n_cases
FROM hat_dates
WHERE n_dates >= 2
GROUP BY first_date / 10000, ((first_date / 100) % 100 - 1) / 3 + 1
ORDER BY index_year, index_quarter;


/* 3. Other mast-cell codes before the first D89.44.
   Look for: how many cases had D89.40-D89.49 (not .44) before their first D89.44.
   If it is a small share of cases, using the first D89.44 date as index is fine. */
WITH hat_first AS (
    SELECT def.PatientDurableKey, MIN(def.StartDateKey) AS first_date
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 20180101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
    GROUP BY def.PatientDurableKey
)
SELECT dt.Value, COUNT(DISTINCT def.PatientDurableKey) AS n_patients
FROM hat_first AS h
INNER JOIN DiagnosisEventFact AS def ON def.PatientDurableKey = h.PatientDurableKey
INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
  AND def.StartDateKey BETWEEN 20180101 AND 20260601
  AND def.StartDateKey < h.first_date
  AND dt.Type = 'ICD-10-CM' AND dt.Value LIKE 'D89.4%' AND dt.Value <> 'D89.44'
GROUP BY dt.Value
ORDER BY n_patients DESC;


/* 4. Sex, race and ethnicity values among HaT patients.
   Look for: the exact spellings (pheauxWAS needs sex as Male/Female or M/F),
   how Sex and ReliableSex disagree, and which race/ethnicity values are
   blank / Unknown / Refused / Other, to group them into one level. */
WITH hat AS (
    SELECT DISTINCT def.PatientDurableKey
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 20180101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
)
SELECT p.Sex, p.ReliableSex, COUNT(*) AS n
FROM hat INNER JOIN PatientDim AS p ON p.DurableKey = hat.PatientDurableKey
WHERE p.IsValid = 1 AND p.IsCurrent = 1 AND p.UseInCosmosAnalytics_X = 1 AND p._IsDeleted = 0
GROUP BY p.Sex, p.ReliableSex
ORDER BY n DESC;

WITH hat AS (
    SELECT DISTINCT def.PatientDurableKey
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 20180101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
)
SELECT p.FirstRace, p.MultiRacial, p.Ethnicity, COUNT(*) AS n
FROM hat INNER JOIN PatientDim AS p ON p.DurableKey = hat.PatientDurableKey
WHERE p.IsValid = 1 AND p.IsCurrent = 1 AND p.UseInCosmosAnalytics_X = 1 AND p._IsDeleted = 0
GROUP BY p.FirstRace, p.MultiRacial, p.Ethnicity
ORDER BY n DESC;


/* 5. Encounter status and type values (one month only, to keep it cheap).
   Look for: which DerivedEncounterStatus means a completed visit (vs cancelled,
   no-show), and that IsOutpatientFaceToFaceVisit = 1 lines up with the
   clinic/office types you expect. */
SELECT ef.DerivedEncounterStatus,
       ef.DerivedEncounterType_X,
       ef.IsOutpatientFaceToFaceVisit,
       ef.IsEdVisit,
       ef.IsHospitalAdmission,
       COUNT(*) AS n
FROM EncounterFact AS ef
WHERE ef._IsDeleted = 0
  AND ef.DateKey BETWEEN 20240101 AND 20240131
GROUP BY ef.DerivedEncounterStatus, ef.DerivedEncounterType_X,
         ef.IsOutpatientFaceToFaceVisit, ef.IsEdVisit, ef.IsHospitalAdmission
ORDER BY n DESC;


/* 6. Terminology type spellings.
   Look for: the exact dt.Type values for ICD-10-CM and ICD-9-CM, to use in every
   filter above and in the diagnosis-event pull. */
SELECT dt.Type, COUNT(*) AS n
FROM DiagnosisTerminologyDim AS dt
WHERE dt._IsDeleted = 0
GROUP BY dt.Type
ORDER BY n DESC;


/* ---- Follow-ups to the first results (2026-10-01) ---- */

/* 7. First D89.44 with no 2018 floor.
   Query 1 only searched from 2018, so a "first" D89.44 in 2018 may not be the first.
   Look for: how many patients' true first D89.44 is before 2018 (first_year < 2018),
   and the case count per year of first D89.44. One row per year, so it fits on screen. */
WITH hat_dates AS (
    SELECT def.PatientDurableKey,
           COUNT(DISTINCT def.StartDateKey) AS n_dates,
           MIN(def.StartDateKey)            AS first_date
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 19900101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
    GROUP BY def.PatientDurableKey
)
SELECT first_date / 10000                              AS first_year,
       COUNT(*)                                        AS n_any,
       SUM(CASE WHEN n_dates >= 2 THEN 1 ELSE 0 END)   AS n_2plus
FROM hat_dates
GROUP BY first_date / 10000
ORDER BY first_year;


/* 8. How long before the first D89.44 the first other D89.4x code came.
   Query 3 showed 1,433 patients with D89.40 before their first D89.44.
   Look for: whether the gap is short (weeks: the same workup, recoded) or long
   (years: an earlier mast-cell diagnosis). */
WITH hat_first AS (
    SELECT def.PatientDurableKey, MIN(def.StartDateKey) AS first_44
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 19900101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
    GROUP BY def.PatientDurableKey
),
other_first AS (
    SELECT h.PatientDurableKey, h.first_44, MIN(def.StartDateKey) AS first_other
    FROM hat_first AS h
    INNER JOIN DiagnosisEventFact AS def ON def.PatientDurableKey = h.PatientDurableKey
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 19900101 AND 20260601
      AND def.StartDateKey < h.first_44
      AND dt.Type = 'ICD-10-CM' AND dt.Value LIKE 'D89.4%' AND dt.Value <> 'D89.44'
    GROUP BY h.PatientDurableKey, h.first_44
)
SELECT CASE
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 90  THEN '1: up to 3 months'
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 365 THEN '2: 3-12 months'
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 1095 THEN '3: 1-3 years'
         ELSE '4: over 3 years'
       END AS gap,
       COUNT(*) AS n_patients
FROM other_first
GROUP BY CASE
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 90  THEN '1: up to 3 months'
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 365 THEN '2: 3-12 months'
         WHEN DATEDIFF(day, CONVERT(date, CAST(first_other AS char(8))),
                            CONVERT(date, CAST(first_44 AS char(8)))) <= 1095 THEN '3: 1-3 years'
         ELSE '4: over 3 years'
       END
ORDER BY gap;


/* 9. Race and ethnicity, each on its own (shorter than query 4's combinations).
   Look for: every spelling, especially values starting with '*' (*Unspecified,
   *Unknown...) or blank, which go into the grouped "Unknown/Other" level. */
WITH hat AS (
    SELECT DISTINCT def.PatientDurableKey
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 19900101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
)
SELECT 'FirstRace' AS field, p.FirstRace AS value, COUNT(*) AS n
FROM hat INNER JOIN PatientDim AS p ON p.DurableKey = hat.PatientDurableKey
WHERE p.IsValid = 1 AND p.IsCurrent = 1 AND p.UseInCosmosAnalytics_X = 1 AND p._IsDeleted = 0
GROUP BY p.FirstRace
UNION ALL
SELECT 'Ethnicity', p.Ethnicity, COUNT(*)
FROM hat INNER JOIN PatientDim AS p ON p.DurableKey = hat.PatientDurableKey
WHERE p.IsValid = 1 AND p.IsCurrent = 1 AND p.UseInCosmosAnalytics_X = 1 AND p._IsDeleted = 0
GROUP BY p.Ethnicity
ORDER BY field, n DESC;


/* 10. Which encounter types count as outpatient face-to-face, and every status.
   Query 5 showed Office Visit and Hospital Outpatient Visit both flagged.
   Look for: every type with the flag (to decide which are "clinic visits"),
   and every status value besides Complete. */
SELECT ef.DerivedEncounterStatus, ef.DerivedEncounterType_X, COUNT(*) AS n
FROM EncounterFact AS ef
WHERE ef._IsDeleted = 0
  AND ef.DateKey BETWEEN 20240101 AND 20240131
  AND ef.IsOutpatientFaceToFaceVisit = 1
GROUP BY ef.DerivedEncounterStatus, ef.DerivedEncounterType_X
ORDER BY n DESC;


/* 11. The case count, broken down: how many distinct D89.44 dates each patient has.
   Reconciles "about 6,000 HaT patients" (any D89.44) with 3,693 (2+ dates).
   Columns:
     n_dates         1, 2, 3, 4, or 5+ distinct D89.44 dates
     n_patients      patients with that many
     recent_only     single-date patients whose date is in the last 6 months of the
                     window (they had no time for a second code)
     on_problem_list single-date patients whose D89.44 rows include a problem-list
                     entry (DiagnosisEventFact.Type; the exact value is shown in 11b)
   Look for: whether the single-date group looks like real HaT patients seen once
   (or recently), or like rule-outs. */
WITH hat_rows AS (
    SELECT def.PatientDurableKey, def.StartDateKey, def.Type
    FROM DiagnosisEventFact AS def
    INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
    WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
      AND def.StartDateKey BETWEEN 19900101 AND 20260601
      AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
),
per_patient AS (
    SELECT PatientDurableKey,
           COUNT(DISTINCT StartDateKey) AS n_dates,
           MAX(StartDateKey)            AS last_date,
           MAX(CASE WHEN Type LIKE '%Problem%' THEN 1 ELSE 0 END) AS any_problem_list
    FROM hat_rows
    GROUP BY PatientDurableKey
)
SELECT CASE WHEN n_dates >= 5 THEN '5+' ELSE CAST(n_dates AS varchar(2)) END AS n_dates,
       COUNT(*) AS n_patients,
       SUM(CASE WHEN n_dates = 1 AND last_date >= 20251201 THEN 1 ELSE 0 END) AS recent_only,
       SUM(CASE WHEN n_dates = 1 AND any_problem_list = 1 THEN 1 ELSE 0 END)  AS on_problem_list
FROM per_patient
GROUP BY CASE WHEN n_dates >= 5 THEN '5+' ELSE CAST(n_dates AS varchar(2)) END
ORDER BY n_dates;

/* 11b. The diagnosis types D89.44 rows carry (billing, problem list, ...). */
SELECT def.Type, COUNT(*) AS n_rows, COUNT(DISTINCT def.PatientDurableKey) AS n_patients
FROM DiagnosisEventFact AS def
INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey
WHERE def._IsDeleted = 0 AND dt._IsDeleted = 0
  AND def.StartDateKey BETWEEN 19900101 AND 20260601
  AND dt.Type = 'ICD-10-CM' AND dt.Value = 'D89.44'
GROUP BY def.Type
ORDER BY n_rows DESC;
