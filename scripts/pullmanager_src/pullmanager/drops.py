"""Dropping a pull's Projects tables once it is packaged (D165).

A finished pull's tables fill its database, and its parquets hold them. Once
tables are named per pull (D163), they are its own: each destination, the
PK's copy and each upload's copy, all under its table prefix. They go only
after a clean Execute packaged the pull and the run scan (D152) found nothing
wrong; otherwise they stay, and the log says why. Artifacts run by hand drops
nothing.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import uploads
from .executor import iter_units
from .manifest import Manifest
from .naming import destination, projects_table
from .yaml_io import load_yaml


def pull_tables(manifest: Manifest) -> list[str]:
    """Every table the pull made in Projects, by its prefixed name; none when
    it has no table prefix (split before D163), whose names may be shared."""
    prefix = str(manifest.project.get("table_prefix") or "").strip()
    if not prefix:
        return []
    names: set[str] = set()
    for session in manifest.sessions:
        for _, _, path in iter_units(manifest, session):
            doc = (load_yaml(path) or {}) if path.is_file() else {}
            for cohort in doc.get("cohorts") or []:
                if isinstance(cohort, dict) and cohort.get("dest_table"):
                    names.add(projects_table(str(cohort["dest_table"]), prefix))
            for upload in uploads.enabled_uploads(doc):
                names.add(projects_table(uploads.copy_table(uploads.upload_dest(upload)), prefix))
    # Never a table that is not the pull's own.
    return sorted(name for name in names if name.lower().startswith(f"{prefix.lower()}_"))


def why_kept(manifest: Manifest) -> str:
    """Why the pull's tables must stay, or '' if they can go."""
    from .scan import count_problems, scan_manifest

    if not str(manifest.project.get("table_prefix") or "").strip():
        return ("it was split before tables were named per pull (D163), so a name may be another "
                "pull's. Drop its tables in clear_projects_db once you are sure")
    problems = scan_manifest(manifest)
    if problems:
        return (f"the run scan found {count_problems(problems)} problem(s) "
                f"({', '.join(sorted(problems))}). Scan runs says which; re-pull what is short, "
                "and they go when it packages cleanly")
    return ""


def drop_tables(manifest: Manifest, connection: Any, say: Callable[[str], None] = print) -> list[str]:
    """Drop the pull's tables, and record that it was done."""
    project_db = str(manifest.project.get("project_db") or "")
    dropped = []
    cursor = connection.cursor()
    for name in pull_tables(manifest):
        cursor.execute(f"DROP TABLE IF EXISTS {destination(project_db, name)};")
        dropped.append(name)
    connection.commit()
    manifest.data["tables_dropped"] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "database": project_db,
        "tables": dropped,
    }
    manifest.save()
    say(f"Dropped its {len(dropped)} table(s) from {project_db}: the parquets hold them. It can "
        "now only be re-pulled from the start.")
    return dropped
