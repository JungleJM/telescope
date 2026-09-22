"""Shared fixture for the runtime tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..manifest import Manifest


def _node(yaml_path: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "yaml": yaml_path,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "rows": None,
        "outputs": {},
        "error": None,
    }
    node.update(extra)
    return node


# Shaped exactly like real `makeYaml.py --export-split` output: one generated-PK
# session with two batch-product runs, one uploaded-PK session with a single run.
SAMPLE_MANIFEST: dict[str, Any] = {
    "manifest_version": 1,
    "project": {
        "name": "Manager Valid Multipliers",
        "project_folder": "Manager Valid Multipliers",
        "project_db": "PROJECTD33A929",
        "created_by": "yamlmanager",
    },
    "source": {
        "template": "YAMLs/manager_test_cases/02_valid_multipliers_batching.yaml",
        "recipes": "YAMLs/recipes.yaml",
    },
    "sessions": [
        {
            "session_id": "UCblackPatients",
            "cohort": "UCblackPatients",
            "pk_table": "UCblackPatients",
            "status": "pending",
            "phases": {
                "setup": _node("sessions/UCblackPatients/setup.yaml"),
                "upload_cohorts": _node("sessions/UCblackPatients/upload_cohorts.yaml"),
                "pk": _node(
                    "sessions/UCblackPatients/pk.yaml",
                    pk_source={"kind": "generated", "table": "UCblackPatients"},
                ),
            },
            "runs": [
                _node(
                    "sessions/UCblackPatients/runs/LA-Female.yaml",
                    run_id="UCblackPatients__LA-Female",
                    batch={
                        "name": "LA-Female",
                        "dimensions": [
                            {
                                "name": "state",
                                "kind": "column_values",
                                "column": "StateOrProvinceAbbreviation",
                                "value": "LA",
                            },
                            {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Female"},
                        ],
                        "runtime": [
                            {
                                "name": "chunk",
                                "kind": "row_chunk",
                                "rows_per_batch": 2000,
                                "applies_to": "PKTable",
                            }
                        ],
                    },
                ),
                _node(
                    "sessions/UCblackPatients/runs/LA-Male.yaml",
                    run_id="UCblackPatients__LA-Male",
                    batch={
                        "name": "LA-Male",
                        "dimensions": [
                            {
                                "name": "state",
                                "kind": "column_values",
                                "column": "StateOrProvinceAbbreviation",
                                "value": "LA",
                            },
                            {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Male"},
                        ],
                        "runtime": [],
                    },
                ),
            ],
            "multiplier": {"session_label": "UCblackPatients"},
        },
        {
            "session_id": "ClientList",
            "cohort": "ClientList",
            "pk_table": "ClientPatientList",
            "status": "pending",
            "phases": {
                "setup": _node("sessions/ClientList/setup.yaml"),
                "upload_cohorts": _node("sessions/ClientList/upload_cohorts.yaml"),
                "pk": _node(
                    "sessions/ClientList/pk.yaml",
                    pk_source={
                        "kind": "uploaded_cohort",
                        "upload_name": "ClientPatientList",
                        "table": "ClientPatientList",
                        "key_columns": ["PatientDurableKey"],
                    },
                ),
            },
            "runs": [_node("sessions/ClientList/runs/run.yaml", run_id="ClientList__run")],
        },
    ],
}


def sample_manifest() -> Manifest:
    return Manifest(copy.deepcopy(SAMPLE_MANIFEST), path=Path("split/pullmanifest.yaml"))
