"""The dictionary audit (D155, D161), against a fake Cosmos that knows some tables."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ..audit import audit_dictionary, report, write_report

# sys.columns rows: name, TYPE_NAME, max_length (bytes), precision, scale, is_nullable
COSMOS = {
    "dbo.MedicationDispenseFact": [
        ("_IsDeleted", "tinyint", 1, 3, 0, 0), ("FilledDateKey", "bigint", 8, 19, 0, 1),
        ("RefillsRemaining_X", "smallint", 2, 5, 0, 1), ("SupplyEndDateKey_X", "bigint", 8, 19, 0, 1)],
    "dbo.PatientDim": [("DurableKey", "bigint", 8, 19, 0, 0), ("Sex", "nvarchar", 600, 0, 0, 1)],
}
KEY = {"type": "bigint (DateKey; foreign key to DateDim)", "nullable": True}
DICTIONARY = {
    "MedicationDispenseFact": {"columns": {
        "_IsDeleted": {"type": "tinyint (flag)", "nullable": False}, "FilledDateKey": KEY,
        "ReadyToDispenseDateKey": KEY, "RefillsRemaining": {"type": "smallint"},
        "SupplyEndDateKey_X": KEY}},
    "PatientDim": {"columns": {"durablekey": {"type": "bigint", "nullable": False},
                               "Sex": {"type": "nvarchar(300)", "nullable": True}}},
    "NoSuchTable": {"columns": {"A": {}}},
}
# Another Cosmos table, and a dictionary wrong about its types and nullability.
VITALS = {"dbo.VitalsFact": [
    ("FillNumber", "nvarchar", 100, 0, 0, 1), ("MinimumDose_X", "numeric", 9, 19, 4, 1),
    ("Height", "numeric", 9, 18, 2, 1), ("Label", "nvarchar", -1, 0, 0, 1),
    ("TakenInstant", "datetime2", 8, 27, 7, 1), ("DateKey", "bigint", 8, 19, 0, 0),
    ("Count", "tinyint", 1, 3, 0, 0), ("Flag", "tinyint", 1, 3, 0, 1)]}
VITALS_DICTIONARY = {"VitalsFact": {"columns": {
    "FillNumber": {"type": "int", "nullable": True},                  # wrong type
    "MinimumDose_X": {"type": "numeric", "nullable": True},           # no size: wrong
    "Height": {"type": "numeric(18, 2)", "nullable": True},           # right
    "Label": {"type": "nvarchar(max)", "nullable": True},             # right
    "TakenInstant": {"type": "datetime2", "nullable": True},          # right: scale 7
    "DateKey": {"type": "bigint (DateKey; partition key)", "nullable": True},  # not nullable
    "Count": {"type": "tinyint"},                                     # none is nullable
    "Flag": {"type": "tinyint (flag)", "nullable": False},            # nullable in Cosmos
}}}


class FakeCosmos:
    def __init__(self):
        self.asked = []

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.asked.append((sql, params))
        self.rows = list({**COSMOS, **VITALS}.get(params[0], []))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class AuditTests(unittest.TestCase):
    def test_it_finds_what_the_dictionary_lists_and_cosmos_lacks(self):
        result = audit_dictionary(FakeCosmos(), DICTIONARY, "COSMOS")
        self.assertEqual(result.checked, 3)
        self.assertEqual(result.tables_not_found, ["NoSuchTable"])
        gone = dict(result.missing["MedicationDispenseFact"])
        self.assertEqual(sorted(gone), ["ReadyToDispenseDateKey", "RefillsRemaining"])
        self.assertEqual(gone["RefillsRemaining"], ["RefillsRemaining_X"])
        self.assertNotIn("PatientDim", result.missing, "names compare without case")

    def test_the_report_lists_only_what_did_not_check_out(self):
        text = report(audit_dictionary(FakeCosmos(), DICTIONARY, "COSMOS"), "abcd1234")
        self.assertTrue(text.startswith("# dictionary audit, "))
        self.assertIn("bundle abcd1234, database COSMOS: 3 tables, 3 wrong", text)
        self.assertIn("  - NoSuchTable", text)
        self.assertIn("  MedicationDispenseFact: [ReadyToDispenseDateKey, RefillsRemaining]"
                      "  # near: RefillsRemaining -> RefillsRemaining_X", text)
        self.assertNotIn("PatientDim", text)
        import yaml

        data = yaml.safe_load(text)
        self.assertEqual(data["columns_not_in_cosmos"]["MedicationDispenseFact"],
                         ["ReadyToDispenseDateKey", "RefillsRemaining"])

    def test_it_finds_only_the_types_that_differ_from_cosmos(self):
        result = audit_dictionary(FakeCosmos(), VITALS_DICTIONARY, "COSMOS")
        self.assertEqual(result.types, {"VitalsFact": {
            "FillNumber": "nvarchar(50)", "MinimumDose_X": "numeric(19,4)"}})

    def test_it_finds_only_the_nullability_that_differs_from_cosmos(self):
        result = audit_dictionary(FakeCosmos(), VITALS_DICTIONARY, "COSMOS")
        self.assertEqual(result.nullable, {"VitalsFact": {
            "DateKey": False, "Count": False, "Flag": True}})

    def test_the_report_lists_types_and_nullability_in_one_line_a_table(self):
        text = report(audit_dictionary(FakeCosmos(), VITALS_DICTIONARY, "COSMOS"))
        self.assertIn("1 tables, 5 wrong", text)
        self.assertIn('  VitalsFact: {FillNumber: nvarchar(50), MinimumDose_X: "numeric(19,4)"}', text)
        self.assertIn("  VitalsFact: {DateKey: false, Count: false, Flag: true}", text)
        import yaml

        data = yaml.safe_load(text)
        self.assertEqual(data["types_wrong"]["VitalsFact"]["MinimumDose_X"], "numeric(19,4)")
        self.assertIs(data["nullable_wrong"]["VitalsFact"]["Flag"], True)

    def test_the_query_asks_for_types_and_nullability(self):
        cosmos = FakeCosmos()
        audit_dictionary(cosmos, VITALS_DICTIONARY, "COSMOS")
        sql, params = cosmos.asked[0]
        self.assertIn("TYPE_NAME(c.user_type_id)", sql)
        self.assertIn("c.is_nullable", sql)
        self.assertEqual(params, ["dbo.VitalsFact"])

    def test_a_dictionary_that_matches_is_one_line(self):
        text = report(audit_dictionary(FakeCosmos(), {"PatientDim": DICTIONARY["PatientDim"]}, "COSMOS"))
        self.assertEqual(text.splitlines()[1:], ["the dictionary matches Cosmos"])

    def test_the_command_writes_runs_dictionary_audit_yaml(self):
        from .. import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            args = argparse.Namespace(env=None)
            out = io.StringIO()
            with mock.patch("pullmanager.contents.load_dictionary", return_value=DICTIONARY), \
                    contextlib.redirect_stdout(out):
                code = cli.audit_dictionary(args, connect_fn=lambda *a, **k: FakeCosmos(), cwd=home)
            self.assertEqual(code, 1)
            written = home / "runs" / "dictionary_audit.yaml"
            self.assertIn("ReadyToDispenseDateKey", written.read_text(encoding="utf-8"))
            self.assertIn("Written to", out.getvalue())

    def test_write_report_replaces_the_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = audit_dictionary(FakeCosmos(), DICTIONARY, "COSMOS")
            path, _ = write_report(result, Path(tmp))
            path.write_text("old", encoding="utf-8")
            write_report(result, Path(tmp))
            self.assertIn("NoSuchTable", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
