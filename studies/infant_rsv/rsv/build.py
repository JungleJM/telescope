"""`python rsv build`: line every table up on the ED visit, and write the analysis parquets."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import RsvError, Settings, Source, find_source
from .page import Page, data_version, fit

# The columns read from each table, when it has them; `REQUIRED` must be there.
WANT: dict[str, list[str]] = {
    "visits": ["EdVisitKey", "EncounterKey", "PatientDurableKey", "ArrivalInstant", "DepartureInstant",
               "HospitalAdmissionKey", "FinancialClass", "DischargeDisposition", "EdGenericDispo",
               "AcuityLevel", "ArrivalMethod"],
    "patients": ["DurableKey", "BirthDate", "Sex", "FirstRace", "SecondRace", "MultiRacial", "Ethnicity",
                 "PreferredLanguage", "StateOrProvinceAbbreviation", "PrimaryRUCA_X"],
    "births": ["BabyPatientDurableKey", "BirthKey", "BirthInstant", "GestationalAgeDays", "BirthWeightGrams"],
    "admissions": ["HospitalAdmissionKey", "EncounterKey", "InpatientAdmissionInstant", "DischargeInstant",
                   "LengthOfStayInDays", "AdmitSpecialty", "DischargeSpecialty"],
    "stay_departments": ["HospitalAdmissionKey", "DepartmentSpecialty"],
    "ed_vitals": ["VitalsKey", "EncounterKey", "TakenInstant", "RespirationRate", "SpO2", "Temperature"],
    "stay_vitals": ["VitalsKey", "InpatientEncounterKey", "TakenInstant", "RespirationRate", "SpO2", "Temperature"],
    "ed_labs": ["LabComponentResultKey", "EncounterKey", "LabComponentKey", "ComponentLoincCode", "ComponentName",
                "ComponentCommonName", "CollectionInstant", "NumericValue", "Value", "Unit"],
    "ed_meds": ["MedicationAdministrationKey", "EncounterKey", "AdministrationInstant", "AdministrationRoute",
                "AdministrationAction", "ActionIsMedAdministration", "MedicationKey", "MedicationName",
                "MedicationGenericName", "Dose", "DoseUnit", "Rate"],
    "diagnoses": ["EncounterKey", "BillingCodeValue"],
}
WANT["stay_labs"] = WANT["ed_labs"]
REQUIRED: dict[str, list[str]] = {
    "visits": ["EdVisitKey", "EncounterKey", "PatientDurableKey", "ArrivalInstant", "DepartureInstant",
               "HospitalAdmissionKey"],
    "patients": ["DurableKey"],
    "births": ["BabyPatientDurableKey"],
    "admissions": ["HospitalAdmissionKey", "EncounterKey"],
    "stay_departments": ["HospitalAdmissionKey", "DepartmentSpecialty"],
    "ed_vitals": ["EncounterKey", "TakenInstant"],
    "stay_vitals": ["InpatientEncounterKey", "TakenInstant"],
    "ed_labs": ["EncounterKey", "CollectionInstant"],
    "stay_labs": ["EncounterKey", "CollectionInstant"],
    "ed_meds": ["EncounterKey", "AdministrationInstant"],
    "diagnoses": ["EncounterKey", "BillingCodeValue"],
}
VITALS = {"RespirationRate": "rr", "SpO2": "spo2", "Temperature": "temp_c"}
PHASES = ("before", "ED", "stay", "after")
VISITS_FILE, VITALS_FILE, LABS_FILE, MEDS_FILE = "visits.parquet", "vitals.parquet", "labs.parquet", "meds.parquet"


@dataclass
class Built:
    """What `build` made, and what the build page reports."""
    visits: pd.DataFrame
    vitals: pd.DataFrame
    labs: pd.DataFrame
    meds: pd.DataFrame
    sources: dict[str, Source]
    notes: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- reading

def read_table(settings: Settings, table: str, extra: list[str] | None = None) -> tuple[pd.DataFrame | None, Source]:
    import pyarrow.parquet as pq
    source = find_source(settings, table)
    if not source.found:
        return None, source
    have = set(pq.ParquetFile(source.path).schema_arrow.names)
    missing = [c for c in REQUIRED.get(table, []) if c not in have]
    if missing:
        raise RsvError(f"{source.path} lacks {', '.join(missing)}, which `{table}` needs. "
                       f"Point `sources: {table}` in settings.yaml at a file that has them.")
    wanted = [c for c in WANT.get(table, []) + (extra or []) if c in have]
    frame = pq.read_table(source.path, columns=list(dict.fromkeys(wanted))).to_pandas()
    return frame, source


def times(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def text(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip()


# ---------------------------------------------------------------- groupings

def grouped(series: pd.Series, mapping: dict[str, str], unknown: set[str]) -> pd.Series:
    values = text(series).fillna("")
    out = values.map(lambda v: "Unknown" if v.lower() in unknown else mapping.get(v, v))
    return out.astype("string")


def season_of(when: pd.Series, start_month: int) -> pd.Series:
    year = when.dt.year - (when.dt.month < start_month).astype("Int64")
    label = year.astype("string") + "-" + ((year + 1) % 100).astype("string").str.zfill(2)
    return label.where(when.notna())


def era_of(season: pd.Series, eras: list[dict[str, str]]) -> pd.Series:
    out = pd.Series(pd.NA, index=season.index, dtype="string")
    for era in eras:
        inside = (season >= str(era["from"])) & (season <= str(era["to"]))
        out = out.mask(inside.fillna(False) & out.isna(), era["name"])
    return out.where(season.isna(), out.fillna("other"))


def banded(values: pd.Series, bands: dict[str, float], upper: float) -> pd.Series:
    names = list(bands)
    edges = [float(bands[n]) for n in names] + [float(upper)]
    cut = pd.cut(values.astype("float"), bins=edges, labels=names, right=False)
    return cut.astype("string")


def svi_quartile(values: pd.Series) -> pd.Series:
    numbers = pd.to_numeric(values, errors="coerce")
    scale = 100.0 if numbers.max(skipna=True) > 1.5 else 1.0
    quartile = pd.cut(numbers / scale, bins=[-0.001, 0.25, 0.5, 0.75, 1.0001], labels=["Q1", "Q2", "Q3", "Q4"])
    return quartile.astype("string").fillna("Unknown")


def matches(series: pd.Series, patterns: list[str]) -> pd.Series:
    if not patterns:
        return pd.Series(False, index=series.index)
    joined = "|".join(f"(?:{p})" for p in patterns)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="This pattern is interpreted as a regular expression")
        return text(series).fillna("").str.contains(joined, flags=re.IGNORECASE, regex=True)


# ---------------------------------------------------------------- the build

def link_admissions(visits: pd.DataFrame, admissions: pd.DataFrame | None) -> pd.DataFrame:
    """Each visit's admission, if it has one: `admitted`, `stay_end`, length of stay."""
    visits["admitted"] = pd.to_numeric(visits["HospitalAdmissionKey"], errors="coerce").fillna(0) > 0
    visits["stay_end"] = visits["DepartureInstant"]
    visits["admission_found"] = False
    if admissions is None:
        return visits
    adm = admissions.drop_duplicates("HospitalAdmissionKey").copy()
    adm["DischargeInstant"] = times(adm.get("DischargeInstant", pd.Series(pd.NaT, index=adm.index)))
    keep = ["HospitalAdmissionKey"] + [c for c in ("DischargeInstant", "LengthOfStayInDays", "InpatientAdmissionInstant")
                                       if c in adm]
    merged = visits.merge(adm[keep], on="HospitalAdmissionKey", how="left", indicator=True)
    merged["admission_found"] = (merged.pop("_merge") == "both") & merged["admitted"]
    later = merged["admitted"] & merged["DischargeInstant"].notna() & (merged["DischargeInstant"] > merged["DepartureInstant"])
    merged.loc[later, "stay_end"] = merged.loc[later, "DischargeInstant"]
    if "LengthOfStayInDays" in merged:
        merged = merged.rename(columns={"LengthOfStayInDays": "los_days"})
    return merged


def icu_flags(visits: pd.DataFrame, admissions: pd.DataFrame | None, departments: pd.DataFrame | None,
              icu: list[str]) -> tuple[pd.Series, pd.Series | None]:
    """ICU per visit, and every specialty seen, or (NA, None) when no specialties were pulled."""
    pairs = []
    if admissions is not None:
        for column in ("AdmitSpecialty", "DischargeSpecialty"):
            if column in admissions and admissions[column].notna().any():
                pairs.append(admissions[["HospitalAdmissionKey", column]].rename(columns={column: "specialty"}))
    if departments is not None and departments["DepartmentSpecialty"].notna().any():
        pairs.append(departments[["HospitalAdmissionKey", "DepartmentSpecialty"]].rename(
            columns={"DepartmentSpecialty": "specialty"}))
    if not pairs:
        return pd.Series(pd.NA, index=visits.index, dtype="boolean"), None
    seen = pd.concat(pairs, ignore_index=True).dropna()
    seen["specialty"] = text(seen["specialty"])
    wanted = {s.lower() for s in icu}
    seen["is_icu"] = seen["specialty"].str.lower().isin(wanted)
    icu_keys = set(seen.loc[seen["is_icu"], "HospitalAdmissionKey"])
    flags = visits["admitted"] & visits["HospitalAdmissionKey"].isin(icu_keys)
    per_admission = seen.drop_duplicates(["HospitalAdmissionKey", "specialty"])
    return flags.astype("boolean"), per_admission["specialty"]


def phase_of(when: pd.Series, arrival: pd.Series, departure: pd.Series, stay_end: pd.Series) -> pd.Series:
    choice = np.select([when < arrival, when <= departure, when <= stay_end], ["before", "ED", "stay"], "after")
    return pd.Series(choice, index=when.index, dtype="string")


def attach(frame: pd.DataFrame, links: pd.DataFrame, on: str, when: str) -> pd.DataFrame:
    """Rows joined to their visits by `on`, with minutes from arrival and the phase."""
    joined = frame.merge(links, on=on, how="inner")
    joined[when] = times(joined[when])
    joined["minutes"] = (joined[when] - joined["ArrivalInstant"]).dt.total_seconds() / 60.0
    joined["phase"] = phase_of(joined[when], joined["ArrivalInstant"], joined["DepartureInstant"], joined["stay_end"])
    return joined


def visit_links(visits: pd.DataFrame) -> pd.DataFrame:
    return visits[["EdVisitKey", "EncounterKey", "ArrivalInstant", "DepartureInstant", "stay_end"]]


def stay_links(visits: pd.DataFrame, admissions: pd.DataFrame | None, column: str) -> pd.DataFrame | None:
    """An admission's encounter, named `column`, to the visits it came from."""
    if admissions is None:
        return None
    pairs = admissions[["HospitalAdmissionKey", "EncounterKey"]].drop_duplicates()
    links = visits.loc[visits["admitted"], ["EdVisitKey", "HospitalAdmissionKey", "ArrivalInstant",
                                            "DepartureInstant", "stay_end"]]
    return links.merge(pairs, on="HospitalAdmissionKey").drop(columns="HospitalAdmissionKey").rename(
        columns={"EncounterKey": column})


def clean_vitals(vitals: pd.DataFrame, settings: Settings, notes: dict[str, Any]) -> pd.DataFrame:
    for raw, name in VITALS.items():
        vitals[name] = pd.to_numeric(vitals[raw], errors="coerce") if raw in vitals else np.nan
    above = float(settings["temperature_fahrenheit_above"])
    fahrenheit = vitals["temp_c"] > above
    notes["temps_converted"] = int(fahrenheit.sum())
    vitals.loc[fahrenheit, "temp_c"] = (vitals.loc[fahrenheit, "temp_c"] - 32.0) * 5.0 / 9.0
    dropped = {}
    for name, (low, high) in settings["plausible"].items():
        outside = vitals[name].notna() & ~vitals[name].between(float(low), float(high))
        dropped[name] = int(outside.sum())
        vitals.loc[outside, name] = np.nan
    notes["vitals_dropped"] = dropped
    vitals = vitals[vitals[list(VITALS.values())].notna().any(axis=1)]
    return vitals[["EdVisitKey", "VitalsKey", "TakenInstant", "minutes", "phase", "rr", "spo2", "temp_c"]]


def vital_metrics(vitals: pd.DataFrame) -> pd.DataFrame:
    ed = vitals[vitals["phase"] == "ED"].sort_values("TakenInstant")
    stay = vitals[vitals["phase"].isin(["ED", "stay"])]
    out = pd.DataFrame(index=pd.Index([], name="EdVisitKey"))
    parts = {
        "rr_initial": ed.dropna(subset=["rr"]).groupby("EdVisitKey")["rr"].first(),
        "rr_max_ed": ed.groupby("EdVisitKey")["rr"].max(),
        "rr_max_stay": stay.groupby("EdVisitKey")["rr"].max(),
        "spo2_min_ed": ed.groupby("EdVisitKey")["spo2"].min(),
        "spo2_min_stay": stay.groupby("EdVisitKey")["spo2"].min(),
        "temp_initial": ed.dropna(subset=["temp_c"]).groupby("EdVisitKey")["temp_c"].first(),
        "temp_max_ed": ed.groupby("EdVisitKey")["temp_c"].max(),
        "temp_max_stay": stay.groupby("EdVisitKey")["temp_c"].max(),
        "n_vitals_ed": ed.groupby("EdVisitKey").size(),
    }
    for name, series in parts.items():
        out = out.join(series.rename(name), how="outer")
    return out


def build(settings: Settings, write: bool = True) -> Built:
    notes: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    unknown = {str(u).lower() for u in settings["unknown"]}

    def read(table: str, extra: list[str] | None = None) -> pd.DataFrame | None:
        frame, sources[table] = read_table(settings, table, extra)
        return frame

    visits = read("visits")
    if visits is None:
        tried = "; ".join(sources["visits"].tried)
        raise RsvError(f"No visits table with rows ({tried}). Check `pull_folder` in settings.yaml.")
    notes["visit_rows"] = len(visits)
    visits = visits.drop_duplicates("EdVisitKey").reset_index(drop=True)
    notes["visit_duplicates"] = notes["visit_rows"] - len(visits)
    visits["ArrivalInstant"] = times(visits["ArrivalInstant"])
    visits["DepartureInstant"] = times(visits["DepartureInstant"])
    late = visits["DepartureInstant"].isna() | (visits["DepartureInstant"] < visits["ArrivalInstant"])
    notes["no_departure"] = int(late.sum())
    hours = pd.Timedelta(hours=float(settings["no_departure_hours"]))
    visits.loc[late, "DepartureInstant"] = visits.loc[late, "ArrivalInstant"] + hours

    # Patients: demographics, and the birth date.
    svi = settings["svi_column"]
    patients = read("patients", [svi])
    if patients is not None:
        patients = patients.drop_duplicates("DurableKey").rename(columns={"DurableKey": "PatientDurableKey"})
        visits = visits.merge(patients, on="PatientDurableKey", how="left", indicator="patient_found")
        visits["patient_found"] = visits["patient_found"] == "both"
        multiple = visits.get("MultiRacial", pd.Series(pd.NA, index=visits.index)).astype("string").str.lower().isin(
            ["1", "true", "y", "yes"])
        second = grouped(visits.get("SecondRace", pd.Series(pd.NA, index=visits.index)), {}, unknown)
        multiple |= second.ne("Unknown").fillna(False)
        race = grouped(visits.get("FirstRace", pd.Series(pd.NA, index=visits.index)), settings["race_map"], unknown)
        visits["race"] = race.mask(multiple & visits["patient_found"], "Multiple").where(visits["patient_found"])
        for column, name, mapping in (("Ethnicity", "ethnicity", settings["ethnicity_map"]), ("Sex", "sex", {})):
            raw = visits.get(column, pd.Series(pd.NA, index=visits.index))
            visits[name] = grouped(raw, mapping, unknown).where(visits["patient_found"])
        visits["svi"] = pd.to_numeric(visits.get(svi), errors="coerce") if svi in visits else np.nan
        visits["svi_quartile"] = svi_quartile(visits["svi"]).where(visits["patient_found"]) \
            if visits["svi"].notna().any() else pd.Series(pd.NA, index=visits.index, dtype="string")
        visits["birth_date"] = times(visits.get("BirthDate", pd.Series(pd.NaT, index=visits.index)))
    else:
        visits["patient_found"] = False
        for name in ("race", "ethnicity", "sex", "svi_quartile"):
            visits[name] = pd.Series(pd.NA, index=visits.index, dtype="string")
        visits["svi"] = np.nan
        visits["birth_date"] = pd.NaT
    visits["birth_date_source"] = pd.Series(np.where(visits["birth_date"].notna(), "patient", None),
                                            index=visits.index, dtype="string")

    # Births: gestational age and birth weight; a birth date where the patient has none.
    births = read("births")
    visits["ga_weeks"] = np.nan
    visits["birth_weight_g"] = np.nan
    if births is not None:
        births = births.copy()
        births["ga_days"] = pd.to_numeric(births.get("GestationalAgeDays"), errors="coerce") \
            if "GestationalAgeDays" in births else np.nan
        births["BirthInstant"] = times(births.get("BirthInstant", pd.Series(pd.NaT, index=births.index)))
        births = births.sort_values(["ga_days", "BirthInstant"], na_position="last").drop_duplicates("BabyPatientDurableKey")
        one = births.set_index("BabyPatientDurableKey")
        key = visits["PatientDurableKey"]
        weeks = key.map(one["ga_days"]) // 7
        notes["ga_implausible"] = int((weeks.notna() & ~weeks.between(22, 44)).sum())
        visits["ga_weeks"] = weeks.where(weeks.between(22, 44))
        if "BirthWeightGrams" in one:
            visits["birth_weight_g"] = pd.to_numeric(key.map(one["BirthWeightGrams"]), errors="coerce")
        fallback = key.map(one["BirthInstant"].dt.normalize())
        use = visits["birth_date"].isna() & fallback.notna()
        visits.loc[use, "birth_date"] = fallback[use]
        visits.loc[use, "birth_date_source"] = "birth"
    visits["ga_band"] = banded(visits["ga_weeks"], settings["ga_bands"], 60).fillna("Unknown") \
        if visits["ga_weeks"].notna().any() else pd.Series(pd.NA, index=visits.index, dtype="string")

    age = (visits["ArrivalInstant"].dt.normalize() - visits["birth_date"]).dt.days
    notes["age_outside"] = int((age.notna() & ~age.between(0, int(settings["age_max_days"]) - 1)).sum())
    visits["age_days"] = age.where(age.between(0, int(settings["age_max_days"]) - 1))
    visits["age_months"] = visits["age_days"] / 30.4375
    visits["age_band"] = banded(visits["age_days"], settings["age_bands"], settings["age_max_days"])

    # Admissions, and the ICU.
    admissions = read("admissions")
    visits = link_admissions(visits, admissions)
    departments = read("stay_departments")
    visits["icu"], specialties = icu_flags(visits, admissions, departments, settings["icu_specialties"])
    notes["specialties"] = specialties

    # Diagnoses on the visit's encounter.
    diagnoses = read("diagnoses")
    visits["dx_group"] = pd.Series(pd.NA, index=visits.index, dtype="string")
    if diagnoses is not None:
        codes = diagnoses.assign(code=text(diagnoses["BillingCodeValue"]).str.upper())
        by_encounter = codes.groupby("EncounterKey")["code"].agg(lambda s: set(s.dropna()))
        found = visits["EncounterKey"].map(by_encounter)
        group = pd.Series(pd.NA, index=visits.index, dtype="string")
        for entry in settings["dx_groups"]:
            wanted = {str(c).upper() for c in entry["codes"]}
            has = found.map(lambda s, w=wanted: bool(s & w) if isinstance(s, set) else False)
            group = group.mask(group.isna() & has, entry["name"])
        visits["dx_group"] = group.fillna("Unknown")

    # Season, era, first visit.
    visits["year"] = visits["ArrivalInstant"].dt.year
    visits["season"] = season_of(visits["ArrivalInstant"], int(settings["season_start_month"]))
    visits["era"] = era_of(visits["season"], settings["eras"])
    order = visits.sort_values(["PatientDurableKey", "ArrivalInstant", "EdVisitKey"])
    visits["first_visit"] = ~order["PatientDurableKey"].duplicated().reindex(visits.index)
    visits["financial_class"] = grouped(visits.get("FinancialClass", pd.Series(pd.NA, index=visits.index)),
                                        settings["financial_class_map"], unknown)

    # Vitals: the ED's, and the stay's through the admission's encounter.
    links = visit_links(visits)
    frames = []
    ed_vitals = read("ed_vitals")
    if ed_vitals is not None:
        frames.append(attach(ed_vitals, links, "EncounterKey", "TakenInstant"))
    stay_vitals = read("stay_vitals")
    stay_v = stay_links(visits, admissions, "InpatientEncounterKey")
    if stay_vitals is not None and stay_v is not None:
        frames.append(attach(stay_vitals, stay_v, "InpatientEncounterKey", "TakenInstant"))
    if frames:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            vitals = pd.concat(frames, ignore_index=True)
        if "VitalsKey" not in vitals:
            vitals["VitalsKey"] = pd.NA
        vitals = vitals.drop_duplicates(["EdVisitKey", "VitalsKey", "TakenInstant"])
        vitals = clean_vitals(vitals, settings, notes)
        metrics = vital_metrics(vitals)
        visits = visits.merge(metrics, left_on="EdVisitKey", right_index=True, how="left")
    else:
        vitals = pd.DataFrame(columns=["EdVisitKey", "VitalsKey", "TakenInstant", "minutes", "phase", "rr", "spo2", "temp_c"])
        for name in ("rr_initial", "rr_max_ed", "rr_max_stay", "spo2_min_ed", "spo2_min_stay", "temp_initial",
                     "temp_max_ed", "temp_max_stay", "n_vitals_ed"):
            visits[name] = np.nan
    visits["n_vitals_ed"] = visits["n_vitals_ed"].fillna(0) if frames else visits["n_vitals_ed"]

    # Labs, and the VBG.
    frames = []
    ed_labs = read("ed_labs")
    if ed_labs is not None:
        frames.append(attach(ed_labs, links, "EncounterKey", "CollectionInstant"))
    stay_labs = read("stay_labs")
    stay_l = stay_links(visits, admissions, "EncounterKey")
    if stay_labs is not None and stay_l is not None:
        frames.append(attach(stay_labs, stay_l, "EncounterKey", "CollectionInstant"))
    lab_columns = ["EdVisitKey", "LabComponentResultKey", "LabComponentKey", "ComponentLoincCode", "ComponentName",
                   "ComponentCommonName", "CollectionInstant", "minutes", "phase", "NumericValue", "Value", "Unit"]
    if frames:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            labs = pd.concat(frames, ignore_index=True)
        for column in lab_columns:
            if column not in labs:
                labs[column] = pd.NA
        labs = labs.drop_duplicates(["EdVisitKey", "LabComponentResultKey", "CollectionInstant"])[lab_columns]
        loinc = text(labs["ComponentLoincCode"]).isin([str(c) for c in settings["vbg_loinc"]]).fillna(False)
        named = matches(labs["ComponentName"], settings["vbg_name_patterns"]) | \
            matches(labs["ComponentCommonName"], settings["vbg_name_patterns"])
        labs["vbg"] = loinc | named
        names_known = labs["ComponentName"].notna().any() or labs["ComponentCommonName"].notna().any()
        notes["vbg_by"] = "LOINC and name" if names_known else "LOINC only"
        vbg_visits = set(labs.loc[labs["vbg"] & (labs["phase"] == "ED"), "EdVisitKey"])
        visits["vbg_ed"] = visits["EdVisitKey"].isin(vbg_visits).astype("boolean")
        ed_rows = labs[labs["phase"] == "ED"]
        visits["n_labs_ed"] = visits["EdVisitKey"].map(ed_rows.groupby("EdVisitKey").size()).fillna(0)
    else:
        labs = pd.DataFrame(columns=lab_columns + ["vbg"])
        visits["vbg_ed"] = pd.Series(pd.NA, index=visits.index, dtype="boolean")
        visits["n_labs_ed"] = np.nan
        notes["vbg_by"] = "no labs"

    # Medications, and IV fluids.
    ed_meds = read("ed_meds")
    visits["iv_fluids_ed"] = pd.Series(pd.NA, index=visits.index, dtype="boolean")
    notes["iv_by"] = "no medications pulled"
    if ed_meds is not None:
        meds = attach(ed_meds, links, "EncounterKey", "AdministrationInstant")
        name_columns = [c for c in ("MedicationName", "MedicationGenericName") if c in meds and meds[c].notna().any()]
        route = matches(meds.get("AdministrationRoute", pd.Series(pd.NA, index=meds.index)), settings["iv_routes"])
        given = pd.Series(True, index=meds.index)
        if "ActionIsMedAdministration" in meds:
            given &= pd.to_numeric(meds["ActionIsMedAdministration"], errors="coerce").fillna(1) == 1
        if settings["iv_given_actions"] and "AdministrationAction" in meds:
            given &= text(meds["AdministrationAction"]).isin([str(a) for a in settings["iv_given_actions"]]).fillna(False)
        if name_columns:
            fluid = pd.Series(False, index=meds.index)
            for column in name_columns:
                fluid |= matches(meds[column], settings["iv_fluid_patterns"])
            meds["iv_fluid"] = route & given & fluid
            fluid_visits = set(meds.loc[meds["iv_fluid"] & (meds["phase"] == "ED"), "EdVisitKey"])
            visits["iv_fluids_ed"] = visits["EdVisitKey"].isin(fluid_visits).astype("boolean")
            notes["iv_by"] = "route and name"
        else:
            meds["iv_fluid"] = pd.NA
            notes["iv_by"] = "no medication names"
        meds = meds.drop(columns=["ArrivalInstant", "DepartureInstant", "stay_end", "EncounterKey"], errors="ignore")
    else:
        meds = pd.DataFrame(columns=["EdVisitKey", "AdministrationInstant", "minutes", "phase", "iv_fluid"])

    keep = ["EdVisitKey", "EncounterKey", "PatientDurableKey", "HospitalAdmissionKey", "ArrivalInstant",
            "DepartureInstant", "stay_end", "year", "season", "era", "first_visit", "age_days", "age_months",
            "age_band", "birth_date_source", "ga_weeks", "ga_band", "birth_weight_g", "sex", "race", "ethnicity",
            "svi", "svi_quartile", "financial_class", "dx_group", "admitted", "admission_found", "los_days", "icu",
            "rr_initial", "rr_max_ed", "rr_max_stay", "spo2_min_ed", "spo2_min_stay", "temp_initial",
            "temp_max_ed", "temp_max_stay", "n_vitals_ed", "n_labs_ed", "vbg_ed", "iv_fluids_ed",
            "DischargeDisposition", "EdGenericDispo", "AcuityLevel", "ArrivalMethod", "patient_found"]
    for column in keep:
        if column not in visits:
            visits[column] = pd.NA
    visits = visits[keep].sort_values(["ArrivalInstant", "EdVisitKey"]).reset_index(drop=True)
    built = Built(visits, vitals.reset_index(drop=True), labs.reset_index(drop=True), meds.reset_index(drop=True),
                  sources, notes)
    if write:
        write_parquets(settings, built)
    return built


def write_parquets(settings: Settings, built: Built) -> Path:
    folder = settings.analysis_folder
    folder.mkdir(parents=True, exist_ok=True)
    for name, frame in ((VISITS_FILE, built.visits), (VITALS_FILE, built.vitals), (LABS_FILE, built.labs),
                        (MEDS_FILE, built.meds)):
        frame.to_parquet(folder / name, index=False)
    return folder


# ---------------------------------------------------------------- the build page

def top_values(page: Page, series: pd.Series | None, limit: int = 10, label_width: int = 30) -> None:
    if series is None or series.dropna().empty:
        page.line("  (none)")
        return
    counts = text(series).fillna("(blank)").value_counts().head(limit)
    for value, n in counts.items():
        page.line(f"  {fit(value, label_width):<{label_width}} {page.count(n):>10}")


def build_page(settings: Settings, built: Built, version: str) -> Page:
    v, notes = built.visits, built.notes
    page = Page(settings, "build", "build" + (" --sp" if settings.sp else ""), version=version)
    total = len(v)
    lw = 28

    columns = f"{'n':>10} {'%':>5}"
    at = 2 + lw + len(columns)

    def row(label: str, n: int | float | None, of: int | None = total) -> None:
        pct = page.pct(n, of) if of else ""
        page.line(f"  {fit(label, lw):<{lw}}{page.count(n):>10} {pct:>5}")

    page.heading("Sources")
    for table, source in built.sources.items():
        used = next((t for t in source.tried if not t.endswith(": none") and not t.endswith(": 0 rows")), None)
        page.line(f"{fit(table, 17):<17} {fit(used.rsplit(':', 1)[0] if used else 'NOT FOUND', page.width - 18)}")
        if used:
            page.line(f"  {'rows':<{lw}}{page.count(source.rows):>10}")
        for tried in source.tried:
            if tried != used:
                spec, reason = tried.rsplit(": ", 1)
                page.line(fit(f"  skipped {spec}", page.width))
                page.line(f"    ({reason})")

    page.heading("Visits", columns, at)
    row("ED visits", total, None)
    row("duplicate EdVisitKey dropped", notes["visit_duplicates"], None)
    row("patients", v["PatientDurableKey"].nunique(), None)
    row("first visits", int(v["first_visit"].sum()))
    row(f"no departure (+{settings['no_departure_hours']}h)", notes["no_departure"])
    row("patient row found", int(v["patient_found"].sum()))
    row("birth date from patient", int((v["birth_date_source"] == "patient").sum()))
    row("birth date from birth row", int((v["birth_date_source"] == "birth").sum()))
    row("age known", int(v["age_days"].notna().sum()))
    row("age outside 0-730 days", notes["age_outside"])
    row("gestational age known", int(v["ga_weeks"].notna().sum()))
    row("GA outside 22-44 weeks", notes.get("ga_implausible", 0))
    row("admitted", int(v["admitted"].sum()))
    row("admission row found", int(v["admission_found"].sum()))
    row("ICU", int(v["icu"].sum()) if v["icu"].notna().any() else None)
    row("dx group known", int((v["dx_group"].notna() & (v["dx_group"] != "Unknown")).sum()))

    page.heading("Vitals", columns, at)
    row("visits with ED vitals", int((v["n_vitals_ed"] > 0).sum()))
    phases = built.vitals["phase"].value_counts() if len(built.vitals) else pd.Series(dtype=int)
    for phase in PHASES:
        row(f"readings {phase}", int(phases.get(phase, 0)), None)
    row("temperatures read as F", notes.get("temps_converted", 0), None)
    for name, n in notes.get("vitals_dropped", {}).items():
        row(f"{name} implausible, dropped", n, None)

    page.heading("Metrics known", columns, at)
    for column in ("rr_initial", "rr_max_ed", "rr_max_stay", "spo2_min_ed", "spo2_min_stay", "temp_initial",
                   "temp_max_ed", "temp_max_stay", "vbg_ed", "iv_fluids_ed"):
        row(column, int(v[column].notna().sum()))
    page.line(fit(f"  VBG found by: {notes['vbg_by']}", page.width))
    page.line(fit(f"  IV fluids by: {notes['iv_by']}", page.width))

    labs = built.labs
    ed_labs = labs[labs["phase"] == "ED"] if len(labs) else labs
    page.heading("VBG codes in the ED", f"{'n':>10}", 41)
    for code in settings["vbg_loinc"]:
        n = int((text(ed_labs["ComponentLoincCode"]) == str(code)).sum()) if len(ed_labs) else 0
        page.line(f"  {fit(code, 30):<30} {page.count(n):>10}")
    page.heading("Top LOINC codes, ED", f"{'n':>10}", 41)
    top_values(page, ed_labs["ComponentLoincCode"] if len(ed_labs) else None, 15)
    if len(ed_labs) and ed_labs["ComponentName"].notna().any():
        page.heading("Top lab names, ED", f"{'n':>10}", 41)
        top_values(page, ed_labs["ComponentName"], 15)

    page.heading("Values to map in settings.yaml")
    for title, column in (("race", "race"), ("ethnicity", "ethnicity"), ("financial class", "financial_class"),
                          ("sex", "sex"), ("svi quartile", "svi_quartile"), ("dx group", "dx_group")):
        page.line(f"{title}:")
        top_values(page, v[column] if v[column].notna().any() else None, 10)
    page.line("ICU department specialties:")
    top_values(page, notes.get("specialties"), 12)
    if len(built.meds):
        page.line("medication routes:")
        top_values(page, built.meds.get("AdministrationRoute"), 6)
        page.line("medication actions:")
        top_values(page, built.meds.get("AdministrationAction"), 6)
    return page
