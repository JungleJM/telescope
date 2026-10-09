#!/usr/bin/env python3
"""Write Infant RSV's two intakes from one set of table definitions (D213, D214).

    python3 studies/infant_rsv/make_intakes.py

- YAMLs/temp/Infant_RSV_Followup_intake.yaml: what the first pull lacks, with
  the first pull's visits uploaded as its PK (`python rsv keys` on the VM).
- YAMLs/temp/Infant_RSV_intake.yaml: everything again, fresh, in place of the
  first pull ("redo everything"): the same EDVisits PK, every table built as the
  follow-up builds it, and the admissions, inpatient vitals and labs, mother and
  pregnancy beside them.

The table names are the ones `rsv/settings.yaml`'s `sources` list, so `rsv build`
reads either pull. Edit the definitions here and run this again; do not edit the
written intakes by hand.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TEMP = REPO / "YAMLs" / "temp"
# The first pull as it ran on 2 October 2026 (a frozen copy: YAMLs/temp's blueprint
# becomes the redo's once exported). EDVisits and the grouped tables come from it.
FIRST_BLUEPRINT = Path(__file__).resolve().parent / "first_pull_blueprint.yaml"
FOLLOWUP = TEMP / "Infant_RSV_Followup_intake.yaml"
REDO = TEMP / "Infant_RSV_intake.yaml"

ED_WINDOW = "BETWEEN DATEADD(DAY, -1, pk.ArrivalInstant) AND DATEADD(DAY, 1, pk.DepartureInstant)"
STAY_WINDOW = "BETWEEN DATEADD(DAY, -1, haf.InpatientAdmissionInstant) AND DATEADD(DAY, 1, haf.DischargeInstant)"
LAB_NAMES = [("lcd.LoincCode", "ComponentLoincCode"), ("lcd.LoincName", "ComponentLoincName"),
             ("lcd.Name", "ComponentName"), ("lcd.CommonName", "ComponentCommonName"),
             ("lcd.BaseName", "ComponentBaseName")]
LAB_COLUMNS = ["LabComponentResultKey", "EncounterKey", "PatientDurableKey", "LabComponentKey", "CollectionInstant",
               "NumericValue", "Value", "Unit", "Abnormal", "Flag"]
MED_NAMES = [("md.Name", "MedicationName"), ("md.GenericName", "MedicationGenericName"),
             ("md.SimpleGenericName", "MedicationSimpleGenericName"),
             ("md.PharmaceuticalClass", "MedicationPharmaceuticalClass"),
             ("md.PharmaceuticalSubclass", "MedicationPharmaceuticalSubclass"),
             ("md.TherapeuticClass", "MedicationTherapeuticClass"), ("md.Form", "MedicationForm"),
             ("md.Route", "MedicationRoute")]


def cols(alias: str, names: list[str], extra: list[tuple[str, str]] | None = None) -> list[dict]:
    out = [{"source": f"{alias}.{n}", "name": n} for n in names]
    return out + [{"source": s, "name": n} for s, n in (extra or [])]


def fact(name: str, description: str, granularity: str, columns: list[dict], frm: str, join: list[str],
         where: list[str], dedup: list[str] | None = None, order: list[str] | None = None) -> dict:
    cohort = {"name": name, "dest_table": name, "type": "fact", "pull_this_cycle": True, "description": description,
              "granularity": granularity, "columns": columns, "filter": {"from": [frm], "join": join, "where": where}}
    if dedup:
        cohort["dedup_keys"] = [dedup]
    if order:
        cohort["dedup_order_by"] = order
    return cohort


def visit_tables(pk: str) -> list[dict]:
    """The tables both pulls make, joined to the visits as `pk` (`pk` is the PK's table name)."""
    on_pk = f"INNER JOIN {{{{prefix}}}}_{pk} AS pk"
    return [
        fact("Patients", "Each visit's patient: birth date, sex, race, ethnicity, SVI", "One row per patient",
             cols("pd", ["DurableKey", "BirthDate", "DeathDate", "Sex", "SexAssignedAtBirth", "FirstRace", "SecondRace",
                         "MultiRacial", "Ethnicity", "PreferredLanguage", "StateOrProvinceAbbreviation", "PrimaryRUCA_X",
                         "SviOverallPctlRankByZip2020_X", "SviSocioeconomicPctlRankByZip2020_X",
                         "SviHouseholdCharacteristicsPctlRankByZip2020_X",
                         "SviRacialEthnicMinorityStatusPctlRankByZip2020_X",
                         "SviHousingTypeTransportationPctlRankByZip2020_X"]),
             "PatientDim AS pd", [f"{on_pk} ON pk.PatientDurableKey = pd.DurableKey"],
             ["pd.IsValid = 1", "pd.IsCurrent = 1", "pd.UseInCosmosAnalytics_X = 1", "pd._IsDeleted = 0"],
             dedup=["DurableKey"]),
        fact("EDVitals", "Vitals on the visit's encounter, a day either side of the ED stay", "One row per vitals reading",
             cols("vf", ["VitalsKey", "EncounterKey", "PatientDurableKey", "TakenInstant", "RespirationRate", "SpO2",
                         "Temperature", "PulseRate", "Weight"]),
             "VitalsFact AS vf",
             [f"{on_pk} ON pk.EncounterKey = vf.EncounterKey AND pk.PatientDurableKey = vf.PatientDurableKey"],
             ["vf._IsDeleted = 0", "vf.DateKey BETWEEN {{min_date_key}} AND {{max_date_key}}", f"vf.TakenInstant {ED_WINDOW}"]),
        fact("EDLabs", "Lab components on the visit's encounter, a day either side of the ED stay, with the component's names",
             "One row per lab component result", cols("lcrf", LAB_COLUMNS, LAB_NAMES), "LabComponentResultFact AS lcrf",
             [f"{on_pk} ON pk.EncounterKey = lcrf.EncounterKey AND pk.PatientDurableKey = lcrf.PatientDurableKey",
              "INNER JOIN LabComponentDim AS lcd ON lcd.LabComponentKey = lcrf.LabComponentKey"],
             ["lcrf._IsDeleted = 0", "lcrf.PrioritizedDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
              f"lcrf.CollectionInstant {ED_WINDOW}"]),
        fact("EDDiagnoses", "Every ICD-10 diagnosis on the visit's encounter (the RSV codes, and anything beside them)",
             "One row per diagnosis event and code",
             cols("def", ["DiagnosisEventKey", "EncounterKey", "PatientDurableKey", "DiagnosisKey",
                          "EmergencyDepartmentDiagnosis", "IsPrimary", "Type"],
                  [("dt.Value", "BillingCodeValue"), ("dt.Type", "CodeType")]),
             "DiagnosisEventFact AS def",
             [f"{on_pk} ON pk.EncounterKey = def.EncounterKey AND pk.PatientDurableKey = def.PatientDurableKey",
              "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"],
             ["def._IsDeleted = 0", "dt._IsDeleted = 0", "def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
              "dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM')"],
             dedup=["DiagnosisEventKey", "BillingCodeValue"]),
        fact("Births", "Each child's birth, whenever it was: gestational age, birth weight", "One row per baby",
             cols("bf", ["BabyPatientDurableKey", "BirthKey", "BirthInstant", "BirthDateKey", "GestationalAgeDays",
                         "BirthWeightGrams", "BirthLength", "MultipleDeliveryCount", "MultipleDeliveryOrder",
                         "DeliveryMethod", "TotalApgarFiveMinute", "BabyInpatientLengthOfStayInDays", "NeonatalDemise",
                         "MotherPatientDurableKey", "PregnancyKey"]),
             "BirthFact AS bf", [f"{on_pk} ON pk.PatientDurableKey = bf.BabyPatientDurableKey"], ["bf._IsDeleted = 0"],
             dedup=["BabyPatientDurableKey"], order=["BirthKey"]),
        fact("EDMeds", "Medication administrations on the visit's encounter, a day either side of the ED stay, with the medication's names (IV fluids)",
             "One row per administration",
             cols("maf", ["MedicationAdministrationKey", "EncounterKey", "PatientDurableKey", "AdministrationInstant",
                          "AdministrationRoute", "AdministrationAction", "ActionIsMedAdministration", "MedicationKey",
                          "MedicationOrderKey", "Dose", "DoseUnit", "Rate", "AdministrationDepartmentKey"], MED_NAMES),
             "MedicationAdministrationFact AS maf",
             [f"{on_pk} ON pk.EncounterKey = maf.EncounterKey AND pk.PatientDurableKey = maf.PatientDurableKey",
              "LEFT JOIN MedicationDim AS md ON md.MedicationKey = maf.MedicationKey"],
             ["maf._IsDeleted = 0", "maf.AdministrationDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
              f"maf.AdministrationInstant {ED_WINDOW}"]),
        fact("StayDepartments", "Each department a medication was given in during the admission, with its specialty and the first time (ICU transfers mid-stay)",
             "One row per admission and department",
             [{"source": "haf.HospitalAdmissionKey", "name": "HospitalAdmissionKey"},
              {"source": "maf.AdministrationDepartmentKey", "name": "AdministrationDepartmentKey"},
              {"source": "dept.DepartmentSpecialty", "name": "DepartmentSpecialty"},
              {"source": "maf.AdministrationInstant", "name": "AdministrationInstant"}],
             "MedicationAdministrationFact AS maf",
             ["INNER JOIN HospitalAdmissionFact AS haf ON haf.EncounterKey = maf.EncounterKey",
              f"{on_pk} ON pk.HospitalAdmissionKey = haf.HospitalAdmissionKey",
              "INNER JOIN DepartmentDim AS dept ON dept.DepartmentKey = maf.AdministrationDepartmentKey"],
             ["maf._IsDeleted = 0", "haf._IsDeleted = 0", "pk.HospitalAdmissionKey > 0",
              "maf.AdministrationDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
              "haf.AdmissionDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
              "maf.AdministrationInstant BETWEEN haf.InpatientAdmissionInstant AND haf.DischargeInstant"],
             dedup=["HospitalAdmissionKey", "AdministrationDepartmentKey"], order=["AdministrationInstant"]),
    ]


ADMISSION_COLUMNS = ["HospitalAdmissionKey", "EncounterKey", "PatientDurableKey", "InpatientAdmissionInstant",
                     "DischargeInstant", "LengthOfStayInDays", "DepartmentKey", "DischargeDepartmentKey_X"]
SPECIALTIES = [("ad.DepartmentSpecialty", "AdmitSpecialty"), ("dd.DepartmentSpecialty", "DischargeSpecialty")]
SPECIALTY_JOINS = ["LEFT JOIN DepartmentDim AS ad ON ad.DepartmentKey = haf.DepartmentKey",
                   "LEFT JOIN DepartmentDim AS dd ON dd.DepartmentKey = haf.DischargeDepartmentKey_X"]


def header(first: dict, project: str, min_key: str, max_key: int) -> dict:
    return {"cosmos_vars": copy.deepcopy(first["cosmos_vars"]),
            "run_vars": {"min_date_key": min_key, "max_date_key": max_key},
            "test_options": {"smallset": False, "stop_at_for_pk_table": 10, "random_pk_sample": False},
            "project_vars": {"project_folder": project}, "vars": {}}


def followup(first: dict) -> dict:
    doc = header(first, "Infant_RSV_Followup", "20181201", 20260701)
    doc["upload_cohorts"] = [{
        "name": "RSVVisitKeys", "dest_table": "RSVVisitKeys", "type": "pk", "file_type": "parquet",
        "file_loc": "rsv_visit_keys.parquet", "key_columns": ["EdVisitKey"],
        "description": "Infant_RSV's ED visits, written by `python rsv keys` from its EDVisits.parquet",
        "columns": [{"name": n, "type": t} for n, t in (
            ("EdVisitKey", "BIGINT"), ("EncounterKey", "BIGINT"), ("PatientDurableKey", "BIGINT"),
            ("HospitalAdmissionKey", "BIGINT"), ("ArrivalInstant", "DATETIME2"), ("DepartureInstant", "DATETIME2"))],
        "awaiting_file": True, "push_this_cycle": True}]
    doc["multipliers"] = []
    doc["batching"] = [{"chunk": 200000}]
    admissions = fact(
        "AdmissionDepartments", "Each admitted visit's admission, with the specialty of the department it was admitted to and discharged from (ICU)",
        "One row per admission", cols("haf", ADMISSION_COLUMNS, SPECIALTIES), "HospitalAdmissionFact AS haf",
        ["INNER JOIN {{prefix}}_RSVVisitKeys AS pk ON pk.HospitalAdmissionKey = haf.HospitalAdmissionKey"] + SPECIALTY_JOINS,
        ["haf._IsDeleted = 0", "pk.HospitalAdmissionKey > 0", "haf.AdmissionDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"],
        dedup=["HospitalAdmissionKey"])
    doc["cohorts"] = visit_tables("RSVVisitKeys") + [admissions]
    return doc


def first_cohort(first: dict, name: str) -> dict:
    for cohort in first["cohorts"]:
        if cohort.get("name") == name:
            return copy.deepcopy(cohort)
    raise SystemExit(f"{FIRST_BLUEPRINT} has no cohort {name}")


def redo(first: dict) -> dict:
    doc = header(first, first["project_vars"]["project_folder"], str(first["run_vars"]["min_date_key"]),
                 first["run_vars"]["max_date_key"])
    doc["upload_cohorts"] = []
    doc["multipliers"] = []
    doc["batching"] = [{"chunk": 200000}]
    visits = first_cohort(first, "EDVisits")

    admissions = first_cohort(first, "HospitalAdmissionFact")
    admissions["description"] = "Each admitted visit's admission, with the specialty of the department it was admitted to and discharged from (ICU)"
    admissions["columns"] = admissions["columns"] + [{"source": s, "name": n} for s, n in SPECIALTIES]
    admissions["filter"]["join"] = list(admissions["filter"]["join"]) + SPECIALTY_JOINS
    admissions["dedup_keys"] = [["HospitalAdmissionKey"]]

    stay_vitals = first_cohort(first, "InpatientVitals")
    stay_vitals["dest_table"] = "InpatientVitals"
    stay_labs = fact("InpatientLabs", "Lab components on the admission's encounter, a day either side of the stay, with the component's names",
                     "One row per lab component result", cols("lcrf", LAB_COLUMNS, LAB_NAMES), "LabComponentResultFact AS lcrf",
                     ["INNER JOIN {{prefix}}_HospitalAdmissionFact AS haf ON haf.EncounterKey = lcrf.EncounterKey",
                      "INNER JOIN LabComponentDim AS lcd ON lcd.LabComponentKey = lcrf.LabComponentKey"],
                     ["lcrf._IsDeleted = 0", "lcrf.PrioritizedDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}",
                      f"lcrf.CollectionInstant {STAY_WINDOW}"])

    mother = first_cohort(first, "MotherPatientInfo")
    mother["filter"]["join"] = ["INNER JOIN {{prefix}}_Births AS b ON pd.DurableKey = b.MotherPatientDurableKey"]
    mother["dedup_keys"] = [["DurableKey"]]
    pregnancy = first_cohort(first, "PregnancyFact")
    pregnancy["filter"]["join"] = ["INNER JOIN {{prefix}}_Births AS b ON pf.PregnancyKey = b.PregnancyKey"]
    pregnancy["dedup_keys"] = [["PregnancyKey"]]

    tables = visit_tables("EDVisits")
    births = next(t for t in tables if t["name"] == "Births")
    doc["cohorts"] = [visits] + [t for t in tables if t["name"] != "Births"] + [
        admissions, stay_vitals, stay_labs, births, mother, pregnancy]
    doc["table_groups"] = [
        {"name": "Hospitalizations", "tables": ["HospitalAdmissionFact", "InpatientVitals", "InpatientLabs"]},
        {"name": "Birth", "tables": ["Births", "MotherPatientInfo", "PregnancyFact"]}]
    return doc


def dump(doc: dict, path: Path) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=200, allow_unicode=True), encoding="utf-8")
    print(f"Wrote {path.relative_to(REPO)}")


def main() -> int:
    # The first pull's definitions are read from its blueprint, which keeps them as they ran.
    first = yaml.safe_load(FIRST_BLUEPRINT.read_text(encoding="utf-8"))
    first.setdefault("run_vars", {})
    dump(followup(first), FOLLOWUP)
    dump(redo(first), REDO)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
