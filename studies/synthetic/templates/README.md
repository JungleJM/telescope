# Infant RSV: synthetic data for practice

> **Everything here is SYNTHETIC.** No row, value or count comes from a patient or from Cosmos. A seeded random generator made every value. The tables, columns and types match the real pull, so code written here runs on the real files. The numbers mean nothing: any pattern in them was put there by the generator, so don't draw conclusions from them.
>
> **How to tell:** every key (`EdVisitKey`, `PatientDurableKey`, `EncounterKey` and the rest) begins with **7007**. A real key does not. A number column can't start with `0`, so the prefix is 7007 rather than 007.

## What it is

The Infant RSV pull's tables: every ED visit for RSV (ICD-10 J21.0, B97.4, J12.1, J20.5) by a child under 2, from 2019 to May 2026. Beside them are the visit's patient, vitals, labs, medications and diagnoses; the admission it led to, with its vitals, labs and departments; and the child's birth, mother and pregnancy. This copy holds {{visits}} visits by {{patients}} children.

```
data/cosmos_parquets/   one parquet per table, as the real pull writes them
STATS.md                questions about this data, with their answers and the code that finds them
Contents.md             each table: what a row is, how it links, its columns and types
DataDictionary.yaml     the Cosmos tables these come from, column by column
python/  R/             loading the tables, and worked examples
rsv/                    the analysis we run on the real data (below)
```

## What you should find

These are this synthetic data's answers to a few questions. [STATS.md](STATS.md) has all {{questions}}, each with the Python and R code that finds it. Write your own query first, then check it against the answer. `python python/stats.py` or `Rscript R/stats.R` prints every one.

{{headline}}

The RSV season is taken here as October to March.

## Start here

**Python** (pandas and pyarrow):

```bash
python python/examples.py
```

```python
from python.load_parquets import load
tables = load()                  # {"EDVisits": DataFrame, "Patients": DataFrame, ...}
visits = tables["EDVisits"]
```

**R** (arrow and dplyr):

```r
source("R/load_parquets.R")
tables <- load_parquets()        # list(EDVisits = tibble, Patients = tibble, ...)
source("R/examples.R")
```

## How the tables link

- **A visit and its encounter.** `EDVisits` holds one row per ED visit. Its `EncounterKey` is the visit's encounter, which `EDVitals`, `EDLabs`, `EDMeds` and `EDDiagnoses` share.
- **The patient.** `PatientDurableKey` is the patient. In `Patients` it is called `DurableKey`.
- **The admission.** An admitted visit has a `HospitalAdmissionKey` above 0; one not admitted has -1. `HospitalAdmissionFact` holds the admission. Its `EncounterKey` is the admission's encounter: `InpatientVitals` calls it `InpatientEncounterKey`, and `InpatientLabs` calls it `EncounterKey`.
- **Where an admission went, and the ICU.** `HospitalAdmissionFact` has `AdmitSpecialty` and `DischargeSpecialty`: the specialty of the department an admission was admitted to and left from. `StayDepartments` has the departments medications were given in. The specialty names are Cosmos's own, including Pediatric Intensive Care, Critical Care Medicine and Neonatology. Hospitals name their units differently, so which specialties count as an ICU is a choice. In Cosmos, an admission's admitted-to and discharged-from departments were nearly always the same, so a move into the ICU mid-stay may not show in either. How to see such moves is still being worked out.
- **The birth.** `Births.BabyPatientDurableKey` is the patient, and `MotherPatientDurableKey` and `PregnancyKey` lead to `MotherPatientInfo` and `PregnancyFact`.
- **Temperatures** are mostly in °F, as in Cosmos. Some readings are implausible, also as in Cosmos.

## The analysis (`rsv/`)

This is the code we run on the real data. It lines every table up on the ED visit, then writes one-page summaries.

```bash
python rsv build                  # output/analysis/visits.parquet and friends, and pages/build.txt
python rsv report under_3_months  # every metric for a section
python rsv compare era            # the metrics side by side, by a grouping
python rsv admission              # race, ethnicity, SVI, financial class against admission and ICU
python rsv all                    # all of the above
python rsv sections               # the named sections and groupings
```

Every definition is in `rsv/settings.yaml`, so changing one doesn't mean changing code:
- **Sections.** Each is a filter on the visits, such as `under_3_months: age_days < 91`.
- **Groupings.** Age band, prematurity, RSV season and era (before 2023–24 against the nirsevimab seasons, with 2020–21 as the COVID off-season), sex, race, ethnicity, SVI quartile, financial class, admitted, ICU and diagnosis.
- **Metric definitions.** These include the first and highest respiratory rate, the lowest SpO2, the first and highest temperature, gestational age, IV fluids and a venous blood gas.

The same functions work from Python:

```python
import rsv
settings = rsv.load_settings()
visits = rsv.load(settings)
print(rsv.compare(settings, "age_band").render())
```

A page reports each metric's n, median (IQR) and % missing, and n (%) for the yes/no ones. **Compare** tests sections side by side (Mann–Whitney, Kruskal–Wallis or chi-square). **Admission** adds a logistic regression with errors clustered by patient.

## The real data: what is in, what is coming, what is not pulled

This synthetic copy has every table filled. The real pull is not there yet:

**In hand:**
- the ED visits;
- the admissions they led to, with inpatient vitals;
- inpatient lab results, by LOINC code only;
- births since 2019, with their mothers and pregnancies.

**Being pulled now** (a fresh pull of everything, which also replaces the tables above):
- each child's demographics: birth date, sex, race, ethnicity, language, state and SVI;
- ED vitals;
- ED lab results, with each component's name as well as its LOINC code (how a venous blood gas is found);
- every diagnosis on the visit;
- ED medications, with names (how IV fluids are found);
- the department specialties of each admission and of each medication during the stay (how the ICU is found);
- births before 2019, so every child's gestational age is there, not only the younger ones';
- inpatient lab names.

The first pull's demographics, ED vitals, ED labs and ED diagnoses came back empty. Until the new pull lands, race, ethnicity, SVI, age, the ED vitals, VBG, IV fluids and the ICU can be practised here but not answered on the real data.

**Not pulled, and not in this copy either:**
- RSV immunisation: nirsevimab, palivizumab and the maternal RSV vaccine;
- respiratory support: high-flow oxygen, CPAP and ventilation;
- what happened after discharge: follow-up visits and readmissions;
- medication orders, as against administrations;
- providers;
- anything outside the ED visit and the admission it led to.

If your question needs one of these, say so: it can go into a later pull.

## Ideas to try

- Does the lowest SpO2 in the ED differ by age band? By prematurity?
- How often does a VBG go with an admission, and with an ICU stay?
- Did anything change in the nirsevimab era?
- Repeat visits: how many children came back, and how soon?
- Write a new section or grouping in `rsv/settings.yaml`, then run `python rsv compare` with it.
