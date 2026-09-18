# Manager Report

OK: False

## Errors
- `missing_recipe`: Recipe `MissingRecipe` was not found. cohorts[0]
- `missing_upload_file`: Upload file not found: YAMLs/manager_test_cases/fixtures/does_not_exist.csv MissingHospitalCodes
- `missing_variable`: Cohort `badstagemissingsplitPatients` requires variable `max_date_key`, but no value was provided. filter.where[0]
- `missing_variable`: Cohort `badstagemissingsplitOtherHospitalizations` requires variable `max_date_key`, but no value was provided. filter.where[2]
- `bad_multiplier_stage`: Unsupported multiplier stage `mystery_stage`. BadStage
- `missing_split_column`: Multiplier `BadSplit` references missing column `MissingRace` on `PKTable`. missingsplit
- `missing_batch_column`: Batching `bad_batch_column` requires missing PK column `MissingBatchColumn`. PKTable
- `bad_cosmos_db`: Unsupported cosmos_db value `Mars`. cosmos_db

## Warnings
- None

## Expanded Cohorts
- badstagemissingsplitPatients -> badstagemissingsplitPatients
- badstagemissingsplitOtherHospitalizations -> badstagemissingsplitOtherHospitalizations

## Required Columns
- badstagemissingsplitPatients: {}
- badstagemissingsplitOtherHospitalizations: {'PKTable': ['PatientDurableKey'], 'HospitalICDTable': ['DiagnosisCode']}
