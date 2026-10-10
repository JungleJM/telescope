"""The app: Author and Run in one window, on the Mac and the VM (D93).

Author is YAML Manager's tkinter view (scripts/yamlmanager_tk.py, over its
model); Run is the launcher (gui.py). Author saves an intake and exports its
blueprint, which Run then takes (D94). Both are found from this
package's location, as the launcher finds makeYaml.

    python scope.py                # no arguments: this window (D123)
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from . import launcher
from .gui import LauncherApp, add_credit
from .launcher import LauncherError

TITLE = "Scope"


def load_utilities() -> Any:
    """utilities.py, beside this package, as `utils.py` runs it (D124, D194)."""
    path = Path(__file__).resolve().parents[1] / "utilities.py"
    spec = importlib.util.spec_from_file_location("utilities", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"No utilities at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_projects(names: list[str]) -> list[tuple[str, str]]:
    """Each Projects database's room, measured as Execute measures it (D164, D218)."""
    from .databases import measure
    from .db import DatabaseError, Settings, connect, load_env_file

    try:
        load_env_file(None)
    except DatabaseError:
        pass
    return [(room.database, room.text()) for room in measure(names, Settings.from_env(), connect)]


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
        self.utils_frame = ttk.Frame(self.halves, padding=12)
        self.halves.add(self.author_frame, text="Author")
        self.halves.add(self.run_frame, text="Run")
        self.halves.add(self.utils_frame, text="Utils")
        try:
            # The utilities window's own buttons, each opening its window (D194).
            load_utilities().fill(self.utils_frame)
        except Exception as exc:  # noqa: BLE001 - Author and Run work without it
            ttk.Label(self.utils_frame, wraplength=700, justify="left",
                      text=f"The utilities could not be listed: {type(exc).__name__}: {exc}\n\n"
                           "python utils.py opens them in a window of their own.").pack(anchor="nw")

        self.author_title = ""
        self.run_title = ""
        self.run = LauncherApp(root, tools, workdir, parent=self.run_frame)
        self.run.on_loaded = self.run_loaded
        self.run.show_loaded()
        self.author = None
        try:
            author_module = load_author(tools)
            workspace = author_module.model.Workspace.default()
            from . import config
            self.author = author_module.AuthorView(
                self.author_frame, root, workspace,
                on_open_in_run=self.take_blueprint, on_title=self.set_title,
                project_databases=list(config.DEFAULT_PROJECTS_DATABASES), measure_databases=measure_projects,
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
        """Author's project, as the title names it (D166)."""
        self.author_title = text
        self.show_title()

    def run_loaded(self, project: str) -> None:
        self.run_title = project
        self.show_title()

    def show_title(self) -> None:
        """`Scope · Author: <project> · Run: <pull>` (D166)."""
        parts = [TITLE]
        if self.author_title:
            parts.append(f"Author: {self.author_title}")
        if self.run_title:
            parts.append(f"Run: {self.run_title}")
        self.root.title(" · ".join(parts))

    def take_blueprint(self, path: Path) -> None:
        """Author exported a blueprint: Run takes it, and is shown."""
        self.run.use_blueprint(path)
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
