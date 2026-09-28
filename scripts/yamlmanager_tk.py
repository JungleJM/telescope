"""YAML Manager's Author half, in tkinter (D93).

A view over yamlmanager_model (D92): every value shown is read from the
model, every edit is a call to it, and it holds no rules of its own. The
Builder's sections follow D96; Validate, Exports and YAML sit beside it.

It is opened as one half of the app, beside Run (pullmanager/app.py):

    python pullmanager.py          # on the VM, in the working folder
    python3.13 scripts/pullmanager_src/pullmanager.py    # on the Mac, at the root

Standard library only (tkinter), as on the VM.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import yamlmanager_model as model  # noqa: E402

COLOURS = {
    "error": "#cf222e",
    "warning": "#9a6700",
    "pending": "#0969da",
    "pass": "#1a7f37",
    "muted": "#6e7781",
}
SECTIONS = (
    ("project", "Project"),
    ("pk", "PK Table"),
    ("supporting", "Supporting Tables"),
    ("multipliers", "Multipliers"),
    ("splitters", "Splitters"),
    ("fact", "Fact Tables"),
)
# How long after the last edit the draft is checked (D96).
VALIDATE_DELAY_MS = 400
NEW_FILE_LABEL = "(new, not saved yet)"


# =============================================================================
# Widgets every section uses
# =============================================================================


class ScrollFrame(ttk.Frame):
    """A frame whose contents scroll: Tk has none built in."""

    def __init__(self, parent: Any):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=(12, 8))
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # The contents take the full width, so fields can use the monitor.
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._wheel(False))

    def _wheel(self, on: bool) -> None:
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _scroll(self, event: Any) -> None:
        # Windows reports multiples of 120, the Mac small numbers.
        step = -int(event.delta / 120) if abs(event.delta) >= 120 else -int(event.delta)
        self.canvas.yview_scroll(step, "units")

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)


def note(parent: Any, text: str, colour: str = "muted", wrap: int = 900) -> ttk.Label:
    return ttk.Label(parent, text=text, foreground=COLOURS.get(colour, colour), wraplength=wrap, justify="left")


def heading(parent: Any, text: str, explain: str = "") -> None:
    ttk.Label(parent, text=text, font=("TkDefaultFont", 13, "bold")).pack(anchor="w", pady=(4, 0))
    if explain:
        note(parent, explain).pack(anchor="w", pady=(0, 8))


# =============================================================================
# The Author half
# =============================================================================


class AuthorView:
    """The Builder, Validate, Exports and YAML, over one draft at a time."""

    def __init__(self, parent: Any, root: Any, workspace: model.Workspace,
                 on_transfer: Callable[[Path], None] | None = None,
                 on_title: Callable[[str], None] | None = None):
        self.parent = parent
        self.root = root
        self.ws = workspace
        self.on_transfer = on_transfer
        self.on_title = on_title
        self.draft = model.Draft.new(workspace)
        self._pending_check: Any = None
        self._file_paths: dict[str, Path] = {}
        self.section_key = "project"
        self.highlight: model.FieldRef | None = None

        self._build_top()
        self._build_tabs()
        self.status = ttk.Label(parent, text="", anchor="w", padding=(10, 4))
        self.status.pack(fill="x", side="bottom")
        self.sections: dict[str, Any] = {}
        self.section_builders: dict[str, Callable[[Any], None]] = {}
        self.show_section("project")
        self.refresh_files()
        self.loaded()

    # ------------------------------------------------------------- layout

    def _build_top(self) -> None:
        top = ttk.Frame(self.parent, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Project name").pack(side="left")
        self.name_var = tk.StringVar()
        self.name_box = ttk.Combobox(top, textvariable=self.name_var, width=36)
        self.name_box.pack(side="left", padx=(6, 4))
        self.name_box.bind("<<ComboboxSelected>>", self.on_pick)
        self.name_var.trace_add("write", lambda *a: self.on_name_typed())
        ttk.Button(top, text="Browse", command=self.browse).pack(side="left", padx=2)
        ttk.Button(top, text="New", command=self.new).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Save", command=self.save).pack(side="left", padx=2)
        ttk.Button(top, text="Transfer to Run", command=self.transfer).pack(side="left", padx=2)
        self.target = note(top, "")
        self.target.pack(side="left", padx=(10, 0))

    def _build_tabs(self) -> None:
        self.tabs = ttk.Notebook(self.parent)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=4)

        builder = ttk.Frame(self.tabs)
        self.tabs.add(builder, text="Builder")
        self.builder_tab = builder
        nav = ttk.Frame(builder, padding=(0, 4, 8, 4))
        nav.pack(side="left", fill="y")
        self.nav = ttk.Treeview(nav, show="tree", selectmode="browse", height=len(SECTIONS))
        self.nav.column("#0", width=190)
        for key, label in SECTIONS:
            self.nav.insert("", "end", iid=key, text=label)
        for kind, colour in COLOURS.items():
            self.nav.tag_configure(kind, foreground=colour)
        self.nav.pack(fill="y", expand=True)
        self.nav.bind("<<TreeviewSelect>>", lambda e: self.show_section(self.nav.selection()[0])
                      if self.nav.selection() else None)
        self.body = ScrollFrame(builder)
        self.body.pack(side="left", fill="both", expand=True)

        self.validate_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.validate_tab, text="Validate")
        self.exports_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.exports_tab, text="Exports")
        self.yaml_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.yaml_tab, text="YAML")
        self.yaml_text = scrolledtext.ScrolledText(self.yaml_tab, wrap="none", font=("Courier", 11))
        self.yaml_text.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.on_tab())

    # ------------------------------------------------------------ sections

    def register(self, key: str, build: Callable[[Any], None]) -> None:
        """A section's builder: it fills the frame it is given from the model."""
        self.section_builders[key] = build

    def show_section(self, key: str) -> None:
        self.section_key = key
        if self.nav.selection() != (key,):
            self.nav.selection_set(key)
        self.render()

    def render(self) -> None:
        """Build the open section again from the model."""
        self.body.clear()
        build = self.section_builders.get(self.section_key)
        if build is None:
            label = dict(SECTIONS)[self.section_key]
            heading(self.body.inner, label)
            note(self.body.inner, "Not built yet.").pack(anchor="w")
            return
        try:
            build(self.body.inner)
        except model.DraftError as exc:
            note(self.body.inner, str(exc), "error").pack(anchor="w")

    # -------------------------------------------------------------- edits

    def edit(self, change: Callable[[], Any], rerender: bool = False) -> Any:
        """Make a change through the model; a refusal is said, not raised."""
        try:
            value = change()
        except model.DraftError as exc:
            self.say(str(exc), "error")
            return None
        self.changed(rerender)
        return value

    def changed(self, rerender: bool = False) -> None:
        self.update_title()
        if rerender:
            self.render()
        self.schedule_check()

    def say(self, text: str, kind: str = "muted") -> None:
        self.status.configure(text=text, foreground=COLOURS.get(kind, kind))

    def schedule_check(self) -> None:
        if self._pending_check is not None:
            self.root.after_cancel(self._pending_check)
        self._pending_check = self.root.after(VALIDATE_DELAY_MS, self.check)

    def check(self) -> None:
        """Validate the draft and show the result everywhere it shows (D96)."""
        self._pending_check = None
        validation = self.draft.validation()
        counts = {kind: len(validation.of_kind(kind)) for kind in ("error", "warning", "pending")}
        if validation.ok and not counts["warning"] and not counts["pending"]:
            self.say("Valid: ready to save and transfer.", "pass")
        else:
            parts = [f"{n} {kind}{'s' if n != 1 and kind != 'pending' else ''}" for kind, n in counts.items() if n]
            worst = "error" if counts["error"] else ("warning" if counts["warning"] else "pending")
            self.say(", ".join(parts) + ". See Validate.", worst)
        worst_by_section: dict[str, str] = {}
        rank = {"pending": 1, "warning": 2, "error": 3}
        for message in validation.messages:
            if message.field is not None:
                current = worst_by_section.get(message.field.section)
                if current is None or rank[message.kind] > rank[current]:
                    worst_by_section[message.field.section] = message.kind
        for key, label in SECTIONS:
            kind = worst_by_section.get(key)
            self.nav.item(key, text=f"{label}  ●" if kind else label, tags=(kind,) if kind else ())
        refresh = getattr(self, "refresh_validate", None)
        if refresh:
            refresh(validation)

    # ---------------------------------------------------- files and title

    def update_title(self) -> None:
        name = self.draft.path.name if self.draft.path else NEW_FILE_LABEL
        title = f"{name}{' *' if self.draft.dirty else ''}"
        if self.on_title:
            self.on_title(title)
        target = self.draft.save_target()
        try:
            shown = target.relative_to(self.ws.home)
        except ValueError:
            shown = target
        self.target.configure(text=f"Save writes {shown.as_posix()}")

    def refresh_files(self) -> None:
        self._file_paths = {}
        for path in self.ws.intakes() + self.ws.transfers():
            try:
                label = path.relative_to(self.ws.home).as_posix()
            except ValueError:
                label = str(path)
            self._file_paths[label] = path
        self.name_box.configure(values=list(self._file_paths))

    def loaded(self) -> None:
        """A draft was opened or started: show it everywhere."""
        self._setting_name = True
        self.name_var.set(self.draft.project_name)
        self._setting_name = False
        self.update_title()
        self.render()
        self.schedule_check()

    def on_name_typed(self) -> None:
        if getattr(self, "_setting_name", False):
            return
        value = self.name_var.get()
        if value in self._file_paths:
            return  # a file chosen from the list, opened by on_pick
        self.edit(lambda: setattr(self.draft, "project_name", value))

    def on_pick(self, event: Any = None) -> None:
        path = self._file_paths.get(self.name_var.get())
        if path is not None:
            self.open(path)

    def confirm_discard(self) -> bool:
        return not self.draft.dirty or messagebox.askyesno(
            "Unsaved changes", "This draft has changes that are not saved. Discard them?")

    def open(self, path: Path) -> None:
        if not self.confirm_discard():
            self.loaded()
            return
        try:
            self.draft = model.Draft.open(self.ws, path)
        except model.DraftError as exc:
            messagebox.showerror("Open", str(exc))
            self.loaded()
            return
        self.say(f"Opened {path.name}.")
        self.loaded()

    def browse(self) -> None:
        chosen = filedialog.askopenfilename(
            initialdir=str(self.ws.temp_dir if self.ws.temp_dir.is_dir() else self.ws.home),
            filetypes=[("YAML", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if chosen:
            self.open(Path(chosen))

    def new(self) -> None:
        if not self.confirm_discard():
            return
        self.draft = model.Draft.new(self.ws)
        self.show_section("project")
        self.loaded()
        self.say("A new draft. Give it a Project name.")

    def save(self) -> bool:
        result = self.draft.save()
        self.say(result.message, "pass" if result.ok else "error")
        if result.ok:
            self.refresh_files()
            self.update_title()
            self.render()
            self.schedule_check()
        return result.ok

    def transfer(self) -> None:
        """Save, export the transfer YAML, and hand it to Run (D94)."""
        if self.draft.dirty or self.draft.path is None:
            if not self.save():
                return
        ok, message, path = self.draft.export_transfer()
        self.say(message, "pass" if ok else "error")
        if ok:
            self.refresh_files()
            if self.on_transfer and path is not None:
                self.on_transfer(path)

    def on_tab(self) -> None:
        current = self.tabs.select()
        if current == str(self.yaml_tab):
            self.yaml_text.delete("1.0", "end")
            self.yaml_text.insert("1.0", self.draft.yaml_text())
        refresh = getattr(self, "refresh_exports", None)
        if refresh and current == str(self.exports_tab):
            refresh()

    def close(self) -> bool:
        return self.confirm_discard()
