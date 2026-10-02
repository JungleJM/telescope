HaT PheWAS: hat\_ pull (YAMLs/temp/hat_phewas_intake.yaml)

Purpose: every patient with hereditary alpha tryptasemia (ICD-10-CM D89.44), and what a PheWAS needs about them. A later ctrl\_ pull will have the same columns. Window 2015-01-01 to 2026-06-01, wide on purpose; the analysis window is set later, in Python. Every table carries every column of its Cosmos tables (per the data dictionary), to be culled in Python. Dual (Cosmos + SneakPeek), 2,000 patients per chunk.

1.  hat_Patients (PK): one row per patient with ANY D89.44, at their first one. From DiagnosisEventFact, joined to:

    - DiagnosisTerminologyDim (Type 'ICD-10-CM', Value 'D89.44')
    - PatientDim (IsCurrent, IsValid, UseInCosmosAnalytics_X)
    - DurationDim (LEFT JOIN, for age) Dedup: one row per PatientDurableKey, earliest StartDateKey. Columns (85): the index diagnosis event (Index...), its terminology row (IndexDx...), age at index (AgeAtIndex...), and every PatientDim column. Expect about 5,967 rows. It is everyone with a code; the 1+ vs 2+ date case rule is applied later, in Python.

2.  hat_Encounters: every encounter for those patients. From EncounterFact, joined to the PK. One row per EncounterKey. Columns: all 39 EncounterFact columns, including DepartmentKey. Used for first/last encounter (observation time) and clinic-visit counts. This is the biggest table: every telephone, refill and message encounter is in it.

3.  hat_Diagnoses: every ICD-10-CM diagnosis event for those patients. From DiagnosisEventFact, joined to the PK and DiagnosisTerminologyDim (Type 'ICD-10-CM'). One row per DiagnosisEventKey + code. This is NOT one row per code: a code diagnosed on 10 dates gives 10+ rows. Columns (30): every DiagnosisEventFact and DiagnosisTerminologyDim column. These are the PheWAS events. D89.44 stays in here so its dates can be counted, and is dropped before the PheWAS.

Check after the pull: - hat_Patients rows are about 5,967, one per patient. - How many IndexDates are before 20211001, when D89.44 entered ICD-10-CM (see HaT Considerations.md)? - Rows per patient (median, max) for Encounters and Diagnoses. Before running: confirm project_db (PROJECTD93A5E7).