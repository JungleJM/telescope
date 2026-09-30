"""YAML load/dump for Pullmanager.

Mirrors the backend selection in scripts/makeYaml.py so manifests round-trip
through the same representation YAML Manager wrote them with.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

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


# A refused rename is tried again: quickly first, since most holds last
# milliseconds, then once a minute (D153).
QUICK_WAITS = (0.1, 0.25, 0.5, 1.0, 2.0)
MINUTE_WAITS = 5
MINUTE = 60.0


class FileBusy(PermissionError):
    """A file could not be replaced: something kept it open (D153)."""


def replace_patiently(source: str | Path, target: str | Path, *,
                      sleep: Callable[[float], None] | None = None,
                      say: Callable[[str], None] | None = None) -> None:
    """`os.replace`, waiting while Windows refuses it because the target is open.

    Trying is the check: Windows cannot say who holds a file on a share.
    """
    target = Path(target)
    sleep = sleep or time.sleep
    say = say or (lambda line: print(line, flush=True))
    for wait in QUICK_WAITS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            sleep(wait)
    for attempt in range(1, MINUTE_WAITS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            say(f"  {target.name} is busy ({exc.strerror or exc}); trying again in "
                f"{MINUTE:g}s, {attempt} of {MINUTE_WAITS}")
            sleep(MINUTE)
    try:
        os.replace(source, target)
    except PermissionError as exc:
        raise FileBusy(
            exc.errno, f"{target} could not be replaced for {MINUTE_WAITS} minutes: another "
            "program has it open. Usually the Run window showing this pull, antivirus, or an "
            "editor. Close it, then Execute again: what was running is pulled again.",
            str(target),
        ) from exc
