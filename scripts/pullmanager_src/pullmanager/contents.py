"""`contents.md`: every packaged table, for people and for the VM's AI (D73).

A pull summary, then per table its granularity, what it is specific to, its
description and its columns. Each column shows its SQL type as the table
holds it and its type once loaded in Python and R, because the types are for
writing code against the files. Descriptions are the template's own, else the
data dictionary's for the column's source, else "No description": nothing is
guessed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import BATCH_COLUMN, Plan, TableSpec
from .manifest import Manifest
from .normalize import normalize_bool, normalize_dedup_keys
from .server_sql import test_option
from .yaml_io import load_yaml

NO_DESCRIPTION = "No description"
NO_GRANULARITY = "No granularity given"

# SQL type -> (Python, as pyarrow and pandas-with-Arrow read it; R, as arrow reads it
# with arrow.int64_downcast = FALSE, D75).
LOADED_TYPES = {
    "BIGINT": ("int64", "integer64"),
    "INT": ("int32", "integer"),
    "SMALLINT": ("int16", "integer"),
    "TINYINT": ("int16", "integer"),
    "BIT": ("bool", "logical"),
    "FLOAT": ("float64", "double"),
    "REAL": ("float32", "double"),
    "DECIMAL": ("decimal128", "double"),
    "NUMERIC": ("decimal128", "double"),
    "MONEY": ("decimal128", "double"),
    "DATE": ("date32", "Date"),
    "DATETIME": ("timestamp[us]", "POSIXct"),
    "DATETIME2": ("timestamp[us]", "POSIXct"),
    "SMALLDATETIME": ("timestamp[us]", "POSIXct"),
    "TIME": ("time64[us]", "hms"),
    "BINARY": ("binary", "arrow_binary"),
    "VARBINARY": ("binary", "arrow_binary"),
}
# An upload's columns are known by their Arrow type only.
ARROW_TO_R = {
    "int64": "integer64", "int32": "integer", "int16": "integer", "int8": "integer",
    "bool": "logical", "double": "double", "float": "double", "string": "character",
    "large_string": "character", "date32[day]": "Date", "binary": "arrow_binary",
}


def loaded_types(sql_type: str) -> tuple[str, str]:
    base = sql_type.split("(")[0].upper()
    if base in LOADED_TYPES:
        return LOADED_TYPES[base]
    return ("string", "character")


def dictionary_path() -> Path | None:
    """The data dictionary makeYaml validates against: the bundled copy, or
    `YAMLMANAGER_DATA_DICTIONARY`."""
    chosen = os.environ.get("YAMLMANAGER_DATA_DICTIONARY")
    if chosen:
        return Path(chosen)
    try:
        from .launcher import locate_tools

        return locate_tools().make_yaml.parent.parent / "YAMLs" / "datadictionary.yaml"
    except Exception:
        return None


def load_dictionary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    data = load_yaml(path) or {}
    return data.get("DataDictionary") or data


def tidy(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def aliases(cohort: dict[str, Any]) -> dict[str, str]:
    """`dxf` -> `DiagnosisEventFact`, from the table's `from` and `join` lines."""
    found: dict[str, str] = {}
    filters = cohort.get("filter") or {}
    froms = filters.get("from") or []
    for line in [froms] if isinstance(froms, str) else froms:
        match = re.match(r"\s*\[?(\w+)\]?(?:\s+(?:AS\s+)?(\w+))?\s*$", str(line), re.IGNORECASE)
        if match:
            found[match.group(2) or match.group(1)] = match.group(1)
    for line in filters.get("join") or []:
        for table, alias in re.findall(
            r"JOIN\s+\[?(\w+)\]?\s+(?:AS\s+)?(\w+)", str(line), re.IGNORECASE
        ):
            if alias.upper() != "ON":
                found[alias] = table
    return found


def column_description(column: dict[str, Any], table_aliases: dict[str, str],
                       dictionary: dict[str, Any]) -> str:
    own = tidy(column.get("description"))
    if own:
        return own
    match = re.fullmatch(r"\s*(\w+)\.\[?(\w+)\]?\s*", str(column.get("source") or ""))
    if match and match.group(1) in table_aliases:
        table = dictionary.get(table_aliases[match.group(1)]) or {}
        meta = (table.get("columns") or {}).get(match.group(2)) or {}
        text = tidy(meta.get("description") if isinstance(meta, dict) else "")
        if text:
            return text
    return NO_DESCRIPTION


def granularity(cohort: dict[str, Any]) -> str:
    own = tidy(cohort.get("granularity"))
    if own:
        return own
    try:
        key_sets, _ = normalize_dedup_keys(cohort)
    except Exception:
        key_sets = []
    if key_sets:
        return "One row per " + " and ".join(key_sets[0])
    return NO_GRANULARITY


def values_words(values: Any) -> str:
    values = values if isinstance(values, list) else [values]
    return ", ".join(str(v) for v in values)


def specific_to(pk: dict[str, Any]) -> list[str]:
    """What the session's population is, from its PK's multiplier levels (D73)."""
    lines: list[str] = []
    for level in pk.get("multiplier_levels") or []:
        if level.get("stage") != "during_build":
            continue
        variables = "; ".join(
            f"{name} {values_words(value)}" for name, value in (level.get("vars") or {}).items()
        )
        lines.append(f"{level.get('strat')} ({level.get('multiplier')}): {variables}")
    for item in pk.get("split_after_build") or []:
        condition = item.get("condition") or item.get("where") or ""
        text = f"{item.get('strat')} ({item.get('multiplier')}): {condition}"
        if item.get("role") == "control" and item.get("row_mult"):
            try:
                times = f"{float(item['row_mult']):g}"
            except (TypeError, ValueError):
                times = str(item["row_mult"])
            text += f", sampled at {times} times {item.get('matched_to')} per batch"
        lines.append(text)
    return lines


def pull_summary(manifest: Manifest, plan: Plan) -> list[str]:
    project = manifest.project
    folders = sorted({spec.folder for spec in plan.tables if spec.kind != "upload"})
    finished = [
        str(node.data.get("finished_at")) for _, node in manifest.iter_nodes()
        if node.data.get("finished_at")
    ]
    pk_doc = next((spec.doc for spec in plan.tables if spec.kind == "pk"), {})
    smallset = normalize_bool(test_option(pk_doc, "smallset")) if pk_doc else False
    if smallset:
        limit = test_option(pk_doc, "stop_at_for_pk_table")
        how = ("a reproducible random sample (hash of the key)"
               if normalize_bool(test_option(pk_doc, "random_pk_sample")) else "the first rows found")
        sample = f"**Yes: a test sample.** Each PK table holds at most {limit} rows, {how}."
    else:
        sample = "No: the whole population the filters select."
    refresh = manifest.cosmos_refresh
    lines = [
        f"# Contents: {project.get('project_folder') or project.get('name') or ''}".rstrip(),
        "",
        "## About This Pull",
        "",
        f"- **Project database:** {project.get('project_db')}",
        f"- **Pulled from:** {', '.join(folders) or 'none'}",
        f"- **Last finished:** {max(finished) if finished else 'unknown'}",
        "- **Cosmos refreshed:** "
        + (", ".join(f"{db} {when}" for db, when in sorted(refresh.items())) or "unknown"),
        f"- **Test sample:** {sample}",
        f"- **Packaged:** {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| File | Rows |",
        "| --- | ---: |",
    ]
    for spec in plan.tables:
        for part in spec.parts:
            if part.path is not None:
                lines.append(f"| `{spec.folder}/{part.path.name}` | {part.rows:,} |")
    if plan.left_out:
        lines += ["", "**Not packaged** (not finished, or not a table):", ""]
        lines += [f"- `{dest}`: {why}" for dest, why in plan.left_out]
    return lines


def table_section(spec: TableSpec, pk: dict[str, Any], dictionary: dict[str, Any]) -> list[str]:
    cohort = spec.cohort
    lines: list[str] = []
    for part in spec.parts:
        title = part.path.stem if part.path is not None else spec.dest
        lines += ["", f"## {title}", ""]
        lines.append(f"- **File:** `parquets/{spec.folder}/{title}.parquet` ({part.rows:,} rows)")
        if spec.kind == "upload":
            lines.append("- **Granularity:** " + (tidy(cohort.get("granularity")) or NO_GRANULARITY))
            lines.append("- **Description:** " + (tidy(cohort.get("description")) or NO_DESCRIPTION)
                         + " (an upload, as it was supplied)")
        else:
            lines.append(f"- **Granularity:** {granularity(cohort)}")
            specifics = specific_to(pk)
            if part.label:
                specifics.append("only the rows of batch " + part.label.replace("_", ", "))
            if specifics:
                lines.append("- **Specific to:** " + "; ".join(specifics))
            lines.append(f"- **Description:** {tidy(cohort.get('description')) or NO_DESCRIPTION}")
        lines += ["", "Columns:", ""]
        lines += column_lines(spec, dictionary)
    return lines


def column_lines(spec: TableSpec, dictionary: dict[str, Any]) -> list[str]:
    if spec.kind == "upload":
        return [
            f"- `{name}` (py: {arrow}, r: {ARROW_TO_R.get(arrow, 'character')})"
            for name, arrow in spec.columns
        ]
    declared = {
        str(column.get("name")): column
        for column in spec.cohort.get("columns") or [] if isinstance(column, dict)
    }
    table_aliases = aliases(spec.cohort)
    lines = []
    for name, sql_type in spec.columns:
        if name == BATCH_COLUMN:
            continue
        py, r = loaded_types(sql_type)
        text = column_description(declared.get(name, {}), table_aliases, dictionary)
        lines.append(f"- `{name}`: {sql_type} (py: {py}, r: {r}): {text}")
    return lines


def render(manifest: Manifest, plan: Plan, dictionary: dict[str, Any] | None = None) -> str:
    dictionary = dictionary if dictionary is not None else load_dictionary(dictionary_path())
    pks = {spec.session: spec.cohort for spec in plan.tables if spec.kind == "pk"}
    lines = pull_summary(manifest, plan)
    lines += ["", "---", "", "Each column: its name, its SQL type in Projects, its type once "
              "loaded in Python (py) and R (r), and what it holds."]
    for spec in plan.tables:
        lines += table_section(spec, pks.get(spec.session, spec.cohort if spec.kind == "pk" else {}),
                               dictionary)
    return "\n".join(lines).rstrip() + "\n"
