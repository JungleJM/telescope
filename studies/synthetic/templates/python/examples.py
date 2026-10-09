"""Worked examples on the synthetic tables. Run from the repository's top folder:

    python python/examples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_parquets import load  # noqa: E402

tables = load()
visits = tables["EDVisits"]
patients = tables["Patients"].rename(columns={"DurableKey": "PatientDurableKey"})

# 1. Each visit with its patient, and the child's age at arrival in days.
v = visits.merge(patients, on="PatientDurableKey", how="left")
v["age_days"] = (v["ArrivalInstant"].dt.normalize() - pd.to_datetime(v["BirthDate"])).dt.days
v["admitted"] = v["HospitalAdmissionKey"] > 0
print(f"{len(v):,} visits by {v['PatientDurableKey'].nunique():,} children; {v['admitted'].mean():.1%} admitted\n")

# 2. Admission rate by race.
print("Admission rate by race")
print(v.groupby("FirstRace")["admitted"].agg(visits="size", admitted="sum", rate="mean")
       .sort_values("visits", ascending=False).round(3).to_string(), "\n")

# 3. The lowest SpO2 in the ED, per visit: readings between arrival and departure.
vitals = tables["EDVitals"].merge(visits[["EncounterKey", "EdVisitKey", "ArrivalInstant", "DepartureInstant"]],
                                  on="EncounterKey")
in_ed = vitals["TakenInstant"].between(vitals["ArrivalInstant"], vitals["DepartureInstant"].fillna(vitals["ArrivalInstant"] + pd.Timedelta(hours=6)))
lowest = vitals[in_ed].groupby("EdVisitKey")["SpO2"].min().rename("spo2_min_ed")
v = v.merge(lowest, on="EdVisitKey", how="left")
print("Lowest ED SpO2, admitted against not")
print(v.groupby("admitted")["spo2_min_ed"].describe()[["count", "25%", "50%", "75%"]].round(1).to_string(), "\n")

# 4. Temperatures: Cosmos mixes °F and °C. Above 45 is °F.
t = pd.to_numeric(tables["EDVitals"]["Temperature"].astype(float))
celsius = t.where(t <= 45, (t - 32) * 5 / 9)
print(f"Temperatures read as °F: {(t > 45).mean():.0%}; median in °C {celsius.median():.1f}\n")

# 5. Where admissions were admitted to, and ICU stays among them.
haf = tables["HospitalAdmissionFact"]
print("Admitted to")
print(haf["AdmitSpecialty"].value_counts().to_string(), "\n")
icu = set(haf.loc[haf["AdmitSpecialty"].isin(["Pediatric Intensive Care", "Critical Care Medicine"]), "HospitalAdmissionKey"])
v["icu"] = v["HospitalAdmissionKey"].isin(icu)
print(f"ICU: {v['icu'].sum():,} visits, {v['icu'].sum() / max(v['admitted'].sum(), 1):.1%} of admissions\n")

# 6. IV fluids in the ED, by medication name and route.
meds = tables["EDMeds"]
fluids = meds[meds["AdministrationRoute"].eq("Intravenous") &
              meds["MedicationName"].str.contains("SODIUM CHLORIDE 0.9|DEXTROSE", regex=True, na=False)]
print(f"Visits with IV fluids: {fluids['EncounterKey'].nunique():,}")
