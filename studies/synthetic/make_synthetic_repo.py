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
             "medorder": 14, "department": 15, "provider": 16, "other": 99}


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

RACES = [("White", .52), ("Black or African American", .18), ("Asian", .05), ("Other Race", .09),
         ("American Indian or Alaska Native", .01), ("Native Hawaiian or Other Pacific Islander", .005),
         ("*Unspecified", .1), ("Patient Declined", .045)]
ETHNICITIES = [("Not Hispanic or Latino", .68), ("Hispanic or Latino", .22), ("*Unspecified", .07),
               ("Patient Declined", .03)]
FINANCIAL = [("Miscellaneous/Other", .58), ("Medicaid", .37), ("Self-Pay", .025), ("Unknown", .01),
             ("*Not Applicable", .01), ("Medicare", .005)]
LANGUAGES = [("English", .82), ("Spanish", .13), ("Arabic", .01), ("Vietnamese", .01), ("*Unspecified", .03)]
STATES = ["WI", "MN", "IL", "TX", "CA", "NY", "OH", "PA", "FL", "GA", "NC", "WA", "CO", "AZ", "MI"]
RSV_CODES = [("J21.0", .6), ("B97.4", .3), ("J12.1", .07), ("J20.5", .03)]
OTHER_CODES = ["R06.03", "J96.01", "E86.0", "H66.90", "R50.9", "R09.02", "J06.9", "U07.1"]
ED_LABS = [  # LOINC, name, common name, base name, low, high, unit, share of lab draws that include it
    ("6690-2", "WBC", "White Blood Cell Count", "WBC", 5, 20, "10*3/uL", .9),
    ("718-7", "HEMOGLOBIN", "Hemoglobin", "HGB", 9, 15, "g/dL", .9),
    ("777-3", "PLATELET COUNT", "Platelets", "PLT", 150, 500, "10*3/uL", .9),
    ("2951-2", "SODIUM", "Sodium", "NA", 132, 145, "mmol/L", .8),
    ("2823-3", "POTASSIUM", "Potassium", "K", 3.5, 5.8, "mmol/L", .8),
    ("2075-0", "CHLORIDE", "Chloride", "CL", 98, 110, "mmol/L", .8),
    ("2028-9", "CO2", "Carbon Dioxide", "CO2", 15, 28, "mmol/L", .8),
    ("3094-0", "BUN", "Blood Urea Nitrogen", "BUN", 3, 20, "mg/dL", .8),
    ("2160-0", "CREATININE", "Creatinine", "CREAT", .15, .5, "mg/dL", .8),
    ("2345-7", "GLUCOSE", "Glucose", "GLU", 60, 160, "mg/dL", .8),
    ("*Unspecified", "RSV PCR", "RSV by PCR", "RSVPCR", None, None, "", .6),
]
VBG_LABS = [("2746-4", "PH, VENOUS", "pH Venous", "PHVEN", 7.2, 7.42, ""),
            ("2021-4", "PCO2, VENOUS", "pCO2 Venous", "PCO2VEN", 35, 70, "mm[Hg]")]
MEDS = [  # name, generic, simple generic, pharm class, route, dose unit, share of visits (scaled by severity for IV)
    ("ACETAMINOPHEN 160 MG/5 ML ORAL SUSP", "acetaminophen", "Acetaminophen", "ANALGESICS", "Oral", "mg", .45, False),
    ("IBUPROFEN 100 MG/5 ML ORAL SUSP", "ibuprofen", "Ibuprofen", "NSAIDS", "Oral", "mg", .2, False),
    ("ALBUTEROL 2.5 MG/3 ML NEB SOLN", "albuterol sulfate", "Albuterol", "BETA-ADRENERGIC AGENTS", "Inhalation", "mg", .2, False),
    ("SODIUM CHLORIDE 0.9 % IV BOLUS", "sodium chloride 0.9 %", "Sodium Chloride", "IV SOLUTIONS", "Intravenous", "mL/kg", .045, True),
    ("DEXTROSE 5 %-SODIUM CHLORIDE 0.45 % IV SOLP", "dextrose 5 %-sodium chloride 0.45 %", "Dextrose-Sodium Chloride", "IV SOLUTIONS", "Intravenous", "mL/hr", .03, True),
    ("CEFTRIAXONE 50 MG/KG IV", "ceftriaxone", "Ceftriaxone", "CEPHALOSPORINS", "Intravenous", "mg/kg", .04, False),
    ("SODIUM CHLORIDE 3 % INHALATION", "sodium chloride 3 %", "Sodium Chloride", "RESPIRATORY THERAPY", "Inhalation", "mL", .05, False),
]
TEXT_DEFAULTS = {
    "AcuityLevel": ["1 - Immediate", "2 - Emergent", "3 - Urgent", "4 - Less Urgent", "5 - Non-Urgent"],
    "ArrivalMethod": ["Car", "Ambulance", "Walk-in", "*Unspecified"],
    "DerivedEncounterStatus_X": ["Complete"],
    "EncounterType": ["Hospital Encounter"],
    "Country": ["United States of America"],
    "Status": ["Alive"],
    "MaritalStatus": ["Single", "Married", "*Unspecified"],
    "GenderIdentity": ["*Unspecified"],
    "LaborType": ["Spontaneous", "Induced", "*Unspecified"],
    "DeliveryMethod": ["Vaginal, Spontaneous", "C-Section, Low Transverse", "*Unspecified"],
    "LivingStatus": ["Living"],
    "PresentationType": ["Vertex", "Breech", "*Unspecified"],
    "PlacentaMethod": ["Spontaneous", "Manual", "*Unspecified"],
    "Flag": ["", "High", "Low"],
}


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
    """Patients, their visits, and everything that happens on them."""

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
        preterm = rng.random(n) < 0.11
        ga = np.where(preterm, rng.normal(33, 2.5, n), rng.normal(39, 1.1, n)).clip(23, 42)
        ga_days = (ga * 7 + rng.integers(0, 7, n)).astype(int)
        race = pick(rng, RACES, n)
        second = np.where(rng.random(n) < 0.04, pick(rng, RACES[:5], n), None)
        self.patients = pd.DataFrame({
            "n": np.arange(1, n + 1), "DurableKey": key("patient", np.arange(1, n + 1)), "BirthDate": births.normalize(),
            "Sex": rng.choice(["Female", "Male"], n, p=[.45, .55]), "FirstRace": race, "SecondRace": second,
            "MultiRacial": (second != None).astype(int), "Ethnicity": pick(rng, ETHNICITIES, n),  # noqa: E711
            "PreferredLanguage": pick(rng, LANGUAGES, n), "StateOrProvinceAbbreviation": rng.choice(STATES, n),
            "PrimaryRUCA_X": rng.choice(["1", "1", "1", "2", "4", "7", "10"], n),
            "svi": rng.beta(1.3, 1.3, n), "ga_days": ga_days, "born_here": rng.random(n) < 0.73,
            "birth_weight": (ga * 85 - 230 + rng.normal(0, 350, n)).clip(500, 5200).round(),
        })
        self.patients["SexAssignedAtBirth"] = self.patients["Sex"]

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
                    weight *= .15            # the COVID off-season
                if arrival.year == 2021 and month in (6, 7, 8):
                    weight = .8              # its summer surge
                if pd.Timestamp(FIRST_DAY) <= arrival <= pd.Timestamp(LAST_DAY) and rng.random() < weight:
                    break
            else:
                arrival = pd.Timestamp(FIRST_DAY) + pd.Timedelta(days=int(rng.integers(0, 2000)))
            rows.append((index, arrival + pd.Timedelta(minutes=int(rng.integers(0, 1440)))))
        v = pd.DataFrame(rows, columns=["patient_index", "ArrivalInstant"]).sort_values("ArrivalInstant").reset_index(drop=True)
        v["n"] = np.arange(1, n + 1)
        v["EdVisitKey"] = key("visit", v["n"])
        v["EncounterKey"] = key("encounter", v["n"])
        v["PatientDurableKey"] = p["DurableKey"].to_numpy()[v["patient_index"]]
        age_days = (v["ArrivalInstant"].dt.normalize() - p["BirthDate"].to_numpy()[v["patient_index"]]).dt.days
        v["age_days"] = age_days
        ga = p["ga_days"].to_numpy()[v["patient_index"]] / 7
        svi = p["svi"].to_numpy()[v["patient_index"]]
        # Severity: younger, more premature and (a little) higher-SVI children are sicker. Invented, for practice.
        v["severity"] = (1.6 * np.exp(-age_days / 90) + 0.12 * np.clip(37 - ga, 0, None) + 0.4 * svi
                         + rng.normal(0, .7, n))
        v["financial"] = np.where(rng.random(n) < 0.15 + 0.4 * svi, "Medicaid", pick(rng, FINANCIAL, n))
        admit_p = 1 / (1 + np.exp(-(v["severity"] - 1.9) * 1.8))
        v["admitted"] = rng.random(n) < admit_p
        v["icu"] = v["admitted"] & (rng.random(n) < 1 / (1 + np.exp(-(v["severity"] - 2.6) * 2)))
        v["ed_hours"] = rng.uniform(1.5, 7, n)
        v["DepartureInstant"] = v["ArrivalInstant"] + pd.to_timedelta(v["ed_hours"] * 60, unit="m").dt.round("min")
        no_departure = rng.random(n) < .025
        v.loc[no_departure, "DepartureInstant"] = pd.NaT
        v["los_days"] = np.where(v["admitted"], np.clip(rng.gamma(2, 1.3 + v["severity"].clip(0) * .4), 1, 21).round(), 0)
        admitted_n = np.cumsum(v["admitted"])
        v["HospitalAdmissionKey"] = np.where(v["admitted"], key("admission", admitted_n), -1)
        v["AdmissionEncounterKey"] = np.where(v["admitted"], key("encounter", 5_000_000 + admitted_n), -1)
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
        dispo = np.where(v["admitted"], "Admitted as an Inpatient", "Home or Self Care")
        acuity = np.where(v["severity"] > 2.5, "2 - Emergent", np.where(v["severity"] > 1.2, "3 - Urgent", "4 - Less Urgent"))
        return pd.DataFrame({
            "EdVisitKey": v["EdVisitKey"], "EncounterKey": v["EncounterKey"], "PatientDurableKey": v["PatientDurableKey"],
            "ArrivalInstant": v["ArrivalInstant"], "DepartureInstant": v["DepartureInstant"],
            "HospitalAdmissionKey": v["HospitalAdmissionKey"], "FinancialClass": v["financial"],
            "DischargeDisposition": dispo, "EdGenericDispo": np.where(v["admitted"], "Admit", "Discharge"),
            "AcuityLevel": acuity, "Count": 1, "LeftWithoutBeingSeen": 0, "LeftAgainstMedicalAdvice": 0,
            "UnderObservation": (rng.random(n) < .05).astype(int),
        })

    def patients_table(self) -> pd.DataFrame:
        p = self.patients
        out = p[["DurableKey", "BirthDate", "Sex", "SexAssignedAtBirth", "FirstRace", "SecondRace", "MultiRacial",
                 "Ethnicity", "PreferredLanguage", "StateOrProvinceAbbreviation", "PrimaryRUCA_X"]].copy()
        out["BirthDate"] = out["BirthDate"].dt.date
        out["DeathDate"] = None
        rng = self.rng
        out["SviOverallPctlRankByZip2020_X"] = p["svi"].round(4)
        for name in ("SviSocioeconomicPctlRankByZip2020_X", "SviHouseholdCharacteristicsPctlRankByZip2020_X",
                     "SviRacialEthnicMinorityStatusPctlRankByZip2020_X", "SviHousingTypeTransportationPctlRankByZip2020_X"):
            out[name] = (p["svi"] + rng.normal(0, .12, len(p))).clip(0, 1).round(4)
        return out

    def vitals(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        v, rng = self.visits, self.rng
        ed, stay = [], []
        counter = 0
        for i in range(len(v)):
            if rng.random() < .06:
                continue                             # a visit with no vitals charted
            start, end = self.ed_window(i)
            sev, age = v.at[i, "severity"], v.at[i, "age_days"]
            base_rr = 45 - age / 40 + sev * 6
            readings = int(rng.integers(2, 7))
            times = sorted(start + (end - start) * rng.random(readings))
            if rng.random() < .1:
                times = [start - pd.Timedelta(minutes=int(rng.integers(5, 120)))] + times   # before arrival
            for t in times:
                counter += 1
                ed.append(self.vital_row(counter, v.at[i, "EncounterKey"], v.at[i, "PatientDurableKey"], t,
                                         base_rr, sev, "EncounterKey"))
            if v.at[i, "admitted"]:
                t = v.at[i, "InpatientAdmissionInstant"]
                while t < v.at[i, "DischargeInstant"]:
                    counter += 1
                    stay.append(self.vital_row(counter, v.at[i, "AdmissionEncounterKey"], v.at[i, "PatientDurableKey"],
                                               t, base_rr * .9, sev * .8, "InpatientEncounterKey"))
                    t += pd.Timedelta(hours=4)
        return pd.DataFrame(ed), pd.DataFrame(stay)

    def vital_row(self, counter, encounter, patient, when, base_rr, sev, encounter_column):
        rng = self.rng
        when = pd.Timestamp(when)
        rr = int(np.clip(rng.normal(base_rr, 6), 15, 95))
        spo2 = int(np.clip(rng.normal(97.5 - sev * 2.2, 2), 70, 100))
        temp_c = float(np.clip(rng.normal(37.6 + sev * .25, .6), 35.5, 41))
        fahrenheit = rng.random() < .85
        if rng.random() < .003:
            rr = 0                                   # a charting slip the analysis drops
        return {"VitalsKey": key("vitals", counter), encounter_column: encounter, "PatientDurableKey": patient,
                "TakenInstant": when.round("min"), "DateKey": date_key(when),
                "RespirationRate": rr if rng.random() > .05 else None,
                "SpO2": spo2 if rng.random() > .05 else None,
                "Temperature": round(temp_c * 9 / 5 + 32, 1) if fahrenheit else round(temp_c, 1),
                "PulseRate": int(np.clip(rng.normal(150 + sev * 8, 15), 80, 220)),
                "Weight": round(float(np.clip(rng.normal(6.5, 2.2), 2, 16)), 2), "Count": 1,
                "_IsDeleted": 0, "_IsInferred": 0}

    def labs(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        v, rng = self.visits, self.rng
        ed, stay = [], []
        counter = 0

        def draw(rows, encounter, patient, when, sev, vbg_p):
            nonlocal counter
            when = pd.Timestamp(when)
            panel = [lab for lab in ED_LABS if rng.random() < lab[7]]
            if rng.random() < vbg_p:
                panel += [lab + (1.0,) for lab in VBG_LABS]
            for component, lab in enumerate(panel):
                loinc, name, common, base, low, high, unit = lab[:7]
                counter += 1
                if low is None:
                    value, number = ("Detected" if rng.random() < .9 else "Not Detected"), None
                else:
                    number = float(rng.uniform(low, high))
                    if loinc == "2746-4":
                        number = 7.38 - sev * .03 + rng.normal(0, .03)
                    if loinc == "2021-4":
                        number = 40 + sev * 6 + rng.normal(0, 5)
                    number = round(number, 2)
                    value = str(number)
                rows.append({"LabComponentResultKey": key("lab", counter), "EncounterKey": encounter,
                             "PatientDurableKey": patient, "LabComponentKey": key("labcomponent", ED_LAB_INDEX[name]),
                             "CollectionInstant": when.round("min"), "NumericValue": number, "Value": value,
                             "Unit": unit, "Abnormal": int(rng.random() < .15), "Flag": "",
                             "ComponentLoincCode": loinc, "ComponentLoincName": common, "ComponentName": name,
                             "ComponentCommonName": common, "ComponentBaseName": base})

        for i in range(len(v)):
            sev = v.at[i, "severity"]
            start, end = self.ed_window(i)
            if rng.random() < .25 + .15 * min(sev, 3):
                draw(ed, v.at[i, "EncounterKey"], v.at[i, "PatientDurableKey"], start + (end - start) * rng.random(),
                     sev, .05 + .08 * max(sev, 0))
            if v.at[i, "admitted"]:
                t = v.at[i, "InpatientAdmissionInstant"] + pd.Timedelta(hours=6)
                while t < v.at[i, "DischargeInstant"]:
                    draw(stay, v.at[i, "AdmissionEncounterKey"], v.at[i, "PatientDurableKey"], t, sev, .2 if v.at[i, "icu"] else .03)
                    t += pd.Timedelta(hours=24)
        return pd.DataFrame(ed), pd.DataFrame(stay)

    def ed_meds(self) -> pd.DataFrame:
        v, rng = self.visits, self.rng
        rows, counter = [], 0
        for i in range(len(v)):
            sev = max(v.at[i, "severity"], 0)
            start, end = self.ed_window(i)
            for m, med in enumerate(MEDS):
                name, generic, simple, pclass, route, unit, share, fluid = med
                if rng.random() < (share * (1 + 1.5 * sev) if fluid else share):
                    counter += 1
                    rows.append({"MedicationAdministrationKey": key("medadmin", counter), "EncounterKey": v.at[i, "EncounterKey"],
                                 "PatientDurableKey": v.at[i, "PatientDurableKey"],
                                 "AdministrationInstant": pd.Timestamp(start + (end - start) * rng.random()).round("min"),
                                 "AdministrationRoute": route, "AdministrationAction": "Given", "ActionIsMedAdministration": 1,
                                 "MedicationKey": key("medication", m + 1), "MedicationOrderKey": key("medorder", counter),
                                 "Dose": round(float(rng.uniform(5, 20)), 2), "DoseUnit": unit,
                                 "Rate": round(float(rng.uniform(10, 40)), 2) if unit == "mL/hr" else None,
                                 "AdministrationDepartmentKey": key("department", 1),
                                 "MedicationName": name, "MedicationGenericName": generic,
                                 "MedicationSimpleGenericName": simple, "MedicationPharmaceuticalClass": pclass,
                                 "MedicationPharmaceuticalSubclass": pclass, "MedicationTherapeuticClass": pclass,
                                 "MedicationForm": "Solution", "MedicationRoute": route})
        return pd.DataFrame(rows)

    def diagnoses(self) -> pd.DataFrame:
        v, rng = self.visits, self.rng
        rows, counter = [], 0
        for i in range(len(v)):
            codes = [pick(rng, RSV_CODES, 1)[0]]
            if rng.random() < .25:
                codes.append("B97.4" if codes[0] != "B97.4" else "J21.0")
            codes += list(rng.choice(OTHER_CODES, int(rng.integers(0, 3)), replace=False))
            for position, code in enumerate(dict.fromkeys(codes)):
                counter += 1
                rows.append({"DiagnosisEventKey": key("diagnosisevent", counter), "EncounterKey": v.at[i, "EncounterKey"],
                             "PatientDurableKey": v.at[i, "PatientDurableKey"],
                             "DiagnosisKey": key("diagnosis", 1 + sum(map(ord, code)) % 9000),
                             "EmergencyDepartmentDiagnosis": 1, "IsPrimary": int(position == 0),
                             "Type": "Encounter Diagnosis", "BillingCodeValue": code, "CodeType": "ICD-10-CM"})
        return pd.DataFrame(rows)

    def admissions(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        v, rng = self.visits[self.visits["admitted"]], self.rng
        admitted_icu = v["icu"] & (rng.random(len(v)) < .5)
        admit = np.where(admitted_icu, "Pediatric Critical Care Medicine", "Pediatrics")
        discharge = np.where(v["icu"] & ~admitted_icu & (rng.random(len(v)) < .3), "Pediatric Critical Care Medicine", "Pediatrics")
        haf = pd.DataFrame({
            "HospitalAdmissionKey": v["HospitalAdmissionKey"], "EncounterKey": v["AdmissionEncounterKey"],
            "PatientDurableKey": v["PatientDurableKey"], "AdmissionDateKey": v["InpatientAdmissionInstant"].map(date_key),
            "InpatientAdmissionInstant": v["InpatientAdmissionInstant"].dt.round("min"),
            "InpatientAdmissionDateKey": v["InpatientAdmissionInstant"].map(date_key),
            "DischargeInstant": v["DischargeInstant"].dt.round("min"), "DischargeDateKey": v["DischargeInstant"].map(date_key),
            "LengthOfStayInDays": v["los_days"].astype(int), "InpatientLengthOfStayInDays": v["los_days"].astype(int),
            "DepartmentKey": np.where(admit == "Pediatrics", key("department", 2), key("department", 3)),
            "DischargeDepartmentKey_X": np.where(discharge == "Pediatrics", key("department", 2), key("department", 3)),
            "AdmitSpecialty": admit, "DischargeSpecialty": discharge, "FinancialClass": v["financial"],
            "EncounterType": "Hospital Encounter", "DischargeDisposition": "Home or Self Care", "StartedInED_X": 1, "Count": 1,
        })
        rows = []
        for (_, row), icu in zip(v.iterrows(), v["icu"]):
            rows.append({"HospitalAdmissionKey": row["HospitalAdmissionKey"], "AdministrationDepartmentKey": key("department", 2),
                         "DepartmentSpecialty": "Pediatrics", "AdministrationInstant": row["InpatientAdmissionInstant"].round("min")})
            if icu:
                rows.append({"HospitalAdmissionKey": row["HospitalAdmissionKey"], "AdministrationDepartmentKey": key("department", 3),
                             "DepartmentSpecialty": "Pediatric Critical Care Medicine",
                             "AdministrationInstant": (row["InpatientAdmissionInstant"] + pd.Timedelta(hours=int(rng.integers(1, 30)))).round("min")})
        return haf, pd.DataFrame(rows)

    def births(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        p, rng = self.patients[self.patients["born_here"]].reset_index(drop=True), self.rng
        n = len(p)
        mothers = key("patient", 5_000_000 + np.arange(1, n + 1))
        pregnancies = key("pregnancy", np.arange(1, n + 1))
        instant = p["BirthDate"] + pd.to_timedelta(rng.integers(0, 1440, n), unit="m")
        births = pd.DataFrame({
            "BabyPatientDurableKey": p["DurableKey"], "BirthKey": key("birth", np.arange(1, n + 1)), "BirthInstant": instant,
            "BirthDateKey": instant.map(date_key), "GestationalAgeDays": p["ga_days"], "BirthWeightGrams": p["birth_weight"],
            "BirthLength": (p["ga_days"] / 7 * 1.1 + 7).round(1), "MultipleDeliveryCount": 1, "MultipleDeliveryOrder": 1,
            "DeliveryMethod": rng.choice(["Vaginal, Spontaneous", "C-Section, Low Transverse"], n, p=[.68, .32]),
            "TotalApgarFiveMinute": rng.integers(6, 10, n), "BabyInpatientLengthOfStayInDays": np.where(p["ga_days"] < 245, rng.integers(5, 60, n), rng.integers(1, 4, n)),
            "NeonatalDemise": 0, "MotherPatientDurableKey": mothers, "PregnancyKey": pregnancies,
        })
        mother = pd.DataFrame({"DurableKey": mothers,
                               "BirthDate": (p["BirthDate"] - pd.to_timedelta(rng.normal(29, 5, n).clip(16, 45) * 365.25, unit="D")).dt.date,
                               "Sex": "Female", "SexAssignedAtBirth": "Female", "FirstRace": p["FirstRace"],
                               "Ethnicity": p["Ethnicity"], "PreferredLanguage": p["PreferredLanguage"],
                               "StateOrProvinceAbbreviation": p["StateOrProvinceAbbreviation"], "IsCurrent": 1, "IsValid": 1,
                               "UseInCosmosAnalytics_X": 1, "SviOverallPctlRankByZip2020_X": p["svi"].round(4)})
        pregnancy = pd.DataFrame({"PregnancyKey": pregnancies, "PatientDurableKey": mothers, "NumberOfFetuses": 1,
                                  "HasDelivery": 1, "LastDeliveryGestationalAge": (p["ga_days"] // 7).astype(str) + "w",
                                  "LastDeliveryDateKey": instant.map(date_key), "PregnancyGravidaCount": rng.integers(1, 5, n),
                                  "PregnancyParaCount": rng.integers(0, 4, n), "Count": 1})
        return births, mother, pregnancy

    def tables(self) -> dict[str, pd.DataFrame]:
        ed_vitals, stay_vitals = self.vitals()
        ed_labs, stay_labs = self.labs()
        haf, stay_departments = self.admissions()
        births, mother, pregnancy = self.births()
        return {"EDVisits": self.ed_visits(), "Patients": self.patients_table(), "EDVitals": ed_vitals,
                "EDLabs": ed_labs, "EDDiagnoses": self.diagnoses(), "EDMeds": self.ed_meds(),
                "StayDepartments": stay_departments, "HospitalAdmissionFact": haf, "InpatientVitals": stay_vitals,
                "InpatientLabs": stay_labs, "Births": births, "MotherPatientInfo": mother, "PregnancyFact": pregnancy}


ED_LAB_INDEX = {lab[1]: i + 1 for i, lab in enumerate(ED_LABS + [v + (1.0,) for v in VBG_LABS])}


# ---------------------------------------------------------------- every column of the real table

ANCHORS = ["ArrivalInstant", "TakenInstant", "CollectionInstant", "AdministrationInstant", "InpatientAdmissionInstant",
           "BirthInstant"]


def complete(frame: pd.DataFrame, columns: list[tuple[str, str]], rng: np.random.Generator) -> pd.DataFrame:
    """Every column the real table has, in its order: the generated ones as made, the rest filled by name and type."""
    n = len(frame)
    anchor = next((frame[a] for a in ANCHORS if a in frame), None)
    out = {}
    for name, sql in columns:
        if name in frame:
            out[name] = frame[name]
            continue
        base = sql.split("(")[0].upper()
        if name in ("_IsDeleted", "_IsInferred"):
            out[name] = 0
        elif name == "Count":
            out[name] = 1
        elif base.startswith("DATETIME") or base == "SMALLDATETIME":
            out[name] = ((anchor + pd.to_timedelta(rng.integers(5, 240, n), unit="m")).where(rng.random(n) < .8)
                         if anchor is not None else pd.Series(pd.NaT, index=frame.index))
        elif name.endswith("TimeOfDayKey"):
            partner = frame.get(name.replace("TimeOfDayKey", "Instant"), anchor)
            out[name] = partner.map(time_key) if partner is not None else None
        elif name.endswith("DateKey"):
            partner = out.get(name.replace("DateKey", "Instant"), frame.get(name.replace("DateKey", "Instant"), anchor))
            out[name] = partner.map(date_key) if partner is not None else None
        elif base == "BIGINT" and name.endswith("Key"):
            out[name] = key("other", rng.integers(1, 9_000_000, n))
        elif base in ("BIT", "TINYINT") or name.startswith(("Is", "Has")):
            out[name] = (rng.random(n) < .1).astype(int)
        elif base in ("INT", "SMALLINT", "BIGINT"):
            out[name] = rng.integers(0, 5, n)
        elif base in ("FLOAT", "REAL", "NUMERIC", "DECIMAL"):
            out[name] = np.where(rng.random(n) < .7, rng.uniform(0, 10, n).round(2), np.nan)
        elif base == "DATE":
            out[name] = None
        elif name in TEXT_DEFAULTS:
            out[name] = rng.choice(TEXT_DEFAULTS[name], n)
        else:
            out[name] = np.where(rng.random(n) < .5, "*Unspecified", None)
    result = pd.DataFrame(out, index=frame.index)
    return result.reset_index(drop=True)


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
            ("icu_pct_of_admissions", "Admissions that included the ICU"),
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
