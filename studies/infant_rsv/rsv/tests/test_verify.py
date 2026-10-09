"""`rsv verify` against a fake Cosmos: every check runs, and each judge reads its rows."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from rsv import verify as v
from rsv.config import load_settings


class FakeCursor:
    def __init__(self, answers: dict[str, list[tuple]], failing_setup: str = ""):
        self.answers, self.failing_setup, self.rows = answers, failing_setup, []
        self.params = None

    def execute(self, sql: str):
        if self.failing_setup and self.failing_setup in sql:
            raise RuntimeError("Invalid object name 'dbo.BirthFact'.")
        self.rows = []
        for check in v.CHECKS:
            if check.sql.format(**self.params).strip() in sql:
                if check.id not in self.answers:
                    raise RuntimeError(f"Invalid column name in {check.id}")
                self.rows = self.answers[check.id]

    def fetchall(self):
        return self.rows

    def nextset(self):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


ANSWERS = {
    "admission_key": [(">0", 2000), ("-1", 18000)],
    "admission_found": [(2000, 1990)],
    "departure": [(20000, 300, 2, 4.5)],
    "same_clock": [(20000, 20000)],
    "ed_vitals_link": [(20000, 15000, 15200)],
    "temperature_units": [(50000, 42000, 7900, 10, 90, 20.1, 106.2)],
    "spo2_scale": [(50000, 0, 49800, 0, 50, 100)],
    "patient_filters": [(18000, 17900, 17900, 17800, 1200, 1100)],
    "multiracial": [("0", 17000), ("1", 600), ("(null)", 200)],
    "svi_scale": [(15000, 0.0, 1.0)],
    "vbg_codes": [("2021-4", "PCO2, VENOUS", "Carbon dioxide [Partial pressure] in Venous blood", 900),
                  ("2747-2", "PH, VENOUS", "pH of Venous blood", 880)],
    "routes": [("Oral", 5000), ("Intravenous", 3000), ("IV Push", 400), ("Intraveneous", 4)],
    "iv_fluid_names": [("SODIUM CHLORIDE 0.9 % IV BOLUS", 800), ("CEFTRIAXONE IV", 300), ("(null)", 50)],
    "icu_specialties": [("Pediatric Intensive Care", 300), ("Neonatology", 500)],
    "stay_times": [(0, None, None)],
}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rsv_verify_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.settings = load_settings(root=self.root)
        self.cursor = FakeCursor(ANSWERS, failing_setup="#birth")
        self.cursor.params = v.window(self.settings)
        self.results, self.setup_errors = v.run(FakeConnection(self.cursor), self.settings, log=lambda _m: None)
        self.by_id = {r.check.id: r for r in self.results}

    def test_every_check_runs_and_a_failure_stops_none(self):
        self.assertEqual(len(self.results), len(v.CHECKS))
        self.assertEqual(self.by_id["race_values"].verdict, "ERR")
        self.assertEqual(self.by_id["temperature_units"].verdict, "OK")
        self.assertEqual(len(self.setup_errors), 1)
        self.assertIn("#birth", self.setup_errors[0])

    def test_judges_read_the_rows(self):
        self.assertEqual(self.by_id["admission_key"].verdict, "OK")
        self.assertEqual(self.by_id["patient_filters"].verdict, "NO")          # UseInCosmosAnalytics_X drops most
        self.assertIn("UseInCosmosAnalytics_X 6.7%", self.by_id["patient_filters"].evidence)
        self.assertEqual(self.by_id["vbg_codes"].verdict, "NO")
        self.assertIn("not seen: 2746-4", self.by_id["vbg_codes"].evidence)
        self.assertEqual(self.by_id["routes"].verdict, "NO")                   # "Intraveneous" missed
        self.assertIn("missed: Intraveneous", self.by_id["routes"].evidence)
        listed = v.detail_page(self.settings, self.results).render()
        self.assertIn("Intraveneous | <11", listed)                         # a small count is hidden
        self.assertEqual(self.by_id["icu_specialties"].verdict, "NO")
        self.assertIn("not seen: Critical Care Medicine", self.by_id["icu_specialties"].evidence)
        self.assertEqual(self.by_id["svi_scale"].verdict, "OK")
        self.assertEqual(self.by_id["multiracial"].verdict, "OK")

    def test_an_empty_sample_is_no_not_a_crash(self):
        self.assertEqual(self.by_id["stay_times"].verdict, "NO")

    def test_a_check_on_a_failed_temp_table_is_err(self):
        self.assertEqual(self.by_id["gestational_age"].verdict, "ERR")

    def test_pages_fit_and_name_every_check(self):
        summary = v.summary_page(self.settings, self.results, self.setup_errors)
        text = summary.render()
        for line in text.splitlines():
            self.assertLessEqual(len(line), self.settings["page_width"], line)
        for number in range(1, len(v.CHECKS) + 1):
            self.assertRegex(text, rf"\n{number:>2} (OK|NO|LOOK|ERR) ")
        self.assertIn("SETUP FAILED #birth", text)
        detail = v.detail_page(self.settings, self.results).render()
        for line in detail.splitlines():
            self.assertLessEqual(len(line), self.settings["page_width"], line)
        self.assertIn("2747-2", detail)

    def test_verify_writes_both_pages(self):
        summary, detail = v.verify(self.settings, FakeConnection(self.cursor))
        self.assertEqual(summary.name, "assumption-verify.txt")
        self.assertTrue(detail.is_file())

    def test_scope_runtime_found_from_scope_py(self):
        (self.root / "Scope" / "pullmanager").mkdir(parents=True)
        (self.root / "scope.py").write_text('ENTRY = Path(__file__).resolve().parent / \'Scope\' / "pullmanager.py"\n')
        self.assertEqual(v.scope_runtime(self.root), self.root / "Scope")


if __name__ == "__main__":
    unittest.main()
