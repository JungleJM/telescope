"""Compatibility rules for hand-authored cohort YAML."""

from __future__ import annotations

import unittest

from ..normalize import (
    NormalizationError,
    dead_options,
    joined_generated_tables,
    normalize_bool,
    normalize_dedup_keys,
    root_pk_cohort,
    root_pk_cohorts,
    validate_dedup_columns,
)

# The chained PK from inputSimple.yaml: a patient list, then diagnosis events
# for those patients.
PATIENTS = {
    "name": "PKTable",
    "type": "PK",
    "dest_table": "PKTable2",
    "columns": [{"name": "PatientDurableKey"}],
    "filter": {"from": "PatientDim AS p", "join": []},
}
EVENTS = {
    "name": "PKTable",
    "type": "PK",
    "dest_table": "PKTable",
    "dedup_key": ["DiagnosisEventKey"],
    "columns": [{"name": "DiagnosisEventKey"}, {"name": "PatientDurableKey"}],
    "filter": {
        "from": "DiagnosisEventFact AS def",
        "join": [
            "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
            "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
        ],
    },
}
FACT = {"name": "OtherDx", "type": "fact", "dest_table": "OtherDx", "filter": {}}


class BooleanTests(unittest.TestCase):
    def test_accepts_truthy_spellings(self):
        for given in (True, 1, "true", "True", "YES", "y", "1", "on", "t"):
            with self.subTest(given=given):
                self.assertTrue(normalize_bool(given))

    def test_accepts_falsy_spellings(self):
        for given in (False, 0, "false", "No", "n", "0", "off", ""):
            with self.subTest(given=given):
                self.assertFalse(normalize_bool(given))

    def test_none_takes_the_default(self):
        self.assertFalse(normalize_bool(None))
        self.assertTrue(normalize_bool(None, default=True))

    def test_rejects_nonsense(self):
        with self.assertRaises(NormalizationError):
            normalize_bool("maybe")


class DedupKeyTests(unittest.TestCase):
    def test_legacy_singular_is_accepted_with_a_note(self):
        # The old generator accepted only `dedup_keys` and silently skipped
        # deduplication entirely, changing row counts with no warning.
        keys, notes = normalize_dedup_keys(EVENTS)
        self.assertEqual(keys, [["DiagnosisEventKey"]])
        self.assertEqual(len(notes), 1)
        self.assertIn("dedup_key", notes[0])

    def test_canonical_form_produces_no_note(self):
        keys, notes = normalize_dedup_keys({"dedup_keys": [["A", "B"], ["C"]]})
        self.assertEqual(keys, [["A", "B"], ["C"]])
        self.assertEqual(notes, [])

    def test_flat_list_is_one_key_set(self):
        keys, _ = normalize_dedup_keys({"dedup_keys": ["A", "B"]})
        self.assertEqual(keys, [["A", "B"]])

    def test_bare_string_is_one_key_set(self):
        keys, _ = normalize_dedup_keys({"dedup_keys": "A"})
        self.assertEqual(keys, [["A"]])

    def test_absent_means_no_dedup(self):
        keys, notes = normalize_dedup_keys({"dest_table": "X"})
        self.assertEqual(keys, [])
        self.assertEqual(notes, [])

    def test_both_spellings_at_once_is_an_error(self):
        with self.assertRaises(NormalizationError):
            normalize_dedup_keys({"dedup_key": ["A"], "dedup_keys": [["A"]]})

    def test_empty_is_an_error(self):
        with self.assertRaises(NormalizationError):
            normalize_dedup_keys({"dedup_keys": []})
        with self.assertRaises(NormalizationError):
            normalize_dedup_keys({"dedup_keys": [[]]})

    def test_mixed_shapes_are_an_error(self):
        with self.assertRaises(NormalizationError):
            normalize_dedup_keys({"dedup_keys": ["A", ["B"]]})


class DedupColumnTests(unittest.TestCase):
    def test_keys_must_name_produced_columns(self):
        self.assertEqual(validate_dedup_columns([["DiagnosisEventKey"]], EVENTS), [])

    def test_unknown_column_is_reported(self):
        problems = validate_dedup_columns([["Nope"]], EVENTS)
        self.assertEqual(len(problems), 1)
        self.assertIn("Nope", problems[0])


class DeadOptionTests(unittest.TestCase):
    def test_retired_options_are_named(self):
        notes = dead_options({"printout_md": True, "stop_at_for_pk_table": 500})
        self.assertEqual(len(notes), 1)
        self.assertIn("printout_md", notes[0])

    def test_live_options_are_silent(self):
        self.assertEqual(dead_options({"stop_at_for_pk_table": 500}), [])
        self.assertEqual(dead_options(None), [])


class DependencyTests(unittest.TestCase):
    def test_finds_joined_global_temps(self):
        self.assertEqual(joined_generated_tables(EVENTS), {"##JVM_PKTABLE2"})

    def test_reports_none_for_an_independent_cohort(self):
        self.assertEqual(joined_generated_tables(PATIENTS), set())


class RootPkTests(unittest.TestCase):
    def test_chained_pks_have_one_root(self):
        # Row limits apply here only; limiting the downstream PK too would
        # compound 500 patients x 500 events into an unrepresentative sample.
        root = root_pk_cohort([PATIENTS, EVENTS, FACT])
        self.assertEqual(root["dest_table"], "PKTable2")

    def test_order_does_not_matter(self):
        root = root_pk_cohort([EVENTS, FACT, PATIENTS])
        self.assertEqual(root["dest_table"], "PKTable2")

    def test_single_pk_is_its_own_root(self):
        self.assertEqual(root_pk_cohort([PATIENTS, FACT])["dest_table"], "PKTable2")

    def test_no_pk_cohorts_gives_none(self):
        self.assertIsNone(root_pk_cohort([FACT]))

    def test_fact_cohorts_are_never_roots(self):
        self.assertEqual(len(root_pk_cohorts([PATIENTS, EVENTS, FACT])), 1)

    def test_ambiguous_roots_are_an_error(self):
        other = dict(PATIENTS, dest_table="OtherPK", name="OtherPK")
        with self.assertRaises(NormalizationError):
            root_pk_cohort([PATIENTS, other])
