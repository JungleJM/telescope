"""`contents.md`: every packaged table, described (D73)."""

from __future__ import annotations

from ..contents import aliases, render, specific_to
from ..manifest import Manifest
from ..yaml_io import dump_yaml, load_yaml
from .test_artifacts import ArtifactTestCase

DICTIONARY = {
    "PatientDim": {"columns": {"Sex": {"description": "Legal sex, as registered.\n"}}},
    "DiagnosisEventFact": {"columns": {"PatientDurableKey": {"description": "The patient."}}},
}


class ContentsTestCase(ArtifactTestCase):
    def edit(self, rel, change):
        doc = load_yaml(self.split / rel)
        change(doc)
        dump_yaml(doc, self.split / rel)

    def contents(self, labels=("all",)):
        manifest = Manifest.load(self.split / "pullmanifest.yaml")
        from .. import artifacts

        result = artifacts.package(manifest, self.projects(labels), self.out, log=lambda _: None)
        return render(manifest, result, DICTIONARY)

    def section(self, text, title):
        start = text.index(f"\n## {title}\n")
        end = text.find("\n## ", start + 1)
        return text[start: end if end != -1 else None]


class ColumnTests(ContentsTestCase):
    def test_each_column_has_its_sql_python_and_r_types(self):
        self.set_status()
        patients = self.section(self.contents(), "Patients")
        self.assertIn("- `PatientDurableKey`: BIGINT (py: int64, r: integer64): The patient.", patients)
        self.assertIn("- `BirthDate`: DATE (py: date32, r: Date): ", patients)

    def test_a_description_is_its_own_else_the_dictionarys_else_none(self):
        self.set_status()

        def describe(doc):
            for column in doc["cohorts"][0]["columns"]:
                if column["name"] == "PatientDurableKey":
                    column["description"] = "Each patient, once."

        self.edit("sessions/Patients/pk.yaml", describe)
        patients = self.section(self.contents(), "Patients")
        self.assertIn("`PatientDurableKey`: BIGINT (py: int64, r: integer64): Each patient, once.", patients)
        self.assertIn("`Sex`: VARCHAR(50) (py: string, r: character): Legal sex, as registered.", patients)
        # p.BirthDate, which the dictionary here does not describe.
        self.assertIn("`BirthDate`: DATE (py: date32, r: Date): No description", patients)

    def test_batch_is_never_listed(self):
        self.set_status()
        self.assertNotIn("_batch", self.contents())

    def test_there_is_no_key_line(self):
        # Removed after trying it: nearly every table shares PatientDurableKey,
        # so it listed most of the pull for each table.
        self.set_status()
        self.assertNotIn("Key (testing)", self.contents())


class TableTests(ContentsTestCase):
    def test_granularity_is_its_own_else_from_its_dedup_keys(self):
        self.set_status()
        text = self.contents()
        self.assertIn("- **Granularity:** One row per PatientDurableKey",
                      self.section(text, "Patients"))
        self.edit("sessions/Patients/pk.yaml",
                  lambda doc: doc["cohorts"][0].__setitem__("granularity", "One row per patient"))
        self.assertIn("- **Granularity:** One row per patient", self.section(self.contents(), "Patients"))

    def test_the_summary_says_it_is_a_test_sample_and_counts_rows(self):
        self.set_status(runs="failed")

        def sample(doc):
            doc["smallset"] = True
            doc["stop_at_for_pk_table"] = 3000
            doc["random_pk_sample"] = True

        self.edit("sessions/Patients/pk.yaml", sample)
        text = self.contents()
        self.assertIn("**Yes: a test sample.** Each PK table holds at most 3000 rows, "
                      "a reproducible random sample", text)
        self.assertIn("| `cosmos_parquets/Patients.parquet` | 3 |", text)
        self.assertIn("- `OtherHospitalizations`: 1 of its 1 run(s) are not done (failed)", text)

    def test_separated_batches_are_tables_of_their_own(self):
        self.set_status()
        self.make_batched(separate=True)
        text = self.contents(labels=("b1of2-Female", "b2of2-Male"))
        female = self.section(text, "OtherHospitalizations_Female")
        self.assertIn("only the rows of batch Female", female)
        self.assertIn("(2 rows)", female)
        self.assertIn("\n## OtherHospitalizations_Male\n", text)

    def test_uploads_are_listed_with_their_loaded_types(self):
        self.set_status()
        uploads = self.section(self.contents(), "HospitalICDCodes")
        self.assertIn("uploads_parquets/HospitalICDCodes.parquet", uploads)
        self.assertIn("(py: string, r: character)", uploads)


class SpecificToTests(ContentsTestCase):
    def test_it_says_how_the_rows_were_chosen(self):
        pk = {
            "multiplier_levels": [
                {"multiplier": "IBDType", "strat": "Crohns", "stage": "during_build",
                 "vars": {"ICD_Value": ["K50.%"]}},
                {"multiplier": "Race", "strat": "white", "stage": "split_after_build"},
            ],
            "split_after_build": [{
                "multiplier": "Race", "strat": "white", "condition": "p.FirstRace LIKE 'White%'",
                "role": "control", "row_mult": 4, "matched_to": "CrohnsblackPatients",
            }],
        }
        self.assertEqual(specific_to(pk), [
            "Crohns (IBDType): ICD_Value K50.%",
            "white (Race): p.FirstRace LIKE 'White%', sampled at 4 times CrohnsblackPatients per batch",
        ])

    def test_a_run_table_is_specific_to_its_sessions_pk(self):
        self.set_status()
        self.edit("sessions/Patients/pk.yaml", lambda doc: doc["cohorts"][0].__setitem__(
            "multiplier_levels",
            [{"multiplier": "IBDType", "strat": "UC", "stage": "during_build", "vars": {"ICD_Value": "K51.%"}}],
        ))
        facts = self.section(self.contents(), "OtherHospitalizations")
        self.assertIn("- **Specific to:** UC (IBDType): ICD_Value K51.%", facts)

    def test_aliases_are_read_from_from_and_joins(self):
        self.assertEqual(aliases({"filter": {
            "from": ["DiagnosisEventFact as dxf"],
            "join": ["INNER JOIN PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey",
                     "INNER JOIN {{prefix}}_{{PKTable}} AS pk ON pk.a = dxf.a"],
        }}), {"dxf": "DiagnosisEventFact", "p": "PatientDim"})
