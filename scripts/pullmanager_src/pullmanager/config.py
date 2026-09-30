"""Where the core files and runs are: datascope.json (D111).

The working folder may hold a `datascope.json` naming, relative to itself,
where the recipes, data dictionary, template and VM package list are, and
where runs go. Without it: the core files beside the code (the extracted
bundle's layout) and `runs/` in the working folder. makeYaml keeps the same
defaults; a test holds the two together, since neither imports the other.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_NAME = "datascope.json"
CORE_DEFAULTS = {
    "recipes": "reference/recipes.yaml",
    "datadictionary": "reference/datadictionary.yaml",
    "template": "reference/template.yaml",
    "vm_plugins": "reference/DSVM Plugins.yaml",
}
RUNS_DEFAULT = "runs"
CONFIG_KEYS = (*CORE_DEFAULTS, "runs")


class ConfigError(RuntimeError):
    """datascope.json cannot be read, or names what it cannot mean."""


def read(home: Path) -> dict[str, str]:
    """`home/datascope.json`, or {} where there is none."""
    path = Path(home) / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} could not be read: {exc}. Fix it, or remove it to use the defaults.") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of names to paths.")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ConfigError(f"{path} names {', '.join(unknown)}, which nothing reads. "
                          f"It may set: {', '.join(CONFIG_KEYS)}.")
    return {str(k): str(v) for k, v in data.items()}


def runs_setting(home: Path) -> Path:
    """The runs folder, relative to `home`: `runs`, or what datascope.json says."""
    return Path(read(home).get("runs", RUNS_DEFAULT))


def runs_dir(home: Path) -> Path:
    return Path(home) / runs_setting(home)


def code_home(code_root: Path) -> Path:
    """The working folder for code at `code_root`: its parent in an extracted
    bundle, else itself (the repository), as makeYaml's transfer_home."""
    code_root = Path(code_root)
    return code_root.parent if (code_root / ".bundle-manifest.json").is_file() else code_root


def core_path(key: str, code_root: Path) -> Path:
    """A core file for the code at `code_root`: where datascope.json says,
    else its default beside the code."""
    home = code_home(code_root)
    config = read(home)
    if key in config:
        return home / config[key]
    return Path(code_root) / CORE_DEFAULTS[key]


def bundle_id() -> str:
    """The first 8 characters of the extracted bundle's content_id (D147), from
    the .bundle-manifest.json above this file; empty when run from source."""
    for folder in Path(__file__).resolve().parents:
        manifest = folder / ".bundle-manifest.json"
        if manifest.is_file():
            try:
                return str(json.loads(manifest.read_text(encoding="utf-8"))["content_id"])[:8]
            except (OSError, ValueError, KeyError, TypeError):
                return ""
    return ""

