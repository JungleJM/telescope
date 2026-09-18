# Manager Report

OK: False

## Errors
- `missing_input_column`: Cohort `CrohnsmissingraceOtherHospitalizations` uses `HospitalICDTable=HospitalICDCodes`, but `HospitalICDCodes` is missing columns: DiagnosisCode. HospitalICDTable
- `missing_split_column`: Multiplier `BadSplit` references missing column `MissingRace` on `PKTable`. missingrace
- `missing_batch_column`: Batching `bad_batch_column` requires missing PK column `MissingBatchColumn`. PKTable

## Warnings
- None

## Expanded Cohorts
- CrohnsmissingracePatients -> CrohnsmissingracePatients
- CrohnsmissingraceOtherHospitalizations -> CrohnsmissingraceOtherHospitalizations

## Required Columns
- CrohnsmissingracePatients: {}
- CrohnsmissingraceOtherHospitalizations: {'PKTable': ['PatientDurableKey'], 'HospitalICDTable': ['DiagnosisCode']}
