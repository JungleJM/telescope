#!/usr/bin/env python3
"""
Compile human-authored YAML Manager templates into VM-facing YAML artifacts.

The file is intentionally self-contained for the VM copy-update workflow.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import hashlib
import io
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
TRANSFER_SUFFIX = "_transfer"
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
    # What to change, and where. On the VM the YAML is edited by hand (D49), so
    # an error that only says what is wrong leaves the reader guessing.
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "context": self.context,
            "fix": self.fix,
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

    def error(self, code: str, message: str, context: str = "", fix: str = "") -> None:
        self.ok = False
        self.errors.append(Message("ERROR", code, message, context, fix))

    def warn(self, code: str, message: str, context: str = "", fix: str = "") -> None:
        self.warnings.append(Message("WARN", code, message, context, fix))


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


YAML_SYNTAX_FIX = "Correct the YAML syntax at the line and column named above."
MISSING_TEMPLATE_FIX = (
    "Pass `--template` with the file to use. On the VM that is a transfer YAML, "
    "exported on the Mac with `makeYaml.py --export-transfer`."
)


def missing_template_message(template_path: Path) -> str | None:
    """A useful sentence for a template that does not exist, or None if it does.

    The bundle ships no template (D49): the VM works from transfer YAMLs, so
    running without --template from an extracted bundle points at nothing.
    """
    path = Path(template_path)
    if path.is_file():
        return None
    return f"No template at {path}."


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


def batching_reference(item: Any) -> str | None:
    """The batching recipe a batching item names, or None if it stands alone.

    `sex` and `{state: {values: [...]}}` lean on recipes.yaml; `2000`,
    `{chunk: 2000}` and a full definition with a `name` do not.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and len(item) == 1:
        key = next(iter(item))
        if key not in ("chunk", "name"):
            return str(key)
    return None


def recipe_references(template: dict[str, Any]) -> list[str]:
    """Every place a template leans on a recipes file, as field paths.

    A transfer YAML (D49) has none, which is what lets it split with no
    recipes file at all.
    """
    refs = []
    for idx, cohort in enumerate(template.get("cohorts", []) or []):
        if isinstance(cohort, dict) and "recipe" in cohort:
            refs.append(f"cohorts[{idx}].recipe: {cohort['recipe']}")
    for idx, item in enumerate(template.get("batching", []) or []):
        name = batching_reference(item)
        if name:
            refs.append(f"batching[{idx}]: {name}")
    return refs


def load_recipes(recipes_path: Path, template: dict[str, Any], result: CompileResult) -> dict[str, Any] | None:
    """The recipes document, read only when the template refers to it.

    None means an error was recorded. A template that refers to nothing gets an
    empty document, so a missing or unreadable recipes file cannot stop it.
    """
    refs = recipe_references(template)
    if not refs:
        return {}
    if not recipes_path.is_file():
        result.error(
            "recipes_not_found",
            f"This YAML refers to recipes ({'; '.join(refs)}), but there is no "
            f"recipes file at {recipes_path}.",
            refs[0].split(":")[0],
            fix="Recipes are kept on the Mac (D49). There, export this template with "
            "`makeYaml.py --export-transfer`, which writes every recipe out in full, and "
            "bring that file across. Or pass `--recipes` with the recipes file.",
        )
        return None
    try:
        return load_yaml(recipes_path) or {}
    except Exception as exc:
        result.error(
            "yaml_load_error",
            f"Could not read the recipes file: {exc}",
            str(recipes_path),
            fix="Correct the YAML syntax at the line named above.",
        )
        return None


def cohort_label(cohort: dict[str, Any]) -> str:
    """Where a cohort is in the file and what it is called: `cohorts[1] (Patients)`."""
    name = cohort.get("name") or cohort.get("dest_table") or "cohort"
    source = cohort.get("_source")
    return f"{source} ({name})" if source else str(name)


def import_recipes(template: dict[str, Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
    recipes = recipe_index(recipes_doc)
    imported: list[dict[str, Any]] = []
    for idx, cohort in enumerate(template.get("cohorts", []) or []):
        if not isinstance(cohort, dict):
            result.error(
                "invalid_cohort",
                "Each cohort must be a mapping.",
                f"cohorts[{idx}]",
                fix="Write the cohort as keys under a `- `, e.g. `- name: Patients` "
                "followed by `type:`, `from:` and `columns:`.",
            )
            continue
        if "recipe" in cohort:
            recipe_name = cohort["recipe"]
            if recipe_name not in recipes:
                available = ", ".join(sorted(recipes)) or "none"
                result.error(
                    "missing_recipe",
                    f"Recipe `{recipe_name}` was not found.",
                    f"cohorts[{idx}].recipe",
                    fix=f"Correct the name. Recipes available: {available}.",
                )
                continue
            merged = deep_merge(copy.deepcopy(recipes[recipe_name]), cohort)
            merged["_recipe"] = recipe_name
            merged.pop("recipe", None)
        else:
            merged = copy.deepcopy(cohort)
        merged["_source"] = f"cohorts[{idx}]"
        if not merged.get("name"):
            merged["name"] = merged.get("dest_table") or merged.get("_recipe") or f"cohort_{idx + 1}"
            result.warn(
                "default_name",
                f"Cohort had no name; `{merged['name']}` was assigned.",
                f"cohorts[{idx}]",
                fix="Add `name:` to the cohort.",
            )
        if not merged.get("dest_table"):
            merged["dest_table"] = merged["name"]
            result.warn(
                "default_dest_table",
                f"`dest_table` defaulted to cohort name `{merged['name']}`.",
                cohort_label(merged),
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
            ", ".join(cohort_label(c) for c in pk),
            fix="Keep `type: PK` on one cohort, or bind `vars: {PKTable: <table>}` "
            "on each cohort that reads the PK.",
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
            fix="Remove `type: pk` from all but one entry under `upload_cohorts`.",
        )
        return None
    if not pk_uploads:
        return None
    upload = pk_uploads[0]
    if not upload.get("key_columns"):
        result.error(
            "uploaded_pk_missing_keys",
            "Uploaded PK cohort must declare `key_columns`.",
            f"upload_cohorts ({upload.get('name')}).key_columns",
            fix="Add `key_columns: [<column>, ...]` naming the columns in the file "
            "that identify a row, e.g. `[PatientDurableKey]`.",
        )
    return str(upload.get("dest_table") or upload.get("name"))


MAX_LISTED_CANDIDATES = 6


def unbound_table_input_message(
    cohort: dict[str, Any],
    table_var: str,
    meta: dict[str, Any],
    table_schemas: dict[str, list[str] | None],
    upload_tables: set[str],
) -> tuple[str, str]:
    """Explain an unbound table input and name what could fill it, as (message, fix).

    It suggests; it never picks. Binding the wrong table would produce SQL that
    runs and returns the wrong rows, so the choice stays with the author. The
    candidates are what makes the error actionable: tables in this template
    whose known columns cover everything the recipe reads through the alias.
    """
    name = cohort.get("name")
    alias = meta.get("alias")
    needed = list(meta.get("required_columns") or [])
    own = cohort.get("dest_table") or name

    def label(table: str) -> str:
        return f"{table} ({'upload' if table in upload_tables else 'cohort'})"

    fits: list[str] = []
    unknown: list[str] = []
    for table, columns in sorted(table_schemas.items()):
        if table == own:
            continue
        if columns is None:
            unknown.append(label(table))
        elif all(col in columns for col in needed):
            fits.append(label(table))

    def listing(items: list[str]) -> str:
        shown = ", ".join(items[:MAX_LISTED_CANDIDATES])
        extra = len(items) - MAX_LISTED_CANDIDATES
        return shown + (f", and {extra} more" if extra > 0 else "")

    columns_text = f"column(s) {', '.join(needed)}" if needed else "no particular columns"
    parts = [
        f"Cohort `{name}` joins a table through `{table_var}` (as `{alias}`) and reads "
        f"{columns_text} from it, but nothing binds `{table_var}`."
    ]
    if fits:
        parts.append(f"Tables in this template that fit: {listing(fits)}.")
    if unknown:
        parts.append(f"Schema unknown, so they may fit: {listing(unknown)}.")
    if not fits and not unknown:
        parts.append(
            "No table in this template provides those columns; add an upload "
            "cohort or a cohort that produces them."
        )
    example = (fits or unknown or ["<table>"])[0].split(" (")[0]
    recipe = cohort.get("_recipe")
    where = f"the cohort using recipe `{recipe}`" if recipe else "this cohort"
    return " ".join(parts), f"Bind it on {where}: `vars: {{{table_var}: {example}}}`."


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
            f"upload_cohorts ({uploaded_pk_table}); "
            + ", ".join(cohort_label(c) for c in generated_pk),
            fix="Use one PK: remove `type: pk` from the upload, or remove `type: PK` "
            "from the cohorts named here.",
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
        table_inputs = analysis["table_inputs"].get(name, {})
        for var, paths in required.items():
            if var in vars_for_cohort:
                continue
            if var in table_inputs:
                # A table input is not a plain value: say what kind of table it
                # needs and which ones in this template could supply it.
                message, fix = unbound_table_input_message(
                    cohort, var, table_inputs[var], table_schemas, set(uploads)
                )
                result.error(
                    "unbound_table_input",
                    message,
                    f"{cohort_label(cohort)}: " + ", ".join(paths),
                    fix=fix,
                )
            else:
                result.error(
                    "missing_variable",
                    f"Cohort `{name}` requires variable `{var}`, but no value was provided.",
                    f"{cohort_label(cohort)}: " + ", ".join(paths),
                    fix=f"Add `{var}: <value>` under the top-level `vars`, or under this "
                    "cohort's own `vars`.",
                )
        for table_var, cols in analysis["required_table_columns"].get(name, {}).items():
            table_name = vars_for_cohort.get(table_var)
            if not table_name:
                continue
            table_name = str(table_name)
            if table_name not in table_schemas:
                known = ", ".join(sorted(table_schemas)) or "none"
                result.error(
                    "missing_input_table",
                    f"Cohort `{name}` uses `{table_var}={table_name}`, but no cohort/upload table provides it.",
                    f"{cohort_label(cohort)}: vars.{table_var}",
                    fix=f"Set `{table_var}` to the `dest_table` of a cohort or upload in this "
                    f"file ({known}), or add an upload that provides `{table_name}`.",
                )
                continue
            if table_schemas[table_name] is None:
                continue
            missing = [col for col in cols if col not in table_schemas[table_name]]
            if missing:
                result.error(
                    "missing_input_column",
                    f"Cohort `{name}` uses `{table_var}={table_name}`, but `{table_name}` is missing columns: {', '.join(missing)}.",
                    f"{cohort_label(cohort)}: vars.{table_var}",
                    fix=f"Add {', '.join(missing)} to `{table_name}` (its `columns`, or the "
                    f"upload file's header), or bind `{table_var}` to a table that has them.",
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
    for idx, upload in enumerate(template.get("upload_cohorts", []) or []):
        if isinstance(upload, dict) and upload.get("name"):
            item = copy.deepcopy(upload)
            item["_source"] = f"upload_cohorts[{idx}]"
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
        where = f"{upload.get('_source', 'upload_cohorts')} ({upload.get('name')})"
        file_type = str(upload.get("file_type", "")).lower()
        if file_type == "csv" and upload.get("file_loc"):
            file_path = resolve_file(base_dir, upload["file_loc"])
            if not file_path.exists():
                result.error(
                    "missing_upload_file",
                    f"Upload file not found: {file_path}",
                    f"{where}.file_loc",
                    fix=f"Correct `file_loc`; a relative path is read from {base_dir}. Or "
                    "copy the file to where it points.",
                )
                schemas[dest] = None
                continue
            try:
                with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.reader(handle)
                    schemas[dest] = next(reader, [])
            except Exception as exc:
                result.error(
                    "upload_read_error",
                    f"Could not read upload CSV `{file_path}`: {exc}",
                    f"{where}.file_loc",
                    fix="Save the file as a UTF-8 CSV with a header row.",
                )
                schemas[dest] = []
        elif file_type in ("dbtable", "parquet"):
            schema = upload.get("columns") or upload.get("schema") or []
            if schema and isinstance(schema[0], dict):
                schemas[dest] = [str(c.get("name")) for c in schema if c.get("name")]
            elif schema:
                schemas[dest] = [str(c) for c in schema]
            else:
                # Unknown, not empty. An empty list would claim the table has no
                # columns, so binding a recipe to it reported every column it
                # reads as missing -- when the truth is only that nothing
                # locally can check.
                schemas[dest] = None
                result.warn(
                    "upload_schema_unknown",
                    f"Upload `{dest}` has no locally discoverable schema.",
                    where,
                    fix="List its columns under `columns:` so the cohorts that read it can be checked.",
                )
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
                f"{upload.get('_source', 'upload_cohorts')} ({name}).push_this_cycle",
                fix="Set `push_this_cycle: true`, or add `assume_exists: true` if the table "
                "is already in the Projects database.",
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
                result.error(
                    "bad_sql_condition",
                    f"Could not parse sql_condition expression `{expr}`.",
                    context,
                    fix="Write it as `{{ sql_condition('Column', VarName) }}`: a quoted "
                    "column, then the variable holding the values.",
                )
                return match.group(0)
            column = strip_quotes(args[0])
            var_name = args[1].strip()
            if var_name not in vars_for_cohort:
                result.error(
                    "missing_variable",
                    f"`sql_condition` references missing variable `{var_name}`.",
                    context,
                    fix=f"Add `{var_name}: <value or list>` under the top-level `vars`, or "
                    "under this cohort's own `vars`.",
                )
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
        result.error(
            "missing_variable",
            f"Missing variable `{expr}`.",
            context,
            fix=f"Add `{expr}: <value>` under the top-level `vars`, or under this "
            "cohort's own `vars`.",
        )
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
        out = render_value(clean, vars_for_cohort, result, cohort_label(cohort))
        # Kept so later checks can say where the cohort is; stripped before output.
        if "_source" in cohort:
            out["_source"] = cohort["_source"]
        rendered.append(out)
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
    for idx, mult in enumerate(template.get("multipliers", []) or []):
        if not isinstance(mult, dict):
            continue
        where = f"multipliers[{idx}] ({mult.get('name')})"
        stage = mult.get("stage")
        if stage not in ("during_build", "split_after_build"):
            result.error(
                "bad_multiplier_stage",
                f"Unsupported multiplier stage `{stage}`.",
                f"{where}.stage",
                fix="Use `stage: during_build` (each level builds its own cohorts) or "
                "`stage: split_after_build` (one build, split by a PK column).",
            )
        if stage == "split_after_build":
            targets = pk_candidates if mult.get("applies_to") == "PKTable" else [mult.get("applies_to")]
            target_cols = sorted({col for target in targets for col in (table_schemas.get(str(target)) or [])})
            for level_idx, level in enumerate(mult.get("levels", []) or []):
                col = level.get("column") if isinstance(level, dict) else None
                if col and col not in target_cols:
                    result.error(
                        "missing_split_column",
                        f"Multiplier `{mult.get('name')}` references missing column `{col}` on `{mult.get('applies_to')}`.",
                        f"{where}.levels[{level_idx}] ({level.get('strat')}).column",
                        fix=f"Add `{col}` to the columns of `{mult.get('applies_to')}`, or "
                        "correct `column` to one it has: "
                        f"{', '.join(target_cols) or 'none known'}.",
                    )


def expand_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    normalized = public_batching(normalize_batching(template.get("batching", []) or [], recipes_doc, CompileResult()))
    for cohort in cohorts:
        cohort["batching"] = normalized
    return cohorts


BATCHING_FORMS = (
    "Write each batching item as a batching recipe name (`sex`, Mac only), "
    "`chunk: <rows>`, or a full definition: `{name: sex, kind: column_values, "
    "applies_to: PKTable, column: Sex, values: [Female, Male]}`."
)


def normalize_batching(batch_items: list[Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
    """Every batching item as a full definition, with its template overrides applied.

    Each carries `_source` (its place under `batching`) for error messages; it
    is stripped wherever a definition is written out.
    """
    presets = {item["name"]: item for item in recipes_doc.get("batching_recipes", []) or [] if isinstance(item, dict) and item.get("name")}
    normalized = []
    for idx, item in enumerate(batch_items):
        where = f"batching[{idx}]"
        if isinstance(item, int) and not isinstance(item, bool):
            entry = {"name": "chunk", "kind": "row_chunk", "rows_per_batch": item, "applies_to": "PKTable"}
        elif isinstance(item, dict) and "chunk" in item:
            entry = {"name": "chunk", "kind": "row_chunk", "rows_per_batch": item["chunk"], "applies_to": "PKTable"}
        elif isinstance(item, str) and item in presets:
            entry = copy.deepcopy(presets[item])
        elif isinstance(item, dict) and len(item) == 1 and next(iter(item)) in presets:
            name = next(iter(item))
            entry = deep_merge(presets[name], item[name] or {})
        elif isinstance(item, dict) and item.get("name"):
            entry = copy.deepcopy(item)
        else:
            available = ", ".join(sorted(presets))
            known = f" Batching recipes available: {available}." if available else ""
            result.error(
                "bad_batching",
                f"Could not understand batching item `{item}`.",
                where,
                fix=BATCHING_FORMS + known,
            )
            continue
        entry["_source"] = where
        normalized.append(entry)
    return normalized


def public_batching(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


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
        label = cohort_label(cohort)
        aliases = cohort_aliases(cohort)

        for table in sorted(set(aliases.values())):
            if is_generated_reference(table):
                continue
            if table not in dictionary:
                result.error(
                    "unknown_table",
                    f"Table `{table}` is not in the data dictionary.",
                    f"{label}: from/join",
                    fix=f"Correct the table name in `from` or `join`, or add `{table}` to "
                    "YAMLs/datadictionary.yaml on the Mac and rebuild the bundle.",
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
                    f"{label}: columns ({column.get('name')})",
                )
                continue

            alias, column_name = match.group("alias"), match.group("column")
            table = aliases.get(alias)
            if table is None:
                declared_aliases = ", ".join(sorted(aliases)) or "none"
                result.error(
                    "unknown_alias",
                    f"Source `{source}` uses alias `{alias}`, which is not declared "
                    f"in this cohort's `from` or `join`.",
                    f"{label}: columns ({column.get('name')}).source",
                    fix=f"Use one of this cohort's aliases ({declared_aliases}), or add a "
                    f"`join` that declares `{alias}`.",
                )
                continue
            if is_generated_reference(table) or table not in dictionary:
                continue

            dd_columns = (dictionary[table] or {}).get("columns") or {}
            if column_name not in dd_columns:
                result.error(
                    "unknown_column",
                    f"Column `{column_name}` is not listed under `{table}` in the "
                    f"data dictionary.",
                    f"{label}: columns ({column.get('name')}).source",
                    fix=f"Check the spelling of `{column_name}`, and that `{alias}` is the "
                    "alias of the table that has it.",
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
                    f"{label}: columns ({column.get('name')}).type",
                )
                continue
            if tsql_base_type(declared) not in accepted:
                dd_type = (dd_columns[column_name] or {}).get("type")
                result.error(
                    "dd_type_mismatch",
                    f"`{source}` is declared `{declared}`, but the data dictionary "
                    f"says `{table}.{column_name}` is `{dd_type}`.",
                    f"{label}: columns ({column.get('name')}).type",
                    fix=f"Change `type` to a type compatible with `{dd_type}`.",
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



BATCHING_KINDS = ("column_values", "row_chunk")


def validate_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], table_schemas: dict[str, list[str] | None], result: CompileResult) -> None:
    """Check each batching definition field by field.

    On the VM these are written out in full and edited by hand (D49), so a
    missing `column` or a chunk with no size has to be caught here, with the
    field named, rather than surface as a confusing split or a failed run.
    """
    normalized = normalize_batching(template.get("batching", []) or [], recipes_doc, result)
    pk_candidates = [c.get("dest_table") for c in cohorts if str(c.get("type", "")).lower() == "pk"]
    pk_cols = sorted({col for pk_table in pk_candidates for col in (table_schemas.get(str(pk_table)) or [])})
    for item in normalized:
        where = f"{item.get('_source', 'batching')} ({item.get('name')})"
        kind = str(item.get("kind") or "column_values")
        if kind not in BATCHING_KINDS:
            result.error(
                "bad_batching_kind",
                f"Batching `{item.get('name')}` has unknown kind `{kind}`.",
                f"{where}.kind",
                fix="Use `kind: column_values` (one run per value of a PK column) or "
                "`kind: row_chunk` (fixed-size slices of the PK).",
            )
            continue
        if kind == "row_chunk":
            size = item.get("rows_per_batch")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                result.error(
                    "bad_chunk_size",
                    f"Batching `{item.get('name')}` needs a whole number of rows per "
                    f"batch, not `{size}`.",
                    f"{where}.rows_per_batch",
                    fix="Give it a size: `chunk: 2000`, or `rows_per_batch: 2000` in a "
                    "full definition.",
                )
            continue
        col = item.get("column")
        if not col:
            result.error(
                "batching_missing_column",
                f"Batching `{item.get('name')}` does not say which PK column to split on.",
                f"{where}.column",
                fix="Add `column: <PK column>`, e.g. `column: Sex`.",
            )
            continue
        if col not in pk_cols:
            result.error(
                "missing_batch_column",
                f"Batching `{item.get('name')}` requires missing PK column `{col}`.",
                f"{where}.column",
                fix=f"Output `{col}` from the PK cohort, or correct `column` to one it has: "
                f"{', '.join(pk_cols) or 'none known'}.",
            )
        values = item.get("values")
        if values == "all":
            result.warn(
                "batching_values_all",
                f"Batching `{item.get('name')}` uses `values: all`, which is not supported "
                "yet: the pull stops when it reaches this batch.",
                f"{where}.values",
                fix="List the values: `values: [LA, MS, ...]`, with `include_other: true` "
                "to catch the rest.",
            )
        elif not isinstance(values, list) or not values:
            result.error(
                "batching_missing_values",
                f"Batching `{item.get('name')}` has no `values` to split `{col}` by.",
                f"{where}.values",
                fix="Add `values: [<value>, ...]`, with `include_other: true` to catch the rest.",
            )


COSMOS_DB_FIX = "Use `cosmos_db: COSMOS`, `cosmos_db: COSMOS_SneakPeek`, or `cosmos_db: Dual` for both."


TEMP_MARKER = "##JVM_"


def temp_base(name: Any) -> str:
    """`##JVM_Patients`, `JVM_Patients` or `Patients`: the bare table name."""
    text = str(name or "").strip().lstrip("#")
    if text.upper().startswith("JVM_"):
        text = text[4:]
    return text


def assign_sessions(cohorts: list[dict[str, Any]], uploaded_pk: str | None) -> None:
    """Record on each cohort, as `session_pk`, the PK whose session builds it.

    A session is one PK and everything pulled for it. A PK cohort owns itself;
    any other cohort belongs to its multiplier group's PK, which is also the
    PK its `PKTable` is bound to (a template has one PK per group, or
    `multiple_pk_cohorts` stops it). With an uploaded PK there is one session.
    Without this, the split put every cohort in every session, where some
    joined another session's PK.
    """
    group_pk: dict[str, str] = {}
    for cohort in cohorts:
        if str(cohort.get("type", "")).lower() == "pk":
            group_pk.setdefault(str(cohort.get("_group_key", "")), str(cohort.get("dest_table") or cohort.get("name")))
    for cohort in cohorts:
        owner = group_pk.get(str(cohort.get("_group_key", ""))) or uploaded_pk
        if owner:
            cohort["session_pk"] = owner


def expand_cosmos(template: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
    cosmos = str(template.get("cosmos_db", "COSMOS"))
    value = cosmos.lower()
    generated = {str(c.get("dest_table") or c.get("name")) for c in cohorts}
    if value in ("cosmos",):
        return cohorts
    if value in ("cosmos_sneakpeek", "sneakpeek", "sp"):
        return [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek", generated) for c in cohorts]
    if value in ("dual", "both"):
        return cohorts + [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek", generated) for c in cohorts]
    result.error(
        "bad_cosmos_db",
        f"Unsupported cosmos_db value `{cosmos}`.",
        "cosmos_db",
        fix=COSMOS_DB_FIX,
    )
    return cohorts


def validate_cosmos(template: dict[str, Any], result: CompileResult) -> None:
    value = str(template.get("cosmos_db", "COSMOS")).lower()
    if value not in ("cosmos", "cosmos_sneakpeek", "sneakpeek", "sp", "dual", "both"):
        result.error(
            "bad_cosmos_db",
            f"Unsupported cosmos_db value `{template.get('cosmos_db')}`.",
            "cosmos_db",
            fix=COSMOS_DB_FIX,
        )


def with_cosmos_suffix(
    cohort: dict[str, Any], suffix: str, cosmos_db: str, generated: set[str] | None = None
) -> dict[str, Any]:
    """The cohort's copy for another Cosmos database, pointing at its own temps.

    Renaming the cohort is not enough: its SQL names the temps of the cohorts
    it reads (`##JVM_Patients`), and the copy must read their copies
    (`##JVM_Patients_sp`), or it pulls for the other database's population.
    Uploads are shared by both copies, so they keep their names.
    """
    new = copy.deepcopy(cohort)
    new["name"] = f"{new.get('name')}{suffix}"
    new["dest_table"] = f"{new.get('dest_table', new.get('name'))}{suffix}"
    new["cosmos_db"] = cosmos_db
    if generated:
        pattern = re.compile(
            re.escape(TEMP_MARKER)
            + "("
            + "|".join(re.escape(name) for name in sorted(generated, key=len, reverse=True))
            + ")(?![A-Za-z0-9_])"
        )

        def rename(value: Any) -> Any:
            if isinstance(value, str):
                return pattern.sub(lambda m: f"{TEMP_MARKER}{m.group(1)}{suffix}", value)
            if isinstance(value, list):
                return [rename(item) for item in value]
            if isinstance(value, dict):
                return {k: rename(v) for k, v in value.items()}
            return value

        for key, value in list(new.items()):
            if key not in ("name", "dest_table", "session_pk"):
                new[key] = rename(value)
        if new.get("session_pk") in generated:
            new["session_pk"] = f"{new['session_pk']}{suffix}"
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
            if msg.fix:
                lines.append(f"  - Fix: {msg.fix}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Warnings")
    if result.warnings:
        for msg in result.warnings:
            lines.append(f"- `{msg.code}`: {msg.message} {msg.context}".rstrip())
            if msg.fix:
                lines.append(f"  - Fix: {msg.fix}")
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
        result.error("template_not_found", missing, str(template_path), fix=MISSING_TEMPLATE_FIX)
        return result
    try:
        raw = load_yaml(template_path)
    except Exception as exc:
        result.error("yaml_load_error", str(exc), str(template_path), fix=YAML_SYNTAX_FIX)
        return result
    if raw is not None and not isinstance(raw, dict):
        result.error(
            "invalid_template",
            "Template YAML must be a mapping.",
            str(template_path),
            fix="Start the file with top-level keys such as `project_folder:` and `cohorts:`.",
        )
        return result
    template = normalize_template(raw, result)
    recipes_doc = load_recipes(recipes_path, template, result)
    if recipes_doc is None:
        return result

    cohorts = import_recipes(template, recipes_doc, result)
    cohorts = expand_multipliers(template, cohorts, result)
    analysis = analyze_cohorts(cohorts)
    cohorts = validate_and_resolve(template, recipes_doc, cohorts, analysis, result, template_path.parent)
    assign_sessions(cohorts, find_uploaded_pk_table(template, CompileResult()))
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
    finished["cohorts"] = [public_cohort(c) for c in rendered_cohorts]
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
        result.error("yaml_load_error", str(exc), str(recipes_path), fix=YAML_SYNTAX_FIX)
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
            fix="Remove `type: pk` from all but one entry under `upload_cohorts`.",
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
            f"upload_cohorts ({upload.get('name')}).key_columns",
            fix="Add `key_columns: [<column>, ...]` naming the columns in the file "
            "that identify a row, e.g. `[PatientDurableKey]`.",
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
                    "Two batching dimensions produce the same label.",
                    "batching",
                    fix="Change one of the `values` so the labels differ; labels keep only "
                    "letters, digits, `-` and `_`, so `A B` and `A-B` collide.",
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

    source: dict[str, Any] = {"template": str(template_path), "recipes": str(recipes_path)}
    if isinstance(finished_yaml.get("transfer"), dict):
        # A transfer YAML carries its recipes inline (D49); whatever --recipes
        # defaulted to was never read, so naming it would mislead.
        source["recipes"] = None
        source["transfer"] = copy.deepcopy(finished_yaml["transfer"])
    return SplitPlan(
        project=project_metadata(finished_yaml),
        source=source,
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
    doc.pop("transfer", None)
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
        if isinstance(cohort, dict)
        and str(cohort.get("type", "")).lower() != "pk"
        and (pk_table is None or cohort.get("session_pk") in (None, pk_table))
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
        result.error("yaml_load_error", str(exc), str(template_path), fix=YAML_SYNTAX_FIX)
        return result
    if not isinstance(template, dict):
        result.error(
            "invalid_template",
            "Template YAML must be a mapping.",
            str(template_path),
            fix="Start the file with top-level keys such as `project_folder:` and `cohorts:`.",
        )
        return result
    if mode not in ("symbolic", "expanded-recipes"):
        result.error(
            "bad_preyaml_mode",
            f"Unsupported pre-YAML mode `{mode}`.",
            mode,
            fix="Use `symbolic` or `expanded-recipes`.",
        )
        return result

    if mode == "symbolic":
        preyaml = copy.deepcopy(template)
        suffix = PREYAML_SUFFIX
    else:
        recipes_doc = load_recipes(recipes_path, template, result)
        if recipes_doc is None:
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


def transfer_output_path(template: dict[str, Any], template_path: Path) -> Path:
    """`<project>_transfer.yaml`, beside the template it came from."""
    name = str(template.get("project_folder") or template_path.stem).strip() or "project"
    clean = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "project"
    return template_path.parent / f"{clean}{TRANSFER_SUFFIX}.yaml"


def place_uploads(
    transfer: dict[str, Any],
    template_dir: Path,
    out_dir: Path,
    result: CompileResult,
    write: bool,
) -> list[str]:
    """Keep every upload at its `file_loc`, relative to the transfer YAML.

    `file_loc` is never rewritten: it is what the VM resolves, relative to the
    transfer YAML. Written beside the template, the files are already in place.
    Written elsewhere, each is copied into the output folder at the same
    relative path, so that folder is the unit to carry across. A `file_loc`
    that leaves the template's folder (`..`) or is absolute cannot be copied
    that way; it is left as written, with a warning.

    Returns each upload as `file_loc`, the path the VM will look for.
    """
    listed: list[str] = []
    same_place = out_dir.resolve() == template_dir.resolve()
    for idx, upload in enumerate(transfer.get("upload_cohorts", []) or []):
        if not isinstance(upload, dict) or not upload.get("file_loc"):
            continue
        file_loc = str(upload["file_loc"])
        listed.append(file_loc)
        if same_place:
            continue
        rel = Path(file_loc)
        if rel.is_absolute() or ".." in rel.parts:
            result.warn(
                "upload_not_copied",
                f"`{file_loc}` is outside the template's folder, so it was not copied "
                "beside the transfer YAML.",
                f"upload_cohorts[{idx}] ({upload.get('name')}).file_loc",
                fix="Put the file at that path relative to the transfer YAML on the VM, "
                "or move it under the template's folder and point `file_loc` there.",
            )
            continue
        if write:
            target = out_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolve_file(template_dir, file_loc), target)
    return listed


def build_transfer(
    template_path: str | Path | None = None,
    recipes_path: str | Path | None = None,
    output_path: str | Path | None = None,
    write: bool = False,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    """The template with every recipe written out in full, for the VM (D49).

    Cohort recipes are merged into their cohorts and batching recipes replaced
    by their full definitions. Multipliers and batching are not applied: they
    stay declared for the split on the VM. Written only if the template passes
    full validation here, so a file that will not split never leaves the Mac.
    Written to another folder, its upload files are copied alongside it.
    """
    template_path = Path(template_path) if template_path else default_template_path()
    recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
    result = compile_yaml(
        template_path=template_path,
        recipes_path=recipes_path,
        datadictionary_path=datadictionary_path,
    )
    if result.errors:
        return result

    # Validation passed, so these cannot fail; their messages were already
    # reported by the compile above and would only repeat.
    quiet = CompileResult()
    template = normalize_template(load_yaml(template_path), quiet)
    recipes_doc = load_recipes(recipes_path, template, quiet) or {}
    used = [str(cohort["recipe"]) for cohort in template.get("cohorts", []) or [] if isinstance(cohort, dict) and "recipe" in cohort]

    body = copy.deepcopy(template)
    body["cohorts"] = [public_cohort(c) for c in import_recipes(template, recipes_doc, quiet)]
    if body.get("batching"):
        body["batching"] = public_batching(normalize_batching(body["batching"], recipes_doc, quiet))
    body.pop("example_cohorts", None)
    body.pop("transfer", None)

    refs = recipe_references(template)
    provenance: dict[str, Any] = {"from_template": template_path.name}
    if refs:
        provenance["recipes_sha256"] = hashlib.sha256(recipes_path.read_bytes()).hexdigest()[:12]
        provenance["recipes_used"] = sorted(set(used)) + sorted(
            {ref.split(": ", 1)[1] for ref in refs if ref.startswith("batching")}
        )
    elif isinstance(template.get("transfer"), dict):
        # Re-exporting a transfer YAML keeps the record of where it came from.
        provenance = copy.deepcopy(template["transfer"])
    transfer = {"transfer": provenance, **body}

    out_path = Path(output_path) if output_path else transfer_output_path(template, template_path)
    if write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    result.analysis["transfer_uploads"] = place_uploads(
        transfer, template_path.parent, out_path.parent, result, write
    )
    result.finished_yaml = transfer
    result.output_path = str(out_path)
    if write:
        dump_yaml(transfer, out_path)
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

    def test_missing_template_points_at_the_transfer_export(self):
        # The bundle ships no template (D49), so the default is absent by
        # design; the error has to say what to pass rather than a bare errno.
        res = compile_yaml(self.tmp / "nope.yaml", tiny_recipes_path(self.tmp))
        self.assertHasError(res, "template_not_found")
        self.assertIn("--template", res.errors[0].fix)
        self.assertIn("--export-transfer", res.errors[0].fix)

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


class TableBindingTests(MakeYamlTest):
    """A recipe that joins a table through a placeholder must say what it needs.

    It suggests candidates but never picks one: binding the wrong table would
    produce SQL that runs and returns the wrong rows.
    """

    TEMPLATE = """
project_folder: Bind Test
cosmos_db: COSMOS
vars: {{min_date_key: 20200101, max_date_key: 20240101{extra_vars}}}
upload_cohorts:
  - name: Codes
    dest_table: Codes
    file_type: csv
    file_loc: codes.csv
  - name: Unrelated
    dest_table: Unrelated
    file_type: csv
    file_loc: unrelated.csv
{extra_uploads}
cohorts:
  - name: Patients
    type: PK
    dest_table: Patients
    columns: [{{source: p.DurableKey, name: PatientDurableKey, type: BIGINT, nullable: false}}]
    filter: {{from: PatientDim AS p}}
  - name: Visits
    type: fact
    dest_table: Visits
    columns: [{{source: e.EncounterKey, name: EncounterKey, type: BIGINT}}]
{cohort_vars}
    filter:
      from: EncounterFact AS e
      join:
        - "INNER JOIN ##JVM_{{{{CodesTable}}}} AS c ON c.Code = e.EncounterKey"
"""

    def compile(self, extra_vars="", extra_uploads="", cohort_vars=""):
        (self.tmp / "codes.csv").write_text("Code,Label\nK50,Crohns\n", encoding="utf-8")
        (self.tmp / "unrelated.csv").write_text("Something\nx\n", encoding="utf-8")
        text = self.TEMPLATE.format(
            extra_vars=extra_vars, extra_uploads=extra_uploads, cohort_vars=cohort_vars
        )
        return self.compile_template(text)

    def binding_error(self, res):
        errors = [m for m in res.errors if m.code == "unbound_table_input"]
        self.assertTrue(errors, summarize_result(res))
        return errors[0].message

    def test_an_unbound_table_input_is_named_as_a_table(self):
        message = self.binding_error(self.compile())
        self.assertIn("`CodesTable`", message)
        self.assertIn("as `c`", message)
        self.assertIn("Code", message)

    def test_suggests_only_tables_that_have_the_columns(self):
        message = self.binding_error(self.compile())
        self.assertIn("Codes (upload)", message)
        self.assertNotIn("Unrelated (upload)", message)

    def test_never_binds_on_its_own(self):
        # Even with exactly one fitting table, the author has to choose it.
        self.assertFalse(self.compile().ok)

    def test_tables_of_unknown_shape_are_offered_as_possible(self):
        extra = """  - name: FromProjects
    dest_table: FromProjects
    file_type: dbtable
"""
        message = self.binding_error(self.compile(extra_uploads=extra))
        self.assertIn("Schema unknown", message)
        self.assertIn("FromProjects (upload)", message)

    def test_says_so_when_nothing_fits(self):
        (self.tmp / "codes.csv").write_text("Wrong\nx\n", encoding="utf-8")
        text = self.TEMPLATE.format(extra_vars="", extra_uploads="", cohort_vars="")
        (self.tmp / "unrelated.csv").write_text("Something\nx\n", encoding="utf-8")
        message = self.binding_error(self.compile_template(text))
        self.assertIn("No table in this template provides those columns", message)

    def test_shows_how_to_bind_it(self):
        errors = [m for m in self.compile().errors if m.code == "unbound_table_input"]
        self.assertIn("vars: {CodesTable: Codes}", errors[0].fix)

    def test_binding_on_the_cohort_resolves_it(self):
        # The recommended place: next to the recipe that needs it, rather than
        # as a global that reads like unexplained metadata.
        res = self.compile(cohort_vars="    vars: {CodesTable: Codes}")
        self.assertEqual([m for m in res.errors if m.code == "unbound_table_input"], [])

    def test_binding_globally_still_works(self):
        res = self.compile(extra_vars=", CodesTable: Codes")
        self.assertEqual([m for m in res.errors if m.code == "unbound_table_input"], [])

    def test_binding_to_a_table_of_unknown_shape_is_not_a_column_error(self):
        # An upload with no discoverable schema is unknown, not empty. Treating
        # it as empty reported every column the recipe reads as missing.
        extra = """  - name: FromProjects
    dest_table: FromProjects
    file_type: dbtable
"""
        res = self.compile(extra_uploads=extra, cohort_vars="    vars: {CodesTable: FromProjects}")
        self.assertNotIn("missing_input_column", [m.code for m in res.errors])
        self.assertIn("upload_schema_unknown", [m.code for m in res.warnings])

    def test_a_plain_value_is_still_a_missing_variable(self):
        template = tiny_template().replace("  ICD_Value:\n    - K50\n    - K51\n", "")
        res = self.compile_template(template)
        self.assertHasError(res, "missing_variable")
        self.assertNotIn("unbound_table_input", [m.code for m in res.errors])


class SessionMembershipTests(MakeYamlTest):
    """Each session builds only its own group's cohorts, against its own PK."""

    GROUPED = """
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
  - name: Race
    stage: split_after_build
    applies_to: PKTable
    levels:
      - strat: black
        column: FirstRace
        values: [Black]
      - strat: white
        column: FirstRace
        values: [White]
"""

    def split(self, cosmos_db: str = "Dual") -> tuple[dict[str, Any], Path]:
        text = tiny_template(self.GROUPED).replace("cosmos_db: COSMOS", f"cosmos_db: {cosmos_db}")
        out = self.tmp / "split"
        res = write_split_artifacts(*self.write_pair(text), output_dir=out)
        self.assertCompiles(res)
        return load_yaml(out / "pullmanifest.yaml"), out

    def run_cohorts(self, manifest: dict[str, Any], out: Path) -> dict[str, list[dict[str, Any]]]:
        return {
            session["pk_table"]: [
                cohort
                for run in session["runs"]
                for cohort in load_yaml(out / run["yaml"]).get("cohorts", [])
            ]
            for session in manifest["sessions"]
        }

    def test_each_session_runs_only_its_own_cohorts(self):
        manifest, out = self.split()
        by_session = self.run_cohorts(manifest, out)
        self.assertEqual(len(by_session), 8)
        for pk_table, cohorts in by_session.items():
            with self.subTest(session=pk_table):
                expected = pk_table.replace("Patients", "OtherDx")
                self.assertEqual([c["dest_table"] for c in cohorts], [expected])

    def test_each_cohort_joins_its_own_sessions_pk(self):
        # The outcome that matters: the population a fact table is pulled for.
        manifest, out = self.split()
        for pk_table, cohorts in self.run_cohorts(manifest, out).items():
            for cohort in cohorts:
                with self.subTest(session=pk_table, cohort=cohort["dest_table"]):
                    temps = set(re.findall(r"##JVM_[A-Za-z0-9_]+", json.dumps(cohort)))
                    self.assertEqual(temps, {f"##JVM_{pk_table}"})

    def test_every_cohort_is_built_exactly_once(self):
        manifest, out = self.split()
        built = [c["dest_table"] for cs in self.run_cohorts(manifest, out).values() for c in cs]
        self.assertEqual(sorted(built), sorted(set(built)))
        self.assertEqual(len(built), 8)

    def test_sneakpeek_alone_joins_the_sneakpeek_pk(self):
        manifest, out = self.split("COSMOS_SneakPeek")
        for pk_table, cohorts in self.run_cohorts(manifest, out).items():
            self.assertTrue(pk_table.endswith("_sp"), pk_table)
            for cohort in cohorts:
                temps = set(re.findall(r"##JVM_[A-Za-z0-9_]+", json.dumps(cohort)))
                self.assertEqual(temps, {f"##JVM_{pk_table}"})

    def test_an_uploaded_pk_keeps_one_session_for_both_databases(self):
        text = uploaded_pk_template().replace("cosmos_db: COSMOS", "cosmos_db: Dual")
        (self.tmp / "pks.csv").write_text(
            "PatientDurableKey,DiagnosisEventKey\n1,2\n", encoding="utf-8"
        )
        out = self.tmp / "split"
        res = write_split_artifacts(*self.write_pair(text), output_dir=out)
        self.assertCompiles(res)
        by_session = self.run_cohorts(load_yaml(out / "pullmanifest.yaml"), out)
        self.assertEqual(
            {pk: sorted(c["dest_table"] for c in cs) for pk, cs in by_session.items()},
            {"ClientPK": ["OtherDx", "OtherDx_sp"]},
        )


class TransferTests(MakeYamlTest):
    """The transfer YAML (D49): recipes written out, nothing applied."""

    EXTRA = """
upload_cohorts:
  - name: Codes
    dest_table: Codes
    file_type: csv
    file_loc: data/codes.csv
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
  - state:
      values: [LA, MS]
      include_other: true
"""

    def setUp(self):
        super().setUp()
        (self.tmp / "data").mkdir()
        (self.tmp / "data" / "codes.csv").write_text("Code\nK50\n", encoding="utf-8")
        self.template, self.recipes = self.write_pair(extra=self.EXTRA)
        self.no_recipes = self.tmp / "no_such_recipes.yaml"

    def export(self, out: Path | None = None) -> CompileResult:
        res = build_transfer(self.template, self.recipes, output_path=out, write=True)
        self.assertCompiles(res)
        return res

    def split_tree(self, template: Path, recipes: Path, out: Path) -> dict[str, str]:
        res = write_split_artifacts(template, recipes, output_dir=out)
        self.assertCompiles(res)
        tree = {}
        for path in sorted(out.rglob("*")):
            if path.is_file():
                tree[path.relative_to(out).as_posix()] = path.read_text(encoding="utf-8")
        manifest = load_yaml(out / "pullmanifest.yaml")
        manifest.pop("source")
        tree["pullmanifest.yaml"] = json.dumps(manifest, sort_keys=True, default=str)
        return tree

    def test_splits_alone_exactly_as_the_template_does(self):
        # The outcome that matters: with no recipes file at all, the VM gets
        # the same sessions, runs and SQL inputs the Mac would have produced.
        transfer = Path(self.export().output_path)
        expected = self.split_tree(self.template, self.recipes, self.tmp / "from_template")
        actual = self.split_tree(transfer, self.no_recipes, self.tmp / "from_transfer")
        self.assertEqual(sorted(expected), sorted(actual))
        for rel in expected:
            with self.subTest(file=rel):
                self.assertEqual(expected[rel], actual[rel])

    def test_the_realistic_templates_split_alone_too(self):
        cases = project_root() / "YAMLs" / "manager_test_cases"
        recipes = default_recipes_path()
        if not recipes.is_file() or not cases.is_dir():
            self.skipTest("needs the repo's recipes and test cases")
        for name in ("01_valid_basic.yaml", "02_valid_multipliers_batching.yaml"):
            with self.subTest(template=name):
                work = self.tmp / name
                shutil.copytree(cases, work)
                template = work / name
                res = build_transfer(template, recipes, write=True)
                self.assertCompiles(res)
                expected = self.split_tree(template, recipes, work / "a")
                actual = self.split_tree(Path(res.output_path), self.no_recipes, work / "b")
                self.assertEqual(expected, actual)

    def test_refers_to_no_recipes(self):
        transfer = load_yaml(self.export().output_path)
        self.assertEqual(recipe_references(transfer), [])
        self.assertEqual(transfer["batching"][0]["column"], "Sex")
        self.assertEqual(transfer["batching"][1]["values"], ["LA", "MS"])
        self.assertTrue(transfer["batching"][1]["include_other"])

    def test_applies_neither_multipliers_nor_batching(self):
        transfer = load_yaml(self.export().output_path)
        self.assertEqual([c["name"] for c in transfer["cohorts"]], ["Patients", "OtherDx"])
        self.assertEqual(len(transfer["multipliers"]), 1)
        self.assertNotIn("batching", transfer["cohorts"][0])

    def test_named_for_the_project_beside_the_template(self):
        self.assertEqual(Path(self.export().output_path), self.tmp / "Test_Run_transfer.yaml")

    def test_records_where_it_came_from(self):
        provenance = load_yaml(self.export().output_path)["transfer"]
        self.assertEqual(provenance["from_template"], "template.yaml")
        self.assertEqual(
            provenance["recipes_sha256"],
            hashlib.sha256(self.recipes.read_bytes()).hexdigest()[:12],
        )
        self.assertIn("PatientWithDx", provenance["recipes_used"])
        self.assertIn("sex", provenance["recipes_used"])

    def test_same_inputs_give_the_same_file(self):
        first = Path(self.export().output_path).read_bytes()
        second = Path(self.export().output_path).read_bytes()
        self.assertEqual(first, second)

    def test_written_elsewhere_its_folder_is_self_contained(self):
        # file_loc is what the VM resolves, so it must not become a path that
        # only exists on this machine; the file moves instead.
        out = self.tmp / "for_vm" / "IBD_transfer.yaml"
        res = self.export(out)
        self.assertEqual(load_yaml(out)["upload_cohorts"][0]["file_loc"], "data/codes.csv")
        self.assertEqual(res.analysis["transfer_uploads"], ["data/codes.csv"])
        self.assertEqual((out.parent / "data" / "codes.csv").read_text(encoding="utf-8"), "Code\nK50\n")
        (self.tmp / "data" / "codes.csv").unlink()
        self.assertCompiles(compile_yaml(out, self.no_recipes))

    def test_an_upload_outside_the_template_folder_is_left_with_a_warning(self):
        shared = self.tmp.parent / f"{self.tmp.name}_shared"
        shared.mkdir()
        self.addCleanup(shutil.rmtree, shared, True)
        (shared / "codes.csv").write_text("Code\nK50\n", encoding="utf-8")
        template = write_temp_yaml(
            self.tmp, "outside.yaml",
            tiny_template(self.EXTRA.replace("data/codes.csv", f"../{shared.name}/codes.csv")),
        )
        out = self.tmp / "for_vm" / "IBD_transfer.yaml"
        res = build_transfer(template, self.recipes, output_path=out, write=True)
        self.assertCompiles(res)
        self.assertHasWarning(res, "upload_not_copied")
        self.assertEqual(
            load_yaml(out)["upload_cohorts"][0]["file_loc"], f"../{shared.name}/codes.csv"
        )

    def test_an_invalid_template_writes_nothing(self):
        broken = write_temp_yaml(
            self.tmp, "broken.yaml", tiny_template().replace("cosmos_db: COSMOS", "cosmos_db: Nowhere")
        )
        res = build_transfer(broken, self.recipes, write=True)
        self.assertHasError(res, "bad_cosmos_db")
        self.assertFalse((self.tmp / "Test_Run_transfer.yaml").exists())

    def test_a_template_without_its_recipes_points_at_the_export(self):
        res = compile_yaml(self.template, self.no_recipes)
        self.assertHasError(res, "recipes_not_found")
        self.assertIn("--export-transfer", res.errors[0].fix)
        self.assertIn("cohorts[0].recipe: PatientWithDx", res.errors[0].message)
        self.assertEqual(len(res.errors), 1)

    def test_an_unreadable_recipes_file_is_ignored_when_nothing_refers_to_it(self):
        transfer = Path(self.export().output_path)
        garbage = write_temp_yaml(self.tmp, "garbage.yaml", "recipes: [unclosed")
        self.assertCompiles(compile_yaml(transfer, garbage))


class BatchingDefinitionTests(MakeYamlTest):
    """Batching written out in full is checked field by field (D49)."""

    def check(self, batching: str) -> CompileResult:
        return self.compile_template(extra="batching:\n" + batching)

    def assertFlags(self, res: CompileResult, code: str, field: str) -> None:
        found = [m for m in res.errors + res.warnings if m.code == code]
        self.assertTrue(found, summarize_result(res))
        self.assertTrue(found[0].context.endswith(field), found[0].context)
        self.assertTrue(found[0].fix, "no fix")

    def test_a_full_definition_compiles(self):
        self.assertCompiles(self.check(
            "  - {name: sex, kind: column_values, applies_to: PKTable, column: Sex, values: [Female]}\n"
        ))

    def test_missing_column_is_named(self):
        res = self.check("  - {name: sex, kind: column_values, values: [Female]}\n")
        self.assertFlags(res, "batching_missing_column", "batching[0] (sex).column")

    def test_missing_values_is_named(self):
        res = self.check("  - {name: sex, kind: column_values, column: Sex}\n")
        self.assertFlags(res, "batching_missing_values", ".values")

    def test_unknown_kind_is_named(self):
        res = self.check("  - {name: sex, kind: by_value, column: Sex, values: [F]}\n")
        self.assertFlags(res, "bad_batching_kind", ".kind")

    def test_a_chunk_without_a_size_is_refused(self):
        # The `chunk` batching recipe ships with `rows_per_batch: required`.
        self.assertFlags(self.check("  - chunk\n"), "bad_chunk_size", ".rows_per_batch")

    def test_a_sized_chunk_compiles_cleanly(self):
        res = self.check("  - chunk: 2000\n")
        self.assertCompiles(res)
        self.assertFalse([m for m in res.warnings if "chunk" in m.code], summarize_result(res))

    def test_values_all_warns_before_the_pull_does(self):
        res = self.check("  - state\n")
        self.assertCompiles(res)
        self.assertFlags(res, "batching_values_all", ".values")

    def test_an_unknown_item_lists_the_forms(self):
        res = self.check("  - nosuch\n")
        self.assertFlags(res, "bad_batching", "batching[0]")
        self.assertIn("chunk: <rows>", res.errors[0].fix)


class FixTests(unittest.TestCase):
    """Every error says what to change (D49): on the VM the YAML is edited by hand."""

    def test_every_error_carries_a_fix(self):
        import ast

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        missing = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "error"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "result"
                and not any(kw.arg == "fix" for kw in node.keywords)
            ):
                missing.append(node.lineno)
        self.assertEqual(missing, [], "result.error(...) without fix= at these lines")

    def test_the_fix_is_printed_under_its_error(self):
        result = CompileResult()
        result.error("x", "Broken.", "cohorts[0]", fix="Mend it.")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_messages(result)
        self.assertEqual(
            out.getvalue().splitlines(),
            ["ERROR [x] at cohorts[0]: Broken.", "      fix: Mend it."],
        )

    def test_errors_point_at_the_cohort_by_position_and_name(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template = write_temp_yaml(tmp, "t.yaml", tiny_template().replace("ICD_Value:", "Unused:"))
            res = compile_yaml(template, tiny_recipes_path(tmp))
        contexts = [m.context for m in res.errors if m.code == "missing_variable"]
        self.assertTrue(contexts, summarize_result(res))
        self.assertTrue(contexts[0].startswith("cohorts[0] (Patients)"), contexts[0])


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
    "table_binding": TableBindingTests,
    "sessions": SessionMembershipTests,
    "transfer": TransferTests,
    "batching_definitions": BatchingDefinitionTests,
    "fixes": FixTests,
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
    for label, messages in (("ERROR", result.errors), ("WARN ", result.warnings)):
        for msg in messages:
            where = f" at {msg.context}" if msg.context else ""
            print(f"{label} [{msg.code}]{where}: {msg.message}")
            if msg.fix:
                print(f"      fix: {msg.fix}")


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
    parser.add_argument(
        "--export-transfer",
        action="store_true",
        help="Write <project>_transfer.yaml for the VM: recipes written out in full, "
        "multipliers and batching left for the split (D49). --out chooses the file.",
    )
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

    if args.export_transfer:
        result = build_transfer(
            template_path=args.template,
            recipes_path=args.recipes,
            output_path=args.out,
            write=not args.validate,
            datadictionary_path=args.datadictionary,
        )
        print_messages(result)
        if not result.ok:
            print("FAILED: errors block the transfer YAML")
            return 1
        print(f"{'OK: transfer YAML ready at' if args.validate else 'Wrote'} {result.output_path}")
        uploads = result.analysis.get("transfer_uploads") or []
        if uploads:
            folder = Path(result.output_path).parent
            print(f"Carry these with it, at these paths relative to {folder}:")
            for upload in uploads:
                print(f"  {upload}")
        return 0

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
