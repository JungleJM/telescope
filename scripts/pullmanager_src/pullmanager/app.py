"""The app: Author and Run in one window, on the Mac and the VM (D93).

Author is YAML Manager's tkinter view (scripts/yamlmanager_tk.py, over its
model); Run is the launcher (gui.py). Author saves an intake and exports its
transfer YAML, which Run then takes (D94). Both are found from this
package's location, as the launcher finds makeYaml.

    python scope.py                # no arguments: this window (D123)
"""

from __future__ import annotations

import importlib
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from . import launcher
from .gui import LauncherApp, add_credit
from .launcher import LauncherError

TITLE = "Telescope"


def load_author(tools: launcher.Tools) -> Any:
    """yamlmanager_tk, beside makeYaml; an ImportError says what is missing."""
    folder = str(Path(tools.make_yaml).parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return importlib.import_module("yamlmanager_tk")


class App:
    def __init__(self, root: Any, tools: launcher.Tools, workdir: Path):
        self.root = root
        self.workdir = workdir
        root.title(f"{TITLE} - {workdir}")
        root.geometry("1280x860")
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.halves = ttk.Notebook(root)
        self.halves.pack(fill="both", expand=True)
        self.author_frame = ttk.Frame(self.halves)
        self.run_frame = ttk.Frame(self.halves)
        self.halves.add(self.author_frame, text="Author")
        self.halves.add(self.run_frame, text="Run")

        self.run = LauncherApp(root, tools, workdir, parent=self.run_frame)
        self.author = None
        try:
            author_module = load_author(tools)
            workspace = author_module.model.Workspace.default()
            self.author = author_module.AuthorView(
                self.author_frame, root, workspace,
                on_transfer=self.take_transfer, on_title=self.set_title,
            )
        except Exception as exc:  # noqa: BLE001 - Run still works without Author
            ttk.Label(
                self.author_frame, padding=20, wraplength=700, justify="left",
                text=f"Author could not open: {type(exc).__name__}: {exc}\n\n"
                     "Run, in the other tab, works without it.",
            ).pack(anchor="nw")
            self.halves.select(self.run_frame)
        add_credit(root)

    def set_title(self, text: str) -> None:
        self.root.title(f"{TITLE} - {text}")

    def take_transfer(self, path: Path) -> None:
        """Author exported a transfer YAML: Run takes it, and is shown."""
        self.run.use_transfer(path)
        self.halves.select(self.run_frame)

    def on_close(self) -> None:
        if self.author is not None and not self.author.close():
            return
        if self.run.close():
            self.root.destroy()


def main(workdir: Path | None = None) -> int:
    workdir = Path(workdir or Path.cwd())
    root = tk.Tk()
    try:
        tools = launcher.locate_tools()
    except LauncherError as exc:
        root.withdraw()
        messagebox.showerror(TITLE, str(exc))
        return 1
    App(root, tools, workdir)
    root.mainloop()
    return 0
