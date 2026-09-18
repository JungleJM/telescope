# Manager Report

OK: True

## Errors
- None

## Warnings
- `like_underscore`: `_` in `K50_%` will be treated as a SQL LIKE single-character wildcard. Use [_] for a literal underscore. Patients.filter.where[8]

## Expanded Cohorts
- Patients -> Patients
- OtherHospitalizations -> OtherHospitalizations

## Required Columns
- Patients: {}
- OtherHospitalizations: {'PKTable': ['PatientDurableKey'], 'HospitalICDTable': ['DiagnosisCode']}
