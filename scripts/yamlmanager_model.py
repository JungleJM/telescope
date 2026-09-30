#!/usr/bin/env python3
"""YAML Manager's model: the draft template and every edit to it (D92).

Every UI is a view of this module: it holds no tkinter and no HTML. A view
reads the draft through it, calls it to change anything, and asks it what to
show: where a variable comes from, which tables fit a binding, a PK's
columns, each message's kind and the field it points to. It validates in
process, through makeYaml, so a view can check a draft as it is edited.

The draft is the template itself, a plain mapping, laid out in the Builder's
sections (D96): Project, PK Table, Supporting Tables, Multipliers, Splitters,
Fact Tables. The sections map onto the template's own keys; nothing here
invents a key makeYaml does not read.

    python3 scripts/yamlmanager_model.py --tdd    # its tests

Standard library and makeYaml only, so it runs wherever makeYaml does (D93).
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import makeYaml as my  # noqa: E402

INTAKE_SUFFIX = "_intake.yaml"
TRANSFER_SUFFIX = "_transfer.yaml"
# What a new template starts with, when YAMLs/template.yaml does not say (D86).
BUILT_IN_DEFAULTS: dict[str, dict[str, Any]] = {
    "cosmos_vars": {"project_db": "PROJECTD93A5E7", "cosmos_db": "Dual"},
    "run_vars": {"min_date_key": "19900101", "max_date_key": "20260601"},
    "test_options": {"smallset": False, "stop_at_for_pk_table": 10, "random_pk_sample": False},
}
# Retired (D62): dropped from a loaded draft when it is saved.
RETIRED_TEST_OPTIONS = ("stop_at_for_non_pk_tables", "print_md", "printout_md")
# Where each project setting lives when a template groups it (the flat key
# at the top level is read too, and written if that is where it already is).
SETTING_GROUPS = {
    "project_folder": "project_vars",
    "project_db": "cosmos_vars",
    "cosmos_db": "cosmos_vars",
    "min_date_key": "run_vars",
    "max_date_key": "run_vars",
    "smallset": "test_options",
    "stop_at_for_pk_table": "test_options",
    "random_pk_sample": "test_options",
}
# `cosmos_db` when neither database is chosen. Left blank it would mean Dual
# (D86), so it is written as a value makeYaml refuses.
NO_DATABASE = "none"
FILE_KINDS = ("parquet", "csv")
UPLOAD_KINDS = ("parquet", "csv", "dbtable")
AUTOMATIC_VARS = ("prefix", "PKTable")
# The row key a patient list nearly always has (D107).
USUAL_ROW_KEY = "PatientDurableKey"


class DraftError(ValueError):
    """An edit the model refuses, with what to do instead."""


# =============================================================================
# The workspace: where drafts, recipes and the dictionary are
# =============================================================================


@dataclass
class Workspace:
    """The folders the app works in, the same shape on the Mac and the VM.

    `home` is the working folder: the repository root on the Mac, the folder
    beside the extracted bundle on the VM (makeYaml.transfer_home). Intakes
    live in `home/YAMLs/temp/`, transfer YAMLs in `home` (D94, D95).
    """

    home: Path
    recipes_path: Path
    dictionary_path: Path
    defaults_path: Path
    # Running from an extracted bundle: the VM side (D108). There a file is
    # where it will be read, so none is pending and a missing one is an error.
    vm_side: bool = False
    _recipes_cache: tuple[float, dict[str, Any], str] | None = field(default=None, repr=False)

    @classmethod
    def default(cls) -> "Workspace":
        return cls(
            home=my.transfer_home(),
            recipes_path=my.default_recipes_path(),
            dictionary_path=my.default_datadictionary_path(),
            defaults_path=my.default_template_path(),
            vm_side=(my.project_root() / ".bundle-manifest.json").is_file(),
        )

    @property
    def temp_dir(self) -> Path:
        return self.home / "YAMLs" / "temp"

    @property
    def has_recipes(self) -> bool:
        return self.recipes_path.is_file()

    def recipes_doc(self) -> dict[str, Any]:
        """recipes.yaml, read again when it changes; empty where there is none."""
        return self._read_recipes()[1]

    def recipes_problem(self) -> str:
        """Why no recipes are listed, or ''."""
        return self._read_recipes()[2]

    def _read_recipes(self) -> tuple[float, dict[str, Any], str]:
        if not self.has_recipes:
            return (0.0, {}, f"No recipes file here ({self.recipes_path.name}): recipes stay on the "
                    "Mac (D49), and a transfer YAML carries its own written out.")
        mtime = self.recipes_path.stat().st_mtime
        if self._recipes_cache and self._recipes_cache[0] == mtime:
            return self._recipes_cache
        try:
            doc = my.load_yaml(self.recipes_path) or {}
            problem = "" if isinstance(doc, dict) else f"{self.recipes_path.name} is not a mapping."
        except Exception as exc:  # noqa: BLE001 - shown in the view
            doc, problem = {}, f"{self.recipes_path.name} could not be read: {exc}"
        self._recipes_cache = (mtime, doc if isinstance(doc, dict) else {}, problem)
        return self._recipes_cache

    def recipes(self) -> list[dict[str, Any]]:
        return [r for r in self.recipes_doc().get("recipes") or [] if isinstance(r, dict) and r.get("name")]

    def recipe(self, name: str) -> dict[str, Any] | None:
        return next((r for r in self.recipes() if r.get("name") == name), None)

    def pk_recipes(self) -> list[str]:
        return [str(r["name"]) for r in self.recipes() if is_pk(r)]

    def fact_recipes(self) -> list[str]:
        return [str(r["name"]) for r in self.recipes() if not is_pk(r)]

    def recipe_sets(self) -> list[dict[str, Any]]:
        """`recipe_sets` in recipes.yaml (D135): several recipes added as one table group."""
        sets = self.recipes_doc().get("recipe_sets") or []
        return [s for s in sets if isinstance(s, dict) and s.get("name")] if isinstance(sets, list) else []

    def recipe_set(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.recipe_sets() if str(s.get("name")) == name), None)

    def batching_recipes(self) -> list[dict[str, Any]]:
        return [r for r in self.recipes_doc().get("batching_recipes") or [] if isinstance(r, dict) and r.get("name")]

    def defaults(self) -> dict[str, dict[str, Any]]:
        """A new template's settings: template.yaml's, so editing that file edits
        them; the built-in values fill anything it leaves out (D86)."""
        try:
            doc = my.load_yaml(self.defaults_path) if self.defaults_path.is_file() else {}
        except Exception:  # noqa: BLE001 - a broken file falls back
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        out: dict[str, dict[str, Any]] = {}
        for group, fallback in BUILT_IN_DEFAULTS.items():
            own = doc.get(group) if isinstance(doc.get(group), dict) else {}
            out[group] = {k: own[k] if own.get(k) is not None else v for k, v in fallback.items()}
        return out

    def intakes(self) -> list[Path]:
        return sorted(self.temp_dir.glob(f"*{INTAKE_SUFFIX}")) if self.temp_dir.is_dir() else []

    def transfers(self) -> list[Path]:
        return sorted(self.home.glob(f"*{TRANSFER_SUFFIX}")) if self.home.is_dir() else []

    def dictionary(self) -> dict[str, Any]:
        """The data dictionary's tables, or {} where it cannot be read."""
        entries = my.load_datadictionary(self.dictionary_path, my.CompileResult())
        return entries if isinstance(entries, dict) else {}


def sql_literal(value: str) -> str:
    """A value typed in, as SQL: a number or a `{{Variable}}` as it is, text quoted."""
    text = value.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text) or re.fullmatch(r"\{\{\s*\w+\s*\}\}", text):
        return text
    return "'" + text.replace("'", "''") + "'"


# How a where line by column compares (D119); `in_table` reads a supporting table.
WHERE_OPERATORS = ("=", "<>", "<", "<=", ">", ">=", "IN", "LIKE", "BETWEEN")
WHERE_MODES = WHERE_OPERATORS + ("in_table",)


def where_line(source: str, mode: str, value: str = "", table: str = "", column: str = "",
               higher: str = "") -> str:
    """A where line by column (D105, D119). `mode` is an operator: `IN` takes
    a comma list, `BETWEEN` a value and `higher`, the rest one value. Or
    `in_table`: `IN` the column of a supporting table, so a code listed twice
    cannot duplicate rows."""
    source = source.strip()
    if not source:
        raise DraftError("Choose the column the condition is on.")
    if mode == "in_table":
        if not table or not column:
            raise DraftError("Choose the supporting table and its column.")
        return f"{source} IN (SELECT [{column}] FROM {{{{prefix}}}}_{table})"
    if mode not in WHERE_OPERATORS:
        raise DraftError(f"A where line compares with {' '.join(WHERE_OPERATORS)}, or is in a supporting table, not {mode}.")
    values = split_list(value) if mode == "IN" else ([value.strip()] if value.strip() else [])
    if not values:
        raise DraftError("Type the lower value." if mode == "BETWEEN" else "Type the value the column must have.")
    if mode == "IN":
        return f"{source} IN ({', '.join(sql_literal(v) for v in values)})"
    if mode == "BETWEEN":
        if not higher.strip():
            raise DraftError("Type the higher value.")
        return f"{source} BETWEEN {sql_literal(values[0])} AND {sql_literal(higher)}"
    if mode != "LIKE" and "%" in values[0]:
        raise DraftError(f"`{values[0]}` has a %, which only LIKE reads as a pattern: choose LIKE, or remove the %.")
    if mode == "=" and "," in values[0]:
        raise DraftError(f"`{values[0]}` has commas: choose IN for a list of values.")
    return f"{source} {mode} {sql_literal(values[0])}"


def join_line(join_type: str, source: str, operator: str, table: str, alias: str, column: str) -> str:
    return (f"{join_type} JOIN {{{{prefix}}}}_{table} AS {alias} "
            f"ON {source} {operator} {alias}.{column}")


def line_aliases(lines: list[tuple[str, str]]) -> dict[str, str]:
    """Each alias the (`from`/`join`, line) pairs declare, with its table."""
    aliases: dict[str, str] = {}
    for key, line in lines:
        for alias, table, _ in my.declared_sql_tables(line, key == "from"):
            aliases.setdefault(alias, table)
    return aliases


def line_problem(line: str, aliases: dict[str, str]) -> str | None:
    """Why a written line would fail at Execute, or None (D118): an alias the
    table does not define. The same check Validate makes, as the line is added."""
    own = {alias for alias, _, _ in my.declared_sql_tables(line)}
    for alias, column in my.referenced_sql_names(line):
        if alias in my.SQL_SCHEMAS or alias in aliases or alias in own:
            continue
        have = ", ".join(sorted(set(aliases) | own)) or "none"
        return f"`{alias}.{column}` names `{alias}`, which this table does not define; it has {have}."
    return None


_OUTSIDE_STRINGS = re.compile(r"(N?'(?:[^']|'')*')")


def rename_alias(line: str, old: str, new: str) -> str:
    """`old.Column` as `new.Column` throughout a line, strings left alone."""
    pattern = re.compile(rf"(?<![\w.#@\]]){re.escape(old)}\.")
    parts = _OUTSIDE_STRINGS.split(line)
    return "".join(part if i % 2 else pattern.sub(f"{new}.", part) for i, part in enumerate(parts))


def standard_where_lines(entry: Any, alias: str) -> list[str]:
    """A dictionary table's `standard_where` (D120), each line's leading column
    written under `alias`: `_IsDeleted = 0` is `vf._IsDeleted = 0`."""
    lines = entry.get("standard_where") if isinstance(entry, dict) else None
    out = []
    for line in [lines] if isinstance(lines, str) else lines or []:
        match = re.match(r"\s*([A-Za-z_]\w*)(.*)$", str(line))
        if match:
            out.append(f"{alias}.{match.group(1)}{match.group(2)}".strip())
    return out


def read_pasted_csv(text: str) -> tuple[list[str], list[list[str]], str]:
    """Pasted CSV as (header, rows, what separated it) (D127). Copied from a
    spreadsheet it is tab-separated, and read so. A blank or repeated header,
    or a row of another width, is refused, naming the line."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        raise DraftError("Nothing pasted yet.")
    tabs = "\t" in lines[0]
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter="\t" if tabs else ","))
    header = [cell.strip() for cell in rows[0]]
    for position, cell in enumerate(header, start=1):
        if not cell:
            raise DraftError(f"The first line is the header, and its column {position} is blank.")
    repeated = sorted({cell for cell in header if header.count(cell) > 1})
    if repeated:
        raise DraftError(f"The header names {', '.join(repeated)} more than once.")
    if len(rows) < 2:
        raise DraftError("Only a header: paste its rows too.")
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise DraftError(f"Line {number} has {len(row)} values; the header has {len(header)}.")
    return header, rows[1:], "tabs" if tabs else "commas"


def pasted_summary(text: str) -> tuple[bool, str]:
    """What a paste reads as, for showing as it is typed: (ok, words)."""
    try:
        header, rows, separator = read_pasted_csv(text)
    except DraftError as exc:
        return False, str(exc)
    first = ", ".join(rows[0])
    return True, (f"{len(header)} columns ({', '.join(header)}), {len(rows)} row{'' if len(rows) == 1 else 's'}, separated by "
                  f"{separator}. First row: {first}")


def is_pk(cohort: dict[str, Any] | None) -> bool:
    return str((cohort or {}).get("type", "")).lower() == "pk"


def intake_name(project: str) -> str:
    """`IBD Ancestry` is `IBD_Ancestry_intake.yaml`; no name yet is `_intake.yaml` (D95)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(project or "")).strip("_") + INTAKE_SUFFIX


def split_list(text: Any) -> list[str]:
    """`a, b` or a list, as a list of trimmed, non-empty strings."""
    items = text if isinstance(text, list) else str(text or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def value_text(value: Any) -> str:
    return ", ".join(str(v) for v in value) if isinstance(value, list) else ("" if value is None else str(value))


def level_vars_text(level_vars: dict[str, Any] | None) -> str:
    """`{ICD_Value: [K51.%, K52.%]}` as `ICD_Value: K51.%, K52.%`; `;` between variables."""
    return "; ".join(f"{k}: {value_text(v)}" for k, v in (level_vars or {}).items())


def parse_level_vars(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in str(text or "").split(";"):
        key, _, rest = part.partition(":")
        if key.strip():
            out[key.strip()] = split_list(rest)
    return out


# =============================================================================
# What the model tells a view
# =============================================================================


@dataclass
class PkInfo:
    """The template's one PK (D96), wherever it is written."""

    kind: str               # recipe, dictionary, parquet, csv or dbtable
    name: str               # the table it makes
    where: str              # "cohorts" or "upload_cohorts"
    index: int              # its place in that list
    recipe: str = ""
    location: str = ""      # file_loc, or a dbtable's source table
    key_columns: list[str] = field(default_factory=list)
    pending_transfer: bool = False


@dataclass
class ColumnRow:
    """One column of a supporting table: the file's name for it, and what it lands as."""

    source: str
    name: str
    type: str = ""
    dropped: bool = False


@dataclass
class VarRow:
    """A variable a table's SQL needs, and where its value comes from when unset."""

    name: str
    value: str              # this table's own value, as text; '' if unset
    source: str             # label for an unset value: "from the PK (K50.%)", ...
    required: bool          # unset and nothing supplies it


@dataclass
class Candidate:
    table: str
    kind: str               # upload or cohort
    matched: list[str]
    missing: list[str]
    known: bool             # False when the table's columns cannot be seen

    @property
    def fits(self) -> bool:
        return self.known and not self.missing


@dataclass
class Binding:
    """A table input (`{{prefix}}_{{Var}} AS alias`) and the tables that could fill it."""

    var: str
    alias: str
    columns: list[str]
    bound: str
    candidates: list[Candidate]


@dataclass
class Splitter:
    """One entry of the Splitters section (D96)."""

    kind: str               # "separate" (split_after_build) or "pieces" (batching)
    where: str              # "multipliers" or "batching"
    index: int
    name: str
    column: str = ""        # pieces by column
    values: list[str] | None = None   # None: every value (D82)
    rows: int | None = None           # pieces by chunk
    separate_parquets: bool = False
    levels: list[dict[str, Any]] = field(default_factory=list)  # separate
    problem: str = ""


@dataclass
class FieldRef:
    """Where a message points: a section of the Builder, and an entry in it."""

    section: str            # project, pk, supporting, multipliers, splitters, fact
    index: int | None = None
    detail: str = ""


@dataclass
class Message:
    kind: str               # error, warning or pending
    code: str
    text: str
    fix: str = ""
    context: str = ""
    field: FieldRef | None = None


@dataclass
class Validation:
    ok: bool
    messages: list[Message]
    steps: list[tuple[str, str]]   # (label, pass | fail | pending)
    analysis: dict[str, Any] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[Message]:
        return [m for m in self.messages if m.kind == kind]


@dataclass
class SaveResult:
    ok: bool
    message: str
    path: Path | None = None


# =============================================================================
# The draft
# =============================================================================


class Draft:
    """A template being built or adjusted. Every edit goes through a method,
    which marks it unsaved and forgets the last validation."""

    def __init__(self, workspace: Workspace, doc: dict[str, Any], path: Path | None = None):
        self.ws = workspace
        self.doc = doc
        self.path = path
        self.dirty = False
        self._validation: Validation | None = None
        self._shape()
        self._fill_project_defaults()

    # ------------------------------------------------------------ open, new

    @classmethod
    def new(cls, workspace: Workspace) -> "Draft":
        defaults = workspace.defaults()
        doc = {
            "cosmos_vars": copy.deepcopy(defaults["cosmos_vars"]),
            "run_vars": copy.deepcopy(defaults["run_vars"]),
            "test_options": copy.deepcopy(defaults["test_options"]),
            "project_vars": {"project_folder": ""},
            "vars": {},
            "upload_cohorts": [],
            "multipliers": [],
            "batching": [],
            "cohorts": [],
        }
        return cls(workspace, doc)

    @classmethod
    def open(cls, workspace: Workspace, path: str | Path) -> "Draft":
        path = Path(path)
        try:
            doc = my.load_yaml(path)
        except Exception as exc:  # noqa: BLE001 - said to the view
            raise DraftError(f"{path.name} could not be read: {exc}") from exc
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            raise DraftError(f"{path.name} is not a template: its top level is not a mapping.")
        return cls(workspace, doc, path)

    def _shape(self) -> None:
        for key in ("upload_cohorts", "multipliers", "batching", "cohorts"):
            if not isinstance(self.doc.get(key), list):
                self.doc[key] = []
        if not isinstance(self.doc.get("vars"), dict):
            self.doc["vars"] = {}

    def _fill_project_defaults(self) -> None:
        """The project database and dates, where a template has none (D83)."""
        defaults = self.ws.defaults()
        for key, group in (("project_db", "cosmos_vars"), ("min_date_key", "run_vars"), ("max_date_key", "run_vars")):
            if self._get(key) in (None, ""):
                self._put(key, defaults[group][key])

    def _changed(self) -> None:
        self.dirty = True
        self._validation = None

    # Settings a template may write grouped (`cosmos_vars.project_db`) or flat.
    def _get(self, key: str) -> Any:
        group = self.doc.get(SETTING_GROUPS[key])
        if isinstance(group, dict) and group.get(key) not in (None, ""):
            return group[key]
        if key in ("min_date_key", "max_date_key") and self.doc["vars"].get(key) not in (None, ""):
            return self.doc["vars"][key]
        return self.doc.get(key)

    def _put(self, key: str, value: Any) -> None:
        if key in self.doc and not isinstance(self.doc.get(SETTING_GROUPS[key]), dict):
            self.doc[key] = value
            return
        if key in ("min_date_key", "max_date_key") and key in self.doc["vars"]:
            self.doc["vars"][key] = value
            return
        self.doc.setdefault(SETTING_GROUPS[key], {})
        if not isinstance(self.doc[SETTING_GROUPS[key]], dict):
            self.doc[SETTING_GROUPS[key]] = {}
        self.doc[SETTING_GROUPS[key]][key] = value
        self.doc.pop(key, None)  # one copy, in its group

    # ------------------------------------------------------------- project

    @property
    def project_name(self) -> str:
        return str(self._get("project_folder") or "")

    @project_name.setter
    def project_name(self, value: str) -> None:
        self._put("project_folder", str(value).strip())
        self._changed()

    def pull_from(self) -> tuple[bool, bool]:
        """(Cosmos, Cosmos_SneakPeek): both is Dual; a missing setting is Dual (D86)."""
        text = str(self._get("cosmos_db") or "dual").lower()
        if text in ("dual", "both"):
            return True, True
        if text == "cosmos":
            return True, False
        if text in ("cosmos_sneakpeek", "sneakpeek", "sp"):
            return False, True
        return False, False

    def set_pull_from(self, cosmos: bool, sneakpeek: bool) -> None:
        value = {(True, True): "Dual", (True, False): "COSMOS", (False, True): "COSMOS_SneakPeek"}
        self._put("cosmos_db", value.get((bool(cosmos), bool(sneakpeek)), NO_DATABASE))
        self._changed()

    @property
    def project_db(self) -> str:
        return str(self._get("project_db") or "")

    @project_db.setter
    def project_db(self, value: str) -> None:
        self._put("project_db", str(value).strip())
        self._changed()

    def dates(self) -> tuple[str, str]:
        return str(self._get("min_date_key") or ""), str(self._get("max_date_key") or "")

    def set_dates(self, min_date: str | None = None, max_date: str | None = None) -> None:
        if min_date is not None:
            self._put("min_date_key", str(min_date).strip())
        if max_date is not None:
            self._put("max_date_key", str(max_date).strip())
        self._changed()

    @property
    def collect_all(self) -> bool:
        """"Collect all patients matching criteria": `smallset: false` (D96)."""
        return not bool(self._get("smallset"))

    @collect_all.setter
    def collect_all(self, value: bool) -> None:
        self._put("smallset", not bool(value))
        self._changed()

    @property
    def sample_size(self) -> int:
        try:
            return int(self._get("stop_at_for_pk_table") or 0)
        except (TypeError, ValueError):
            return 0

    @sample_size.setter
    def sample_size(self, value: Any) -> None:
        try:
            number = int(str(value).strip() or 0)
        except ValueError as exc:
            raise DraftError(f"Sample size `{value}` is not a whole number.") from exc
        self._put("stop_at_for_pk_table", number)
        self._changed()

    @property
    def random_sample(self) -> bool:
        return bool(self._get("random_pk_sample"))

    @random_sample.setter
    def random_sample(self, value: bool) -> None:
        self._put("random_pk_sample", bool(value))
        self._changed()

    # ------------------------------------------------------------------ PK

    def cohort_is_pk(self, cohort: dict[str, Any]) -> bool:
        if cohort.get("type"):
            return is_pk(cohort)
        return bool(cohort.get("recipe")) and is_pk(self.ws.recipe(str(cohort["recipe"])))

    def pk(self) -> PkInfo | None:
        for i, upload in enumerate(self.doc["upload_cohorts"]):
            if isinstance(upload, dict) and is_pk(upload):
                kind = str(upload.get("file_type") or "parquet").lower()
                location = upload.get("source_table") if kind == "dbtable" else upload.get("file_loc")
                return PkInfo(
                    kind=kind, name=str(upload.get("dest_table") or upload.get("name") or ""),
                    where="upload_cohorts", index=i, location=str(location or ""),
                    key_columns=split_list(upload.get("key_columns") or []),
                    pending_transfer=upload.get("pending_transfer") is True,
                )
        for i, cohort in enumerate(self.doc["cohorts"]):
            if isinstance(cohort, dict) and self.cohort_is_pk(cohort):
                return PkInfo(
                    kind="recipe" if cohort.get("recipe") else "dictionary",
                    name=str(cohort.get("dest_table") or cohort.get("name") or cohort.get("recipe") or ""),
                    where="cohorts", index=i, recipe=str(cohort.get("recipe") or ""),
                )
        return None

    def _drop_pk(self) -> None:
        pk = self.pk()
        if pk:
            del self.doc[pk.where][pk.index]

    def set_pk_recipe(self, recipe: str, name: str = "") -> None:
        """The PK from a prefabricated recipe; it replaces any PK there was."""
        found = self.ws.recipe(recipe)
        if found is None:
            raise DraftError(f"No recipe named {recipe}. {self.ws.recipes_problem()}".strip())
        if not is_pk(found):
            raise DraftError(f"Recipe {recipe} is not a PK recipe; add it under Fact Tables.")
        self._drop_pk()
        self.doc["cohorts"].insert(0, {"recipe": recipe, "name": name.strip() or recipe})
        self._changed()

    def set_pk_table(self, cohort: dict[str, Any]) -> None:
        """The PK built from the dictionary (the table builder makes `cohort`)."""
        cohort = copy.deepcopy(cohort)
        cohort["type"] = "PK"
        self._drop_pk()
        self.doc["cohorts"].insert(0, cohort)
        self._changed()

    def set_pk_upload(self, kind: str, name: str, location: str = "", key_columns: Any = (),
                      pending_transfer: bool = False) -> None:
        """The PK from a parquet, a CSV or a Projects table (`dbtable`)."""
        if kind not in UPLOAD_KINDS:
            raise DraftError(f"A PK file is parquet, csv or dbtable, not {kind}.")
        name = name.strip()
        if not name:
            raise DraftError("The PK table needs a name.")
        upload: dict[str, Any] = {"name": name, "dest_table": name, "type": "pk", "file_type": kind}
        self._place_location(upload, kind, location)
        upload["key_columns"] = split_list(key_columns)
        if pending_transfer and kind in FILE_KINDS:
            self._set_pending(upload, True)
        upload["push_this_cycle"] = True
        self._drop_pk()
        self.doc["upload_cohorts"].insert(0, upload)
        self._changed()
        self._prefill_row_key()

    def _prefill_row_key(self) -> None:
        """An uploaded PK with no row key takes PatientDurableKey when its
        file, or the columns typed in for it, has that column (D107)."""
        pk = self.pk()
        if pk is None or pk.where != "upload_cohorts" or pk.key_columns:
            return
        if USUAL_ROW_KEY in (self.pk_columns() or []):
            self.doc["upload_cohorts"][pk.index]["key_columns"] = [USUAL_ROW_KEY]
            self._changed()

    def update_pk(self, **fields: Any) -> None:
        """Change the PK in place: name, location, key_columns, pending_transfer."""
        pk = self.pk()
        if pk is None:
            raise DraftError("There is no PK Table yet; choose one first.")
        entry = self.doc[pk.where][pk.index]
        if "name" in fields:
            name = str(fields["name"]).strip()
            if pk.kind == "recipe":
                entry["name"] = name
            else:
                entry["name"] = name
                entry["dest_table"] = name
        if pk.where == "upload_cohorts":
            if "location" in fields:
                self._place_location(entry, pk.kind, fields["location"])
            if "key_columns" in fields:
                entry["key_columns"] = split_list(fields["key_columns"])
            if "pending_transfer" in fields:
                self._set_pending(entry, bool(fields["pending_transfer"]))
        self._changed()
        if "location" in fields:
            self._prefill_row_key()

    def clear_pk(self) -> None:
        self._drop_pk()
        self._changed()

    def pk_columns(self) -> list[str] | None:
        """The PK's columns as it will land, for the Splitters; None if unknown."""
        pk = self.pk()
        if pk is None:
            return None
        if pk.where == "cohorts":
            merged = self._merged_cohort(pk.index)
            return my.output_columns(merged) if merged else None
        schemas = self.validation().analysis.get("table_schemas") or {}
        columns = schemas.get(pk.name)
        if columns is None:
            columns = my.listed_column_names(self.doc["upload_cohorts"][pk.index])
        return list(columns) if columns is not None else None

    # ---------------------------------------------------- supporting tables

    @staticmethod
    def _place_location(entry: dict[str, Any], kind: str, location: Any) -> None:
        location = str(location or "").strip()
        entry.pop("file_loc", None)
        entry.pop("source_table", None)
        if kind == "dbtable":
            entry.pop("pending_transfer", None)
            if location and location != entry.get("dest_table"):
                entry["source_table"] = location
        elif location:
            entry["file_loc"] = location

    def _set_pending(self, entry: dict[str, Any], pending: bool) -> None:
        if pending and self.ws.vm_side:
            raise DraftError("Pending transfer is for the Mac: here on the VM the file must be "
                             "where the transfer YAML reads it (D108).")
        if pending:
            if str(entry.get("file_type", "")).lower() not in FILE_KINDS:
                raise DraftError("Only a parquet or CSV file can be pending transfer (D97).")
            entry["pending_transfer"] = True
        else:
            entry.pop("pending_transfer", None)

    def supporting(self) -> list[tuple[int, dict[str, Any]]]:
        """The uploads that are not the PK, with their place in `upload_cohorts`."""
        return [(i, u) for i, u in enumerate(self.doc["upload_cohorts"]) if isinstance(u, dict) and not is_pk(u)]

    def add_supporting(self, kind: str, name: str, location: str = "", pending_transfer: bool = False) -> int:
        if kind not in UPLOAD_KINDS:
            raise DraftError(f"A supporting table is parquet, csv or dbtable, not {kind}.")
        name = name.strip() or f"Upload{len(self.doc['upload_cohorts']) + 1}"
        upload: dict[str, Any] = {"name": name, "dest_table": name, "file_type": kind}
        self._place_location(upload, kind, location)
        if pending_transfer:
            self._set_pending(upload, True)
        upload["push_this_cycle"] = True
        self.doc["upload_cohorts"].append(upload)
        self._changed()
        index = len(self.doc["upload_cohorts"]) - 1
        self._unquote_columns(index)
        return index

    def add_pasted_csv(self, name: str, text: str) -> int:
        """Pasted rows as a named CSV in YAMLs/temp/csv/, kept to use again, and
        a supporting table that reads it (D127). A file of that name is not
        overwritten."""
        name = name.strip()
        if not name:
            raise DraftError("Name the pasted table: it becomes csv/<name>.csv.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise DraftError(f"`{name}` cannot name a table: letters, digits and _ only, not starting with a digit.")
        header, rows, _ = read_pasted_csv(text)
        path = self.ws.temp_dir / "csv" / f"{name}.csv"
        if path.exists():
            raise DraftError(f"{path.name} is already in {path.parent}: choose another name, or Browse to use that file.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows([header, *rows])
        return self.add_supporting("csv", name, self.location_for(path))

    def _supporting_entry(self, index: int) -> dict[str, Any]:
        uploads = self.doc["upload_cohorts"]
        if not (0 <= index < len(uploads)) or not isinstance(uploads[index], dict) or is_pk(uploads[index]):
            raise DraftError(f"There is no supporting table at {index}.")
        return uploads[index]

    def update_supporting(self, index: int, **fields: Any) -> None:
        """name, dest_table, file_type, location, pending_transfer, push_this_cycle."""
        entry = self._supporting_entry(index)
        if "name" in fields:
            entry["name"] = str(fields["name"]).strip()
        if "dest_table" in fields:
            entry["dest_table"] = str(fields["dest_table"]).strip() or entry.get("name")
        if "file_type" in fields:
            if fields["file_type"] not in UPLOAD_KINDS:
                raise DraftError(f"A supporting table is parquet, csv or dbtable, not {fields['file_type']}.")
            location = entry.get("source_table") or entry.get("file_loc") or ""
            entry["file_type"] = fields["file_type"]
            self._place_location(entry, fields["file_type"], location)
        if "location" in fields:
            self._place_location(entry, str(entry.get("file_type", "")).lower(), fields["location"])
        if "pending_transfer" in fields:
            self._set_pending(entry, bool(fields["pending_transfer"]))
        if "push_this_cycle" in fields:
            entry["push_this_cycle"] = bool(fields["push_this_cycle"])
        self._changed()
        if "location" in fields or "file_type" in fields:
            self._unquote_columns(index)

    def _unquote_columns(self, index: int) -> None:
        """A file column whose name carries quotes (`'DiagnosisCode'`) lands
        without them, written as a rename the user can see and change (D159).
        Done when a file is chosen, so a rename back to the quoted name stays."""
        if not self.columns_readable(index):
            return
        upload = self._upload(index)
        changed = {my.column_source(c) for c in upload.get("columns") or [] if isinstance(c, dict)
                   and (c.get("from") or c.get("drop"))}
        quoted = [row.source for row in self.column_rows(index)
                  if any(q in row.source for q in ("'", '"')) and row.source not in changed]
        for source in quoted:
            clean = source.strip("'\"").strip()
            if clean and clean != source:
                self.rename_column(index, source, clean)

    def remove_supporting(self, index: int) -> None:
        self._supporting_entry(index)
        del self.doc["upload_cohorts"][index]
        self._changed()

    def _upload(self, index: int) -> dict[str, Any]:
        uploads = self.doc["upload_cohorts"]
        if not (0 <= index < len(uploads)) or not isinstance(uploads[index], dict):
            raise DraftError(f"There is no upload at {index}.")
        return uploads[index]

    def column_rows(self, index: int) -> list[ColumnRow]:
        """An upload's columns: the file's, with what each lands as (D98). For a
        table nothing here can read, the columns typed in for it (D97)."""
        upload = self._upload(index)
        entries = {my.column_source(c): c for c in upload.get("columns") or [] if isinstance(c, dict) and (c.get("name") or c.get("from"))}
        dest = str(upload.get("dest_table") or upload.get("name"))
        file_columns = (self.validation().analysis.get("upload_file_columns") or {}).get(dest)
        sources = file_columns if file_columns is not None else list(entries)
        rows = []
        for source in sources:
            entry = entries.get(source, {})
            renamed = entry.get("from") and entry.get("drop") is not True
            rows.append(ColumnRow(
                source=source, name=str(entry["name"]) if renamed else source,
                type=str(entry.get("type") or ""), dropped=entry.get("drop") is True,
            ))
        return rows

    def columns_readable(self, index: int) -> bool:
        upload = self._upload(index)
        dest = str(upload.get("dest_table") or upload.get("name"))
        return dest in (self.validation().analysis.get("upload_file_columns") or {})

    def _column_entry(self, upload: dict[str, Any], source: str) -> dict[str, Any]:
        columns = upload.setdefault("columns", [])
        for entry in columns:
            if isinstance(entry, dict) and my.column_source(entry) == source:
                return entry
        entry = {"name": source}
        columns.append(entry)
        return entry

    def _tidy_columns(self, index: int) -> None:
        """Drop entries that say nothing, where the file's own columns are known;
        where they are not, a bare name is the typed-in schema (D97) and stays."""
        upload = self._upload(index)
        readable = self.columns_readable(index)
        kept = []
        for entry in upload.get("columns") or []:
            if isinstance(entry, dict) and entry.get("from") == entry.get("name"):
                entry.pop("from", None)
            says_something = not isinstance(entry, dict) or any(entry.get(k) for k in ("type", "from", "drop"))
            if says_something or not readable:
                kept.append(entry)
        if kept:
            upload["columns"] = kept
        else:
            upload.pop("columns", None)

    def rename_column(self, index: int, source: str, name: str) -> None:
        """The file's column `source` lands as `name` (D98)."""
        upload = self._upload(index)
        name = name.strip() or source
        entry = self._column_entry(upload, source)
        entry.pop("drop", None)
        if name == source:
            entry.pop("from", None)
            entry["name"] = source
        else:
            entry["from"] = source
            entry["name"] = name
        self._tidy_columns(index)
        self._changed()

    def drop_column(self, index: int, source: str, dropped: bool = True) -> None:
        upload = self._upload(index)
        entry = self._column_entry(upload, source)
        if dropped:
            entry.clear()
            entry.update({"name": source, "drop": True})
        else:
            entry.pop("drop", None)
        self._tidy_columns(index)
        self._changed()

    def set_column_type(self, index: int, source: str, sql_type: str) -> None:
        upload = self._upload(index)
        entry = self._column_entry(upload, source)
        if sql_type.strip():
            entry["type"] = sql_type.strip().upper()
        else:
            entry.pop("type", None)
        self._tidy_columns(index)
        self._changed()

    def set_listed_columns(self, index: int, names: Any) -> None:
        """Type in the columns of a table nothing here can read (D97). Kept
        entries keep their type, rename and drop."""
        upload = self._upload(index)
        old = {my.column_source(c): c for c in upload.get("columns") or [] if isinstance(c, dict)}
        listed = [old.get(name, {"name": name}) for name in split_list(names)]
        if listed:
            upload["columns"] = listed
        else:
            upload.pop("columns", None)
        self._changed()
        self._prefill_row_key()

    # ---------------------------------------------------------- multipliers

    def multipliers(self) -> list[tuple[int, dict[str, Any]]]:
        """`during_build` multipliers: each level its own set of tables (D96)."""
        return [(i, m) for i, m in enumerate(self.doc["multipliers"])
                if isinstance(m, dict) and m.get("stage") != "split_after_build"]

    def _multiplier(self, index: int, stage: str | None = None) -> dict[str, Any]:
        mults = self.doc["multipliers"]
        if not (0 <= index < len(mults)) or not isinstance(mults[index], dict):
            raise DraftError(f"There is no multiplier at {index}.")
        if stage == "split_after_build" and mults[index].get("stage") != stage:
            raise DraftError(f"Multiplier {mults[index].get('name')} is not a Separate PK per level splitter.")
        if stage == "during_build" and mults[index].get("stage") == "split_after_build":
            raise DraftError(f"{mults[index].get('name')} is a Separate PK per level splitter, under Splitters.")
        return mults[index]

    def add_multiplier(self, name: str = "") -> int:
        name = name.strip() or f"Multiplier{len(self.doc['multipliers']) + 1}"
        self.doc["multipliers"].append({"name": name, "stage": "during_build", "levels": []})
        self._changed()
        return len(self.doc["multipliers"]) - 1

    def rename_multiplier(self, index: int, name: str) -> None:
        self._multiplier(index)["name"] = name.strip()
        self._changed()

    def remove_multiplier(self, index: int) -> None:
        self._multiplier(index)
        del self.doc["multipliers"][index]
        self._changed()

    def add_level(self, index: int, strat: str = "", vars_text: str = "") -> int:
        mult = self._multiplier(index, "during_build")
        mult.setdefault("levels", []).append({"strat": strat.strip(), "vars": parse_level_vars(vars_text)})
        self._changed()
        return len(mult["levels"]) - 1

    def set_level(self, index: int, level: int, strat: str | None = None, vars_text: str | None = None) -> None:
        entry = self._level(index, level)
        if strat is not None:
            entry["strat"] = strat.strip()
        if vars_text is not None:
            entry["vars"] = parse_level_vars(vars_text)
        self._changed()

    def _level(self, index: int, level: int) -> dict[str, Any]:
        levels = self._multiplier(index).setdefault("levels", [])
        if not (0 <= level < len(levels)) or not isinstance(levels[level], dict):
            raise DraftError(f"There is no level {level + 1} on that multiplier.")
        return levels[level]

    def remove_level(self, index: int, level: int) -> None:
        self._level(index, level)
        del self._multiplier(index)["levels"][level]
        self._changed()

    # ------------------------------------------------------------ splitters

    def splitters(self) -> list[Splitter]:
        """Both kinds of split, each saying which it is (D96)."""
        out: list[Splitter] = []
        for i, mult in enumerate(self.doc["multipliers"]):
            if isinstance(mult, dict) and mult.get("stage") == "split_after_build":
                out.append(Splitter(kind="separate", where="multipliers", index=i,
                                    name=str(mult.get("name") or ""),
                                    levels=[lv for lv in mult.get("levels") or [] if isinstance(lv, dict)]))
        result = my.CompileResult()
        normalized = my.normalize_batching(self.doc["batching"], self.ws.recipes_doc(), result)
        by_source = {item["_source"]: item for item in normalized}
        for i, raw in enumerate(self.doc["batching"]):
            item = by_source.get(f"batching[{i}]")
            if item is None:
                problem = next((m.message for m in result.errors if m.context.startswith(f"batching[{i}]")), "")
                out.append(Splitter(kind="pieces", where="batching", index=i, name=str(raw), problem=problem))
                continue
            chunk = str(item.get("kind")) == "row_chunk"
            values = item.get("values")
            out.append(Splitter(
                kind="pieces", where="batching", index=i, name=str(item.get("name") or ""),
                column="" if chunk else str(item.get("column") or ""),
                values=None if chunk or values in (None, "all") else split_list(values),
                rows=int(item["rows_per_batch"]) if chunk and str(item.get("rows_per_batch")).isdigit() else None,
                separate_parquets=item.get("separate_parquets") is True,
            ))
        return out

    def _needs_pk_columns(self, column: str) -> None:
        if self.pk() is None:
            raise DraftError("Choose a PK Table first: a split by column needs the PK's columns.")
        known = self.pk_columns()
        if known is not None and column not in known:
            raise DraftError(f"The PK has no column {column}. Its columns: {', '.join(known) or 'none'}.")

    def add_separate(self, name: str = "") -> int:
        """A splitter making separate tables: a `split_after_build` multiplier (D59)."""
        if self.pk() is None:
            raise DraftError("Choose a PK Table first: separate tables split the PK by one of its columns.")
        name = name.strip() or f"Split{len(self.doc['multipliers']) + 1}"
        self.doc["multipliers"].append({"name": name, "stage": "split_after_build", "applies_to": "PKTable", "levels": []})
        self._changed()
        return len(self.doc["multipliers"]) - 1

    def add_separate_level(self, index: int, strat: str, column: str, values: Any,
                           role: str = "", row_mult: Any = None) -> int:
        mult = self._multiplier(index, "split_after_build")
        self._needs_pk_columns(column)
        level: dict[str, Any] = {"strat": strat.strip(), "column": column, "values": split_list(values)}
        self._level_role(level, role, row_mult)
        mult.setdefault("levels", []).append(level)
        self._changed()
        return len(mult["levels"]) - 1

    def set_separate_level(self, index: int, level: int, **fields: Any) -> None:
        """strat, column, values, role, row_mult."""
        self._multiplier(index, "split_after_build")
        entry = self._level(index, level)
        if "strat" in fields:
            entry["strat"] = str(fields["strat"]).strip()
        if "column" in fields:
            self._needs_pk_columns(fields["column"])
            entry["column"] = fields["column"]
        if "values" in fields:
            entry["values"] = split_list(fields["values"])
        if "role" in fields or "row_mult" in fields:
            self._level_role(entry, fields.get("role", entry.get("role", "")),
                             fields.get("row_mult", entry.get("row_mult")))
        self._changed()

    @staticmethod
    def _level_role(level: dict[str, Any], role: str, row_mult: Any) -> None:
        level.pop("role", None)
        level.pop("row_mult", None)
        if role:
            if role != "control":
                raise DraftError(f"A level's role is control or nothing, not {role}.")
            level["role"] = "control"
        if row_mult not in (None, ""):
            try:
                level["row_mult"] = int(row_mult)
            except (TypeError, ValueError) as exc:
                raise DraftError(f"Row mult `{row_mult}` is not a whole number.") from exc

    def add_pieces_by_column(self, column: str, values: Any = None, separate_parquets: bool = False,
                             name: str = "") -> int:
        """A splitter making pieces of one table: batching by a PK column.
        No values means every value the PK has (D82)."""
        self._needs_pk_columns(column)
        self.doc["batching"].append(self._pieces_definition(name.strip() or column, column, values, separate_parquets))
        self._changed()
        return len(self.doc["batching"]) - 1

    def add_chunk(self, rows: Any) -> int:
        self.doc["batching"].append({"chunk": self._rows(rows)})
        self._changed()
        return len(self.doc["batching"]) - 1

    @staticmethod
    def _rows(rows: Any) -> int:
        try:
            number = int(str(rows).strip())
        except ValueError as exc:
            raise DraftError(f"Rows per piece `{rows}` is not a whole number.") from exc
        if number <= 0:
            raise DraftError("Rows per piece must be more than 0.")
        return number

    @staticmethod
    def _pieces_definition(name: str, column: str, values: Any, separate_parquets: bool) -> dict[str, Any]:
        item: dict[str, Any] = {"name": name, "kind": "column_values", "applies_to": "PKTable",
                                "required_column": column}
        listed = split_list(values) if values not in (None, "") else []
        if listed:
            item["values"] = listed
        if separate_parquets:
            item["separate_parquets"] = True
        return item

    def set_pieces(self, index: int, **fields: Any) -> None:
        """column, values, separate_parquets, name; or rows for a chunk. The item
        is written out in full afterwards, as a transfer YAML writes it."""
        current = next((s for s in self.splitters() if s.where == "batching" and s.index == index), None)
        if current is None or current.problem:
            raise DraftError(f"Splitter {index + 1} cannot be read: {current.problem if current else 'none there'}")
        if current.rows is not None or "rows" in fields and not current.column:
            self.doc["batching"][index] = {"chunk": self._rows(fields.get("rows", current.rows))}
        else:
            column = fields.get("column", current.column)
            if "column" in fields:
                self._needs_pk_columns(column)
            self.doc["batching"][index] = self._pieces_definition(
                str(fields.get("name", current.name)), column,
                fields.get("values", current.values),
                bool(fields.get("separate_parquets", current.separate_parquets)),
            )
        self._changed()

    def remove_splitter(self, where: str, index: int) -> None:
        if where == "multipliers":
            self._multiplier(index, "split_after_build")
        elif not (0 <= index < len(self.doc.get(where) or [])):
            raise DraftError(f"There is no splitter at {index}.")
        del self.doc[where][index]
        self._changed()

    # ---------------------------------------------------------- fact tables

    def fact_tables(self) -> list[tuple[int, dict[str, Any]]]:
        """The cohorts that are not the PK, with their place in `cohorts`."""
        return [(i, c) for i, c in enumerate(self.doc["cohorts"]) if isinstance(c, dict) and not self.cohort_is_pk(c)]

    def _cohort(self, index: int) -> dict[str, Any]:
        cohorts = self.doc["cohorts"]
        if not (0 <= index < len(cohorts)) or not isinstance(cohorts[index], dict):
            raise DraftError(f"There is no table at {index}.")
        return cohorts[index]

    def add_prefab(self, recipe: str, name: str = "") -> int:
        found = self.ws.recipe(recipe)
        if found is None:
            raise DraftError(f"No recipe named {recipe}. {self.ws.recipes_problem()}".strip())
        if is_pk(found):
            raise DraftError(f"Recipe {recipe} makes a PK; choose it under PK Table.")
        self.doc["cohorts"].append({"recipe": recipe, "name": name.strip() or recipe})
        self._changed()
        return len(self.doc["cohorts"]) - 1

    def add_fact_table(self, cohort: dict[str, Any]) -> int:
        """A fact table built from the dictionary (the table builder makes it)."""
        cohort = copy.deepcopy(cohort)
        if is_pk(cohort):
            raise DraftError("That table is marked PK; set it under PK Table.")
        cohort.setdefault("type", "fact")
        self.doc["cohorts"].append(cohort)
        self._changed()
        return len(self.doc["cohorts"]) - 1

    TABLE_TEXTS = ("description", "granularity")

    def table_text(self, index: int, key: str) -> str:
        """A table's description or granularity as it will be written: its own,
        else its recipe's collated with the dictionary's (D121)."""
        if key not in self.TABLE_TEXTS:
            raise DraftError(f"A table's text is its description or granularity, not {key}.")
        merged = self._merged_cohort(index) or self._cohort(index)
        return " ".join(str(merged.get(key) or "").split())

    def set_table_text(self, index: int, key: str, text: str) -> None:
        """Keep, clear or extend the imported text. Only a change is written:
        the same text as imported writes nothing of the table's own (D121)."""
        cohort = self._cohort(index)
        cohort.pop(key, None)
        imported = self.table_text(index, key)
        text = " ".join(str(text or "").split())
        if text != imported:
            cohort[key] = text
        self._changed()

    def duplicate_table(self, index: int) -> int:
        """A copy of a fact table after it, as `<name>_copy` with its own
        destination, to change a name or a line (D125). Returns its place."""
        cohort = self._cohort(index)
        if self.cohort_is_pk(cohort):
            raise DraftError("That is the PK; a template has one PK.")
        copy_of = copy.deepcopy(cohort)
        taken = {str(c.get(k) or "") for c in self.doc["cohorts"] if isinstance(c, dict) for k in ("name", "dest_table")}
        base = str(cohort.get("name") or cohort.get("recipe") or "Table")
        name = f"{base}_copy"
        number = 2
        while name in taken:
            name, number = f"{base}_copy{number}", number + 1
        copy_of["name"] = name
        # Its own, always: a recipe's would land both tables in one destination.
        copy_of["dest_table"] = name
        self.doc["cohorts"].insert(index + 1, copy_of)
        # In the same table group, right after the table it copies (D134).
        for group in self._groups():
            tables = group.get("tables")
            if isinstance(tables, list) and base in tables:
                tables.insert(tables.index(base) + 1, name)
        self._changed()
        return index + 1

    def rename_table(self, index: int, name: str) -> None:
        cohort = self._cohort(index)
        old, name = str(cohort.get("name") or ""), name.strip()
        cohort["name"] = name
        # A group lists its tables by name, so it follows the rename (D134).
        for group in self._groups():
            tables = group.get("tables")
            if isinstance(tables, list) and old and old in tables:
                tables[tables.index(old)] = name
        self._changed()

    def remove_fact_table(self, index: int) -> None:
        cohort = self._cohort(index)
        if self.cohort_is_pk(cohort):
            raise DraftError("That is the PK; change it under PK Table.")
        del self.doc["cohorts"][index]
        for group in self._groups():
            tables = group.get("tables")
            if isinstance(tables, list) and cohort.get("name") in tables:
                tables.remove(cohort.get("name"))
        self._changed()

    def add_recipe_set(self, name: str) -> list[int]:
        """A recipe set's tables as ordinary fact tables, in a table group
        named for the set (D135). Checked whole before anything is added."""
        found = self.ws.recipe_set(name)
        if found is None:
            raise DraftError(f"No recipe set named {name}. {self.ws.recipes_problem()}".strip())
        entries = [t for t in found.get("tables") or [] if isinstance(t, dict)]
        if not entries:
            raise DraftError(f"Recipe set {name} lists no tables.")
        taken = {str(c.get("name")) for c in self.doc["cohorts"] if isinstance(c, dict)}
        names: list[str] = []
        for entry in entries:
            recipe = self.ws.recipe(str(entry.get("recipe") or ""))
            if recipe is None:
                raise DraftError(f"Recipe set {name} names recipe {entry.get('recipe')}, which recipes.yaml lacks.")
            if is_pk(recipe):
                raise DraftError(f"Recipe set {name} names {entry.get('recipe')}, a PK; a set holds fact tables.")
            table = str(entry.get("name") or entry.get("recipe"))
            if table in taken or table in names:
                raise DraftError(f"{table} is a table here already. Rename it, then add the set.")
            names.append(table)
        group_name = self._group_name(name)
        start = len(self.doc["cohorts"])
        for entry, table in zip(entries, names):
            self.doc["cohorts"].append({**copy.deepcopy(entry), "name": table})
        groups = self.doc.get("table_groups")
        if not isinstance(groups, list):
            groups = self.doc["table_groups"] = []
        groups.append({"name": group_name, "tables": names})
        self._changed()
        return list(range(start, len(self.doc["cohorts"])))

    # --------------------------------------------------------- table groups

    def _groups(self) -> list[dict[str, Any]]:
        groups = self.doc.get("table_groups")
        return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []

    def table_groups(self) -> list[tuple[int, str, list[str]]]:
        """Each table group (D134): its place, its name and its tables, in the order they run."""
        return [
            (i, str(g.get("name") or ""), [str(t) for t in g.get("tables") or []])
            for i, g in enumerate(self.doc.get("table_groups") or []) if isinstance(g, dict)
        ]

    def _group(self, index: int) -> dict[str, Any]:
        groups = self.doc.get("table_groups")
        if not isinstance(groups, list) or not (0 <= index < len(groups)) or not isinstance(groups[index], dict):
            raise DraftError(f"There is no table group at {index}.")
        return groups[index]

    def _group_name(self, name: str, index: int | None = None) -> str:
        name = str(name or "").strip()
        if not name:
            raise DraftError("A table group needs a name, such as Meds.")
        if my.safe_id(name, "group").lower() in my.RESERVED_GROUP_IDS:
            raise DraftError(f"A table group cannot be called {name}: the tables in no group run under that name.")
        for i, other, _ in self.table_groups():
            if i != index and my.safe_id(other, "group") == my.safe_id(name, "group"):
                raise DraftError(f"There is a table group called {other} already.")
        return name

    def add_group(self, name: str) -> int:
        groups = self.doc.get("table_groups")
        if not isinstance(groups, list):
            groups = self.doc["table_groups"] = []
        groups.append({"name": self._group_name(name), "tables": []})
        self._changed()
        return len(groups) - 1

    def rename_group(self, index: int, name: str) -> None:
        self._group(index)["name"] = self._group_name(name, index)
        self._changed()

    def remove_group(self, index: int) -> None:
        """Its tables go back to running with the tables in no group."""
        self._group(index)
        del self.doc["table_groups"][index]
        if not self.doc["table_groups"]:
            del self.doc["table_groups"]
        self._changed()

    def group_of(self, table: str) -> str | None:
        return next((name for _, name, tables in self.table_groups() if table in tables), None)

    def ungrouped_tables(self) -> list[str]:
        """The fact tables in no group, in Fact Tables order: what a group can take."""
        return [str(c.get("name")) for _, c in self.fact_tables()
                if c.get("name") and self.group_of(str(c.get("name"))) is None]

    def add_to_group(self, index: int, table: str) -> None:
        group = self._group(index)
        names = [str(c.get("name")) for _, c in self.fact_tables()]
        if table not in names:
            raise DraftError(f"{table} is not a fact table here.")
        owner = self.group_of(table)
        if owner is not None:
            raise DraftError(f"{table} is in table group {owner}; remove it from there first.")
        if not isinstance(group.get("tables"), list):
            group["tables"] = []
        group["tables"].append(table)
        self._changed()

    def remove_from_group(self, index: int, table: str) -> None:
        tables = self._group(index).get("tables")
        if not isinstance(tables, list) or table not in tables:
            raise DraftError(f"{table} is not in that group.")
        tables.remove(table)
        self._changed()

    def move_fact_table(self, index: int, position: int) -> None:
        """Move a fact table to `position` among the fact tables, 1 first; a
        number past the end is the end. The PK keeps its place."""
        places = [i for i, _ in self.fact_tables()]
        if index not in places:
            raise DraftError(f"There is no fact table at {index}.")
        order = [self.doc["cohorts"][i] for i in places]
        moving = order.pop(places.index(index))
        order.insert(min(max(int(position) - 1, 0), len(order)), moving)
        for place, cohort in zip(places, order):
            self.doc["cohorts"][place] = cohort
        self._changed()

    def _merged_cohort(self, index: int) -> dict[str, Any] | None:
        """The template's cohort with its recipe written in, as makeYaml reads it."""
        cohort = self._cohort(index)
        template = {"cohorts": [cohort]}
        merged = my.import_recipes(template, self.ws.recipes_doc(), my.CompileResult(),
                                   self.ws.dictionary())
        return merged[0] if merged else None

    def var_rows(self, index: int) -> list[VarRow]:
        """The variables a table's SQL needs that the template does not already
        supply, each labelled with where an unset value comes from (D78, D96)."""
        cohort = self._cohort(index)
        merged = self._merged_cohort(index)
        if merged is None:
            return []
        inputs = my.infer_table_inputs(merged)
        supplied = set(AUTOMATIC_VARS) | set(self.doc["vars"]) | set(self.doc.get("run_vars") or {})
        names = [n for n in my.infer_required_vars(merged) if n not in supplied and n not in inputs]
        pk = self.pk()
        this_is_pk = pk is not None and pk.where == "cohorts" and pk.index == index
        pk_vars = (self._cohort(pk.index).get("vars") or {}) if pk and pk.where == "cohorts" and not this_is_pk else {}
        own = cohort.get("vars") or {}
        rows = []
        for name in names:
            source = ""
            mult = next((m for _, m in self.multipliers()
                         if any(isinstance(lv, dict) and name in (lv.get("vars") or {}) for lv in m.get("levels") or [])), None)
            if mult is not None:
                source = f"set by multiplier {mult.get('name')}"
            elif pk is not None and not this_is_pk:
                source = f"from the PK ({value_text(pk_vars[name])})" if pk_vars.get(name) not in (None, "") else "from the PK's value"
            value = value_text(own.get(name))
            rows.append(VarRow(name=name, value=value, source=source, required=not value and not source))
        return rows

    def set_var(self, index: int, name: str, text: str) -> None:
        """A value, or several separated by commas; blank takes it from its source."""
        cohort = self._cohort(index)
        parts = split_list(text)
        own = cohort.setdefault("vars", {})
        if not parts:
            own.pop(name, None)
        else:
            own[name] = parts if len(parts) > 1 else parts[0]
        if not own:
            cohort.pop("vars", None)
        self._changed()

    def bindings(self, index: int) -> list[Binding]:
        """Each table input and the tables that could fill it, those that fit
        first. It never picks one (D45)."""
        cohort = self._cohort(index)
        merged = self._merged_cohort(index)
        if merged is None:
            return []
        own_dest = str(merged.get("dest_table") or merged.get("name"))
        schemas = self.validation().analysis.get("table_schemas") or {}
        uploads = {str(u.get("dest_table") or u.get("name")) for u in self.doc["upload_cohorts"] if isinstance(u, dict)}
        out = []
        for var, meta in my.infer_table_inputs(merged).items():
            if var == "PKTable":
                continue
            needed = list(meta.get("required_columns") or [])
            candidates = []
            for table, columns in schemas.items():
                if table == own_dest:
                    continue
                known = columns is not None
                have = set(columns or [])
                candidates.append(Candidate(
                    table=table, kind="upload" if table in uploads else "cohort",
                    matched=[c for c in needed if c in have] if known else [],
                    missing=[c for c in needed if c not in have] if known else [],
                    known=known,
                ))
            candidates.sort(key=lambda c: (not c.fits, c.known, len(c.missing), c.table))
            candidates.sort(key=lambda c: 0 if c.fits else (1 if not c.known else 2))
            bound = (cohort.get("vars") or {}).get(var) or self.doc["vars"].get(var) or ""
            out.append(Binding(var=var, alias=str(meta.get("alias") or ""), columns=needed,
                               bound=str(bound), candidates=candidates))
        return out

    def bind(self, index: int, var: str, table: str) -> None:
        self.set_var(index, var, table)

    # ------------------------------------------------ added where and join

    def filter_sources(self, index: int) -> list[tuple[str, str, str]]:
        """A table's columns a where line or join can be on: (its name, the
        SQL it is read from, its type)."""
        merged = self._merged_cohort(index) or {}
        out = []
        for column in merged.get("columns") or []:
            if isinstance(column, dict) and column.get("source"):
                source = str(column["source"])
                out.append((str(column.get("name") or source.split(".")[-1]), source, str(column.get("type") or "")))
        return out

    def template_tables(self, exclude: int | None = None) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """The template's tables a join or condition can reach, but the one at
        `exclude`: (name, kind, [(column, type)]), columns as they land."""
        out = []
        for i, cohort in enumerate(self.doc["cohorts"]):
            if i == exclude or not isinstance(cohort, dict):
                continue
            merged = self._merged_cohort(i) or cohort
            name = str(merged.get("dest_table") or merged.get("name") or "")
            columns = [(str(c.get("name") or str(c.get("source") or "").split(".")[-1]), str(c.get("type") or ""))
                       for c in merged.get("columns") or [] if isinstance(c, dict)]
            if name:
                out.append((name, "recipe" if cohort.get("recipe") else "table", columns))
        for index, upload in enumerate(self.doc["upload_cohorts"]):
            if not isinstance(upload, dict):
                continue
            name = str(upload.get("dest_table") or upload.get("name") or "")
            types = {my.column_source(c): str(c.get("type") or "")
                     for c in upload.get("columns") or [] if isinstance(c, dict)}
            columns = [(row.name, types.get(row.source, "")) for row in self.column_rows(index) if not row.dropped]
            if name:
                out.append((name, "upload", columns))
        return out

    def supporting_columns(self) -> dict[str, list[str]]:
        """Each supporting table's columns as they land, for "In supporting table"."""
        return {name: [c for c, _ in columns] for name, kind, columns in self.template_tables() if kind == "upload"}

    def join_check(self, source_type: str, table: str, column: str, exclude: int | None = None) -> tuple[bool, str]:
        """Whether a column of type `source_type` can join `table.column`."""
        other = next((t for t in self.template_tables(exclude) if t[0] == table), None)
        if other is None:
            return False, "No such table in this template."
        right = dict(other[2]).get(column)
        if right is None:
            return False, f"{table} has no column {column}."
        if not source_type or not right:
            return False, f"{'This column' if not source_type else f'{table}.{column}'} has no declared type, so the match cannot be checked."
        if types_compatible(source_type, right):
            return True, f"{type_family(source_type)} match"
        return False, f"{source_type} vs {right}"

    def _lines_key(self, index: int, kind: str) -> tuple[dict[str, Any], str]:
        """Where a table's added lines go: `add_where` / `add_join` on a
        prefabricated table, its own `where` / `join` on a built one (D105)."""
        if kind not in ("where", "join"):
            raise DraftError(f"A line is a where or a join, not {kind}.")
        cohort = self._cohort(index)
        filt = cohort.setdefault("filter", {})
        if not isinstance(filt, dict):
            raise DraftError("This table's filter is not a mapping; fix it in the YAML.")
        return filt, (f"add_{kind}" if cohort.get("recipe") else kind)

    def added_lines(self, index: int, kind: str) -> list[str]:
        filt, key = self._lines_key(index, kind)
        lines = filt.get(key) or []
        return [lines] if isinstance(lines, str) else [str(line) for line in lines]

    def table_aliases(self, index: int) -> dict[str, str]:
        """The aliases a table's `from` and joins define, its recipe's included."""
        merged = self._merged_cohort(index) or self._cohort(index)
        return line_aliases(my.cohort_sql_lines(merged))

    def add_line(self, index: int, kind: str, line: str) -> None:
        line = line.strip()
        if not line:
            raise DraftError("The line is empty.")
        problem = line_problem(line, self.table_aliases(index))
        if problem:
            raise DraftError(problem)
        filt, key = self._lines_key(index, kind)
        filt[key] = self.added_lines(index, kind) + [line]
        self._changed()

    def remove_line(self, index: int, kind: str, position: int) -> None:
        filt, key = self._lines_key(index, kind)
        lines = self.added_lines(index, kind)
        del lines[position]
        if lines:
            filt[key] = lines
        else:
            filt.pop(key, None)
            if not filt:
                self._cohort(index).pop("filter", None)
        self._changed()

    def add_where_by_column(self, index: int, source: str, mode: str, value: str = "",
                            table: str = "", column: str = "", higher: str = "") -> None:
        self.add_line(index, "where", where_line(source, mode, value, table, column, higher))

    def add_join_to(self, index: int, source: str, table: str, column: str,
                    join_type: str = "INNER", operator: str = "=") -> None:
        """A join from this table's column to another table of the template,
        refused unless the two columns' types match (D105)."""
        if join_type not in JOIN_TYPES or operator not in JOIN_OPERATORS:
            raise DraftError(f"A join is {'/'.join(JOIN_TYPES)} with {' '.join(JOIN_OPERATORS)}.")
        types = {src: typ for _, src, typ in self.filter_sources(index)}
        ok, text = self.join_check(types.get(source, ""), table, column, exclude=index)
        if not ok:
            raise DraftError(f"Not joined: {text}.")
        self.add_line(index, "join", join_line(join_type, source, operator, table, alias_for(table), column))

    # ----------------------------------------------------------- validation

    def location_for(self, chosen: str | Path) -> str:
        """A file chosen by Browse, as `file_loc`: relative to the draft's
        folder, with `..` where it must (D104). Only where no relative path
        exists (another drive) is it written in full."""
        path = Path(chosen)
        if not path.is_absolute():
            return path.as_posix()
        try:
            return Path(os.path.relpath(path.resolve(), self.base_path().parent.resolve())).as_posix()
        except ValueError:
            return str(path)

    def base_path(self) -> Path:
        """The file a relative `file_loc` is read from: the opened file, else
        where it will be saved."""
        return self.path or self.save_target()

    def validation(self) -> Validation:
        if self._validation is None:
            self._validation = self.validate()
        return self._validation

    def validate(self) -> Validation:
        """The draft checked as makeYaml checks a transfer: a file that is not
        here yet is a warning, or pending if marked so (D97)."""
        try:
            result = my.compile_yaml(
                template_path=self.base_path(),
                recipes_path=self.ws.recipes_path,
                datadictionary_path=self.ws.dictionary_path,
                # On the Mac a file may arrive later; on the VM it must be here (D108).
                uploads_elsewhere=not self.ws.vm_side,
                template_data=self.doc,
            )
        except Exception as exc:  # noqa: BLE001 - a crash is a message, never lost work
            result = my.CompileResult()
            result.error("compile_crashed", f"The check stopped: {type(exc).__name__}: {exc}",
                         fix="The draft is kept. Undo the last change, or report this with the draft.")
        messages = [self._message(kind, m) for kind, ms in
                    (("error", result.errors), ("warning", result.warnings), ("pending", result.pending))
                    for m in ms]
        if self.pull_from() == (False, False):
            messages.insert(0, Message(
                kind="error", code="no_database", text="Pull from: no database is chosen.",
                fix="Turn on Cosmos, Cosmos_SneakPeek, or both.", context="cosmos_db",
                field=FieldRef("project", None, "cosmos_db"),
            ))
            messages = [m for m in messages if m.code != "bad_cosmos_db"]
        errors = [m for m in messages if m.kind == "error"]
        return Validation(ok=not errors, messages=messages, steps=self._steps(messages),
                          analysis=result.analysis or {})

    @staticmethod
    def _steps(messages: list[Message]) -> list[tuple[str, str]]:
        """The pipeline, as the old Pipeline tab showed it, with pending (D96)."""
        codes = {m.code for m in messages if m.kind == "error"}
        pending = any(m.kind == "pending" for m in messages)
        def status(failed: bool, waiting: bool = False) -> str:
            return "fail" if failed else ("pending" if waiting else "pass")
        upload_failed = any(c.startswith(("missing_upload", "upload_", "bad_upload", "unknown_upload",
                                          "duplicate_upload", "bad_pending")) for c in codes)
        return [
            ("Load", status(bool(codes & {"invalid_template", "yaml_load_error", "compile_crashed"}))),
            ("Recipes", status(bool(codes & {"missing_recipe", "recipes_not_found"}))),
            ("Variables", status(bool(codes & {"missing_variable", "unbound_table_input"}))),
            ("Uploads", status(upload_failed, pending)),
            ("Columns", status(any("column" in c for c in codes))),
            ("Ready for the VM", status(bool(codes), pending)),
        ]

    def _message(self, kind: str, m: my.Message) -> Message:
        return Message(kind=kind, code=m.code, text=m.message, fix=m.fix, context=m.context,
                       field=self.field_of(m.context))

    def field_of(self, context: str) -> FieldRef | None:
        """The Builder field a message's context names, so a view can go there."""
        context = str(context or "")
        match = re.match(r"^(cohorts|upload_cohorts|multipliers|batching|table_groups)\[(\d+)\]\s*(.*)$", context)
        if match:
            where, index, detail = match.group(1), int(match.group(2)), match.group(3)
            return FieldRef(self._section_of(where, index), index, detail.strip())
        match = re.match(r"^upload_cohorts \((.+?)\)(.*)$", context)
        if match:
            for i, upload in enumerate(self.doc["upload_cohorts"]):
                if isinstance(upload, dict) and match.group(1) in (upload.get("name"), upload.get("dest_table")):
                    return FieldRef(self._section_of("upload_cohorts", i), i, match.group(2).strip(". "))
        if context.startswith("batching"):
            return FieldRef("splitters", None, context)
        project_keys = ("project_db", "cosmos_db", "min_date_key", "max_date_key", "smallset",
                        "stop_at_for_pk_table", "random_pk_sample", "project_folder", "temp_prefix")
        for key in project_keys:
            if key in context:
                return FieldRef("project", None, key)
        return None

    def _section_of(self, where: str, index: int) -> str:
        items = self.doc.get(where) or []
        item = items[index] if 0 <= index < len(items) and isinstance(items[index], dict) else {}
        if where == "cohorts":
            return "pk" if self.cohort_is_pk(item) else "fact"
        if where == "upload_cohorts":
            return "pk" if is_pk(item) else "supporting"
        if where == "multipliers":
            return "splitters" if item.get("stage") == "split_after_build" else "multipliers"
        if where == "table_groups":
            return "groups"
        return "splitters"

    # ------------------------------------------------------- save and export

    def save_target(self) -> Path:
        """`YAMLs/temp/<project>_intake.yaml` in the working folder (D95)."""
        return self.ws.temp_dir / intake_name(self.project_name)

    def yaml_text(self) -> str:
        return my.dump_yaml_text(self._document_to_save())

    def _document_to_save(self) -> dict[str, Any]:
        doc = copy.deepcopy(self.doc)
        options = doc.get("test_options")
        if isinstance(options, dict):
            for key in RETIRED_TEST_OPTIONS:
                options.pop(key, None)
        return doc

    def save(self) -> SaveResult:
        """Write the intake, read back before it replaces anything. Never
        replaces a file this draft did not open (D85)."""
        target = self.save_target()
        if target.exists() and (self.path is None or target.resolve() != self.path.resolve()):
            return SaveResult(False, (
                f"{target.name} already exists in {target.parent.name}/, and this draft did not "
                "open it, so it was not replaced. Open it, or change the Project name to save "
                "under a new name."
            ))
        doc = self._document_to_save()
        notes = self._repoint_uploads(doc, self.base_path().parent, target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        try:
            temp.write_text(my.dump_yaml_text(doc).rstrip() + "\n", encoding="utf-8")
            if not isinstance(my.load_yaml(temp), dict):
                raise ValueError("it did not read back as a template")
            os.replace(temp, target)
        except Exception as exc:  # noqa: BLE001 - said to the view
            temp.unlink(missing_ok=True)
            return SaveResult(False, f"Could not save {target.name}: {exc}")
        self.doc = doc
        self._shape()
        self.path = target
        self.dirty = False
        self._validation = None
        queued = queue_add(target.name, target.parent)
        if queued:
            notes.append("Added to the bundle queue.")
        return SaveResult(True, " ".join([f"Saved {target.parent.name}/{target.name}."] + notes), target)

    @staticmethod
    def _repoint_uploads(doc: dict[str, Any], source_dir: Path, target_dir: Path) -> list[str]:
        """Keep each relative `file_loc` reaching the same file from the new
        folder, with `..` where it must: a transfer YAML opened at the root and
        saved as an intake in YAMLs/temp/ keeps its uploads (D103)."""
        notes: list[str] = []
        if source_dir.resolve() == target_dir.resolve():
            return notes
        for upload in doc.get("upload_cohorts") or []:
            loc = upload.get("file_loc") if isinstance(upload, dict) else None
            if not loc or Path(str(loc)).is_absolute():
                continue
            upload["file_loc"] = my.repoint_file_loc(str(loc), source_dir, target_dir)
        return notes
        for upload in doc.get("upload_cohorts") or []:
            loc = upload.get("file_loc") if isinstance(upload, dict) else None
            if not loc or Path(str(loc)).is_absolute():
                continue
            moved = os.path.relpath((source_dir / str(loc)).resolve(), target_dir.resolve())
            if moved.startswith(".."):
                notes.append(f"{upload.get('name')}: file_loc {loc} is kept as written; put the file "
                             f"at {target_dir.name}/{loc} so it is found.")
            else:
                upload["file_loc"] = Path(moved).as_posix()
        return notes

    def export_transfer(self) -> tuple[bool, str, Path | None]:
        """The saved intake's transfer YAML, written to the working folder,
        where Run takes it (D94). The draft must be saved first."""
        if self.path is None or self.dirty:
            return False, "Save first: the transfer YAML is made from the saved intake.", None
        result = my.build_transfer(self.path, self.ws.recipes_path, write=True,
                                   datadictionary_path=self.ws.dictionary_path,
                                   output_dir=self.ws.home)
        if not result.ok:
            first = result.errors[0]
            return False, f"Not exported: {len(result.errors)} error(s). First: {first.message}", None
        out = Path(result.output_path)
        missing = result.analysis.get("transfer_uploads_missing") or []
        note = f" Not here yet, to copy to the VM: {', '.join(missing)}." if missing else ""
        return True, f"Wrote {out.name}.{note}", out


# =============================================================================
# The table builder: a PK or fact table from the data dictionary
# =============================================================================


def type_family(sql_type: Any) -> str:
    """A dictionary or SQL type's family, for matching join columns."""
    text = str(sql_type or "").lower()
    if re.search(r"char|text|string", text):
        return "string"
    if re.search(r"date|time", text):
        return "datetime"
    if re.search(r"bool|bit|flag", text):
        return "boolean"
    if re.search(r"bigint|integer|int|numeric|decimal|float|double|real", text):
        return "number"
    return re.sub(r"\s*\(.*", "", text).strip() or "unknown"


def types_compatible(left: Any, right: Any) -> bool:
    a, b = type_family(left), type_family(right)
    return a != "unknown" and b != "unknown" and a == b


def sql_type_for_dictionary(dictionary_type: Any) -> str:
    """The column type a cohort declares for a dictionary type: the one the
    export fills in (D115)."""
    return my.dictionary_sql_type(dictionary_type) or str(dictionary_type or "")


def alias_for(table: str) -> str:
    """`DiagnosisEventFact` is `def`: the first letter of each word."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", str(table or ""))
    return "".join(w[0] for w in words).lower() or str(table or "")[:3].lower() or "t"


JOIN_TYPES = ("INNER", "LEFT", "RIGHT", "FULL")
JOIN_OPERATORS = ("=", "<>", "<", "<=", ">", ">=")


class TableBuilder:
    """One table being built from the dictionary, before it joins the draft.

    It is the PK or a fact table depending on where it is committed; a
    loaded table is edited in place. Its columns come from the chosen table,
    in an order set by number; its joins reach the template's other tables.
    """

    def __init__(self, draft: Draft, index: int | None = None, pk: bool = False):
        self.draft = draft
        self.index = index
        self.pk = pk
        self.dictionary = draft.ws.dictionary()
        cohort = draft._cohort(index) if index is not None else {}
        if index is not None:
            self.pk = draft.cohort_is_pk(cohort)
        self.name = str(cohort.get("name") or "")
        self._imported: dict[str, str] = {}
        self.dest_table = str(cohort.get("dest_table") or "")
        self.description = str(cohort.get("description") or "")
        self.granularity = str(cohort.get("granularity") or "")
        self.pull_this_cycle = cohort.get("pull_this_cycle") is not False
        filt = cohort.get("filter") or {}
        self.from_table, self.alias = self._parse_from(filt.get("from"))
        self.joins: list[Any] = list(copy.deepcopy(filt.get("join") or []))
        self.wheres: list[str] = [str(w) for w in filt.get("where") or []]
        self.extra = {k: copy.deepcopy(v) for k, v in cohort.items()
                      if k not in ("name", "dest_table", "description", "granularity", "pull_this_cycle",
                                   "filter", "columns", "type", "recipe")}
        self.columns: list[dict[str, Any]] = []
        for column in cohort.get("columns") or []:
            if isinstance(column, dict):
                column = dict(column)
                source = str(column.get("source") or "")
                column.setdefault("name", source.split(".")[-1] if source else "")
                # The dictionary's type, which the export fills in (D115), for the
                # join checks; the template keeps none.
                column["type"] = self._dictionary_type(column) or column.get("type")
                if not column["type"]:
                    column.pop("type")
                self.columns.append(column)

    def _parse_from(self, value: Any) -> tuple[str, str]:
        text = str((value[0] if isinstance(value, list) and value else value) or "").strip()
        match = re.match(r"^(.+?)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)$", text, re.I)
        if match and match.group(1).strip() in self.dictionary:
            return match.group(1).strip(), match.group(2)
        return (text, alias_for(text)) if text in self.dictionary else ("", "")

    # ------------------------------------------------------------ columns

    def tables(self) -> list[str]:
        return sorted(self.dictionary)

    def dictionary_columns(self, table: str | None = None) -> dict[str, Any]:
        entry = self.dictionary.get(table or self.from_table) or {}
        columns = entry.get("columns") if isinstance(entry, dict) else None
        return columns if isinstance(columns, dict) else {}

    def _column_from(self, column: str) -> dict[str, Any]:
        meta = self.dictionary_columns().get(column) or {}
        out: dict[str, Any] = {"source": f"{self.alias}.{column}", "name": column,
                               "type": sql_type_for_dictionary(meta.get("type"))}
        if meta.get("nullable") is not None:
            out["nullable"] = bool(meta["nullable"])
        return out

    def _dictionary_type(self, column: dict[str, Any]) -> str | None:
        """The type the export fills in for a column of the from table, if any."""
        alias, _, name = str(column.get("source") or "").partition(".")
        if alias != self.alias or not name:
            return None
        meta = self.dictionary_columns().get(name)
        return my.dictionary_sql_type(meta.get("type")) if isinstance(meta, dict) else None

    @staticmethod
    def source_column(column: dict[str, Any]) -> str:
        return str(column.get("source") or "").split(".")[-1] or str(column.get("name") or "")

    def set_from_table(self, table: str) -> None:
        """Choose the table the rows come from: every column of it, joins cleared."""
        if table not in self.dictionary:
            raise DraftError(f"{table} is not in the data dictionary.")
        if table == self.from_table:
            return
        self.from_table = table
        self.alias = alias_for(table)
        self.columns = [self._column_from(c) for c in self.dictionary_columns()]
        self.joins = []
        # The table's standard lines, as ordinary where lines to keep or remove (D120).
        self.wheres = standard_where_lines(self.dictionary.get(table), self.alias)
        # Its description and granularity, to keep, clear or add to (D121); a
        # text left from the table chosen before is replaced too.
        entry = self.dictionary.get(table) or {}
        for key in ("description", "granularity"):
            if not getattr(self, key).strip() or getattr(self, key) == self._imported.get(key):
                setattr(self, key, " ".join(str(entry.get(key) or "").split()))
                self._imported[key] = getattr(self, key)

    def set_alias(self, alias: str) -> None:
        """Rename the from table's alias in its columns, joins and where lines (D118)."""
        alias = alias.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
            raise DraftError(f"`{alias}` cannot be an alias: letters, digits and _ only.")
        if alias == self.alias:
            return
        if alias in self.aliases():
            raise DraftError(f"`{alias}` is already a join's alias here; choose another.")
        old = self.alias
        for column in self.columns:
            if str(column.get("source") or "").startswith(f"{old}."):
                column["source"] = f"{alias}.{self.source_column(column)}"
        for i, join in enumerate(self.joins):
            if isinstance(join, str):
                self.joins[i] = rename_alias(join, old, alias)
            elif join.get("base_alias") == old:
                join["base_alias"] = alias
        self.wheres = [rename_alias(w, old, alias) for w in self.wheres]
        self.alias = alias

    def aliases(self) -> dict[str, str]:
        """This table's aliases: the from table's, and each join's."""
        found = {self.alias: self.from_table} if self.alias else {}
        for join in self.joins:
            if isinstance(join, str):
                found.update(line_aliases([("join", join)]))
            else:
                found[str(join.get("alias"))] = str(join.get("table"))
        return found

    def line_problem(self, line: str) -> str | None:
        return line_problem(line, self.aliases()) if line.strip() else None

    def removed_columns(self) -> list[str]:
        chosen = {self.source_column(c) for c in self.columns}
        return [c for c in self.dictionary_columns() if c not in chosen]

    def remove_column(self, index: int) -> None:
        del self.columns[index]

    def restore_column(self, column: str) -> None:
        if column not in self.dictionary_columns():
            raise DraftError(f"{self.from_table} has no column {column}.")
        self.columns.append(self._column_from(column))

    def move_column(self, index: int, position: int) -> int:
        """To `position`, 1 the top; past the end is the bottom. Returns where it went."""
        column = self.columns.pop(index)
        to = min(max(int(position) - 1, 0), len(self.columns))
        self.columns.insert(to, column)
        return to

    def set_column(self, index: int, **fields: Any) -> None:
        """name (its output name), description (for contents.md)."""
        column = self.columns[index]
        for key in ("name", "description"):
            if key in fields:
                value = str(fields[key]).strip()
                if value:
                    column[key] = value
                else:
                    column.pop(key, None)
        if not column.get("name"):
            column["name"] = self.source_column(column)

    # -------------------------------------------------------------- joins

    def join_tables(self) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """The template's other tables a join can reach: (name, kind, [(column, type)])."""
        return self.draft.template_tables(self.index)

    def join_check(self, base: int, table: str, column: str) -> tuple[bool, str]:
        """Whether a join of this table's column `base` to `table.column` matches types."""
        if not (0 <= base < len(self.columns)):
            return False, "Choose a column of this table."
        return self.draft.join_check(str(self.columns[base].get("type") or ""), table, column, self.index)

    def add_where_by_column(self, base: int, mode: str, value: str = "", table: str = "", column: str = "",
                            higher: str = "") -> None:
        if not (0 <= base < len(self.columns)):
            raise DraftError("Choose a column of this table.")
        self.wheres.append(where_line(str(self.columns[base].get("source") or ""), mode, value, table, column, higher))

    def add_join(self, base: int, table: str, column: str, join_type: str = "INNER", operator: str = "=") -> None:
        """A join to another table of the template, refused unless the types match."""
        ok, text = self.join_check(base, table, column)
        if not ok:
            raise DraftError(f"Not joined: {text}.")
        if join_type not in JOIN_TYPES or operator not in JOIN_OPERATORS:
            raise DraftError(f"A join is {'/'.join(JOIN_TYPES)} with {' '.join(JOIN_OPERATORS)}.")
        self.joins.append({"join_type": join_type, "base_alias": self.alias,
                           "base_column": self.source_column(self.columns[base]), "operator": operator,
                           "table": table, "alias": alias_for(table), "column": column})

    def add_join_text(self, text: str) -> None:
        """A join written out, for a Cosmos table: `INNER JOIN PatientDim AS p ON ...`,
        refused if it names an alias the table does not define (D118)."""
        if text.strip():
            problem = self.line_problem(text)
            if problem:
                raise DraftError(problem)
            self.joins.append(text.strip())

    def remove_join(self, index: int) -> None:
        del self.joins[index]

    @staticmethod
    def join_line(join: Any) -> str:
        if isinstance(join, str):
            return join
        return (f"{join.get('join_type', 'INNER')} JOIN {{{{prefix}}}}_{join['table']} AS {join['alias']} "
                f"ON {join['base_alias']}.{join['base_column']} {join.get('operator', '=')} "
                f"{join['alias']}.{join['column']}")

    def add_where(self, text: str = "") -> int:
        self.wheres.append(text.strip())
        return len(self.wheres) - 1

    def set_where(self, index: int, text: str) -> None:
        self.wheres[index] = text.strip()

    def remove_where(self, index: int) -> None:
        del self.wheres[index]

    # ------------------------------------------------------------- finish

    def build(self) -> dict[str, Any]:
        """The table as the template writes it."""
        name = self.name.strip() or "CustomTable"
        cohort: dict[str, Any] = {"name": name, "dest_table": self.dest_table.strip() or name,
                                  "type": "PK" if self.pk else "fact",
                                  "pull_this_cycle": self.pull_this_cycle}
        if self.description.strip():
            cohort["description"] = self.description.strip()
        if self.granularity.strip():
            cohort["granularity"] = self.granularity.strip()
        cohort.update(copy.deepcopy(self.extra))
        cohort["columns"] = [
            {k: c[k] for k in ("source", "name", "type", "nullable", "description") if c.get(k) not in (None, "")
             and not (k == "type" and self._dictionary_type(c))}  # the export fills it in (D115)
            for c in self.columns
        ]
        filt: dict[str, Any] = {}
        if self.from_table:
            filt["from"] = [f"{self.from_table} as {self.alias}"]
        joins = [self.join_line(j) for j in self.joins]
        if joins:
            filt["join"] = joins
        wheres = [w for w in self.wheres if w]
        if wheres:
            filt["where"] = wheres
        cohort["filter"] = filt
        return cohort

    def commit(self) -> int:
        """Put the table into the draft: in place if it was loaded, else as the
        PK or a new fact table. Returns its place in `cohorts`."""
        if not self.from_table:
            raise DraftError("Choose the table its rows come from first.")
        for line in [j for j in self.joins if isinstance(j, str)] + self.wheres:
            problem = self.line_problem(line)
            if problem:
                raise DraftError(f"{problem} In: {line}")
        cohort = self.build()
        if self.index is not None:
            if self.pk and not self.draft.cohort_is_pk(self.draft._cohort(self.index)):
                pk = self.draft.pk()
                if pk is not None:
                    raise DraftError(f"The PK is already {pk.name}; a template has one PK.")
            self.draft.doc["cohorts"][self.index] = cohort
            self.draft._changed()
            return self.index
        if self.pk:
            self.draft.set_pk_table(cohort)
            return 0
        return self.draft.add_fact_table(cohort)


def _read_recipes_file(recipes_path: Path, what: str) -> tuple[str, dict[str, Any]]:
    if not recipes_path.is_file():
        raise DraftError(f"There is no {recipes_path.name} here: recipes are kept on the Mac (D49), "
                         f"so {what} is saved there.")
    try:
        return recipes_path.read_text(encoding="utf-8"), my.load_yaml(recipes_path) or {}
    except Exception as exc:  # noqa: BLE001 - said to the view
        raise DraftError(f"Could not read {recipes_path.name}: {exc}") from exc


def _append_to_recipes(recipes_path: Path, text: str, additions: dict[str, list[dict[str, Any]]]) -> None:
    """Add entries to lists of recipes.yaml, keeping its comments and layout,
    and read it back before it replaces the file, so a bad write cannot break it."""
    lines = text.splitlines()
    for section, entries in additions.items():
        if not entries:
            continue
        start = next((i for i, line in enumerate(lines) if re.match(rf"^{section}:\s*(#.*)?$", line)), None)
        if start is None:
            lines += ["", f"{section}:"]
            end, indent = len(lines), "  "
        else:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#")), len(lines))
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            item = next((line for line in lines[start + 1:end] if line.lstrip().startswith("- ")), "  - ")
            indent = item[: len(item) - len(item.lstrip())]
        entry = [indent + line if line else line for line in my.dump_yaml_text(entries).rstrip().splitlines()]
        lines = lines[:end] + entry + lines[end:]
    temp = recipes_path.with_name(recipes_path.name + ".tmp")
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        check = my.load_yaml(temp) or {}
        for section, entries in additions.items():
            have = {str(r.get("name")) for r in check.get(section) or [] if isinstance(r, dict)}
            missing = [str(e.get("name")) for e in entries if str(e.get("name")) not in have]
            if missing:
                raise ValueError(f"the saved file did not read back with {', '.join(missing)}")
        os.replace(temp, recipes_path)
    except Exception as exc:  # noqa: BLE001 - said to the view
        temp.unlink(missing_ok=True)
        raise DraftError(f"Could not save to {recipes_path.name}: {exc}. The file is unchanged.") from exc


def save_recipe(recipes_path: Path, cohort: dict[str, Any]) -> tuple[bool, str]:
    """Add a table to recipes.yaml as a recipe (D56). A name already there is refused."""
    recipe = copy.deepcopy(cohort)
    recipe.pop("recipe", None)
    name = str(recipe.get("name") or "").strip()
    if not name:
        return False, "The table needs a Name before it can be saved as a recipe."
    try:
        text, existing = _read_recipes_file(recipes_path, "a table")
        names = {str(r.get("name")) for r in existing.get("recipes") or [] if isinstance(r, dict)}
        if name in names:
            return False, f"A recipe named {name} is already in {recipes_path.name}. Rename the table and save again."
        _append_to_recipes(recipes_path, text, {"recipes": [recipe]})
    except DraftError as exc:
        return False, str(exc)
    return True, f"Saved {name} to {recipes_path.name}."


def save_recipe_set(recipes_path: Path, draft: "Draft", group: int) -> tuple[bool, str]:
    """A table group as a recipe set in recipes.yaml (D135), in Fact Tables order.

    A prefabricated table is saved as it is in the template: its recipe, its
    name, and what the template adds to it (its variables, bindings to its
    siblings among them, and filters). A table built from the dictionary is
    saved as a recipe too, in the same write, since a set names recipes.
    """
    try:
        index, name, tables = next((g for g in draft.table_groups() if g[0] == group), (None, "", []))
        if index is None:
            raise DraftError(f"There is no table group at {group}.")
        if not tables:
            raise DraftError(f"Table group {name} has no tables to save.")
        text, existing = _read_recipes_file(recipes_path, "a recipe set")
        if name in {str(s.get("name")) for s in existing.get("recipe_sets") or [] if isinstance(s, dict)}:
            raise DraftError(f"A recipe set named {name} is already in {recipes_path.name}. Rename the group and save again.")
        recipe_names = {str(r.get("name")) for r in existing.get("recipes") or [] if isinstance(r, dict)}
        new_recipes: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        for _, cohort in draft.fact_tables():
            table = str(cohort.get("name") or "")
            if table not in tables:
                continue
            if cohort.get("recipe"):
                entries.append(copy.deepcopy(cohort))
                continue
            if table in recipe_names:
                raise DraftError(f"{table} is built here, and a recipe named {table} is already in "
                                 f"{recipes_path.name}. Rename the table and save again.")
            recipe = copy.deepcopy(cohort)
            new_recipes.append(recipe)
            entries.append({"recipe": table, "name": table})
        _append_to_recipes(recipes_path, text, {
            "recipes": new_recipes,
            "recipe_sets": [{"name": name, "tables": entries}],
        })
    except DraftError as exc:
        return False, str(exc)
    also = f", with {', '.join(r['name'] for r in new_recipes)} as recipes" if new_recipes else ""
    return True, f"Saved recipe set {name} to {recipes_path.name}{also}."


# =============================================================================
# Exports and the bundle queue (D91)
# =============================================================================


def exports(workspace: Workspace, path: Path) -> list[tuple[str, str, bool]]:
    """A saved intake's pre-YAML, transfer YAML and manifest, as
    (title, text, ok); a failed one shows its errors."""
    builds = (
        ("pre-YAML", lambda: my.build_preyaml(path, workspace.recipes_path, mode="symbolic")),
        ("Transfer YAML (to be bundled with bundle.py)", lambda: my.build_transfer(
            path, workspace.recipes_path, datadictionary_path=workspace.dictionary_path)),
        ("pullmanifest.yaml", lambda: my.build_pullmanifest(
            path, workspace.recipes_path, datadictionary_path=workspace.dictionary_path)),
    )
    out = []
    for title, build in builds:
        try:
            result = build()
        except Exception as exc:  # noqa: BLE001 - shown in its box
            out.append((title, f"{type(exc).__name__}: {exc}", False))
            continue
        if result.ok:
            out.append((title, my.dump_yaml_text(result.finished_yaml), True))
        else:
            out.append((title, json.dumps({"errors": [m.to_dict() for m in result.errors]}, indent=2), False))
    return out


def _queue_module():
    """The bundle queue lives with the bundle builder, on the Mac only."""
    try:
        import bundle_pullmanager
    except ImportError:
        return None
    return bundle_pullmanager


def queue_available() -> bool:
    return _queue_module() is not None


def queue_add(name: str, folder: Path) -> bool:
    module = _queue_module()
    return bool(module and module.queue_add(name, folder))


def queue_remove(name: str, folder: Path) -> bool:
    module = _queue_module()
    return bool(module and module.queue_remove(name, folder))


def make_bundle(workspace: Workspace, yamls_only: bool = False) -> tuple[bool, list[str], str]:
    """Build dist/bundle.py, the software with every queued intake's transfer
    YAML, or with `yamls_only` dist/yamls_to_transfer.py, the YAMLs alone; the
    queue is emptied after (D122). Returns (ok, what was done or why not, content_id)."""
    module = _queue_module()
    if module is None:
        return False, ["Bundles are made on the Mac, beside makebundle.py."], ""
    try:
        _, manifest, said = module.build_bundle([], True, queue_folder=workspace.temp_dir,
                                                export_dir=workspace.home, yamls_only=yamls_only)
    except module.BundleError as exc:
        return False, [str(exc)], ""
    return True, said, str(manifest["content_id"])


def project_of(intake: str) -> str:
    """`Infant_RSV_intake.yaml` is `Infant_RSV` (D122: Exports lists projects)."""
    name = Path(intake).name
    return name[: -len(INTAKE_SUFFIX)] if name.endswith(INTAKE_SUFFIX) else Path(name).stem


def queue_state(workspace: Workspace) -> dict[str, Any]:
    """The queued intakes, and the ones that could join (D91)."""
    module = _queue_module()
    queued = module.read_queue(workspace.temp_dir) if module else []
    names = [p.name for p in workspace.intakes()]
    return {
        "available": module is not None,
        "queue": [{"name": n, "project": project_of(n), "exists": (workspace.temp_dir / n).is_file()}
                  for n in queued],
        "addable": [n for n in names if n not in queued],
    }


# =============================================================================
# Tests
# =============================================================================


RECIPES = """
batching_recipes:
  - name: sex
    kind: column_values
    applies_to: PKTable
    required_column: Sex
    values: [Female, Male]
recipes:
  - name: Patients
    type: PK
    dest_table: Patients
    columns:
      - {source: p.DurableKey, name: PatientDurableKey, type: BIGINT, nullable: false}
      - {source: p.Sex, name: Sex, type: VARCHAR(50)}
    filter:
      from: PatientDim AS p
      where:
        - "{{sql_condition('p.Value', ICD_Value)}}"
  - name: Codes
    type: fact
    dest_table: CodedVisits
    columns:
      - {source: e.EncounterKey, name: EncounterKey, type: BIGINT}
    filter:
      from: EncounterFact AS e
      join:
        - "INNER JOIN {{prefix}}_{{PKTable}} AS pk ON pk.PatientDurableKey = e.PatientDurableKey"
        - "INNER JOIN {{prefix}}_{{CodesTable}} AS c ON c.Code = e.EncounterKey"
      where:
        - "{{sql_condition('e.Value', ICD_Value)}}"
"""


DICTIONARY = """
DataDictionary:
  EncounterFact:
    description: Encounters.
    columns:
      EncounterKey: {type: bigint, nullable: false}
      PatientDurableKey: {type: bigint, nullable: false}
      DateKey: {type: integer, nullable: true}
      Department: {type: string, nullable: true}
  PatientDim:
    columns:
      DurableKey: {type: bigint, nullable: false}
      Sex: {type: string, nullable: true}
"""


class ModelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "YAMLs" / "temp").mkdir(parents=True)
        (self.home / "YAMLs" / "recipes.yaml").write_text(RECIPES, encoding="utf-8")
        (self.home / "YAMLs" / "datadictionary.yaml").write_text(DICTIONARY, encoding="utf-8")
        self.ws = Workspace(
            home=self.home,
            recipes_path=self.home / "YAMLs" / "recipes.yaml",
            dictionary_path=self.home / "YAMLs" / "datadictionary.yaml",
            defaults_path=self.home / "YAMLs" / "template.yaml",
        )

    def draft(self) -> Draft:
        draft = Draft.new(self.ws)
        draft.project_name = "Test Run"
        return draft

    def codes_csv(self, header: str = "Code,Label") -> None:
        (self.home / "YAMLs" / "temp" / "csv").mkdir(exist_ok=True)
        (self.home / "YAMLs" / "temp" / "csv" / "codes.csv").write_text(f"{header}\nK50,x\n", encoding="utf-8")

    def codes_draft(self) -> Draft:
        """A PK recipe, a supporting CSV, and a fact table bound to it."""
        self.codes_csv()
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        draft.add_supporting("csv", "Codes", "csv/codes.csv")
        draft.add_prefab("Codes")
        return draft


class ProjectTests(ModelTest):
    def test_a_new_draft_takes_the_defaults(self):
        draft = Draft.new(self.ws)
        self.assertEqual(draft.project_db, "PROJECTD93A5E7")
        self.assertEqual(draft.pull_from(), (True, True))
        self.assertEqual(draft.dates(), ("19900101", "20260601"))
        self.assertTrue(draft.collect_all)

    def test_defaults_come_from_template_yaml_where_it_says(self):
        self.ws.defaults_path.write_text("cosmos_vars:\n  project_db: PROJECTDABC\n", encoding="utf-8")
        self.assertEqual(Draft.new(self.ws).project_db, "PROJECTDABC")

    def test_pull_from_maps_onto_cosmos_db(self):
        draft = self.draft()
        for pair, written in (((True, False), "COSMOS"), ((False, True), "COSMOS_SneakPeek"), ((True, True), "Dual")):
            draft.set_pull_from(*pair)
            self.assertEqual(draft.doc["cosmos_vars"]["cosmos_db"], written)
            self.assertEqual(draft.pull_from(), pair)

    def test_neither_database_is_an_error_not_dual(self):
        # Left blank, cosmos_db would mean Dual (D86): neither must stay loud.
        draft = self.codes_draft()
        draft.set_pull_from(False, False)
        self.assertEqual(draft.pull_from(), (False, False))
        codes = [m.code for m in draft.validate().of_kind("error")]
        self.assertIn("no_database", codes)
        self.assertNotIn("bad_cosmos_db", codes)

    def test_collect_all_is_smallset_off(self):
        draft = self.draft()
        draft.collect_all = False
        draft.sample_size = "250"
        draft.random_sample = True
        options = draft.doc["test_options"]
        self.assertEqual((options["smallset"], options["stop_at_for_pk_table"], options["random_pk_sample"]),
                         (True, 250, True))

    def test_a_flat_setting_is_edited_where_it_is(self):
        # A hand-written template may say project_db at the top level.
        draft = Draft(self.ws, {"project_db": "PROJECTD1", "cohorts": []})
        draft.project_db = "PROJECTD2"
        self.assertEqual(draft.doc["project_db"], "PROJECTD2")
        self.assertNotIn("cosmos_vars", draft.doc)


class PkTests(ModelTest):
    def test_one_pk_whichever_kind_replaces_the_other(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_pk_upload("parquet", "ClientPK", "pks.parquet", "PatientDurableKey", pending_transfer=True)
        self.assertEqual([c for c in draft.doc["cohorts"]], [])
        pk = draft.pk()
        self.assertEqual((pk.kind, pk.name, pk.location, pk.key_columns, pk.pending_transfer),
                         ("parquet", "ClientPK", "pks.parquet", ["PatientDurableKey"], True))
        self.assertEqual(draft.doc["upload_cohorts"][0]["type"], "pk")

    def test_a_fact_recipe_is_refused_as_the_pk_and_the_reverse(self):
        draft = self.draft()
        with self.assertRaises(DraftError):
            draft.set_pk_recipe("Codes")
        with self.assertRaises(DraftError):
            draft.add_prefab("Patients")

    def test_a_recipe_pks_columns_are_known_before_validation(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        self.assertEqual(draft.pk_columns(), ["PatientDurableKey", "Sex"])

    def test_a_pending_pk_takes_its_typed_columns(self):
        draft = self.draft()
        draft.set_pk_upload("csv", "ClientPK", "pks.csv", "PatientDurableKey", pending_transfer=True)
        self.assertIsNone(draft.pk_columns())
        draft.set_listed_columns(0, "PatientDurableKey, Sex")
        self.assertEqual(draft.pk_columns(), ["PatientDurableKey", "Sex"])

    def test_a_dbtable_is_never_pending(self):
        draft = self.draft()
        draft.set_pk_upload("dbtable", "ClientPK", "ListFromClient", "PatientDurableKey", pending_transfer=True)
        entry = draft.doc["upload_cohorts"][0]
        self.assertEqual(entry["source_table"], "ListFromClient")
        self.assertNotIn("pending_transfer", entry)
        with self.assertRaises(DraftError):
            draft.update_pk(pending_transfer=True)


class PastedCsvTests(ModelTest):
    """D127: rows pasted in become a named CSV kept on the Mac, and a table that reads it."""

    def test_pasted_rows_become_a_named_csv_and_a_table_reading_it(self):
        draft = self.draft()
        index = draft.add_pasted_csv("RsvCodes", "Code\tLabel\nJ12.1\tRSV pneumonia\nB97.4\tRSV, as the cause\n")
        path = self.home / "YAMLs" / "temp" / "csv" / "RsvCodes.csv"
        self.assertEqual(path.read_text(encoding="utf-8"),
                         'Code,Label\nJ12.1,RSV pneumonia\nB97.4,"RSV, as the cause"\n')
        upload = draft.doc["upload_cohorts"][index]
        self.assertEqual((upload["name"], upload["file_type"], upload["file_loc"]), ("RsvCodes", "csv", "csv/RsvCodes.csv"))
        self.assertEqual(draft.supporting_columns()["RsvCodes"], ["Code", "Label"])

    def test_a_ragged_row_or_a_bad_header_is_refused_naming_it(self):
        for text, words in (("A,B\n1,2\n3\n", "Line 3 has 1 values"), ("A,,C\n1,2,3\n", "column 2 is blank"),
                            ("A,A\n1,2\n", "A more than once"), ("A,B\n", "Only a header")):
            with self.subTest(text=text):
                ok, said = pasted_summary(text)
                self.assertFalse(ok)
                self.assertIn(words, said)
        self.assertTrue(pasted_summary("A,B\n1,2\n")[0])

    def test_it_asks_for_a_name_and_keeps_a_file_already_there(self):
        draft = self.draft()
        with self.assertRaises(DraftError):
            draft.add_pasted_csv("", "A\n1\n")
        draft.add_pasted_csv("Codes", "A\n1\n")
        with self.assertRaises(DraftError) as caught:
            draft.add_pasted_csv("Codes", "A\n2\n")
        self.assertIn("choose another name", str(caught.exception))
        self.assertEqual((self.home / "YAMLs" / "temp" / "csv" / "Codes.csv").read_text(encoding="utf-8"), "A\n1\n")


class SupportingTests(ModelTest):
    def test_the_files_columns_show_and_rename_and_drop_as_written(self):
        draft = self.codes_draft()
        index = draft.supporting()[0][0]
        self.assertEqual([r.source for r in draft.column_rows(index)], ["Code", "Label"])
        draft.rename_column(index, "Code", "ICDCode")
        draft.drop_column(index, "Label")
        self.assertEqual(draft.doc["upload_cohorts"][index]["columns"],
                         [{"name": "ICDCode", "from": "Code"}, {"name": "Label", "drop": True}])
        rows = draft.column_rows(index)
        self.assertEqual([(r.source, r.name, r.dropped) for r in rows],
                         [("Code", "ICDCode", False), ("Label", "Label", True)])

    def test_renaming_back_leaves_nothing_behind(self):
        draft = self.codes_draft()
        index = draft.supporting()[0][0]
        draft.rename_column(index, "Code", "ICDCode")
        draft.rename_column(index, "Code", "Code")
        self.assertNotIn("columns", draft.doc["upload_cohorts"][index])

    def test_a_rename_reaches_validation(self):
        # The fact table reads c.Code: renamed away, it is missing (D98).
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        index = draft.supporting()[0][0]
        self.assertNotIn("missing_input_column", [m.code for m in draft.validate().messages])
        draft.rename_column(index, "Code", "ICDCode")
        self.assertIn("missing_input_column", [m.code for m in draft.validate().messages])

    def test_pending_marks_a_missing_file_blue(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        index = draft.add_supporting("csv", "Later", "csv/later.csv")
        self.assertIn("missing_upload_file", [m.code for m in draft.validate().of_kind("warning")])
        draft.update_supporting(index, pending_transfer=True)
        validation = draft.validate()
        self.assertEqual([m.code for m in validation.of_kind("pending")], ["upload_pending_transfer"])
        self.assertIn(("Uploads", "pending"), validation.steps)


class SplitterTests(ModelTest):
    def test_before_a_pk_only_a_chunk_can_be_added(self):
        draft = self.draft()
        with self.assertRaises(DraftError) as caught:
            draft.add_pieces_by_column("Sex")
        self.assertIn("Choose a PK Table first", str(caught.exception))
        with self.assertRaises(DraftError):
            draft.add_separate("Race")
        draft.add_chunk("5000")
        self.assertEqual(draft.doc["batching"], [{"chunk": 5000}])

    def test_a_column_the_pk_lacks_is_refused(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        with self.assertRaises(DraftError) as caught:
            draft.add_pieces_by_column("State")
        self.assertIn("PatientDurableKey, Sex", str(caught.exception))

    def test_each_kind_writes_the_key_makeyaml_reads(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.add_pieces_by_column("Sex", "Female", separate_parquets=True)
        mult = draft.add_separate("SexTables")
        draft.add_separate_level(mult, "f", "Sex", "Female")
        draft.add_separate_level(mult, "m", "Sex", "Male", role="control", row_mult=2)
        self.assertEqual(draft.doc["batching"][0], {
            "name": "Sex", "kind": "column_values", "applies_to": "PKTable",
            "required_column": "Sex", "values": ["Female"], "separate_parquets": True})
        self.assertEqual(draft.doc["multipliers"][mult]["stage"], "split_after_build")
        self.assertEqual(draft.doc["multipliers"][mult]["levels"][1],
                         {"strat": "m", "column": "Sex", "values": ["Male"], "role": "control", "row_mult": 2})
        kinds = [(s.kind, s.name) for s in draft.splitters()]
        self.assertEqual(kinds, [("separate", "SexTables"), ("pieces", "Sex")])
        self.assertEqual(draft.multipliers(), [])

    def test_a_recipe_batch_reads_as_its_definition(self):
        draft = self.draft()
        draft.doc["batching"] = ["sex", {"chunk": 100}]
        pieces = draft.splitters()
        self.assertEqual((pieces[0].column, pieces[0].values), ("Sex", ["Female", "Male"]))
        self.assertEqual(pieces[1].rows, 100)

    def test_no_values_is_every_value(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.add_pieces_by_column("Sex")
        self.assertNotIn("values", draft.doc["batching"][0])
        self.assertIsNone(draft.splitters()[0].values)


class FactTableTests(ModelTest):
    def test_variables_say_where_an_unset_value_comes_from(self):
        draft = self.codes_draft()
        rows = {r.name: r for r in draft.var_rows(1)}
        self.assertEqual(list(rows), ["ICD_Value"])  # CodesTable is a binding, prefix and PKTable automatic
        self.assertEqual(rows["ICD_Value"].source, "from the PK (K50%)")
        self.assertFalse(rows["ICD_Value"].required)
        pk_row = draft.var_rows(0)[0]
        self.assertEqual((pk_row.value, pk_row.source), ("K50%", ""))

    def test_a_multiplier_is_named_as_the_source(self):
        draft = self.codes_draft()
        mult = draft.add_multiplier("IBDType")
        draft.add_level(mult, "UC", "ICD_Value: K51.%")
        self.assertEqual(draft.var_rows(1)[0].source, "set by multiplier IBDType")

    def test_a_pk_with_no_value_is_required(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        self.assertTrue(draft.var_rows(0)[0].required)

    def test_binding_candidates_fit_first_and_nothing_is_picked(self):
        draft = self.codes_draft()
        binding = draft.bindings(1)[0]
        self.assertEqual((binding.var, binding.alias, binding.columns, binding.bound),
                         ("CodesTable", "c", ["Code"], ""))
        first = binding.candidates[0]
        self.assertEqual((first.table, first.matched, first.fits), ("Codes", ["Code"], True))
        self.assertNotIn("CodedVisits", [c.table for c in binding.candidates])
        self.assertIn("unbound_table_input", [m.code for m in draft.validate().of_kind("error")])

    def test_order_by_number_keeps_the_pk_in_place(self):
        draft = self.codes_draft()
        draft.doc["cohorts"].append({"recipe": "Codes", "name": "Second"})
        draft.move_fact_table(2, 1)
        self.assertEqual([c.get("name") for c in draft.doc["cohorts"]], ["Patients", "Second", "Codes"])
        draft.move_fact_table(1, 99)
        self.assertEqual([c.get("name") for c in draft.doc["cohorts"]], ["Patients", "Codes", "Second"])


class MessageTests(ModelTest):
    def test_a_message_points_at_its_section_and_entry(self):
        draft = self.codes_draft()
        unbound = next(m for m in draft.validate().messages if m.code == "unbound_table_input")
        self.assertEqual((unbound.field.section, unbound.field.index), ("fact", 1))
        self.assertEqual(draft.field_of("upload_cohorts[1] (Codes).file_loc").section, "supporting")
        self.assertEqual(draft.field_of("cohorts[0] (Patients): vars").section, "pk")
        self.assertEqual(draft.field_of("cosmos_db").section, "project")

    def test_a_crash_in_the_check_is_a_message_and_the_draft_is_kept(self):
        draft = self.codes_draft()
        before = copy.deepcopy(draft.doc)
        original = my.compile_yaml
        my.compile_yaml = lambda **kwargs: (_ for _ in ()).throw(TypeError("'NoneType' object is not iterable"))
        try:
            validation = draft.validate()
        finally:
            my.compile_yaml = original
        self.assertEqual([m.code for m in validation.of_kind("error")], ["compile_crashed"])
        self.assertEqual(draft.doc, before)


class SaveTests(ModelTest):
    def test_saved_as_an_intake_and_read_back_the_same(self):
        draft = self.codes_draft()
        result = draft.save()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.path, self.home / "YAMLs" / "temp" / "Test_Run_intake.yaml")
        self.assertFalse(draft.dirty)
        again = Draft.open(self.ws, result.path)
        self.assertEqual(again.doc["cohorts"], draft.doc["cohorts"])
        self.assertEqual(again.doc["upload_cohorts"], draft.doc["upload_cohorts"])

    def test_a_new_draft_never_replaces_an_intake_it_did_not_open(self):
        self.assertTrue(self.codes_draft().save().ok)
        other = self.draft()
        result = other.save()
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.message)

    def test_an_opened_intake_saves_over_itself(self):
        path = self.codes_draft().save().path
        draft = Draft.open(self.ws, path)
        draft.project_db = "PROJECTD2"
        self.assertTrue(draft.save().ok)
        self.assertEqual(Draft.open(self.ws, path).project_db, "PROJECTD2")

    def test_the_transfer_needs_a_saved_draft_and_lands_in_the_working_folder(self):
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        self.assertFalse(draft.export_transfer()[0])
        self.assertTrue(draft.save().ok)
        ok, message, path = draft.export_transfer()
        self.assertTrue(ok, message)
        self.assertEqual(path.parent, self.home)
        # On the VM the same step: open the transfer, adjust, save, export (D94).
        vm = Draft.open(Workspace(self.home, self.home / "none.yaml", self.ws.dictionary_path,
                                  self.ws.defaults_path), path)
        self.assertEqual(vm.ws.pk_recipes(), [])
        self.assertTrue(vm.validate().ok, [m.text for m in vm.validate().of_kind("error")])


class TableBuilderTests(ModelTest):
    def test_the_builder_declares_the_type_the_export_fills_in(self):
        res = my.CompileResult()
        for table, entry in (my.load_datadictionary(None, res) or {}).items():
            for column, meta in ((entry or {}).get("columns") or {}).items():
                raw = (meta or {}).get("type")
                with self.subTest(table=table, column=column, type=raw):
                    self.assertEqual(sql_type_for_dictionary(raw), my.dictionary_sql_type(raw))

    def encounters(self, draft: Draft, pk: bool = False) -> TableBuilder:
        builder = TableBuilder(draft, pk=pk)
        builder.name = "Visits"
        builder.set_from_table("EncounterFact")
        return builder

    def test_a_table_takes_every_column_of_its_source_typed(self):
        builder = self.encounters(self.draft())
        self.assertEqual(builder.alias, "ef")
        self.assertEqual(builder.columns[0], {"source": "ef.EncounterKey", "name": "EncounterKey",
                                              "type": "BIGINT", "nullable": False})
        self.assertEqual(builder.columns[3]["type"], "NVARCHAR(900)")

    def test_columns_are_removed_restored_renamed_and_ordered_by_number(self):
        builder = self.encounters(self.draft())
        builder.remove_column(3)
        self.assertEqual(builder.removed_columns(), ["Department"])
        builder.restore_column("Department")
        builder.set_column(3, name="Dept", description="Where it happened")
        self.assertEqual(builder.move_column(3, 1), 0)
        self.assertEqual([c["name"] for c in builder.columns], ["Dept", "EncounterKey", "PatientDurableKey", "DateKey"])
        builder.set_alias("e")
        self.assertEqual(builder.columns[0]["source"], "e.Department")

    def test_committed_it_is_a_fact_table_the_check_accepts(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        builder = self.encounters(draft)
        builder.add_join(1, "Patients", "PatientDurableKey")
        builder.add_where("ef.DateKey BETWEEN {{min_date_key}} AND {{max_date_key}}")
        index = builder.commit()
        cohort = draft.doc["cohorts"][index]
        self.assertEqual(cohort["filter"]["from"], ["EncounterFact as ef"])
        self.assertEqual(cohort["filter"]["join"],
                         ["INNER JOIN {{prefix}}_Patients AS p ON ef.PatientDurableKey = p.PatientDurableKey"])
        validation = draft.validate()
        self.assertTrue(validation.ok, [m.text for m in validation.of_kind("error")])

    def test_a_written_join_naming_an_alias_the_table_lacks_is_refused(self):
        # Infant_RSV: `pk.` from a recipe, on a table aliased `ef` here.
        draft = self.draft()
        builder = self.encounters(draft)
        with self.assertRaises(DraftError) as caught:
            builder.add_join_text("INNER JOIN DurationDim AS age ON age.DurationKey = pk.AgeKey")
        self.assertIn("`pk`", str(caught.exception))
        self.assertIn("age, ef", str(caught.exception))
        self.assertEqual(builder.joins, [])
        builder.add_join_text("INNER JOIN DurationDim AS age ON age.DurationKey = ef.AgeKey")
        self.assertEqual(len(builder.joins), 1)

    def test_a_table_with_a_bad_written_where_is_not_committed(self):
        draft = self.draft()
        builder = self.encounters(draft)
        builder.add_where("p.IsValid = 1")
        self.assertIn("`p`", builder.line_problem("p.IsValid = 1"))
        with self.assertRaises(DraftError):
            builder.commit()
        self.assertEqual(draft.doc["cohorts"], [])

    def test_renaming_the_alias_rewrites_the_tables_own_lines(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        builder = self.encounters(draft)
        builder.add_join(1, "Patients", "PatientDurableKey")
        builder.add_join_text("LEFT JOIN {{prefix}}_Patients AS pt ON pt.PatientDurableKey = ef.PatientDurableKey")
        builder.add_where("ef.DateKey BETWEEN {{min_date_key}} AND {{max_date_key}}")
        builder.add_where("ef.Department <> 'ef.x'")
        builder.set_alias("enc")
        cohort = draft.doc["cohorts"][builder.commit()]
        self.assertEqual(cohort["filter"]["join"], [
            "INNER JOIN {{prefix}}_Patients AS p ON enc.PatientDurableKey = p.PatientDurableKey",
            "LEFT JOIN {{prefix}}_Patients AS pt ON pt.PatientDurableKey = enc.PatientDurableKey"])
        self.assertEqual(cohort["filter"]["where"], [
            "enc.DateKey BETWEEN {{min_date_key}} AND {{max_date_key}}", "enc.Department <> 'ef.x'"])
        self.assertTrue(draft.validate().ok, [m.text for m in draft.validate().of_kind("error")])

    def test_an_alias_a_join_already_has_is_refused(self):
        builder = self.encounters(self.draft())
        builder.add_join_text("INNER JOIN DurationDim AS age ON age.DurationKey = ef.AgeKey")
        with self.assertRaises(DraftError):
            builder.set_alias("age")
        self.assertEqual(builder.alias, "ef")

    def test_a_table_arrives_with_its_standard_where_lines(self):
        path = self.home / "YAMLs" / "datadictionary.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "  EncounterFact:\n", "  EncounterFact:\n    standard_where: [DateKey > 0, Department <> 'X']\n", 1),
            encoding="utf-8")
        draft = self.draft()
        builder = self.encounters(draft)
        self.assertEqual(builder.wheres, ["ef.DateKey > 0", "ef.Department <> 'X'"])
        builder.remove_where(1)
        builder.set_alias("e")
        cohort = draft.doc["cohorts"][builder.commit()]
        self.assertEqual(cohort["filter"]["where"], ["e.DateKey > 0"])

    def test_the_repositorys_standard_lines_name_their_tables_columns(self):
        for table, entry in (my.load_datadictionary(None, my.CompileResult()) or {}).items():
            for line in standard_where_lines(entry, "x"):
                column = line.split()[0].split(".", 1)[1]
                with self.subTest(table=table, line=line):
                    self.assertIn(column, entry.get("columns") or {})

    def test_a_new_table_starts_with_the_dictionarys_description_and_granularity(self):
        draft = self.draft()
        builder = self.encounters(draft)
        self.assertEqual(builder.description, "Encounters.")
        builder.description += " Only the ones we need."
        cohort = draft.doc["cohorts"][builder.commit()]
        self.assertEqual(cohort["description"], "Encounters. Only the ones we need.")

    def test_the_template_keeps_no_type_the_dictionary_supplies(self):
        # A copy in the template goes stale when the dictionary is corrected.
        draft = self.draft()
        index = self.encounters(draft).commit()
        columns = draft.doc["cohorts"][index]["columns"]
        self.assertTrue(columns and not any("type" in c for c in columns), columns)
        # Opened again, the builder still knows the types, for its join checks.
        again = TableBuilder(draft, index)
        self.assertEqual(again.columns[0]["type"], "BIGINT")

    def test_a_join_whose_types_differ_is_refused(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        builder = self.encounters(draft)
        self.assertEqual(builder.join_check(3, "Patients", "PatientDurableKey"), (False, "NVARCHAR(900) vs BIGINT"))
        with self.assertRaises(DraftError):
            builder.add_join(3, "Patients", "PatientDurableKey")

    def test_an_upload_column_without_a_type_cannot_be_checked(self):
        self.codes_csv()
        draft = self.draft()
        draft.add_supporting("csv", "Codes", "csv/codes.csv")
        builder = self.encounters(draft)
        ok, text = builder.join_check(0, "Codes", "Code")
        self.assertFalse(ok)
        self.assertIn("no declared type", text)
        draft.set_column_type(0, "Code", "BIGINT")
        self.assertTrue(self.encounters(draft).join_check(0, "Codes", "Code")[0])

    def test_a_loaded_table_is_edited_in_place_keeping_what_the_builder_does_not_show(self):
        draft = self.draft()
        index = self.encounters(draft).commit()
        draft.doc["cohorts"][index]["dedup_keys"] = ["EncounterKey"]
        again = TableBuilder(draft, index)
        self.assertEqual((again.from_table, again.alias, len(again.columns)), ("EncounterFact", "ef", 4))
        again.remove_column(3)
        self.assertEqual(again.commit(), index)
        self.assertEqual(len(draft.doc["cohorts"]), 1)
        self.assertEqual(draft.doc["cohorts"][index]["dedup_keys"], ["EncounterKey"])
        self.assertEqual(len(draft.doc["cohorts"][index]["columns"]), 3)

    def test_as_the_pk_it_replaces_the_pk_there_was(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        self.encounters(draft, pk=True).commit()
        pk = draft.pk()
        self.assertEqual((pk.kind, pk.name), ("dictionary", "Visits"))
        self.assertEqual(len(draft.doc["cohorts"]), 1)

    def test_nothing_is_committed_without_a_source_table(self):
        with self.assertRaises(DraftError):
            TableBuilder(self.draft()).commit()


class SaveRecipeTests(ModelTest):
    def test_the_recipe_joins_the_list_and_the_file_keeps_its_comments(self):
        self.ws.recipes_path.write_text("# my recipes\n" + RECIPES + "# the end\n", encoding="utf-8")
        table = TableBuilder(self.draft())
        table.name = "Visits"
        table.set_from_table("EncounterFact")
        ok, message = save_recipe(self.ws.recipes_path, table.build())
        self.assertTrue(ok, message)
        text = self.ws.recipes_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# my recipes\n"))
        self.assertIn("Visits", [r["name"] for r in my.load_yaml(self.ws.recipes_path)["recipes"]])
        self.assertIn("Visits", self.ws.fact_recipes())

    def test_a_name_already_there_is_refused_and_the_file_unchanged(self):
        before = self.ws.recipes_path.read_text(encoding="utf-8")
        ok, message = save_recipe(self.ws.recipes_path, {"name": "Codes", "columns": []})
        self.assertFalse(ok)
        self.assertIn("already", message)
        self.assertEqual(self.ws.recipes_path.read_text(encoding="utf-8"), before)

    def test_without_a_recipes_file_it_says_where_recipes_live(self):
        ok, message = save_recipe(self.home / "none.yaml", {"name": "X"})
        self.assertFalse(ok)
        self.assertIn("Mac", message)


class RowKeyTests(ModelTest):
    """D107: an uploaded PK's row key, prefilled from its file."""

    def pks(self, header: str) -> None:
        (self.home / "YAMLs" / "temp" / "pks.csv").write_text(f"{header}\n1,F\n", encoding="utf-8")

    def test_a_list_with_patientdurablekey_gets_it_as_its_row_key(self):
        self.pks("PatientDurableKey,Sex")
        draft = self.draft()
        draft.set_pk_upload("csv", "ClientPK", "pks.csv")
        self.assertEqual(draft.pk().key_columns, ["PatientDurableKey"])

    def test_a_key_given_is_kept(self):
        self.pks("PatientDurableKey,EncounterKey")
        draft = self.draft()
        draft.set_pk_upload("csv", "ClientPK", "pks.csv", "EncounterKey")
        self.assertEqual(draft.pk().key_columns, ["EncounterKey"])

    def test_a_list_without_it_is_left_for_you(self):
        self.pks("MRN,Sex")
        draft = self.draft()
        draft.set_pk_upload("csv", "ClientPK", "pks.csv")
        self.assertEqual(draft.pk().key_columns, [])
        self.assertIn("uploaded_pk_missing_keys", [m.code for m in draft.validate().of_kind("error")])

    def test_typed_columns_on_a_pending_list_prefill_it_too(self):
        draft = self.draft()
        draft.set_pk_upload("csv", "ClientPK", "later.csv", pending_transfer=True)
        draft.set_listed_columns(0, "PatientDurableKey, Sex")
        self.assertEqual(draft.pk().key_columns, ["PatientDurableKey"])


class LocationTests(ModelTest):
    """D104: Browse writes a path relative to the draft, never the machine's."""

    def test_a_file_beside_the_transfer_is_written_relative_from_the_intake(self):
        draft = self.draft()
        chosen = self.home / "IBD_meds.parquet"
        self.assertEqual(draft.location_for(chosen), "../../IBD_meds.parquet")
        self.assertEqual(draft.location_for(self.home / "YAMLs" / "temp" / "csv" / "c.csv"), "csv/c.csv")

    def test_it_reaches_the_file_it_names(self):
        draft = self.draft()
        chosen = self.home / "data" / "x.parquet"
        written = draft.location_for(chosen)
        self.assertEqual((draft.base_path().parent / written).resolve(), chosen.resolve())

    def test_another_drive_is_written_in_full(self):
        from unittest import mock
        draft = self.draft()
        with mock.patch.object(os.path, "relpath", side_effect=ValueError("different drives")):
            self.assertEqual(draft.location_for(self.home / "x.csv"), str(self.home / "x.csv"))


class VmSideTests(ModelTest):
    """D108: running from an extracted bundle, no file is pending."""

    def vm(self) -> Workspace:
        self.ws.vm_side = True
        return self.ws

    def test_a_missing_file_is_an_error_on_the_vm_and_a_warning_on_the_mac(self):
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        draft.add_supporting("csv", "Later", "csv/later.csv", pending_transfer=True)
        self.assertEqual([m.code for m in draft.validate().of_kind("pending")], ["upload_pending_transfer"])
        self.vm()
        codes = [m.code for m in draft.validate().of_kind("error")]
        self.assertIn("missing_upload_file", codes)

    def test_pending_cannot_be_set_on_the_vm(self):
        self.vm()
        draft = self.draft()
        with self.assertRaises(DraftError):
            draft.add_supporting("csv", "Later", "csv/later.csv", pending_transfer=True)
        with self.assertRaises(DraftError):
            draft.set_pk_upload("csv", "ClientPK", "pks.csv", "PatientDurableKey", pending_transfer=True)

    def test_an_extracted_bundle_is_the_vm_side(self):
        from unittest import mock
        with mock.patch.object(my, "project_root", return_value=self.home):
            self.assertFalse(Workspace.default().vm_side)
            (self.home / ".bundle-manifest.json").write_text("{}", encoding="utf-8")
            self.assertTrue(Workspace.default().vm_side)


class VmFlowTests(ModelTest):
    """D103: on the VM a transfer YAML at the root, whose uploads sit beside it,
    is opened, saved as an intake in YAMLs/temp/ and exported again."""

    def test_uploads_beside_a_transfer_still_resolve_after_save_and_export(self):
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        self.assertTrue(draft.save().ok)
        ok, message, transfer = draft.export_transfer()
        self.assertTrue(ok, message)
        # The file sits beside the transfer, as on the VM.
        (self.home / "csv").mkdir(exist_ok=True)
        (self.home / "csv" / "codes.csv").write_text("Code,Label\nK50,x\n", encoding="utf-8")
        (self.home / "YAMLs" / "temp" / "csv" / "codes.csv").unlink()
        (self.home / "YAMLs" / "temp" / "Test_Run_intake.yaml").unlink()
        vm = Draft.open(Workspace(self.home, self.home / "none.yaml", self.ws.dictionary_path,
                                  self.ws.defaults_path), transfer)
        self.assertEqual(vm.doc["upload_cohorts"][0]["file_loc"], "csv/codes.csv")
        vm.project_db = "PROJECTD2"
        self.assertTrue(vm.save().ok)
        self.assertEqual(vm.doc["upload_cohorts"][0]["file_loc"], "../../csv/codes.csv")
        self.assertNotIn("missing_upload_file", [m.code for m in vm.validate().messages])
        ok, message, again = vm.export_transfer()
        self.assertTrue(ok, message)
        written = my.load_yaml(again)["upload_cohorts"][0]["file_loc"]
        self.assertEqual(written, "csv/codes.csv")
        self.assertTrue((again.parent / written).is_file())

    def test_a_quoted_csv_header_lands_without_its_quotes_by_itself(self):
        # D159: the VM's HospitalICDCodes.csv header 'DiagnosisCode', quotes and
        # all, stopped a recipe finding DiagnosisCode until renamed by hand.
        self.codes_csv(header="'DiagnosisCode',Label")
        draft = self.draft()
        index = draft.add_supporting("csv", "Codes", "csv/codes.csv")
        self.assertEqual(draft.doc["upload_cohorts"][index]["columns"],
                         [{"name": "DiagnosisCode", "from": "'DiagnosisCode'"}])
        self.assertEqual([(r.source, r.name) for r in draft.column_rows(index)],
                         [("'DiagnosisCode'", "DiagnosisCode"), ("Label", "Label")])
        self.assertNotIn("quoted_column_name", [m.code for m in draft.validate().messages])

    def test_a_rename_back_to_the_quoted_name_is_kept(self):
        self.codes_csv(header="'DiagnosisCode',Label")
        draft = self.draft()
        index = draft.add_supporting("csv", "Codes", "csv/codes.csv")
        draft.rename_column(index, "'DiagnosisCode'", "'DiagnosisCode'")
        draft.update_supporting(index, name="Codes2")
        self.assertEqual(draft.column_rows(index)[0].name, "'DiagnosisCode'")

    def test_a_quoted_csv_header_keeps_its_quotes_through_a_save(self):
        # The VM's HospitalICDCodes.csv has the header 'DiagnosisCode', quotes
        # and all: renamed, the save lost them and the split refused it.
        self.codes_csv(header="'DiagnosisCode',Label")
        draft = self.draft()
        index = draft.add_supporting("csv", "Codes", "csv/codes.csv")
        draft.rename_column(index, "'DiagnosisCode'", "DiagnosisCode")
        self.assertTrue(draft.save().ok)
        again = Draft.open(self.ws, draft.path)
        self.assertEqual(again.doc["upload_cohorts"][0]["columns"],
                         [{"name": "DiagnosisCode", "from": "'DiagnosisCode'"}])
        self.assertNotIn("unknown_upload_column", [m.code for m in again.validate().messages])


class FilterLineTests(ModelTest):
    """D105: where lines by column, and extra lines on a prefabricated table."""

    def test_a_where_line_is_written_by_column(self):
        self.assertEqual(where_line("e.Dept", "=", "Cardiology"), "e.Dept = 'Cardiology'")
        self.assertEqual(where_line("e.Key", "=", "12"), "e.Key = 12")
        self.assertEqual(where_line("e.Key", "<>", "12"), "e.Key <> 12")
        self.assertEqual(where_line("e.Key", ">=", "{{min_date_key}}"), "e.Key >= {{min_date_key}}")
        self.assertEqual(where_line("e.Dept", "IN", "A, B"), "e.Dept IN ('A', 'B')")
        self.assertEqual(where_line("dt.Value", "LIKE", "K50.%"), "dt.Value LIKE 'K50.%'")
        self.assertEqual(where_line("e.Name", "=", "O'Brien"), "e.Name = 'O''Brien'")

    def test_between_writes_its_two_values_unquoted_when_they_are_variables(self):
        # Infant_RSV: Value mode wrote `vf.DateKey = 'BETWEEN {{min_date_key}} AND {{max_date_key}}'`.
        self.assertEqual(where_line("vf.DateKey", "BETWEEN", "{{min_date_key}}", higher="{{max_date_key}}"),
                         "vf.DateKey BETWEEN {{min_date_key}} AND {{max_date_key}}")
        self.assertEqual(where_line("e.Name", "BETWEEN", "A", higher="M"), "e.Name BETWEEN 'A' AND 'M'")
        with self.assertRaises(DraftError):
            where_line("vf.DateKey", "BETWEEN", "{{min_date_key}}")

    def test_a_pattern_or_a_list_needs_its_own_operator(self):
        with self.assertRaises(DraftError) as caught:
            where_line("dt.Value", "=", "K50.%")
        self.assertIn("LIKE", str(caught.exception))
        with self.assertRaises(DraftError) as caught:
            where_line("e.Dept", "=", "A, B")
        self.assertIn("IN", str(caught.exception))
        self.assertEqual(where_line("m.MedicationKey", "in_table", table="MedCodes", column="Code"),
                         "m.MedicationKey IN (SELECT [Code] FROM {{prefix}}_MedCodes)")
        with self.assertRaises(DraftError):
            where_line("m.Key", "in_table", table="MedCodes")

    def test_a_prefabricated_tables_lines_reach_the_transfer_after_its_own(self):
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        sources = dict((name, source) for name, source, _ in draft.filter_sources(1))
        self.assertEqual(sources["EncounterKey"], "e.EncounterKey")
        self.assertEqual(draft.supporting_columns(), {"Codes": ["Code", "Label"]})
        draft.add_where_by_column(1, "e.EncounterKey", "in_table", table="Codes", column="Code")
        self.assertEqual(draft.doc["cohorts"][1]["filter"],
                         {"add_where": ["e.EncounterKey IN (SELECT [Code] FROM {{prefix}}_Codes)"]})
        self.assertTrue(draft.save().ok)
        ok, message, transfer = draft.export_transfer()
        self.assertTrue(ok, message)
        cohort = next(c for c in my.load_yaml(transfer)["cohorts"] if c["name"] == "Codes")
        self.assertEqual(cohort["filter"]["where"][-1], "e.EncounterKey IN (SELECT [Code] FROM {{prefix}}_Codes)")
        self.assertEqual(len(cohort["filter"]["where"]), 2)  # the recipe's own is kept

    def test_a_recipe_tables_text_is_imported_and_written_only_when_changed(self):
        # D121: shown in full, kept, cleared or added to; unchanged, nothing of its own.
        draft = self.codes_draft()
        imported = draft.table_text(1, "description")
        draft.set_table_text(1, "description", imported)
        self.assertNotIn("description", draft.doc["cohorts"][1])
        draft.set_table_text(1, "description", imported + " Only 2020 on.")
        self.assertEqual(draft.doc["cohorts"][1]["description"], f"{imported} Only 2020 on.".strip())
        self.assertEqual(draft.table_text(1, "description"), f"{imported} Only 2020 on.".strip())
        draft.set_table_text(1, "description", imported)
        self.assertNotIn("description", draft.doc["cohorts"][1])

    def test_a_duplicate_is_a_copy_with_its_own_name_and_destination(self):
        draft = self.codes_draft()
        draft.add_where_by_column(1, "e.EncounterKey", "=", "5")
        at = draft.duplicate_table(1)
        original, copied = draft.doc["cohorts"][1], draft.doc["cohorts"][at]
        self.assertEqual(copied["name"], f"{original['name']}_copy")
        self.assertEqual(copied["dest_table"], copied["name"])
        self.assertEqual(copied["filter"], original["filter"])
        copied["filter"]["add_where"].append("e.EncounterKey > 0")
        self.assertEqual(len(original["filter"]["add_where"]), 1)  # a copy, not the same lines
        self.assertEqual(draft.doc["cohorts"][draft.duplicate_table(1)]["name"], f"{original['name']}_copy2")

    def test_removing_the_last_line_leaves_no_filter_behind(self):
        draft = self.codes_draft()
        draft.add_where_by_column(1, "e.EncounterKey", "=", "5")
        draft.remove_line(1, "where", 0)
        self.assertNotIn("filter", draft.doc["cohorts"][1])

    def test_a_join_is_refused_unless_the_types_match(self):
        draft = self.codes_draft()
        with self.assertRaises(DraftError) as caught:
            draft.add_join_to(1, "e.EncounterKey", "Codes", "Code")
        self.assertIn("no declared type", str(caught.exception))
        draft.set_column_type(draft.supporting()[0][0], "Code", "BIGINT")
        draft.add_join_to(1, "e.EncounterKey", "Codes", "Code", "LEFT", "<>")
        self.assertEqual(draft.added_lines(1, "join"),
                         ["LEFT JOIN {{prefix}}_Codes AS c ON e.EncounterKey <> c.Code"])

    def test_a_built_tables_where_is_its_own(self):
        draft = self.draft()
        builder = TableBuilder(draft)
        builder.name = "Visits"
        builder.set_from_table("EncounterFact")
        builder.add_where_by_column(3, "=", "Cardiology")
        index = builder.commit()
        self.assertEqual(draft.doc["cohorts"][index]["filter"]["where"], ["ef.Department = 'Cardiology'"])
        draft.add_line(index, "where", "ef.DateKey > 0")
        self.assertEqual(len(draft.doc["cohorts"][index]["filter"]["where"]), 2)


class TableGroupModelTests(ModelTest):
    """D134: table groups edited in the app, written as makeYaml reads them."""

    def two_tables(self) -> Draft:
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        draft.duplicate_table(1)  # Codes_copy, its own destination
        return draft

    def test_a_group_is_written_as_makeyaml_reads_it_and_checks(self):
        draft = self.two_tables()
        at = draft.add_group("Meds")
        draft.add_to_group(at, "Codes")
        self.assertEqual(draft.doc["table_groups"], [{"name": "Meds", "tables": ["Codes"]}])
        self.assertEqual(draft.group_of("Codes"), "Meds")
        self.assertEqual(draft.ungrouped_tables(), ["Codes_copy"])
        check = draft.validate()
        self.assertTrue(check.ok, [m.text for m in check.messages if m.kind == "error"])

    def test_renaming_removing_and_duplicating_a_table_keep_its_group_true(self):
        draft = self.two_tables()
        at = draft.add_group("Meds")
        draft.add_to_group(at, "Codes")
        draft.rename_table(1, "Orders")
        self.assertEqual(draft.table_groups()[0][2], ["Orders"])
        draft.duplicate_table(1)
        self.assertEqual(draft.table_groups()[0][2], ["Orders", "Orders_copy"])
        draft.remove_fact_table(1)
        self.assertEqual(draft.table_groups()[0][2], ["Orders_copy"])
        self.assertTrue(draft.validate().ok)

    def test_what_is_refused(self):
        draft = self.two_tables()
        at = draft.add_group("Meds")
        draft.add_to_group(at, "Codes")
        other = draft.add_group("Visits")
        for attempt, says in (
            (lambda: draft.add_to_group(other, "Codes"), "in table group Meds"),
            (lambda: draft.add_to_group(other, "Patients"), "not a fact table"),
            (lambda: draft.add_group(""), "needs a name"),
            (lambda: draft.add_group("run"), "cannot be called run"),
            (lambda: draft.add_group("meds "), None),
            (lambda: draft.rename_group(other, "Meds"), "called Meds already"),
            (lambda: draft.remove_from_group(other, "Codes"), "not in that group"),
        ):
            if says is None:
                continue
            with self.subTest(says=says):
                with self.assertRaises(DraftError) as caught:
                    attempt()
                self.assertIn(says, str(caught.exception))

    def test_removing_the_last_group_leaves_no_list(self):
        draft = self.two_tables()
        draft.add_group("Meds")
        draft.remove_group(0)
        self.assertNotIn("table_groups", draft.doc)

    def test_a_group_message_points_at_the_groups_section(self):
        draft = self.two_tables()
        draft.doc["table_groups"] = [{"name": "Meds", "tables": ["Nope"]}]
        draft._changed()
        message = next(m for m in draft.validate().messages if m.code == "unknown_group_table")
        self.assertEqual((message.field.section, message.field.index), ("groups", 0))


class RecipeSetTests(ModelTest):
    """D135: a table group saved as a recipe set, and the set added as a group."""

    VISITS = {
        "name": "Visits", "dest_table": "Visits", "type": "fact",
        "columns": [{"source": "e.EncounterKey", "name": "EncounterKey"}],
        "filter": {"from": "EncounterFact AS e",
                   "join": ["INNER JOIN {{prefix}}_CodedVisits AS cv ON cv.EncounterKey = e.EncounterKey"]},
    }

    def grouped(self) -> Draft:
        """A prefabricated table bound to a supporting table, and a built one reading it."""
        draft = self.codes_draft()
        draft.bind(1, "CodesTable", "Codes")
        draft.add_fact_table(self.VISITS)
        at = draft.add_group("Visit block")
        for table in ("Codes", "Visits"):
            draft.add_to_group(at, table)
        return draft

    def fresh(self) -> Draft:
        draft = self.draft()
        draft.set_pk_recipe("Patients")
        draft.set_var(0, "ICD_Value", "K50%")
        draft.add_supporting("csv", "Codes", "csv/codes.csv")
        return draft

    def test_a_saved_set_comes_back_as_the_same_tables_in_a_group(self):
        path = self.ws.recipes_path
        path.write_text("# kept\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        draft = self.grouped()
        self.assertTrue(draft.validate().ok)
        ok, said = save_recipe_set(path, draft, 0)
        self.assertTrue(ok, said)
        self.assertIn("with Visits as recipes", said)
        written = my.load_yaml(path)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# kept\n"))
        self.assertIn("Visits", [r["name"] for r in written["recipes"]])
        self.assertEqual(written["recipe_sets"][0]["name"], "Visit block")

        again = self.fresh()
        added = again.add_recipe_set("Visit block")
        cohorts = [again.doc["cohorts"][i] for i in added]
        self.assertEqual([(c["recipe"], c["name"]) for c in cohorts], [("Codes", "Codes"), ("Visits", "Visits")])
        self.assertEqual(cohorts[0]["vars"], {"CodesTable": "Codes"})
        self.assertEqual(again.doc["table_groups"], [{"name": "Visit block", "tables": ["Codes", "Visits"]}])
        check = again.validate()
        self.assertTrue(check.ok, [m.text for m in check.messages if m.kind == "error"])

    def test_adding_a_set_is_all_or_nothing(self):
        draft = self.grouped()
        save_recipe_set(self.ws.recipes_path, draft, 0)
        again = self.fresh()
        again.add_prefab("Codes")
        before = copy.deepcopy(again.doc)
        with self.assertRaises(DraftError) as caught:
            again.add_recipe_set("Visit block")
        self.assertIn("Codes is a table here already", str(caught.exception))
        self.assertEqual(again.doc, before)
        with self.assertRaises(DraftError):
            again.add_recipe_set("Nope")

    def test_a_refused_save_leaves_the_file_as_it_was(self):
        path = self.ws.recipes_path
        draft = self.grouped()
        self.assertTrue(save_recipe_set(path, draft, 0)[0])
        before = path.read_text(encoding="utf-8")
        ok, said = save_recipe_set(path, draft, 0)
        self.assertFalse(ok)
        self.assertIn("A recipe set named Visit block is already", said)
        empty = self.codes_draft()
        empty.add_group("Nothing")
        ok, said = save_recipe_set(path, empty, 0)
        self.assertFalse(ok)
        self.assertIn("has no tables", said)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_set_naming_a_recipe_the_file_lacks_is_refused(self):
        path = self.ws.recipes_path
        path.write_text(path.read_text(encoding="utf-8") + "recipe_sets:\n  - name: Broken\n    tables:\n"
                        "      - {recipe: Missing}\n", encoding="utf-8")
        with self.assertRaises(DraftError) as caught:
            self.fresh().add_recipe_set("Broken")
        self.assertIn("names recipe Missing", str(caught.exception))


def run_tdd(verbosity: int = 2) -> int:
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for case in (ProjectTests, PkTests, SupportingTests, SplitterTests, FactTableTests, MessageTests,
                 SaveTests, PastedCsvTests, TableBuilderTests, SaveRecipeTests, VmFlowTests, VmSideTests,
                 LocationTests, FilterLineTests, RowKeyTests, TableGroupModelTests,
                 RecipeSetTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--tdd" in sys.argv[1:]:
        raise SystemExit(run_tdd())
    print(__doc__)
