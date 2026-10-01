"""Server and local SQL rendering, checked against the real fixtures."""

from __future__ import annotations

import unittest
from pathlib import Path

from .. import local_sql, server_sql
from ..local_sql import LocalRenderError
from ..server_sql import RenderError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "split"


def pk_cohort(**overrides):
    cohort = {
        "name": "Patients",
        "type": "PK",
        "dest_table": "PKTable2",
        "columns": [{"source": "p.DurableKey", "name": "PatientDurableKey",
                     "type": "BIGINT", "nullable": False}],
        "filter": {"from": "PatientDim AS p", "join": [], "where": ["p._IsDeleted = 0"]},
    }
    cohort.update(overrides)
    return cohort


def doc_with(*cohorts, **extra):
    doc = {"project_db": "PROJECTD93A5E7", "cosmos_db": "COSMOS", "cohorts": list(cohorts)}
    doc.update(extra)
    return doc


class ServerRenderTests(unittest.TestCase):
    def render(self, doc, prefix="S/pk"):
        return server_sql.render_phase(doc, prefix)

    def test_creates_and_populates_the_global_temp(self):
        blocks, _ = self.render(doc_with(pk_cohort()))
        sql = blocks[0].sql
        self.assertIn("DROP TABLE IF EXISTS ##JVM_PKTable2;", sql)
        self.assertIn("CREATE TABLE ##JVM_PKTable2", sql)
        self.assertIn("INSERT INTO ##JVM_PKTable2", sql)
        self.assertEqual(blocks[0].block_id, "S/pk/PKTable2")
        self.assertEqual(blocks[0].meta["global_temp"], "##JVM_PKTable2")

    def test_adds_non_null_filters(self):
        blocks, _ = self.render(doc_with(pk_cohort()))
        self.assertIn("AND p.DurableKey IS NOT NULL", blocks[0].sql)

    def test_top_applies_only_to_the_root_pk(self):
        # A downstream PK joins the root's temp; limiting it too would compound
        # the restriction into an unrepresentative sample.
        downstream = pk_cohort(
            dest_table="PKTable",
            columns=[{"source": "d.Key", "name": "Key", "type": "BIGINT", "nullable": False}],
            filter={"from": "DiagnosisEventFact AS d",
                    "join": ["INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = d.PatientDurableKey"]},
        )
        doc = doc_with(pk_cohort(), downstream,
                       test_options={"smallset": True, "stop_at_for_pk_table": 500})
        blocks, _ = self.render(doc)
        by_dest = {b.dest_table: b.sql for b in blocks}
        self.assertIn("TOP (500)", by_dest["PKTable2"])
        self.assertNotIn("TOP (", by_dest["PKTable"])

    def test_top_level_test_options_limit_the_pk(self):
        # Where YAML Manager writes them. Reading only a test_options group
        # silently dropped this limit.
        doc = doc_with(pk_cohort())
        doc.update({"smallset": True, "stop_at_for_pk_table": 25})
        sql, _ = server_sql.render_cohort(doc["cohorts"][0], doc, doc["cohorts"])
        self.assertIn("TOP (25)", sql)

    def test_a_random_sample_is_ordered_by_a_hash_of_the_key(self):
        # D60: TOP alone keeps whichever rows the server reaches first.
        cohort = pk_cohort(dedup_keys=[["PatientDurableKey"]])
        doc = doc_with(cohort, smallset=True, stop_at_for_pk_table=3000, random_pk_sample=True)
        sql, _ = server_sql.render_cohort(cohort, doc, [cohort])
        self.assertIn("SELECT TOP (3000)", sql)
        self.assertIn(
            "WHERE [_deduped].[_dedup_rn] = 1\n"
            "ORDER BY HASHBYTES('SHA2_256', CAST([_deduped].[PatientDurableKey] AS NVARCHAR(4000)));",
            sql,
        )

    def test_without_dedup_the_sample_hashes_the_keys_source(self):
        cohort = pk_cohort(key_column="PatientDurableKey")
        doc = doc_with(cohort, smallset=True, stop_at_for_pk_table=10, random_pk_sample=True)
        sql, _ = server_sql.render_cohort(cohort, doc, [cohort])
        self.assertIn("ORDER BY HASHBYTES('SHA2_256', CAST(p.DurableKey AS NVARCHAR(4000)));", sql)

    def test_no_sample_order_without_a_limit_or_the_option(self):
        cohort = pk_cohort(dedup_keys=[["PatientDurableKey"]])
        for extra in ({"random_pk_sample": True}, {"smallset": True, "stop_at_for_pk_table": 5}):
            with self.subTest(extra=extra):
                sql, _ = server_sql.render_cohort(cohort, doc_with(cohort, **extra), [cohort])
                self.assertNotIn("HASHBYTES", sql)

    def test_a_sample_without_a_key_is_refused(self):
        cohort = pk_cohort()
        doc = doc_with(cohort, smallset=True, stop_at_for_pk_table=10, random_pk_sample=True)
        with self.assertRaises(RenderError):
            server_sql.render_cohort(cohort, doc, [cohort])

    def test_a_sampled_controls_limit_is_row_mult_times(self):
        # D59: it keeps row_mult times its case, so it needs that many to draw from.
        cohort = pk_cohort(split_after_build=[{"role": "control", "row_mult": 4}])
        doc = doc_with(cohort, smallset=True, stop_at_for_pk_table=3000)
        sql, _ = server_sql.render_cohort(cohort, doc, [cohort])
        self.assertIn("SELECT TOP (12000)", sql)

    def test_no_top_without_smallset(self):
        doc = doc_with(pk_cohort(), test_options={"stop_at_for_pk_table": 500})
        blocks, _ = self.render(doc)
        self.assertNotIn("TOP (", blocks[0].sql)

    def test_dedup_renders_and_is_visible(self):
        # The old generator accepted only `dedup_keys` and silently emitted no
        # deduplication at all.
        cohort = pk_cohort(dedup_key=["PatientDurableKey"])
        blocks, notes = self.render(doc_with(cohort))
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY p.DurableKey", blocks[0].sql)
        self.assertIn("[_dedup_rn] = 1", blocks[0].sql)
        self.assertTrue(any("legacy" in n for n in notes))
        self.assertTrue(any("arbitrary and may differ" in n for n in notes))

    def test_dedup_partitions_by_sources_not_column_names(self):
        # D58: inside the SELECT that names them, only source columns exist.
        # `[BillingCodeValue]` failed as an invalid column on the server, and
        # `[PatientDurableKey]` would be ambiguous beside the PK's own.
        cohort = pk_cohort(
            type="fact",
            columns=[
                {"source": "def.PatientDurableKey", "name": "PatientDurableKey", "type": "BIGINT"},
                {"source": "dt.Value", "name": "BillingCodeValue", "type": "VARCHAR(400)"},
            ],
            dedup_keys=[["PatientDurableKey", "BillingCodeValue"]],
        )
        sql, _ = server_sql.render_cohort(cohort, doc_with(cohort))
        over = sql[sql.index("ROW_NUMBER() OVER ("):]
        over = over[: over.index(") AS [_dedup_rn]")]
        self.assertIn("PARTITION BY def.PatientDurableKey, dt.Value", over)
        self.assertNotIn("[", over)

    def test_dedup_order_by_keeps_the_earliest(self):
        # D58: every recipe writes dedup_order_by, which was never read, so the
        # "first diagnosis" kept was any one.
        cohort = pk_cohort(
            columns=[
                {"source": "dxf.PatientDurableKey", "name": "PatientDurableKey", "type": "BIGINT"},
                {"source": "dxf.StartDateKey", "name": "IndexDate", "type": "INT"},
                {"source": "dxf.EncounterKey", "name": "IndexEncounter", "type": "BIGINT"},
            ],
            dedup_keys=[["PatientDurableKey"]],
            dedup_order_by=["IndexDate", "IndexEncounter DESC"],
        )
        sql, notes = server_sql.render_cohort(cohort, doc_with(cohort))
        self.assertIn(
            "PARTITION BY dxf.PatientDurableKey ORDER BY dxf.StartDateKey, dxf.EncounterKey DESC",
            sql,
        )
        self.assertFalse(any("arbitrary" in n for n in notes))

    def test_dedup_order_by_naming_no_column_is_refused(self):
        cohort = pk_cohort(dedup_keys=[["PatientDurableKey"]], dedup_order_by=["IndexDate"])
        with self.assertRaises(RenderError) as caught:
            self.render(doc_with(cohort))
        self.assertIn("IndexDate", str(caught.exception))

    def test_the_old_ordering_spellings_are_refused(self):
        for old in ("dedup_order", "order_by"):
            with self.subTest(old=old):
                cohort = pk_cohort(dedup_keys=[["PatientDurableKey"]], **{old: "p.DurableKey"})
                with self.assertRaises(RenderError) as caught:
                    self.render(doc_with(cohort))
                self.assertIn("dedup_order_by", str(caught.exception))

    def test_dedup_key_naming_a_missing_column_is_refused(self):
        with self.assertRaises(RenderError):
            self.render(doc_with(pk_cohort(dedup_keys=[["NoSuchColumn"]])))

    def test_unsubstituted_placeholder_is_refused(self):
        cohort = pk_cohort(filter={"from": "PatientDim AS p",
                                   "where": ["p.StartDateKey > {{min_date_key}}"]})
        with self.assertRaises(RenderError) as caught:
            self.render(doc_with(cohort))
        self.assertIn("min_date_key", str(caught.exception))

    def test_duplicate_column_names_are_refused(self):
        cohort = pk_cohort(columns=[
            {"source": "p.A", "name": "Dup", "type": "BIGINT"},
            {"source": "p.B", "name": "Dup", "type": "BIGINT"},
        ])
        with self.assertRaises(RenderError):
            self.render(doc_with(cohort))

    def test_missing_dest_table_is_refused(self):
        with self.assertRaises(RenderError):
            self.render(doc_with(pk_cohort(dest_table=None)))

    def test_disabled_cohorts_are_skipped_with_a_note(self):
        blocks, notes = self.render(doc_with(pk_cohort(pull_this_cycle=False)))
        self.assertEqual(blocks, [])
        self.assertTrue(any("pull_this_cycle" in n for n in notes))

    def test_setup_captures_the_runtime_instance_name(self):
        blocks = server_sql.render_setup(doc_with(), "S/setup")
        self.assertIn("@@SERVERNAME", blocks[0].sql)
        self.assertEqual(blocks[0].meta["captures"], "linked_server")


class DualCosmosTests(unittest.TestCase):
    """`cosmos_db: Dual` renders each cohort twice, against two databases."""

    def cohorts(self):
        base = pk_cohort()
        sneak = pk_cohort(name="P_sp", dest_table="P_sp", cosmos_db="COSMOS_SneakPeek")
        return base, sneak

    def test_the_sneakpeek_variant_qualifies_its_own_database(self):
        # Without this a two-part name resolves against the connected COSMOS,
        # so the SneakPeek cohort would silently read the wrong data.
        base, sneak = self.cohorts()
        blocks, _ = server_sql.render_phase(doc_with(base, sneak, cosmos_db="Dual"), "S/pk")
        by_dest = {b.dest_table: b.sql for b in blocks}
        self.assertIn("FROM dbo.PatientDim AS p", by_dest["PKTable2"])
        self.assertIn("FROM COSMOS_SneakPeek.dbo.PatientDim AS p", by_dest["P_sp"])

    def test_both_variants_are_roots_and_both_are_limited(self):
        # They are parallel chains, one per database, not competing ones.
        base, sneak = self.cohorts()
        doc = doc_with(base, sneak, cosmos_db="Dual",
                       test_options={"smallset": True, "stop_at_for_pk_table": 500})
        blocks, _ = server_sql.render_phase(doc, "S/pk")
        self.assertTrue(all("TOP (500)" in b.sql for b in blocks))

    def test_global_temps_stay_unqualified_in_both(self):
        base, sneak = self.cohorts()
        sneak["filter"]["join"] = ["INNER JOIN ##JVM_Other AS o ON 1 = 1"]
        blocks, _ = server_sql.render_phase(doc_with(base, sneak, cosmos_db="Dual"), "S/pk")
        sql = {b.dest_table: b.sql for b in blocks}["P_sp"]
        self.assertIn("INNER JOIN ##JVM_Other AS o", sql)
        self.assertNotIn("COSMOS_SneakPeek.dbo.##JVM_Other", sql)


class LocalRenderTests(unittest.TestCase):
    LINKED = "et4003vpdsql032"

    def test_shell_drops_and_creates_the_destination(self):
        blocks = local_sql.render_setup(doc_with(pk_cohort()), [pk_cohort()], "S/setup")
        sql = blocks[0].sql
        self.assertIn("DROP TABLE IF EXISTS PROJECTD93A5E7.dbo.PKTable2;", sql)
        self.assertIn("CREATE TABLE PROJECTD93A5E7.dbo.PKTable2", sql)

    def test_two_pulls_with_one_table_name_drop_only_their_own(self):
        # D163: UC_Visits and the Crohns pulls both wrote OtherDiagnoses in
        # PROJECTD93A5E7, and each one's setup dropped the other's.
        first = local_sql.render_setup(doc_with(pk_cohort(), table_prefix="ucvis"), [pk_cohort()], "S/setup")
        second = local_sql.render_setup(doc_with(pk_cohort(), table_prefix="cro"), [pk_cohort()], "S/setup")
        self.assertIn("DROP TABLE IF EXISTS PROJECTD93A5E7.dbo.ucvis_PKTable2;", first[0].sql)
        self.assertIn("DROP TABLE IF EXISTS PROJECTD93A5E7.dbo.cro_PKTable2;", second[0].sql)
        self.assertEqual(first[0].dest_table, "PKTable2")  # its parquet keeps the plain name
        sql = local_sql.render_phase(doc_with(pk_cohort(), table_prefix="ucvis"), "S/pk", self.LINKED)[0].sql
        self.assertIn("INSERT INTO PROJECTD93A5E7.dbo.ucvis_PKTable2", sql)
        self.assertIn("FROM PROJECTD93A5E7.dbo.ucvis_PKTable2;", sql)  # the Projects count

    def test_transfer_stages_then_inserts_in_a_transaction(self):
        sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
        self.assertIn("DROP TABLE IF EXISTS #Local_PKTable2;", sql)
        self.assertIn("INTO #Local_PKTable2", sql)
        self.assertIn(f"OPENQUERY(\n    [{self.LINKED}],", sql)
        self.assertIn("BEGIN TRANSACTION;", sql)
        self.assertIn("COMMIT TRANSACTION;", sql)
        # The destination is created in setup, so a run only appends.
        self.assertNotIn("CREATE TABLE PROJECTD93A5E7", sql)
        self.assertNotIn("DROP TABLE IF EXISTS PROJECTD93A5E7", sql)

    def test_slow_pull_happens_outside_the_transaction(self):
        sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
        self.assertLess(sql.index("OPENQUERY"), sql.index("BEGIN TRANSACTION"))

    def test_captures_both_row_counts(self):
        sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
        self.assertIn("'cosmos' AS [Side]", sql)
        self.assertIn("'projects' AS [Side]", sql)

    def test_measures_string_column_lengths(self):
        cohort = pk_cohort(columns=[
            {"source": "p.Name", "name": "Name", "type": "VARCHAR(400)"},
            {"source": "p.Key", "name": "Key", "type": "BIGINT"},
        ])
        sql = local_sql.render_phase(doc_with(cohort), "S/pk", self.LINKED)[0].sql
        self.assertIn("MAX(LEN([Name]))", sql)
        self.assertNotIn("MAX(LEN([Key]))", sql)

    def test_no_length_probe_without_string_columns(self):
        self.assertIsNone(local_sql.render_length_probe(pk_cohort()))

    def test_missing_linked_server_is_refused(self):
        # The instance name changes every connection, so a blank one means the
        # session identity was never captured.
        with self.assertRaises(LocalRenderError):
            local_sql.render_phase(doc_with(pk_cohort()), "S/pk", "")

    def test_missing_project_db_is_refused(self):
        doc = doc_with(pk_cohort())
        doc.pop("project_db")
        with self.assertRaises(LocalRenderError):
            local_sql.render_phase(doc, "S/pk", self.LINKED)


class FixtureRenderTests(unittest.TestCase):
    """Render the real split output rather than hand-built dictionaries."""

    @classmethod
    def setUpClass(cls):
        if not FIXTURES.is_dir():
            raise unittest.SkipTest(f"fixtures not found at {FIXTURES}")
        from ..yaml_io import load_yaml
        cls.load = staticmethod(load_yaml)

    def phase(self, name):
        return self.load(FIXTURES / "sessions" / "Patients" / name)

    def test_pk_phase_renders(self):
        blocks, _ = server_sql.render_phase(self.phase("pk.yaml"), "Patients/pk")
        self.assertEqual([b.dest_table for b in blocks], ["Patients"])
        self.assertIn("##manvalbas_Patients", blocks[0].sql)

    def test_run_phase_renders_both_sides(self):
        doc = self.phase("runs/run.yaml")
        server, _ = server_sql.render_phase(doc, "Patients/run")
        local = local_sql.render_phase(doc, "Patients/run", "et4003vpdsql032")
        transfers = [b for b in local if not b.meta.get("clears")]
        self.assertEqual([b.dest_table for b in server], [b.dest_table for b in transfers])
        self.assertTrue(all(b.side == "server" for b in server))
        self.assertTrue(all(b.side == "local" for b in local))

    def test_a_run_clears_its_batch_before_landing_it(self):
        # D52: the clear is its own block, ahead of the transfer, so a chunked
        # run can clear once and land every chunk.
        doc = self.phase("runs/run.yaml")
        local = local_sql.render_phase(doc, "Patients/run", "et4003vpdsql032")
        self.assertEqual([bool(b.meta.get("clears")) for b in local], [True, False])
        self.assertIn("DELETE FROM PROJECTD33A929.dbo.OtherHospitalizations WHERE [_batch] = 'all'", local[0].sql)
        self.assertIn(", 'all' FROM #Local_OtherHospitalizations", local[1].sql)

    def test_block_ids_are_unique_and_addressable(self):
        doc = self.phase("runs/run.yaml")
        server, _ = server_sql.render_phase(doc, "Patients/run")
        ids = [b.block_id for b in server]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(b.dest_table in b.block_id for b in server))
