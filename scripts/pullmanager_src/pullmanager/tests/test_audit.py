"""The dictionary audit (D155), against a fake Cosmos that knows some tables."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ..audit import audit_dictionary, report, write_report

COSMOS = {
    "dbo.MedicationDispenseFact": ["_IsDeleted", "FilledDateKey", "RefillsRemaining_X", "SupplyEndDateKey_X"],
    "dbo.PatientDim": ["DurableKey", "Sex"],
}
DICTIONARY = {
    "MedicationDispenseFact": {"columns": {
        "_IsDeleted": {}, "FilledDateKey": {}, "ReadyToDispenseDateKey": {},
        "RefillsRemaining": {}, "SupplyEndDateKey_X": {}}},
    "PatientDim": {"columns": {"durablekey": {}, "Sex": {}}},
    "NoSuchTable": {"columns": {"A": {}}},
}


class FakeCosmos:
    def __init__(self):
        self.asked = []

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.asked.append((sql, params))
        self.rows = [(name,) for name in COSMOS.get(params[0], [])]

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
