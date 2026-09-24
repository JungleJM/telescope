"""YAML load/dump for Pullmanager.

Mirrors the backend selection in scripts/makeYaml.py so manifests round-trip
through the same representation YAML Manager wrote them with.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

NO_BACKEND = (
    "No YAML backend available in this Python ({python}). Install one: "
    "`{python} -m pip install ruamel.yaml pyyaml`."
)


def _backend():
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
        return "none", None


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def load_yaml(path: str | Path) -> Any:
    path = Path(path)
    backend, mod = _backend()
    if backend == "ruamel":
        with path.open("r", encoding="utf-8") as handle:
            return _plain(mod.load(handle))
    if backend == "pyyaml":
        with path.open("r", encoding="utf-8") as handle:
            return mod.safe_load(handle)
    raise RuntimeError(NO_BACKEND.format(python=sys.executable))


def dump_yaml(data: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backend, mod = _backend()
    if backend == "ruamel":
        with path.open("w", encoding="utf-8") as handle:
            mod.dump(data, handle)
        return
    if backend == "pyyaml":
        with path.open("w", encoding="utf-8") as handle:
            mod.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
        return
    raise RuntimeError(NO_BACKEND.format(python=sys.executable))
