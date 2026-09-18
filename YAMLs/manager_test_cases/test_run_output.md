# Manager Test Case Run

Command pattern:

```bash
python3 scripts/makeYaml.py \
  --template YAMLs/manager_test_cases/<case>.yaml \
  --recipes YAMLs/recipes.yaml \
  --write \
  --out YAMLs/manager_test_cases/outputs/<case>_Full.yaml \
  --report \
  --report-out YAMLs/manager_test_cases/reports/<case>_report.md
```

## 00_problem_suite

Expected: fail with one representative error from each major validation family.

```text
ERROR [missing_recipe] Recipe `MissingRecipe` was not found. cohorts[0]
ERROR [missing_upload_file] Upload file not found: YAMLs/manager_test_cases/fixtures/does_not_exist.csv MissingHospitalCodes
ERROR [missing_variable] Cohort `badstagemissingsplitPatients` requires variable `max_date_key`, but no value was provided. filter.where[0]
ERROR [missing_variable] Cohort `badstagemissingsplitOtherHospitalizations` requires variable `max_date_key`, but no value was provided. filter.where[2]
ERROR [bad_multiplier_stage] Unsupported multiplier stage `mystery_stage`. BadStage
ERROR [missing_split_column] Multiplier `BadSplit` references missing column `MissingRace` on `PKTable`. missingsplit
ERROR [missing_batch_column] Batching `bad_batch_column` requires missing PK column `MissingBatchColumn`. PKTable
ERROR [bad_cosmos_db] Unsupported cosmos_db value `Mars`. cosmos_db
FAILED: errors block YAML generation
```

## 01_valid_basic

Expected: pass and write finished YAML.

```text
OK: finished YAML ready at YAMLs/manager_test_cases/outputs/01_valid_basic_Full.yaml
Wrote YAMLs/manager_test_cases/outputs/01_valid_basic_Full.yaml
```

## 02_valid_multipliers_batching

Expected: pass and write finished YAML.

```text
OK: finished YAML ready at YAMLs/manager_test_cases/outputs/02_valid_multipliers_batching_Full.yaml
Wrote YAMLs/manager_test_cases/outputs/02_valid_multipliers_batching_Full.yaml
```

## 03_warning_underscore_like

Expected: warn but pass and write finished YAML.

```text
WARN  [like_underscore] `_` in `K50_%` will be treated as a SQL LIKE single-character wildcard. Use [_] for a literal underscore. Patients.filter.where[8]
OK: finished YAML ready at YAMLs/manager_test_cases/outputs/03_warning_underscore_like_Full.yaml
Wrote YAMLs/manager_test_cases/outputs/03_warning_underscore_like_Full.yaml
```

## 04_missing_upload

Expected: fail because a referenced upload file is missing.

```text
ERROR [missing_upload_file] Upload file not found: YAMLs/manager_test_cases/fixtures/missing_hospital_icd_codes.csv HospitalICDCodes
FAILED: errors block YAML generation
```

## 05_mixed_column_errors

Expected: fail with upload-column, split-column, and batch-column errors.

```text
ERROR [missing_input_column] Cohort `CrohnsmissingraceOtherHospitalizations` uses `HospitalICDTable=HospitalICDCodes`, but `HospitalICDCodes` is missing columns: DiagnosisCode. HospitalICDTable
ERROR [missing_split_column] Multiplier `BadSplit` references missing column `MissingRace` on `PKTable`. missingrace
ERROR [missing_batch_column] Batching `bad_batch_column` requires missing PK column `MissingBatchColumn`. PKTable
FAILED: errors block YAML generation
```

