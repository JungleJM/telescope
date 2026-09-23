#!/usr/bin/env python3
"""
Compile human-authored YAML Manager templates into VM-facing YAML artifacts.

The file is intentionally self-contained for the VM copy-update workflow.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any


OUTPUT_SUFFIX = "_Full"
PREYAML_SUFFIX = "_preyaml"
EXPANDED_PREYAML_SUFFIX = "_preyaml_expanded"
WILDCARD_CHARS = ("%", "_", "[", "]")
DEFAULT_MANIFEST_PATH = Path("split") / "pullmanifest.yaml"
DEFAULT_SPLIT_DIR = Path("split")


# =============================================================================
# Result structures
# =============================================================================


@dataclass
class Message:
    level: str
    code: str
    message: str
    context: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


@dataclass
class CompileResult:
    ok: bool = True
    errors: list[Message] = field(default_factory=list)
    warnings: list[Message] = field(default_factory=list)
    finished_yaml: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=lambda: {"nodes": [], "edges": []})
    output_path: str | None = None

    def error(self, code: str, message: str, context: str = "") -> None:
        self.ok = False
        self.errors.append(Message("ERROR", code, message, context))

    def warn(self, code: str, message: str, context: str = "") -> None:
        self.warnings.append(Message("WARN", code, message, context))


@dataclass
class SplitPhase:
    name: str
    yaml: str
    status: str = "pending"
    pk_source: dict[str, Any] | None = None
    rows: int | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "yaml": self.yaml,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rows": self.rows,
            "outputs": self.outputs,
            "error": self.error,
        }
        if self.pk_source is not None:
            out["pk_source"] = self.pk_source
        return out


@dataclass
class SplitRun:
    run_id: str
    yaml: str
    status: str = "pending"
    batch: dict[str, Any] | None = None
    rows: int | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "yaml": self.yaml,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rows": self.rows,
            "outputs": self.outputs,
            "error": self.error,
        }
        if self.batch is not None:
            out["batch"] = self.batch
        return out


@dataclass
class SplitSession:
    session_id: str
    cohort: str
    pk_table: str | None
    phases: dict[str, SplitPhase]
    runs: list[SplitRun]
    status: str = "pending"
    multiplier: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "cohort": self.cohort,
            "pk_table": self.pk_table,
            "status": self.status,
            "phases": {name: phase.to_dict() for name, phase in self.phases.items()},
            "runs": [run.to_dict() for run in self.runs],
        }
        if self.multiplier is not None:
            out["multiplier"] = self.multiplier
        return out


@dataclass
class SplitPlan:
    project: dict[str, Any]
    source: dict[str, Any]
    sessions: list[SplitSession]
    manifest_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "project": self.project,
            "source": self.source,
            "sessions": [session.to_dict() for session in self.sessions],
        }


# =============================================================================
# YAML loading and writing
# =============================================================================


def _yaml_backend():
    try:
        from ruamel.yaml import YAML  # type: ignore

        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.default_flow_style = False
        return "ruamel", yaml
    except Exception:
        pass

    try:
        import yaml  # type: ignore

        return "pyyaml", yaml
    except Exception:
        return "ruby", None


def load_yaml(path: str | Path) -> Any:
    path = Path(path)
    backend, yaml_mod = _yaml_backend()
    if backend == "ruamel":
        with path.open("r", encoding="utf-8") as handle:
            data = yaml_mod.load(handle)
        return _plain_data(data)
    if backend == "pyyaml":
        with path.open("r", encoding="utf-8") as handle:
            return yaml_mod.safe_load(handle)

    # Local-development fallback for machines without Python YAML packages.
    cmd = [
        "ruby",
        "-ryaml",
        "-rjson",
        "-e",
        "print JSON.generate(YAML.load_file(ARGV[0]))",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except Exception as exc:
        raise RuntimeError(
            "No Python YAML backend available. Install ruamel.yaml or pyyaml."
        ) from exc
    return json.loads(proc.stdout)


def dump_yaml(data: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backend, yaml_mod = _yaml_backend()
    if backend == "ruamel":
        with path.open("w", encoding="utf-8") as handle:
            yaml_mod.dump(data, handle)
        return
    if backend == "pyyaml":
        with path.open("w", encoding="utf-8") as handle:
            yaml_mod.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
        return

    # Minimal writer fallback. This keeps local TDD runnable; production should
    # use ruamel.yaml or pyyaml.
    path.write_text(_simple_yaml_dump(data), encoding="utf-8")


def _plain_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_data(v) for v in value]
    return value


def _simple_yaml_dump(data: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_simple_yaml_dump(value, indent + 2).rstrip())
            else:
                lines.append(f"{pad}{key}: {_format_scalar(value)}")
        return "\n".join(lines) + "\n"
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = True
                for key, value in item.items():
                    bullet = "- " if first else "  "
                    if isinstance(value, (dict, list)):
                        lines.append(f"{pad}{bullet}{key}:")
                        lines.append(_simple_yaml_dump(value, indent + 4).rstrip())
                    else:
                        lines.append(f"{pad}{bullet}{key}: {_format_scalar(value)}")
                    first = False
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(_simple_yaml_dump(item, indent + 2).rstrip())
            else:
                lines.append(f"{pad}- {_format_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{pad}{_format_scalar(data)}\n"


def dump_yaml_text(data: Any) -> str:
    return _simple_yaml_dump(data)


def _format_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in [":", "#", "{", "}", "[", "]", "%"]):
        return json.dumps(text)
    return text


# =============================================================================
# Normalization and recipe import
# =============================================================================


def script_root() -> Path:
    return Path(__file__).resolve().parent


def project_root() -> Path:
    return script_root().parent


def default_template_path() -> Path:
    return project_root() / "YAMLs" / "template.yaml"


def default_recipes_path() -> Path:
    return project_root() / "YAMLs" / "recipes.yaml"


def missing_template_message(template_path: Path) -> str | None:
    """A useful sentence for a template that does not exist, or None if it does.

    The bundle ships the template as template.yaml.example so updates never land
    on a real one -- which means running without --template from an extracted
    bundle points at a file that is deliberately absent.
    """
    path = Path(template_path)
    if path.is_file():
        return None
    example = path.with_name(path.name + ".example")
    if example.is_file():
        return (
            f"No template at {path}. Pass --template with your own file, or copy "
            f"{example.name} to start one."
        )
    return f"No template at {path}. Pass --template with the file to use."


def normalize_template(template: dict[str, Any], result: CompileResult) -> dict[str, Any]:
    template = copy.deepcopy(template or {})
    for section_name in ("cosmos_vars", "project_vars", "test_options"):
        section = template.get(section_name)
        if isinstance(section, dict):
            for key, value in section.items():
                template.setdefault(key, value)
    vars_block = dict(template.get("vars") or {})
    run_vars = template.get("run_vars")
    if isinstance(run_vars, dict):
        vars_block = merge_vars(run_vars, vars_block)
    for legacy_key in ("min_date_key", "max_date_key"):
        if legacy_key in template and legacy_key not in vars_block:
            vars_block[legacy_key] = template[legacy_key]
            result.warn(
                "legacy_global_var",
                f"`{legacy_key}` should live under top-level `vars`.",
                legacy_key,
            )
    template["vars"] = vars_block
    if "project_folder" not in template and "projaect_folder" in template:
        template["project_folder"] = template["projaect_folder"]
        result.warn(
            "legacy_project_folder",
            "`projaect_folder` is misspelled; use `project_folder`.",
            "project_folder",
        )
    return template


def recipe_index(recipes_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {recipe["name"]: recipe for recipe in recipes_doc.get("recipes", []) or []}


def import_recipes(template: dict[str, Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
    recipes = recipe_index(recipes_doc)
    imported: list[dict[str, Any]] = []
    for idx, cohort in enumerate(template.get("cohorts", []) or []):
        if not isinstance(cohort, dict):
            result.error("invalid_cohort", "Each cohort must be a mapping.", f"cohorts[{idx}]")
            continue
        if "recipe" in cohort:
            recipe_name = cohort["recipe"]
            if recipe_name not in recipes:
                result.error("missing_recipe", f"Recipe `{recipe_name}` was not found.", f"cohorts[{idx}]")
                continue
            merged = deep_merge(copy.deepcopy(recipes[recipe_name]), cohort)
            merged["_recipe"] = recipe_name
            merged.pop("recipe", None)
        else:
            merged = copy.deepcopy(cohort)
        if not merged.get("name"):
            merged["name"] = merged.get("dest_table") or merged.get("_recipe") or f"cohort_{idx + 1}"
            result.warn("default_name", "Cohort had no name; a generated name was assigned.", f"cohorts[{idx}]")
        if not merged.get("dest_table"):
            merged["dest_table"] = merged["name"]
            result.warn(
                "default_dest_table",
                f"`dest_table` defaulted to cohort name `{merged['name']}`.",
                merged["name"],
            )
        imported.append(merged)
    return imported


def deep_merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = copy.deepcopy(base)
        for key, value in overlay.items():
            if key in out and isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    return copy.deepcopy(overlay)


def output_path_for(template: dict[str, Any], suffix: str = OUTPUT_SUFFIX, out: str | None = None) -> Path:
    if out:
        return Path(out)
    name = str(template.get("project_folder") or "finished").strip() or "finished"
    clean = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return project_root() / "YAMLs" / f"{clean}{suffix}.yaml"


# =============================================================================
# Inference
# =============================================================================


JINJA_EXPR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
TABLE_ALIAS_RE = re.compile(r"##JVM_\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def iter_strings(value: Any, path: str = ""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from iter_strings(item, f"{path}[{idx}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{path}.{key}" if path else str(key))


def infer_required_vars(cohort: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path, text in iter_strings(cohort):
        for expr in JINJA_EXPR_RE.findall(text):
            for var in vars_from_expr(expr):
                found.setdefault(var, []).append(path)
    return found


def vars_from_expr(expr: str) -> set[str]:
    expr = expr.strip()
    if expr.startswith("sql_condition"):
        inside = expr[len("sql_condition") :].strip()
        match = re.match(r"^\((.*)\)$", inside)
        if not match:
            return set()
        args = split_args(match.group(1))
        if len(args) >= 2:
            return {args[1].strip()}
        return set()
    if "|" in expr:
        left = expr.split("|", 1)[0].strip()
        return {left} if IDENT_RE.fullmatch(left) else set()
    ignored = {"sql_condition", "true", "false", "none", "null"}
    return {tok for tok in IDENT_RE.findall(expr) if tok not in ignored}


def split_args(arg_text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in arg_text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == ",":
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current or arg_text.endswith(","):
        args.append("".join(current).strip())
    return args


def infer_table_inputs(cohort: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    strings = list(iter_strings(cohort))
    for path, text in strings:
        for table_var, alias in TABLE_ALIAS_RE.findall(text):
            inputs.setdefault(table_var, {"alias": alias, "paths": [], "required_columns": []})
            inputs[table_var]["paths"].append(path)
    full_text = "\n".join(text for _, text in strings)
    for table_var, meta in inputs.items():
        alias = re.escape(meta["alias"])
        cols = sorted(set(re.findall(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)\b", full_text)))
        meta["required_columns"] = cols
    return inputs


def output_columns(cohort: dict[str, Any]) -> list[str]:
    cols: list[str] = []
    for column in cohort.get("columns", []) or []:
        if not isinstance(column, dict):
            continue
        name = column.get("name")
        if not name and column.get("source"):
            name = str(column["source"]).split(".")[-1]
        if name:
            cols.append(str(name))
    return cols


def analyze_cohorts(cohorts: list[dict[str, Any]]) -> dict[str, Any]:
    required_vars = {}
    table_inputs = {}
    required_table_columns = {}
    outputs = {}
    for cohort in cohorts:
        name = cohort.get("name")
        required_vars[name] = infer_required_vars(cohort)
        table_inputs[name] = infer_table_inputs(cohort)
        required_table_columns[name] = {
            var: meta["required_columns"] for var, meta in table_inputs[name].items()
        }
        outputs[cohort.get("dest_table", name)] = output_columns(cohort)
    return {
        "required_vars": required_vars,
        "table_inputs": table_inputs,
        "required_table_columns": required_table_columns,
        "output_columns": outputs,
    }


# =============================================================================
# Validation and variable resolution
# =============================================================================


def merge_vars(*scopes: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for scope in scopes:
        if scope:
            merged.update(scope)
    return merged


def find_pk_table(cohorts: list[dict[str, Any]], result: CompileResult, group_key: str | None = None) -> str | None:
    scope = [c for c in cohorts if group_key is None or c.get("_group_key", "") == group_key]
    pk = [c for c in scope if str(c.get("type", "")).lower() == "pk"]
    if len(pk) == 1:
        return pk[0].get("dest_table") or pk[0].get("name")
    if len(pk) > 1:
        result.error(
            "multiple_pk_cohorts",
            "Cannot infer PKTable because multiple type: PK cohorts exist.",
            ", ".join(str(c.get("name")) for c in pk),
        )
    return None


def find_uploaded_pk_table(template: dict[str, Any], result: CompileResult) -> str | None:
    pk_uploads = [
        upload for upload in template.get("upload_cohorts", []) or []
        if isinstance(upload, dict) and str(upload.get("type", "")).lower() == "pk"
    ]
    if len(pk_uploads) > 1:
        result.error(
            "multiple_uploaded_pk",
            "Only one upload cohort may be marked `type: pk`.",
            ", ".join(str(upload.get("name")) for upload in pk_uploads),
        )
        return None
    if not pk_uploads:
        return None
    upload = pk_uploads[0]
    if not upload.get("key_columns"):
        result.error(
            "uploaded_pk_missing_keys",
            "Uploaded PK cohort must declare `key_columns`.",
            str(upload.get("name")),
        )
    return str(upload.get("dest_table") or upload.get("name"))


def validate_and_resolve(
    template: dict[str, Any],
    recipes_doc: dict[str, Any],
    cohorts: list[dict[str, Any]],
    analysis: dict[str, Any],
    result: CompileResult,
    base_dir: Path,
) -> list[dict[str, Any]]:
    uploads = upload_index(template)
    table_schemas: dict[str, list[str] | None] = {table: cols for table, cols in analysis["output_columns"].items()}
    table_schemas.update(upload_schemas(template, uploads, result, base_dir))
    uploaded_pk_table = find_uploaded_pk_table(template, result)
    generated_pk = [c for c in cohorts if str(c.get("type", "")).lower() == "pk"]
    if uploaded_pk_table and generated_pk:
        result.error(
            "uploaded_pk_with_generated_pk",
            "A template may not define both an uploaded PK cohort and generated type: PK cohorts.",
            uploaded_pk_table,
        )
    resolved_cohorts: list[dict[str, Any]] = []
    for cohort in cohorts:
        name = cohort.get("name")
        pk_table = find_pk_table(cohorts, result, cohort.get("_group_key", "")) or uploaded_pk_table
        auto_vars = {}
        required = analysis["required_vars"].get(name, {})
        if "PKTable" in required and "PKTable" not in (cohort.get("vars") or {}) and pk_table:
            auto_vars["PKTable"] = pk_table
        vars_for_cohort = merge_vars(template.get("vars"), upload_vars(template), auto_vars, cohort.get("vars"))
        for var, paths in required.items():
            if var not in vars_for_cohort:
                result.error(
                    "missing_variable",
                    f"Cohort `{name}` requires variable `{var}`, but no value was provided.",
                    ", ".join(paths),
                )
        for table_var, cols in analysis["required_table_columns"].get(name, {}).items():
            table_name = vars_for_cohort.get(table_var)
            if not table_name:
                continue
            table_name = str(table_name)
            if table_name not in table_schemas:
                result.error(
                    "missing_input_table",
                    f"Cohort `{name}` uses `{table_var}={table_name}`, but no cohort/upload table provides it.",
                    table_var,
                )
                continue
            if table_schemas[table_name] is None:
                continue
            missing = [col for col in cols if col not in table_schemas[table_name]]
            if missing:
                result.error(
                    "missing_input_column",
                    f"Cohort `{name}` uses `{table_var}={table_name}`, but `{table_name}` is missing columns: {', '.join(missing)}.",
                    table_var,
                )
        resolved = copy.deepcopy(cohort)
        resolved["_resolved_vars"] = vars_for_cohort
        resolved_cohorts.append(resolved)
    validate_upload_references(template, uploads, analysis, result, base_dir)
    validate_multipliers(template, cohorts, table_schemas, result)
    validate_batching(template, recipes_doc, cohorts, table_schemas, result)
    return resolved_cohorts


def upload_index(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    uploads = {}
    for upload in template.get("upload_cohorts", []) or []:
        if isinstance(upload, dict) and upload.get("name"):
            item = copy.deepcopy(upload)
            item.setdefault("dest_table", item["name"])
            item.setdefault("scope", "global")
            item.setdefault("push_this_cycle", True)
            uploads[item["dest_table"]] = item
            uploads[item["name"]] = item
    return uploads


def upload_vars(template: dict[str, Any]) -> dict[str, Any]:
    # Upload variables are primarily user-defined under vars. This hook exists
    # for future derived aliases.
    return {}


def upload_schemas(
    template: dict[str, Any],
    uploads: dict[str, dict[str, Any]],
    result: CompileResult,
    base_dir: Path,
) -> dict[str, list[str] | None]:
    schemas: dict[str, list[str] | None] = {}
    seen: set[int] = set()
    for upload in uploads.values():
        ident = id(upload)
        if ident in seen:
            continue
        seen.add(ident)
        dest = str(upload.get("dest_table") or upload.get("name"))
        file_type = str(upload.get("file_type", "")).lower()
        if file_type == "csv" and upload.get("file_loc"):
            file_path = resolve_file(base_dir, upload["file_loc"])
            if not file_path.exists():
                result.error("missing_upload_file", f"Upload file not found: {file_path}", dest)
                schemas[dest] = None
                continue
            try:
                with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.reader(handle)
                    schemas[dest] = next(reader, [])
            except Exception as exc:
                result.error("upload_read_error", f"Could not read upload CSV `{file_path}`: {exc}", dest)
                schemas[dest] = []
        elif file_type in ("dbtable", "parquet"):
            schema = upload.get("columns") or upload.get("schema") or []
            if schema and isinstance(schema[0], dict):
                schemas[dest] = [str(c.get("name")) for c in schema if c.get("name")]
            else:
                schemas[dest] = [str(c) for c in schema] if schema else []
                if not schema:
                    result.warn("upload_schema_unknown", f"Upload `{dest}` has no locally discoverable schema.", dest)
        else:
            schemas[dest] = []
    return schemas


def resolve_file(base_dir: Path, file_loc: str) -> Path:
    path = Path(file_loc)
    if path.is_absolute():
        return path
    candidates = [base_dir / path, project_root() / path, project_root() / "YAMLs" / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def validate_upload_references(
    template: dict[str, Any],
    uploads: dict[str, dict[str, Any]],
    analysis: dict[str, Any],
    result: CompileResult,
    base_dir: Path,
) -> None:
    referenced = set()
    for cohort_inputs in analysis.get("table_inputs", {}).values():
        for table_var in cohort_inputs:
            value = template.get("vars", {}).get(table_var)
            if value:
                referenced.add(str(value))
    for name in referenced:
        upload = uploads.get(name)
        if upload and upload.get("push_this_cycle") is False and not upload.get("assume_exists"):
            result.error(
                "upload_not_pushed",
                f"Upload `{name}` is referenced but has push_this_cycle: false.",
                name,
            )


# =============================================================================
# Rendering
# =============================================================================


def render_sql_condition(column: str, value: Any, result: CompileResult, context: str = "") -> str:
    values = value if isinstance(value, list) else [value]
    values = [str(v) for v in values]
    has_wildcard = any(any(ch in v for ch in WILDCARD_CHARS) for v in values)
    for v in values:
        if "_" in v and has_wildcard:
            result.warn(
                "like_underscore",
                f"`_` in `{v}` will be treated as a SQL LIKE single-character wildcard. Use [_] for a literal underscore.",
                context,
            )
    if has_wildcard:
        parts = [f"{column} LIKE {sql_quote(v)}" for v in values]
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
    if len(values) == 1:
        return f"{column} = {sql_quote(values[0])}"
    return f"{column} IN ({', '.join(sql_quote(v) for v in values)})"


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_value(value: Any, vars_for_cohort: dict[str, Any], result: CompileResult, context: str = "") -> Any:
    if isinstance(value, str):
        return render_string(value, vars_for_cohort, result, context)
    if isinstance(value, list):
        return [render_value(item, vars_for_cohort, result, f"{context}[{idx}]") for idx, item in enumerate(value)]
    if isinstance(value, dict):
        return {k: render_value(v, vars_for_cohort, result, f"{context}.{k}" if context else str(k)) for k, v in value.items()}
    return value


def render_string(text: str, vars_for_cohort: dict[str, Any], result: CompileResult, context: str = "") -> str:
    def repl(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr.startswith("sql_condition"):
            args = split_args(re.match(r"^sql_condition\((.*)\)$", expr).group(1)) if re.match(r"^sql_condition\((.*)\)$", expr) else []
            if len(args) < 2:
                result.error("bad_sql_condition", f"Could not parse sql_condition expression `{expr}`.", context)
                return match.group(0)
            column = strip_quotes(args[0])
            var_name = args[1].strip()
            if var_name not in vars_for_cohort:
                result.error("missing_variable", f"`sql_condition` references missing variable `{var_name}`.", context)
                return match.group(0)
            return render_sql_condition(column, vars_for_cohort[var_name], result, context)
        if "|" in expr and "sql_condition" in expr:
            var_name, rest = [part.strip() for part in expr.split("|", 1)]
            match_args = re.search(r"sql_condition\((.*)\)", rest)
            if match_args and var_name in vars_for_cohort:
                column = strip_quotes(split_args(match_args.group(1))[0])
                return render_sql_condition(column, vars_for_cohort[var_name], result, context)
        if expr in vars_for_cohort:
            return str(vars_for_cohort[expr])
        result.error("missing_variable", f"Missing variable `{expr}`.", context)
        return match.group(0)

    return JINJA_EXPR_RE.sub(repl, text)


def strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def render_cohorts(cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    rendered = []
    for cohort in cohorts:
        vars_for_cohort = cohort.get("_resolved_vars", {})
        clean = {k: v for k, v in cohort.items() if not k.startswith("_")}
        rendered.append(render_value(clean, vars_for_cohort, result, str(cohort.get("name"))))
    return rendered


# =============================================================================
# Expansion: multipliers, batching, cosmos
# =============================================================================


def expand_multipliers(template: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    multipliers = template.get("multipliers", []) or []
    if not multipliers:
        return cohorts
    level_sets = []
    for mult in multipliers:
        levels = mult.get("levels", []) if isinstance(mult, dict) else []
        level_sets.append([(mult, level) for level in levels])
    expanded: list[dict[str, Any]] = []
    for combo in product(*level_sets):
        prefix = "".join(str(level.get("strat", "")) for _, level in combo)
        group_vars: dict[str, Any] = {}
        group_meta: list[dict[str, Any]] = []
        for mult, level in combo:
            if mult.get("stage") == "during_build":
                group_vars.update(level.get("vars") or {})
            group_meta.append({"name": mult.get("name"), "stage": mult.get("stage"), "level": level})
        for cohort in cohorts:
            new = copy.deepcopy(cohort)
            base_name = str(new.get("name"))
            base_dest = str(new.get("dest_table", base_name))
            new["name"] = f"{prefix}{base_name}" if prefix else base_name
            new["dest_table"] = f"{prefix}{base_dest}" if prefix else base_dest
            new["vars"] = merge_vars(group_vars, new.get("vars"))
            new["_group_key"] = prefix
            new["_multiplier_group"] = group_meta
            split_filters = [
                split_after_build_filter(mult, level, result)
                for mult, level in combo
                if mult.get("stage") == "split_after_build"
                and mult.get("applies_to") == "PKTable"
                and str(new.get("type", "")).lower() == "pk"
            ]
            split_filters = [item for item in split_filters if item]
            if split_filters:
                new["split_after_build"] = split_filters
            expanded.append(new)
    return expanded


def split_after_build_filter(mult: dict[str, Any], level: dict[str, Any], result: CompileResult) -> dict[str, Any] | None:
    if not isinstance(level, dict):
        return None
    item = {
        "multiplier": mult.get("name"),
        "strat": level.get("strat"),
        "applies_to": mult.get("applies_to"),
    }
    for key in ("role", "row_mult"):
        if key in level:
            item[key] = level[key]
    if level.get("column") and "values" in level:
        values = level.get("values")
        item["column"] = level["column"]
        item["values"] = values
        item["condition"] = render_sql_condition(f"pk.{level['column']}", values, result, str(level.get("strat")))
    elif level.get("where"):
        item["where"] = level["where"]
    return item


def validate_multipliers(template: dict[str, Any], cohorts: list[dict[str, Any]], table_schemas: dict[str, list[str] | None], result: CompileResult) -> None:
    pk_candidates = [c.get("dest_table") for c in cohorts if str(c.get("type", "")).lower() == "pk"]
    for mult in template.get("multipliers", []) or []:
        if not isinstance(mult, dict):
            continue
        stage = mult.get("stage")
        if stage not in ("during_build", "split_after_build"):
            result.error("bad_multiplier_stage", f"Unsupported multiplier stage `{stage}`.", str(mult.get("name")))
        if stage == "split_after_build":
            targets = pk_candidates if mult.get("applies_to") == "PKTable" else [mult.get("applies_to")]
            target_cols = sorted({col for target in targets for col in (table_schemas.get(str(target)) or [])})
            for level in mult.get("levels", []) or []:
                col = level.get("column") if isinstance(level, dict) else None
                if col and col not in target_cols:
                    result.error(
                        "missing_split_column",
                        f"Multiplier `{mult.get('name')}` references missing column `{col}` on `{mult.get('applies_to')}`.",
                        str(level.get("strat")),
                    )


def expand_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    normalized = normalize_batching(template.get("batching", []) or [], recipes_doc, result)
    for cohort in cohorts:
        cohort["batching"] = normalized
    return cohorts


def normalize_batching(batch_items: list[Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
    presets = {item["name"]: item for item in recipes_doc.get("batching_recipes", []) or [] if isinstance(item, dict) and item.get("name")}
    normalized = []
    for item in batch_items:
        if isinstance(item, int):
            normalized.append({"name": "chunk", "kind": "row_chunk", "rows_per_batch": item, "applies_to": "PKTable"})
        elif isinstance(item, dict) and "chunk" in item:
            normalized.append({"name": "chunk", "kind": "row_chunk", "rows_per_batch": item["chunk"], "applies_to": "PKTable"})
        elif isinstance(item, str) and item in presets:
            normalized.append(copy.deepcopy(presets[item]))
        elif isinstance(item, dict) and len(item) == 1 and next(iter(item)) in presets:
            name = next(iter(item))
            merged = deep_merge(presets[name], item[name] or {})
            normalized.append(merged)
        elif isinstance(item, dict) and item.get("name"):
            normalized.append(item)
        else:
            result.error("bad_batching", f"Could not understand batching item `{item}`.", "batching")
    return normalized


# =============================================================================
# Data dictionary validation
# =============================================================================


def default_datadictionary_path() -> Path:
    return project_root() / "YAMLs" / "datadictionary.yaml"


# Dictionary types are abstract and annotated ("bigint (foreign key to ...)");
# cohorts declare T-SQL. Compare families, not literals. Widening is accepted
# because it cannot lose data; narrowing is not.
TYPE_FAMILIES: dict[str, set[str]] = {
    "bigint": {"BIGINT"},
    "integer": {"INT", "SMALLINT", "TINYINT", "BIGINT"},
    "string": {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT"},
    "boolean": {"BIT"},
    "numeric": {"DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY", "SMALLMONEY"},
    "datetime": {"DATETIME", "DATETIME2", "SMALLDATETIME", "DATE"},
    "date/datetime": {"DATE", "DATETIME", "DATETIME2", "SMALLDATETIME"},
    "date": {"DATE", "DATETIME", "DATETIME2"},
    "time": {"TIME"},
}

# `PatientDim AS p`, `INNER JOIN X AS y ON ...`, `BirthFact as bf`
_ALIAS_PATTERN = re.compile(
    # Braces are allowed so an unsubstituted `##JVM_{{PKTable}}` still binds
    # its alias, rather than looking like an undeclared one.
    r"(?:\bFROM\s+|\bJOIN\s+|^)\s*(?P<table>\[[^\]]+\]|[A-Za-z_#@][\w@$#.{}]*)\s+AS\s+(?P<alias>\w+)",
    re.IGNORECASE,
)
# Only a bare `alias.Column` source can be resolved to a dictionary entry.
_SIMPLE_SOURCE = re.compile(r"^(?P<alias>\w+)\.(?P<column>\w+)$")


def dictionary_family(raw_type: Any) -> str:
    """`bigint (foreign key to PatientDim.DurableKey)` -> `bigint`."""
    return str(raw_type or "").split("(")[0].strip().lower()


def tsql_base_type(declared: Any) -> str:
    """`VARCHAR(400)` -> `VARCHAR`."""
    return str(declared or "").split("(")[0].strip().upper()


def is_generated_reference(table: str) -> bool:
    """Temp tables and unresolved placeholders are not dictionary entries."""
    return table.startswith("#") or "{{" in table


def filter_text_parts(cohort: dict[str, Any]) -> list[str]:
    block = cohort.get("filter") or {}
    parts: list[str] = []
    for key in ("from", "join"):
        value = block.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
    return parts


def cohort_aliases(cohort: dict[str, Any]) -> dict[str, str]:
    """Map each alias declared in `from`/`join` to its table."""
    aliases: dict[str, str] = {}
    for part in filter_text_parts(cohort):
        for match in _ALIAS_PATTERN.finditer(part):
            table = match.group("table").strip("[]")
            aliases[match.group("alias")] = table
    return aliases


def validate_data_dictionary(
    cohorts: list[dict[str, Any]],
    dictionary: dict[str, Any] | None,
    result: CompileResult,
) -> None:
    """Check every cohort column against the data dictionary.

    An unknown table is a hard error rather than a warning: it usually means a
    table name was invented or left as pseudocode, and the dictionary is meant
    to stay complete, so the fix is to add the table rather than route around
    the check.
    """
    if not dictionary:
        return

    for cohort in cohorts:
        if not isinstance(cohort, dict):
            continue
        label = str(cohort.get("dest_table") or cohort.get("name") or "cohort")
        aliases = cohort_aliases(cohort)

        for table in sorted(set(aliases.values())):
            if is_generated_reference(table):
                continue
            if table not in dictionary:
                result.error(
                    "unknown_table",
                    f"Table `{table}` is not in the data dictionary. Add it to "
                    f"YAMLs/datadictionary.yaml, or correct the name.",
                    label,
                )

        for column in cohort.get("columns") or []:
            if not isinstance(column, dict):
                continue
            source = str(column.get("source") or "").strip()
            if not source:
                continue
            match = _SIMPLE_SOURCE.match(source)
            if not match:
                result.warn(
                    "dd_source_not_checked",
                    f"Source `{source}` is not a plain `alias.Column`, so its type "
                    f"cannot be checked against the data dictionary.",
                    label,
                )
                continue

            alias, column_name = match.group("alias"), match.group("column")
            table = aliases.get(alias)
            if table is None:
                result.error(
                    "unknown_alias",
                    f"Source `{source}` uses alias `{alias}`, which is not declared "
                    f"in this cohort's `from` or `join`.",
                    label,
                )
                continue
            if is_generated_reference(table) or table not in dictionary:
                continue

            dd_columns = (dictionary[table] or {}).get("columns") or {}
            if column_name not in dd_columns:
                result.error(
                    "unknown_column",
                    f"Column `{column_name}` is not listed under `{table}` in the "
                    f"data dictionary. Check the alias and the spelling.",
                    label,
                )
                continue

            declared = column.get("type")
            if not declared:
                continue
            family = dictionary_family((dd_columns[column_name] or {}).get("type"))
            accepted = TYPE_FAMILIES.get(family)
            if accepted is None:
                result.warn(
                    "dd_unknown_family",
                    f"Data dictionary type `{family}` for `{table}.{column_name}` is "
                    f"not a family this checker knows, so `{declared}` was not verified.",
                    label,
                )
                continue
            if tsql_base_type(declared) not in accepted:
                result.error(
                    "dd_type_mismatch",
                    f"`{source}` is declared `{declared}`, but the data dictionary "
                    f"says `{table}.{column_name}` is "
                    f"`{(dd_columns[column_name] or {}).get('type')}`.",
                    label,
                )


_DATADICT_CACHE: dict[tuple[str, float], dict[str, Any]] = {}


def load_datadictionary(path: str | Path | None, result: CompileResult) -> dict[str, Any] | None:
    """Load the dictionary, warning rather than failing when it is absent.

    Cached by path and mtime: it is a few thousand lines and is otherwise
    reparsed on every compile, including once per test.
    """
    dict_path = Path(path) if path else default_datadictionary_path()
    if not dict_path.is_file():
        result.warn(
            "datadictionary_missing",
            f"No data dictionary at {dict_path}; column types were not verified.",
            str(dict_path),
        )
        return None
    cache_key = (str(dict_path.resolve()), dict_path.stat().st_mtime)
    cached = _DATADICT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        doc = load_yaml(dict_path) or {}
    except Exception as exc:
        result.warn("datadictionary_unreadable", str(exc), str(dict_path))
        return None
    entries = doc.get("DataDictionary") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        result.warn(
            "datadictionary_malformed",
            f"{dict_path} has no `DataDictionary` mapping; column types were not verified.",
            str(dict_path),
        )
        return None
    _DATADICT_CACHE[cache_key] = entries
    return entries



def validate_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], table_schemas: dict[str, list[str] | None], result: CompileResult) -> None:
    normalized = normalize_batching(template.get("batching", []) or [], recipes_doc, result)
    pk_candidates = [c.get("dest_table") for c in cohorts if str(c.get("type", "")).lower() == "pk"]
    pk_cols = sorted({col for pk_table in pk_candidates for col in (table_schemas.get(str(pk_table)) or [])})
    for item in normalized:
        if item.get("kind") == "row_chunk":
            continue
        col = item.get("column")
        if col and col not in pk_cols:
            result.error("missing_batch_column", f"Batching `{item.get('name')}` requires missing PK column `{col}`.", "PKTable")


def expand_cosmos(template: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    cosmos = str(template.get("cosmos_db", "COSMOS"))
    value = cosmos.lower()
    if value in ("cosmos",):
        return cohorts
    if value in ("cosmos_sneakpeek", "sneakpeek", "sp"):
        return [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek") for c in cohorts]
    if value in ("dual", "both"):
        return cohorts + [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek") for c in cohorts]
    result.error("bad_cosmos_db", f"Unsupported cosmos_db value `{cosmos}`.", "cosmos_db")
    return cohorts


def validate_cosmos(template: dict[str, Any], result: CompileResult) -> None:
    value = str(template.get("cosmos_db", "COSMOS")).lower()
    if value not in ("cosmos", "cosmos_sneakpeek", "sneakpeek", "sp", "dual", "both"):
        result.error("bad_cosmos_db", f"Unsupported cosmos_db value `{template.get('cosmos_db')}`.", "cosmos_db")


def with_cosmos_suffix(cohort: dict[str, Any], suffix: str, cosmos_db: str) -> dict[str, Any]:
    new = copy.deepcopy(cohort)
    new["name"] = f"{new.get('name')}{suffix}"
    new["dest_table"] = f"{new.get('dest_table', new.get('name'))}{suffix}"
    new["cosmos_db"] = cosmos_db
    return new


# =============================================================================
# Reports
# =============================================================================


def build_report(result: CompileResult) -> str:
    lines = ["# Manager Report", ""]
    lines.append(f"OK: {result.ok}")
    lines.append("")
    lines.append("## Errors")
    if result.errors:
        for msg in result.errors:
            lines.append(f"- `{msg.code}`: {msg.message} {msg.context}".rstrip())
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Warnings")
    if result.warnings:
        for msg in result.warnings:
            lines.append(f"- `{msg.code}`: {msg.message} {msg.context}".rstrip())
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Expanded Cohorts")
    for cohort in result.finished_yaml.get("cohorts", []) or []:
        lines.append(f"- {cohort.get('name')} -> {cohort.get('dest_table')}")
    lines.append("")
    lines.append("## Required Columns")
    for cohort, cols in result.analysis.get("required_table_columns", {}).items():
        lines.append(f"- {cohort}: {cols}")
    return "\n".join(lines) + "\n"


# =============================================================================
# Public API
# =============================================================================


def compile_yaml(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    output_path: str | Path | None = None,
    suffix: str = OUTPUT_SUFFIX,
    write: bool = False,
    report_path: str | Path | None = None,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    result = CompileResult()
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    missing = missing_template_message(template_path)
    if missing:
        result.error("template_not_found", missing, str(template_path))
        return result
    try:
        template = normalize_template(load_yaml(template_path), result)
        recipes_doc = load_yaml(recipes_path) or {}
    except Exception as exc:
        result.error("yaml_load_error", str(exc), str(template_path))
        return result

    cohorts = import_recipes(template, recipes_doc, result)
    cohorts = expand_multipliers(template, cohorts, result)
    analysis = analyze_cohorts(cohorts)
    cohorts = validate_and_resolve(template, recipes_doc, cohorts, analysis, result, template_path.parent)
    validate_cosmos(template, result)
    if result.errors:
        rendered_cohorts = [{k: v for k, v in cohort.items() if not k.startswith("_")} for cohort in cohorts]
    else:
        rendered_cohorts = render_cohorts(cohorts, result)
        # Checked after rendering so template variables are already substituted,
        # and before expansion so each real cohort reports once rather than once
        # per multiplier and Cosmos variant.
        validate_data_dictionary(
            rendered_cohorts, load_datadictionary(datadictionary_path, result), result
        )
        rendered_cohorts = expand_batching(template, recipes_doc, rendered_cohorts, result)
        rendered_cohorts = expand_cosmos(template, rendered_cohorts, result)

    finished = copy.deepcopy(template)
    finished["cohorts"] = rendered_cohorts
    finished.pop("example_cohorts", None)
    result.finished_yaml = finished
    result.analysis = analysis
    out_path = Path(output_path) if output_path else output_path_for(template, suffix)
    result.output_path = str(out_path)
    if write and result.ok:
        dump_yaml(finished, out_path)
    if report_path:
        report_out = Path(report_path)
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(build_report(result), encoding="utf-8")
    return result


def validate_yaml(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return compile_yaml(
        template_path=template_path,
        recipes_path=recipes_path,
        write=False,
        datadictionary_path=datadictionary_path,
    )


def inspect_recipes(recipes_path: str | Path | None = None) -> CompileResult:
    result = CompileResult()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    try:
        recipes_doc = load_yaml(recipes_path) or {}
    except Exception as exc:
        result.error("yaml_load_error", str(exc), str(recipes_path))
        return result
    result.analysis = {
        "recipes": [r.get("name") for r in recipes_doc.get("recipes", []) or []],
        "batching_recipes": [r.get("name") for r in recipes_doc.get("batching_recipes", []) or []],
    }
    return result


def safe_id(value: Any, fallback: str = "item") -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return text or fallback


def project_metadata(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": template.get("project_folder") or template.get("project_db") or "YAML Manager Project",
        "project_folder": template.get("project_folder"),
        "project_db": template.get("project_db"),
        "created_by": "yamlmanager",
    }


def uploaded_pk_source(template: dict[str, Any], result: CompileResult) -> dict[str, Any] | None:
    pk_uploads = [
        upload for upload in template.get("upload_cohorts", []) or []
        if isinstance(upload, dict) and str(upload.get("type", "")).lower() == "pk"
    ]
    if len(pk_uploads) > 1:
        result.error(
            "multiple_uploaded_pk",
            "Only one upload cohort may be marked `type: pk`.",
            ", ".join(str(upload.get("name")) for upload in pk_uploads),
        )
        return None
    if not pk_uploads:
        return None
    upload = pk_uploads[0]
    key_columns = upload.get("key_columns") or upload.get("columns") or []
    if not key_columns:
        result.error(
            "uploaded_pk_missing_keys",
            "Uploaded PK cohort must declare `key_columns`.",
            str(upload.get("name")),
        )
    return {
        "kind": "uploaded_cohort",
        "upload_name": upload.get("name"),
        "table": upload.get("dest_table") or upload.get("name"),
        "key_columns": key_columns,
    }


def session_paths(session_id: str) -> dict[str, str]:
    base = f"sessions/{session_id}"
    return {
        "setup": f"{base}/setup.yaml",
        "upload_cohorts": f"{base}/upload_cohorts.yaml",
        "pk": f"{base}/pk.yaml",
        "run": f"{base}/runs/run.yaml",
    }


def batch_buckets(dim: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Plan-time buckets for one batching dimension.

    Returns None when the buckets cannot be known until the PK table exists:
    row chunks depend on the row count, and `values: all` needs a DISTINCT over
    real data. Those dimensions stay logical for Pullmanager to materialize.
    """
    if str(dim.get("kind", "")).lower() == "row_chunk":
        return None
    values = dim.get("values")
    if not isinstance(values, list) or not values:
        return None
    buckets = [{"value": value, "is_other": False} for value in values]
    if dim.get("include_other"):
        buckets.append({"value": None, "is_other": True})
    return buckets


def bucket_label(dim: dict[str, Any], bucket: dict[str, Any]) -> str:
    if bucket.get("is_other"):
        return safe_id(f"{dim.get('name') or 'batch'}-other", "other")
    return safe_id(bucket.get("value"), "value")


def resolved_dimension(dim: dict[str, Any], bucket: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {
        "name": dim.get("name"),
        "kind": dim.get("kind") or "column_values",
        "column": dim.get("column"),
    }
    if bucket.get("is_other"):
        # The catch-all is defined by what it is not, so it has to carry the
        # named values; a predicate for it cannot be built from `is_other` alone.
        resolved["is_other"] = True
        resolved["excludes"] = [v for v in (dim.get("values") or [])]
    else:
        resolved["value"] = bucket.get("value")
    return resolved


def session_runs(
    session_id: str,
    pk_cohort: dict[str, Any],
    result: CompileResult | None = None,
) -> list[SplitRun]:
    """One run per batch combination.

    Batching dimensions multiply: state[LA, MS] x sex[Female, Male] is four
    runs, each a disjoint slice of the cohort, not three runs describing three
    different axes of the whole cohort.
    """
    base = f"sessions/{session_id}/runs"
    dims = [
        dim if isinstance(dim, dict) else {"name": str(dim)}
        for dim in pk_cohort.get("batching") or []
    ]
    if not dims:
        return [SplitRun(run_id=f"{session_id}__run", yaml=f"{base}/run.yaml")]

    static: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    runtime: list[dict[str, Any]] = []
    for dim in dims:
        buckets = batch_buckets(dim)
        if buckets is None:
            runtime.append(copy.deepcopy(dim))
        else:
            static.append((dim, buckets))

    if not static:
        return [
            SplitRun(
                run_id=f"{session_id}__run",
                yaml=f"{base}/run.yaml",
                batch={"name": "run", "dimensions": [], "runtime": runtime},
            )
        ]

    runs: list[SplitRun] = []
    seen: set[str] = set()
    for combo in product(*[buckets for _, buckets in static]):
        pairs = list(zip(static, combo))
        name = safe_id("-".join(bucket_label(dim, bucket) for (dim, _), bucket in pairs), "batch")
        if name in seen:
            if result is not None:
                result.error(
                    "duplicate_batch_name",
                    f"Batch combination `{name}` is not unique in session `{session_id}`. "
                    "Two batching dimensions produce the same label; rename a value.",
                    session_id,
                )
            continue
        seen.add(name)
        runs.append(
            SplitRun(
                run_id=f"{session_id}__{name}",
                yaml=f"{base}/{name}.yaml",
                batch={
                    "name": name,
                    "dimensions": [resolved_dimension(dim, bucket) for (dim, _), bucket in pairs],
                    "runtime": copy.deepcopy(runtime),
                },
            )
        )
    return runs


def session_multiplier_context(pk_cohort: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    context: dict[str, Any] = {"session_label": session_id}
    if pk_cohort.get("split_after_build"):
        context["split_after_build"] = copy.deepcopy(pk_cohort.get("split_after_build"))
    return context if len(context) > 1 else None


def build_split_plan_from_finished(
    finished_yaml: dict[str, Any],
    template_path: Path,
    recipes_path: Path,
    result: CompileResult,
) -> SplitPlan:
    cohorts = finished_yaml.get("cohorts", []) or []
    pk_source = uploaded_pk_source(finished_yaml, result)
    pk_cohorts = [cohort for cohort in cohorts if isinstance(cohort, dict) and str(cohort.get("type", "")).lower() == "pk"]
    if not pk_cohorts:
        if pk_source:
            pk_cohorts = [{
                "name": pk_source.get("upload_name") or pk_source.get("table"),
                "dest_table": pk_source.get("table"),
                "type": "PK",
            }]
        else:
            session_id = safe_id(finished_yaml.get("project_folder") or finished_yaml.get("project_db"), "default")
            pk_cohorts = [{"name": session_id, "dest_table": None}]

    sessions: list[SplitSession] = []
    for pk_cohort in pk_cohorts:
        pk_name = str(pk_cohort.get("name") or pk_cohort.get("dest_table") or "PKTable")
        pk_table = pk_cohort.get("dest_table") or pk_cohort.get("name")
        session_id = safe_id(pk_table or pk_name, "session")
        paths = session_paths(session_id)
        source = pk_source or {"kind": "generated", "table": pk_table}
        phases = {
            "setup": SplitPhase("setup", paths["setup"]),
            "upload_cohorts": SplitPhase("upload_cohorts", paths["upload_cohorts"]),
            "pk": SplitPhase("pk", paths["pk"], pk_source=source),
        }
        runs = session_runs(session_id, pk_cohort, result)
        sessions.append(
            SplitSession(
                session_id=session_id,
                cohort=pk_name,
                pk_table=str(pk_table) if pk_table else None,
                phases=phases,
                runs=runs,
                multiplier=session_multiplier_context(pk_cohort, session_id),
            )
        )

    return SplitPlan(
        project=project_metadata(finished_yaml),
        source={
            "template": str(template_path),
            "recipes": str(recipes_path),
        },
        sessions=sessions,
    )


def plan_split_runs(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    result = compile_yaml(
        template_path=template_path,
        recipes_path=recipes_path,
        write=False,
        datadictionary_path=datadictionary_path,
    )
    if result.errors:
        return result
    plan = build_split_plan_from_finished(result.finished_yaml, template_path, recipes_path, result)
    result.analysis["split_plan"] = plan.to_dict()
    return result


def build_pullmanifest(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    output_path: str | Path | None = None,
    write: bool = False,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    result = plan_split_runs(
        template_path=template_path,
        recipes_path=recipes_path,
        datadictionary_path=datadictionary_path,
    )
    if result.errors:
        return result
    manifest = result.analysis.get("split_plan", {})
    result.finished_yaml = manifest
    out_path = Path(output_path) if output_path else project_root() / DEFAULT_MANIFEST_PATH
    result.output_path = str(out_path)
    if write and result.ok:
        dump_yaml(manifest, out_path)
    return result


def split_base_document(finished_yaml: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(finished_yaml)
    doc.pop("cohorts", None)
    doc.pop("multipliers", None)
    doc.pop("batching", None)
    doc.pop("example_cohorts", None)
    return doc


def split_pull_context(session: dict[str, Any], phase: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    context: dict[str, Any] = {
        "session_id": session.get("session_id"),
        "phase": phase,
        "cohort": session.get("cohort"),
        "pk_table": session.get("pk_table"),
    }
    if run is not None:
        context["run_id"] = run.get("run_id")
        if run.get("batch") is not None:
            context["batch"] = run.get("batch")
    if session.get("multiplier") is not None:
        context["multiplier"] = session.get("multiplier")
    pk_phase = (session.get("phases") or {}).get("pk") or {}
    if pk_phase.get("pk_source") is not None:
        context["pk_source"] = pk_phase.get("pk_source")
    return context


def split_phase_document(
    finished_yaml: dict[str, Any],
    session: dict[str, Any],
    phase: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc = split_base_document(finished_yaml)
    cohorts = finished_yaml.get("cohorts", []) or []
    pk_table = session.get("pk_table")
    pk_cohorts = [
        cohort for cohort in cohorts
        if isinstance(cohort, dict)
        and str(cohort.get("type", "")).lower() == "pk"
        and (pk_table is None or cohort.get("dest_table") == pk_table or cohort.get("name") == pk_table)
    ]
    fact_cohorts = [
        cohort for cohort in cohorts
        if isinstance(cohort, dict) and str(cohort.get("type", "")).lower() != "pk"
    ]
    doc["pull_context"] = split_pull_context(session, phase, run)
    if phase == "upload_cohorts":
        doc["upload_cohorts"] = copy.deepcopy(finished_yaml.get("upload_cohorts", []) or [])
        doc["cohorts"] = []
    elif phase == "pk":
        doc["cohorts"] = copy.deepcopy(pk_cohorts)
    elif phase == "run":
        doc["cohorts"] = copy.deepcopy(fact_cohorts)
    else:
        doc["cohorts"] = []
    return doc


UPLOAD_STAGING_DIR = "uploads"


def stage_upload_files(
    finished_yaml: dict[str, Any],
    template_path: Path,
    out_dir: Path,
    result: CompileResult,
) -> None:
    """Copy upload files into the split folder and repoint `file_loc` at them.

    `file_loc` is written relative to the template, but the split folder is
    what travels to the VM, and Pullmanager resolves relative to the manifest.
    Without this the two anchors disagree and every upload fails to open on
    the far side. Copying makes the split folder self-contained.
    """
    uploads = finished_yaml.get("upload_cohorts") or []
    if not uploads:
        return
    staging = out_dir / UPLOAD_STAGING_DIR
    for upload in uploads:
        if not isinstance(upload, dict):
            continue
        file_loc = upload.get("file_loc")
        if not file_loc:
            continue
        source = Path(str(file_loc))
        if not source.is_absolute():
            source = template_path.parent / source
        if not source.is_file():
            result.warn(
                "upload_file_not_staged",
                f"Upload file {source} could not be copied into the split folder; "
                "Pullmanager will not find it.",
                str(upload.get("name")),
            )
            continue
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / source.name
        shutil.copyfile(source, target)
        upload["file_loc"] = f"{UPLOAD_STAGING_DIR}/{source.name}"


def write_split_artifacts(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    result = plan_split_runs(
        template_path=template_path,
        recipes_path=recipes_path,
        datadictionary_path=datadictionary_path,
    )
    if result.errors:
        return result
    out_dir = Path(output_dir) if output_dir else project_root() / DEFAULT_SPLIT_DIR
    finished_yaml = copy.deepcopy(result.finished_yaml)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage_upload_files(
        finished_yaml,
        Path(template_path) if template_path else default_template_path(),
        out_dir,
        result,
    )
    manifest = result.analysis.get("split_plan", {})
    manifest_path = out_dir / "pullmanifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, manifest_path)

    for session in manifest.get("sessions", []) or []:
        phases = session.get("phases", {}) or {}
        for phase_name in ("setup", "upload_cohorts", "pk"):
            phase = phases.get(phase_name)
            if not phase:
                continue
            path = out_dir / phase["yaml"]
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_yaml(split_phase_document(finished_yaml, session, phase_name), path)
        for run in session.get("runs", []) or []:
            path = out_dir / run["yaml"]
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_yaml(split_phase_document(finished_yaml, session, "run", run), path)

    result.finished_yaml = manifest
    result.output_path = str(manifest_path)
    result.analysis["split_output_dir"] = str(out_dir)
    return result


def public_cohort(cohort: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cohort.items() if not k.startswith("_")}


def build_preyaml(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    output_path: str | Path | None = None,
    mode: str = "symbolic",
    write: bool = False,
    report_path: str | Path | None = None,
) -> CompileResult:
    result = CompileResult()
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    try:
        template = load_yaml(template_path) or {}
    except Exception as exc:
        result.error("yaml_load_error", str(exc), str(template_path))
        return result
    if not isinstance(template, dict):
        result.error("invalid_template", "Template YAML must be a mapping.", str(template_path))
        return result
    if mode not in ("symbolic", "expanded-recipes"):
        result.error("bad_preyaml_mode", f"Unsupported pre-YAML mode `{mode}`.", mode)
        return result

    if mode == "symbolic":
        preyaml = copy.deepcopy(template)
        suffix = PREYAML_SUFFIX
    else:
        try:
            recipes_doc = load_yaml(recipes_path) or {}
        except Exception as exc:
            result.error("yaml_load_error", str(exc), str(recipes_path))
            return result
        normalized = normalize_template(template, result)
        cohorts = import_recipes(normalized, recipes_doc, result)
        preyaml = copy.deepcopy(normalized)
        preyaml["cohorts"] = [public_cohort(cohort) for cohort in cohorts]
        preyaml.pop("example_cohorts", None)
        result.analysis = analyze_cohorts(cohorts)
        suffix = EXPANDED_PREYAML_SUFFIX

    result.finished_yaml = preyaml
    out_path = Path(output_path) if output_path else output_path_for(template, suffix)
    result.output_path = str(out_path)
    if write and result.ok:
        dump_yaml(preyaml, out_path)
    if report_path:
        report_out = Path(report_path)
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(build_report(result), encoding="utf-8")
    return result


# =============================================================================
# Embedded TDD
# =============================================================================


def write_temp_yaml(tmp: Path, name: str, data: str) -> Path:
    path = tmp / name
    path.write_text(data.strip() + "\n", encoding="utf-8")
    return path


def tiny_recipes() -> str:
    return """
batching_recipes:
  - name: state
    kind: column_values
    applies_to: PKTable
    column: StateOrProvinceAbbreviation
    values: all
  - name: sex
    kind: column_values
    applies_to: PKTable
    column: Sex
    values: [Female, Male]
  - name: chunk
    kind: row_chunk
    applies_to: PKTable
    rows_per_batch: required
recipes:
  - name: PatientWithDx
    type: PK
    columns:
      - source: dxf.PatientDurableKey
        name: PatientDurableKey
      - source: dxf.DiagnosisEventKey
        name: DiagnosisEventKey
      - source: p.FirstRace
        name: FirstRace
      - source: p.Sex
        name: Sex
      - source: p.StateOrProvinceAbbreviation
        name: StateOrProvinceAbbreviation
    filter:
      from:
        - DiagnosisEventFact AS dxf
      join:
        - "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey"
        - "INNER JOIN PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey"
      where:
        - "dxf.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
        - "{{sql_condition('dt.Value', ICD_Value)}}"
  - name: OtherDx
    type: fact
    columns:
      - source: def.PatientDurableKey
        name: PatientDurableKey
    filter:
      from:
        - DiagnosisEventFact AS def
      join:
        - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.PatientDurableKey = def.PatientDurableKey AND pk.DiagnosisEventKey <> def.DiagnosisEventKey"
      where:
        - "def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
"""


def tiny_recipes_path(tmp: Path) -> Path:
    return write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())


def uploaded_pk_template(extra_upload: str = "", key_columns: bool = True) -> str:
    keys = "    key_columns: [PatientDurableKey, DiagnosisEventKey]\n" if key_columns else ""
    return f"""
project_folder: Uploaded PK
cosmos_db: COSMOS
vars:
  min_date_key: 20200101
  max_date_key: 20240101
upload_cohorts:
  - name: ClientPK
    type: pk
    dest_table: ClientPK
    file_type: csv
    file_loc: pks.csv
{keys}{extra_upload}
cohorts:
  - recipe: OtherDx
    name: OtherDx
"""


def tiny_template(extra: str = "") -> str:
    return f"""
project_folder: Test Run
cosmos_db: COSMOS
vars:
  min_date_key: 20200101
  max_date_key: 20240101
  ICD_Value:
    - K50
    - K51
cohorts:
  - recipe: PatientWithDx
    name: Patients
  - recipe: OtherDx
    name: OtherDx
{extra}
"""


def load_yaml_from_text(text: str) -> Any:
    with tempfile.TemporaryDirectory() as d:
        path = write_temp_yaml(Path(d), "inline.yaml", text)
        return load_yaml(path)


def has_error(result: CompileResult, code: str) -> bool:
    return any(msg.code == code for msg in result.errors)


def has_warning(result: CompileResult, code: str) -> bool:
    return any(msg.code == code for msg in result.warnings)


def summarize_result(result: CompileResult) -> str:
    bits = []
    if result.errors:
        bits.append("errors=" + json.dumps([m.to_dict() for m in result.errors]))
    if result.warnings:
        bits.append("warnings=" + json.dumps([m.to_dict() for m in result.warnings]))
    return "; ".join(bits) or "ok"


class MakeYamlTest(unittest.TestCase):
    """Base case: a scratch dir plus the template/recipes boilerplate folded in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_pair(self, template: str | None = None, extra: str = "") -> tuple[Path, Path]:
        text = tiny_template(extra) if template is None else template
        return (
            write_temp_yaml(self.tmp, "template.yaml", text),
            write_temp_yaml(self.tmp, "recipes.yaml", tiny_recipes()),
        )

    def compile_template(self, template: str | None = None, extra: str = "") -> CompileResult:
        return compile_yaml(*self.write_pair(template, extra))

    def plan_split(self, template: str | None = None, extra: str = "") -> CompileResult:
        return plan_split_runs(*self.write_pair(template, extra))

    def runs_for(self, extra: str = "") -> tuple[CompileResult, list[dict[str, Any]]]:
        res = self.plan_split(extra=extra)
        sessions = res.analysis.get("split_plan", {}).get("sessions", [])
        return res, (sessions[0].get("runs", []) if sessions else [])

    def cohorts_by_name(self, res: CompileResult) -> dict[str, Any]:
        return {c.get("name"): c for c in res.finished_yaml.get("cohorts", [])}

    def assertCompiles(self, res: CompileResult) -> None:
        self.assertTrue(res.ok, summarize_result(res))

    def assertHasError(self, res: CompileResult, code: str) -> None:
        self.assertTrue(has_error(res, code), summarize_result(res))

    def assertHasWarning(self, res: CompileResult, code: str) -> None:
        self.assertTrue(has_warning(res, code), summarize_result(res))


class LoadingTests(MakeYamlTest):
    def test_valid_template_compiles(self):
        self.assertCompiles(self.compile_template())

    def test_malformed_yaml_is_reported(self):
        res = self.compile_template("vars:\n  - bad: [")
        self.assertFalse(res.ok)
        self.assertHasError(res, "yaml_load_error")


class RecipeTests(MakeYamlTest):
    def test_recipe_cohorts_are_imported(self):
        names = [c.get("name") for c in self.compile_template().finished_yaml.get("cohorts", [])]
        self.assertIn("Patients", names)
        self.assertIn("OtherDx", names)

    def test_dest_table_can_be_overridden(self):
        res = self.compile_template("""
project_folder: Test
vars: {min_date_key: 1, max_date_key: 2, ICD_Value: K50}
cohorts:
  - recipe: PatientWithDx
    name: Patients
    dest_table: MyPatients
""")
        self.assertEqual(res.finished_yaml["cohorts"][0]["dest_table"], "MyPatients")

    def test_dest_table_defaults_to_cohort_name(self):
        res = self.compile_template()
        self.assertEqual(res.finished_yaml["cohorts"][0]["dest_table"], "Patients")


class InferenceTests(MakeYamlTest):
    def test_required_vars_are_inferred_from_recipe_body(self):
        required = self.compile_template().analysis["required_vars"]["Patients"]
        for name in ("min_date_key", "max_date_key", "ICD_Value"):
            with self.subTest(var=name):
                self.assertIn(name, required)


class NormalizationTests(MakeYamlTest):
    def test_grouped_metadata_vars_are_flattened(self):
        template = tiny_template().replace(
            "project_folder: Test Run\ncosmos_db: COSMOS\nvars:\n  min_date_key: 20200101\n  max_date_key: 20240101\n",
            "cosmos_vars:\n  project_db: PROJECTD33A929\n  cosmos_db: COSMOS\n"
            "run_vars:\n  min_date_key: 20200101\n  max_date_key: 20240101\n"
            "project_vars:\n  project_folder: Test Run\nvars:\n",
        )
        res = self.compile_template(template)
        self.assertCompiles(res)
        self.assertEqual(res.finished_yaml.get("project_db"), "PROJECTD33A929")
        self.assertEqual(res.finished_yaml.get("project_folder"), "Test Run")
        self.assertEqual(res.finished_yaml.get("vars", {}).get("min_date_key"), 20200101)
        self.assertIn(
            "dxf.StartDateKey BETWEEN 20200101 AND 20240101",
            json.dumps(res.finished_yaml),
        )


class ValidationTests(MakeYamlTest):
    def test_missing_variable_is_an_error(self):
        template = tiny_template().replace("  ICD_Value:\n    - K50\n    - K51\n", "")
        self.assertHasError(self.compile_template(template), "missing_variable")


class RenderingTests(MakeYamlTest):
    def test_multiple_exact_values_render_as_in(self):
        self.assertIn(
            "dt.Value IN ('K50', 'K51')",
            json.dumps(self.compile_template().finished_yaml),
        )

    def test_wildcard_values_render_as_or_ed_likes(self):
        template = tiny_template().replace("- K50\n    - K51", "- K50.%\n    - K51.%")
        text = json.dumps(self.compile_template(template).finished_yaml)
        self.assertIn("dt.Value LIKE 'K50.%'", text)
        self.assertIn(" OR ", text)

    def test_underscore_in_like_value_warns(self):
        template = tiny_template().replace("- K50\n    - K51", "- K50_%")
        self.assertHasWarning(self.compile_template(template), "like_underscore")


class MultiplierTests(MakeYamlTest):
    def test_split_on_missing_column_is_an_error(self):
        res = self.compile_template(extra="""
multipliers:
  - name: BadSplit
    stage: split_after_build
    applies_to: PKTable
    levels:
      - strat: bad
        column: MissingRace
        values: [x]
""")
        self.assertHasError(res, "missing_split_column")

    def test_during_build_multiplier_gives_each_group_its_own_pk(self):
        template = tiny_template("""
multipliers:
  - name: Type
    stage: during_build
    levels:
      - strat: A
        vars:
          ICD_Value: A%
      - strat: B
        vars:
          ICD_Value: B%
""").replace("  ICD_Value:\n    - K50\n    - K51\n", "")
        res = self.compile_template(template)
        self.assertCompiles(res)
        cohorts = self.cohorts_by_name(res)
        self.assertIn("##JVM_APatients AS pk", json.dumps(cohorts.get("AOtherDx", {})))
        self.assertIn("##JVM_BPatients AS pk", json.dumps(cohorts.get("BOtherDx", {})))

    def test_split_after_build_metadata_survives_on_the_pk(self):
        res = self.compile_template(extra="""
multipliers:
  - name: Race
    stage: split_after_build
    applies_to: PKTable
    levels:
      - strat: black
        column: FirstRace
        values:
          - Black %
""")
        pk = self.cohorts_by_name(res)["blackPatients"]
        self.assertIn("split_after_build", pk)
        self.assertIn("pk.FirstRace LIKE 'Black %'", json.dumps(pk))


class BatchingTests(MakeYamlTest):
    def test_chunk_shorthand_normalizes(self):
        norm = normalize_batching(
            [{"chunk": 2000}], load_yaml_from_text(tiny_recipes()), CompileResult()
        )
        self.assertEqual(norm[0].get("rows_per_batch"), 2000)
        self.assertEqual(norm[0].get("kind"), "row_chunk")

    def test_batching_metadata_reaches_the_pk_cohort(self):
        res = self.compile_template(extra="""
batching:
  - sex
  - chunk: 2000
""")
        first = res.finished_yaml["cohorts"][0]
        self.assertIn("batching", first)
        self.assertEqual(len(first["batching"]), 2)

    def test_include_other_is_preserved_by_normalization(self):
        norm = normalize_batching(
            [{"sex": {"values": ["Female"], "include_other": True}}],
            load_yaml_from_text(tiny_recipes()),
            CompileResult(),
        )
        self.assertEqual(
            {k: norm[0].get(k) for k in ("name", "values", "include_other", "column")},
            {"name": "sex", "values": ["Female"], "include_other": True, "column": "Sex"},
        )

    def test_dimensions_cross_multiply(self):
        # state[LA, MS] x sex[Female, Male] is four disjoint slices, not two axes.
        res, runs = self.runs_for("""
batching:
  - state:
      values: [LA, MS]
  - sex
""")
        self.assertCompiles(res)
        self.assertEqual(
            [run["batch"]["name"] for run in runs],
            ["LA-Female", "LA-Male", "MS-Female", "MS-Male"],
        )

    def test_each_run_records_its_resolved_dimensions(self):
        _, runs = self.runs_for("""
batching:
  - state:
      values: [LA, MS]
  - sex
""")
        self.assertEqual(
            runs[0]["batch"]["dimensions"],
            [
                {
                    "name": "state",
                    "kind": "column_values",
                    "column": "StateOrProvinceAbbreviation",
                    "value": "LA",
                },
                {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Female"},
            ],
        )

    def test_include_other_contributes_a_bucket_to_the_product(self):
        _, runs = self.runs_for("""
batching:
  - sex:
      values: [Female]
      include_other: true
""")
        self.assertEqual([run["batch"]["name"] for run in runs], ["Female", "sex-other"])
        other = runs[1]["batch"]["dimensions"][0]
        self.assertTrue(other["is_other"])
        self.assertNotIn("value", other)
        # The catch-all is defined by exclusion, so it carries the named values.
        self.assertEqual(other["excludes"], ["Female"])

    def test_unresolvable_dimensions_stay_logical(self):
        # `values: all` needs a DISTINCT and chunking needs a row count, so
        # neither can expand until the PK table exists.
        res, runs = self.runs_for("""
batching:
  - state
  - chunk: 2000
""")
        self.assertCompiles(res)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["batch"]["dimensions"], [])
        self.assertEqual([r["name"] for r in runs[0]["batch"]["runtime"]], ["state", "chunk"])

    def test_static_and_runtime_dimensions_coexist(self):
        _, runs = self.runs_for("""
batching:
  - sex
  - state
  - chunk: 2000
""")
        self.assertEqual([run["batch"]["name"] for run in runs], ["Female", "Male"])
        self.assertEqual([r["name"] for r in runs[0]["batch"]["runtime"]], ["state", "chunk"])

    def test_no_batching_gives_one_unbatched_run(self):
        res, runs = self.runs_for("")
        self.assertCompiles(res)
        self.assertEqual(len(runs), 1)
        self.assertIsNone(runs[0].get("batch"))


class CosmosTests(MakeYamlTest):
    def test_dual_expands_to_both_instances(self):
        out = expand_cosmos(
            {"cosmos_db": "Dual"},
            [{"name": "Patients", "dest_table": "Patients"}],
            CompileResult(),
        )
        self.assertEqual([c["dest_table"] for c in out], ["Patients", "Patients_sp"])

    def test_unknown_cosmos_db_is_an_error(self):
        res = CompileResult()
        validate_cosmos({"cosmos_db": "Mars"}, res)
        self.assertHasError(res, "bad_cosmos_db")


class ReportTests(MakeYamlTest):
    def test_report_includes_every_section(self):
        res = CompileResult()
        res.error("x", "bad")
        res.warn("y", "careful")
        res.finished_yaml = {"cohorts": [{"name": "Patients", "dest_table": "Patients"}]}
        res.analysis = {"required_table_columns": {"OtherDx": {"PKTable": ["PatientDurableKey"]}}}
        report = build_report(res)
        for section in ("Errors", "Warnings", "Patients", "Required Columns"):
            with self.subTest(section=section):
                self.assertIn(section, report)


class PreyamlTests(MakeYamlTest):
    MIXED = """
batching:
  - sex
multipliers:
  - name: Type
    stage: during_build
    levels:
      - strat: A
        vars:
          ICD_Value: A%
"""

    def test_symbolic_mode_keeps_recipe_references(self):
        res = build_preyaml(*self.write_pair(extra=self.MIXED), mode="symbolic")
        self.assertCompiles(res)
        self.assertEqual(res.finished_yaml["cohorts"][0].get("recipe"), "PatientWithDx")
        self.assertIn("multipliers", res.finished_yaml)
        self.assertIn("batching", res.finished_yaml)

    def test_expanded_mode_inlines_recipes_without_applying_multipliers(self):
        res = build_preyaml(*self.write_pair(extra=self.MIXED), mode="expanded-recipes")
        self.assertCompiles(res)
        first = res.finished_yaml["cohorts"][0]
        text = json.dumps(res.finished_yaml)
        self.assertEqual(first.get("name"), "Patients")
        self.assertNotIn("recipe", first)
        self.assertIn("DiagnosisEventFact AS dxf", text)
        self.assertNotIn("APatients", text)
        self.assertIn("batching", res.finished_yaml)


class SplitPlanTests(MakeYamlTest):
    def test_plain_template_gives_one_session_with_one_run(self):
        res = self.plan_split()
        plan = res.analysis.get("split_plan", {})
        sessions = plan.get("sessions", [])
        self.assertCompiles(res)
        self.assertEqual(plan.get("manifest_version"), 1)
        self.assertEqual(len(sessions), 1)
        phases = sessions[0]["phases"]
        self.assertEqual(set(phases), {"setup", "upload_cohorts", "pk"})
        self.assertEqual(phases["pk"]["pk_source"]["kind"], "generated")
        self.assertEqual([r["run_id"] for r in sessions[0]["runs"]], ["Patients__run"])

    def test_multiplier_gives_one_session_per_group_each_batched(self):
        res = self.plan_split(extra="""
multipliers:
  - name: Type
    stage: during_build
    levels:
      - strat: A
        vars:
          ICD_Value: A%
      - strat: B
        vars:
          ICD_Value: B%
batching:
  - sex
  - chunk: 2000
""")
        sessions = res.analysis.get("split_plan", {}).get("sessions", [])
        self.assertCompiles(res)
        self.assertEqual(
            sorted(s["session_id"] for s in sessions), ["APatients", "BPatients"]
        )
        for session in sessions:
            with self.subTest(session=session["session_id"]):
                sid = session["session_id"]
                self.assertEqual(
                    [r["run_id"] for r in session["runs"]],
                    [f"{sid}__Female", f"{sid}__Male"],
                )
                first = session["runs"][0]["batch"]
                self.assertEqual([d["value"] for d in first["dimensions"]], ["Female"])
                self.assertEqual([r["name"] for r in first["runtime"]], ["chunk"])


class ManifestTests(MakeYamlTest):
    def test_manifest_is_written_with_pending_status_fields(self):
        out = self.tmp / "pullmanifest.yaml"
        res = build_pullmanifest(*self.write_pair(), output_path=out, write=True)
        self.assertCompiles(res)
        self.assertTrue(out.exists())

        manifest = res.finished_yaml
        session = manifest["sessions"][0]
        pk_phase = session["phases"]["pk"]
        self.assertEqual(manifest.get("manifest_version"), 1)
        self.assertEqual(session.get("status"), "pending")
        self.assertEqual(pk_phase.get("status"), "pending")
        self.assertIsNone(pk_phase.get("rows"))
        self.assertIsNone(pk_phase.get("error"))
        self.assertEqual(session["runs"][0].get("status"), "pending")
        self.assertEqual(session["runs"][0].get("outputs"), {})


class SplitArtifactTests(MakeYamlTest):
    def test_every_phase_yaml_is_written_and_standalone(self):
        out_dir = self.tmp / "split"
        res = write_split_artifacts(*self.write_pair(), output_dir=out_dir)
        self.assertCompiles(res)

        manifest_path = out_dir / "pullmanifest.yaml"
        self.assertTrue(manifest_path.exists())
        session = load_yaml(manifest_path)["sessions"][0]
        expected = [
            session["phases"]["setup"]["yaml"],
            session["phases"]["upload_cohorts"]["yaml"],
            session["phases"]["pk"]["yaml"],
            session["runs"][0]["yaml"],
        ]
        for rel in expected:
            with self.subTest(path=rel):
                self.assertTrue((out_dir / rel).exists())

        pk_doc = load_yaml(out_dir / expected[2])
        self.assertEqual(pk_doc["pull_context"]["phase"], "pk")
        self.assertEqual(len(pk_doc.get("cohorts", [])), 1)
        self.assertEqual(str(pk_doc["cohorts"][0].get("type", "")).lower(), "pk")

        run_doc = load_yaml(out_dir / expected[3])
        self.assertEqual(run_doc["pull_context"]["phase"], "run")
        self.assertTrue(run_doc.get("cohorts"))
        # Expansion instructions must not survive, or they would be applied twice.
        self.assertNotIn("multipliers", run_doc)
        self.assertNotIn("batching", run_doc)


class UploadedPkTests(MakeYamlTest):
    def setUp(self):
        super().setUp()
        (self.tmp / "pks.csv").write_text(
            "PatientDurableKey,DiagnosisEventKey\n1,2\n", encoding="utf-8"
        )

    def test_uploaded_cohort_becomes_the_session_pk(self):
        res = self.plan_split(uploaded_pk_template())
        session = res.analysis["split_plan"]["sessions"][0]
        pk_source = session["phases"]["pk"]["pk_source"]
        self.assertCompiles(res)
        self.assertEqual(session["session_id"], "ClientPK")
        self.assertEqual(pk_source["kind"], "uploaded_cohort")
        self.assertEqual(pk_source["table"], "ClientPK")
        self.assertIn("##JVM_ClientPK AS pk", json.dumps(res.finished_yaml))

    def test_two_uploaded_pk_cohorts_is_an_error(self):
        extra = """  - name: ClientPK2
    type: pk
    dest_table: ClientPK2
    file_type: csv
    file_loc: pks.csv
    key_columns: [PatientDurableKey, DiagnosisEventKey]
"""
        res = self.compile_template(uploaded_pk_template(extra))
        self.assertHasError(res, "multiple_uploaded_pk")

    def test_uploaded_pk_without_key_columns_is_an_error(self):
        res = self.compile_template(uploaded_pk_template(key_columns=False))
        self.assertHasError(res, "uploaded_pk_missing_keys")


class DataDictionaryTests(MakeYamlTest):
    DICT = {
        "PatientDim": {
            "columns": {
                "DurableKey": {"type": "bigint", "nullable": False},
                "Sex": {"type": "string", "nullable": True},
                "BirthDate": {"type": "date/datetime", "nullable": True},
                "IsCurrent": {"type": "boolean (flag)", "nullable": False},
                "StartDateKey": {"type": "integer (DateKey)", "nullable": True},
                "Weight": {"type": "numeric", "nullable": True},
            }
        }
    }

    def cohort(self, source, declared="BIGINT", join=None):
        return {
            "dest_table": "T",
            "columns": [{"source": source, "name": "C", "type": declared}],
            "filter": {"from": "PatientDim AS p", "join": join or []},
        }

    def check(self, cohort, dictionary=None):
        res = CompileResult()
        validate_data_dictionary([cohort], self.DICT if dictionary is None else dictionary, res)
        return res

    def codes(self, res):
        return [m.code for m in res.errors] + [m.code for m in res.warnings]

    def test_valid_column_passes(self):
        res = self.check(self.cohort("p.DurableKey", "BIGINT"))
        self.assertEqual(self.codes(res), [])

    def test_unknown_table_is_an_error(self):
        cohort = self.cohort("h.Whatever")
        cohort["filter"]["from"] = "HallucinatedTable AS h"
        self.assertIn("unknown_table", self.codes(self.check(cohort)))

    def test_unknown_column_is_an_error(self):
        self.assertIn("unknown_column", self.codes(self.check(self.cohort("p.NoSuchColumn"))))

    def test_undeclared_alias_is_an_error(self):
        # Referencing an alias that no from/join declares produces SQL that
        # fails to bind at runtime.
        self.assertIn("unknown_alias", self.codes(self.check(self.cohort("q.DurableKey"))))

    def test_type_family_mismatch_is_an_error(self):
        self.assertIn(
            "dd_type_mismatch", self.codes(self.check(self.cohort("p.Sex", "BIGINT")))
        )

    def test_families_accept_their_members(self):
        cases = [
            ("p.DurableKey", "BIGINT"),
            ("p.Sex", "VARCHAR(400)"),
            ("p.Sex", "NVARCHAR(50)"),
            ("p.IsCurrent", "BIT"),
            ("p.BirthDate", "DATETIME2(7)"),
            ("p.StartDateKey", "INT"),
            ("p.Weight", "FLOAT"),
        ]
        for source, declared in cases:
            with self.subTest(source=source, declared=declared):
                self.assertEqual(self.codes(self.check(self.cohort(source, declared))), [])

    def test_widening_is_accepted_but_narrowing_is_not(self):
        # An integer fits in a BIGINT; a bigint does not fit in an INT.
        self.assertEqual(self.codes(self.check(self.cohort("p.StartDateKey", "BIGINT"))), [])
        self.assertIn(
            "dd_type_mismatch", self.codes(self.check(self.cohort("p.DurableKey", "INT")))
        )

    def test_length_is_not_checked(self):
        # The dictionary carries no lengths, so VARCHAR(50) and VARCHAR(400)
        # are indistinguishable to it.
        for declared in ("VARCHAR(50)", "VARCHAR(4000)"):
            with self.subTest(declared=declared):
                self.assertEqual(self.codes(self.check(self.cohort("p.Sex", declared))), [])

    def test_generated_temps_are_skipped(self):
        cohort = self.cohort("pk.PatientDurableKey")
        cohort["filter"]["join"] = ["INNER JOIN ##JVM_PKTable AS pk ON 1 = 1"]
        self.assertEqual(self.codes(self.check(cohort)), [])

    def test_unresolved_placeholder_tables_are_skipped(self):
        cohort = self.cohort("pk.Anything")
        cohort["filter"]["join"] = ["INNER JOIN ##JVM_{{PKTable}} AS pk ON 1 = 1"]
        self.assertEqual(self.codes(self.check(cohort)), [])

    def test_computed_source_warns_rather_than_failing(self):
        res = self.check(self.cohort("CASE WHEN p.Sex = 'F' THEN 1 ELSE 0 END", "BIT"))
        self.assertEqual([m.code for m in res.errors], [])
        self.assertIn("dd_source_not_checked", [m.code for m in res.warnings])

    def test_lowercase_as_is_recognized(self):
        cohort = self.cohort("p.DurableKey", "BIGINT")
        cohort["filter"]["from"] = "PatientDim as p"
        self.assertEqual(self.codes(self.check(cohort)), [])

    def test_absent_dictionary_checks_nothing(self):
        self.assertEqual(self.codes(self.check(self.cohort("p.NoSuchColumn"), {})), [])

    def test_missing_dictionary_file_warns_but_does_not_fail(self):
        res = CompileResult()
        self.assertIsNone(load_datadictionary(self.tmp / "nope.yaml", res))
        self.assertEqual(res.errors, [])
        self.assertEqual([m.code for m in res.warnings], ["datadictionary_missing"])

    WRONG_DICT = """
DataDictionary:
  UnrelatedTable:
    columns:
      X: {type: bigint, nullable: false}
"""

    def wrong_dictionary(self) -> Path:
        return write_temp_yaml(self.tmp, "wrong_dd.yaml", self.WRONG_DICT)

    def test_every_compiling_route_honours_the_dictionary_path(self):
        # --export-split once ignored --datadictionary and silently validated
        # against the bundled copy, so a table added to a dictionary kept
        # elsewhere was invisible to it.
        template, recipes = self.write_pair()
        wrong = self.wrong_dictionary()
        routes = {
            "validate_yaml": lambda: validate_yaml(template, recipes, datadictionary_path=wrong),
            "plan_split_runs": lambda: plan_split_runs(template, recipes, datadictionary_path=wrong),
            "build_pullmanifest": lambda: build_pullmanifest(
                template, recipes, output_path=self.tmp / "m.yaml", datadictionary_path=wrong
            ),
            "write_split_artifacts": lambda: write_split_artifacts(
                template, recipes, output_dir=self.tmp / "split", datadictionary_path=wrong
            ),
        }
        for name, route in routes.items():
            with self.subTest(route=name):
                self.assertHasError(route(), "unknown_table")

    def test_missing_template_explains_the_example(self):
        # The bundle ships template.yaml.example, so the default is absent by
        # design; the error has to say so rather than report a bare errno.
        (self.tmp / "template.yaml.example").write_text("x: 1\n", encoding="utf-8")
        res = compile_yaml(self.tmp / "template.yaml", tiny_recipes_path(self.tmp))
        self.assertHasError(res, "template_not_found")
        self.assertIn("template.yaml.example", res.errors[0].message)

    def test_missing_template_without_an_example(self):
        res = compile_yaml(self.tmp / "nope.yaml", tiny_recipes_path(self.tmp))
        self.assertHasError(res, "template_not_found")
        self.assertIn("--template", res.errors[0].message)

    def test_real_dictionary_accepts_the_bundled_recipes(self):
        # The shipped recipes and dictionary must agree, or every template
        # built from them fails.
        res = compile_yaml(
            project_root() / "YAMLs" / "manager_test_cases" / "01_valid_basic.yaml",
            project_root() / "YAMLs" / "recipes.yaml",
        )
        offenders = [
            m for m in res.errors
            if m.code in ("unknown_table", "unknown_column", "unknown_alias", "dd_type_mismatch")
        ]
        self.assertEqual(offenders, [], summarize_result(res))


TEST_GROUPS: dict[str, type[unittest.TestCase]] = {
    "loading": LoadingTests,
    "recipes": RecipeTests,
    "inference": InferenceTests,
    "normalization": NormalizationTests,
    "validation": ValidationTests,
    "rendering": RenderingTests,
    "multipliers": MultiplierTests,
    "batching": BatchingTests,
    "cosmos": CosmosTests,
    "reports": ReportTests,
    "preyaml": PreyamlTests,
    "split_plan": SplitPlanTests,
    "manifest": ManifestTests,
    "split_artifacts": SplitArtifactTests,
    "uploaded_pk": UploadedPkTests,
    "datadictionary": DataDictionaryTests,
}


def run_tdd(group: str | None = None, verbosity: int = 2) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    if group:
        case = TEST_GROUPS.get(group)
        if case is None:
            print(f"No test group {group!r}. Available: " + ", ".join(TEST_GROUPS))
            return 1
        suite.addTests(loader.loadTestsFromTestCase(case))
    else:
        for case in TEST_GROUPS.values():
            suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


# =============================================================================
# CLI
# =============================================================================


def print_messages(result: CompileResult) -> None:
    for msg in result.errors:
        print(f"ERROR [{msg.code}] {msg.message} {msg.context}".rstrip())
    for msg in result.warnings:
        print(f"WARN  [{msg.code}] {msg.message} {msg.context}".rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile YAML Manager templates.")
    parser.add_argument("--template", default=str(default_template_path()))
    parser.add_argument("--recipes", default=str(default_recipes_path()))
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--datadictionary",
        default=None,
        help="Data dictionary to validate column types against.",
    )
    parser.add_argument("--suffix", default=OUTPUT_SUFFIX)
    parser.add_argument("--write", action="store_true", help="Write finished YAML if validation passes.")
    parser.add_argument("--validate", action="store_true", help="Validate without writing output.")
    parser.add_argument("--inspect-recipes", action="store_true")
    parser.add_argument("--export-preyaml", choices=("symbolic", "expanded-recipes"), default=None)
    parser.add_argument("--export-split", action="store_true", help="Write split YAML artifacts and pullmanifest.yaml.")
    parser.add_argument("--out-dir", default=None, help="Directory for split export artifacts.")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--report-out", default=None)
    parser.add_argument("--tdd", nargs="?", const="all", default=None)
    args = parser.parse_args(argv)

    if args.tdd is not None:
        return run_tdd(None if args.tdd == "all" else args.tdd)

    if args.inspect_recipes:
        result = inspect_recipes(args.recipes)
        print_messages(result)
        print(json.dumps(result.analysis, indent=2))
        return 0 if result.ok else 1

    if args.export_preyaml:
        report_path = args.report_out if args.report else None
        result = build_preyaml(
            template_path=args.template,
            recipes_path=args.recipes,
            output_path=args.out,
            mode=args.export_preyaml,
            write=not args.validate,
            report_path=report_path,
        )
        print_messages(result)
        if result.ok:
            print(f"OK: pre-YAML ready at {result.output_path}")
            if not args.validate:
                print(f"Wrote {result.output_path}")
        else:
            print("FAILED: errors block pre-YAML export")
        return 0 if result.ok else 1

    if args.export_split:
        result = write_split_artifacts(
            template_path=args.template,
            recipes_path=args.recipes,
            output_dir=args.out_dir,
            datadictionary_path=args.datadictionary,
        )
        print_messages(result)
        if result.ok:
            print(f"Wrote split artifacts to {result.analysis.get('split_output_dir')}")
            print(f"Manifest: {result.output_path}")
        else:
            print("FAILED: errors block split export")
        return 0 if result.ok else 1

    report_path = args.report_out if args.report else None
    result = compile_yaml(
        template_path=args.template,
        recipes_path=args.recipes,
        output_path=args.out,
        suffix=args.suffix,
        write=args.write and not args.validate,
        report_path=report_path,
        datadictionary_path=args.datadictionary,
    )
    print_messages(result)
    if result.ok:
        print(f"OK: finished YAML ready at {result.output_path}")
        if args.write and not args.validate:
            print(f"Wrote {result.output_path}")
    else:
        print("FAILED: errors block YAML generation")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
