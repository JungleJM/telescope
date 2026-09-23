"""SQL construction, with the WHERE builder as the main risk."""

from __future__ import annotations

import unittest

from ..sql import (
    ddl_body,
    from_entries,
    is_nullable,
    non_null_predicates,
    quote_literal,
    render_source_clause,
    render_where,
    where_entries,
)


class WhereBuilderTests(unittest.TestCase):
    def render(self, predicates):
        return [line.strip() for line in render_where(predicates).splitlines()]

    def test_first_predicate_takes_no_and(self):
        self.assertEqual(self.render(["a = 1"]), ["a = 1"])

    def test_subsequent_predicates_take_and(self):
        self.assertEqual(self.render(["a = 1", "b = 2"]), ["a = 1", "AND b = 2"])

    def test_preserves_a_grouped_code_list(self):
        # The K50/K51 filter, authored as separate list entries. Inserting AND
        # before the OR lines or the closing paren would be invalid SQL.
        self.assertEqual(
            self.render([
                "dt.Type IN ('ICD-10-CM')",
                "( dt.Value LIKE 'K50.%'",
                "  OR dt.Value = 'K50'",
                "  OR dt.Value LIKE 'K51.%'",
                "  OR dt.Value = 'K51'",
                ")",
            ]),
            [
                "dt.Type IN ('ICD-10-CM')",
                "AND ( dt.Value LIKE 'K50.%'",
                "OR dt.Value = 'K50'",
                "OR dt.Value LIKE 'K51.%'",
                "OR dt.Value = 'K51'",
                ")",
            ],
        )

    def test_does_not_double_an_explicit_and(self):
        self.assertEqual(self.render(["a = 1", "AND b = 2"]), ["a = 1", "AND b = 2"])

    def test_leading_or_is_left_alone(self):
        self.assertEqual(self.render(["a = 1", "OR b = 2"]), ["a = 1", "OR b = 2"])

    def test_comments_are_not_prefixed(self):
        self.assertEqual(
            self.render(["a = 1", "-- restrict to ICD-10", "b = 2"]),
            ["a = 1", "-- restrict to ICD-10", "AND b = 2"],
        )

    def test_a_group_opening_the_clause_takes_no_and(self):
        self.assertEqual(self.render(["( a = 1", "OR b = 2", ")"]), ["( a = 1", "OR b = 2", ")"])

    def test_blank_entries_are_dropped(self):
        self.assertEqual(self.render(["a = 1", "", "   ", "b = 2"]), ["a = 1", "AND b = 2"])

    def test_multiline_entries_are_split(self):
        self.assertEqual(
            self.render(["a = 1\nAND b = 2", "c = 3"]),
            ["a = 1", "AND b = 2", "AND c = 3"],
        )

    def test_case_insensitive_continuations(self):
        self.assertEqual(self.render(["a = 1", "and b = 2", "or c = 3"]),
                         ["a = 1", "and b = 2", "or c = 3"])


class NonNullTests(unittest.TestCase):
    def test_adds_a_predicate_for_each_non_nullable_column(self):
        columns = [
            {"source": "p.A", "name": "A", "nullable": False},
            {"source": "p.B", "name": "B", "nullable": True},
            {"source": "p.C", "name": "C"},  # absent means nullable
        ]
        self.assertEqual(non_null_predicates(columns), ["p.A IS NOT NULL"])

    def test_accepts_boolean_spellings(self):
        self.assertEqual(
            non_null_predicates([{"source": "p.A", "name": "A", "nullable": "no"}]),
            ["p.A IS NOT NULL"],
        )

    def test_absent_nullable_defaults_to_nullable(self):
        self.assertTrue(is_nullable({"name": "A"}))


class FilterShapeTests(unittest.TestCase):
    def test_from_accepts_a_string_or_a_list(self):
        self.assertEqual(from_entries({"from": "PatientDim AS p"}), ["PatientDim AS p"])
        self.assertEqual(from_entries({"from": ["PatientDim AS p"]}), ["PatientDim AS p"])
        self.assertEqual(from_entries({}), [])

    def test_where_accepts_a_string_or_a_list(self):
        self.assertEqual(where_entries({"where": "a = 1"}), ["a = 1"])
        self.assertEqual(where_entries({"where": ["a = 1", "b = 2"]}), ["a = 1", "b = 2"])

    def test_source_clause_qualifies_from_and_joins(self):
        sql = render_source_clause({
            "from": ["DiagnosisEventFact AS def"],
            "join": [
                "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
                "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
            ],
        })
        self.assertIn("FROM dbo.DiagnosisEventFact AS def", sql)
        self.assertIn("INNER JOIN dbo.DiagnosisTerminologyDim", sql)
        # A generated temp lives in tempdb and must stay unqualified.
        self.assertIn("INNER JOIN ##JVM_PKTable2 AS p", sql)


class LiteralTests(unittest.TestCase):
    def test_escapes_embedded_quotes(self):
        self.assertEqual(quote_literal("HUMIRA(CF) CROHN'S STARTER"), "'HUMIRA(CF) CROHN''S STARTER'")

    def test_renders_scalars(self):
        self.assertEqual(quote_literal(None), "NULL")
        self.assertEqual(quote_literal(42), "42")
        self.assertEqual(quote_literal(True), "1")


class DdlTests(unittest.TestCase):
    def test_renders_nullability(self):
        body = ddl_body([
            {"name": "A", "type": "BIGINT", "nullable": False},
            {"name": "B", "type": "VARCHAR(400)", "nullable": True},
        ])
        self.assertIn("[A] BIGINT NOT NULL", body)
        self.assertIn("[B] VARCHAR(400) NULL", body)
