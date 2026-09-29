#!/usr/bin/env python3
"""The utilities window (D124): a button for each script in `utils/`.

    python utils.py            # beside scope.py on the VM; at the root on the Mac
    python utils.py --list     # the scripts it offers, without a window

Each script runs as a process of its own, from the folder utils.py was started
in, so the window stays usable and a script that fails takes nothing with it.
Put a .py file in utils/ and it appears here; one whose name starts with _ does
not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

UTILS_DIR = Path(__file__).resolve().parent / "utils"


def scripts(folder: Path = UTILS_DIR) -> list[Path]:
    """The utilities, by name."""
    return sorted((path for path in folder.glob("*.py") if not path.name.startswith(("_", "."))),
                  key=lambda path: path.name.lower())


def start(path: Path, cwd: Path | None = None) -> subprocess.Popen:
    """Run one utility with this Python, in its own process."""
    return subprocess.Popen([sys.executable, str(path)], cwd=str(cwd or Path.cwd()))


def window(folder: Path = UTILS_DIR) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Utilities")
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    said = ttk.Label(frame, text="", foreground="#6e7781")

    def run(path: Path) -> None:
        try:
            start(path)
        except OSError as exc:
            said.configure(text=f"{path.name} did not start: {exc}")
        else:
            said.configure(text=f"Started {path.name}; it opens in its own window.")

    found = scripts(folder)
    for path in found:
        ttk.Button(frame, text=path.stem, command=lambda p=path: run(p)).pack(fill="x", pady=2)
    if not found:
        ttk.Label(frame, text=f"No scripts in {folder}.").pack(anchor="w")
    said.pack(anchor="w", pady=(8, 0))
    ttk.Label(frame, text=f"Add a .py file to {folder} and it appears here.",
              foreground="#6e7781").pack(anchor="w")
    root.mainloop()


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for path in scripts():
            print(path.name)
        return 0
    window()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
