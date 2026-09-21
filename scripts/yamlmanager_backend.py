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


def compile_dashboard(template_path: str | Path, recipes_path: str | Path, write: bool = False) -> CompileResult:
    return makeYaml.compile_yaml(template_path=template_path, recipes_path=recipes_path, write=write)


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


def plan_split_runs(template_path: str | Path, recipes_path: str | Path) -> CompileResult:
    return makeYaml.plan_split_runs(template_path=template_path, recipes_path=recipes_path)


def build_pullmanifest(
    template_path: str | Path,
    recipes_path: str | Path,
    output_path: str | Path | None = None,
    write: bool = False,
) -> CompileResult:
    return makeYaml.build_pullmanifest(
        template_path=template_path,
        recipes_path=recipes_path,
        output_path=output_path,
        write=write,
    )


def dump_yaml_text(data: Any) -> str:
    return makeYaml.dump_yaml_text(data)


def recipe_output_columns(recipe: dict[str, Any]) -> list[str]:
    return makeYaml.output_columns(recipe)


def recipe_required_vars(recipe: dict[str, Any]) -> dict[str, Any]:
    return makeYaml.infer_required_vars(recipe)


def recipe_table_inputs(recipe: dict[str, Any]) -> dict[str, list[str]]:
    return makeYaml.infer_table_inputs(recipe)
