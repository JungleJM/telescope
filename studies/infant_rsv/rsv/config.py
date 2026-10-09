"""Settings, folders, and which file each table is read from."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
SETTINGS_PATH = PACKAGE_DIR / "settings.yaml"
SP_SUFFIX = "_sp"
SP_ANALYSIS = "analysis_sneakpeek"


class RsvError(Exception):
    """A problem the user can fix; the message says how."""


@dataclass
class Settings:
    values: dict[str, Any]
    root: Path = ROOT
    sp: bool = False
    path: Path = SETTINGS_PATH

    def __getitem__(self, key: str) -> Any:
        if key not in self.values:
            raise RsvError(f"{self.path} has no `{key}`. Add it, or copy it from a fresh settings.yaml.")
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def folder(self, key: str) -> Path:
        return (self.root / self[key]).resolve()

    @property
    def analysis_folder(self) -> Path:
        folder = self.folder("analysis_folder")
        return folder.parent / SP_ANALYSIS if self.sp else folder

    @property
    def pages_folder(self) -> Path:
        return self.analysis_folder / "pages"


def load_settings(path: Path | None = None, root: Path | None = None, sp: bool = False) -> Settings:
    path = path or SETTINGS_PATH
    if not path.is_file():
        raise RsvError(f"No settings file at {path}.")
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RsvError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(values, dict):
        raise RsvError(f"{path} must be a mapping of settings.")
    return Settings(values=values, root=root or ROOT, sp=sp, path=path)


@dataclass
class Source:
    """Where a table was read from, or why it was not."""
    table: str
    path: Path | None = None
    rows: int = 0
    tried: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.path is not None


def candidate_path(settings: Settings, spec: str) -> Path:
    if ":" not in spec:
        raise RsvError(f"Source `{spec}` in {settings.path} must read `pull:Name` or `followup:Name`.")
    which, name = spec.split(":", 1)
    folder_key = {"pull": "pull_folder", "followup": "followup_folder", "icu": "icu_folder"}.get(which.strip())
    if folder_key is None:
        raise RsvError(f"Source `{spec}` in {settings.path}: use `pull:`, `followup:` or `icu:`.")
    name = name.strip()
    if settings.sp:
        return settings.folder(folder_key) / "sneakpeek_parquets" / f"{name}{SP_SUFFIX}.parquet"
    return settings.folder(folder_key) / "cosmos_parquets" / f"{name}.parquet"


def parquet_rows(path: Path) -> int:
    import pyarrow.parquet as pq
    return pq.ParquetFile(path).metadata.num_rows


def find_source(settings: Settings, table: str) -> Source:
    """The first of a table's files that exists and has rows (settings `sources`)."""
    specs = settings["sources"].get(table)
    if not specs:
        raise RsvError(f"{settings.path} lists no files for `{table}` under `sources`.")
    source = Source(table)
    for spec in specs:
        path = candidate_path(settings, spec)
        if not path.is_file():
            source.tried.append(f"{spec}: none")
            continue
        rows = parquet_rows(path)
        if rows == 0:
            source.tried.append(f"{spec}: 0 rows")
            continue
        source.path, source.rows = path, rows
        source.tried.append(f"{spec}: {rows:,} rows")
        break
    return source
