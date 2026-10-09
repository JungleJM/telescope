/*
  Can Cosmos tell an ICU stay? A check of the rule `rsv` uses: an admission is an
  ICU admission when the department it was admitted to, discharged from, or given
  a medication in has a specialty in settings.yaml's `icu_specialties`.

  Run it in SSMS against the Cosmos database (the one Execute pulls from), all at
  once (F5). It only reads: it makes nothing but #temp tables, which go when the
  window closes. Each result grid is numbered below; send them all.

  It looks at infants' admissions in one month, to stay quick. Change @from and
  @to for another; @meds_to must be after the last discharge you care about.
*/
SET NOCOUNT ON;

DECLARE @from BIGINT = 20241201;      -- admissions from this day
DECLARE @to BIGINT = 20241231;        -- to this day
DECLARE @meds_to BIGINT = 20250331;   -- medications up to this day (stays run past @to)

-- The specialties to test as ICU; add or remove lines to try another rule.
DECLARE @icu TABLE (Specialty NVARCHAR(300) PRIMARY KEY);
INSERT INTO @icu (Specialty) VALUES
    (N'Critical Care Medicine'),
    (N'Pediatric Intensive Care');

/* 1. Is there a table or column that names an ICU or a transfer outright? */
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo'
  AND (COLUMN_NAME LIKE '%Icu%' OR COLUMN_NAME LIKE '%Intensive%' OR COLUMN_NAME LIKE '%CriticalCare%'
       OR COLUMN_NAME LIKE '%LevelOfCare%' OR COLUMN_NAME LIKE '%UnitType%'
       OR TABLE_NAME LIKE '%Icu%' OR TABLE_NAME LIKE '%Adt%' OR TABLE_NAME LIKE '%Transfer%'
       OR TABLE_NAME LIKE '%BedDay%' OR TABLE_NAME LIKE '%Census%' OR TABLE_NAME LIKE '%LevelOfCare%')
ORDER BY TABLE_NAME, COLUMN_NAME;

/* 2. Every column DepartmentDim has: is there a name, a type or a level of care beside the specialty? */
SELECT COLUMN_NAME, DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'DepartmentDim'
ORDER BY ORDINAL_POSITION;

/* 3. Specialties that could mean an ICU, and how many departments carry each, across Cosmos. */
SELECT DepartmentSpecialty, COUNT(*) AS Departments
FROM dbo.DepartmentDim
WHERE _IsDeleted = 0
  AND (DepartmentSpecialty LIKE '%critical%' OR DepartmentSpecialty LIKE '%intensive%'
       OR DepartmentSpecialty LIKE '%ICU%' OR DepartmentSpecialty LIKE '%neonat%'
       OR DepartmentSpecialty LIKE '%cardi%' OR DepartmentSpecialty LIKE '%trauma%')
GROUP BY DepartmentSpecialty
ORDER BY Departments DESC;

/* The month's admissions of children under 2. */
DROP TABLE IF EXISTS #adm;
SELECT haf.HospitalAdmissionKey, haf.EncounterKey, haf.PatientDurableKey, haf.DepartmentKey, haf.DischargeDepartmentKey_X,
       haf.InpatientAdmissionInstant, haf.DischargeInstant, haf.LengthOfStayInDays
INTO #adm
FROM dbo.HospitalAdmissionFact AS haf
INNER JOIN dbo.DurationDim AS age ON age.DurationKey = haf.AgeKey
WHERE haf._IsDeleted = 0
  AND haf.AdmissionDateKey BETWEEN @from AND @to
  AND age.Years < 2;

/* 4. How many, and how many have an inpatient admission time (the medication window needs one). */
SELECT COUNT(*) AS InfantAdmissions,
       SUM(CASE WHEN InpatientAdmissionInstant IS NULL THEN 1 ELSE 0 END) AS NoInpatientAdmissionInstant,
       SUM(CASE WHEN DischargeInstant IS NULL THEN 1 ELSE 0 END) AS NoDischargeInstant,
       SUM(CASE WHEN DepartmentKey IS NULL OR DepartmentKey < 0 THEN 1 ELSE 0 END) AS NoAdmitDepartment,
       SUM(CASE WHEN DischargeDepartmentKey_X IS NULL OR DischargeDepartmentKey_X < 0 THEN 1 ELSE 0 END) AS NoDischargeDepartment
FROM #adm;

/* 5. The specialty of the department they were admitted to, every one. */
SELECT COALESCE(d.DepartmentSpecialty, N'(none)') AS AdmittedToSpecialty, COUNT(*) AS Admissions
FROM #adm AS a
LEFT JOIN dbo.DepartmentDim AS d ON d.DepartmentKey = a.DepartmentKey
GROUP BY COALESCE(d.DepartmentSpecialty, N'(none)')
ORDER BY Admissions DESC;

/* 6. The specialty of the department they were discharged from, every one. */
SELECT COALESCE(d.DepartmentSpecialty, N'(none)') AS DischargedFromSpecialty, COUNT(*) AS Admissions
FROM #adm AS a
LEFT JOIN dbo.DepartmentDim AS d ON d.DepartmentKey = a.DischargeDepartmentKey_X
GROUP BY COALESCE(d.DepartmentSpecialty, N'(none)')
ORDER BY Admissions DESC;

/* Medications given during each stay, and where. */
DROP TABLE IF EXISTS #stay;
SELECT a.HospitalAdmissionKey, maf.AdministrationDepartmentKey
INTO #stay
FROM #adm AS a
INNER JOIN dbo.MedicationAdministrationFact AS maf ON maf.EncounterKey = a.EncounterKey
WHERE maf._IsDeleted = 0
  AND maf.AdministrationDateKey BETWEEN @from AND @meds_to
  AND maf.AdministrationInstant BETWEEN a.InpatientAdmissionInstant AND a.DischargeInstant;

/* 7. Is the administering department filled in, and do most stays have medications at all? */
SELECT COUNT(*) AS Administrations,
       SUM(CASE WHEN AdministrationDepartmentKey IS NULL OR AdministrationDepartmentKey < 0 THEN 1 ELSE 0 END) AS NoDepartment,
       COUNT(DISTINCT HospitalAdmissionKey) AS AdmissionsWithMedications,
       (SELECT COUNT(*) FROM #adm) AS InfantAdmissions
FROM #stay;

/* 8. The specialties medications were given in during the stays: admissions with any in each, every one. */
SELECT COALESCE(d.DepartmentSpecialty, N'(none)') AS GivenInSpecialty,
       COUNT(DISTINCT s.HospitalAdmissionKey) AS Admissions
FROM #stay AS s
LEFT JOIN dbo.DepartmentDim AS d ON d.DepartmentKey = s.AdministrationDepartmentKey
GROUP BY COALESCE(d.DepartmentSpecialty, N'(none)')
ORDER BY Admissions DESC;

/* Each admission flagged by each source, under the @icu list. */
DROP TABLE IF EXISTS #flags;
SELECT a.HospitalAdmissionKey, a.LengthOfStayInDays,
       CASE WHEN EXISTS (SELECT 1 FROM @icu AS i WHERE i.Specialty = ad.DepartmentSpecialty) THEN 1 ELSE 0 END AS AtAdmission,
       CASE WHEN EXISTS (SELECT 1 FROM @icu AS i WHERE i.Specialty = dd.DepartmentSpecialty) THEN 1 ELSE 0 END AS AtDischarge,
       CASE WHEN EXISTS (SELECT 1 FROM #stay AS s
                         INNER JOIN dbo.DepartmentDim AS d ON d.DepartmentKey = s.AdministrationDepartmentKey
                         INNER JOIN @icu AS i ON i.Specialty = d.DepartmentSpecialty
                         WHERE s.HospitalAdmissionKey = a.HospitalAdmissionKey) THEN 1 ELSE 0 END AS ByMedications
INTO #flags
FROM #adm AS a
LEFT JOIN dbo.DepartmentDim AS ad ON ad.DepartmentKey = a.DepartmentKey
LEFT JOIN dbo.DepartmentDim AS dd ON dd.DepartmentKey = a.DischargeDepartmentKey_X;

/* 9. How many admissions each source calls ICU, alone and together. */
SELECT COUNT(*) AS Admissions,
       SUM(AtAdmission) AS IcuAtAdmission,
       SUM(AtDischarge) AS IcuAtDischarge,
       SUM(ByMedications) AS IcuByMedications,
       SUM(CASE WHEN ByMedications = 1 AND AtAdmission = 0 AND AtDischarge = 0 THEN 1 ELSE 0 END) AS OnlyByMedications,
       SUM(CASE WHEN AtAdmission + AtDischarge + ByMedications > 0 THEN 1 ELSE 0 END) AS IcuAny
FROM #flags;

/* 10. A sense check: ICU stays should be longer. Median length of stay, ICU (any source) or not. */
SELECT DISTINCT
       CASE WHEN AtAdmission + AtDischarge + ByMedications > 0 THEN 'ICU' ELSE 'not ICU' END AS Stay,
       COUNT(*) OVER (PARTITION BY CASE WHEN AtAdmission + AtDischarge + ByMedications > 0 THEN 1 ELSE 0 END) AS Admissions,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY LengthOfStayInDays)
           OVER (PARTITION BY CASE WHEN AtAdmission + AtDischarge + ByMedications > 0 THEN 1 ELSE 0 END) AS MedianLengthOfStay
FROM #flags;

/* The ICU Stay Registry's stays for the same children that began within each admission
   (a day's leeway before it): matched by patient and time, so the EncounterKey link is tested, not assumed. */
DROP TABLE IF EXISTS #reg;
SELECT a.HospitalAdmissionKey, r.IcuStayRegistryKey, r.DepartmentKey, r.IcuLengthOfStay,
       CASE WHEN r.EncounterKey = a.EncounterKey THEN 1 ELSE 0 END AS SameEncounter
INTO #reg
FROM #adm AS a
INNER JOIN dbo.IcuStayRegistryDataMart AS r
    ON r.PatientDurableKey = a.PatientDurableKey
   AND r.IcuStayStartInstant BETWEEN DATEADD(DAY, -1, a.InpatientAdmissionInstant) AND a.DischargeInstant
WHERE r._IsDeleted = 0;

/* 11. How many admissions have a registry ICU stay, and is the stay on the admission's own EncounterKey? */
SELECT (SELECT COUNT(*) FROM #adm) AS InfantAdmissions,
       COUNT(DISTINCT HospitalAdmissionKey) AS AdmissionsWithRegistryStay,
       COUNT(*) AS RegistryStays,
       SUM(SameEncounter) AS StaysOnAdmissionEncounter,
       SUM(CASE WHEN IcuLengthOfStay IS NULL THEN 1 ELSE 0 END) AS StaysWithNoLength
FROM #reg;

/* 12. The specialty of the department each registry stay began in: admissions with any, every one. */
SELECT COALESCE(d.DepartmentSpecialty, N'(none)') AS RegistryStaySpecialty,
       COUNT(DISTINCT r.HospitalAdmissionKey) AS Admissions
FROM #reg AS r
LEFT JOIN dbo.DepartmentDim AS d ON d.DepartmentKey = r.DepartmentKey
GROUP BY COALESCE(d.DepartmentSpecialty, N'(none)')
ORDER BY Admissions DESC;

/* 13. The registry against the specialty rule (@icu): admissions in each pairing. */
SELECT CASE WHEN g.HospitalAdmissionKey IS NULL THEN 'no registry stay' ELSE 'registry ICU' END AS Registry,
       CASE WHEN f.AtAdmission + f.AtDischarge + f.ByMedications > 0 THEN 'rule ICU' ELSE 'rule not ICU' END AS SpecialtyRule,
       COUNT(*) AS Admissions
FROM #flags AS f
LEFT JOIN (SELECT DISTINCT HospitalAdmissionKey FROM #reg) AS g ON g.HospitalAdmissionKey = f.HospitalAdmissionKey
GROUP BY CASE WHEN g.HospitalAdmissionKey IS NULL THEN 'no registry stay' ELSE 'registry ICU' END,
         CASE WHEN f.AtAdmission + f.AtDischarge + f.ByMedications > 0 THEN 'rule ICU' ELSE 'rule not ICU' END
ORDER BY Registry, SpecialtyRule;
