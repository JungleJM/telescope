#!/usr/bin/env python3
"""Build a synthetic copy of the Infant RSV pull, as a repository colleagues can explore (D215).

    python3 studies/synthetic/make_synthetic_repo.py               # dist/synthetic_rsv_repo/
    python3 studies/synthetic/make_synthetic_repo.py --patients 5000 --out DIR
    python3 studies/synthetic/make_synthetic_repo.py --tdd

Nothing here reads real data. Each table's columns and SQL types come from the
Infant_RSV blueprint (the redo, D214), and each parquet is typed as Artifacts
types the real ones, so code written against these runs on the real files.
Every value is invented by a seeded random generator; every key begins 7007.

What it writes:

    README.md, STATS.md, Contents.md, DataDictionary.yaml
    data/cosmos_parquets/<Table>.parquet
    python/load_parquets.py, python/examples.py, python/stats.py
    R/load_parquets.R, R/examples.R, R/stats.R

STATS.md's answers are what python/stats.py prints on the data written; the
tests check that R/stats.R prints the same.
    rsv/                     the Infant RSV analysis, set to read data/
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BLUEPRINT = REPO / "YAMLs" / "temp" / "Infant_RSV_blueprint.yaml"
DICTIONARY = REPO / "reference" / "datadictionary.yaml"
RSV_SOURCE = REPO / "studies" / "infant_rsv" / "rsv"
TEMPLATES = HERE / "templates"
DEFAULT_OUT = REPO / "dist" / "synthetic_rsv_repo"
sys.path.insert(0, str(REPO / "scripts" / "pullmanager_src"))
from pullmanager.artifacts import arrow_type  # noqa: E402  (the real parquets' typing)

VISITS_PER_CHILD = 1.14     # about one child in eight comes back
KEY_PREFIX = 7007            # every synthetic key begins with these digits
FIRST_DAY, LAST_DAY = date(2019, 1, 1), date(2026, 5, 31)

# Each kind of key has its own two digits after the prefix: 7007 TT NNNNNNN.
KEY_KINDS = {"patient": 1, "visit": 2, "encounter": 3, "admission": 4, "vitals": 5, "lab": 6, "labcomponent": 7,
             "diagnosisevent": 8, "diagnosis": 9, "birth": 10, "pregnancy": 11, "medadmin": 12, "medication": 13,
             "medorder": 14, "department": 15, "provider": 16, "icustay": 17, "other": 99}


def key(kind: str, n: int | np.ndarray) -> int | np.ndarray:
    return KEY_PREFIX * 10**9 + KEY_KINDS[kind] * 10**7 + n


def date_key(when) -> int | None:
    if when is None or (isinstance(when, float) and math.isnan(when)) or pd.isna(when):
        return None
    return int(pd.Timestamp(when).strftime("%Y%m%d"))


def time_key(when) -> int | None:
    if when is None or pd.isna(when):
        return None
    t = pd.Timestamp(when)
    return t.hour * 100 + t.minute


# ---------------------------------------------------------------- vocabularies

# What Cosmos holds, as `rsv verify` showed it (V: its check number) on 20,000
# infant ED visits from December 2024, 9 October 2026. Each list is a value and
# the count seen, used as weights; values seen fewer than 11 times are left out.
# A column with no verified values here is left empty in the parquets.
RACES = [("White", 8127), ("Black or African American", 4417), ("", 3449), ("Other Race", 1708),      # V14
         ("Asian", 580), ("American Indian or Alaska Native", 308), ("Native Hawaiian or Other Pacific Islander", 125)]
MULTIRACIAL_SHARE = 3269 / 18714                                                                       # V15
ETHNICITIES = [("Not Hispanic or Latino", 10407), ("Hispanic or Latino", 4316), ("*Unspecified", 3991)]  # V16
SEXES = [("Male", 10414), ("Female", 8299)]                                                            # V17
BIRTH_ACCURACY = [("Instant", 13980), ("Day", 2710), ("Month", 1177), ("Week", 847)]                   # V12
BIRTH_ROW_SHARE = 16067 / 18714                                                                        # V24
FINANCIAL = [("Miscellaneous/Other", 12206), ("Medicaid", 6773), ("Self-Pay", 443), ("*Not Applicable", 292),
             ("*Unspecified", 285)]                                                                     # V19
ACUITY = [("Level 4 - Less Urgent", 7632), ("Level 3 - Urgent", 6972), ("Level 2 - Emergent", 4164),
          ("Level 5 - Non-Urgent", 626), ("*Unspecified", 377), ("Level 1 - Immediate", 229)]          # V20
ARRIVAL = [("Private Transport", 12115), ("Pedestrian Transport", 5356), ("Ambulance Transport", 1552),
           ("*Unspecified", 351), ("Unknown", 269), ("Ground Ambulance Transport", 197), ("Public Transport", 81),
           ("Helicopter Ambulance Transport", 73)]                                                     # V20
DISPOSITION = [("Discharged to Home or Self Care (Routine Discharge)", 19483),
               ("Left Against Medical Advice or Discontinued Care", 101),
               ("Discharged/transferred to a Short-Term General Hospital for Inpatient Care", 92),
               ("Discharged/transferred to Home Under Care of Organized Home Health Service Org", 62),
               ("Assisted Living", 52), ("*Unspecified", 48),
               ("Disch/trans to Another Type of Health Care Inst not Defined Elsewhere in this List", 38),
               ("Discharged/transferred to a Designated Cancer Center or Children's Hospital", 38),
               ("*Not Applicable", 21), ("Expired", 20)]                                                # V20
GENERIC_NOT_ADMITTED = [("Discharge", 13884), ("Observation", 227), ("*Unspecified", 206), ("Transfer", 119),
                        ("Left Without Being Seen", 84), ("Against Medical Advice", 36)]                # V20
DEPARTURE_MISSING = 381 / 20000                                                                        # V3
VISITS_WITH_VITALS = 19900 / 20000                                                                     # V5
RSV_CODES = [("J21.0", 3152), ("B97.4", 1215), ("J12.1", 147), ("J20.5", 27)]                         # V25, V27
# ED lab codes seen (the first build page, 9 October 2026, and V28); names only where V28 showed them.
LAB_CODES = [("2345-7", 44018), ("2951-2", 43866), ("2075-0", 43710), ("2160-0", 43096), ("3094-0", 42991),
             ("17861-6", 42506), ("2823-3", 41202), ("2028-9", 40271), ("718-7", 40079), ("786-4", 37419),
             ("777-3", 35696), ("788-0", 34990), ("787-2", 34899), ("789-8", 34077)]
UNSPECIFIED_LOINC_SHARE = 87884 / 619689                                                               # V29
VBG_LABS = [  # LOINC, Name, LoincName, results seen (V28)
    ("2746-6", "pH BldV", "pH of Venous blood", 1611),
    ("2021-4", "pCO2 BldV", "Carbon dioxide [Partial pressure] in Venous blood", 1418),
    ("2705-2", "pO2 BldV", "Oxygen [Partial pressure] in Venous blood", 1338),
    ("14627-4", "HCO3 BldV-sCnc", "Bicarbonate [Moles/volume] in Venous blood", 1165),
    ("1927-3", "Base excess BldV Calc-sCnc", "Base excess in Venous blood by calculation", 1141),
    ("2711-0", "SaO2 % BldV", "Oxygen saturation in Venous blood", 778)]
IV_ROUTES = [("intravenous", 213125), ("Intravenous Drip", 1147), ("Intravenous bolus", 93)]           # V30
OTHER_ROUTES = [("oral", 57403), ("Respiratory (Inhalation)", 37823), ("", 21834), ("Enteral", 8289),
                ("Nasogastric", 5251), ("Topical", 5234)]                                               # V30
IV_NAMES = [  # MedicationDim names of IV doses (V32); the hydrating fluids are marked
    ("*Unspecified", 86215, False),
    ("DEXTROSE 5 % AND 0.9 % SODIUM CHLORIDE INTRAVENOUS SOLUTION", 19842, True),
    ("SODIUM CHLORIDE 0.9 % INTRAVENOUS SOLUTION", 12223, True),
    ("POTASSIUM CHLORIDE 20 MEQ/L IN D5-0.9 % SODIUM CHLORIDE INTRAVENOUS", 11132, True),
    ("DEXTROSE 5 % AND 0.45 % SODIUM CHLORIDE INTRAVENOUS SOLUTION", 8024, True),
    ("POTASSIUM CHLORIDE 20 MEQ/L IN DEXTROSE 5 %-0.45 % SODIUM CHLORIDE IV", 5653, True),
    ("DEXTROSE 5 % AND LACTATED RINGERS INTRAVENOUS SOLUTION", 4170, True),
    ("SODIUM CHLORIDE 0.9 % (FLUSH) INJECTION SYRINGE", 2342, False),
    ("ACETAMINOPHEN 500 MG/50 ML (10 MG/ML) INTRAVENOUS SOLUTION", 2174, False),
    ("LACTATED RINGERS INTRAVENOUS SOLUTION", 1251, True),
    ("SODIUM CHLORIDE 0.9 % INJECTION SOLUTION", 1111, False),
    ("DEXAMETHASONE SODIUM PHOSPHATE 4 MG/ML INJECTION SOLUTION", 789, False),
    ("AMPICILLIN 500 MG SOLUTION FOR INJECTION", 677, False),
    ("ONDANSETRON HCL (PF) 4 MG/2 ML INJECTION SOLUTION", 600, False),
    ("CEFTRIAXONE 2 GRAM SOLUTION FOR INJECTION", 555, False)]
ACTIONS_GIVEN = [(("Given", 1), 145118), (("New Bag", 1), 39070), (("Given", None), 23848)]           # V31
STAY_DOSES_WITH_DEPARTMENT = 1 - 386867 / 439946                                                      # V33
# Department specialties, as Cosmos names them (`rsv icu`, 9 October 2026).
WARD_SPECIALTIES = ["Pediatrics", "Hospital Medicine", "Neonatology", "Pediatric Medical Ward"]
WARD_SHARES = [38115, 2663, 61676, 1583]
ICU_SPECIALTIES = ["Pediatric Intensive Care", "Critical Care Medicine", "Pediatric Critical Care Medicine"]
ICU_SHARES = [1797, 1675, 244]


def pick(rng: np.random.Generator, pairs: list[tuple[str, float]], size: int) -> np.ndarray:
    names = [p[0] for p in pairs]
    weights = np.array([p[1] for p in pairs], dtype=float)
    return rng.choice(names, size=size, p=weights / weights.sum())


# ---------------------------------------------------------------- the blueprint's tables

def table_specs(blueprint: Path = BLUEPRINT) -> dict[str, dict]:
    doc = yaml.safe_load(blueprint.read_text(encoding="utf-8"))
    specs = {}
    for cohort in doc["cohorts"]:
        specs[cohort["dest_table"]] = {
            "columns": [(c["name"], str(c.get("type") or "NVARCHAR(300)")) for c in cohort["columns"]],
            "description": " ".join(str(cohort.get("description") or "").split()),
            "granularity": " ".join(str(cohort.get("granularity") or "").split()),
            "sources": sorted({c["source"].split(".")[0] for c in cohort["columns"]}),
            "from": cohort.get("filter", {}).get("from", []),
            "join": cohort.get("filter", {}).get("join", []),
        }
    return specs


# ---------------------------------------------------------------- the world

class World:
    """Patients, their visits, and everything that happens on them.

    Every text value comes from what Cosmos showed (the lists above). Every
    number, date and relationship between columns is the generator's own: in
    particular, sicker children are the younger and more premature ones, and
    race, ethnicity, SVI and financial class have no built-in relation to
    anything.
    """

    def __init__(self, patients: int, seed: int = 7) -> None:
        self.rng = np.random.default_rng(seed)
        self.n_patients = patients
        self.n_visits = int(round(patients * VISITS_PER_CHILD))
        self.make_patients()
        self.make_visits()

    # Patients first: a birth date, demographics, and a gestational age.
    def make_patients(self) -> None:
        rng = self.rng
        n = self.n_patients
        span = (LAST_DAY - date(2017, 1, 1)).days
        births = pd.to_datetime(date(2017, 1, 1)) + pd.to_timedelta(rng.integers(0, span, n), unit="D")
        preterm = rng.random(n) < 0.11                    # the generator's share, not Cosmos's
        ga = np.where(preterm, rng.normal(33, 2.5, n), rng.normal(39, 1.1, n)).clip(23, 42)
        accuracy = pick(rng, BIRTH_ACCURACY, n)
        self.patients = pd.DataFrame({
            "n": np.arange(1, n + 1), "DurableKey": key("patient", np.arange(1, n + 1)), "BirthDate": births.normalize(),
            "BirthDateAccuracy_X": accuracy,
            "Sex": pick(rng, SEXES, n), "FirstRace": pick(rng, RACES, n),
            "MultiRacial": (rng.random(n) < MULTIRACIAL_SHARE).astype(int), "Ethnicity": pick(rng, ETHNICITIES, n),
            "svi": rng.uniform(0.0001, 1.0, n), "ga_days": (ga * 7 + rng.integers(0, 7, n)).astype(int),
            "born_here": rng.random(n) < BIRTH_ROW_SHARE,
        })

    # Visits: an age at arrival, skewed young, in RSV season, and a severity that drives the rest.
    def make_visits(self) -> None:
        rng = self.rng
        p = self.patients
        n = self.n_visits
        rows = []
        first = rng.permutation(len(p))[: min(len(p), n)]
        repeat = rng.choice(len(p), n - len(first))
        for index in np.concatenate([first, repeat]):
            birth = p.at[index, "BirthDate"]
            for _ in range(40):
                age = int(min(730, rng.exponential(170)))
                arrival = birth + pd.Timedelta(days=age)
                month = arrival.month
                weight = 1.0 if month in (11, 12, 1, 2) else .55 if month in (10, 3) else .12
                if arrival.year == 2020 and month >= 4 or arrival.year == 2021 and month <= 3:
                    weight *= .15
                if arrival.year == 2021 and month in (6, 7, 8):
                    weight = .8
                if pd.Timestamp(FIRST_DAY) <= arrival <= pd.Timestamp(LAST_DAY) and rng.random() < weight:
                    break
            else:                        # no in-season day found: a day in the first two months of life
                arrival = birth + pd.Timedelta(days=int(rng.integers(0, 60)))
            rows.append((index, arrival + pd.Timedelta(minutes=int(rng.integers(0, 1440)))))
        v = pd.DataFrame(rows, columns=["patient_index", "ArrivalInstant"]).sort_values("ArrivalInstant").reset_index(drop=True)
        v["n"] = np.arange(1, n + 1)
        v["EdVisitKey"] = key("visit", v["n"])
        v["EncounterKey"] = key("encounter", v["n"])
        v["PatientDurableKey"] = p["DurableKey"].to_numpy()[v["patient_index"]]
        age_days = (v["ArrivalInstant"].dt.normalize() - p["BirthDate"].to_numpy()[v["patient_index"]]).dt.days
        v["age_days"] = age_days
        ga = p["ga_days"].to_numpy()[v["patient_index"]] / 7
        # Severity: younger and more premature children are sicker. Made up, for practice.
        v["severity"] = 1.6 * np.exp(-age_days.clip(lower=0) / 90) + 0.12 * np.clip(37 - ga, 0, None) + rng.normal(0, .7, n)
        v["financial"] = pick(rng, FINANCIAL, n)
        admit_p = 1 / (1 + np.exp(-(v["severity"] - 1.9) * 1.8))
        v["admitted"] = rng.random(n) < admit_p
        v["icu"] = v["admitted"] & (rng.random(n) < 1 / (1 + np.exp(-(v["severity"] - 2.6) * 2)))
        v["ed_hours"] = rng.uniform(1, 6, n)
        v["DepartureInstant"] = v["ArrivalInstant"] + pd.to_timedelta(v["ed_hours"] * 60, unit="m").dt.round("min")
        v.loc[rng.random(n) < DEPARTURE_MISSING, "DepartureInstant"] = pd.NaT
        v["los_days"] = np.where(v["admitted"], np.clip(rng.gamma(2, 1.3 + v["severity"].clip(0) * .4), 1, 21).round(), 0)
        admitted_n = np.cumsum(v["admitted"])
        v["HospitalAdmissionKey"] = np.where(v["admitted"], key("admission", admitted_n), -1)
        # An admission is on its ED visit's own encounter (V9: 100%).
        v["AdmissionEncounterKey"] = np.where(v["admitted"], v["EncounterKey"], -1)
        departure = v["DepartureInstant"].fillna(v["ArrivalInstant"] + pd.Timedelta(hours=4))
        v["InpatientAdmissionInstant"] = departure.where(v["admitted"])
        v["DischargeInstant"] = (departure + pd.to_timedelta(v["los_days"], unit="D")
                                 + pd.to_timedelta(rng.integers(0, 600, n), unit="m")).where(v["admitted"])
        self.visits = v

    # ------------------------------------------------------------ the tables, by their real names

    def ed_window(self, index: int, hours_after: float | None = None):
        v = self.visits
        start = v.at[index, "ArrivalInstant"]
        end = v.at[index, "DepartureInstant"]
        if pd.isna(end):
            end = start + pd.Timedelta(hours=hours_after or 4)
        return start, end

    def ed_visits(self) -> pd.DataFrame:
        v, rng = self.visits, self.rng
        n = len(v)
        generic = np.where(v["admitted"], "Admit", pick(rng, GENERIC_NOT_ADMITTED, n))
        return pd.DataFrame({
            "EdVisitKey": v["EdVisitKey"], "EncounterKey": v["EncounterKey"], "PatientDurableKey": v["PatientDurableKey"],
            "ArrivalInstant": v["ArrivalInstant"], "DepartureInstant": v["DepartureInstant"],
            "HospitalAdmissionKey": v["HospitalAdmissionKey"], "FinancialClass": v["financial"],
            "DischargeDisposition": pick(rng, DISPOSITION, n), "EdGenericDispo": generic,
            "AcuityLevel": pick(rng, ACUITY, n), "ArrivalMethod": pick(rng, ARRIVAL, n),
        })

    def patients_table(self) -> pd.DataFrame:
        p = self.patients
        out = p[["DurableKey", "BirthDate", "BirthDateAccuracy_X", "Sex", "FirstRace", "MultiRacial", "Ethnicity"]].copy()
        out["BirthDate"] = out["BirthDate"].dt.date
        # Instant and Day birth dates equal the earliest possible one (V12); Month and Week's never
        # do, by how much is not known, so theirs is left empty.
        exact = out["BirthDateAccuracy_X"].isin(["Instant", "Day"])
        out["EarliestPossibleBirthDate_X"] = out["BirthDate"].where(exact, None)
        out["SviOverallPctlRankByZip2020_X"] = p["svi"].round(4)
        return out

    def vitals(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        v, rng = self.visits, self.rng
        ed, stay = [], []
        counter = 0
        for i in range(len(v)):
            if rng.random() > VISITS_WITH_VITALS:
                continue
            start, end = self.ed_window(i)
            sev, age = v.at[i, "severity"], v.at[i, "age_days"]
            base_rr = 45 - age / 40 + sev * 6
            readings = int(rng.integers(2, 7))
            times = sorted(start + (end - start) * rng.random(readings))
            if rng.random() < .1:
                times = [start - pd.Timedelta(minutes=int(rng.integers(5, 120)))] + times
            for t_ in times:
                counter += 1
                ed.append(self.vital_row(counter, v.at[i, "EncounterKey"], v.at[i, "PatientDurableKey"], t_,
                                         base_rr, sev, "EncounterKey"))
            if v.at[i, "admitted"]:
                t_ = v.at[i, "InpatientAdmissionInstant"]
                while t_ < v.at[i, "DischargeInstant"]:
                    counter += 1
                    stay.append(self.vital_row(counter, v.at[i, "AdmissionEncounterKey"], v.at[i, "PatientDurableKey"],
                                               t_, base_rr * .9, sev * .8, "InpatientEncounterKey"))
                    t_ += pd.Timedelta(hours=4)
        return pd.DataFrame(ed), pd.DataFrame(stay)

    def vital_row(self, counter, encounter, patient, when, base_rr, sev, encounter_column):
        rng = self.rng
        when = pd.Timestamp(when)
        rr = int(np.clip(rng.normal(base_rr, 6), 15, 95))
        spo2 = int(np.clip(rng.normal(97.5 - sev * 2.2, 2), 70, 100))
        temp_c = float(np.clip(rng.normal(37.6 + sev * .25, .6), 35.5, 41))
        if rng.random() < .001:
            rr = 0                       # Cosmos has a few out of range (V8: 0.1% outside 5-150)
        return {"VitalsKey": key("vitals", counter), encounter_column: encounter, "PatientDurableKey": patient,
                "TakenInstant": when.round("min"), "DateKey": date_key(when),
                "RespirationRate": rr if rng.random() > .05 else None,
                "SpO2": spo2 if rng.random() > .05 else None,
                "Temperature": round(temp_c * 9 / 5 + 32, 1)}    # every one in °F (V6)

    def labs(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        v, rng = self.visits, self.rng
        ed, stay = [], []
        counter = 0
        top = max(n for _, n in LAB_CODES)
        component = {code: i + 1 for i, code in enumerate([c for c, _ in LAB_CODES] + [c[0] for c in VBG_LABS])}

        def row(rows, encounter, patient, when, loinc, name=None, loinc_name=None):
            nonlocal counter
            counter += 1
            rows.append({"LabComponentResultKey": key("lab", counter), "EncounterKey": encounter, "PatientDurableKey": patient,
                         "LabComponentKey": key("labcomponent", component.get(loinc, 999)),
                         "CollectionInstant": pd.Timestamp(when).round("min"),
                         "ComponentLoincCode": loinc, "ComponentName": name, "ComponentLoincName": loinc_name})

        def draw(rows, encounter, patient, when, vbg_p):
            for loinc, n in LAB_CODES:
                if rng.random() < .9 * n / top:
                    row(rows, encounter, patient, when, loinc)
            for _ in range(rng.poisson(2.3)):         # components with no LOINC code (V29: 14%)
                row(rows, encounter, patient, when, "*Unspecified")
            if rng.random() < vbg_p:
                first = VBG_LABS[0][3]
                for loinc, name, loinc_name, n in VBG_LABS:
                    if rng.random() < n / first:
                        row(rows, encounter, patient, when, loinc, name, loinc_name)

        for i in range(len(v)):
            sev = v.at[i, "severity"]
            start, end = self.ed_window(i)
            if rng.random() < .25 + .15 * min(sev, 3):
                draw(ed, v.at[i, "EncounterKey"], v.at[i, "PatientDurableKey"], start + (end - start) * rng.random(),
                     .05 + .08 * max(sev, 0))
            if v.at[i, "admitted"]:
                t_ = v.at[i, "InpatientAdmissionInstant"] + pd.Timedelta(hours=6)
                while t_ < v.at[i, "DischargeInstant"]:
                    draw(stay, v.at[i, "AdmissionEncounterKey"], v.at[i, "PatientDurableKey"], t_, .2 if v.at[i, "icu"] else .03)
                    t_ += pd.Timedelta(hours=24)
        return pd.DataFrame(ed), pd.DataFrame(stay)

    def ed_meds(self) -> pd.DataFrame:
        v, rng = self.visits, self.rng
        rows, counter = [], 0
        names = [n for n, _, _ in IV_NAMES]
        weights = np.array([w for _, w, _ in IV_NAMES], dtype=float)
        fluid_weights = np.array([w * (6 if fluid else 1) for _, w, fluid in IV_NAMES], dtype=float)
        actions = [a for a, _ in ACTIONS_GIVEN]
        action_weights = np.array([w for _, w in ACTIONS_GIVEN], dtype=float)
        for i in range(len(v)):
            sev = max(v.at[i, "severity"], 0)
            start, end = self.ed_window(i)
            iv_doses = rng.poisson(.4 + .5 * sev)
            other_doses = rng.poisson(.8)
            for d in range(iv_doses + other_doses):
                counter += 1
                iv = d < iv_doses
                if iv:
                    w = fluid_weights if sev > 1.5 else weights
                    name = names[rng.choice(len(names), p=w / w.sum())]
                    route = pick(rng, IV_ROUTES, 1)[0]
                else:
                    name, route = None, pick(rng, OTHER_ROUTES, 1)[0]   # names on these routes not yet verified
                action, is_given = actions[rng.choice(len(actions), p=action_weights / action_weights.sum())]
                rows.append({"MedicationAdministrationKey": key("medadmin", counter), "EncounterKey": v.at[i, "EncounterKey"],
                             "PatientDurableKey": v.at[i, "PatientDurableKey"],
                             "AdministrationInstant": pd.Timestamp(start + (end - start) * rng.random()).round("min"),
                             "AdministrationRoute": route, "AdministrationAction": action,
                             "ActionIsMedAdministration": is_given, "MedicationKey": key("medication", names.index(name) + 1 if name in names else 999),
                             "MedicationName": name})
        return pd.DataFrame(rows)

    def diagnoses(self) -> pd.DataFrame:
        v, rng = self.visits, self.rng
        rows, counter = [], 0
        for i in range(len(v)):
            codes = [pick(rng, RSV_CODES, 1)[0]]
            if rng.random() < .25:
                codes.append("B97.4" if codes[0] != "B97.4" else "J21.0")
            for position, code in enumerate(dict.fromkeys(codes)):
                counter += 1
                rows.append({"DiagnosisEventKey": key("diagnosisevent", counter), "EncounterKey": v.at[i, "EncounterKey"],
                             "PatientDurableKey": v.at[i, "PatientDurableKey"],
                             "DiagnosisKey": key("diagnosis", 1 + sum(map(ord, code)) % 9000),
                             "EmergencyDepartmentDiagnosis": 1, "IsPrimary": int(position == 0),
                             "BillingCodeValue": code, "CodeType": "ICD-10-CM"})
        return pd.DataFrame(rows)

    def admissions(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Specialties as Cosmos names them (`rsv icu`, 9 October 2026), weighted by its infant
        admissions in December 2024 (births among them). There, an admission's admitted-to and
        discharged-from departments were nearly always the same, so they are here too."""
        v, rng = self.visits[self.visits["admitted"]], self.rng
        n = len(v)
        ward = rng.choice(WARD_SPECIALTIES, n, p=np.array(WARD_SHARES) / sum(WARD_SHARES))
        unit = rng.choice(ICU_SPECIALTIES, n, p=np.array(ICU_SHARES) / sum(ICU_SHARES))
        admit = np.where(v["icu"], unit, ward)
        department = {name: key("department", i + 1) for i, name in enumerate(WARD_SPECIALTIES + ICU_SPECIALTIES)}
        haf = pd.DataFrame({
            "HospitalAdmissionKey": v["HospitalAdmissionKey"], "EncounterKey": v["AdmissionEncounterKey"],
            "PatientDurableKey": v["PatientDurableKey"], "AdmissionDateKey": v["InpatientAdmissionInstant"].map(date_key),
            "InpatientAdmissionInstant": v["InpatientAdmissionInstant"].dt.round("min"),
            "InpatientAdmissionDateKey": v["InpatientAdmissionInstant"].map(date_key),
            "DischargeInstant": v["DischargeInstant"].dt.round("min"), "DischargeDateKey": v["DischargeInstant"].map(date_key),
            "LengthOfStayInDays": v["los_days"].astype(int), "InpatientLengthOfStayInDays": v["los_days"].astype(int),
            "DepartmentKey": [department[s] for s in admit], "DischargeDepartmentKey_X": [department[s] for s in admit],
            "AdmitSpecialty": admit, "DischargeSpecialty": admit, "FinancialClass": v["financial"],
        })
        rows, stays = [], []
        for (_, row), specialty in zip(v.iterrows(), admit):
            if rng.random() < STAY_DOSES_WITH_DEPARTMENT * 3:    # most doses name no department (V33)
                rows.append({"HospitalAdmissionKey": row["HospitalAdmissionKey"], "AdministrationDepartmentKey": department[specialty],
                             "DepartmentSpecialty": specialty,
                             "AdministrationInstant": row["InpatientAdmissionInstant"].round("min")})
            if row["icu"]:                        # the ICU Stay Registry's stay, begun soon after admission
                start = (row["InpatientAdmissionInstant"] + pd.Timedelta(hours=int(rng.integers(0, 12)))).round("min")
                end = min(row["DischargeInstant"], start + pd.Timedelta(hours=int(rng.integers(18, 120)))).round("min")
                stays.append({"IcuStayRegistryKey": key("icustay", len(stays) + 1),
                              "HospitalAdmissionKey": row["HospitalAdmissionKey"], "EdVisitKey": row["EdVisitKey"],
                              "IcuEncounterKey": row["AdmissionEncounterKey"], "AdmissionEncounterKey": row["AdmissionEncounterKey"],
                              "PatientDurableKey": row["PatientDurableKey"], "DepartmentKey": department[specialty],
                              "IcuSpecialty": specialty, "IcuStayStartInstant": start, "IcuStayEndInstant": end,
                              "IcuLengthOfStay": round((end - start).total_seconds() / 86400, 2),
                              "AgeAtIcuStayStart": round(row["age_days"] / 365.25, 2)})
        self.icu_stays = pd.DataFrame(stays)
        return haf, pd.DataFrame(rows)

    def births(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Keys, the birth instant and gestational age (days, V23); the rest is not yet verified."""
        p, rng = self.patients[self.patients["born_here"]].reset_index(drop=True), self.rng
        n = len(p)
        mothers = key("patient", 5_000_000 + np.arange(1, n + 1))
        pregnancies = key("pregnancy", np.arange(1, n + 1))
        instant = p["BirthDate"] + pd.to_timedelta(rng.integers(0, 1440, n), unit="m")
        births = pd.DataFrame({
            "BabyPatientDurableKey": p["DurableKey"], "BirthKey": key("birth", np.arange(1, n + 1)), "BirthInstant": instant,
            "BirthDateKey": instant.map(date_key), "GestationalAgeDays": p["ga_days"],
            "MotherPatientDurableKey": mothers, "PregnancyKey": pregnancies,
        })
        mother = pd.DataFrame({"DurableKey": mothers})
        pregnancy = pd.DataFrame({"PregnancyKey": pregnancies, "PatientDurableKey": mothers})
        return births, mother, pregnancy

    def tables(self) -> dict[str, pd.DataFrame]:
        ed_vitals, stay_vitals = self.vitals()
        ed_labs, stay_labs = self.labs()
        haf, stay_departments = self.admissions()
        births, mother, pregnancy = self.births()
        return {"EDVisits": self.ed_visits(), "Patients": self.patients_table(), "EDVitals": ed_vitals,
                "EDLabs": ed_labs, "EDDiagnoses": self.diagnoses(), "EDMeds": self.ed_meds(),
                "StayDepartments": stay_departments, "HospitalAdmissionFact": haf, "InpatientVitals": stay_vitals,
                "InpatientLabs": stay_labs, "Births": births, "MotherPatientInfo": mother, "PregnancyFact": pregnancy,
                "IcuStays": self.icu_stays}


# ---------------------------------------------------------------- every column of the real table

def complete(frame: pd.DataFrame, columns: list[tuple[str, str]], rng: np.random.Generator) -> pd.DataFrame:
    """Every column the real table has, in its order: the generated ones as made; a DateKey or
    TimeOfDayKey from its own Instant when that was made; every other column empty, since what
    Cosmos holds in it has not been verified."""
    out = {}
    for name, _sql in columns:
        if name in frame:
            out[name] = frame[name]
        elif name.endswith("DateKey") and name.replace("DateKey", "Instant") in frame:
            out[name] = frame[name.replace("DateKey", "Instant")].map(date_key)
        elif name.endswith("TimeOfDayKey") and name.replace("TimeOfDayKey", "Instant") in frame:
            out[name] = frame[name.replace("TimeOfDayKey", "Instant")].map(time_key)
        else:
            out[name] = pd.Series([None] * len(frame), index=frame.index, dtype="object")
    return pd.DataFrame(out, index=frame.index).reset_index(drop=True)


def arrow_table(frame: pd.DataFrame, columns: list[tuple[str, str]]):
    import pyarrow as pa
    arrays, fields = [], []
    for name, sql in columns:
        typ = arrow_type(pa, sql)
        values = frame[name]
        if pa.types.is_decimal(typ):
            scale = typ.scale
            data = [None if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v)
                    else Decimal(str(round(float(v), scale))) for v in values]
        elif pa.types.is_timestamp(typ):
            data = pd.to_datetime(values)
        elif pa.types.is_date(typ):
            data = [None if v is None or pd.isna(v) else pd.Timestamp(v).date() for v in values]
        elif pa.types.is_integer(typ) or pa.types.is_boolean(typ):
            data = pd.array(pd.to_numeric(values, errors="coerce"), dtype="Int64")
            data = [None if pd.isna(x) else (bool(x) if pa.types.is_boolean(typ) else int(x)) for x in data]
        elif pa.types.is_floating(typ):
            data = pd.to_numeric(values, errors="coerce")
        else:
            data = [None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v) for v in values]
        arrays.append(pa.array(data, type=typ, from_pandas=True))
        fields.append(pa.field(name, typ))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


# ---------------------------------------------------------------- STATS.md: questions with answers, in Python and R

HEADLINE = [("visits", "ED visits"), ("children", "Children"), ("admitted_pct", "Visits admitted"),
            ("icu_pct_of_admissions", "Admissions to an ICU"),
            ("picu_pct_of_admissions", "Admissions to Pediatric Intensive Care"),
            ("median_age_days", "Median age at arrival, days"), ("under_3_months_pct", "Visits under 3 months old"),
            ("premature_pct", "Born premature (of those with a birth row)"),
            ("outside_season_pct", "Visits outside October to March"),
            ("outside_season_admissions_pct", "Admissions outside October to March"),
            ("iv_fluids_pct", "Visits with IV fluids in the ED"), ("vbg_pct", "Visits with a VBG in the ED"),
            ("median_los", "Median length of stay, days")]

PY_HEAD = '''"""Every answer in STATS.md, computed. Run from the repository's top folder:

    python python/stats.py

Each block answers one question; they run in order, and later ones reuse
`visits` and the rest.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_parquets import load  # noqa: E402

tables = load()


def show(name, answer, kind):
    value = {"count": lambda v: f"{int(v):,}", "pct": lambda v: f"{100 * float(v):.1f}%",
             "num": lambda v: f"{float(v):.1f}", "int": lambda v: str(int(v))}[kind](answer)
    print(f"{name:<30} {value}")
'''

R_HEAD = '''# Every answer in STATS.md, computed. Run from the repository's top folder:
#
#   Rscript R/stats.R      (or source("R/stats.R") in R or RStudio)
#
# Each block answers one question; they run in order, and later ones reuse
# `visits` and the rest.

suppressPackageStartupMessages({
  library(dplyr)
  library(arrow)
  library(bit64)
})
source(file.path("R", "load_parquets.R"))

tables <- load_parquets()

show <- function(name, answer, kind) {
  answer <- as.numeric(answer)
  value <- switch(kind,
    count = formatC(answer, format = "d", big.mark = ","),
    pct = sprintf("%.1f%%", 100 * answer),
    num = sprintf("%.1f", answer),
    int = as.character(as.integer(answer)))
  cat(sprintf("%-30s %s\\n", name, value))
}
'''


def load_questions() -> list[dict]:
    questions = yaml.safe_load((TEMPLATES / "stats_questions.yaml").read_text(encoding="utf-8"))
    section = None
    for q in questions:
        section = q.get("section", section)
        q["section"] = section
    return questions


def stats_python(questions: list[dict]) -> str:
    parts = [PY_HEAD]
    for q in questions:
        parts.append(f"\n# {q['id']}: {q['question']}\n{q['python'].rstrip()}\nshow({q['id']!r}, answer, {q['kind']!r})\n")
    return "".join(parts)


def stats_r(questions: list[dict]) -> str:
    parts = [R_HEAD]
    for q in questions:
        parts.append(f"\n# {q['id']}: {q['question']}\n{q['r'].rstrip()}\nshow(\"{q['id']}\", answer, \"{q['kind']}\")\n")
    return "".join(parts)


def run_stats(out: Path, command: list[str]) -> dict[str, str]:
    """What a stats script prints, as {id: value}."""
    result = subprocess.run(command, cwd=out, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed:\n{result.stdout}\n{result.stderr}")
    answers = {}
    for line in result.stdout.splitlines():
        name, _, value = line.partition(" ")
        answers[name] = value.strip()
    return answers


def stats_md(questions: list[dict], answers: dict[str, str]) -> str:
    out = [render_template("stats_head.md"), ""]
    section = None
    for number, q in enumerate(questions, 1):
        if q["section"] != section:
            section = q["section"]
            out += [f"## {section}", ""]
        out += [f"### {number}. {q['question']}", "", f"**Answer: {answers[q['id']]}**", "",
                "<details><summary>Python</summary>", "", "```python", q["python"].rstrip(), "```", "", "</details>", "",
                "<details><summary>R</summary>", "", "```r", q["r"].rstrip(), "```", "", "</details>", ""]
    return "\n".join(out)


def headline(questions: list[dict], answers: dict[str, str]) -> str:
    rows = ["| | |", "|---|---:|"]
    rows += [f"| {label} | {answers[i]} |" for i, label in HEADLINE]
    return "\n".join(rows)


# ---------------------------------------------------------------- the repository

def strip_comments(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip() + "\n"


def contents_md(specs: dict[str, dict], rows: dict[str, int], patients: int, seed: int) -> str:
    out = [render_template("contents_head.md", visits=f"{rows['EDVisits']:,}", patients=f"{patients:,}", seed=seed),
           "", "## Tables", "",
           "| Table | Rows | One row per |", "|---|---:|---|"]
    for name, spec in specs.items():
        out.append(f"| [{name}](#{name.lower()}) | {rows[name]:,} | {spec['granularity'] or '-'} |")
    for name, spec in specs.items():
        out += ["", f"## {name}", "", spec["description"] or "", "",
                f"File: `data/cosmos_parquets/{name}.parquet`, {rows[name]:,} rows. From Cosmos's "
                + ", ".join(f"`{f.split()[0]}`" for f in spec["from"]) + ".", ""]
        joins = [j for j in spec["join"] if "{{prefix}}" in j]
        if joins:
            out.append("Linked by: " + "; ".join(f"`{re.sub(r'{{prefix}}_', '', j.split(' ON ', 1)[1] if ' ON ' in j else j)}`"
                                                for j in joins) + ".")
            out.append("")
        out += ["| Column | SQL type |", "|---|---|"]
        out += [f"| {c} | {t} |" for c, t in spec["columns"]]
    return "\n".join(out) + "\n"


def render_template(name: str, **values) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def build(out: Path = DEFAULT_OUT, patients: int = 20000, seed: int = 7) -> dict[str, int]:
    import pyarrow.parquet as pq
    specs = table_specs()
    world = World(patients, seed)
    tables = world.tables()
    missing = set(specs) - set(tables)
    if missing:
        raise SystemExit(f"The blueprint has tables this generator does not make: {', '.join(sorted(missing))}")
    if out.exists():
        shutil.rmtree(out)
    data = out / "data" / "cosmos_parquets"
    data.mkdir(parents=True)
    rows = {}
    for name, spec in specs.items():
        frame = complete(tables[name], spec["columns"], world.rng)
        pq.write_table(arrow_table(frame, spec["columns"]), data / f"{name}.parquet")
        rows[name] = len(frame)
    (out / "Contents.md").write_text(contents_md(specs, rows, patients, seed), encoding="utf-8")
    (out / "DataDictionary.yaml").write_text(strip_comments(DICTIONARY.read_text(encoding="utf-8")), encoding="utf-8")
    (out / ".gitignore").write_text(render_template("gitignore"), encoding="utf-8")
    for folder in ("python", "R"):
        (out / folder).mkdir()
        for path in (TEMPLATES / folder).iterdir():
            shutil.copy(path, out / folder / path.name)
    questions = load_questions()
    (out / "python" / "stats.py").write_text(stats_python(questions), encoding="utf-8")
    (out / "R" / "stats.R").write_text(stats_r(questions), encoding="utf-8")
    answers = run_stats(out, [sys.executable, "python/stats.py"])
    (out / "STATS.md").write_text(stats_md(questions, answers), encoding="utf-8")
    (out / "README.md").write_text(render_template(
        "README.md", visits=f"{rows['EDVisits']:,}", patients=f"{patients:,}",
        headline=headline(questions, answers), questions=len(questions)), encoding="utf-8")
    shutil.copytree(RSV_SOURCE, out / "rsv", ignore=shutil.ignore_patterns("__pycache__", "sql"))
    settings = (out / "rsv" / "settings.yaml").read_text(encoding="utf-8")
    for old, new in (("pull_folder: runs/Infant_RSV ", "pull_folder: data "),
                     ("followup_folder: runs/Infant_RSV_Followup", "followup_folder: data/followup"),
                     ("analysis_folder: runs/Infant_RSV/analysis", "analysis_folder: output/analysis"),
                     ("min_cell: 11", "min_cell: 0 ")):
        if old not in settings:
            raise SystemExit(f"rsv/settings.yaml no longer has `{old.strip()}`; update make_synthetic_repo.py")
        settings = settings.replace(old, new)
    (out / "rsv" / "settings.yaml").write_text(settings, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"patients": patients, "seed": seed, "rows": rows, "answers": answers,
                                                    "key_prefix": KEY_PREFIX}, indent=1), encoding="utf-8")
    return rows


# ---------------------------------------------------------------- tests

class SyntheticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = Path(tempfile.mkdtemp(prefix="synthetic_rsv_"))
        cls.rows = build(cls.out, patients=500, seed=3)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.out, ignore_errors=True)

    def read(self, name):
        return pd.read_parquet(self.out / "data" / "cosmos_parquets" / f"{name}.parquet")

    def test_every_table_has_the_real_columns_and_types(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        for name, spec in table_specs().items():
            schema = pq.read_schema(self.out / "data" / "cosmos_parquets" / f"{name}.parquet")
            self.assertEqual(schema.names, [c for c, _ in spec["columns"]], name)
            for column, sql in spec["columns"]:
                self.assertEqual(schema.field(column).type, arrow_type(pa, sql), f"{name}.{column}")

    def test_every_key_begins_with_the_prefix(self):
        for name in table_specs():
            frame = self.read(name)
            for column in frame.columns:
                if column.endswith("Key") and not column.endswith(("DateKey", "TimeOfDayKey")):
                    values = frame[column].dropna()
                    values = values[values > 0]
                    self.assertTrue(values.astype(str).str.startswith(str(KEY_PREFIX)).all(), f"{name}.{column}")

    def test_no_visit_comes_before_the_childs_birth(self):
        visits = self.read("EDVisits").merge(self.read("Patients"), left_on="PatientDurableKey", right_on="DurableKey")
        self.assertTrue((visits["ArrivalInstant"].dt.normalize() >= pd.to_datetime(visits["BirthDate"])).all())

    def test_text_values_are_only_those_cosmos_showed(self):
        # D220: a value not seen in Cosmos is never written; a column not checked is empty.
        allowed = {
            ("Patients", "FirstRace"): {v for v, _ in RACES}, ("Patients", "Ethnicity"): {v for v, _ in ETHNICITIES},
            ("Patients", "Sex"): {v for v, _ in SEXES}, ("Patients", "BirthDateAccuracy_X"): {v for v, _ in BIRTH_ACCURACY},
            ("EDVisits", "FinancialClass"): {v for v, _ in FINANCIAL}, ("EDVisits", "AcuityLevel"): {v for v, _ in ACUITY},
            ("EDVisits", "ArrivalMethod"): {v for v, _ in ARRIVAL}, ("EDVisits", "DischargeDisposition"): {v for v, _ in DISPOSITION},
            ("EDVisits", "EdGenericDispo"): {v for v, _ in GENERIC_NOT_ADMITTED} | {"Admit"},
            ("EDDiagnoses", "BillingCodeValue"): {v for v, _ in RSV_CODES},
            ("EDLabs", "ComponentLoincCode"): {c for c, _ in LAB_CODES} | {c[0] for c in VBG_LABS} | {"*Unspecified"},
            ("EDMeds", "AdministrationRoute"): {v for v, _ in IV_ROUTES + OTHER_ROUTES},
            ("EDMeds", "MedicationName"): {n for n, _, _ in IV_NAMES},
            ("HospitalAdmissionFact", "AdmitSpecialty"): set(WARD_SPECIALTIES + ICU_SPECIALTIES),
        }
        for (table, column), values in allowed.items():
            with self.subTest(table=table, column=column):
                seen = set(self.read(table)[column].dropna())
                self.assertTrue(seen <= values, seen - values)
        specs = table_specs()
        for table, column in (("Patients", "PreferredLanguage"), ("Patients", "SecondRace"), ("Births", "DeliveryMethod"),
                              ("EDVitals", "PulseRate"), ("EDLabs", "NumericValue"), ("EDDiagnoses", "Type")):
            with self.subTest(table=table, column=column):
                self.assertIn(column, [c for c, _ in specs[table]["columns"]])
                self.assertTrue(self.read(table)[column].isna().all())

    def test_tables_link_to_the_visits(self):
        visits = self.read("EDVisits")
        for name in ("EDVitals", "EDLabs", "EDDiagnoses", "EDMeds"):
            self.assertTrue(self.read(name)["EncounterKey"].isin(visits["EncounterKey"]).all(), name)
        haf = self.read("HospitalAdmissionFact")
        admitted = visits[visits["HospitalAdmissionKey"] > 0]
        self.assertEqual(set(haf["HospitalAdmissionKey"]), set(admitted["HospitalAdmissionKey"]))
        self.assertTrue(self.read("InpatientVitals")["InpatientEncounterKey"].isin(haf["EncounterKey"]).all())
        self.assertTrue(self.read("Patients")["DurableKey"].is_unique)
        self.assertTrue(visits["PatientDurableKey"].isin(self.read("Patients")["DurableKey"]).all())

    def test_rsv_runs_on_it_and_finds_what_was_made(self):
        result = subprocess.run([sys.executable, "rsv", "all"], cwd=self.out, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        visits = pd.read_parquet(self.out / "output" / "analysis" / "visits.parquet")
        for column in ("race", "rr_initial", "spo2_min_ed", "temp_initial", "ga_weeks", "icu", "vbg_ed", "iv_fluids_ed"):
            self.assertGreater(visits[column].notna().mean(), .5, column)
        self.assertGreater(visits["icu"].astype(bool).sum(), 0)
        self.assertGreater(visits["iv_fluids_ed"].astype(bool).sum(), 0)
        self.assertGreater(visits["vbg_ed"].astype(bool).sum(), 0)
        self.assertTrue((self.out / "output" / "analysis" / "pages" / "admission.txt").is_file())

    def test_python_examples_run(self):
        result = subprocess.run([sys.executable, "python/examples.py"], cwd=self.out, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Admission rate by race", result.stdout)

    def test_contents_lists_every_table_with_its_rows(self):
        text = (self.out / "Contents.md").read_text()
        for name, n in self.rows.items():
            self.assertIn(f"| [{name}](#{name.lower()}) | {n:,} |", text)
        self.assertIn("SYNTHETIC", (self.out / "README.md").read_text())

    def test_dictionary_has_no_comments(self):
        text = (self.out / "DataDictionary.yaml").read_text()
        self.assertFalse(any(line.lstrip().startswith("#") for line in text.splitlines()))
        self.assertIn("MedicationDim", yaml.safe_load(text)["DataDictionary"])

    def test_stats_md_answers_are_what_python_prints(self):
        answers = run_stats(self.out, [sys.executable, "python/stats.py"])
        text = (self.out / "STATS.md").read_text()
        self.assertEqual(len(answers), len(load_questions()))
        for value in answers.values():
            self.assertIn(f"**Answer: {value}**", text)

    @unittest.skipUnless(shutil.which("Rscript"), "R is not installed")
    def test_r_prints_the_same_answers(self):
        python = run_stats(self.out, [sys.executable, "python/stats.py"])
        r = run_stats(self.out, ["Rscript", "R/stats.R"])
        self.assertEqual(r, python)

    @unittest.skipUnless(shutil.which("Rscript"), "R is not installed")
    def test_r_examples_run(self):
        result = subprocess.run(["Rscript", "R/examples.R"], cwd=self.out, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_seed_same_data(self):
        other = Path(tempfile.mkdtemp(prefix="synthetic_rsv_again_"))
        try:
            build(other, patients=500, seed=3)
            for name in ("EDVisits", "EDVitals"):
                pd.testing.assert_frame_equal(self.read(name), pd.read_parquet(other / "data" / "cosmos_parquets" / f"{name}.parquet"))
        finally:
            shutil.rmtree(other, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--patients", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--tdd", action="store_true")
    args = parser.parse_args(argv)
    if args.tdd:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SyntheticTests)
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    rows = build(args.out, args.patients, args.seed)
    print(f"Wrote {args.out}")
    for name, n in rows.items():
        print(f"  {name:<28}{n:>10,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
