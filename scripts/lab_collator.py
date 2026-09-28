#!/usr/bin/env python3
"""Lab component collator: Convert individual lab results into single-row labs.

Lab systems store results as individual component facts:
  One row per component per patient per lab order

  Lab Order Key | Patient Key | Component | Value | Date
  12345         | 100001      | WBC       | 7.2   | 2024-01-15
  12345         | 100001      | Hb        | 14.5  | 2024-01-15
  12345         | 100001      | MCV       | 88    | 2024-01-15

This script pivots them into single rows with components as columns:
  Lab Order Key | Patient Key | WBC | Hb   | MCV | Date
  12345         | 100001      | 7.2 | 14.5 | 88  | 2024-01-15

Usage:
    # Read component facts from a source (CSV or parquet)
    python3 scripts/lab_collator.py \\
        --input results.csv \\
        --key-column LabOrderKey \\
        --date-column SampleDateKey \\
        --patient-column PatientDurableKey \\
        --component-column ComponentName \\
        --value-column NumericValue \\
        --output cbc_labs.parquet

    # List available components in a file
    python3 scripts/lab_collator.py --input results.csv --list-components

    # Create a sample parquet for testing
    python3 scripts/lab_collator.py --create-sample output.parquet --num-patients 100
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


class LabCollator:
    """Collate lab component results into single-row lab tests."""

    def __init__(
        self,
        key_column: str = "LabOrderKey",
        date_column: str = "SampleDateKey",
        patient_column: str = "PatientDurableKey",
        component_column: str = "LabComponentKey",
        value_column: str = "NumericValue",
        string_value_column: str | None = "Value",
    ):
        """Initialize collator.

        Args:
            key_column: Column holding the lab order key (unique per order)
            date_column: Column holding sample date
            patient_column: Column holding patient key
            component_column: Column holding component name/code
            value_column: Column holding numeric values
            string_value_column: Column holding string/categorical values
        """
        self.key_column = key_column
        self.date_column = date_column
        self.patient_column = patient_column
        self.component_column = component_column
        self.value_column = value_column
        self.string_value_column = string_value_column

    def collate(
        self,
        table: pa.Table,
        components_to_extract: list[str] | None = None,
    ) -> pa.Table:
        """Collate a component fact table into single-row labs.

        Args:
            table: PyArrow table with component facts
            components_to_extract: List of components to extract (None = all)

        Returns:
            PyArrow table with one row per (key, date, patient) combination
        """
        import pandas as pd

        # Convert to pandas for easier pivoting
        df = table.to_pandas()

        # Get list of components
        all_components = df[self.component_column].unique()
        if components_to_extract:
            all_components = [c for c in all_components if c in components_to_extract]

        print(f"Found {len(all_components)} components: {sorted(all_components)}")

        # Create pivot table
        # Group by (LabOrderKey, SampleDateKey, PatientKey)
        # Pivot on component to create columns

        result_rows = []

        for (key, date, patient), group in df.groupby([self.key_column, self.date_column, self.patient_column]):
            row = {
                self.key_column: key,
                self.date_column: date,
                self.patient_column: patient,
            }

            # Extract each component's value
            for component in all_components:
                component_rows = group[group[self.component_column] == component]
                if len(component_rows) > 0:
                    # Take first value if multiple
                    comp_row = component_rows.iloc[0]

                    # Try numeric value first, then string value
                    if self.value_column in component_rows.columns:
                        value = comp_row[self.value_column]
                        row[str(component)] = value
                    elif self.string_value_column and self.string_value_column in component_rows.columns:
                        value = comp_row[self.string_value_column]
                        row[str(component)] = value

            result_rows.append(row)

        # Convert back to arrow table
        # Need to handle different types carefully
        result_df = pd.DataFrame(result_rows)
        result_table = pa.Table.from_pandas(result_df)

        print(f"Collated {len(result_rows)} labs from {len(df)} component facts")
        print(f"Schema: {result_table.schema}")

        return result_table

    def list_components(self, table: pa.Table) -> None:
        """List unique components in the table."""
        df = table.to_pandas()
        components = df[self.component_column].unique()
        print(f"Found {len(components)} unique components:")
        for comp in sorted(components):
            count = len(df[df[self.component_column] == comp])
            print(f"  {comp:20} ({count} results)")


def create_sample_component_facts(
    output_path: Path,
    num_patients: int = 100,
    num_lab_orders: int = 500,
    components: list[str] | None = None,
) -> None:
    """Create a sample parquet with lab component facts.

    Schema:
    - LabOrderKey: unique lab order
    - PatientDurableKey: patient identifier
    - SampleDateKey: date key (YYYYMMDD)
    - LabComponentKey: component ID
    - LabComponentName: component name (WBC, Hb, MCV, etc.)
    - NumericValue: numeric result
    - Value: string result
    - Unit: units of measurement
    """
    import random

    if components is None:
        components = ["WBC", "Hb", "MCV", "Plt", "RBC", "MCHC", "Eosinophils"]

    print(f"Creating sample lab facts: {num_patients} patients, {num_lab_orders} orders...")

    lab_order_keys = list(range(1000000, 1000000 + num_lab_orders))
    patient_keys = list(range(100000, 100000 + num_patients))

    # Component reference data
    component_map = {
        "WBC": ("white blood cell count", "K/uL", 7.2),
        "Hb": ("hemoglobin", "g/dL", 14.5),
        "MCV": ("mean corpuscular volume", "fL", 88),
        "Plt": ("platelet count", "K/uL", 250),
        "RBC": ("red blood cell count", "M/uL", 4.8),
        "MCHC": ("mean corpuscular hemoglobin concentration", "g/dL", 33),
        "Eosinophils": ("eosinophil count", "%", 3),
    }

    facts = []
    for order_key in lab_order_keys:
        patient_key = random.choice(patient_keys)
        date_key = random.randint(20200101, 20240601)

        # Each order has 3-5 components
        num_components = random.randint(3, 5)
        order_components = random.sample(components, min(num_components, len(components)))

        for component_name in order_components:
            component_id = components.index(component_name) + 1
            component_desc, unit, normal_value = component_map.get(component_name, (component_name, "", None))

            # Add some variation
            if normal_value:
                value = normal_value + random.uniform(-2, 2)
            else:
                value = random.uniform(0, 100)

            facts.append({
                "LabOrderKey": order_key,
                "PatientDurableKey": patient_key,
                "SampleDateKey": date_key,
                "LabComponentKey": component_id,
                "LabComponentName": component_name,
                "LabComponentDescription": component_desc,
                "NumericValue": value,
                "Value": None,  # Numeric results use NumericValue
                "Unit": unit,
            })

    # Create Arrow table
    table = pa.table({
        "LabOrderKey": pa.array([f["LabOrderKey"] for f in facts], type=pa.int64()),
        "PatientDurableKey": pa.array([f["PatientDurableKey"] for f in facts], type=pa.int64()),
        "SampleDateKey": pa.array([f["SampleDateKey"] for f in facts], type=pa.int32()),
        "LabComponentKey": pa.array([f["LabComponentKey"] for f in facts], type=pa.int32()),
        "LabComponentName": pa.array([f["LabComponentName"] for f in facts], type=pa.string()),
        "LabComponentDescription": pa.array([f["LabComponentDescription"] for f in facts], type=pa.string()),
        "NumericValue": pa.array([f["NumericValue"] for f in facts], type=pa.float64()),
        "Unit": pa.array([f["Unit"] for f in facts], type=pa.string()),
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path)

    print(f"✓ Created {output_path}")
    print(f"  Rows: {table.num_rows}")
    print(f"  Sample components: {sorted(set(f['LabComponentName'] for f in facts[:10]))}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collate lab component results into single-row labs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", type=Path, help="Input parquet/CSV with component facts")
    parser.add_argument("--output", type=Path, help="Output parquet with collated labs")
    parser.add_argument("--list-components", action="store_true", help="List components in input file")
    parser.add_argument("--create-sample", type=Path, help="Create sample component facts file")
    parser.add_argument("--num-patients", type=int, default=100, help="Number of patients for sample")
    parser.add_argument("--num-orders", type=int, default=500, help="Number of lab orders for sample")
    parser.add_argument("--key-column", default="LabOrderKey", help="Lab order key column name")
    parser.add_argument("--date-column", default="SampleDateKey", help="Sample date column name")
    parser.add_argument("--patient-column", default="PatientDurableKey", help="Patient key column name")
    parser.add_argument("--component-column", default="LabComponentName", help="Component name column name")
    parser.add_argument("--value-column", default="NumericValue", help="Numeric value column name")
    parser.add_argument("--string-value-column", default="Value", help="String value column name")
    parser.add_argument("--components", nargs="+", help="Components to extract (default: all)")

    args = parser.parse_args()

    # Create sample
    if args.create_sample:
        create_sample_component_facts(
            args.create_sample,
            num_patients=args.num_patients,
            num_lab_orders=args.num_orders,
        )
        return 0

    # List components
    if args.list_components:
        if not args.input:
            print("ERROR: --input required with --list-components", file=sys.stderr)
            return 1

        table = pq.read_table(args.input)
        collator = LabCollator(
            key_column=args.key_column,
            date_column=args.date_column,
            patient_column=args.patient_column,
            component_column=args.component_column,
            value_column=args.value_column,
        )
        collator.list_components(table)
        return 0

    # Collate labs
    if args.input and args.output:
        table = pq.read_table(args.input)
        collator = LabCollator(
            key_column=args.key_column,
            date_column=args.date_column,
            patient_column=args.patient_column,
            component_column=args.component_column,
            value_column=args.value_column,
            string_value_column=args.string_value_column,
        )
        collated = collator.collate(table, components_to_extract=args.components)
        pq.write_table(collated, args.output)
        print(f"✓ Wrote {args.output}")
        return 0

    # Default: show usage
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
