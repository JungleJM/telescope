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


# utils/ sorts its scripts for whom they are (D148): client/ goes into every
# pull's folder too; manager/ stays here. A script at utils/'s top still shows.
GROUPS = (("", ""), ("Client", "client"), ("Manager", "manager"))


def _in(folder: Path) -> list[Path]:
    return sorted((path for path in folder.glob("*.py") if not path.name.startswith(("_", "."))),
                  key=lambda path: path.name.lower())


def sections(folder: Path = UTILS_DIR) -> list[tuple[str, list[Path]]]:
    """The utilities under their headings, the empty ones left out."""
    found = [(heading, _in(folder / sub if sub else folder)) for heading, sub in GROUPS]
    return [(heading, paths) for heading, paths in found if paths]


def scripts(folder: Path = UTILS_DIR) -> list[Path]:
    """Every utility, in the order the window shows them."""
    return [path for _heading, paths in sections(folder) for path in paths]


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

    found = sections(folder)
    for heading, paths in found:
        if heading:
            ttk.Label(frame, text=heading, font="TkHeadingFont").pack(anchor="w", pady=(8, 2))
        for path in paths:
            ttk.Button(frame, text=path.stem, command=lambda p=path: run(p)).pack(fill="x", pady=2)
    if not found:
        ttk.Label(frame, text=f"No scripts in {folder}.").pack(anchor="w")
    said.pack(anchor="w", pady=(8, 0))
    ttk.Label(frame, text=f"Add a .py file to {folder} and it appears here.",
              foreground="#6e7781").pack(anchor="w")
    add_credit(root)
    root.mainloop()


# Who made this window, at its foot (D145), and which bundle it is (D147).
# Packed ahead of the window's contents, so a small window squeezes them and
# never this.
CREDIT = "Designed and built by Jason Mathias"
BUNDLE = ""  # Artifacts writes the bundle here in the copy it puts in a pull's folder


def bundle_id() -> str:
    """The first 8 characters of the extracted bundle's content_id, from the
    .bundle-manifest.json above this file; empty when run from source."""
    import json
    from pathlib import Path

    for folder in Path(__file__).resolve().parents:
        manifest = folder / ".bundle-manifest.json"
        if manifest.is_file():
            try:
                return str(json.loads(manifest.read_text(encoding="utf-8"))["content_id"])[:8]
            except (OSError, ValueError, KeyError, TypeError):
                return ""
    return BUNDLE


def credit_text() -> str:
    found = bundle_id()
    return f"{CREDIT} \u00b7 bundle {found}" if found else CREDIT


def add_credit(root) -> None:
    from tkinter import ttk

    label = ttk.Label(root, text=credit_text(), foreground="#8c959f", font="TkSmallCaptionFont")
    placed = {"side": "bottom", "anchor": "e", "padx": 8, "pady": (0, 2)}
    slaves = root.pack_slaves()
    if slaves:
        placed["before"] = slaves[0]
    label.pack(**placed)


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for path in scripts():
            print(path.name)
        return 0
    window()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
