"""Turning a logical batch into a selection over the local PK table."""

from __future__ import annotations

import unittest

from ..batches import BatchError, count_batch_rows, dimension_predicate, select_batch_rows

PROJECT_DB = "PROJECTD93A5E7"
KEYS = ["PatientDurableKey"]


def batch(dimensions=None, runtime=None, name="B"):
    return {"name": name, "dimensions": dimensions or [], "runtime": runtime or []}


def value_dim(column, value, name="d"):
    return {"name": name, "kind": "column_values", "column": column, "value": value}


class PredicateTests(unittest.TestCase):
    def test_value_dimension_binds_its_value(self):
        clause, params = dimension_predicate(value_dim("Sex", "Female"))
        self.assertEqual(clause, "[Sex] = ?")
        self.assertEqual(params, ["Female"])

    def test_catch_all_excludes_the_named_values_and_keeps_nulls(self):
        # NULL is not 'not in' anything in SQL, so without the explicit test the
        # catch-all would silently drop rows with no value.
        clause, params = dimension_predicate(
            {"name": "sex", "column": "Sex", "is_other": True, "excludes": ["Female", "Male"]}
        )
        self.assertEqual(clause, "([Sex] NOT IN (?, ?) OR [Sex] IS NULL)")
        self.assertEqual(params, ["Female", "Male"])

    def test_catch_all_without_exclusions_is_refused(self):
        # It would otherwise select every row.
        with self.assertRaises(BatchError):
            dimension_predicate({"name": "sex", "column": "Sex", "is_other": True})

    def test_dimension_without_a_column_is_refused(self):
        with self.assertRaises(BatchError):
            dimension_predicate({"name": "sex", "value": "Female"})


class SelectionTests(unittest.TestCase):
    def test_no_batch_selects_the_whole_pk(self):
        selection = select_batch_rows(PROJECT_DB, "Patients", None, KEYS)
        self.assertEqual(selection.sql, "SELECT * FROM PROJECTD93A5E7.dbo.Patients;")
        self.assertEqual(selection.params, [])

    def test_selects_whole_rows_not_just_keys(self):
        # Batching selects on PK attributes, and cohort joins may use them.
        selection = select_batch_rows(
            PROJECT_DB, "Patients", batch([value_dim("Sex", "Female")]), KEYS
        )
        self.assertTrue(selection.sql.startswith("SELECT * FROM"))

    def test_combines_dimensions_with_and(self):
        selection = select_batch_rows(
            PROJECT_DB,
            "Patients",
            batch([value_dim("StateOrProvinceAbbreviation", "LA"), value_dim("Sex", "Female")]),
            KEYS,
        )
        self.assertIn("[StateOrProvinceAbbreviation] = ?", selection.sql)
        self.assertIn("AND [Sex] = ?", selection.sql)
        self.assertEqual(selection.params, ["LA", "Female"])

    def test_chunking_orders_by_the_key(self):
        selection = select_batch_rows(
            PROJECT_DB,
            "Patients",
            batch(runtime=[{"name": "chunk", "kind": "row_chunk", "rows_per_batch": 2000}]),
            KEYS,
            chunk_index=2,
        )
        self.assertIn("ORDER BY [PatientDurableKey]", selection.sql)
        self.assertIn("OFFSET 4000 ROWS FETCH NEXT 2000 ROWS ONLY", selection.sql)

    def test_chunking_needs_key_columns(self):
        # Without a total order a chunk means different rows each run.
        with self.assertRaises(BatchError):
            select_batch_rows(
                PROJECT_DB,
                "Patients",
                batch(runtime=[{"name": "chunk", "kind": "row_chunk", "rows_per_batch": 10}]),
                [],
            )

    def test_values_all_is_refused_with_an_explanation(self):
        with self.assertRaises(BatchError) as caught:
            select_batch_rows(
                PROJECT_DB,
                "Patients",
                batch(runtime=[
                    {"name": "state", "kind": "column_values", "values": "all"},
                    {"name": "chunk", "kind": "row_chunk", "rows_per_batch": 10},
                ]),
                KEYS,
            )
        self.assertIn("values: all", str(caught.exception))

    def test_bad_chunk_size_is_refused(self):
        for size in (0, -1, "lots"):
            with self.subTest(size=size):
                with self.assertRaises(BatchError):
                    select_batch_rows(
                        PROJECT_DB,
                        "Patients",
                        batch(runtime=[{"name": "c", "kind": "row_chunk", "rows_per_batch": size}]),
                        KEYS,
                    )

    def test_describes_itself_for_the_manifest(self):
        selection = select_batch_rows(
            PROJECT_DB, "Patients", batch([value_dim("Sex", "Female")]), KEYS
        )
        self.assertIn("Sex=Female", selection.description)


class CountTests(unittest.TestCase):
    def test_counts_before_chunking(self):
        selection = count_batch_rows(PROJECT_DB, "Patients", batch([value_dim("Sex", "Male")]))
        self.assertIn("COUNT_BIG(1)", selection.sql)
        self.assertNotIn("OFFSET", selection.sql)
        self.assertEqual(selection.params, ["Male"])
