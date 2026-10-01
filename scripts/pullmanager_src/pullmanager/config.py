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
# `backup`: the folder Artifacts backs each pull up to (D149), set in Run.
# `projects_databases`: the Projects databases a pull may be given (D164).
CONFIG_KEYS = (*CORE_DEFAULTS, "runs", "backup", "projects_databases")
# Without `projects_databases`, the user's own that their login opens (D170,
# 1 October 2026); the first is the default. PROJECTD52274F is a training
# database, never to be used. A folder `Project D<code>` is `PROJECTD<code>`.
DEFAULT_PROJECTS_DATABASES = (
    "PROJECTD93A5E7", "PROJECTD33A929", "PROJECTD52219B", "PROJECTD125423", "PROJECTD139081",
    "PROJECTD338331",
)


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


def projects_databases(home: Path) -> list[str]:
    """The Projects databases datascope.json lists (D164), a list or one
    comma-separated string; else the defaults."""
    path = Path(home) / CONFIG_NAME
    read(home)  # refuses a broken or unknown file, as every reader does
    if not path.is_file():
        return list(DEFAULT_PROJECTS_DATABASES)
    value = json.loads(path.read_text(encoding="utf-8")).get("projects_databases")
    if value is None:
        return list(DEFAULT_PROJECTS_DATABASES)
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ConfigError(f"{path}: projects_databases must be a list of database names, "
                          'e.g. ["PROJECTD93A5E7", "PROJECTD33A929"].')
    names = [i.strip() for i in items if i.strip()]
    if not names:
        raise ConfigError(f"{path}: projects_databases lists no database. Name at least one, "
                          "or remove it to use the defaults.")
    return names


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


def backup_dir(home: Path) -> Path | None:
    """The backup folder datascope.json names (D149), from `home` if it is
    written relative; None where none is set."""
    value = read(home).get("backup", "").strip()
    return (Path(home) / value) if value else None


def set_backup(home: Path, folder: Path | None) -> Path:
    """Name the backup folder in `home/datascope.json`, or clear it, keeping
    whatever else the file says."""
    read(home)  # refuses a broken or unknown file
    path = Path(home) / CONFIG_NAME
    # As written, so a list (projects_databases) stays a list.
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if folder:
        data["backup"] = str(folder)
    else:
        data.pop("backup", None)
    path = Path(home) / CONFIG_NAME
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def local_backup_dir(home: Path) -> Path:
    """Where a backup goes when the backup folder can't be reached or none is
    set: `backup/` in the runs folder (D149)."""
    return runs_dir(home) / "backup"

