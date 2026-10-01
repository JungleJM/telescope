"""Desktop launcher for running pulls.

A thin tkinter view over launcher.py. It holds no logic of its own: every
button builds a command through the controller and runs it as a subprocess,
exactly as it would be typed. Anything worth testing lives in launcher.py.

Run with:  python scope.py --gui
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import config, launcher, pulls
from .launcher import LauncherError, Options, Paths
from .lock import LockInfo, clear_lock_of, live_lock
from .manifest import Manifest, ManifestError
from .models import DONE
from .yaml_io import file_signature, read_shared

POLL_MS = 100
STATUS_REFRESH_MS = 3000
# How often the loaded pull's lock (D67) and log (D68) are read, whoever
# started the pull. The status tab refreshes every STATUS_REFRESH_MS of it.
LOCK_CHECK_MS = 1000
# After Execute is pressed its buttons stay grey this long waiting for its
# lock, so they cannot be pressed twice before it appears.
EXECUTE_GRACE_SECONDS = 60
# Buttons that wait for a pull that is executing: Export split and Execute
# would overwrite it (D67); Artifacts packages only a finished pull (D72).
PULL_WRITERS = ("Export split", "Execute", "Artifacts")

STATUS_COLOURS = {
    "done": "#1a7f37",
    "failed": "#cf222e",
    "running": "#0969da",
    "blocked": "#6e7781",
    "skipped": "#6e7781",
    "pending": "#24292f",
}

# What the window remembers: the loaded transfer YAML. A pull's split and SQL
# folders are always its run folder's (D57, D142), and the data dictionary is
# the bundle's (D150), so neither is asked for.
FIELDS = (
    ("template", "Transfer YAML"),
)
NOT_RUN = "not run yet"


# Who made this window, at its foot (D145), and which bundle it is (D147).
# Packed ahead of the window's contents, so a small window squeezes them and
# never this.
CREDIT = "Designed and built by Jason Mathias"


def credit_text() -> str:
    found = config.bundle_id()
    return f"{CREDIT} \u00b7 bundle {found}" if found else CREDIT


def add_credit(root) -> None:
    from tkinter import ttk

    label = ttk.Label(root, text=credit_text(), foreground="#8c959f", font="TkSmallCaptionFont")
    placed = {"side": "bottom", "anchor": "e", "padx": 8, "pady": (0, 2)}
    slaves = root.pack_slaves()
    if slaves:
        placed["before"] = slaves[0]
    label.pack(**placed)


class LauncherApp:
    """The launcher. Given `parent`, it builds inside that frame, as the Run
    half of the app (D93), and leaves the window's title and closing to it."""

    def __init__(self, root: tk.Tk, tools: launcher.Tools, workdir: Path, parent=None):
        self.root = root
        self.standalone = parent is None
        self.frame = root if parent is None else parent
        self.tools = tools
        self.workdir = workdir
        self.runner = launcher.CommandRunner()
        self.vars: dict[str, tk.StringVar] = {}
        self.retry_failed = tk.BooleanVar(value=False)
        self.repull = tk.BooleanVar(value=False)
        # Chosen sessions to re-pull (D158): ticked, then added one by one.
        self.repull_some = tk.BooleanVar(value=False)
        self.repull_pick = tk.StringVar(value="")
        self.repull_list: list[str] = []
        self.action_buttons: list[ttk.Button] = []
        self.buttons: dict[str, ttk.Button] = {}
        self._next_status_refresh = 0
        self.pull_lock: LockInfo | None = None  # the loaded pull's live lock
        self._execute_pressed: float | None = None
        # Execute runs in a console window of its own; its log is followed (D68).
        self.console = launcher.ConsoleRunner()
        self.follower = launcher.LogFollower()
        self._console_log_seen = False
        self._console_handled = True
        self._pull_summarized = False
        self._status_countdown = 0

        if self.standalone:
            root.title(f"Pullmanager - {workdir}")
            root.geometry("1100x760")
            root.minsize(820, 520)
            root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_inputs()
        self._build_tabs()
        self._build_status_bar()
        self._load_settings()
        self.refresh_status()
        self.root.after(LOCK_CHECK_MS, self.watch_pull)

    # ------------------------------------------------------------- layout

    def _build_inputs(self) -> None:
        frame = ttk.LabelFrame(self.frame, text="Pull inputs", padding=8)
        frame.pack(fill="x", padx=10, pady=(10, 4))
        frame.columnconfigure(1, weight=1)

        for attr, _ in FIELDS:
            self.vars[attr] = tk.StringVar()
        # Three dropdowns (D126, D140): the pulls executing now, the ones that
        # have run, and the ones to start.
        self.running_pick = tk.StringVar()
        self.ended_pick = tk.StringVar()
        self.start_pick = tk.StringVar()
        self._running: dict[str, Path] = {}
        self._ended: dict[str, Path] = {}
        self._startable: dict[str, Path] = {}
        ttk.Label(frame, text="Running pulls").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        running = ttk.Combobox(frame, textvariable=self.running_pick, state="readonly",
                               postcommand=lambda: running.configure(values=list(self.running_choices())))
        running.grid(row=0, column=1, sticky="ew", pady=2)
        running.bind("<<ComboboxSelected>>", lambda e: self.choose_running(self.running_pick.get()))
        ttk.Label(frame, text="follow its log, status and Stop", foreground="#6e7781").grid(
            row=0, column=3, sticky="w")
        ttk.Label(frame, text="Finished and stopped pulls").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        ended = ttk.Combobox(frame, textvariable=self.ended_pick, state="readonly",
                             postcommand=lambda: ended.configure(values=list(self.ended_choices())))
        ended.grid(row=1, column=1, sticky="ew", pady=2)
        ended.bind("<<ComboboxSelected>>", lambda e: self.choose_ended(self.ended_pick.get()))
        ttk.Label(frame, text="retry, resume, re-pull or package", foreground="#6e7781").grid(
            row=1, column=3, sticky="w")
        ttk.Label(frame, text="Start run").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=2)
        start = ttk.Combobox(frame, textvariable=self.start_pick, state="readonly",
                             postcommand=lambda: start.configure(values=list(self.start_choices())))
        start.grid(row=2, column=1, sticky="ew", pady=2)
        start.bind("<<ComboboxSelected>>", lambda e: self.choose_start(self.start_pick.get()))
        ttk.Button(frame, text="Browse", command=lambda: self.browse("template", "file")).grid(
            row=2, column=2, padx=(6, 6), pady=2)
        ttk.Label(frame, text="transfer YAMLs here, by project", foreground="#6e7781").grid(
            row=2, column=3, sticky="w")
        self.loaded_line = ttk.Label(frame, text="", foreground="#6e7781")
        self.loaded_line.grid(row=3, column=1, columnspan=3, sticky="w")
        # Where Artifacts backs each pull up before replacing it (D149).
        ttk.Label(frame, text="Backup folder").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=2)
        self.backup_line = ttk.Label(frame, text="")
        self.backup_line.grid(row=4, column=1, sticky="w", pady=2)
        backup_buttons = ttk.Frame(frame)
        backup_buttons.grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Button(backup_buttons, text="Browse", command=self.browse_backup).pack(side="left", padx=(6, 6))
        ttk.Button(backup_buttons, text="Clear", command=lambda: self.set_backup(None)).pack(side="left")
        ttk.Button(backup_buttons, text="Back up all", command=self.on_backup_all).pack(side="left", padx=(6, 0))

        options = ttk.Frame(frame)
        options.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 4))
        ttk.Checkbutton(options, text="Retry failed", variable=self.retry_failed).pack(side="left")
        ttk.Checkbutton(
            options, text="Re-pull everything", variable=self.repull
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            options, text="Re-pull sessions", variable=self.repull_some,
            command=self.show_repull_sessions,
        ).pack(side="left", padx=(12, 0))

        # The sessions to re-pull, chosen from the loaded pull's finished
        # ones, `all` first; shown only while Re-pull sessions is ticked (D158).
        self.repull_frame = ttk.Frame(frame)
        self.repull_frame.grid(row=6, column=0, columnspan=4, sticky="w", pady=(0, 4))
        picker = ttk.Combobox(self.repull_frame, textvariable=self.repull_pick, state="readonly",
                              width=32, postcommand=lambda: picker.configure(values=self.repull_choices()))
        picker.pack(side="left")
        ttk.Button(self.repull_frame, text="Add", command=self.add_repull_session).pack(
            side="left", padx=(6, 0))
        ttk.Button(self.repull_frame, text="Remove", command=self.remove_repull_session).pack(
            side="left", padx=(6, 0))
        self.repull_line = ttk.Label(self.repull_frame, text="", foreground="#6e7781")
        self.repull_line.pack(side="left", padx=(8, 0))
        self.repull_frame.grid_remove()

        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        for text, handler in (
            ("Validate", self.on_validate),
            ("Export split", self.on_export_split),
            ("Preview SQL", self.on_dry_run),
            ("Execute", self.on_execute),
            ("Artifacts", self.on_artifacts),
            # Every pull, and the dictionary, not the loaded pull (D152, D155).
            ("Scan runs", self.on_scan_runs),
            ("Audit dictionary", self.on_audit_dictionary),
        ):
            button = ttk.Button(actions, text=text, command=handler)
            button.pack(side="left", padx=(0, 6))
            self.action_buttons.append(button)
            self.buttons[text] = button
        self.stop_button = ttk.Button(actions, text="Stop", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="right")

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self.frame)
        notebook.pack(fill="both", expand=True, padx=10, pady=4)
        self.notebook = notebook

        output_tab = ttk.Frame(notebook)
        self.output = scrolledtext.ScrolledText(
            output_tab, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.output.pack(fill="both", expand=True)
        # Validate, Export split and Preview SQL, which run inside the window.
        notebook.add(output_tab, text="Validation Output")

        # Execute's log, however it was started (D68, D71).
        self.pull_tab = ttk.Frame(notebook)
        self.pull_output = scrolledtext.ScrolledText(
            self.pull_tab, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.pull_output.pack(fill="both", expand=True)
        notebook.add(self.pull_tab, text="Pull Log")

        # The loaded pull's manifest as it is, read-only (D144): the running
        # pull rewrites it, so an edit here would be lost or clobber its record.
        self.manifest_tab = ttk.Frame(notebook)
        self.manifest_text = scrolledtext.ScrolledText(
            self.manifest_tab, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.manifest_text.pack(fill="both", expand=True)
        for status, colour in STATUS_COLOURS.items():
            self.manifest_text.tag_configure(status, foreground=colour)
        self.manifest_text.tag_configure("error", foreground=STATUS_COLOURS["failed"])
        self.manifest_text.tag_configure("found", background="#fff8c5")
        self._manifest_shown = ""
        self._manifest_found: launcher.StatusRow | None = None
        # The manifest and its size and time when Status last read it (D154).
        self._status_seen: tuple[Path, tuple[int, int]] | None = None
        notebook.add(self.manifest_tab, text="Pull Manifest")

        status_tab = ttk.Frame(notebook)
        # Refresh and the manifest it reads, above the tree they describe.
        bar = ttk.Frame(status_tab)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Refresh", command=lambda: self.refresh_status(force=True)).pack(
            side="left", pady=4)
        self.status_message = ttk.Label(bar, text="", foreground="#6e7781")
        self.status_message.pack(side="left", padx=(8, 0), pady=4)

        # A table's rows per join key after its rows and time (D157).
        columns = ("kind", "name", "status", "rows", "duration", "median", "p90", "max", "detail")
        self.tree = ttk.Treeview(status_tab, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Session")
        self.tree.column("#0", width=220)
        widths = {"kind": 70, "name": 150, "status": 80, "rows": 90, "duration": 80,
                  "median": 105, "p90": 60, "max": 60, "detail": 300}
        headings = {"median": "Median per key", "p90": "P90", "max": "Max"}
        for column in columns:
            self.tree.heading(column, text=headings.get(column, column.capitalize()))
            self.tree.column(column, width=widths[column], anchor="w")
        for status, colour in STATUS_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)
        # Double-click a row to see its lines in the manifest (D144).
        self._status_rows: dict[str, launcher.StatusRow] = {}
        self.tree.bind("<Double-1>", self.on_status_double_click)
        scroll = ttk.Scrollbar(status_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        notebook.add(status_tab, text="Status")

    def _build_status_bar(self) -> None:
        self.bar = ttk.Label(self.frame, text="Ready.", anchor="w", padding=(10, 4))
        self.bar.pack(fill="x", side="bottom")

    # ------------------------------------------------------------ settings

    def paths(self) -> Paths:
        return Paths(**{attr: self.vars[attr].get() for attr, _ in FIELDS},
                     runs=str(config.runs_setting(self.workdir)))

    # ------------------------------------------------------- choosing a pull

    def running_choices(self) -> dict[str, Path]:
        """Every pull executing now, as "<project>: executing since ..." (D126)."""
        self._running = {f"{pull.name}: {pull.state}": self.transfer_for(pull.name)
                         for pull in pulls.find_pulls(self.workdir) if pull.lock is not None}
        return self._running

    def ended_choices(self) -> dict[str, Path]:
        """Every pull that has run and is not executing, with how it stands:
        finished, finished with errors, stopped by user or stopped with errors
        (D140)."""
        self._ended = {f"{pull.name}  ({pull.state})": self.transfer_for(pull.name)
                       for pull in pulls.find_pulls(self.workdir)
                       if pull.lock is None and pull.outcome}
        return self._ended

    def start_choices(self) -> dict[str, Path]:
        """The working blueprints by project that have not run yet: never
        split, or split and not executed (D126, D140, D162)."""
        states = {pull.name.lower(): pull for pull in pulls.find_pulls(self.workdir)}
        self._startable = {}
        found = pulls.working_blueprints(self.workdir)
        for project in sorted(found, key=str.lower):
            pull = states.get(project.lower())
            if pull is not None and (pull.lock is not None or pull.outcome):
                continue
            self._startable[f"{project}  ({pull.state if pull else NOT_RUN})"] = found[project]
        return self._startable

    def transfer_for(self, project: str) -> Path:
        """A project's working blueprint; else, once packaged, the copy in its
        run folder; else where its working blueprint would be (D162)."""
        for name, path in pulls.working_blueprints(self.workdir).items():
            if name.lower() == project.lower():
                return path
        for pull in pulls.find_pulls(self.workdir):
            if pull.name.lower() == project.lower():
                record = pulls.record_blueprint(pull.manifest.parent)
                if record is not None:
                    return record
        return self.workdir / "YAMLs" / "temp" / f"{project}{pulls.BLUEPRINT_SUFFIX}"

    def choose_running(self, label: str) -> None:
        path = self._running.get(label) or self.running_choices().get(label)
        if path is not None:
            self.load(path, "Following it: Pull Log, Status and Stop are this pull's.")

    def choose_ended(self, label: str) -> None:
        path = self._ended.get(label) or self.ended_choices().get(label)
        if path is not None:
            then = ("Execute resumes it; Retry failed and Re-pull everything are above; "
                    "Artifacts packages it; Status shows its tables.")
            if self.tables_dropped(label.split("  (")[0]):
                then = ("Packaged, and its tables dropped from Projects (D165): its parquets hold "
                        "them, and only Re-pull everything pulls it again. Status shows its tables.")
            self.load(path, then)

    def tables_dropped(self, project: str) -> bool:
        """Whether a packaged pull's tables were dropped from Projects (D165)."""
        for pull in pulls.find_pulls(self.workdir):
            if pull.name.lower() == project.lower():
                try:
                    return "tables_dropped:" in pull.manifest.read_text(encoding="utf-8")
                except OSError:
                    return False
        return False

    def choose_start(self, label: str) -> None:
        path = self._startable.get(label) or self.start_choices().get(label)
        if path is not None:
            self.load(path, "Validate, Export split, Preview SQL, then Execute.")

    def load(self, path: Path, then: str) -> None:
        self.vars["template"].set(str(path))
        self._save_settings()
        self.show_loaded()
        self.refresh_status()
        self.bar.configure(text=f"Loaded {Path(path).name}. {then}")

    def show_loaded(self) -> None:
        """The loaded pull, where its split and SQL go, and the dictionary used."""
        try:
            run_dir = self.paths().run_dir()
        except LauncherError:
            self.loaded_line.configure(text="Nothing loaded: choose a pull above.")
        else:
            self.loaded_line.configure(text=f"Loaded {Path(self.vars['template'].get()).name}: its run folder is "
                                            f"{run_dir}, the split and SQL in {run_dir / pulls.PULL_FILES_DIR}")
        self.backup_line.configure(text=self.backup_found())

    def backup_found(self) -> str:
        """The backup folder, and whether it can be reached (D149)."""
        local = config.local_backup_dir(self.workdir)
        try:
            folder = config.backup_dir(self.workdir)
        except config.ConfigError as exc:
            return f"datascope.json: {exc}"
        if folder is None:
            return f"none set: each pull is backed up to {local}"
        if not folder.is_dir():
            return f"{folder}  (NOT FOUND: until it is, backups go to {local})"
        return str(folder)

    def set_backup(self, folder: Path | None) -> None:
        try:
            config.set_backup(self.workdir, folder)
        except (OSError, config.ConfigError) as exc:
            messagebox.showerror("Backup folder", str(exc))
        self.show_loaded()

    def browse_backup(self) -> None:
        current = None
        try:
            current = config.backup_dir(self.workdir)
        except config.ConfigError:
            pass
        chosen = filedialog.askdirectory(initialdir=str(current or self.workdir))
        if chosen:
            self.set_backup(Path(chosen))

    def options(self) -> Options:
        chosen = tuple(self.repull_list) if self.repull_some.get() else ()
        return Options(retry_failed=bool(self.retry_failed.get()), repull=bool(self.repull.get()),
                       repull_sessions=chosen)

    # ------------------------------------------------- re-pulling sessions

    def show_repull_sessions(self) -> None:
        if self.repull_some.get():
            self.repull_frame.grid()
        else:
            self.repull_frame.grid_remove()

    def repull_choices(self) -> list[str]:
        """`all`, then the loaded pull's finished sessions."""
        manifest = self._manifest()
        try:
            sessions = Manifest.load(manifest).sessions if manifest is not None else []
        except (ManifestError, OSError, ValueError):
            sessions = []
        return ["all", *[s.session_id for s in sessions if s.status == DONE]]

    def add_repull_session(self) -> None:
        pick = self.repull_pick.get().strip()
        if pick and pick not in self.repull_list:
            self.repull_list.append(pick)
        self.show_repull_list()

    def remove_repull_session(self) -> None:
        pick = self.repull_pick.get().strip()
        if pick in self.repull_list:
            self.repull_list.remove(pick)
        self.show_repull_list()

    def show_repull_list(self) -> None:
        text = ", ".join(self.repull_list)
        self.repull_line.configure(text=f"To re-pull: {text}" if text else "Nothing added yet")

    def _load_settings(self) -> None:
        saved = launcher.load_settings(self.workdir)
        for attr, _ in FIELDS:
            self.vars[attr].set(getattr(saved, attr))
        self.show_loaded()

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
        if chosen and attr == "template":
            self.load(Path(chosen), "Validate, Export split, Preview SQL, then Execute.")

    # ------------------------------------------------------------- actions

    def on_backup_all(self) -> None:
        self.run("Back up all", lambda: launcher.command_backup_all(self.tools))

    def on_scan_runs(self) -> None:
        self.run("Scan runs", lambda: launcher.command_scan_runs(self.tools))

    def on_audit_dictionary(self) -> None:
        self.run("Audit dictionary", lambda: launcher.command_audit_dictionary(self.tools))

    def on_validate(self) -> None:
        self.run("Validate", lambda: launcher.command_validate(self.tools, self.paths()))

    def on_export_split(self) -> None:
        self.run("Export split", lambda: launcher.command_export_split(self.tools, self.paths()))

    def on_dry_run(self) -> None:
        self.run(
            "Preview SQL",
            lambda: launcher.command_dry_run(self.tools, self.paths(), self.options()),
        )

    def on_artifacts(self) -> None:
        self.run("Artifacts", lambda: launcher.command_artifacts(self.tools, self.paths()))

    def on_execute(self) -> None:
        if not messagebox.askokcancel(
            "Execute pull",
            "This runs against Cosmos and Projects and updates the manifest.\n\nContinue?",
        ):
            return
        try:
            command = launcher.command_execute(
                self.tools, self.paths(), self.options(), keep_open=launcher.CAN_OPEN_CONSOLE
            )
        except LauncherError as exc:
            messagebox.showwarning("Execute", str(exc))
            return
        self._save_settings()
        # Grey its buttons at once, before its lock appears (D67).
        self._execute_pressed = time.monotonic()
        self.update_buttons()
        try:
            self.console.start(command, cwd=self.workdir)
        except (LauncherError, OSError) as exc:
            self._execute_pressed = None
            self.update_buttons()
            self.cannot_start(f"its window could not be opened: {exc}")
            return
        self._console_log_seen = False
        self._console_handled = False
        self._pull_summarized = False
        self.update_stop()
        self.bar.configure(text="Execute is running in its own window; its output follows in Pull Log.")
        self.notebook.select(self.pull_tab)

    def cannot_start(self, why: str) -> None:
        """Say so, and give the command that works from a terminal (D66, D68)."""
        manifest = self._manifest()
        command, folder = pulls.execute_command(manifest) if manifest else ("", self.workdir)
        text = (
            f"Execute could not start from the launcher: {why}.\n\n"
            f"Run it from a terminal instead. In {folder}, type:\n\n    {command}"
        )
        self.write_pull_log(f"\n{text}\n")
        self.bar.configure(text="Execute could not start; see Pull Log.")
        messagebox.showerror("Execute", text)

    def on_stop(self) -> None:
        if self.runner.running:
            if messagebox.askyesno(
                "Stop",
                "Stop the running command?\n\nWhatever it was working on stays 'running' "
                "in the manifest, and a resume replays it.",
            ):
                self.runner.stop()
            return
        if self.console.alive and messagebox.askyesno(
            "Stop",
            "Stop the pull? Its window closes.\n\nWhatever it was working on stays "
            "'running' in the manifest, and the next Execute pulls it again.",
        ):
            pid = self.console.pid
            self.console.stop()
            try:
                self.console.wait(timeout=10)
            except Exception:
                pass
            manifest = self._manifest()
            # Ended from outside, it could not remove its own lock, nor say
            # how it ended (D140).
            if manifest is not None:
                clear_lock_of(manifest, pid)
                try:
                    pulls.record_stopped_by_user(manifest, self.console.returncode)
                except Exception:
                    pass
            self.watch_once()

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
        self._execute_pressed = None
        self.set_busy(False, "Finished." if code == 0 else f"Finished with exit code {code}.")
        self.refresh_status()

    def set_busy(self, busy: bool, message: str) -> None:
        self.update_buttons()
        self.update_stop()
        self.bar.configure(text=message)

    def update_stop(self) -> None:
        live = self.runner.running or self.console.alive
        self.stop_button.configure(state="normal" if live else "disabled")

    def update_buttons(self) -> None:
        """Everything waits for the window's own command. While the loaded
        pull is executing, only what would overwrite it is greyed (D67):
        Validate and Preview SQL write nothing a pull reads."""
        busy = self.runner.running
        executing = self.pull_lock is not None or self._execute_pressed is not None
        for text, button in self.buttons.items():
            off = busy or (executing and text in PULL_WRITERS)
            button.configure(state="disabled" if off else "normal")

    def watch_pull(self) -> None:
        """Every second: the loaded pull's lock and log, however it was started."""
        self.watch_once()
        self.root.after(LOCK_CHECK_MS, self.watch_pull)

    def watch_once(self) -> None:
        was_live = self.pull_lock is not None
        self.check_pull()
        self.follow_log()
        self.check_console()
        live = self.pull_lock is not None
        self._status_countdown -= LOCK_CHECK_MS
        if (live and self._status_countdown <= 0) or (was_live and not live):
            self.refresh_status()
            self._status_countdown = STATUS_REFRESH_MS

    def _manifest(self) -> Path | None:
        try:
            return self.workdir / self.paths().manifest()
        except LauncherError:
            return None

    def follow_log(self) -> None:
        """Show what the pull's log has added: the live Execute's, else the newest."""
        manifest = self._manifest()
        log = launcher.pull_log(manifest, self.pull_lock)
        switched, text = self.follower.read(log)
        if switched:
            self.pull_output.configure(state="normal")
            self.pull_output.delete("1.0", "end")
            self.pull_output.configure(state="disabled")
            if log is not None:
                self.write_pull_log(f"--- {pulls.shown(log, self.workdir)} ---\n")
        if text:
            self.write_pull_log(text)
            if "session(s) run completed" in text:
                self._pull_summarized = True
        if log is not None and not self._console_log_seen and self.console.started:
            try:
                self._console_log_seen = log.stat().st_mtime >= self.console.started - 2
            except OSError:
                pass

    def check_console(self) -> None:
        """Notice the console's process ending; one that wrote no log never started."""
        if self._console_handled or self.console.alive:
            return
        self._console_handled = True
        self._execute_pressed = None
        self.follow_log()  # anything written at the very end
        code = self.console.returncode
        if not self._console_log_seen:
            self.cannot_start(f"it ended with exit code {launcher.exit_code_words(code)} "
                              "before writing its log")
        elif code != 0 and not self._pull_summarized:
            # It ended mid-pull: killed (Stop, or anything else, gives 1 on
            # Windows) or an error the log shows above.
            self.write_pull_log(
                f"--- Execute's window closed, exit code {launcher.exit_code_words(code)}, before "
                "the pull finished. What it was working on stays 'running'; the next Execute "
                "pulls it again. If Python gave a reason, it is just above. ---\n"
            )
            self.bar.configure(text="Execute ended before finishing; see Pull Log.")
        else:
            self.write_pull_log(f"--- Execute's window closed, exit code {code} ---\n")
            self.bar.configure(text="Execute has finished.")
        self.update_buttons()
        self.update_stop()

    def check_pull(self) -> None:
        manifest = self._manifest()
        self.pull_lock = live_lock(manifest) if manifest else None
        pressed = self._execute_pressed
        if self.pull_lock is not None or (
            pressed is not None and time.monotonic() - pressed > EXECUTE_GRACE_SECONDS
        ):
            self._execute_pressed = None
        self.update_buttons()

    def write(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def write_pull_log(self, text: str) -> None:
        self.pull_output.configure(state="normal")
        self.pull_output.insert("end", text)
        self.pull_output.see("end")
        self.pull_output.configure(state="disabled")

    def refresh_status(self, force: bool = False) -> None:
        """Redraw Status and Pull Manifest from one read of the manifest, and
        none when it has not changed since the last (D154)."""
        text = None
        try:
            manifest = self.workdir / self.paths().manifest()
        except LauncherError as exc:
            manifest, rows, message = None, [], str(exc)
            self._status_seen = None
        else:
            seen = (manifest, file_signature(manifest))
            if not force and seen[1] is not None and seen == self._status_seen:
                self.show_status_message(manifest, "")
                return
            self._status_seen = seen
            if seen[1] is not None:
                try:
                    text = read_shared(manifest)
                except OSError:
                    self._status_seen = None  # caught mid-save: read it next time
            rows, message = launcher.try_manifest_rows(manifest, text)
        self.tree.delete(*self.tree.get_children())
        self._status_rows = {}
        parents: dict[str, str] = {}
        step = ""
        for row in rows:
            values = (row.kind, row.name, row.status, row.rows, row.duration,
                      row.median, row.p90, row.max, row.detail)
            if row.kind == "session":
                item = parents[row.session] = self.tree.insert(
                    "", "end", text=row.session, values=values, open=True, tags=(row.status,)
                )
            elif row.kind == "table":
                # Under the phase or run that landed it (D137).
                item = self.tree.insert(step or parents.get(row.session, ""), "end", text="",
                                        values=values)
            else:
                item = step = self.tree.insert(
                    parents.get(row.session, ""), "end", text="",
                    values=values, tags=(row.status,), open=True,
                )
            self._status_rows[str(item)] = row
        self.show_status_message(manifest, message)
        self.refresh_manifest_view(manifest, text)

    def show_status_message(self, manifest: Path | None, message: str) -> None:
        if not message and self.pull_lock is not None:
            message = f"{self.pull_lock.summary()}.  {manifest}"
        self.status_message.configure(text=message or f"{manifest}")

    def refresh_manifest_view(self, manifest: Path | None, text: str | None = None) -> None:
        """Show the manifest's text, coloured, keeping where it was scrolled
        to; redrawn only when the file has changed (D144). `text` is the
        manifest already read, so Status and this tab share one read (D154)."""
        if text is None:
            try:
                text = read_shared(manifest) if manifest is not None else ""
            except OSError:
                text = ""
        if not text:
            text = f"No manifest yet at {manifest}. Export a split first.\n" if manifest else ""
        if text == self._manifest_shown:
            return
        self._manifest_shown = text
        view = self.manifest_text
        top = view.yview()[0]
        width = len(str(max(1, text.count("\n") + 1)))
        numbered = "".join(f"{n:>{width}}  {line}\n"
                           for n, line in enumerate(text.splitlines(), start=1))
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", numbered)
        for line, tag in launcher.manifest_colours(text):
            view.tag_add(tag, f"{line + 1}.0", f"{line + 1}.end")
        view.configure(state="disabled")
        view.yview_moveto(top)
        if self._manifest_found is not None:
            self._mark_manifest_line(self._manifest_found, scroll=False)

    def on_status_double_click(self, event=None) -> None:
        item = self.tree.focus()
        row = self._status_rows.get(str(item))
        if row is not None:
            self.show_in_manifest(row)

    def show_in_manifest(self, row: launcher.StatusRow) -> None:
        """Switch to Pull Manifest with the row's lines in view, highlighted:
        its error when it has one (D144)."""
        self.refresh_manifest_view(self._manifest())
        self._manifest_found = row
        self._mark_manifest_line(row, scroll=True)
        self.notebook.select(self.manifest_tab)

    def _mark_manifest_line(self, row: launcher.StatusRow, *, scroll: bool) -> None:
        view = self.manifest_text
        view.tag_remove("found", "1.0", "end")
        span = launcher.manifest_span(self._manifest_shown, row)
        if span is None:
            return
        first, after = span
        view.tag_add("found", f"{first + 1}.0", f"{after + 1}.0")
        if scroll:
            view.see(f"{after}.0")  # the whole of it, then its first line
            view.see(f"{first + 1}.0")

    def use_transfer(self, path: Path) -> None:
        """Take a transfer YAML the Author half exported (D94)."""
        self.load(Path(path), "Validate, Export split, then Execute.")

    def close(self) -> bool:
        """Ready the launcher to close; False if the user chose to keep it open.
        A pull in its own window carries on when this one closes."""
        if self.runner.running and not messagebox.askyesno(
            "Quit", "A command is still running. Stop it and quit?"
        ):
            return False
        self.runner.stop()
        self._save_settings()
        return True

    def on_close(self) -> None:
        if self.close():
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
    add_credit(root)
    root.mainloop()
    return 0
