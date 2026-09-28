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
        for key, build in SECTION_BUILDERS.items():
            self.register(key, lambda parent, build=build: build(self, parent))
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


# =============================================================================
# Small builders the sections share
# =============================================================================


def keep(widget: Any, *variables: Any) -> None:
    """Tk variables must outlive the function that made them."""
    widget._kept = getattr(widget, "_kept", []) + list(variables)


def text_field(view: AuthorView, parent: Any, value: Any, on_change: Callable[[str], Any],
               width: int = 30, rerender_on_leave: bool = False) -> ttk.Entry:
    """An entry that writes to the model as it is typed."""
    var = tk.StringVar(value="" if value is None else str(value))
    entry = ttk.Entry(parent, textvariable=var, width=width)
    keep(entry, var)
    var.trace_add("write", lambda *a: view.edit(lambda: on_change(var.get())))
    if rerender_on_leave:
        entry.bind("<Return>", lambda e: view.render())
        entry.bind("<FocusOut>", lambda e: view.render())
    return entry


def check_field(view: AuthorView, parent: Any, text: str, value: bool,
                on_change: Callable[[bool], Any], rerender: bool = False) -> ttk.Checkbutton:
    var = tk.BooleanVar(value=bool(value))
    box = ttk.Checkbutton(parent, text=text, variable=var,
                          command=lambda: view.edit(lambda: on_change(bool(var.get())), rerender=rerender))
    keep(box, var)
    return box


def choice_field(view: AuthorView, parent: Any, values: list[str], value: str,
                 on_change: Callable[[str], Any], width: int = 24, rerender: bool = False) -> ttk.Combobox:
    var = tk.StringVar(value=value)
    box = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly")
    keep(box, var)
    box.bind("<<ComboboxSelected>>", lambda e: view.edit(lambda: on_change(var.get()), rerender=rerender))
    return box


def grid_row(parent: Any, row: int, label: str, widget: Any, hint: str = "") -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
    widget.grid(row=row, column=1, sticky="w", pady=3)
    if hint:
        note(parent, hint).grid(row=row, column=2, sticky="w", padx=(10, 0))


def entry_box(view: AuthorView, parent: Any, title: str, section: str, index: int | None) -> ttk.LabelFrame:
    """A framed entry; the one a Validate message pointed at says so."""
    box = ttk.LabelFrame(parent, text=title, padding=10)
    box.pack(fill="x", pady=6)
    mark = view.highlight
    if mark is not None and mark.section == section and (mark.index is None or mark.index == index):
        note(box, "Validate points here: " + (mark.detail or "this entry"), "error").pack(anchor="w")
    return box


def relative_location(view: AuthorView, chosen: str) -> str:
    """A chosen file as the template writes it: relative to the draft's folder
    when it is under it, as the transfer export copies those (D85)."""
    base = view.draft.base_path().parent
    path = Path(chosen)
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def pick_file(view: AuthorView, kind: str) -> str:
    types = {"parquet": [("Parquet", "*.parquet")], "csv": [("CSV", "*.csv")]}.get(kind, [])
    start = view.draft.base_path().parent
    chosen = filedialog.askopenfilename(initialdir=str(start if start.is_dir() else view.ws.home),
                                        filetypes=types + [("All files", "*.*")])
    return relative_location(view, chosen) if chosen else ""


def location_row(view: AuthorView, parent: Any, row: int, kind: str, value: str,
                 on_change: Callable[[str], Any]) -> None:
    """File (with Browse), or a Projects table's name for a dbtable."""
    holder = ttk.Frame(parent)
    entry = text_field(view, holder, value, on_change, width=44, rerender_on_leave=True)
    entry.pack(side="left")
    if kind in model.FILE_KINDS:
        def browse() -> None:
            chosen = pick_file(view, kind)
            if chosen:
                view.edit(lambda: on_change(chosen), rerender=True)
        ttk.Button(holder, text="Browse", command=browse).pack(side="left", padx=4)
    label = "Projects table" if kind == "dbtable" else "File"
    hint = ("A table already in the project database." if kind == "dbtable"
            else "Relative to the draft's folder (YAMLs/temp/ for an intake).")
    grid_row(parent, row, label, holder, hint)


def column_editor(view: AuthorView, parent: Any, index: int) -> None:
    """An upload's columns: rename, type and drop each (D98), or type in the
    columns of one nothing here can read (D97)."""
    draft = view.draft
    upload = draft.doc["upload_cohorts"][index]
    if draft.columns_readable(index):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(6, 0))
        for col, text in enumerate(("File column", "Lands as", "Type (optional)", "")):
            ttk.Label(frame, text=text, foreground=COLOURS["muted"]).grid(row=0, column=col, sticky="w", padx=4)
        for row, column in enumerate(draft.column_rows(index), start=1):
            ttk.Label(frame, text=column.source).grid(row=row, column=0, sticky="w", padx=4)
            name = text_field(view, frame, column.name,
                              lambda value, s=column.source: draft.rename_column(index, s, value), width=26)
            name.grid(row=row, column=1, sticky="w", padx=4)
            kind = text_field(view, frame, column.type,
                              lambda value, s=column.source: draft.set_column_type(index, s, value), width=16)
            kind.grid(row=row, column=2, sticky="w", padx=4)
            check_field(view, frame, "Drop", column.dropped,
                        lambda on, s=column.source: draft.drop_column(index, s, on), rerender=True
                        ).grid(row=row, column=3, sticky="w", padx=4)
            if column.dropped:
                name.state(["disabled"])
                kind.state(["disabled"])
        note(parent, "Columns not changed land as they are. A type converts the column as it lands; "
                     "BIGINT for IDs written as decimals.").pack(anchor="w", pady=(4, 0))
        return
    listed = ", ".join(r.name for r in draft.column_rows(index))
    why = ("A table in the project database cannot be read from here." if upload.get("file_type") == "dbtable"
           else "The file is not here, so its columns cannot be read.")
    note(parent, f"{why} Type its columns, separated by commas, so splitters and bindings can be checked (D97).",
         "pending" if upload.get("pending_transfer") else "warning").pack(anchor="w", pady=(6, 2))
    entry = text_field(view, parent, listed, lambda value: draft.set_listed_columns(index, value), width=90,
                       rerender_on_leave=True)
    entry.pack(anchor="w")


def var_editor(view: AuthorView, parent: Any, index: int) -> None:
    """A table's variables, each saying where an unset value comes from (D96)."""
    rows = view.draft.var_rows(index)
    if not rows:
        return
    frame = ttk.Frame(parent)
    frame.pack(fill="x", pady=(6, 0))
    ttk.Label(frame, text="Variables", foreground=COLOURS["muted"]).grid(row=0, column=0, sticky="w")
    for row, var in enumerate(rows, start=1):
        ttk.Label(frame, text=var.name).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        text_field(view, frame, var.value, lambda value, n=var.name: view.draft.set_var(index, n, value),
                   width=40, rerender_on_leave=True).grid(row=row, column=1, sticky="w")
        if var.value:
            hint, colour = "set here", "muted"
        elif var.required:
            hint, colour = "required: a value, or several separated by commas", "error"
        else:
            hint, colour = var.source, "muted"
        note(frame, hint, colour).grid(row=row, column=2, sticky="w", padx=(10, 0))


# =============================================================================
# Project
# =============================================================================


def build_project(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "Project", "Where the pull reads from and lands, and the dates every table uses.")
    form = ttk.Frame(parent)
    form.pack(fill="x")
    cosmos, sneakpeek = draft.pull_from()
    pull = ttk.Frame(form)
    state = {"cosmos": cosmos, "sneakpeek": sneakpeek}

    def toggle(which: str, on: bool) -> None:
        state[which] = on
        draft.set_pull_from(state["cosmos"], state["sneakpeek"])

    check_field(view, pull, "Cosmos", cosmos, lambda on: toggle("cosmos", on)).pack(side="left")
    check_field(view, pull, "Cosmos_SneakPeek", sneakpeek, lambda on: toggle("sneakpeek", on)).pack(side="left", padx=10)
    grid_row(form, 0, "Pull from", pull, "Both: every SneakPeek table first, then Cosmos (Dual).")
    grid_row(form, 1, "Project DB", text_field(view, form, draft.project_db,
                                                 lambda v: setattr(draft, "project_db", v)),
             "Must start with PROJECTD.")
    start, end = draft.dates()
    grid_row(form, 2, "Min start date", text_field(view, form, start, lambda v: draft.set_dates(min_date=v), 12),
             "YYYYMMDD")
    grid_row(form, 3, "Max start date", text_field(view, form, end, lambda v: draft.set_dates(max_date=v), 12),
             "YYYYMMDD")

    ttk.Separator(parent).pack(fill="x", pady=10)
    sample = ttk.Frame(parent)
    sample.pack(fill="x")
    check_field(view, sample, "Collect all patients matching criteria", draft.collect_all,
                lambda on: setattr(draft, "collect_all", on), rerender=True).grid(row=0, column=0, columnspan=3, sticky="w")
    size = text_field(view, sample, draft.sample_size, lambda v: setattr(draft, "sample_size", v), 10)
    grid_row(sample, 1, "Sample size", size, "PK rows, for a trial run.")
    random_box = check_field(view, sample, "Random sample", draft.random_sample,
                             lambda on: setattr(draft, "random_sample", on))
    grid_row(sample, 2, "", random_box, "The same rows every run, chosen by a hash of the key.")
    if draft.collect_all:
        size.state(["disabled"])
        random_box.state(["disabled"])


# =============================================================================
# PK Table
# =============================================================================

PK_KINDS = (
    ("recipe", "Prefabricated"),
    ("dictionary", "Table from the dictionary"),
    ("parquet", "Parquet"),
    ("csv", "CSV"),
    ("dbtable", "Projects table (dbtable)"),
)


def build_pk(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "PK Table",
            "The one table every other table is pulled for: a recipe, a table built from the dictionary, "
            "or a list you were given.")
    pk = draft.pk()
    if pk is None:
        note(parent, "No PK Table yet. Choose where it comes from.", "warning").pack(anchor="w")
    else:
        index = pk.index
        box = entry_box(view, parent, f"{dict(PK_KINDS)[pk.kind]}: {pk.name}", "pk", index)
        form = ttk.Frame(box)
        form.pack(fill="x")
        grid_row(form, 0, "Name", text_field(view, form, pk.name, lambda v: draft.update_pk(name=v)))
        if pk.kind == "recipe":
            grid_row(form, 1, "Recipe", ttk.Label(form, text=pk.recipe))
        if pk.where == "upload_cohorts":
            location_row(view, form, 2, pk.kind, pk.location, lambda v: draft.update_pk(location=v))
            grid_row(form, 3, "Key columns", text_field(view, form, ", ".join(pk.key_columns),
                                                        lambda v: draft.update_pk(key_columns=v), 40),
                     "The columns that identify a row, separated by commas.")
            if pk.kind in model.FILE_KINDS:
                grid_row(form, 4, "", check_field(view, form, "Pending transfer to the VM", pk.pending_transfer,
                                                  lambda on: draft.update_pk(pending_transfer=on), rerender=True),
                         "The file will only exist on the VM (D97).")
            column_editor(view, box, index)
        else:
            if pk.kind == "dictionary":
                ttk.Button(box, text="Edit in the table builder",
                           command=lambda: open_table_builder(view, index)).pack(anchor="w", pady=(6, 0))
            var_editor(view, box, index)
        ttk.Button(box, text="Remove PK", command=lambda: view.edit(draft.clear_pk, rerender=True)).pack(anchor="e")
    ttk.Separator(parent).pack(fill="x", pady=10)
    choose_pk(view, parent, replacing=pk is not None)


def choose_pk(view: AuthorView, parent: Any, replacing: bool) -> None:
    draft = view.draft
    box = ttk.LabelFrame(parent, text="Replace the PK with" if replacing else "Choose the PK", padding=10)
    box.pack(fill="x")
    kind_var = tk.StringVar(value=getattr(view, "_pk_kind", "recipe"))
    keep(box, kind_var)
    kinds = ttk.Frame(box)
    kinds.pack(anchor="w")
    for key, label in PK_KINDS:
        ttk.Radiobutton(kinds, text=label, value=key, variable=kind_var,
                        command=lambda: (setattr(view, "_pk_kind", kind_var.get()), view.render())
                        ).pack(side="left", padx=(0, 10))
    kind = kind_var.get()
    form = ttk.Frame(box)
    form.pack(fill="x", pady=(8, 0))

    def confirm() -> bool:
        return not replacing or messagebox.askyesno("Replace the PK", "A template has one PK. Replace it?")

    if kind == "recipe":
        names = view.ws.pk_recipes()
        if not names:
            note(form, view.ws.recipes_problem() or "recipes.yaml has no PK recipes.", "warning").pack(anchor="w")
            return
        recipe = tk.StringVar(value=names[0])
        name = tk.StringVar()
        keep(form, recipe, name)
        grid_row(form, 0, "Recipe", ttk.Combobox(form, textvariable=recipe, values=names, state="readonly", width=30))
        grid_row(form, 1, "Name", ttk.Entry(form, textvariable=name, width=30), "Blank: the recipe's name.")
        ttk.Button(form, text="Use as PK", command=lambda: confirm() and view.edit(
            lambda: draft.set_pk_recipe(recipe.get(), name.get()), rerender=True)).grid(row=2, column=1, sticky="w", pady=6)
    elif kind == "dictionary":
        note(form, "Build the table in the table builder; it becomes the PK when you add it.").pack(anchor="w")
        ttk.Button(form, text="Open the table builder",
                   command=lambda: confirm() and open_table_builder(view, None, pk=True)).pack(anchor="w", pady=6)
    else:
        name, location, keys = tk.StringVar(), tk.StringVar(), tk.StringVar()
        pending = tk.BooleanVar(value=False)
        keep(form, name, location, keys, pending)
        grid_row(form, 0, "Name", ttk.Entry(form, textvariable=name, width=30))
        holder = ttk.Frame(form)
        ttk.Entry(holder, textvariable=location, width=44).pack(side="left")
        if kind in model.FILE_KINDS:
            ttk.Button(holder, text="Browse",
                       command=lambda: location.set(pick_file(view, kind) or location.get())).pack(side="left", padx=4)
        grid_row(form, 1, "Projects table" if kind == "dbtable" else "File", holder)
        grid_row(form, 2, "Key columns", ttk.Entry(form, textvariable=keys, width=40),
                 "The columns that identify a row.")
        row = 3
        if kind in model.FILE_KINDS:
            grid_row(form, row, "", ttk.Checkbutton(form, text="Pending transfer to the VM", variable=pending))
            row += 1
        ttk.Button(form, text="Use as PK", command=lambda: confirm() and view.edit(
            lambda: draft.set_pk_upload(kind, name.get(), location.get(), keys.get(), pending.get()),
            rerender=True)).grid(row=row, column=1, sticky="w", pady=6)


# =============================================================================
# Supporting Tables
# =============================================================================


def build_supporting(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "Supporting Tables",
            "Tables you bring: a parquet, a CSV (converted to parquet at the split) or a Projects table. "
            "Each lands in Projects as upload_<name>; its columns can be renamed and dropped on the way.")
    for index, upload in draft.supporting():
        kind = str(upload.get("file_type") or "parquet").lower()
        box = entry_box(view, parent, f"{upload.get('name')} ({kind})", "supporting", index)
        form = ttk.Frame(box)
        form.pack(fill="x")
        grid_row(form, 0, "Name", text_field(view, form, upload.get("name"),
                                             lambda v, i=index: draft.update_supporting(i, name=v)))
        grid_row(form, 1, "Destination", text_field(view, form, upload.get("dest_table") or upload.get("name"),
                                                    lambda v, i=index: draft.update_supporting(i, dest_table=v)),
                 "The table name recipes bind to.")
        grid_row(form, 2, "Type", choice_field(view, form, list(model.UPLOAD_KINDS), kind,
                                               lambda v, i=index: draft.update_supporting(i, file_type=v),
                                               width=12, rerender=True))
        location = upload.get("source_table") if kind == "dbtable" else upload.get("file_loc")
        location_row(view, form, 3, kind, str(location or ""),
                     lambda v, i=index: draft.update_supporting(i, location=v))
        if kind in model.FILE_KINDS:
            grid_row(form, 4, "", check_field(
                view, form, "Pending transfer to the VM", upload.get("pending_transfer") is True,
                lambda on, i=index: draft.update_supporting(i, pending_transfer=on), rerender=True),
                "The file will only exist on the VM (D97).")
        column_editor(view, box, index)
        ttk.Button(box, text="Remove", command=lambda i=index: view.edit(
            lambda: draft.remove_supporting(i), rerender=True)).pack(anchor="e", pady=(6, 0))
    if not draft.supporting():
        note(parent, "No supporting tables.").pack(anchor="w")

    add = ttk.LabelFrame(parent, text="Add a supporting table", padding=10)
    add.pack(fill="x", pady=(10, 0))
    kind, name, location = tk.StringVar(value="parquet"), tk.StringVar(), tk.StringVar()
    pending = tk.BooleanVar(value=False)
    keep(add, kind, name, location, pending)
    ttk.Combobox(add, textvariable=kind, values=list(model.UPLOAD_KINDS), state="readonly", width=10).pack(side="left")
    ttk.Entry(add, textvariable=name, width=22).pack(side="left", padx=4)
    ttk.Entry(add, textvariable=location, width=36).pack(side="left", padx=4)
    ttk.Button(add, text="Browse", command=lambda: location.set(
        pick_file(view, kind.get()) or location.get())).pack(side="left")
    ttk.Checkbutton(add, text="Pending transfer", variable=pending).pack(side="left", padx=8)
    ttk.Button(add, text="Add", command=lambda: view.edit(
        lambda: draft.add_supporting(kind.get(), name.get(), location.get(),
                                     pending.get() and kind.get() in model.FILE_KINDS),
        rerender=True)).pack(side="left")
    note(parent, "Name, then the file (or the Projects table's name).").pack(anchor="w")


# =============================================================================
# The table builder: a PK or fact table from the dictionary
# =============================================================================


def open_table_builder(view: AuthorView, index: int | None, pk: bool = False) -> None:
    """A window over model.TableBuilder; Add puts the table into the draft."""
    try:
        builder = model.TableBuilder(view.draft, index, pk=pk)
    except model.DraftError as exc:
        view.say(str(exc), "error")
        return
    if not builder.tables():
        messagebox.showwarning("Table builder", "The data dictionary could not be read, so there are no "
                               f"tables to build from ({view.ws.dictionary_path}).")
        return
    view._last_builder = TableBuilderWindow(view, builder)


class TableBuilderWindow:
    def __init__(self, view: AuthorView, builder: model.TableBuilder):
        self.view = view
        self.b = builder
        self.win = tk.Toplevel(view.root)
        self.win.title(("Edit " if builder.index is not None else "New ") + ("PK table" if builder.pk else "fact table"))
        self.win.geometry("1100x800")
        self.body = ScrollFrame(self.win)
        self.body.pack(fill="both", expand=True)
        bar = ttk.Frame(self.win, padding=8)
        bar.pack(fill="x", side="bottom")
        self.message = note(bar, "")
        self.message.pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.win.destroy).pack(side="right")
        ttk.Button(bar, text="Save Changes" if builder.index is not None else "Add Table",
                   command=self.commit).pack(side="right", padx=6)
        self.render()

    def say(self, text: str, kind: str = "error") -> None:
        self.message.configure(text=text, foreground=COLOURS.get(kind, kind))

    def act(self, change: Callable[[], Any]) -> None:
        try:
            change()
        except (model.DraftError, IndexError, ValueError) as exc:
            self.say(str(exc))
            return
        self.say("")
        self.render()

    def render(self) -> None:
        self.body.clear()
        b = self.b
        parent = self.body.inner
        form = ttk.Frame(parent)
        form.pack(fill="x")
        fields = (("Name", "name"), ("Destination", "dest_table"), ("Description", "description"),
                  ("Granularity", "granularity"))
        for row, (label, attr) in enumerate(fields):
            var = tk.StringVar(value=getattr(b, attr))
            entry = ttk.Entry(form, textvariable=var, width=60)
            keep(entry, var)
            var.trace_add("write", lambda *a, v=var, n=attr: setattr(b, n, v.get()))
            grid_row(form, row, label, entry)
        pull = tk.BooleanVar(value=b.pull_this_cycle)
        box = ttk.Checkbutton(form, text="Pull this cycle", variable=pull,
                              command=lambda: setattr(b, "pull_this_cycle", bool(pull.get())))
        keep(box, pull)
        grid_row(form, len(fields), "", box)
        table = tk.StringVar(value=b.from_table)
        pick = ttk.Combobox(form, textvariable=table, values=b.tables(), state="readonly", width=40)
        keep(pick, table)
        pick.bind("<<ComboboxSelected>>", lambda e: self.act(lambda: b.set_from_table(table.get())))
        grid_row(form, len(fields) + 1, "Rows from", pick, "A table in the data dictionary; every column is added.")
        if not b.from_table:
            return
        alias = tk.StringVar(value=b.alias)
        alias_entry = ttk.Entry(form, textvariable=alias, width=10)
        keep(alias_entry, alias)
        alias_entry.bind("<Return>", lambda e: self.act(lambda: b.set_alias(alias.get())))
        alias_entry.bind("<FocusOut>", lambda e: alias.get() != b.alias and self.act(lambda: b.set_alias(alias.get())))
        grid_row(form, len(fields) + 2, "Alias", alias_entry)
        self.columns(parent)
        self.joins(parent)
        self.wheres(parent)

    def columns(self, parent: Any) -> None:
        b = self.b
        box = ttk.LabelFrame(parent, text="Columns", padding=8)
        box.pack(fill="x", pady=8)
        dictionary = b.dictionary_columns()
        for col, text in enumerate(("#", "Column", "Output name", "Description (for contents.md)", "")):
            ttk.Label(box, text=text, foreground=COLOURS["muted"]).grid(row=0, column=col, sticky="w", padx=3)
        for row, column in enumerate(b.columns, start=1):
            order = tk.StringVar(value=str(row))
            spot = ttk.Entry(box, textvariable=order, width=4)
            keep(spot, order)
            spot.bind("<Return>", lambda e, i=row - 1, v=order: v.get().strip().isdigit()
                      and self.act(lambda: b.move_column(i, int(v.get()))))
            spot.grid(row=row, column=0, padx=3, pady=1)
            source = b.source_column(column)
            meta = dictionary.get(source) or {}
            ttk.Label(box, text=f"{source}  ({column.get('type', '')})").grid(row=row, column=1, sticky="w", padx=3)
            for col, key, width in ((2, "name", 26), (3, "description", 50)):
                var = tk.StringVar(value=str(column.get(key) or ""))
                entry = ttk.Entry(box, textvariable=var, width=width)
                keep(entry, var)
                var.trace_add("write", lambda *a, i=row - 1, k=key, v=var: b.set_column(i, **{k: v.get()}))
                entry.grid(row=row, column=col, sticky="w", padx=3)
            ttk.Button(box, text="Remove", command=lambda i=row - 1: self.act(lambda: b.remove_column(i))
                       ).grid(row=row, column=4, padx=3)
            if not column.get("description") and meta.get("description"):
                note(box, "blank: the dictionary's description").grid(row=row, column=5, sticky="w")
        note(box, "Type a position and press Enter to move a column there; 1 is the top.").grid(
            row=len(b.columns) + 1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        removed = b.removed_columns()
        if removed:
            back = tk.StringVar(value=removed[0])
            menu = ttk.Combobox(box, textvariable=back, values=removed, state="readonly", width=30)
            keep(menu, back)
            menu.grid(row=len(b.columns) + 2, column=1, sticky="w", pady=4)
            ttk.Button(box, text="Add back", command=lambda: self.act(lambda: b.restore_column(back.get()))
                       ).grid(row=len(b.columns) + 2, column=2, sticky="w")

    def joins(self, parent: Any) -> None:
        b = self.b
        box = ttk.LabelFrame(parent, text="Joins", padding=8)
        box.pack(fill="x", pady=8)
        for i, join in enumerate(b.joins):
            line = ttk.Frame(box)
            line.pack(fill="x")
            ttk.Label(line, text=b.join_line(join)).pack(side="left")
            ttk.Button(line, text="Remove", command=lambda i=i: self.act(lambda: b.remove_join(i))).pack(side="right")
        note(box, "INNER keeps only rows that match; LEFT keeps every row of this table, matched or not.").pack(anchor="w")
        tables = b.join_tables()
        form = ttk.Frame(box)
        form.pack(fill="x", pady=4)
        kind = tk.StringVar(value="INNER")
        base = tk.StringVar(value=b.source_column(b.columns[0]) if b.columns else "")
        other = tk.StringVar(value=tables[0][0] if tables else "")
        column = tk.StringVar()
        check = note(form, "")
        keep(form, kind, base, other, column)
        base_names = [b.source_column(c) for c in b.columns]
        ttk.Combobox(form, textvariable=kind, values=list(model.JOIN_TYPES), width=7, state="readonly").pack(side="left")
        ttk.Combobox(form, textvariable=base, values=base_names, width=22, state="readonly").pack(side="left", padx=3)
        ttk.Label(form, text="=").pack(side="left")
        table_box = ttk.Combobox(form, textvariable=other, values=[t[0] for t in tables], width=22, state="readonly")
        table_box.pack(side="left", padx=3)
        column_box = ttk.Combobox(form, textvariable=column, width=22, state="readonly")
        column_box.pack(side="left", padx=3)

        def columns_of_table(*_: Any) -> None:
            found = next((t for t in tables if t[0] == other.get()), None)
            column_box.configure(values=[c for c, _ in found[2]] if found else [])
            update_check()

        def update_check(*_: Any) -> None:
            if base.get() in base_names and other.get() and column.get():
                ok, text = b.join_check(base_names.index(base.get()), other.get(), column.get())
                check.configure(text=text, foreground=COLOURS["pass" if ok else "error"])

        table_box.bind("<<ComboboxSelected>>", columns_of_table)
        column_box.bind("<<ComboboxSelected>>", update_check)
        ttk.Button(form, text="Add join", command=lambda: self.act(lambda: b.add_join(
            base_names.index(base.get()) if base.get() in base_names else -1, other.get(), column.get(), kind.get())
        )).pack(side="left", padx=6)
        check.pack(side="left")
        columns_of_table()
        text = tk.StringVar()
        written = ttk.Frame(box)
        written.pack(fill="x", pady=(6, 0))
        keep(written, text)
        ttk.Entry(written, textvariable=text, width=90).pack(side="left")
        ttk.Button(written, text="Add written join", command=lambda: self.act(lambda: b.add_join_text(text.get()))
                   ).pack(side="left", padx=6)
        note(box, "A written join reaches a Cosmos table: INNER JOIN PatientDim AS p ON p.DurableKey = "
                  f"{b.alias}.PatientDurableKey").pack(anchor="w")

    def wheres(self, parent: Any) -> None:
        b = self.b
        box = ttk.LabelFrame(parent, text="Where", padding=8)
        box.pack(fill="x", pady=8)
        for i, line in enumerate(b.wheres):
            row = ttk.Frame(box)
            row.pack(fill="x", pady=1)
            var = tk.StringVar(value=line)
            entry = ttk.Entry(row, textvariable=var, width=100)
            keep(entry, var)
            var.trace_add("write", lambda *a, i=i, v=var: b.set_where(i, v.get()))
            entry.pack(side="left")
            ttk.Button(row, text="Remove", command=lambda i=i: self.act(lambda: b.remove_where(i))).pack(side="left", padx=4)
        ttk.Button(box, text="Add a condition", command=lambda: self.act(b.add_where)).pack(anchor="w", pady=(4, 0))
        note(box, "Dates as {{min_date_key}} and {{max_date_key}}; a variable as {{Name}}.").pack(anchor="w")

    def commit(self) -> None:
        try:
            self.b.commit()
        except model.DraftError as exc:
            self.say(str(exc))
            return
        self.win.destroy()
        self.view.changed(rerender=True)


SECTION_BUILDERS: dict[str, Callable[[AuthorView, Any], None]] = {
    "project": build_project,
    "pk": build_pk,
    "supporting": build_supporting,
}


# =============================================================================
# Tests: the view on a real Tk, withdrawn; skipped where there is no display
# =============================================================================

import shutil  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

REPO_YAMLS = SCRIPT_DIR.parent / "YAMLs"


def tk_root() -> Any:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def widgets(parent: Any) -> list[Any]:
    out = []
    for child in parent.winfo_children():
        out.append(child)
        out.extend(widgets(child))
    return out


class ViewTest(unittest.TestCase):
    # One Tk for the whole class: a new Tk per test can hang on the Mac.
    root: Any = None

    @classmethod
    def setUpClass(cls):
        cls.root = tk_root()

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            cls.root.destroy()

    def setUp(self):
        if self.root is None:
            self.skipTest("no display for Tk")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(REPO_YAMLS / "temp", self.tmp / "YAMLs" / "temp",
                        ignore=shutil.ignore_patterns("bundle_queue.txt"))
        for name in ("recipes.yaml", "datadictionary.yaml", "template.yaml"):
            shutil.copy(REPO_YAMLS / name, self.tmp / "YAMLs" / name)
        self.ws = model.Workspace(self.tmp, self.tmp / "YAMLs" / "recipes.yaml",
                                  self.tmp / "YAMLs" / "datadictionary.yaml", self.tmp / "YAMLs" / "template.yaml")
        frame = ttk.Frame(self.root)
        frame.pack()
        self.transferred: list[Path] = []
        self.view = AuthorView(frame, self.root, self.ws, on_transfer=self.transferred.append)
        self.addCleanup(self.close_view, frame)

    def close_view(self, frame: Any) -> None:
        if self.view._pending_check is not None:
            self.root.after_cancel(self.view._pending_check)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        frame.destroy()

    def pump(self, ms: int = VALIDATE_DELAY_MS + 200) -> None:
        self.root.after(ms, self.root.quit)
        self.root.mainloop()

    def open(self, name: str) -> None:
        self.view.draft = model.Draft.open(self.ws, self.ws.temp_dir / name)
        self.view.loaded()

    def entries(self) -> list[Any]:
        return [w for w in widgets(self.view.body.inner)
                if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox)]

    def type_into(self, current: str, text: str) -> None:
        entry = next(e for e in self.entries() if e.get() == current)
        entry.delete(0, "end")
        entry.insert(0, text)
        self.root.update()


class SectionViewTests(ViewTest):
    def test_every_section_builds_for_every_intake(self):
        for path in self.ws.intakes():
            self.open(path.name)
            for key, _ in SECTIONS:
                with self.subTest(intake=path.name, section=key):
                    self.view.show_section(key)
                    self.root.update()
                    self.assertTrue(widgets(self.view.body.inner))

    def test_typing_a_project_setting_edits_the_draft_and_marks_it_unsaved(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("project")
        self.type_into(self.view.draft.project_db, "PROJECTD123")
        self.assertEqual(self.view.draft.project_db, "PROJECTD123")
        self.assertTrue(self.view.draft.dirty)

    def test_a_column_renamed_in_the_view_is_renamed_in_the_template(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("supporting")
        self.type_into("DiagnosisCode", "ICDCode")
        self.assertEqual(self.view.draft.doc["upload_cohorts"][0]["columns"],
                         [{"name": "ICDCode", "from": "DiagnosisCode"}])

    def test_the_check_runs_after_an_edit_and_marks_the_section(self):
        self.open("Celiac_intake.yaml")
        self.pump()
        self.assertIn("Valid", self.view.status.cget("text"))
        self.view.edit(lambda: self.view.draft.set_pull_from(False, False))
        self.pump()
        self.assertIn("error", self.view.status.cget("text"))
        self.assertIn("●", self.view.nav.item("project", "text"))

    def test_a_refused_edit_is_said_not_raised(self):
        self.view.edit(lambda: self.view.draft.add_pieces_by_column("Sex"))
        self.assertIn("Choose a PK Table first", self.view.status.cget("text"))

    def test_the_table_builder_adds_a_fact_table(self):
        self.open("Celiac_intake.yaml")
        before = len(self.view.draft.fact_tables())
        open_table_builder(self.view, None)
        window = self.view._last_builder
        window.act(lambda: window.b.set_from_table("EncounterFact"))
        window.b.name = "Visits"
        window.commit()
        names = [c.get("name") for _, c in self.view.draft.fact_tables()]
        self.assertEqual(len(names), before + 1)
        self.assertEqual(names[-1], "Visits")

    def test_transfer_saves_exports_and_hands_the_file_on(self):
        self.open("Celiac_intake.yaml")
        self.view.edit(lambda: setattr(self.view.draft, "project_db", "PROJECTD456"))
        self.view.transfer()
        self.assertFalse(self.view.draft.dirty)
        self.assertEqual(len(self.transferred), 1)
        self.assertEqual(self.transferred[0].parent, self.tmp)
        self.assertIn("PROJECTD456", self.transferred[0].read_text(encoding="utf-8"))


def run_tdd(verbosity: int = 2) -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(SectionViewTests)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--tdd" in sys.argv[1:]:
        raise SystemExit(run_tdd())
    print(__doc__)
