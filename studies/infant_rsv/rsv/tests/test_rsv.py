"""Tests on made-up parquets shaped like the Infant_RSV pull's."""

from __future__ import annotations

import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from rsv import analysis
from rsv.build import build, build_page
from rsv.cli import cmd_all, cmd_keys
from rsv.config import RsvError, load_settings
from rsv.page import Page

T = pd.Timestamp


def write(folder: Path, name: str, rows: list[dict] | pd.DataFrame, columns: list[str] | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows, columns=columns)
    frame.to_parquet(folder / f"{name}.parquet", index=False)


def visit(key, patient, encounter, arrival, departure, admission=None, financial="Medicaid"):
    return {"EdVisitKey": key, "EncounterKey": encounter, "PatientDurableKey": patient, "ArrivalInstant": T(arrival),
            "DepartureInstant": T(departure) if departure else pd.NaT,
            "HospitalAdmissionKey": admission if admission is not None else -1, "FinancialClass": financial,
            "DischargeDisposition": "Home", "EdGenericDispo": "Discharged", "AcuityLevel": "3",
            "ArrivalMethod": "Car"}


def vital(key, encounter, when, rr=None, spo2=None, temp=None, column="EncounterKey"):
    return {"VitalsKey": key, column: encounter, "PatientDurableKey": 0, "TakenInstant": T(when),
            "RespirationRate": rr, "SpO2": spo2, "Temperature": temp}


PATIENT_COLUMNS = ["DurableKey", "BirthDate", "Sex", "FirstRace", "SecondRace", "MultiRacial", "Ethnicity",
                   "SviOverallPctlRankByZip2020_X"]
LAB_COLUMNS = ["LabComponentResultKey", "EncounterKey", "PatientDurableKey", "LabComponentKey",
               "ComponentLoincCode", "CollectionInstant", "NumericValue", "Value", "Unit"]


def make_pull(root: Path, suffix: str = "", folder: str = "cosmos_parquets") -> None:
    """Two patients, three visits; patient 1's first visit admitted to the PICU."""
    pull = root / "runs" / "Infant_RSV" / folder
    write(pull, "EDVisits" + suffix, [
        visit(1, 100, 1001, "2023-11-01 10:00", "2023-11-01 14:00", admission=900),
        visit(2, 100, 1002, "2024-01-05 08:00", "2024-01-05 09:00"),
        visit(3, 200, 2001, "2020-12-10 12:00", None, financial="Commercial"),
    ])
    write(pull, "RSVPatients" + suffix, [], PATIENT_COLUMNS)          # empty, as the pull's was
    write(pull, "HospitalAdmissionFact" + suffix, [
        {"HospitalAdmissionKey": 900, "EncounterKey": 5001, "InpatientAdmissionInstant": T("2023-11-01 14:00"),
         "DischargeInstant": T("2023-11-04 10:00"), "LengthOfStayInDays": 3, "DepartmentKey": 77,
         "DischargeDepartmentKey_X": 78}])
    write(pull, "EDVitals" + suffix, [
        vital(1, 1001, "2023-11-01 09:00", rr=80),                       # before arrival: not initial
        vital(2, 1001, "2023-11-01 10:05", rr=50, spo2=20, temp=102.2),  # SpO2 20 implausible; 102.2 F
        vital(3, 1001, "2023-11-01 10:30", rr=70, spo2=91, temp=38.0),
        vital(4, 2001, "2020-12-10 12:30", rr=40, spo2=97, temp=37.0),
    ])
    write(pull, "InpatientVItals" + suffix, [
        vital(10, 5001, "2023-11-02 03:00", rr=90, spo2=85, column="InpatientEncounterKey")])
    write(pull, "EDLabTestComponents" + suffix, [
        {"LabComponentResultKey": 1, "EncounterKey": 1001, "PatientDurableKey": 100, "LabComponentKey": 7,
         "ComponentLoincCode": "2746-4", "CollectionInstant": T("2023-11-01 10:20"), "NumericValue": 7.31,
         "Value": "7.31", "Unit": ""},
        {"LabComponentResultKey": 2, "EncounterKey": 2001, "PatientDurableKey": 200, "LabComponentKey": 8,
         "ComponentLoincCode": "6690-2", "CollectionInstant": T("2020-12-10 12:20"), "NumericValue": 9.0,
         "Value": "9.0", "Unit": "10*3/uL"}], LAB_COLUMNS)
    write(pull, "InpatientLabTestComponents" + suffix, [], LAB_COLUMNS)
    write(pull, "IndexDiagnosis" + suffix, [
        {"EncounterKey": 1001, "BillingCodeValue": "J21.0", "PatientDurableKey": 100},
        {"EncounterKey": 2001, "BillingCodeValue": "B97.4", "PatientDurableKey": 200}])
    write(pull, "PatientBirthEvent" + suffix, [
        {"BabyPatientDurableKey": 200, "BirthKey": 1, "BirthInstant": T("2020-10-01 04:00"),
         "GestationalAgeDays": 245, "BirthWeightGrams": 2100}])


def make_followup(root: Path) -> None:
    follow = root / "runs" / "Infant_RSV_Followup" / "cosmos_parquets"
    write(follow, "Patients", [
        {"DurableKey": 100, "BirthDate": T("2023-08-01"), "Sex": "Female", "FirstRace": "White", "SecondRace": None,
         "MultiRacial": 0, "Ethnicity": "Not Hispanic or Latino", "SviOverallPctlRankByZip2020_X": 0.8},
        {"DurableKey": 200, "BirthDate": None, "Sex": "Male", "FirstRace": "Black or African American",
         "SecondRace": "White", "MultiRacial": 0, "Ethnicity": "Patient Declined",
         "SviOverallPctlRankByZip2020_X": 0.1}])
    write(follow, "AdmissionDepartments", [
        {"HospitalAdmissionKey": 900, "EncounterKey": 5001, "DischargeInstant": T("2023-11-04 10:00"),
         "LengthOfStayInDays": 3, "AdmitSpecialty": "Pediatrics",
         "DischargeSpecialty": "Pediatric Critical Care Medicine"}])


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rsv_test_"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def settings(self, sp: bool = False, **changes):
        settings = load_settings(root=self.root, sp=sp)
        settings.values.update(changes)
        return settings


class BuildOutcomes(Fixture):
    def setUp(self) -> None:
        super().setUp()
        make_pull(self.root)
        make_followup(self.root)
        self.built = build(self.settings())
        self.v = self.built.visits.set_index("EdVisitKey")

    def test_initial_is_first_reading_after_arrival(self):
        self.assertEqual(self.v.loc[1, "rr_initial"], 50)
        self.assertEqual(self.v.loc[1, "rr_max_ed"], 70)

    def test_no_departure_time_keeps_the_ed_window_open_for_hours(self):
        self.assertEqual(self.v.loc[3, "rr_initial"], 40)
        self.assertEqual(self.built.notes["no_departure"], 1)

    def test_fahrenheit_converted_and_implausible_dropped(self):
        self.assertAlmostEqual(self.v.loc[1, "temp_initial"], 39.0, places=6)
        self.assertEqual(self.v.loc[1, "spo2_min_ed"], 91)
        self.assertEqual(self.built.notes["temps_converted"], 1)
        self.assertEqual(self.built.notes["vitals_dropped"]["spo2"], 1)

    def test_stay_vitals_reach_the_visit_through_its_admission(self):
        self.assertEqual(self.v.loc[1, "rr_max_stay"], 90)
        self.assertEqual(self.v.loc[1, "spo2_min_stay"], 85)
        self.assertEqual(self.v.loc[1, "rr_max_ed"], 70)

    def test_followup_table_read_in_place_of_empty_one(self):
        self.assertEqual(self.built.sources["patients"].path.parts[-3:],
                         ("Infant_RSV_Followup", "cosmos_parquets", "Patients.parquet"))
        self.assertEqual(self.built.sources["patients"].tried, ["followup:Patients: 2 rows"])
        self.assertEqual(self.v.loc[1, "race"], "White")
        self.assertEqual(self.v.loc[1, "age_days"], 92)
        self.assertEqual(self.v.loc[1, "age_band"], "3-5m")

    def test_multiple_race_unknown_ethnicity_svi_quartile(self):
        self.assertEqual(self.v.loc[3, "race"], "Multiple")
        self.assertEqual(self.v.loc[3, "ethnicity"], "Unknown")
        self.assertEqual(self.v.loc[3, "svi_quartile"], "Q1")
        self.assertEqual(self.v.loc[1, "svi_quartile"], "Q4")

    def test_birth_row_gives_birth_date_and_gestational_age(self):
        self.assertEqual(self.v.loc[3, "birth_date_source"], "birth")
        self.assertEqual(self.v.loc[3, "ga_weeks"], 35)
        self.assertEqual(self.v.loc[3, "ga_band"], "32-36w")
        self.assertEqual(self.v.loc[3, "age_days"], 70)

    def test_vbg_by_loinc_in_the_ed(self):
        self.assertTrue(self.v.loc[1, "vbg_ed"])
        self.assertFalse(self.v.loc[3, "vbg_ed"])

    def test_icu_from_department_specialty(self):
        self.assertTrue(self.v.loc[1, "icu"])
        self.assertFalse(self.v.loc[2, "icu"])
        self.assertTrue(self.v.loc[1, "admitted"])
        self.assertEqual(self.v.loc[1, "los_days"], 3)

    def test_first_visit_season_era_dx(self):
        self.assertTrue(self.v.loc[1, "first_visit"])
        self.assertFalse(self.v.loc[2, "first_visit"])
        self.assertEqual(self.v.loc[1, "season"], "2023-24")
        self.assertEqual(self.v.loc[1, "era"], "nirsevimab")
        self.assertEqual(self.v.loc[3, "era"], "COVID")
        self.assertEqual(self.v.loc[1, "dx_group"], "bronchiolitis")
        self.assertEqual(self.v.loc[3, "dx_group"], "RSV other")

    def test_no_medications_leaves_iv_fluids_empty(self):
        self.assertTrue(self.built.visits["iv_fluids_ed"].isna().all())
        self.assertEqual(self.built.notes["iv_by"], "no medications pulled")

    def test_parquets_written(self):
        folder = self.settings().analysis_folder
        for name in ("visits", "vitals", "labs", "meds"):
            self.assertTrue((folder / f"{name}.parquet").is_file(), name)
        self.assertEqual(len(pd.read_parquet(folder / "visits.parquet")), 3)


class Pages(Fixture):
    def setUp(self) -> None:
        super().setUp()
        make_pull(self.root)
        make_followup(self.root)

    def test_small_counts_hidden_and_not_in_check_total(self):
        page = Page(self.settings(), "x", "x")
        self.assertEqual(page.count(5), "<11")
        self.assertEqual(page.count(20), "20")
        self.assertEqual(page.count(0), "0")
        self.assertEqual(page.check, 20)
        self.assertEqual(page.pct(5, 100), "--")
        self.assertEqual(page.pct(50, 100), "50.0")

    def test_every_page_fits_its_width(self):
        settings = self.settings(min_cell=0)
        cmd_all(settings)
        width = settings["page_width"]
        pages = list(settings.pages_folder.glob("*.txt"))
        self.assertGreater(len(pages), 5)
        for path in pages:
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertLessEqual(len(line), width, f"{path.name}: {line!r}")

    def test_check_total_is_the_sum_of_counts_printed(self):
        settings = self.settings(min_cell=0)
        built = build(settings)
        page = build_page(settings, built, "test")
        text = page.render()
        self.assertIn(f"check total {page.check:,}", text)
        self.assertGreaterEqual(page.check, 3)

    def test_unknown_section_names_the_sections(self):
        settings = self.settings()
        build(settings)
        with self.assertRaises(RsvError) as caught:
            analysis.report(settings, "nope")
        self.assertIn("first_visits", str(caught.exception))

    def test_section_filters_visits(self):
        settings = self.settings()
        visits = build(settings).visits
        chosen, _, expression = analysis.section(visits, settings, "first_visits")
        self.assertEqual(sorted(chosen["EdVisitKey"]), [1, 3])
        self.assertEqual(expression, "first_visit")


class SneakPeekAndKeys(Fixture):
    def test_sp_reads_sneakpeek_alone_and_says_so(self):
        make_pull(self.root, suffix="_sp", folder="sneakpeek_parquets")
        settings = self.settings(sp=True)
        built = build(settings)
        self.assertEqual(built.sources["visits"].path.parts[-2:], ("sneakpeek_parquets", "EDVisits_sp.parquet"))
        self.assertEqual(settings.analysis_folder.name, "analysis_sneakpeek")
        page = analysis.report(settings)
        self.assertIn("SNEAKPEEK ONLY", page.render())

    def test_without_cosmos_files_the_build_says_where_to_look(self):
        make_pull(self.root, suffix="_sp", folder="sneakpeek_parquets")
        with self.assertRaises(RsvError) as caught:
            build(self.settings())
        self.assertIn("pull_folder", str(caught.exception))

    def test_without_followup_the_empty_table_is_skipped_and_named(self):
        make_pull(self.root)
        settings = self.settings(min_cell=0)
        built = build(settings)
        self.assertFalse(built.sources["patients"].found)
        self.assertIn("pull:RSVPatients: 0 rows", built.sources["patients"].tried)
        self.assertTrue(built.visits["race"].isna().all())
        self.assertTrue(built.visits["icu"].isna().all())
        self.assertEqual(built.visits.set_index("EdVisitKey").loc[3, "ga_weeks"], 35)
        text = build_page(settings, built, "test").render()
        self.assertIn("  skipped pull:RSVPatients\n    (0 rows)", text)
        page = analysis.admission(settings, None, analysis.load(settings))
        self.assertIn("not available", page.render())

    def test_keys_file_holds_each_visit_once(self):
        make_pull(self.root)
        out = cmd_keys(self.settings())
        keys = pd.read_parquet(out)
        self.assertEqual(list(keys.columns), ["EdVisitKey", "EncounterKey", "PatientDurableKey", "HospitalAdmissionKey",
                                              "ArrivalInstant", "DepartureInstant"])
        self.assertEqual(keys["EdVisitKey"].tolist(), [1, 2, 3])
        self.assertEqual(str(keys["EdVisitKey"].dtype), "int64")
        self.assertFalse(keys["DepartureInstant"].isna().any())


class LaterFixes(Fixture):
    def test_without_ed_labs_vbg_is_not_known(self):
        make_pull(self.root)
        pull = self.root / "runs" / "Infant_RSV" / "cosmos_parquets"
        write(pull, "EDLabTestComponents", [], LAB_COLUMNS)
        write(pull, "InpatientLabTestComponents", [
            {"LabComponentResultKey": 9, "EncounterKey": 5001, "PatientDurableKey": 100, "LabComponentKey": 7,
             "ComponentLoincCode": "2746-4", "CollectionInstant": T("2023-11-01 10:20"), "NumericValue": 7.3,
             "Value": "7.3", "Unit": ""}], LAB_COLUMNS)
        built = build(self.settings())
        self.assertTrue(built.visits["vbg_ed"].isna().all())
        self.assertEqual(built.notes["vbg_by"], "no ED labs")

    def test_redo_names_read_before_the_first_pulls(self):
        make_pull(self.root)
        pull = self.root / "runs" / "Infant_RSV" / "cosmos_parquets"
        write(pull, "Patients", [{"DurableKey": 100, "BirthDate": T("2023-08-01"), "Sex": "Female",
                                  "FirstRace": "Asian", "SecondRace": None, "MultiRacial": 0,
                                  "Ethnicity": "Hispanic or Latino", "SviOverallPctlRankByZip2020_X": 0.3}])
        built = build(self.settings())
        self.assertEqual(built.sources["patients"].path.name, "Patients.parquet")
        self.assertEqual(built.visits.set_index("EdVisitKey").loc[1, "race"], "Asian")

    def test_star_placeholder_is_unknown(self):
        make_pull(self.root)
        pull = self.root / "runs" / "Infant_RSV" / "cosmos_parquets"
        rows = pd.read_parquet(pull / "EDVisits.parquet")
        rows.loc[rows["EdVisitKey"] == 2, "FinancialClass"] = "*Not Applicable"
        write(pull, "EDVisits", rows)
        v = build(self.settings()).visits.set_index("EdVisitKey")
        self.assertEqual(v.loc[2, "financial_class"], "Unknown")
        self.assertEqual(v.loc[1, "financial_class"], "Medicaid")

    def test_iv_fluids_by_route_and_name_in_the_ed(self):
        make_pull(self.root)
        follow = self.root / "runs" / "Infant_RSV_Followup" / "cosmos_parquets"
        base = {"PatientDurableKey": 100, "AdministrationAction": "Given", "ActionIsMedAdministration": 1,
                "MedicationKey": 1, "Dose": 20, "DoseUnit": "mL/kg", "Rate": None}
        write(follow, "EDMeds", [
            dict(base, MedicationAdministrationKey=1, EncounterKey=1001, AdministrationInstant=T("2023-11-01 11:00"),
                 AdministrationRoute="Intravenous", MedicationName="SODIUM CHLORIDE 0.9 % IV BOLUS",
                 MedicationGenericName="sodium chloride 0.9 %", MedicationSimpleGenericName="Sodium Chloride"),
            dict(base, MedicationAdministrationKey=2, EncounterKey=2001, AdministrationInstant=T("2020-12-10 12:30"),
                 AdministrationRoute="Oral", MedicationName="ACETAMINOPHEN 160 MG/5 ML",
                 MedicationGenericName="acetaminophen", MedicationSimpleGenericName="Acetaminophen"),
            dict(base, MedicationAdministrationKey=3, EncounterKey=2001, AdministrationInstant=T("2020-12-10 12:40"),
                 AdministrationRoute="Intravenous", MedicationName="AMPICILLIN IV",
                 MedicationGenericName="ampicillin", MedicationSimpleGenericName="Ampicillin")])
        settings = self.settings(min_cell=0)
        built = build(settings)
        v = built.visits.set_index("EdVisitKey")
        self.assertTrue(v.loc[1, "iv_fluids_ed"])
        self.assertFalse(v.loc[3, "iv_fluids_ed"])
        self.assertEqual(built.notes["iv_by"], "route and name")
        text = build_page(settings, built, "test").render()
        self.assertIn("IV medications in the ED:", text)
        self.assertIn("AMPICILLIN IV", text)


class AdmissionModel(Fixture):
    def test_model_finds_a_real_difference(self):
        rng = np.random.default_rng(7)
        n = 1500
        race = rng.choice(["White", "Black", "Asian"], size=n, p=[0.5, 0.3, 0.2])
        admitted = rng.random(n) < np.where(race == "Black", 0.45, 0.15)
        visits = pd.DataFrame({
            "EdVisitKey": np.arange(n), "PatientDurableKey": rng.integers(0, 1200, n), "race": race,
            "ethnicity": rng.choice(["Hispanic", "Not Hispanic"], n), "svi_quartile": rng.choice(["Q1", "Q2", "Q3", "Q4"], n),
            "financial_class": rng.choice(["Medicaid", "Commercial"], n), "age_band": rng.choice(["0-28d", "1-2m"], n),
            "season": rng.choice(["2022-23", "2023-24"], n), "admitted": admitted,
            "icu": pd.Series([pd.NA] * n, dtype="boolean")})
        settings = self.settings(min_cell=0)
        odds, problem, used = analysis.fit_model(visits, "admitted", ["race"], ["age_band", "season"], 50)
        self.assertEqual(problem, "")
        self.assertEqual(used, n)
        black = [row for row in odds["race"] if row[0] == "Black"][0]
        self.assertGreater(black[2], 1.5)          # lower bound of the 95% CI
        page = analysis.admission(settings, None, visits)
        text = page.render()
        self.assertIn("MODEL: ADMITTED", text)
        self.assertIn("ICU", text)
        self.assertIn("not available", text)

    def test_constant_outcome_says_it_could_not_fit(self):
        visits = pd.DataFrame({"PatientDurableKey": range(100), "race": ["A", "B"] * 50, "admitted": [True] * 100})
        odds, problem, _ = analysis.fit_model(visits, "admitted", ["race"], [], 10)
        self.assertEqual(odds, {})
        self.assertTrue(problem)


if __name__ == "__main__":
    unittest.main()
