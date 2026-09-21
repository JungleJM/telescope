#!/usr/bin/env python3
"""
Compile human-authored Telescope YAML into finished VM-facing YAML.

The file is intentionally self-contained for the VM copy-update workflow.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Callable


OUTPUT_SUFFIX = "_Full"
PREYAML_SUFFIX = "_preyaml"
EXPANDED_PREYAML_SUFFIX = "_preyaml_expanded"
WILDCARD_CHARS = ("%", "_", "[", "]")
DEFAULT_MANIFEST_PATH = Path("split") / "pullmanifest.yaml"


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


def normalize_template(template: dict[str, Any], result: CompileResult) -> dict[str, Any]:
    template = copy.deepcopy(template or {})
    vars_block = dict(template.get("vars") or {})
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
    resolved_cohorts: list[dict[str, Any]] = []
    for cohort in cohorts:
        name = cohort.get("name")
        pk_table = find_pk_table(cohorts, result, cohort.get("_group_key", ""))
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
) -> CompileResult:
    result = CompileResult()
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
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


def validate_yaml(template_path: str | Path | None = None, recipes_path: str | Path | None = None) -> CompileResult:
    return compile_yaml(template_path=template_path, recipes_path=recipes_path, write=False)


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
        runs = [SplitRun(run_id=f"{session_id}__run", yaml=paths["run"])]
        sessions.append(
            SplitSession(
                session_id=session_id,
                cohort=pk_name,
                pk_table=str(pk_table) if pk_table else None,
                phases=phases,
                runs=runs,
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
) -> CompileResult:
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    result = compile_yaml(template_path=template_path, recipes_path=recipes_path, write=False)
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
) -> CompileResult:
    result = plan_split_runs(template_path=template_path, recipes_path=recipes_path)
    if result.errors:
        return result
    manifest = result.analysis.get("split_plan", {})
    result.finished_yaml = manifest
    out_path = Path(output_path) if output_path else project_root() / DEFAULT_MANIFEST_PATH
    result.output_path = str(out_path)
    if write and result.ok:
        dump_yaml(manifest, out_path)
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


@dataclass
class TddCase:
    name: str
    group: str
    run: Callable[[], tuple[bool, str]]


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


def tdd_cases() -> list[TddCase]:
    def case_load_valid():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return res.ok, summarize_result(res)

    def case_malformed_yaml():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", "vars:\n  - bad: [")
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return (not res.ok and has_error(res, "yaml_load_error")), summarize_result(res)

    def case_import_recipe():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            names = [c.get("name") for c in res.finished_yaml.get("cohorts", [])]
            return ("Patients" in names and "OtherDx" in names), str(names)

    def case_override_dest():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template = """
project_folder: Test
vars: {min_date_key: 1, max_date_key: 2, ICD_Value: K50}
cohorts:
  - recipe: PatientWithDx
    name: Patients
    dest_table: MyPatients
"""
            t = write_temp_yaml(tmp, "template.yaml", template)
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return res.finished_yaml["cohorts"][0]["dest_table"] == "MyPatients", summarize_result(res)

    def case_default_dest():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return res.finished_yaml["cohorts"][0]["dest_table"] == "Patients", summarize_result(res)

    def case_infer_vars():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            req = res.analysis["required_vars"]["Patients"]
            return all(v in req for v in ("min_date_key", "max_date_key", "ICD_Value")), str(req)

    def case_missing_var():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template = tiny_template().replace("  ICD_Value:\n    - K50\n    - K51\n", "")
            t = write_temp_yaml(tmp, "template.yaml", template)
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return has_error(res, "missing_variable"), summarize_result(res)

    def case_render_in():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            text = json.dumps(res.finished_yaml)
            return "dt.Value IN ('K50', 'K51')" in text, text

    def case_render_like():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template = tiny_template().replace("- K50\n    - K51", "- K50.%\n    - K51.%")
            t = write_temp_yaml(tmp, "template.yaml", template)
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            text = json.dumps(res.finished_yaml)
            return "dt.Value LIKE 'K50.%'" in text and " OR " in text, text

    def case_like_underscore_warn():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template = tiny_template().replace("- K50\n    - K51", "- K50_%")
            t = write_temp_yaml(tmp, "template.yaml", template)
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return has_warning(res, "like_underscore"), summarize_result(res)

    def case_multiplier_split_missing_column():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
multipliers:
  - name: BadSplit
    stage: split_after_build
    applies_to: PKTable
    levels:
      - strat: bad
        column: MissingRace
        values: [x]
"""
            t = write_temp_yaml(tmp, "template.yaml", tiny_template(extra))
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            return has_error(res, "missing_split_column"), summarize_result(res)

    def case_multiplier_group_pk():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
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
"""
            template = tiny_template(extra).replace(
                "  ICD_Value:\n    - K50\n    - K51\n",
                "",
            )
            t = write_temp_yaml(tmp, "template.yaml", template)
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            cohorts = {c["name"]: c for c in res.finished_yaml.get("cohorts", [])}
            ok = (
                res.ok
                and "##JVM_APatients AS pk" in json.dumps(cohorts.get("AOtherDx", {}))
                and "##JVM_BPatients AS pk" in json.dumps(cohorts.get("BOtherDx", {}))
            )
            return ok, summarize_result(res) + " " + json.dumps(cohorts)

    def case_multiplier_split_metadata():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
multipliers:
  - name: Race
    stage: split_after_build
    applies_to: PKTable
    levels:
      - strat: black
        column: FirstRace
        values:
          - Black %
"""
            t = write_temp_yaml(tmp, "template.yaml", tiny_template(extra))
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            pk = next(c for c in res.finished_yaml.get("cohorts", []) if c.get("name") == "blackPatients")
            text = json.dumps(pk)
            return "split_after_build" in pk and "pk.FirstRace LIKE 'Black %'" in text, text

    def case_batching_chunk():
        norm = normalize_batching([{"chunk": 2000}], load_yaml_from_text(tiny_recipes()), CompileResult())
        return norm[0].get("rows_per_batch") == 2000, str(norm)

    def case_batching_metadata():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
batching:
  - sex
  - chunk: 2000
"""
            t = write_temp_yaml(tmp, "template.yaml", tiny_template(extra))
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = compile_yaml(t, r)
            first = res.finished_yaml["cohorts"][0]
            return "batching" in first and len(first["batching"]) == 2, json.dumps(first)

    def case_batching_include_other():
        recipes_doc = load_yaml_from_text(tiny_recipes())
        norm = normalize_batching([{"sex": {"values": ["Female"], "include_other": True}}], recipes_doc, CompileResult())
        first = norm[0]
        return (
            first.get("name") == "sex"
            and first.get("values") == ["Female"]
            and first.get("include_other") is True
            and first.get("column") == "Sex"
        ), json.dumps(first)

    def case_cosmos_dual():
        cohorts = [{"name": "Patients", "dest_table": "Patients"}]
        res = CompileResult()
        out = expand_cosmos({"cosmos_db": "Dual"}, cohorts, res)
        names = [c["dest_table"] for c in out]
        return names == ["Patients", "Patients_sp"], str(names)

    def case_cosmos_bad_value():
        res = CompileResult()
        validate_cosmos({"cosmos_db": "Mars"}, res)
        return has_error(res, "bad_cosmos_db"), summarize_result(res)

    def case_report():
        res = CompileResult()
        res.error("x", "bad")
        res.warn("y", "careful")
        res.finished_yaml = {"cohorts": [{"name": "Patients", "dest_table": "Patients"}]}
        res.analysis = {"required_table_columns": {"OtherDx": {"PKTable": ["PatientDurableKey"]}}}
        report = build_report(res)
        return all(s in report for s in ["Errors", "Warnings", "Patients", "Required Columns"]), report

    def case_preyaml_symbolic():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
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
            t = write_temp_yaml(tmp, "template.yaml", tiny_template(extra))
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = build_preyaml(t, r, mode="symbolic")
            cohorts = res.finished_yaml.get("cohorts", [])
            ok = (
                res.ok
                and cohorts[0].get("recipe") == "PatientWithDx"
                and "multipliers" in res.finished_yaml
                and "batching" in res.finished_yaml
            )
            return ok, json.dumps(res.finished_yaml)

    def case_preyaml_expanded_recipes():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            extra = """
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
            t = write_temp_yaml(tmp, "template.yaml", tiny_template(extra))
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = build_preyaml(t, r, mode="expanded-recipes")
            cohorts = res.finished_yaml.get("cohorts", [])
            text = json.dumps(res.finished_yaml)
            ok = (
                res.ok
                and cohorts[0].get("name") == "Patients"
                and "recipe" not in cohorts[0]
                and "DiagnosisEventFact AS dxf" in text
                and "APatients" not in text
                and "batching" in res.finished_yaml
            )
            return ok, text

    def case_split_plan_basic():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            res = plan_split_runs(t, r)
            plan = res.analysis.get("split_plan", {})
            sessions = plan.get("sessions", [])
            first = sessions[0] if sessions else {}
            phases = first.get("phases", {})
            runs = first.get("runs", [])
            ok = (
                res.ok
                and plan.get("manifest_version") == 1
                and len(sessions) == 1
                and set(phases) == {"setup", "upload_cohorts", "pk"}
                and phases["pk"].get("pk_source", {}).get("kind") == "generated"
                and len(runs) == 1
                and runs[0].get("run_id") == "Patients__run"
            )
            return ok, json.dumps(plan)

    def case_manifest_basic():
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = write_temp_yaml(tmp, "template.yaml", tiny_template())
            r = write_temp_yaml(tmp, "recipes.yaml", tiny_recipes())
            out = tmp / "pullmanifest.yaml"
            res = build_pullmanifest(t, r, output_path=out, write=True)
            manifest = res.finished_yaml
            first = manifest.get("sessions", [{}])[0]
            run = first.get("runs", [{}])[0]
            pk_phase = first.get("phases", {}).get("pk", {})
            ok = (
                res.ok
                and out.exists()
                and manifest.get("manifest_version") == 1
                and first.get("status") == "pending"
                and pk_phase.get("status") == "pending"
                and pk_phase.get("rows") is None
                and pk_phase.get("error") is None
                and run.get("status") == "pending"
                and run.get("outputs") == {}
            )
            return ok, json.dumps(manifest)

    return [
        TddCase("loading.valid_template", "loading", case_load_valid),
        TddCase("loading.malformed_yaml", "loading", case_malformed_yaml),
        TddCase("recipes.import_recipe", "recipes", case_import_recipe),
        TddCase("recipes.override_dest_table", "recipes", case_override_dest),
        TddCase("recipes.default_dest_table", "recipes", case_default_dest),
        TddCase("inference.required_vars", "inference", case_infer_vars),
        TddCase("validation.missing_var_error", "validation", case_missing_var),
        TddCase("rendering.sql_condition_in", "rendering", case_render_in),
        TddCase("rendering.sql_condition_like", "rendering", case_render_like),
        TddCase("rendering.like_underscore_warning", "rendering", case_like_underscore_warn),
        TddCase("multipliers.split_missing_column", "multipliers", case_multiplier_split_missing_column),
        TddCase("multipliers.group_specific_pk", "multipliers", case_multiplier_group_pk),
        TddCase("multipliers.split_metadata", "multipliers", case_multiplier_split_metadata),
        TddCase("batching.chunk_shorthand", "batching", case_batching_chunk),
        TddCase("batching.metadata_visible", "batching", case_batching_metadata),
        TddCase("batching.include_other_metadata", "batching", case_batching_include_other),
        TddCase("cosmos.dual_suffix", "cosmos", case_cosmos_dual),
        TddCase("cosmos.bad_value", "cosmos", case_cosmos_bad_value),
        TddCase("reports.includes_sections", "reports", case_report),
        TddCase("preyaml.symbolic", "preyaml", case_preyaml_symbolic),
        TddCase("preyaml.expanded_recipes", "preyaml", case_preyaml_expanded_recipes),
        TddCase("split_plan.basic_session", "split_plan", case_split_plan_basic),
        TddCase("manifest.basic", "manifest", case_manifest_basic),
    ]


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


def run_tdd(group: str | None = None) -> int:
    cases = [c for c in tdd_cases() if group in (None, c.group)]
    passed = 0
    failed = 0
    print("TDD Results\n")
    for case in cases:
        try:
            ok, detail = case.run()
        except Exception as exc:
            ok, detail = False, repr(exc)
        if ok:
            passed += 1
            print(f"PASS  {case.name}")
        else:
            failed += 1
            print(f"FAIL  {case.name}")
            print(f"      {detail}")
    print(f"\nSummary: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


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
    parser.add_argument("--suffix", default=OUTPUT_SUFFIX)
    parser.add_argument("--write", action="store_true", help="Write finished YAML if validation passes.")
    parser.add_argument("--validate", action="store_true", help="Validate without writing output.")
    parser.add_argument("--inspect-recipes", action="store_true")
    parser.add_argument("--export-preyaml", choices=("symbolic", "expanded-recipes"), default=None)
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

    report_path = args.report_out if args.report else None
    result = compile_yaml(
        template_path=args.template,
        recipes_path=args.recipes,
        output_path=args.out,
        suffix=args.suffix,
        write=args.write and not args.validate,
        report_path=report_path,
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
