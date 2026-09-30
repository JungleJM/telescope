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

WINDOWS = os.name == "nt"

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
    return parse_yaml(read_shared(path))


def parse_yaml(text: str) -> Any:
    backend, mod = _backend()
    if backend == "ruamel":
        return _plain(mod.load(text))
    if backend == "pyyaml":
        return mod.safe_load(text)
    raise RuntimeError(NO_BACKEND.format(python=sys.executable))


def read_shared(path: str | Path) -> str:
    """A text file's contents, read without stopping another process replacing it.

    An ordinary open on Windows forbids replacing the file until it is closed,
    which made an executing pull's manifest saves fail while the Run window
    read it (D154). On Windows the file is opened with FILE_SHARE_DELETE;
    elsewhere, and if that is unavailable, plainly.
    """
    path = Path(path)
    if WINDOWS:
        handle = _open_shared_windows(path)
        if handle is not None:
            import msvcrt

            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
            with open(descriptor, "r", encoding="utf-8") as stream:
                return stream.read()
    return path.read_text(encoding="utf-8")


def file_signature(path: str | Path) -> tuple[int, int] | None:
    """A file's modified time and size, to tell whether it changed; None if absent.

    A stat asks only for attributes, which no sharing mode blocks, so it never
    stands in the way of a save.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _open_shared_windows(path: Path) -> int | None:
    """A Win32 handle opened for reading with every sharing mode, or None."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    handle = create(str(path), generic_read, share_all, None, open_existing, normal, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # FileNotFoundError and the like
    return handle


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
