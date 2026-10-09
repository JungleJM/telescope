"""`rsv icu` against a fake Cosmos: the script is set from the settings, and its results fit one page."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from rsv import icu
from rsv.config import RsvError, load_settings


class FakeCursor:
    def __init__(self, results):
        self.results, self.index, self.script = results, 0, ""

    def execute(self, script):
        self.script, self.index = script, 0

    @property
    def description(self):
        columns = self.results[self.index][0]
        return [(c,) for c in columns] if columns else None

    def fetchall(self):
        return self.results[self.index][1]

    def nextset(self):
        self.index += 1
        return self.index < len(self.results)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


RESULTS = [
    ([], []),                                                       # a statement with no result set
    (["TABLE_NAME", "COLUMN_NAME", "DATA_TYPE"], [("HospitalAdmissionFact", "IcuDays_X", "int")]),
    (["COLUMN_NAME", "DATA_TYPE"], [("DepartmentKey", "bigint"), ("DepartmentSpecialty", "nvarchar")]),
    (["DepartmentSpecialty", "Departments"], [("Pediatric Critical Care Medicine", 812), ("Neonatology", 4)]),
    (["InfantAdmissions", "NoInpatientAdmissionInstant", "NoDischargeInstant", "NoAdmitDepartment", "NoDischargeDepartment"],
     [(5321, 12, 0, 40, 41)]),
    (["AdmittedToSpecialty", "Admissions"], [(f"Specialty {i}", 500 - i) for i in range(20)]),
    (["DischargedFromSpecialty", "Admissions"], [("Pediatrics", 4000)]),
    (["Administrations", "NoDepartment", "AdmissionsWithMedications", "InfantAdmissions"], [(90000, 120, 5100, 5321)]),
    (["GivenInSpecialty", "Admissions"], [("Pediatrics", 4800), ("Pediatric Critical Care Medicine", 700)]),
    (["Admissions", "IcuAtAdmission", "IcuAtDischarge", "IcuByMedications", "OnlyByMedications", "IcuAny"],
     [(5321, 300, 200, 700, 350, 760)]),
    (["Stay", "Admissions", "MedianLengthOfStay"], [("ICU", 760, Decimal("6.5")), ("not ICU", 4561, Decimal("2"))]),
]


class IcuTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rsv_icu_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.settings = load_settings(root=self.root)
        self.settings.values["verify"] = {"from": 20250101, "to": 20250131, "visits": 100}
        self.settings.values["icu_specialties"] = ["Pediatric Critical Care Medicine", "O'Brien Unit"]

    def test_script_takes_the_month_and_specialties_from_settings(self):
        script = icu.script_for(self.settings)
        self.assertIn("DECLARE @from BIGINT = 20250101;", script)
        self.assertIn("DECLARE @to BIGINT = 20250131;", script)
        self.assertIn("DECLARE @meds_to BIGINT = 20250501;", script)
        self.assertIn("(N'Pediatric Critical Care Medicine'),\n    (N'O''Brien Unit');", script)
        self.assertNotIn("N'Critical Care Medicine'", script)
        self.assertEqual(len(icu.titles(script)), 10)

    def test_page_fits_and_numbers_every_result(self):
        cursor = FakeCursor(RESULTS)
        path = icu.check_icu(self.settings, FakeConnection(cursor))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(path.name, "icu-check.txt")
        for line in text.splitlines():
            self.assertLessEqual(len(line), self.settings["page_width"], line)
        for number in range(1, 11):
            self.assertRegex(text, rf"\n{number}\. ")
        self.assertIn("IcuDays_X", text)
        self.assertIn("Neonatology <11", text)              # small counts hidden
        self.assertIn("+10 more", text)                     # 20 names, 10 shown
        self.assertIn("- ICU | 760 | 6.5", text)
        self.assertIn("- not ICU | 4,561 | 2.0", text)     # a median is never hidden as a count
        self.assertIn("InfantAdmissions=5,321", text)       # one row of totals, as pairs
        self.assertIn("SET NOCOUNT ON", cursor.script)

    def test_a_script_without_its_settings_lines_is_refused(self):
        broken = self.root / "broken.sql"
        broken.write_text("SELECT 1;")
        with self.assertRaises(RsvError) as caught:
            icu.script_for(self.settings, broken)
        self.assertIn("DECLARE @from", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
