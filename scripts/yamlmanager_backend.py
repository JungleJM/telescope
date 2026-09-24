"""
Stable Python API for YAML Manager frontends.

Frontends should import this module instead of reaching into makeYaml directly.
That keeps CLI/UI/Desktop naming independent from the compiler implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import makeYaml


CompileResult = makeYaml.CompileResult


def load_document(path: str | Path) -> Any:
    return makeYaml.load_yaml(path)


def compile_dashboard(
    template_path: str | Path,
    recipes_path: str | Path,
    write: bool = False,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return makeYaml.compile_yaml(
        template_path=template_path,
        recipes_path=recipes_path,
        write=write,
        datadictionary_path=datadictionary_path,
    )


def build_preyaml(
    template_path: str | Path,
    recipes_path: str | Path,
    output_path: str | Path | None = None,
    mode: str = "symbolic",
    write: bool = False,
) -> CompileResult:
    return makeYaml.build_preyaml(
        template_path=template_path,
        recipes_path=recipes_path,
        output_path=output_path,
        mode=mode,
        write=write,
    )


def build_transfer(
    template_path: str | Path,
    recipes_path: str | Path,
    output_path: str | Path | None = None,
    write: bool = False,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return makeYaml.build_transfer(
        template_path=template_path,
        recipes_path=recipes_path,
        output_path=output_path,
        write=write,
        datadictionary_path=datadictionary_path,
    )


def plan_split_runs(
    template_path: str | Path,
    recipes_path: str | Path,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return makeYaml.plan_split_runs(
        template_path=template_path,
        recipes_path=recipes_path,
        datadictionary_path=datadictionary_path,
    )


def build_pullmanifest(
    template_path: str | Path,
    recipes_path: str | Path,
    output_path: str | Path | None = None,
    write: bool = False,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return makeYaml.build_pullmanifest(
        template_path=template_path,
        recipes_path=recipes_path,
        output_path=output_path,
        write=write,
        datadictionary_path=datadictionary_path,
    )


def write_split_artifacts(
    template_path: str | Path,
    recipes_path: str | Path,
    output_dir: str | Path | None = None,
    datadictionary_path: str | Path | None = None,
) -> CompileResult:
    return makeYaml.write_split_artifacts(
        template_path=template_path,
        recipes_path=recipes_path,
        output_dir=output_dir,
        datadictionary_path=datadictionary_path,
    )


def missing_template_message(template_path: str | Path) -> str | None:
    return makeYaml.missing_template_message(Path(template_path))


def dump_yaml_text(data: Any) -> str:
    return makeYaml.dump_yaml_text(data)


def recipe_output_columns(recipe: dict[str, Any]) -> list[str]:
    return makeYaml.output_columns(recipe)


def recipe_required_vars(recipe: dict[str, Any]) -> dict[str, Any]:
    return makeYaml.infer_required_vars(recipe)


def recipe_table_inputs(recipe: dict[str, Any]) -> dict[str, list[str]]:
    return makeYaml.infer_table_inputs(recipe)
