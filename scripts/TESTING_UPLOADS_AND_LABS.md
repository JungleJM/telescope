# Testing Parquet Uploads and Lab Component Collation

Two new utilities are available to support your development priorities:

1. **Parquet Upload Testing** (`scripts/test_parquet_upload.py`) - Test the complete workflow of uploading a parquet as a primary key and batching fact extraction
2. **Lab Component Collation** (`scripts/lab_collator.py`) - Collate individual lab component results into single-row lab tests

## Priority 1: Testing Parquet Uploads with Batching

### Overview

Lab systems often store results as individual component facts (one row per component per patient per lab order). This utility helps you:

1. Create a test dataset of patients in parquet format
2. Generate a template that uses it as an uploaded primary key (PK)
3. Test batching by chunk size to verify facts are retrieved correctly
4. Verify the end-to-end workflow works

### Quick Start

```bash
# Generate 1000 sample patients and template
python3 scripts/test_parquet_upload.py --generate --num-patients 1000

# View info about the generated parquet
python3 scripts/test_parquet_upload.py --info --parquet YAMLs/test_ibd_patients.parquet

# Export template for transfer to VM
python3 scripts/makeYaml.py --template YAMLs/temp/test_upload_pk_temp.yaml --export-transfer

# On VM: Extract, validate, and run
python bundle.py
python pullmanager.py --extract
python pullmanager.py --template test_upload_pk_temp_transfer.yaml --validate
python pullmanager.py --export-split
python pullmanager.py --execute <project_name>
```

### Generated Files

- `YAMLs/test_ibd_patients.parquet` - Sample patient data with PatientDurableKey, Sex, BirthDateKey, IndexDateKey, CohortName
- `YAMLs/temp/test_upload_pk_temp.yaml` - Template that uses the parquet as an uploaded PK, batching by 100 rows at a time

### Key Features Tested

The generated template demonstrates:
- **Uploaded PK** (D97): Using a parquet file as the primary key source (type: pk)
- **Batching by chunk** (D53, D82): Extracting facts in batches of 100 patients at a time
- **Pending transfer** (D97): Marking files that will exist on the VM but not on Mac

### Customization

```bash
# Generate with different batch size
python3 scripts/test_parquet_upload.py --generate --num-patients 5000 --batch-size 250

# Generate with different output paths
python3 scripts/test_parquet_upload.py --generate \
    --parquet data/my_patients.parquet \
    --template YAMLs/temp/my_template_temp.yaml
```

### What Gets Pulled

For each batch of patients, the template pulls:
- **OtherDiagnoses**: All diagnosis events for those patients from DiagnosisEventFact, including the ICD-10 code, date range

This is a realistic fact table pull that demonstrates the batching mechanism working end-to-end.

---

## Priority 2: Lab Component Collation System

### Overview

Lab systems store results as individual component facts:

```
LabOrderKey | PatientKey | Component | Value | Date
12345       | 100001     | WBC       | 7.2   | 2024-01-15
12345       | 100001     | Hb        | 14.5  | 2024-01-15
12345       | 100001     | MCV       | 88    | 2024-01-15
```

This collator pivots them into single-row labs:

```
LabOrderKey | PatientKey | WBC | Hb   | MCV | Date
12345       | 100001     | 7.2 | 14.5 | 88  | 2024-01-15
```

### Quick Start

```bash
# Create sample lab component facts
python3 scripts/lab_collator.py --create-sample sample_labs.parquet \
    --num-patients 100 --num-orders 500

# List available components
python3 scripts/lab_collator.py --input sample_labs.parquet --list-components

# Collate specific lab tests (CBC components)
python3 scripts/lab_collator.py \
    --input sample_labs.parquet \
    --output cbc_labs.parquet \
    --components WBC Hb MCV Plt RBC

# Collate all labs
python3 scripts/lab_collator.py \
    --input sample_labs.parquet \
    --output all_labs.parquet
```

### Generated Files

When you `--create-sample`:
- Creates a parquet with component facts for realistic testing
- Columns: LabOrderKey, PatientDurableKey, SampleDateKey, LabComponentKey, LabComponentName, NumericValue, Unit
- Sample components: WBC, Hb, MCV, Plt, RBC, MCHC, Eosinophils

### Workflow for Real Data

1. **Export from Cosmos**: Use Pullmanager to pull `LabComponentResultFact` (all lab components for your cohort)
2. **Collate Components**: Use this utility to group components by lab order + date into single rows
3. **Create Parquet**: Save as parquet for use as a Supporting Table in future pulls
4. **Create Recipes**: Save the collated structure as a recipe, e.g., "CBC Labs" with WBC, Hb, MCV, Plt columns
5. **Trending**: Use the recipe in subsequent pulls to automatically collate and retrieve trending lab data

### Command Reference

```bash
python3 scripts/lab_collator.py [OPTIONS]

Options:
  --create-sample PATH              Create sample component facts file
  --num-patients N                  Patients in sample (default: 100)
  --num-orders N                    Lab orders in sample (default: 500)
  
  --input PATH                      Input parquet with component facts
  --output PATH                     Output parquet with collated labs
  --list-components                 List components in input file
  
  --key-column COLUMN               Lab order key (default: LabOrderKey)
  --date-column COLUMN              Sample date (default: SampleDateKey)
  --patient-column COLUMN           Patient key (default: PatientDurableKey)
  --component-column COLUMN         Component name (default: LabComponentName)
  --value-column COLUMN             Numeric value (default: NumericValue)
  --string-value-column COLUMN      String value (default: Value)
  
  --components C1 C2 C3             Only collate these components
```

### Integration with Pullmanager

Once you have collated labs in a parquet:

1. **Upload them** as a Supporting Table in a template:
   ```yaml
   upload_cohorts:
     - name: CBCLabs
       dest_table: CBCLabs
       file_type: parquet
       file_loc: cbc_labs.parquet
   ```

2. **Use in facts**:
   ```yaml
   cohorts:
     - name: PatientCBCs
       filter:
         from: CBCLabs
         where:
           - "SampleDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
   ```

3. **Create a recipe** (in YAML Manager):
   - Add custom table for CBC Labs with components as columns
   - Save as Recipe: "CBC Labs with Components"
   - Reuse in future templates

### Column Mapping (D98 Feature)

For real data, you may want to rename or drop columns as data lands in Projects:

```yaml
upload_cohorts:
  - name: CBCLabs
    dest_table: CBCLabs
    columns:
      - name: LabOrderKey
        from: OrderKey        # Rename OrderKey → LabOrderKey
      - name: OldColumn
        drop: true            # Don't include this column
      - name: WBC
        # Keep as-is
```

---

## Testing Strategy

### Test 1: Parquet Upload + Batching (Priority 1)

**Goal**: Prove that uploading a parquet as a PK and batching fact extraction works

```bash
# On Mac
python3 scripts/test_parquet_upload.py --generate --num-patients 1000
python3 scripts/makeYaml.py --template YAMLs/temp/test_upload_pk_temp.yaml --export-transfer
# Copy test_upload_pk_temp_transfer.yaml and YAMLs/test_ibd_patients.parquet to VM

# On VM (in new console window)
python bundle.py
python pullmanager.py
# Load transfer YAML in GUI, Export split, Preview SQL, Execute
# Verify:
# - PK lands in Projects as upload_PatientPK
# - Batches are created (b1of10, b2of10, ... for 1000 rows at 100/batch)
# - Each batch pulls OtherDiagnoses facts for its patients
# - Batch labels match _batch column
```

### Test 2: Lab Collation (Priority 2)

**Goal**: Prove that lab component results can be collated into single-row labs

```bash
# Create sample
python3 scripts/lab_collator.py --create-sample sample_labs.parquet --num-patients 50

# List and collate
python3 scripts/lab_collator.py --input sample_labs.parquet --list-components
python3 scripts/lab_collator.py --input sample_labs.parquet --output cbc_labs.parquet --components WBC Hb MCV

# Verify structure
python3 << 'EOF'
import pyarrow.parquet as pq
table = pq.read_table('cbc_labs.parquet')
print(f"Rows: {table.num_rows}")
print(f"Schema: {table.schema}")
print("\nFirst row:")
print(table.slice(0, 1).to_pandas())
EOF
```

### Full Workflow Test (Both Priorities)

1. Create patient PK parquet
2. Create lab component facts parquet  
3. Collate labs into single-row parquets
4. Export template with uploaded PK + collated labs as supporting table
5. Pull on VM with batching
6. Verify both uploads land correctly and facts are retrieved

---

## Next Steps

1. **Run Priority 1 test** on the VM with the generated template
2. **Run Priority 2 test** to collate your actual lab component data
3. **Report back** on batching behavior (performance, correctness, any issues)
4. **Create recipes** for common lab panels (CBC, CMP, etc.) once data structure is confirmed
5. **Build trending system** that uses recipes to automatically retrieve and collate trending labs

## Notes

- The generated template batches by chunk (100 rows) for simplicity. You can also batch by values (all distinct states, all distinct patient cohorts) or use split_after_build multipliers for more complex strategies.
- Lab collation is deterministic: same input gives same output, so collated parquets can be versioned and reused.
- The lab collator handles null values and missing components gracefully—components not present for a given lab order are simply not included in that row.
- Performance note (from roadmap, item 11): Batching cost is unmeasured. Measure batch efficiency on SneakPeek before tuning batch size or strategy.

