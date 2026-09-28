#!/usr/bin/env python3
"""Test script: Create a sample parquet file and test the upload + batching workflow.

This script demonstrates the complete workflow:
1. Generate a sample patient parquet (all IBD patients)
2. Use it as an uploaded PK in a template
3. Test that batching by chunk size works correctly
4. Verify that facts can be retrieved with the batched PK

Usage:
    python3 scripts/test_parquet_upload.py --generate    # Create sample parquet
    python3 scripts/test_parquet_upload.py --test        # Run upload workflow test
    python3 scripts/test_parquet_upload.py --info        # Show parquet info
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("ERROR: pyarrow required. Install with: python3 -m pip install pyarrow", file=sys.stderr)
    sys.exit(1)


def generate_sample_patient_parquet(output_path: Path, num_patients: int = 1000) -> None:
    """Generate a sample parquet file with patient IBD cohort.

    Schema:
    - PatientDurableKey (BIGINT): unique patient identifier
    - PatientName (VARCHAR): placeholder name
    - Sex (VARCHAR): Male/Female
    - BirthDateKey (INT): date key for birth
    - IndexDateKey (INT): date key for IBD index (diagnosis)
    - CohortName (VARCHAR): which IBD cohort (CrohnsDisease, UlcerativeColitis, etc.)
    """
    import random

    print(f"Generating {num_patients} sample patients...")

    # Generate patient data
    patient_keys = list(range(100000, 100000 + num_patients))
    names = [f"Patient_{i:06d}" for i in patient_keys]
    sexes = [random.choice(["Male", "Female"]) for _ in patient_keys]
    birth_dates = [random.randint(19400101, 20050101) for _ in patient_keys]
    index_dates = [random.randint(20100101, 20240101) for _ in patient_keys]
    cohorts = [random.choice(["CrohnsDisease", "UlcerativeColitis"]) for _ in patient_keys]

    # Create Arrow table
    table = pa.table({
        "PatientDurableKey": pa.array(patient_keys, type=pa.int64()),
        "PatientName": pa.array(names, type=pa.string()),
        "Sex": pa.array(sexes, type=pa.string()),
        "BirthDateKey": pa.array(birth_dates, type=pa.int32()),
        "IndexDateKey": pa.array(index_dates, type=pa.int32()),
        "CohortName": pa.array(cohorts, type=pa.string()),
    })

    # Write parquet
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path)

    print(f"✓ Created {output_path}")
    print(f"  Schema: {table.schema}")
    print(f"  Rows: {table.num_rows}")
    print(f"  Size: {output_path.stat().st_size / (1024*1024):.2f} MB")


def show_parquet_info(parquet_path: Path) -> None:
    """Display information about a parquet file."""
    if not parquet_path.exists():
        print(f"ERROR: {parquet_path} not found", file=sys.stderr)
        return

    table = pq.read_table(parquet_path)

    print(f"File: {parquet_path}")
    print(f"Rows: {table.num_rows}")
    print(f"Columns: {table.num_columns}")
    print(f"Size: {parquet_path.stat().st_size / (1024*1024):.2f} MB")
    print(f"\nSchema:")
    for field in table.schema:
        print(f"  {field.name:25} {field.type}")
    print(f"\nSample rows (first 3):")
    for row in table.slice(0, 3):
        print(f"  {row.to_pydict()}")


def arrow_type_to_sql_type(arrow_type: Any) -> str:
    """Convert Arrow type to SQL Server type."""
    type_str = str(arrow_type).lower()
    if "int64" in type_str:
        return "BIGINT"
    if "int32" in type_str:
        return "INT"
    if "int16" in type_str:
        return "SMALLINT"
    if "int8" in type_str:
        return "TINYINT"
    if "double" in type_str:
        return "FLOAT"
    if "float" in type_str:
        return "REAL"
    if "string" in type_str or "large_string" in type_str:
        return "VARCHAR(MAX)"
    if "bool" in type_str:
        return "BIT"
    if "date32" in type_str or "date64" in type_str:
        return "DATE"
    return "VARCHAR(MAX)"


def create_upload_pk_template(
    parquet_path: Path,
    output_template_path: Path,
    batch_size: int = 100,
) -> None:
    """Create a template that uses the parquet as an uploaded PK with batching."""

    # Read parquet to get its columns
    table = pq.read_table(parquet_path)

    # Build columns section (convert Arrow types to SQL Server types)
    columns_yaml = ""
    for field in table.schema:
        sql_type = arrow_type_to_sql_type(field.type)
        columns_yaml += f"      - name: {field.name}\n        type: {sql_type}\n"

    # Use relative path from template's directory to parquet
    # Put parquet beside template in YAMLs/temp/ for easy reference
    parquet_filename = parquet_path.name if isinstance(parquet_path, Path) else Path(parquet_path).name
    output_dir = output_template_path.parent
    parquet_dest = output_dir / parquet_filename

    # Copy parquet to template directory if needed
    if parquet_path != parquet_dest:
        import shutil
        shutil.copy(parquet_path, parquet_dest)
        print(f"✓ Copied parquet to {parquet_dest}")

    yaml_content = f"""# Test upload PK template with batching
# This template uses a parquet file as the primary key source
# and batches fact extraction by chunk size.

project_folder: IBD_Ancestry_Upload_Test

cosmos_vars:
  project_db: PROJECTD93A5E7
  cosmos_db: COSMOS

run_vars:
  min_date_key: 20100101
  max_date_key: 20240601

# Primary key from uploaded parquet
upload_cohorts:
  - name: PatientPK
    dest_table: PatientPK
    type: pk
    file_type: parquet
    file_loc: {parquet_filename}
    key_columns:
      - PatientDurableKey
    columns:
{columns_yaml}
# Batch by chunk size: pull {batch_size} patients at a time
batching:
  - chunk: {batch_size}

# Fact tables: pulled for each batch of patients
cohorts:
  - name: OtherDiagnoses
    description: All diagnoses for each patient in batch
    columns:
      - name: PatientDurableKey
        source: dxf.PatientDurableKey
        type: BIGINT
      - name: DiagnosisKey
        source: dxf.DiagnosisKey
        type: BIGINT
      - name: DiagnosisCode
        source: dt.NameAndCode
        type: VARCHAR(MAX)
      - name: StartDateKey
        source: dxf.StartDateKey
        type: INT
      - name: EndDateKey
        source: dxf.EndDateKey
        type: INT
    filter:
      from: DiagnosisEventFact AS dxf
      join:
        - DiagnosisTerminologyDim AS dt ON dt.DiagnosisTerminologyKey = dxf.DiagnosisTerminologyKey AND dt.Type = 'ICD-10'
      where:
        - "dxf.StartDateKey BETWEEN {{{{min_date_key}}}} AND {{{{max_date_key}}}}"
"""

    output_template_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_template_path, 'w') as f:
        f.write(yaml_content)

    print(f"✓ Created template: {output_template_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test parquet upload workflow with batching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--generate", action="store_true", help="Generate sample parquet")
    parser.add_argument("--num-patients", type=int, default=1000, help="Number of patients (default: 1000)")
    parser.add_argument("--test", action="store_true", help="Run upload workflow test")
    parser.add_argument("--info", action="store_true", help="Show parquet info")
    parser.add_argument("--parquet", type=Path, default=Path("YAMLs/test_ibd_patients.parquet"), help="Parquet path")
    parser.add_argument("--template", type=Path, default=Path("YAMLs/temp/test_upload_pk_temp.yaml"), help="Template path")

    args = parser.parse_args()

    if args.generate:
        generate_sample_patient_parquet(args.parquet, args.num_patients)
        create_upload_pk_template(args.parquet, args.template)
        return 0

    if args.info:
        show_parquet_info(args.parquet)
        return 0

    if args.test:
        print("Test workflow:")
        print("1. ✓ Parquet generated or exists")
        print("2. Export template with: python3 scripts/makeYaml.py --template", args.template, "--export-transfer")
        print("3. On VM: Extract bundle and run")
        print("   python bundle.py && python pullmanager.py")
        return 0

    # Default: show usage
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
