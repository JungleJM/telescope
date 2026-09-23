"""CSV and dbtable uploads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..uploads import (
    LENGTH_HEADROOM,
    MAX_COLUMN_WIDTH,
    MIN_COLUMN_WIDTH,
    UploadError,
    enabled_uploads,
    measure_widths,
    plan_csv_upload,
    read_csv,
    render_create,
    safe_identifier,
    upload_kind,
)


class IdentifierTests(unittest.TestCase):
    def test_normalizes_awkward_headers(self):
        cases = [
            ("Medication Key", "Medication_Key"),
            ("Therapeutic-Class", "Therapeutic_Class"),
            ("2ndCode", "_2ndCode"),
            ("  spaced  ", "spaced"),
            ("a.b.c", "a_b_c"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(safe_identifier(raw, 0), expected)

    def test_blank_header_gets_a_position_name(self):
        self.assertEqual(safe_identifier("", 3), "Column4")


class CsvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, text, name="codes.csv", encoding="utf-8"):
        path = self.root / name
        path.write_text(text, encoding=encoding)
        return path

    def test_reads_headers_and_rows(self):
        path = self.write("Key,Name\n46,RISANKIZUMAB\n403,HUMIRA\n")
        columns, rows = read_csv(path)
        self.assertEqual(columns, ["Key", "Name"])
        self.assertEqual(rows, [("46", "RISANKIZUMAB"), ("403", "HUMIRA")])

    def test_strips_a_byte_order_mark(self):
        # A BOM otherwise becomes part of the first column name and silently
        # breaks every reference to it.
        path = self.write("Key,Name\n1,x\n", encoding="utf-8-sig")
        columns, _ = read_csv(path)
        self.assertEqual(columns[0], "Key")

    def test_empty_cells_become_null(self):
        path = self.write("Key,Name\n1,\n")
        _, rows = read_csv(path)
        self.assertEqual(rows, [("1", None)])

    def test_short_rows_are_padded(self):
        path = self.write("A,B,C\n1,2\n")
        _, rows = read_csv(path)
        self.assertEqual(rows, [("1", "2", None)])

    def test_blank_lines_are_dropped(self):
        path = self.write("A\n1\n\n2\n")
        _, rows = read_csv(path)
        self.assertEqual(rows, [("1",), ("2",)])

    def test_duplicate_headers_are_made_unique(self):
        path = self.write("Name,Name\n1,2\n")
        columns, _ = read_csv(path)
        self.assertEqual(columns, ["Name", "Name_1"])

    def test_quotes_survive_binding(self):
        # Values are bound, not interpolated, so an apostrophe needs no escaping.
        path = self.write("Name\n\"HUMIRA(CF) CROHN'S STARTER\"\n")
        _, rows = read_csv(path)
        self.assertEqual(rows[0][0], "HUMIRA(CF) CROHN'S STARTER")

    def test_missing_file_is_refused(self):
        with self.assertRaises(UploadError):
            read_csv(self.root / "nope.csv")

    def test_empty_file_is_refused(self):
        with self.assertRaises(UploadError):
            read_csv(self.write(""))

    def test_header_only_uploads_an_empty_table_with_a_note(self):
        self.write("Key,Name\n")
        plan = plan_csv_upload(
            {"name": "U", "dest_table": "U", "file_type": "csv", "file_loc": "codes.csv"},
            self.root,
        )
        self.assertEqual(plan.rows, [])
        self.assertTrue(plan.notes)


class WidthTests(unittest.TestCase):
    def test_sizes_from_the_data_with_headroom(self):
        # The whole file is in hand before the table exists, so measuring works
        # here even though it cannot for a batched pull.
        widths = measure_widths(["A"], [("x" * 100,)])
        self.assertEqual(widths["A"], 150)

    def test_width_is_the_longest_value_plus_headroom(self):
        self.assertEqual(measure_widths(["A"], [("x",)])["A"], 1 + LENGTH_HEADROOM)

    def test_an_all_null_column_falls_back_to_the_floor(self):
        # The floor only binds when there is nothing to measure.
        self.assertEqual(measure_widths(["A"], [(None,)])["A"], MIN_COLUMN_WIDTH)

    def test_width_is_capped(self):
        self.assertEqual(measure_widths(["A"], [("x" * 9000,)])["A"], MAX_COLUMN_WIDTH)

    def test_create_uses_the_measured_widths(self):
        from ..uploads import UploadPlan

        plan = UploadPlan(
            name="U", dest_table="U", global_temp="##JVM_U",
            columns=["A"], rows=[("x" * 100,)], widths={"A": 150},
        )
        sql = render_create(plan)
        self.assertIn("DROP TABLE IF EXISTS ##JVM_U;", sql)
        self.assertIn("[A] NVARCHAR(150) NULL", sql)


class KindTests(unittest.TestCase):
    def test_accepts_csv_and_dbtable(self):
        self.assertEqual(upload_kind({"file_type": "csv"}), "csv")
        self.assertEqual(upload_kind({"file_type": "DBTable"}), "dbtable")

    def test_parquet_is_refused_with_guidance(self):
        with self.assertRaises(UploadError) as caught:
            upload_kind({"name": "U", "file_type": "parquet"})
        self.assertIn("Cosmos cannot read", str(caught.exception))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(UploadError):
            upload_kind({"name": "U", "file_type": "xlsx"})

    def test_push_this_cycle_gates_uploads(self):
        doc = {"upload_cohorts": [
            {"name": "A", "push_this_cycle": True},
            {"name": "B", "push_this_cycle": False},
            {"name": "C"},
        ]}
        self.assertEqual([u["name"] for u in enabled_uploads(doc)], ["A", "C"])
