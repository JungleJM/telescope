"""Uploads (D54): parquet and dbtable, landing typed in Projects first."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..uploads import (
    LENGTH_HEADROOM,
    UploadError,
    copy_table,
    enabled_uploads,
    read_parquet,
    render_copy_dbtable,
    render_create,
    type_from_info,
    upload_kind,
)

try:
    import pyarrow
    import pyarrow.parquet
except ImportError:  # the VM has it; tests needing it skip elsewhere
    pyarrow = None


class NamingTests(unittest.TestCase):
    def test_the_copy_is_marked_as_an_upload(self):
        self.assertEqual(copy_table("HospitalICDCodes"), "upload_HospitalICDCodes")
        self.assertEqual(copy_table("##JVM_HospitalICDCodes"), "upload_HospitalICDCodes")


class KindTests(unittest.TestCase):
    def test_accepts_parquet_and_dbtable(self):
        for kind in ("parquet", "dbtable"):
            self.assertEqual(upload_kind({"name": "x", "file_type": kind}), kind)

    def test_a_csv_is_sent_back_to_the_split(self):
        # Splits convert CSVs to typed parquet (D54); one arriving here is old.
        with self.assertRaises(UploadError) as caught:
            upload_kind({"name": "x", "file_type": "csv"})
        self.assertIn("Export the split again", str(caught.exception))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(UploadError):
            upload_kind({"name": "x", "file_type": "xlsx"})

    def test_push_this_cycle_gates_uploads(self):
        doc = {"upload_cohorts": [
            {"name": "a"}, {"name": "b", "push_this_cycle": False}, "not a mapping",
        ]}
        self.assertEqual([u["name"] for u in enabled_uploads(doc)], ["a"])


class RenderTests(unittest.TestCase):
    def test_create_declares_each_type(self):
        sql = render_create("PROJECTD1.dbo.upload_X", [("Key", "BIGINT"), ("Label", "NVARCHAR(60)")])
        self.assertIn("DROP TABLE IF EXISTS PROJECTD1.dbo.upload_X;", sql)
        self.assertIn("[Key] BIGINT NULL", sql)
        self.assertIn("[Label] NVARCHAR(60) NULL", sql)

    def test_a_dbtable_is_copied_server_side(self):
        sql = render_copy_dbtable("P.dbo.upload_X", "P.dbo.X")
        self.assertIn("SELECT * INTO P.dbo.upload_X FROM P.dbo.X;", sql)

    def test_types_are_rebuilt_from_information_schema(self):
        self.assertEqual(type_from_info("nvarchar", -1, None, None, None), "NVARCHAR(MAX)")
        self.assertEqual(type_from_info("varchar", 40, None, None, None), "VARCHAR(40)")
        self.assertEqual(type_from_info("decimal", None, 10, 2, None), "DECIMAL(10,2)")
        self.assertEqual(type_from_info("datetime2", None, None, None, 7), "DATETIME2(7)")
        self.assertEqual(type_from_info("bigint", None, 19, 0, None), "BIGINT")


@unittest.skipUnless(pyarrow, "needs pyarrow")
class ParquetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, columns: dict) -> dict:
        pyarrow.parquet.write_table(pyarrow.table(columns), str(self.root / "f.parquet"))
        return {"name": "F", "dest_table": "F", "file_type": "parquet", "file_loc": "f.parquet"}

    def test_the_files_types_become_sql_types(self):
        import datetime
        import decimal

        table = read_parquet(self.write({
            "Key": pyarrow.array([1, 2], pyarrow.int64()),
            "Small": pyarrow.array([1, 2], pyarrow.int32()),
            "Score": pyarrow.array([1.5, 2.5]),
            "Flag": pyarrow.array([True, False]),
            "Day": pyarrow.array([datetime.date(2024, 1, 1), None]),
            "Amount": pyarrow.array([decimal.Decimal("1.25"), None], pyarrow.decimal128(10, 2)),
            "Label": pyarrow.array(["abc", None]),
        }), self.root)
        self.assertEqual(dict(table.columns), {
            "Key": "BIGINT", "Small": "INT", "Score": "FLOAT", "Flag": "BIT", "Day": "DATE",
            "Amount": "DECIMAL(10,2)", "Label": f"NVARCHAR({3 + LENGTH_HEADROOM})",
        })
        self.assertEqual(table.rows[0][0], 1)

    def test_text_is_sized_from_the_longest_value(self):
        table = read_parquet(self.write({"Label": ["x" * 120]}), self.root)
        self.assertEqual(table.columns, [("Label", f"NVARCHAR({120 + LENGTH_HEADROOM})")])

    def test_a_declared_type_converts_the_column(self):
        # R writes large IDs as doubles; declaring BIGINT lands them as numbers.
        cohort = self.write({"PatientDurableKey": [1.0, 2.0]})
        cohort["columns"] = [{"name": "PatientDurableKey", "type": "BIGINT"}]
        table = read_parquet(cohort, self.root)
        self.assertEqual(table.columns, [("PatientDurableKey", "BIGINT")])
        self.assertEqual([row[0] for row in table.rows], [1, 2])
        self.assertIsInstance(table.rows[0][0], int)

    def test_a_value_that_does_not_fit_names_its_column(self):
        cohort = self.write({"PatientDurableKey": [1.5]})
        cohort["columns"] = [{"name": "PatientDurableKey", "type": "BIGINT"}]
        with self.assertRaises(UploadError) as caught:
            read_parquet(cohort, self.root)
        self.assertIn("`PatientDurableKey`", str(caught.exception))

    def test_a_declared_column_the_file_lacks_is_refused(self):
        cohort = self.write({"Key": [1]})
        cohort["columns"] = [{"name": "Nope", "type": "BIGINT"}]
        with self.assertRaises(UploadError) as caught:
            read_parquet(cohort, self.root)
        self.assertIn("Nope", str(caught.exception))

    def test_a_renamed_column_lands_under_its_new_name_with_its_type(self):
        # D98: `from:` is the file's name for it; the type is declared on it too.
        cohort = self.write({"ICD10": ["K50.0", "K51.9"], "Key": [1.0, 2.0]})
        cohort["columns"] = [{"name": "ICDCode", "from": "ICD10"},
                             {"name": "PatientDurableKey", "from": "Key", "type": "BIGINT"}]
        table = read_parquet(cohort, self.root)
        self.assertEqual([name for name, _ in table.columns], ["ICDCode", "PatientDurableKey"])
        self.assertEqual(table.columns[1], ("PatientDurableKey", "BIGINT"))
        self.assertEqual(table.rows, [("K50.0", 1), ("K51.9", 2)])

    def test_a_dropped_column_does_not_land_and_the_rest_are_kept(self):
        cohort = self.write({"Keep": [1], "Secret": ["x"], "Also": ["y"]})
        cohort["columns"] = [{"name": "Secret", "drop": True}]
        table = read_parquet(cohort, self.root)
        self.assertEqual([name for name, _ in table.columns], ["Keep", "Also"])
        self.assertEqual(table.rows, [(1, "y")])

    def test_renaming_or_dropping_a_column_the_file_lacks_is_refused(self):
        for entry in ({"name": "New", "from": "Nope"}, {"name": "Nope", "drop": True}):
            cohort = self.write({"Key": [1]})
            cohort["columns"] = [entry]
            with self.subTest(entry=entry), self.assertRaises(UploadError) as caught:
                read_parquet(cohort, self.root)
            self.assertIn("Nope", str(caught.exception))

    def test_two_columns_ending_with_one_name_are_refused(self):
        cohort = self.write({"A": [1], "B": [2]})
        cohort["columns"] = [{"name": "B", "from": "A"}]
        with self.assertRaises(UploadError) as caught:
            read_parquet(cohort, self.root)
        self.assertIn("B", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(UploadError):
            read_parquet({"name": "F", "file_type": "parquet", "file_loc": "gone.parquet"}, self.root)
