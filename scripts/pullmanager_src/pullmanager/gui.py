"""Desktop launcher for running pulls.

A thin tkinter view over launcher.py. It holds no logic of its own: every
button builds a command through the controller and runs it as a subprocess,
exactly as it would be typed. Anything worth testing lives in launcher.py.

Run with:  python pullmanager.py --gui
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import launcher
from .launcher import LauncherError, Options, Paths

POLL_MS = 100
STATUS_REFRESH_MS = 3000

STATUS_COLOURS = {
    "done": "#1a7f37",
    "failed": "#cf222e",
    "running": "#0969da",
    "blocked": "#6e7781",
    "skipped": "#6e7781",
    "pending": "#24292f",
}

FIELDS = (
    # attribute, label, kind, hint
    ("template", "Template", "file", "required"),
    ("recipes", "Recipes", "file", "blank = bundled copy"),
    ("datadictionary", "Data dictionary", "file", "blank = bundled copy"),
    ("split_dir", "Split folder", "dir", "written by Export split"),
    ("sql_dir", "SQL folder", "dir", "written by Dry run"),
)


class LauncherApp:
    def __init__(self, root: tk.Tk, tools: launcher.Tools, workdir: Path):
        self.root = root
        self.tools = tools
        self.workdir = workdir
        self.runner = launcher.CommandRunner()
        self.vars: dict[str, tk.StringVar] = {}
        self.retry_failed = tk.BooleanVar(value=False)
        self.action_buttons: list[ttk.Button] = []
        self._next_status_refresh = 0

        root.title(f"Pullmanager - {workdir}")
        root.geometry("1100x760")
        root.minsize(820, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_inputs()
        self._build_tabs()
        self._build_status_bar()
        self._load_settings()
        self.refresh_status()

    # ------------------------------------------------------------- layout

    def _build_inputs(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Pull inputs", padding=8)
        frame.pack(fill="x", padx=10, pady=(10, 4))
        frame.columnconfigure(1, weight=1)

        for row, (attr, label, kind, hint) in enumerate(FIELDS):
            var = tk.StringVar()
            self.vars[attr] = var
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Button(
                frame, text="Browse", command=lambda a=attr, k=kind: self.browse(a, k)
            ).grid(row=row, column=2, padx=(6, 6), pady=2)
            ttk.Label(frame, text=hint, foreground="#6e7781").grid(row=row, column=3, sticky="w")

        options = ttk.Frame(frame)
        options.grid(row=len(FIELDS), column=0, columnspan=4, sticky="w", pady=(8, 4))
        ttk.Checkbutton(options, text="Retry failed", variable=self.retry_failed).pack(side="left")

        actions = ttk.Frame(frame)
        actions.grid(row=len(FIELDS) + 1, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        for text, handler in (
            ("Validate", self.on_validate),
            ("Export split", self.on_export_split),
            ("Dry run", self.on_dry_run),
            ("Execute", self.on_execute),
        ):
            button = ttk.Button(actions, text=text, command=handler)
            button.pack(side="left", padx=(0, 6))
            self.action_buttons.append(button)
        self.stop_button = ttk.Button(actions, text="Stop", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="right")

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=4)

        output_tab = ttk.Frame(notebook)
        self.output = scrolledtext.ScrolledText(
            output_tab, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.output.pack(fill="both", expand=True)
        notebook.add(output_tab, text="Output")

        status_tab = ttk.Frame(notebook)
        columns = ("kind", "name", "status", "rows", "duration", "detail")
        self.tree = ttk.Treeview(status_tab, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Session")
        self.tree.column("#0", width=220)
        widths = {"kind": 70, "name": 150, "status": 80, "rows": 90, "duration": 80, "detail": 360}
        for column in columns:
            self.tree.heading(column, text=column.capitalize())
            self.tree.column(column, width=widths[column], anchor="w")
        for status, colour in STATUS_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)
        scroll = ttk.Scrollbar(status_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bar = ttk.Frame(status_tab)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh_status).pack(side="right", pady=4)
        self.status_message = ttk.Label(bar, text="", foreground="#6e7781")
        self.status_message.pack(side="left", pady=4)
        notebook.add(status_tab, text="Status")

    def _build_status_bar(self) -> None:
        self.bar = ttk.Label(self.root, text="Ready.", anchor="w", padding=(10, 4))
        self.bar.pack(fill="x", side="bottom")

    # ------------------------------------------------------------ settings

    def paths(self) -> Paths:
        return Paths(**{attr: self.vars[attr].get() for attr, *_ in FIELDS})

    def options(self) -> Options:
        return Options(retry_failed=bool(self.retry_failed.get()))

    def _load_settings(self) -> None:
        saved = launcher.load_settings(self.workdir)
        for attr, *_ in FIELDS:
            self.vars[attr].set(getattr(saved, attr))

    def _save_settings(self) -> None:
        try:
            launcher.save_settings(self.paths(), self.workdir)
        except OSError:
            pass  # remembering choices is a convenience, not a requirement

    def browse(self, attr: str, kind: str) -> None:
        start = self.vars[attr].get() or str(self.workdir)
        if kind == "dir":
            chosen = filedialog.askdirectory(initialdir=start)
        else:
            chosen = filedialog.askopenfilename(
                initialdir=str(Path(start).parent) if Path(start).suffix else start,
                filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")],
            )
        if chosen:
            self.vars[attr].set(chosen)

    # ------------------------------------------------------------- actions

    def on_validate(self) -> None:
        self.run("Validate", lambda: launcher.command_validate(self.tools, self.paths()))

    def on_export_split(self) -> None:
        self.run("Export split", lambda: launcher.command_export_split(self.tools, self.paths()))

    def on_dry_run(self) -> None:
        self.run(
            "Dry run",
            lambda: launcher.command_dry_run(self.tools, self.paths(), self.options()),
        )

    def on_execute(self) -> None:
        if not messagebox.askokcancel(
            "Execute pull",
            "This runs against Cosmos and Projects and updates the manifest.\n\nContinue?",
        ):
            return
        self.run(
            "Execute",
            lambda: launcher.command_execute(self.tools, self.paths(), self.options()),
        )

    def on_stop(self) -> None:
        if not self.runner.running:
            return
        if messagebox.askyesno(
            "Stop",
            "Stop the running command?\n\nWhatever it was working on stays 'running' "
            "in the manifest, and a resume replays it.",
        ):
            self.runner.stop()

    def run(self, label: str, build) -> None:
        try:
            command = build()
        except LauncherError as exc:
            messagebox.showwarning(label, str(exc))
            return
        self._save_settings()
        self.write(f"\n=== {label} ===\n$ {' '.join(command)}\n")
        try:
            self.runner.start(command, cwd=self.workdir)
        except (LauncherError, OSError) as exc:
            messagebox.showerror(label, str(exc))
            return
        self.set_busy(True, f"Running: {label}")
        self._next_status_refresh = 0
        self.root.after(POLL_MS, self.poll)

    def poll(self) -> None:
        for line in self.runner.poll():
            self.write(line + "\n")
        if self.runner.running:
            self._next_status_refresh -= POLL_MS
            if self._next_status_refresh <= 0:
                self.refresh_status()
                self._next_status_refresh = STATUS_REFRESH_MS
            self.root.after(POLL_MS, self.poll)
            return
        code = self.runner.returncode
        self.write(f"--- finished, exit code {code} ---\n")
        self.set_busy(False, "Finished." if code == 0 else f"Finished with exit code {code}.")
        self.refresh_status()

    def set_busy(self, busy: bool, message: str) -> None:
        for button in self.action_buttons:
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.bar.configure(text=message)

    def write(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def refresh_status(self) -> None:
        manifest = self.workdir / self.paths().manifest()
        rows, message = launcher.try_manifest_rows(manifest)
        self.tree.delete(*self.tree.get_children())
        parents: dict[str, str] = {}
        for row in rows:
            values = (row.kind, row.name, row.status, row.rows, row.duration, row.detail)
            if row.kind == "session":
                parents[row.session] = self.tree.insert(
                    "", "end", text=row.session, values=values, open=True, tags=(row.status,)
                )
            else:
                self.tree.insert(
                    parents.get(row.session, ""), "end", text="",
                    values=values, tags=(row.status,),
                )
        self.status_message.configure(text=message or f"{manifest}")

    def on_close(self) -> None:
        if self.runner.running and not messagebox.askyesno(
            "Quit", "A command is still running. Stop it and quit?"
        ):
            return
        self.runner.stop()
        self._save_settings()
        self.root.destroy()


def main(workdir: Path | None = None) -> int:
    workdir = Path(workdir or Path.cwd())
    root = tk.Tk()
    try:
        tools = launcher.locate_tools()
    except LauncherError as exc:
        root.withdraw()
        messagebox.showerror("Pullmanager", str(exc))
        return 1
    LauncherApp(root, tools, workdir)
    root.mainloop()
    return 0
