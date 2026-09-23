"""Naming invariants: server, staging and destination must agree."""

from __future__ import annotations

import unittest

from ..naming import (
    NamingError,
    base_name,
    destination,
    global_temp,
    is_schema_qualified,
    is_temp_table,
    local_staging,
    qualify,
    qualify_join_clause,
    qualify_table_ref,
)


class GlobalTempTests(unittest.TestCase):
    def test_prefixes_a_plain_name(self):
        self.assertEqual(global_temp("PKTable"), "##JVM_PKTable")

    def test_never_doubles_the_jvm_prefix(self):
        # The old generator's ##JVM_JVM_Foo bug.
        for given in ("JVM_PKTable", "jvm_PKTable", "##JVM_PKTable"):
            with self.subTest(given=given):
                self.assertEqual(global_temp(given), "##JVM_PKTable")

    def test_is_idempotent(self):
        once = global_temp("PKTable")
        self.assertEqual(global_temp(once), once)


class LocalStagingTests(unittest.TestCase):
    def test_prefixes_a_plain_name(self):
        self.assertEqual(local_staging("PKTable"), "#Local_PKTable")

    def test_is_idempotent(self):
        self.assertEqual(local_staging("#Local_PKTable"), "#Local_PKTable")

    def test_derives_from_a_global_temp_name(self):
        self.assertEqual(local_staging("##JVM_PKTable"), "#Local_PKTable")


class DestinationTests(unittest.TestCase):
    def test_is_always_fully_qualified(self):
        self.assertEqual(
            destination("PROJECTD93A5E7", "PKTable"),
            "PROJECTD93A5E7.dbo.PKTable",
        )

    def test_strips_generator_prefixes(self):
        self.assertEqual(
            destination("PROJECTD93A5E7", "##JVM_PKTable"),
            "PROJECTD93A5E7.dbo.PKTable",
        )

    def test_requires_a_project_db(self):
        with self.assertRaises(NamingError):
            destination("", "PKTable")


class MissingNameTests(unittest.TestCase):
    def test_blank_dest_table_is_rejected(self):
        for given in (None, "", "   "):
            with self.subTest(given=given):
                for fn in (base_name, global_temp, local_staging):
                    with self.assertRaises(NamingError):
                        fn(given)


class PredicateTests(unittest.TestCase):
    def test_identifies_temp_tables(self):
        self.assertTrue(is_temp_table("#Local_X"))
        self.assertTrue(is_temp_table("##JVM_X"))
        self.assertFalse(is_temp_table("PatientDim"))

    def test_dots_inside_brackets_do_not_qualify(self):
        self.assertTrue(is_schema_qualified("dbo.PatientDim"))
        self.assertFalse(is_schema_qualified("[My.Table]"))
        self.assertTrue(is_schema_qualified("[dbo].[PatientDim]"))


class QualifyTests(unittest.TestCase):
    def test_adds_the_default_schema(self):
        self.assertEqual(qualify("PatientDim"), "dbo.PatientDim")
        self.assertEqual(qualify("[PatientDim]"), "dbo.[PatientDim]")

    def test_never_produces_dbo_dbo(self):
        for given in ("dbo.PatientDim", "[dbo].[PatientDim]", "COSMOS.dbo.PatientDim"):
            with self.subTest(given=given):
                self.assertEqual(qualify(given), given)

    def test_leaves_temp_tables_unqualified(self):
        # Global temps live in tempdb; qualifying them would break the reference.
        self.assertEqual(qualify("##JVM_PKTable2"), "##JVM_PKTable2")
        self.assertEqual(qualify("#Local_PKTable"), "#Local_PKTable")


class QualifyWithDatabaseTests(unittest.TestCase):
    """Three-part names, for a cohort reading a database it is not connected to."""

    def test_adds_the_database_when_given(self):
        self.assertEqual(
            qualify("PatientDim", "COSMOS_SneakPeek"),
            "COSMOS_SneakPeek.dbo.PatientDim",
        )

    def test_already_qualified_names_are_left_alone(self):
        self.assertEqual(
            qualify("dbo.PatientDim", "COSMOS_SneakPeek"), "dbo.PatientDim"
        )

    def test_temp_tables_are_never_database_qualified(self):
        # Global temps live in tempdb regardless of the connected database.
        self.assertEqual(qualify("##JVM_PKTable", "COSMOS_SneakPeek"), "##JVM_PKTable")

    def test_joins_take_the_database_too(self):
        self.assertEqual(
            qualify_join_clause("INNER JOIN EncounterFact AS e ON 1 = 1", "COSMOS_SneakPeek"),
            "INNER JOIN COSMOS_SneakPeek.dbo.EncounterFact AS e ON 1 = 1",
        )


class QualifyTableRefTests(unittest.TestCase):
    def test_qualifies_a_from_entry_keeping_the_alias(self):
        self.assertEqual(qualify_table_ref("PatientDim AS p"), "dbo.PatientDim AS p")

    def test_is_idempotent(self):
        once = qualify_table_ref("PatientDim AS p")
        self.assertEqual(qualify_table_ref(once), once)

    def test_leaves_generated_temps_alone(self):
        self.assertEqual(
            qualify_table_ref("##JVM_PKTable2 AS p"), "##JVM_PKTable2 AS p"
        )


class QualifyJoinClauseTests(unittest.TestCase):
    def test_qualifies_the_joined_table(self):
        self.assertEqual(
            qualify_join_clause(
                "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"
            ),
            "INNER JOIN dbo.DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
        )

    def test_handles_every_join_flavour(self):
        for kind in ("INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "CROSS JOIN", "join"):
            with self.subTest(kind=kind):
                self.assertIn(
                    "dbo.EncounterFact",
                    qualify_join_clause(f"{kind} EncounterFact AS e ON 1 = 1"),
                )

    def test_leaves_generated_temps_alone(self):
        clause = "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey"
        self.assertEqual(qualify_join_clause(clause), clause)

    def test_does_not_touch_the_on_predicate(self):
        # Column references are aliases, not tables, and must be left alone.
        clause = "INNER JOIN Foo AS f ON f.Bar = baz.Qux"
        self.assertEqual(
            qualify_join_clause(clause),
            "INNER JOIN dbo.Foo AS f ON f.Bar = baz.Qux",
        )

    def test_is_idempotent(self):
        once = qualify_join_clause("INNER JOIN Foo AS f ON 1 = 1")
        self.assertEqual(qualify_join_clause(once), once)
