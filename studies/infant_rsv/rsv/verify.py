"""`python rsv verify`: test, against Cosmos, every assumption the analysis makes about what a column holds.

It takes a sample of infants' ED visits from one month (settings `verify`), copies
what it needs into #temp tables, and runs one query per assumption. It only reads.
Two pages come out:

    assumption-verify.txt          one line per assumption: OK, NO, LOOK (values to
                                   read and decide on) or ERR, and its evidence
    assumption-verify-detail.txt   each assumption in full, and the values seen

Run from the folder that holds `rsv` and `scope.py`, so it finds Scope's
connection settings (.env) and Pullmanager.
"""

from __future__ import annotations

import re
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, RsvError, Settings
from .page import Page

Rows = list[tuple]


def key_of(day: date) -> int:
    return int(day.strftime("%Y%m%d"))


def day_of(datekey: int) -> date:
    text = str(datekey)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def window(settings: Settings) -> dict[str, int]:
    v = settings.get("verify") or {}
    start = day_of(int(v.get("from", 20241201)))
    end = day_of(int(v.get("to", 20241231)))
    return {"n": int(v.get("visits", 20000)), "from": key_of(start), "to": key_of(end),
            "from_minus": key_of(start - timedelta(days=1)), "to_plus": key_of(end + timedelta(days=1)),
            "to_stay": key_of(end + timedelta(days=90))}


# ---------------------------------------------------------------- the sample

SETUP: list[tuple[str, str]] = [
    ("#v infant ED visits", """
DROP TABLE IF EXISTS #v;
SELECT TOP ({n}) evf.EdVisitKey, evf.EncounterKey, evf.PatientDurableKey, evf.ArrivalInstant, evf.DepartureInstant,
       evf.ArrivalDateKey, evf.HospitalAdmissionKey, evf.AgeKey, evf.FinancialClass, evf.AcuityLevel,
       evf.ArrivalMethod, evf.DischargeDisposition, evf.EdGenericDispo
INTO #v
FROM dbo.EdVisitFact AS evf
INNER JOIN dbo.DurationDim AS age ON age.DurationKey = evf.AgeKey
WHERE evf._IsDeleted = 0 AND evf.ArrivalDateKey BETWEEN {from} AND {to} AND age.Years < 2;"""),
    ("#a their admissions", """
DROP TABLE IF EXISTS #a;
SELECT haf.HospitalAdmissionKey, haf.EncounterKey, haf.DepartmentKey, haf.DischargeDepartmentKey_X,
       haf.InpatientAdmissionInstant, haf.DischargeInstant
INTO #a
FROM dbo.HospitalAdmissionFact AS haf
WHERE haf._IsDeleted = 0 AND haf.AdmissionDateKey BETWEEN {from_minus} AND {to_stay}
  AND haf.HospitalAdmissionKey IN (SELECT HospitalAdmissionKey FROM #v WHERE HospitalAdmissionKey > 0);"""),
    ("#vit vitals on the visits", """
DROP TABLE IF EXISTS #vit;
SELECT vf.EncounterKey, vf.PatientDurableKey, vf.TakenInstant, vf.Temperature, vf.SpO2, vf.RespirationRate
INTO #vit
FROM dbo.VitalsFact AS vf
WHERE vf._IsDeleted = 0 AND vf.DateKey BETWEEN {from_minus} AND {to_plus}
  AND vf.EncounterKey IN (SELECT EncounterKey FROM #v);"""),
    ("#lab labs on the visits", """
DROP TABLE IF EXISTS #lab;
SELECT l.EncounterKey, l.LabComponentKey, lcd.LoincCode, lcd.Name, lcd.CommonName, lcd.LoincName
INTO #lab
FROM dbo.LabComponentResultFact AS l
INNER JOIN dbo.LabComponentDim AS lcd ON lcd.LabComponentKey = l.LabComponentKey
WHERE l._IsDeleted = 0 AND l.PrioritizedDateKey BETWEEN {from_minus} AND {to_plus}
  AND l.EncounterKey IN (SELECT EncounterKey FROM #v);"""),
    ("#med medications on the visits", """
DROP TABLE IF EXISTS #med;
SELECT m.EncounterKey, m.AdministrationRoute, m.AdministrationAction, m.ActionIsMedAdministration,
       m.AdministrationDepartmentKey, md.Name, md.SimpleGenericName
INTO #med
FROM dbo.MedicationAdministrationFact AS m
LEFT JOIN dbo.MedicationDim AS md ON md.MedicationKey = m.MedicationKey
WHERE m._IsDeleted = 0 AND m.AdministrationDateKey BETWEEN {from_minus} AND {to_plus}
  AND m.EncounterKey IN (SELECT EncounterKey FROM #v);"""),
    ("#smed medications during the stays", """
DROP TABLE IF EXISTS #smed;
SELECT a.HospitalAdmissionKey, m.AdministrationDepartmentKey
INTO #smed
FROM #a AS a
INNER JOIN dbo.MedicationAdministrationFact AS m ON m.EncounterKey = a.EncounterKey
WHERE m._IsDeleted = 0 AND m.AdministrationDateKey BETWEEN {from_minus} AND {to_stay}
  AND m.AdministrationInstant BETWEEN a.InpatientAdmissionInstant AND a.DischargeInstant;"""),
    ("#pat the visits' patients", """
DROP TABLE IF EXISTS #pat;
SELECT pd.DurableKey, pd.IsCurrent, pd.IsValid, pd.UseInCosmosAnalytics_X, pd._IsDeleted, pd.BirthDate,
       pd.BirthDateAccuracy_X, pd.EarliestPossibleBirthDate_X, pd.Sex, pd.FirstRace, pd.SecondRace,
       pd.MultiRacial, pd.Ethnicity, pd.SviOverallPctlRankByZip2020_X
INTO #pat
FROM dbo.PatientDim AS pd
WHERE pd.DurableKey IN (SELECT PatientDurableKey FROM #v);"""),
    ("#birth their births", """
DROP TABLE IF EXISTS #birth;
SELECT bf.BabyPatientDurableKey, bf.GestationalAgeDays, bf.BirthInstant
INTO #birth
FROM dbo.BirthFact AS bf
WHERE bf._IsDeleted = 0 AND bf.BabyPatientDurableKey IN (SELECT PatientDurableKey FROM #v);"""),
    ("#dx diagnoses on the visits", """
DROP TABLE IF EXISTS #dx;
SELECT d.EncounterKey, d.EmergencyDepartmentDiagnosis, d.IsPrimary, dt.Value, dt.Type
INTO #dx
FROM dbo.DiagnosisEventFact AS d
INNER JOIN dbo.DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = d.DiagnosisKey
WHERE d._IsDeleted = 0 AND dt._IsDeleted = 0 AND d.StartDateKey BETWEEN {from_minus} AND {to_plus}
  AND dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM')
  AND d.EncounterKey IN (SELECT EncounterKey FROM #v);"""),
]


# ---------------------------------------------------------------- the checks

@dataclass
class Check:
    id: str
    label: str            # short, for the one-line verdict
    assumption: str       # in full, for the detail page
    used_in: str
    sql: str
    judge: Callable[[Rows, Settings], tuple[str, str]]   # rows -> (verdict, evidence)


MIN_CELL = 0    # set from settings `min_cell` by run(): counts under it print as <N


def fmt(n: Any) -> str:
    """A count, hidden as <N when it is small (settings `min_cell`)."""
    n = int(n or 0)
    return f"<{MIN_CELL}" if 0 < n < MIN_CELL else f"{n:,}"


def z(row: tuple) -> list:
    """An aggregate row with NULL (a SUM over no rows) read as 0."""
    return [0 if c is None else c for c in row]


def pct(k: float, n: float) -> str:
    return "-" if not n else f"{100.0 * k / n:.1f}%"


def values(rows: Rows, limit: int = 8) -> str:
    return "; ".join(f"{r[0]} {fmt(r[1])}" for r in rows[:limit]) or "none"


def j_admission_key(rows, s):
    other = [r for r in rows if r[0] not in (">0",)]
    ok = all(r[0] in ("-1", "NULL") or (r[0].lstrip("-").isdigit() and int(r[0]) <= 0) for r in other)
    return ("OK" if ok and any(r[0] == "-1" for r in rows) else "NO"), values(rows)


def j_found(rows, s):
    keyed, found = z(rows[0])
    return ("OK" if keyed and found / keyed >= .98 else "NO"), f"{found:,} of {keyed:,} keys found ({pct(found, keyed)})"


def j_departure(rows, s):
    n, missing, backwards, avg_hours = z(rows[0])
    return ("OK" if n and missing / n < .1 and backwards / n < .01 else "NO"), \
        f"missing {pct(missing, n)}, before arrival {pct(backwards, n)}, mean {float(avg_hours or 0):.1f} h"


def j_clock(rows, s):
    n, same = z(rows[0])
    return ("OK" if n and same / n >= .99 else "NO"), f"Instant's date = DateKey for {pct(same, n)}"


def j_ed_vitals(rows, s):
    visits, matched, by_encounter = z(rows[0])
    verdict = "OK" if visits and matched / visits >= .5 else "NO"
    return verdict, f"visits with vitals: {pct(matched, visits)} on encounter+patient, {pct(by_encounter, visits)} on encounter"


def j_temperature(rows, s):
    n, over, celsius, between, below, low, high = z(rows[0])
    odd = between + below
    verdict = "OK" if n and odd / n < .01 else "NO"
    return verdict, f">45 {pct(over, n)}; 30-45 {pct(celsius, n)}; 45-86 {pct(between, n)}; <30 {pct(below, n)}; {low}-{high}"


def j_spo2(rows, s):
    n, fraction, normal, over, low, high = z(rows[0])
    verdict = "OK" if n and fraction / n < .01 and over / n < .01 else "NO"
    return verdict, f"40-100 {pct(normal, n)}; <=1 {pct(fraction, n)}; >100 {pct(over, n)}; {low}-{high}"


def j_rr(rows, s):
    n, outside, low, high = z(rows[0])
    return ("OK" if n and outside / n < .02 else "NO"), f"outside 5-150 {pct(outside, n)}; {low}-{high}"


def j_admission_encounter(rows, s):
    n, same = z(rows[0])
    return "LOOK", f"admission on the ED's encounter: {pct(same, n)} of {n:,}"


def j_stay_times(rows, s):
    n, no_start, no_end = z(rows[0])
    return ("OK" if n and (no_start + no_end) / n < .05 else "NO"), \
        f"no inpatient admission time {pct(no_start, n)}, no discharge {pct(no_end, n)}"


def j_patient_filters(rows, s):
    patients, found, current, valid, analytics, all_four = z(rows[0])
    verdict = "OK" if patients and all_four / patients >= .9 else "NO"
    return verdict, (f"of {patients:,}: in PatientDim {pct(found, patients)}, IsCurrent {pct(current, patients)}, "
                     f"IsValid {pct(valid, patients)}, UseInCosmosAnalytics_X {pct(analytics, patients)}, all {pct(all_four, patients)}")


def j_birth_accuracy(rows, s):
    return "LOOK", values(rows)


def j_age(rows, s):
    n, same = z(rows[0])
    return ("OK" if n and same / n >= .97 else "NO"), f"BirthDate age = DurationDim.Years for {pct(same, n)} of {n:,}"


def j_listing(rows, s):
    return "LOOK", values(rows)


def j_multiracial(rows, s):
    seen = {str(r[0]) for r in rows}
    ok = seen <= {"0", "1", "(null)"}
    return ("OK" if ok else "NO"), values(rows)


def j_svi(rows, s):
    n, low, high = z(rows[0])
    if not n:
        return "NO", "no values"
    scale = "0-1" if float(high) <= 1 else "0-100" if float(high) <= 100 else "unknown"
    return ("OK" if scale != "unknown" else "NO"), f"{n:,} values, {low}-{high}: scale {scale}"


def j_ga(rows, s):
    rows_n, babies, low, high, inside = z(rows[0])
    verdict = "OK" if rows_n and inside / rows_n >= .97 else "NO"
    return verdict, f"{rows_n:,} rows, {babies:,} babies; {low}-{high} days; 22-44 weeks {pct(inside, rows_n)}"


def j_birth_link(rows, s):
    patients, born = z(rows[0])
    return "LOOK", f"{pct(born, patients)} of {patients:,} children have a birth row"


def j_dx_format(rows, s):
    seen = {(r[0], r[1]) for r in rows}
    ok = all(("ICD-10-CM", code) in seen for code in ("J21.0", "B97.4"))
    return ("OK" if ok else "NO"), "; ".join(f"{r[0]} {r[1]} {fmt(r[2])}" for r in rows[:8])


def j_ed_dx(rows, s):
    visits, encounters, ed_flag, primary = z(rows[0])
    return "LOOK", f"visits with ICD-10 dx {pct(encounters, visits)}; ED dx flag {pct(ed_flag, visits)}; a primary {pct(primary, visits)}"


def j_rsv_codes(rows, s):
    return "LOOK", values(rows)


def j_vbg(rows, s):
    codes = {str(r[0]) for r in rows}
    wanted = {str(c) for c in s["vbg_loinc"]}
    missing = sorted(wanted - codes)
    verdict = "OK" if not missing else "NO"
    return verdict, ("all listed codes seen" if not missing else f"not seen: {', '.join(missing)}") + \
        "; seen: " + "; ".join(f"{r[0]} {str(r[1])[:24]} {fmt(r[3])}" for r in rows[:6])


def j_lab_codes(rows, s):
    total, no_loinc, starred, named = z(rows[0])
    return "LOOK", f"{total:,} labs: no LOINC {pct(no_loinc, total)}, '*' LOINC {pct(starred, total)}, a Name {pct(named, total)}"


def j_routes(rows, s):
    patterns = re.compile("|".join(f"(?:{p})" for p in s["iv_routes"]), re.IGNORECASE)
    hit = [str(r[0]) for r in rows if patterns.search(str(r[0]))]
    near = [str(r[0]) for r in rows if re.search(r"intraven|\biv\b", str(r[0]), re.I) and str(r[0]) not in hit]
    verdict = "OK" if hit and not near else "NO"
    return verdict, f"matched: {', '.join(hit) or 'none'}; missed: {', '.join(near) or 'none'}"


def j_fluids(rows, s):
    patterns = re.compile("|".join(f"(?:{p})" for p in s["iv_fluid_patterns"]), re.IGNORECASE)
    total = sum(int(r[1]) for r in rows)
    matched = sum(int(r[1]) for r in rows if patterns.search(str(r[0])))
    named = sum(int(r[1]) for r in rows if r[0] != "(null)")
    return "LOOK", f"IV doses with a name {pct(named, total)}; matching iv_fluid_patterns {pct(matched, total)}"


def j_stay_dept(rows, s):
    n, missing = z(rows[0])
    return ("OK" if n and missing / n < .1 else "NO"), f"{n:,} stay doses; no department {pct(missing, n)}"


def j_icu(rows, s):
    seen = {str(r[0]) for r in rows}
    wanted = [str(x) for x in s["icu_specialties"]]
    missing = [w for w in wanted if w not in seen]
    return ("OK" if not missing else "NO"), ("all listed seen" if not missing else "not seen: " + ", ".join(missing))


def j_star(rows, s):
    return "LOOK", "; ".join(f"{r[0]}={r[1]} {fmt(r[2])}" for r in rows[:8]) or "none"


def j_unknown(rows, s):
    wanted = {str(u).lower() for u in s["unknown"] if str(u)}
    seen = {str(r[1]).lower() for r in rows}
    return "LOOK", f"of the `unknown` list, seen: {', '.join(sorted(wanted & seen)) or 'none'}"


CHECKS: list[Check] = [
    Check("admission_key", "no admission is key -1",
          "A visit not admitted has HospitalAdmissionKey -1; a real admission's key is above 0.",
          "build: admitted", """
SELECT CASE WHEN HospitalAdmissionKey IS NULL THEN 'NULL' WHEN HospitalAdmissionKey > 0 THEN '>0'
            ELSE CAST(HospitalAdmissionKey AS VARCHAR(20)) END, COUNT(*)
FROM #v GROUP BY CASE WHEN HospitalAdmissionKey IS NULL THEN 'NULL' WHEN HospitalAdmissionKey > 0 THEN '>0'
            ELSE CAST(HospitalAdmissionKey AS VARCHAR(20)) END ORDER BY COUNT(*) DESC;""", j_admission_key),
    Check("admission_found", "a key >0 is a real admission",
          "Every HospitalAdmissionKey above 0 has its row in HospitalAdmissionFact.", "build: admission_found", """
SELECT COUNT(*), SUM(CASE WHEN a.HospitalAdmissionKey IS NULL THEN 0 ELSE 1 END)
FROM #v AS v LEFT JOIN (SELECT DISTINCT HospitalAdmissionKey FROM #a) AS a ON a.HospitalAdmissionKey = v.HospitalAdmissionKey
WHERE v.HospitalAdmissionKey > 0;""", j_found),
    Check("departure", "departure time is there",
          "DepartureInstant is filled for nearly every visit and is after arrival (a missing one is taken as arrival + no_departure_hours).",
          "build: the ED window", """
SELECT COUNT(*), SUM(CASE WHEN DepartureInstant IS NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN DepartureInstant < ArrivalInstant THEN 1 ELSE 0 END),
       AVG(CAST(DATEDIFF(MINUTE, ArrivalInstant, DepartureInstant) AS FLOAT)) / 60
FROM #v;""", j_departure),
    Check("same_clock", "Instants on DateKey's clock",
          "An Instant and its DateKey are on the same clock (local), so seasons and dates read from Instants are right.",
          "build: season, age", """
SELECT COUNT(*), SUM(CASE WHEN CONVERT(INT, CONVERT(CHAR(8), ArrivalInstant, 112)) = ArrivalDateKey THEN 1 ELSE 0 END)
FROM #v;""", j_clock),
    Check("ed_vitals_link", "ED vitals on the visit's encounter",
          "VitalsFact rows for an ED visit carry its EncounterKey and PatientDurableKey (the first pull's EDVitals was empty).",
          "pulls: EDVitals; build: ED metrics", """
SELECT (SELECT COUNT(*) FROM #v),
       (SELECT COUNT(DISTINCT v.EdVisitKey) FROM #v AS v INNER JOIN #vit AS t
            ON t.EncounterKey = v.EncounterKey AND t.PatientDurableKey = v.PatientDurableKey),
       (SELECT COUNT(DISTINCT v.EdVisitKey) FROM #v AS v INNER JOIN #vit AS t ON t.EncounterKey = v.EncounterKey);""",
          j_ed_vitals),
    Check("temperature_units", "temperature >45 is F",
          "VitalsFact.Temperature is °C (30-45) or °F (above 86); a value above 45 is °F.", "build: temp_c", """
SELECT COUNT(Temperature), SUM(CASE WHEN Temperature > 45 THEN 1 ELSE 0 END),
       SUM(CASE WHEN Temperature BETWEEN 30 AND 45 THEN 1 ELSE 0 END),
       SUM(CASE WHEN Temperature > 45 AND Temperature < 86 THEN 1 ELSE 0 END),
       SUM(CASE WHEN Temperature < 30 THEN 1 ELSE 0 END), MIN(Temperature), MAX(Temperature)
FROM #vit;""", j_temperature),
    Check("spo2_scale", "SpO2 is a percent",
          "VitalsFact.SpO2 is a percentage (40-100), not a fraction.", "build: spo2", """
SELECT COUNT(SpO2), SUM(CASE WHEN SpO2 <= 1 THEN 1 ELSE 0 END), SUM(CASE WHEN SpO2 BETWEEN 40 AND 100 THEN 1 ELSE 0 END),
       SUM(CASE WHEN SpO2 > 100 THEN 1 ELSE 0 END), MIN(SpO2), MAX(SpO2)
FROM #vit;""", j_spo2),
    Check("rr_range", "RR is breaths a minute",
          "VitalsFact.RespirationRate is breaths per minute; few fall outside 5-150.", "build: rr", """
SELECT COUNT(RespirationRate), SUM(CASE WHEN RespirationRate < 5 OR RespirationRate > 150 THEN 1 ELSE 0 END),
       MIN(RespirationRate), MAX(RespirationRate)
FROM #vit;""", j_rr),
    Check("admission_encounter", "admission's own encounter",
          "How often an admission's EncounterKey is the ED visit's own (its vitals then appear in both tables; the build keeps one).",
          "build: stay vitals and labs", """
SELECT COUNT(*), SUM(CASE WHEN a.EncounterKey = v.EncounterKey THEN 1 ELSE 0 END)
FROM #v AS v INNER JOIN #a AS a ON a.HospitalAdmissionKey = v.HospitalAdmissionKey;""", j_admission_encounter),
    Check("stay_times", "admission has start and end",
          "HospitalAdmissionFact.InpatientAdmissionInstant and DischargeInstant are filled (the stay's window needs both).",
          "pulls: InpatientVitals, InpatientLabs, StayDepartments", """
SELECT COUNT(*), SUM(CASE WHEN InpatientAdmissionInstant IS NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN DischargeInstant IS NULL THEN 1 ELSE 0 END)
FROM #a;""", j_stay_times),
    Check("patient_filters", "PatientDim filters keep them",
          "PatientDim's IsCurrent = 1, IsValid = 1, UseInCosmosAnalytics_X = 1 and _IsDeleted = 0 keep the visits' patients (the first pull's patients came back empty).",
          "pulls: Patients, MotherPatientInfo", """
SELECT (SELECT COUNT(DISTINCT PatientDurableKey) FROM #v),
       COUNT(DISTINCT DurableKey),
       COUNT(DISTINCT CASE WHEN IsCurrent = 1 THEN DurableKey END),
       COUNT(DISTINCT CASE WHEN IsValid = 1 THEN DurableKey END),
       COUNT(DISTINCT CASE WHEN UseInCosmosAnalytics_X = 1 THEN DurableKey END),
       COUNT(DISTINCT CASE WHEN IsCurrent = 1 AND IsValid = 1 AND UseInCosmosAnalytics_X = 1 AND _IsDeleted = 0 THEN DurableKey END)
FROM #pat;""", j_patient_filters),
    Check("birth_date_accuracy", "BirthDate is the real date",
          "PatientDim.BirthDate is the true date of birth, not shifted or rounded (BirthDateAccuracy_X and EarliestPossibleBirthDate_X say).",
          "build: age_days", """
SELECT COALESCE(CAST(BirthDateAccuracy_X AS NVARCHAR(100)), '(null)'), COUNT(*),
       SUM(CASE WHEN BirthDate = EarliestPossibleBirthDate_X THEN 1 ELSE 0 END)
FROM #pat WHERE IsCurrent = 1
GROUP BY COALESCE(CAST(BirthDateAccuracy_X AS NVARCHAR(100)), '(null)') ORDER BY COUNT(*) DESC;""", j_birth_accuracy),
    Check("age_agrees", "age from BirthDate fits",
          "Age worked out from PatientDim.BirthDate agrees with the visit's DurationDim.Years.", "build: age_days", """
SELECT COUNT(*), SUM(CASE WHEN FLOOR(DATEDIFF(DAY, p.BirthDate, v.ArrivalInstant) / 365.25) = age.Years THEN 1 ELSE 0 END)
FROM #v AS v
INNER JOIN #pat AS p ON p.DurableKey = v.PatientDurableKey AND p.IsCurrent = 1
INNER JOIN dbo.DurationDim AS age ON age.DurationKey = v.AgeKey;""", j_age),
    Check("race_values", "race values",
          "FirstRace's values: which are real races and which placeholders (settings race_map, unknown).", "build: race", """
SELECT TOP 15 COALESCE(FirstRace, '(null)'), COUNT(*) FROM #pat WHERE IsCurrent = 1
GROUP BY COALESCE(FirstRace, '(null)') ORDER BY COUNT(*) DESC;""", j_listing),
    Check("multiracial", "MultiRacial is 1/0",
          "PatientDim.MultiRacial holds 1 or 0 (the build reads 1, true, Y or Yes as more than one race).", "build: race", """
SELECT COALESCE(CAST(MultiRacial AS NVARCHAR(50)), '(null)'), COUNT(*) FROM #pat WHERE IsCurrent = 1
GROUP BY COALESCE(CAST(MultiRacial AS NVARCHAR(50)), '(null)') ORDER BY COUNT(*) DESC;""", j_multiracial),
    Check("ethnicity_values", "ethnicity values",
          "Ethnicity's values (settings ethnicity_map, unknown).", "build: ethnicity", """
SELECT TOP 10 COALESCE(Ethnicity, '(null)'), COUNT(*) FROM #pat WHERE IsCurrent = 1
GROUP BY COALESCE(Ethnicity, '(null)') ORDER BY COUNT(*) DESC;""", j_listing),
    Check("sex_values", "sex values", "Sex's values.", "build: sex", """
SELECT TOP 10 COALESCE(Sex, '(null)'), COUNT(*) FROM #pat WHERE IsCurrent = 1
GROUP BY COALESCE(Sex, '(null)') ORDER BY COUNT(*) DESC;""", j_listing),
    Check("svi_scale", "SVI is a 0-1 or 0-100 rank",
          "SviOverallPctlRankByZip2020_X is a percentile rank, 0-1 or 0-100 (the build reads its scale from the largest value).",
          "build: svi_quartile", """
SELECT COUNT(SviOverallPctlRankByZip2020_X), MIN(SviOverallPctlRankByZip2020_X), MAX(SviOverallPctlRankByZip2020_X)
FROM #pat WHERE IsCurrent = 1;""", j_svi),
    Check("financial_values", "financial class values",
          "EdVisitFact.FinancialClass's values (settings financial_class_map).", "build: financial_class", """
SELECT TOP 12 COALESCE(FinancialClass, '(null)'), COUNT(*) FROM #v
GROUP BY COALESCE(FinancialClass, '(null)') ORDER BY COUNT(*) DESC;""", j_listing),
    Check("visit_text_values", "visit text values",
          "AcuityLevel, ArrivalMethod, DischargeDisposition and EdGenericDispo's values (the synthetic copy invents them until these are known).",
          "synthetic data", """
SELECT TOP 24 col, val, n FROM (
    SELECT 'acuity' AS col, COALESCE(AcuityLevel, '(null)') AS val, COUNT(*) AS n FROM #v GROUP BY AcuityLevel
    UNION ALL SELECT 'arrival', COALESCE(ArrivalMethod, '(null)'), COUNT(*) FROM #v GROUP BY ArrivalMethod
    UNION ALL SELECT 'dispo', COALESCE(DischargeDisposition, '(null)'), COUNT(*) FROM #v GROUP BY DischargeDisposition
    UNION ALL SELECT 'generic', COALESCE(EdGenericDispo, '(null)'), COUNT(*) FROM #v GROUP BY EdGenericDispo
) AS s ORDER BY col, n DESC;""", j_star),
    Check("star_values", "'*' values are placeholders",
          "A value beginning with * (*Unspecified, *Not Applicable) is a placeholder, read as Unknown.", "build: groupings", """
SELECT TOP 12 col, val, n FROM (
    SELECT 'race' AS col, FirstRace AS val, COUNT(*) AS n FROM #pat WHERE FirstRace LIKE '*%' GROUP BY FirstRace
    UNION ALL SELECT 'ethnicity', Ethnicity, COUNT(*) FROM #pat WHERE Ethnicity LIKE '*%' GROUP BY Ethnicity
    UNION ALL SELECT 'sex', Sex, COUNT(*) FROM #pat WHERE Sex LIKE '*%' GROUP BY Sex
    UNION ALL SELECT 'financial', FinancialClass, COUNT(*) FROM #v WHERE FinancialClass LIKE '*%' GROUP BY FinancialClass
) AS s ORDER BY n DESC;""", j_star),
    Check("unknown_list", "the `unknown` words occur",
          "The words in settings `unknown` (Patient Declined, Not Reported...) are values Cosmos uses.", "build: groupings", """
SELECT TOP 20 col, val, n FROM (
    SELECT 'race' AS col, FirstRace AS val, COUNT(*) AS n FROM #pat GROUP BY FirstRace
    UNION ALL SELECT 'ethnicity', Ethnicity, COUNT(*) FROM #pat GROUP BY Ethnicity
    UNION ALL SELECT 'sex', Sex, COUNT(*) FROM #pat GROUP BY Sex
    UNION ALL SELECT 'financial', FinancialClass, COUNT(*) FROM #v GROUP BY FinancialClass
) AS s WHERE val NOT LIKE '*%' ORDER BY n DESC;""", j_unknown),
    Check("gestational_age", "GA is in days, 22-44 weeks",
          "BirthFact.GestationalAgeDays is days (154-308 for 22-44 weeks), one row per baby.", "build: ga_weeks", """
SELECT COUNT(*), COUNT(DISTINCT BabyPatientDurableKey), MIN(GestationalAgeDays), MAX(GestationalAgeDays),
       SUM(CASE WHEN GestationalAgeDays BETWEEN 154 AND 308 THEN 1 ELSE 0 END)
FROM #birth;""", j_ga),
    Check("birth_link", "a child's birth row",
          "BirthFact.BabyPatientDurableKey is the child's PatientDurableKey (how many have one).", "pulls: Births", """
SELECT (SELECT COUNT(DISTINCT PatientDurableKey) FROM #v), COUNT(DISTINCT BabyPatientDurableKey) FROM #birth;""",
          j_birth_link),
    Check("dx_code_format", "ICD codes written J21.0",
          "DiagnosisTerminologyDim.Value writes ICD-10 codes with the dot (J21.0) under Type ICD-10-CM.", "pulls: EDVisits, EDDiagnoses; build: dx_group", """
SELECT TOP 12 dt.Type, dt.Value, COUNT(*) FROM dbo.DiagnosisTerminologyDim AS dt
WHERE dt._IsDeleted = 0 AND dt.Value IN ('J21.0', 'J210', 'B97.4', 'B974', 'J12.1', 'J121', 'J20.5', 'J205')
GROUP BY dt.Type, dt.Value ORDER BY COUNT(*) DESC;""", j_dx_format),
    Check("ed_diagnoses", "ED dx flag and primary",
          "Visits carry ICD-10 diagnoses on their encounter; EmergencyDepartmentDiagnosis and IsPrimary are 1/0.", "pulls: EDDiagnoses", """
SELECT (SELECT COUNT(*) FROM #v), COUNT(DISTINCT EncounterKey),
       COUNT(DISTINCT CASE WHEN EmergencyDepartmentDiagnosis = 1 THEN EncounterKey END),
       COUNT(DISTINCT CASE WHEN IsPrimary = 1 THEN EncounterKey END)
FROM #dx;""", j_ed_dx),
    Check("rsv_codes", "the RSV codes occur",
          "The dx_groups codes (J21.0, J12.1, J20.5, B97.4) are on infants' ED encounters.", "build: dx_group", """
SELECT Value, COUNT(DISTINCT EncounterKey) FROM #dx WHERE Value IN ('J21.0', 'J12.1', 'J20.5', 'B97.4')
GROUP BY Value ORDER BY COUNT(DISTINCT EncounterKey) DESC;""", j_rsv_codes),
    Check("vbg_codes", "VBG LOINC codes are right",
          "The vbg_loinc codes are the venous blood gas's; the components named venous or VBG show what is used.", "build: vbg_ed", """
SELECT TOP 12 COALESCE(LoincCode, '(null)'), COALESCE(Name, ''), COALESCE(LoincName, ''), COUNT(*)
FROM #lab
WHERE Name LIKE '%venous%' OR LoincName LIKE '%venous%' OR CommonName LIKE '%venous%' OR Name LIKE '%VBG%'
   OR LoincCode IN ('2746-4', '2021-4')
GROUP BY COALESCE(LoincCode, '(null)'), COALESCE(Name, ''), COALESCE(LoincName, '') ORDER BY COUNT(*) DESC;""", j_vbg),
    Check("lab_codes", "labs have LOINC and names",
          "LabComponentDim gives most components a LOINC code, and a Name (*Unspecified is no code).", "build: vbg_ed", """
SELECT COUNT(*), SUM(CASE WHEN LoincCode IS NULL OR LoincCode = '' THEN 1 ELSE 0 END),
       SUM(CASE WHEN LoincCode LIKE '*%' THEN 1 ELSE 0 END), SUM(CASE WHEN Name IS NOT NULL AND Name <> '' THEN 1 ELSE 0 END)
FROM #lab;""", j_lab_codes),
    Check("routes", "IV route values",
          "AdministrationRoute names IV as settings iv_routes match (IV..., Intravenous).", "build: iv_fluids_ed", """
SELECT TOP 15 COALESCE(AdministrationRoute, '(null)'), COUNT(*) FROM #med
GROUP BY COALESCE(AdministrationRoute, '(null)') ORDER BY COUNT(*) DESC;""", j_routes),
    Check("actions", "administration actions",
          "AdministrationAction and ActionIsMedAdministration say which doses were given (settings iv_given_actions).",
          "build: iv_fluids_ed", """
SELECT TOP 12 COALESCE(AdministrationAction, '(null)') + ' / ' + COALESCE(CAST(ActionIsMedAdministration AS NVARCHAR(10)), '(null)'),
       COUNT(*)
FROM #med GROUP BY COALESCE(AdministrationAction, '(null)') + ' / ' + COALESCE(CAST(ActionIsMedAdministration AS NVARCHAR(10)), '(null)')
ORDER BY COUNT(*) DESC;""", j_listing),
    Check("iv_fluid_names", "IV fluid names match",
          "The IV doses' MedicationDim names; how many the iv_fluid_patterns catch.", "build: iv_fluids_ed", """
SELECT TOP 25 COALESCE(Name, '(null)'), COUNT(*) FROM #med
WHERE AdministrationRoute LIKE 'IV%' OR AdministrationRoute LIKE '%intraven%'
GROUP BY COALESCE(Name, '(null)') ORDER BY COUNT(*) DESC;""", j_fluids),
    Check("stay_departments", "stay doses have a department",
          "MedicationAdministrationFact.AdministrationDepartmentKey is filled during stays (how a mid-stay ICU is seen).",
          "pulls: StayDepartments; build: icu", """
SELECT COUNT(*), SUM(CASE WHEN AdministrationDepartmentKey IS NULL OR AdministrationDepartmentKey < 0 THEN 1 ELSE 0 END)
FROM #smed;""", j_stay_dept),
    Check("icu_specialties", "ICU specialty names exist",
          "The icu_specialties strings are DepartmentSpecialty values as written; the detail lists every critical-care-like one.",
          "build: icu", """
SELECT DepartmentSpecialty, COUNT(*) FROM dbo.DepartmentDim
WHERE _IsDeleted = 0 AND (DepartmentSpecialty LIKE '%critical%' OR DepartmentSpecialty LIKE '%intensive%'
   OR DepartmentSpecialty LIKE '%ICU%' OR DepartmentSpecialty LIKE '%neonat%')
GROUP BY DepartmentSpecialty ORDER BY COUNT(*) DESC;""", j_icu),
]


# ---------------------------------------------------------------- running them

@dataclass
class Result:
    check: Check
    verdict: str
    evidence: str
    rows: Rows
    seconds: float


def run(connection: Any, settings: Settings, log: Callable[[str], None] = print) -> tuple[list[Result], list[str]]:
    global MIN_CELL
    MIN_CELL = int(settings.get("min_cell", 0) or 0)
    params = window(settings)
    cursor = connection.cursor()
    setup_errors = []
    for label, sql in SETUP:
        started = time.time()
        try:
            cursor.execute("SET NOCOUNT ON;\n" + sql.format(**params))
            while cursor.nextset():
                pass
            log(f"  built {label} ({time.time() - started:.0f}s)")
        except Exception as exc:          # one failed step leaves the rest to run
            setup_errors.append(f"{label}: {first_line(exc)}")
            log(f"  FAILED {label}: {first_line(exc)}")
    results = []
    for check in CHECKS:
        started = time.time()
        try:
            cursor.execute("SET NOCOUNT ON;\n" + check.sql.format(**params))
            rows = [tuple(r) for r in cursor.fetchall()]
            verdict, evidence = check.judge(rows, settings) if rows else ("NO", "no rows")
        except Exception as exc:
            rows, verdict, evidence = [], "ERR", first_line(exc)
        results.append(Result(check, verdict, evidence, rows, time.time() - started))
        log(f"  {verdict:<4} {check.id}")
    return results, setup_errors


def first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


def summary_page(settings: Settings, results: list[Result], setup_errors: list[str]) -> Page:
    params = window(settings)
    page = Page(settings, "assumption check", "verify",
                section=f"{params['n']:,} infant ED visits",
                section_filter=f"arrivals {params['from']}-{params['to']}")
    counts = {v: sum(r.verdict == v for r in results) for v in ("OK", "NO", "LOOK", "ERR")}
    page.line("  ".join(f"{k} {n}" for k, n in counts.items()))
    page.line("OK holds; NO fails; LOOK read and decide")
    for error in setup_errors:
        for text in textwrap.wrap(f"SETUP FAILED {error}", page.width):
            page.line(text)
    for number, result in enumerate(results, 1):
        page.line()
        page.line(f"{number:>2} {result.verdict:<4} {result.check.label}"[: page.width])
        for text in textwrap.wrap(result.evidence, page.width - 3):
            page.line("   " + text)
    return page


def detail_page(settings: Settings, results: list[Result]) -> Page:
    page = Page(settings, "assumption check: detail", "verify")
    for number, result in enumerate(results, 1):
        page.line()
        page.line(f"{number:>2} {result.verdict} {result.check.id}"[: page.width])
        for text in textwrap.wrap(result.check.assumption, page.width - 3):
            page.line("   " + text)
        page.line(f"   used by: {result.check.used_in}"[: page.width])
        for row in result.rows[:25]:
            cells = " | ".join("" if c is None else fmt(c) if isinstance(c, int) and not isinstance(c, bool)
                               else str(c) for c in row)
            for i, text in enumerate(textwrap.wrap(cells, page.width - 5) or [""]):
                page.line(("   - " if i == 0 else "     ") + text)
    return page


# ---------------------------------------------------------------- connecting

def scope_runtime(root: Path = ROOT) -> Path:
    """The folder that holds Scope's `pullmanager` package, from scope.py beside `rsv`."""
    launcher = root / "scope.py"
    if launcher.is_file():
        found = re.search(r"/\s*['\"]([^'\"]+)['\"]\s*/\s*['\"]pullmanager\.py['\"]", launcher.read_text(encoding="utf-8"))
        if found and (root / found.group(1) / "pullmanager").is_dir():
            return root / found.group(1)
    for candidate in sorted(root.glob("*/pullmanager/db.py")) + sorted(root.glob("*/*/pullmanager/db.py")):
        return candidate.parent.parent
    raise RsvError(f"No Scope beside {root / 'rsv'}: `verify` connects the way Scope does. "
                   "Install rsv in the folder that holds scope.py.")


def connect_cosmos(root: Path = ROOT) -> Any:
    sys.path.insert(0, str(scope_runtime(root)))
    from pullmanager.db import DatabaseError, Settings as DbSettings, connect, load_env_file
    from pullmanager.normalize import cosmos_database
    try:
        load_env_file(None)
        db = DbSettings.from_env()
        return connect(db.cosmos_connection_string(cosmos_database(None)), login_timeout=db.login_timeout,
                       query_timeout=db.query_timeout)
    except DatabaseError as exc:
        raise RsvError(f"Could not connect to Cosmos: {exc}") from exc


def verify(settings: Settings, connection: Any | None = None) -> tuple[Path, Path]:
    connection = connection or connect_cosmos(settings.root)
    print("Checking the analysis's assumptions against Cosmos (it only reads) ...", flush=True)
    results, setup_errors = run(connection, settings)
    summary = summary_page(settings, results, setup_errors).write("assumption-verify")
    detail = detail_page(settings, results).write("assumption-verify-detail")
    return summary, detail
