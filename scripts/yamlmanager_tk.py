"""YAML Manager's Author half, in tkinter (D93).

A view over yamlmanager_model (D92): every value shown is read from the
model, every edit is a call to it, and it holds no rules of its own. The
Builder's sections follow D96; Validate, Exports and YAML sit beside it.

It is opened as one half of the app, beside Run (pullmanager/app.py):

    python pullmanager.py          # on the VM, in the working folder
    python3 datascope.py           # on the Mac, at the root (D112)

Standard library only (tkinter), as on the VM.
"""

from __future__ import annotations

import re
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
        self.inline_builder: model.TableBuilder | None = None  # the table being built in place (D110)

        self._build_top()
        self._build_tabs()
        self.status = ttk.Label(parent, text="", anchor="w", padding=(10, 4))
        self.status.pack(fill="x", side="bottom")
        self.sections: dict[str, Any] = {}
        self.section_builders: dict[str, Callable[[Any], None]] = {}
        for key, build in SECTION_BUILDERS.items():
            self.register(key, lambda parent, build=build: build(self, parent))
        self.validate_panel = ValidatePanel(self)
        self.refresh_validate = self.validate_panel.refresh
        self.exports_panel = ExportsPanel(self)
        self.refresh_exports = self.exports_panel.refresh
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

    def goto(self, field: model.FieldRef | None) -> None:
        """Open the Builder at the field a message points to, marked there."""
        if field is None or field.section not in dict(SECTIONS):
            self.say("That message names no field in the Builder; its fix says what to change.")
            return
        self.highlight = field
        self.tabs.select(self.builder_tab)
        self.show_section(field.section)

    def show_section(self, key: str) -> None:
        if key != self.section_key:
            self.highlight = None if self.highlight is None or self.highlight.section != key else self.highlight
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
            self.render_soon()
        self.schedule_check()

    def render_soon(self) -> None:
        """Render once Tk is idle: never from inside the event of a widget the
        render destroys (a combobox's own selection, say)."""
        if not getattr(self, "_render_queued", False):
            self._render_queued = True
            self.root.after_idle(self._render_now)

    def _render_now(self) -> None:
        self._render_queued = False
        if self.body.winfo_exists():
            self.render()

    def say(self, text: str, kind: str = "muted") -> None:
        self.status.configure(text=text, foreground=COLOURS.get(kind, kind))

    def schedule_check(self) -> None:
        if self._pending_check is not None:
            self.root.after_cancel(self._pending_check)
        self._pending_check = self.root.after(VALIDATE_DELAY_MS, self.check)

    def check(self) -> None:
        """Validate the draft and show the result everywhere it shows (D96)."""
        self._pending_check = None
        if not self.nav.winfo_exists():
            return
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
        if getattr(self, "exports_panel", None) is not None:
            self.exports_panel.chosen = None
        self.highlight = None
        self.inline_builder = None
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
    """A chosen file as the template writes it: relative to the draft's
    folder, `..` and all (D104)."""
    return view.draft.location_for(chosen)


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
            else "Relative to the draft's folder: Browse writes it so (D104).")
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


JOIN_EXPLAINED = ("INNER keeps only rows that match; LEFT keeps every row of this table, matched or not; "
                  "RIGHT and FULL keep the other table's rows too.")
# The operator dropdown (D119): each operator as itself, and the supporting-table test.
MODE_LABELS = {**{op: op for op in model.WHERE_OPERATORS}, "In supporting table": "in_table"}


def where_form(view: AuthorView, parent: Any, sources: list[tuple[str, str]],
               add: Callable[[str, str, str, str, str, str], Any]) -> None:
    """A where line by column: this table's column, an operator and its value
    (two for BETWEEN), or a supporting table's column the value must be in
    (D105, D119)."""
    form = ttk.Frame(parent)
    form.pack(fill="x", pady=(4, 0))
    names = [name for name, _ in sources]
    column, mode, value = tk.StringVar(value=names[0] if names else ""), tk.StringVar(value="="), tk.StringVar()
    higher, table, table_column = tk.StringVar(), tk.StringVar(), tk.StringVar()
    keep(form, column, mode, value, higher, table, table_column)
    ttk.Label(form, text="By column:").pack(side="left")
    ttk.Combobox(form, textvariable=column, values=names, state="readonly", width=24).pack(side="left", padx=4)
    ttk.Combobox(form, textvariable=mode, values=list(MODE_LABELS), state="readonly", width=18).pack(side="left", padx=4)
    value_box = ttk.Entry(form, textvariable=value, width=30)
    and_label = ttk.Label(form, text="and")
    higher_box = ttk.Entry(form, textvariable=higher, width=30)
    supporting = view.draft.supporting_columns()
    table_box = ttk.Combobox(form, textvariable=table, values=list(supporting), state="readonly", width=22)
    column_box = ttk.Combobox(form, textvariable=table_column, state="readonly", width=22)
    button = ttk.Button(form, text="Add where", command=lambda: add(
        dict(sources).get(column.get(), ""), MODE_LABELS[mode.get()], value.get(), table.get(), table_column.get(),
        higher.get()))

    def show(*_: Any) -> None:
        for widget in (value_box, and_label, higher_box, table_box, column_box, button):
            widget.pack_forget()
        if mode.get() in model.WHERE_OPERATORS:
            value_box.pack(side="left", padx=4)
            if mode.get() == "BETWEEN":
                and_label.pack(side="left")
                higher_box.pack(side="left", padx=4)
        else:
            table_box.pack(side="left", padx=4)
            column_box.pack(side="left", padx=4)
        button.pack(side="left", padx=6)

    table_box.bind("<<ComboboxSelected>>", lambda e: column_box.configure(values=supporting.get(table.get(), [])))
    mode.trace_add("write", show)
    show()
    note(parent, "IN takes values separated by commas, LIKE a pattern with %, BETWEEN a lower and a higher. "
                 "Numbers and {{Variables}} are written as they are, text in quotes. In supporting table: the "
                 "column's value must be in that table's column, as a code list uploaded for the pull.").pack(anchor="w")


def join_form(view: AuthorView, parent: Any, sources: list[tuple[str, str]], exclude: int | None,
              check: Callable[[str, str, str], tuple[bool, str]],
              add: Callable[[str, str, str, str, str], Any]) -> None:
    """A join by column to another table of the template: its type (with what
    each keeps), the operator, and a check that the types match (D105)."""
    form = ttk.Frame(parent)
    form.pack(fill="x", pady=(4, 0))
    tables = view.draft.template_tables(exclude)
    names = [name for name, _ in sources]
    kind, column, operator = tk.StringVar(value="INNER"), tk.StringVar(value=names[0] if names else ""), tk.StringVar(value="=")
    other, other_column = tk.StringVar(value=tables[0][0] if tables else ""), tk.StringVar()
    keep(form, kind, column, operator, other, other_column)
    ttk.Combobox(form, textvariable=kind, values=list(model.JOIN_TYPES), width=7, state="readonly").pack(side="left")
    ttk.Label(form, text="JOIN by column:").pack(side="left", padx=(4, 0))
    ttk.Combobox(form, textvariable=column, values=names, width=22, state="readonly").pack(side="left", padx=4)
    ttk.Combobox(form, textvariable=operator, values=list(model.JOIN_OPERATORS), width=4, state="readonly").pack(side="left")
    table_box = ttk.Combobox(form, textvariable=other, values=[t[0] for t in tables], width=22, state="readonly")
    table_box.pack(side="left", padx=4)
    column_box = ttk.Combobox(form, textvariable=other_column, width=22, state="readonly")
    column_box.pack(side="left", padx=4)
    result = note(form, "")

    def columns_of_table(*_: Any) -> None:
        found = next((t for t in tables if t[0] == other.get()), None)
        column_box.configure(values=[c for c, _ in found[2]] if found else [])
        update()

    def update(*_: Any) -> None:
        if column.get() and other.get() and other_column.get():
            ok, text = check(column.get(), other.get(), other_column.get())
            result.configure(text=text, foreground=COLOURS["pass" if ok else "error"])
        else:
            result.configure(text="")

    table_box.bind("<<ComboboxSelected>>", columns_of_table)
    column_box.bind("<<ComboboxSelected>>", update)
    # This table's column counts as much as the other's: a stale "match" misleads.
    column.trace_add("write", update)
    ttk.Button(form, text="Add join", command=lambda: add(
        kind.get(), column.get(), operator.get(), other.get(), other_column.get())).pack(side="left", padx=6)
    result.pack(side="left")
    columns_of_table()
    note(parent, JOIN_EXPLAINED).pack(anchor="w")


def filter_editor(view: AuthorView, parent: Any, index: int) -> None:
    """A table's own lines, added to its recipe's (D105): where by column,
    joins to the template's tables, each removable."""
    draft = view.draft
    box = ttk.LabelFrame(parent, text="Filters added to this table", padding=8)
    box.pack(fill="x", pady=(8, 0))
    for kind in ("where", "join"):
        for position, line in enumerate(draft.added_lines(index, kind)):
            row = ttk.Frame(box)
            row.pack(fill="x")
            ttk.Label(row, text=f"{kind}: {line}").pack(side="left")
            ttk.Button(row, text="Remove", command=lambda k=kind, i=position: view.edit(
                lambda: draft.remove_line(index, k, i), rerender=True)).pack(side="right")
    sources = [(name, source) for name, source, _ in draft.filter_sources(index)]
    types = {source: kind for _, source, kind in draft.filter_sources(index)}
    if not sources:
        note(box, "This table's columns are not known, so filters are written in the YAML.").pack(anchor="w")
        return
    where_form(view, box, sources, lambda source, mode, value, table, column, higher: view.edit(
        lambda: draft.add_where_by_column(index, source, mode, value, table, column, higher), rerender=True))
    lookup = dict(sources)
    join_form(view, box, sources, index,
              lambda name, table, column: draft.join_check(types.get(lookup.get(name, ""), ""), table, column, index),
              lambda kind, name, operator, table, column: view.edit(
                  lambda: draft.add_join_to(index, lookup.get(name, ""), table, column, kind, operator), rerender=True))


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
            grid_row(form, 3, "Row key", text_field(view, form, ", ".join(pk.key_columns),
                                                    lambda v: draft.update_pk(key_columns=v), 40),
                     "The columns that make each row one of its own: checked unique before any batch; chunks, the random sample and controls follow it (D107).")
            if pk.kind in model.FILE_KINDS and not view.ws.vm_side:
                grid_row(form, 4, "", check_field(view, form, "Pending transfer to the VM", pk.pending_transfer,
                                                  lambda on: draft.update_pk(pending_transfer=on), rerender=True),
                         "The file will only exist on the VM (D97).")
            column_editor(view, box, index)
        else:
            if pk.kind == "dictionary":
                editing = view.inline_builder
                if editing is not None and editing.index == index:
                    TableBuilderPanel(view, editing, box)
                else:
                    ttk.Button(box, text="Edit",
                               command=lambda: open_table_builder(view, index)).pack(anchor="w", pady=(6, 0))
            var_editor(view, box, index)
            if pk.kind == "recipe":
                filter_editor(view, box, index)
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
        builder = view.inline_builder
        if builder is not None and builder.pk and builder.index is None:
            TableBuilderPanel(view, builder, form)
            return
        table, name = tk.StringVar(), tk.StringVar()
        keep(form, table, name)
        tables = model.TableBuilder(draft).tables()
        pick = ttk.Combobox(form, textvariable=table, values=tables, state="readonly", width=34)
        grid_row(form, 0, "Rows from", pick, "A table in the data dictionary: the form opens here.")
        grid_row(form, 1, "Name", ttk.Entry(form, textvariable=name, width=30))
        pick.bind("<<ComboboxSelected>>", lambda e: confirm() and open_table_builder(
            view, None, pk=True, table=table.get(), name=name.get()))
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
        grid_row(form, 2, "Row key", ttk.Entry(form, textvariable=keys, width=40),
                 "Blank: PatientDurableKey, if the file has it. " + "The columns that make each row one of its own: checked unique before any batch; chunks, the random sample and controls follow it (D107).")
        row = 3
        if kind in model.FILE_KINDS and not view.ws.vm_side:
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
        if kind in model.FILE_KINDS and not view.ws.vm_side:
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
    if not view.ws.vm_side:
        ttk.Checkbutton(add, text="Pending transfer", variable=pending).pack(side="left", padx=8)
    ttk.Button(add, text="Add", command=lambda: view.edit(
        lambda: draft.add_supporting(kind.get(), name.get(), location.get(),
                                     pending.get() and kind.get() in model.FILE_KINDS),
        rerender=True)).pack(side="left")
    note(parent, "Name, then the file (or the Projects table's name).").pack(anchor="w")


# =============================================================================
# The table builder: a PK or fact table from the dictionary
# =============================================================================


def open_table_builder(view: AuthorView, index: int | None, pk: bool = False,
                       table: str = "", name: str = "") -> None:
    """Open model.TableBuilder inline (D110): in Fact Tables' add panel, or
    the PK Table's; Add puts the table into the draft."""
    try:
        builder = model.TableBuilder(view.draft, index, pk=pk)
        if table:
            builder.set_from_table(table)
        if name:
            builder.name = name
    except model.DraftError as exc:
        view.say(str(exc), "error")
        return
    if not builder.tables():
        messagebox.showwarning("Table builder", "The data dictionary could not be read, so there are no "
                               f"tables to build from ({view.ws.dictionary_path}).")
        return
    view.inline_builder = builder
    if builder.pk and index is None:
        view._pk_kind = "dictionary"  # the PK section shows the dictionary form
    view.tabs.select(view.builder_tab)
    view.show_section("pk" if builder.pk else "fact")


def shaded_box(parent: Any, title: str) -> ttk.Frame:
    """A frame set apart by a coloured border: where something is added (D110)."""
    outer = tk.Frame(parent, highlightthickness=2, highlightbackground=COLOURS["pending"],
                     highlightcolor=COLOURS["pending"], bd=0)
    outer.pack(fill="x", pady=(4, 10))
    inner = ttk.Frame(outer, padding=10)
    inner.pack(fill="x")
    ttk.Label(inner, text=title, font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0, 6))
    return inner


class TableBuilderPanel:
    """The table builder, drawn in place inside a section (D110)."""

    def __init__(self, view: AuthorView, builder: model.TableBuilder, parent: Any):
        self.view = view
        self.b = builder
        view._last_builder = self
        self.win = ttk.Frame(parent)
        self.win.pack(fill="x", pady=(6, 0))
        title = ("Editing " if builder.index is not None else "New ") + ("PK table" if builder.pk else "fact table")
        ttk.Label(self.win, text=title, foreground=COLOURS["muted"]).pack(anchor="w")
        self.frame = ttk.Frame(self.win)
        self.frame.pack(fill="x")
        bar = ttk.Frame(self.win, padding=(0, 8, 0, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save Changes" if builder.index is not None else "Add Table",
                   command=self.commit).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="left", padx=6)
        self.message = note(bar, "")
        self.message.pack(side="left", padx=6)
        self.render()

    def cancel(self) -> None:
        self.view.inline_builder = None
        self.view.render_soon()

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
        for child in self.frame.winfo_children():
            child.destroy()
        b = self.b
        parent = self.frame
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
        names = [b.source_column(c) for c in b.columns]
        sources = [(name, name) for name in names]
        join_form(self.view, box, sources, b.index,
                  lambda name, table, column: b.join_check(names.index(name), table, column)
                  if name in names else (False, "Choose a column of this table."),
                  lambda kind, name, operator, table, column: self.act(lambda: b.add_join(
                      names.index(name) if name in names else -1, table, column, kind, operator)))
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
            problem = ttk.Label(box, text=b.line_problem(line) or "", foreground="#b3261e")

            def typed(*_: Any, i: int = i, v: tk.StringVar = var, shown: ttk.Label = problem) -> None:
                b.set_where(i, v.get())
                # Checked as it is typed, and again at Done (D118).
                shown.configure(text=b.line_problem(v.get()) or "")

            var.trace_add("write", typed)
            entry.pack(side="left")
            ttk.Button(row, text="Remove", command=lambda i=i: self.act(lambda: b.remove_where(i))).pack(side="left", padx=4)
            problem.pack(anchor="w")
        names = [b.source_column(c) for c in b.columns]
        where_form(self.view, box, [(name, name) for name in names],
                   lambda source, mode, value, table, column, higher: self.act(lambda: b.add_where_by_column(
                       names.index(source) if source in names else -1, mode, value, table, column, higher)))
        ttk.Button(box, text="Add a written condition", command=lambda: self.act(b.add_where)).pack(anchor="w", pady=(4, 0))
        note(box, "Dates as {{min_date_key}} and {{max_date_key}}; a variable as {{Name}}.").pack(anchor="w")

    def commit(self) -> None:
        try:
            self.b.commit()
        except model.DraftError as exc:
            self.say(str(exc))
            return
        self.view.inline_builder = None
        self.view.changed(rerender=True)


# =============================================================================
# Multipliers
# =============================================================================


def build_multipliers(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "Multipliers",
            "Each level becomes its own set of tables, built with its own variables and named with its "
            "strat (UCPatients, CrohnsPatients). Multipliers stack: two of two levels make four sets.")
    for index, mult in draft.multipliers():
        box = entry_box(view, parent, f"Multiplier: {mult.get('name')}", "multipliers", index)
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Name").pack(side="left")
        text_field(view, head, mult.get("name"), lambda v, i=index: draft.rename_multiplier(i, v)).pack(side="left", padx=6)
        ttk.Button(head, text="Remove multiplier", command=lambda i=index: view.edit(
            lambda: draft.remove_multiplier(i), rerender=True)).pack(side="right")
        levels = ttk.Frame(box)
        levels.pack(fill="x", pady=(6, 0))
        ttk.Label(levels, text="Strat", foreground=COLOURS["muted"]).grid(row=0, column=0, sticky="w")
        ttk.Label(levels, text="Variables (; between them)", foreground=COLOURS["muted"]).grid(row=0, column=1, sticky="w")
        for row, level in enumerate(mult.get("levels") or [], start=1):
            text_field(view, levels, level.get("strat"), lambda v, i=index, li=row - 1: draft.set_level(i, li, strat=v),
                       width=16).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
            text_field(view, levels, model.level_vars_text(level.get("vars")),
                       lambda v, i=index, li=row - 1: draft.set_level(i, li, vars_text=v),
                       width=60).grid(row=row, column=1, sticky="w")
            ttk.Button(levels, text="Remove", command=lambda i=index, li=row - 1: view.edit(
                lambda: draft.remove_level(i, li), rerender=True)).grid(row=row, column=2, padx=6)
        ttk.Button(box, text="Add level", command=lambda i=index: view.edit(
            lambda: draft.add_level(i), rerender=True)).pack(anchor="w", pady=(6, 0))
        note(box, "For example: strat UC, variables ICD_Value: K51.%, K52.%").pack(anchor="w")
    if not draft.multipliers():
        note(parent, "No multipliers.").pack(anchor="w")
    add = ttk.Frame(parent)
    add.pack(fill="x", pady=(10, 0))
    name = tk.StringVar()
    keep(add, name)
    ttk.Entry(add, textvariable=name, width=24).pack(side="left")
    ttk.Button(add, text="Add multiplier", command=lambda: view.edit(
        lambda: draft.add_multiplier(name.get()), rerender=True)).pack(side="left", padx=6)
    note(add, "A name, such as IBDType.").pack(side="left")


# =============================================================================
# Splitters
# =============================================================================


def build_splitters(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "Splitters",
            "Two ways to split, and each splitter says which it is. Separate tables: each level gets its "
            "own PK, pulled in its own session, and its own tables (blackPatients, whitePatients), with an "
            "optional control sampled against its case. Pieces of one table: the pull runs in batches, by "
            "a PK column or by a number of rows, and the rows land in one table.")
    columns = draft.pk_columns()
    if draft.pk() is None:
        note(parent, "Choose a PK Table first: splitting by a column needs the PK's columns. "
                     "A chunk of rows works without one.", "warning").pack(anchor="w", pady=(0, 6))
    elif columns is None:
        note(parent, "The PK's columns are not known here: type them under PK Table to split by one.",
             "warning").pack(anchor="w", pady=(0, 6))
    column_values = columns or []
    for splitter in draft.splitters():
        if splitter.kind == "separate":
            separate_box(view, parent, splitter, column_values)
        else:
            pieces_box(view, parent, splitter, column_values)
    if not draft.splitters():
        note(parent, "No splitters.").pack(anchor="w")
    add_splitter(view, parent, column_values)


def separate_box(view: AuthorView, parent: Any, splitter: model.Splitter, columns: list[str]) -> None:
    draft = view.draft
    index = splitter.index
    box = entry_box(view, parent, f"Separate tables: {splitter.name}", "splitters", index)
    head = ttk.Frame(box)
    head.pack(fill="x")
    ttk.Label(head, text="Name").pack(side="left")
    text_field(view, head, splitter.name, lambda v: draft.rename_multiplier(index, v)).pack(side="left", padx=6)
    ttk.Button(head, text="Remove splitter", command=lambda: view.edit(
        lambda: draft.remove_splitter("multipliers", index), rerender=True)).pack(side="right")
    grid = ttk.Frame(box)
    grid.pack(fill="x", pady=(6, 0))
    for col, text in enumerate(("Strat", "PK column", "Values (% is a wildcard)", "Role", "Row mult")):
        ttk.Label(grid, text=text, foreground=COLOURS["muted"]).grid(row=0, column=col, sticky="w", padx=3)
    for row, level in enumerate(splitter.levels, start=1):
        li = row - 1
        text_field(view, grid, level.get("strat"), lambda v, li=li: draft.set_separate_level(index, li, strat=v),
                   width=14).grid(row=row, column=0, padx=3, pady=1)
        choice_field(view, grid, columns, str(level.get("column") or ""),
                     lambda v, li=li: draft.set_separate_level(index, li, column=v), width=22
                     ).grid(row=row, column=1, padx=3)
        text_field(view, grid, model.value_text(level.get("values")),
                   lambda v, li=li: draft.set_separate_level(index, li, values=v), width=30).grid(row=row, column=2, padx=3)
        choice_field(view, grid, ["", "control"], str(level.get("role") or ""),
                     lambda v, li=li: draft.set_separate_level(index, li, role=v), width=9
                     ).grid(row=row, column=3, padx=3)
        text_field(view, grid, level.get("row_mult") or "",
                   lambda v, li=li: draft.set_separate_level(index, li, row_mult=v), width=6).grid(row=row, column=4, padx=3)
        ttk.Button(grid, text="Remove", command=lambda li=li: view.edit(
            lambda: draft.remove_level(index, li), rerender=True)).grid(row=row, column=5, padx=3)
    add = ttk.Frame(box)
    add.pack(fill="x", pady=(6, 0))
    strat, column, values = tk.StringVar(), tk.StringVar(value=columns[0] if columns else ""), tk.StringVar()
    keep(add, strat, column, values)
    ttk.Entry(add, textvariable=strat, width=14).pack(side="left")
    pick = ttk.Combobox(add, textvariable=column, values=columns, width=22, state="readonly")
    pick.pack(side="left", padx=4)
    ttk.Entry(add, textvariable=values, width=30).pack(side="left")
    button = ttk.Button(add, text="Add level", command=lambda: view.edit(
        lambda: draft.add_separate_level(index, strat.get(), column.get(), values.get()), rerender=True))
    button.pack(side="left", padx=6)
    if not columns:
        pick.state(["disabled"])
        button.state(["disabled"])
    note(box, "A control (role control, row mult n) keeps n times its case's rows per batch, "
              "matched on the batching columns (D59).").pack(anchor="w")


def pieces_box(view: AuthorView, parent: Any, splitter: model.Splitter, columns: list[str]) -> None:
    draft = view.draft
    index = splitter.index
    if splitter.problem:
        box = entry_box(view, parent, f"Pieces: {splitter.name}", "splitters", index)
        note(box, splitter.problem, "error").pack(anchor="w")
    elif splitter.rows is not None or not splitter.column:
        box = entry_box(view, parent, "Pieces of one table: by rows", "splitters", index)
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Rows per piece").pack(side="left")
        text_field(view, row, splitter.rows or "", lambda v: draft.set_pieces(index, rows=v), width=10).pack(side="left", padx=6)
        note(box, "Each piece is this many PK rows, in the order of the PK's key; the pieces run in turn.").pack(anchor="w")
    else:
        box = entry_box(view, parent, f"Pieces of one table: by {splitter.column}", "splitters", index)
        form = ttk.Frame(box)
        form.pack(fill="x")
        grid_row(form, 0, "PK column", choice_field(view, form, columns or [splitter.column], splitter.column,
                                                    lambda v: draft.set_pieces(index, column=v), rerender=True))
        grid_row(form, 1, "Values", text_field(view, form, model.value_text(splitter.values),
                                               lambda v: draft.set_pieces(index, values=v or None), width=40),
                 "Blank: every value the PK has, each a piece (D82). Listed: one piece each, "
                 "and one piece of the rest.")
        grid_row(form, 2, "", check_field(view, form, "Separate parquets", splitter.separate_parquets,
                                          lambda on: draft.set_pieces(index, separate_parquets=on)),
                 "Artifacts writes one parquet per value instead of one per table.")
    ttk.Button(box, text="Remove splitter", command=lambda: view.edit(
        lambda: draft.remove_splitter("batching", index), rerender=True)).pack(anchor="e")


def add_splitter(view: AuthorView, parent: Any, columns: list[str]) -> None:
    draft = view.draft
    box = ttk.LabelFrame(parent, text="Add a splitter", padding=10)
    box.pack(fill="x", pady=(10, 0))
    kind = tk.StringVar(value=getattr(view, "_splitter_kind", "column"))
    keep(box, kind)
    kinds = ttk.Frame(box)
    kinds.pack(anchor="w")
    for value, label in (("separate", "Separate tables"), ("column", "Pieces of one table, by a PK column"),
                         ("chunk", "Pieces of one table, by rows")):
        ttk.Radiobutton(kinds, text=label, value=value, variable=kind, command=lambda: (
            setattr(view, "_splitter_kind", kind.get()), view.render())).pack(side="left", padx=(0, 12))
    form = ttk.Frame(box)
    form.pack(fill="x", pady=(8, 0))
    chosen = kind.get()
    if chosen == "chunk":
        rows = tk.StringVar(value="100000")
        keep(form, rows)
        ttk.Label(form, text="Rows per piece").pack(side="left")
        ttk.Entry(form, textvariable=rows, width=10).pack(side="left", padx=6)
        ttk.Button(form, text="Add", command=lambda: view.edit(lambda: draft.add_chunk(rows.get()), rerender=True)).pack(side="left")
        return
    if not columns:
        note(form, "Choose a PK Table first; this splits by one of its columns.", "warning").pack(anchor="w")
        return
    if chosen == "separate":
        name = tk.StringVar()
        keep(form, name)
        ttk.Label(form, text="Name").pack(side="left")
        ttk.Entry(form, textvariable=name, width=20).pack(side="left", padx=6)
        ttk.Button(form, text="Add", command=lambda: view.edit(lambda: draft.add_separate(name.get()), rerender=True)).pack(side="left")
        note(form, "Then add a level for each group: its PK column and values.").pack(side="left", padx=8)
        return
    column, values = tk.StringVar(value=columns[0]), tk.StringVar()
    separate = tk.BooleanVar(value=False)
    keep(form, column, values, separate)
    ttk.Label(form, text="PK column").pack(side="left")
    ttk.Combobox(form, textvariable=column, values=columns, state="readonly", width=24).pack(side="left", padx=6)
    ttk.Label(form, text="Values").pack(side="left")
    ttk.Entry(form, textvariable=values, width=26).pack(side="left", padx=6)
    ttk.Checkbutton(form, text="Separate parquets", variable=separate).pack(side="left")
    ttk.Button(form, text="Add", command=lambda: view.edit(lambda: draft.add_pieces_by_column(
        column.get(), values.get() or None, separate.get()), rerender=True)).pack(side="left", padx=6)
    note(box, "Values blank: every value the PK has.").pack(anchor="w")


# =============================================================================
# Fact Tables
# =============================================================================


def candidate_label(candidate: model.Candidate) -> str:
    if not candidate.known:
        return f"{candidate.table} ({candidate.kind}) ? columns not known here"
    if candidate.fits:
        return f"{candidate.table} ({candidate.kind}) ✓ {', '.join(candidate.matched)}"
    return f"{candidate.table} ({candidate.kind}) ✗ lacks {', '.join(candidate.missing)}"


def binding_editor(view: AuthorView, parent: Any, index: int) -> None:
    """Each table input with the tables that could fill it, those that fit
    first, each saying which needed columns it has. Never picked (D45)."""
    for binding in view.draft.bindings(index):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(6, 0))
        needs = f" needs {', '.join(binding.columns)}" if binding.columns else ""
        ttk.Label(row, text=f"{binding.var} (as {binding.alias}){needs}").pack(side="left")
        labels = ["(not bound)"] + [candidate_label(c) for c in binding.candidates]
        tables = [""] + [c.table for c in binding.candidates]
        current = labels[tables.index(binding.bound)] if binding.bound in tables else (
            binding.bound or "(not bound)")
        choice_field(view, row, labels, current,
                     lambda label, i=index, b=binding, ls=labels, ts=tables: view.draft.bind(i, b.var, ts[ls.index(label)]),
                     width=60, rerender=True).pack(side="left", padx=8)
        if not binding.bound:
            note(row, "choose the table it reads", "error").pack(side="left")


def build_fact(view: AuthorView, parent: Any) -> None:
    draft = view.draft
    heading(parent, "Fact Tables",
            "The tables pulled for the PK: prefabricated recipes, or tables built from the dictionary. "
            "Each is joined to its PK. A variable left blank comes from where it says.")
    add_fact_panel(view, parent)
    facts = draft.fact_tables()
    for position, (index, cohort) in enumerate(facts, start=1):
        what = f"recipe {cohort['recipe']}" if cohort.get("recipe") else "built from the dictionary"
        box = entry_box(view, parent, f"{position}. {cohort.get('name') or cohort.get('recipe')} ({what})", "fact", index)
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Position").pack(side="left")
        spot = tk.StringVar(value=str(position))
        spot_entry = ttk.Entry(head, textvariable=spot, width=4)
        keep(spot_entry, spot)
        spot_entry.bind("<Return>", lambda e, i=index, v=spot: v.get().strip().isdigit() and view.edit(
            lambda: draft.move_fact_table(i, int(v.get())), rerender=True))
        spot_entry.pack(side="left", padx=(4, 12))
        ttk.Label(head, text="Name").pack(side="left")
        text_field(view, head, cohort.get("name"), lambda v, i=index: draft.rename_table(i, v)).pack(side="left", padx=6)
        editing = view.inline_builder is not None and view.inline_builder.index == index
        if not cohort.get("recipe") and not editing:
            ttk.Button(head, text="Edit", command=lambda i=index: open_table_builder(view, i)).pack(side="left", padx=4)
            if view.ws.has_recipes:
                ttk.Button(head, text="Save as Recipe", command=lambda i=index: save_as_recipe(view, i)).pack(side="left")
        ttk.Button(head, text="Remove", command=lambda i=index: view.edit(
            lambda: draft.remove_fact_table(i), rerender=True)).pack(side="right")
        note(box, "Type a position and press Enter to move it; the PK keeps its place.").pack(anchor="w")
        if editing:
            TableBuilderPanel(view, view.inline_builder, box)
        var_editor(view, box, index)
        binding_editor(view, box, index)
        if cohort.get("recipe"):
            filter_editor(view, box, index)
    if not facts:
        note(parent, "No fact tables.").pack(anchor="w")

def add_fact_panel(view: AuthorView, parent: Any) -> None:
    """At the top, set apart: a prefabricated table, or one from the data
    dictionary, whose whole form opens here when its table is chosen (D110)."""
    draft = view.draft
    inner = shaded_box(parent, "Add a fact table")
    builder = view.inline_builder
    if builder is not None and not builder.pk and builder.index is None:
        TableBuilderPanel(view, builder, inner)
        return
    prefab = ttk.Frame(inner)
    prefab.pack(fill="x", pady=2)
    names = view.ws.fact_recipes()
    ttk.Label(prefab, text="Prefabricated", width=20).pack(side="left")
    if names:
        recipe, name = tk.StringVar(value=names[0]), tk.StringVar()
        keep(prefab, recipe, name)
        ttk.Combobox(prefab, textvariable=recipe, values=names, state="readonly", width=34).pack(side="left", padx=4)
        ttk.Label(prefab, text="Name").pack(side="left", padx=(8, 4))
        ttk.Entry(prefab, textvariable=name, width=22).pack(side="left")
        ttk.Button(prefab, text="Add", command=lambda: view.edit(
            lambda: draft.add_prefab(recipe.get(), name.get()), rerender=True)).pack(side="left", padx=6)
    else:
        note(prefab, view.ws.recipes_problem() or "recipes.yaml has no fact recipes.", "warning").pack(side="left")
    dictionary = ttk.Frame(inner)
    dictionary.pack(fill="x", pady=2)
    table, table_name = tk.StringVar(), tk.StringVar()
    keep(dictionary, table, table_name)
    ttk.Label(dictionary, text="From data dictionary", width=20).pack(side="left")
    pick = ttk.Combobox(dictionary, textvariable=table, values=model.TableBuilder(draft).tables(),
                        state="readonly", width=34)
    pick.pack(side="left", padx=4)
    ttk.Label(dictionary, text="Name").pack(side="left", padx=(8, 4))
    ttk.Entry(dictionary, textvariable=table_name, width=22).pack(side="left")
    pick.bind("<<ComboboxSelected>>", lambda e: open_table_builder(view, None, table=table.get(),
                                                                   name=table_name.get()))
    note(inner, "Choosing a dictionary table opens its whole form here.").pack(anchor="w")
    ttk.Separator(parent).pack(fill="x", pady=(0, 6))


def save_as_recipe(view: AuthorView, index: int) -> None:
    ok, message = model.save_recipe(view.ws.recipes_path, view.draft.doc["cohorts"][index])
    view.say(message, "pass" if ok else "error")


# =============================================================================
# Validate
# =============================================================================


KIND_LABELS = {"error": "Error", "warning": "Warning", "pending": "Pending transfer"}
SECTION_LABELS = dict(SECTIONS)


class ValidatePanel:
    """The pipeline's steps, then every message with its fix; choosing one
    goes to the field it points at (D96)."""

    def __init__(self, view: AuthorView):
        self.view = view
        frame = view.validate_tab
        self.steps = ttk.Frame(frame, padding=(10, 8))
        self.steps.pack(fill="x")
        columns = ("where", "message")
        self.tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Kind")
        self.tree.column("#0", width=150, stretch=False)
        self.tree.heading("where", text="Where")
        self.tree.column("where", width=260, stretch=False)
        self.tree.heading("message", text="Message")
        self.tree.column("message", width=800)
        for kind, colour in COLOURS.items():
            self.tree.tag_configure(kind, foreground=colour)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.fix = note(frame, "Choose a message to see its fix; double-click it to go there.")
        self.fix.pack(side="bottom", fill="x", padx=10, pady=8)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0))
        bar.pack(side="left", fill="y")
        self.messages: dict[str, model.Message] = {}
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_fix())
        self.tree.bind("<Double-1>", lambda e: self.go())
        self.tree.bind("<Return>", lambda e: self.go())

    def refresh(self, validation: model.Validation) -> None:
        for child in self.steps.winfo_children():
            child.destroy()
        for label, status in validation.steps:
            mark = {"pass": "✓", "fail": "✗", "pending": "…"}[status]
            ttk.Label(self.steps, text=f"{mark} {label}", foreground=COLOURS[status if status != "fail" else "error"]
                      ).pack(side="left", padx=(0, 16))
        self.tree.delete(*self.tree.get_children())
        self.messages = {}
        order = {"error": 0, "warning": 1, "pending": 2}
        for message in sorted(validation.messages, key=lambda m: order[m.kind]):
            where = SECTION_LABELS.get(message.field.section, "") if message.field else ""
            if message.field and message.field.detail:
                where = f"{where}: {message.field.detail}" if where else message.field.detail
            iid = self.tree.insert("", "end", text=KIND_LABELS[message.kind],
                                   values=(where or message.context, f"[{message.code}] {message.text}"),
                                   tags=(message.kind,))
            self.messages[iid] = message
        if not validation.messages:
            self.tree.insert("", "end", text="Valid", values=("", "Nothing to fix."), tags=("pass",))
        # "Validate points here" lasts only while a message still points there.
        mark = self.view.highlight
        if mark is not None and not any(m.field == mark for m in validation.messages):
            self.view.highlight = None
            self.view.render_soon()
        self.fix.configure(text="Choose a message to see its fix; double-click it to go there.",
                           foreground=COLOURS["muted"])

    def selected(self) -> model.Message | None:
        chosen = self.tree.selection()
        return self.messages.get(chosen[0]) if chosen else None

    def show_fix(self) -> None:
        message = self.selected()
        if message is not None:
            self.fix.configure(text=f"Fix: {message.fix}" if message.fix else "No fix recorded.",
                               foreground=COLOURS[message.kind])

    def go(self) -> None:
        message = self.selected()
        if message is not None:
            self.view.goto(message.field)


# =============================================================================
# Exports
# =============================================================================


class ExportsPanel:
    """The bundle queue (D91) and a saved intake's three exports."""

    def __init__(self, view: AuthorView):
        self.view = view
        self.frame = view.exports_tab
        self.chosen: Path | None = None
        self.last_bundle: tuple[bool, list[str], str] | None = None  # shown until the next build

    def refresh(self) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        view = self.view
        top = ttk.Frame(self.frame, padding=(10, 8))
        top.pack(fill="x")
        state = model.queue_state(view.ws)
        if state["available"]:
            queue = ttk.LabelFrame(top, text="Bundle queue: python3 makebundle.py queue carries these", padding=8)
            queue.pack(fill="x")
            for item in state["queue"]:
                row = ttk.Frame(queue)
                row.pack(fill="x")
                ttk.Button(row, text=item["name"], command=lambda n=item["name"]: self.show(view.ws.temp_dir / n)
                           ).pack(side="left")
                if not item["exists"]:
                    note(row, "not in YAMLs/temp/: the build will stop on it", "error").pack(side="left", padx=6)
                ttk.Button(row, text="Remove", command=lambda n=item["name"]: self.queue("remove", n)).pack(side="right")
            if not state["queue"]:
                note(queue, "Nothing queued. Saving an intake queues it.").pack(anchor="w")
            make = ttk.Frame(queue)
            make.pack(fill="x", pady=(8, 0))
            ttk.Button(make, text="Make bundle", command=self.make_bundle).pack(side="left")
            note(make, "Writes dist/bundle_with_yamls.py with every queued intake's transfer YAML; "
                       "dist/bundle.py, the runtime alone, is left as it is (D106).").pack(side="left", padx=8)
            if self.last_bundle is not None:
                ok, lines, content_id = self.last_bundle
                if ok:
                    ttk.Label(queue, text=f"content_id: {content_id}", foreground=COLOURS["pass"],
                              font=("Courier", 11, "bold")).pack(anchor="w", pady=(6, 0))
                note(queue, "\n".join(line for line in lines if not line.startswith("content_id")),
                     "muted" if ok else "error").pack(anchor="w")
            if state["addable"]:
                add = ttk.Frame(queue)
                add.pack(fill="x", pady=(6, 0))
                pick = tk.StringVar(value=state["addable"][0])
                keep(add, pick)
                ttk.Combobox(add, textvariable=pick, values=state["addable"], state="readonly", width=40).pack(side="left")
                ttk.Button(add, text="Add to queue", command=lambda: self.queue("add", pick.get())).pack(side="left", padx=6)
        else:
            note(top, "The bundle queue is kept on the Mac, beside makebundle.py.").pack(anchor="w")
        path = self.chosen or (view.draft.path if not view.draft.dirty else None)
        if path is None:
            note(self.frame, "Save the draft to see its exports: they are made from the saved intake.",
                 "warning").pack(anchor="w", padx=10, pady=10)
            return
        ttk.Label(self.frame, text=f"Exports of {path.name}", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=10)
        panes = ttk.Frame(self.frame, padding=(10, 4))
        panes.pack(fill="both", expand=True)
        for column, (title, text, ok) in enumerate(model.exports(view.ws, path)):
            panes.columnconfigure(column, weight=1)
            ttk.Label(panes, text=title, foreground=COLOURS["pass" if ok else "error"]).grid(row=0, column=column, sticky="w")
            box = scrolledtext.ScrolledText(panes, wrap="none", font=("Courier", 10), height=30)
            box.insert("1.0", text)
            box.configure(state="disabled")
            box.grid(row=1, column=column, sticky="nsew", padx=3)
        panes.rowconfigure(1, weight=1)

    def make_bundle(self) -> None:
        self.view.say("Making the bundle...")
        self.view.root.update_idletasks()
        self.last_bundle = model.make_bundle(self.view.ws)
        ok, lines, _ = self.last_bundle
        self.view.say(lines[-1] if ok else lines[0], "pass" if ok else "error")
        self.refresh()

    def show(self, path: Path) -> None:
        self.chosen = path
        self.refresh()

    def queue(self, action: str, name: str) -> None:
        folder = self.view.ws.temp_dir
        done = model.queue_add(name, folder) if action == "add" else model.queue_remove(name, folder)
        self.view.say(f"{'Queued' if action == 'add' else 'Removed'} {name}." if done else f"{name}: nothing to change.")
        self.refresh()


SECTION_BUILDERS: dict[str, Callable[[AuthorView, Any], None]] = {
    "project": build_project,
    "pk": build_pk,
    "supporting": build_supporting,
    "multipliers": build_multipliers,
    "splitters": build_splitters,
    "fact": build_fact,
}


# =============================================================================
# Tests: the view on a real Tk, withdrawn; skipped where there is no display
# =============================================================================

import shutil  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

REPO_YAMLS = SCRIPT_DIR.parent / "YAMLs"


_TEST_ROOT: list[Any] = []


def tk_root() -> Any:
    """One Tk for every test: a second Tk in one process can hang on the Mac."""
    if not _TEST_ROOT:
        try:
            root = tk.Tk()
        except tk.TclError:
            root = None
        if root is not None:
            root.withdraw()
        _TEST_ROOT.append(root)
    return _TEST_ROOT[0]


def widgets(parent: Any) -> list[Any]:
    out = []
    for child in parent.winfo_children():
        out.append(child)
        out.extend(widgets(child))
    return out


class ViewTest(unittest.TestCase):
    # One Tk for every test (tk_root).
    root: Any = None

    @classmethod
    def setUpClass(cls):
        cls.root = tk_root()

    def setUp(self):
        if self.root is None:
            self.skipTest("no display for Tk")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(REPO_YAMLS / "temp", self.tmp / "YAMLs" / "temp",
                        ignore=shutil.ignore_patterns("bundle_queue.txt"))
        # The core files, from wherever datascope.json says (D111).
        (self.tmp / "recipes").mkdir()
        for key in ("recipes", "datadictionary", "template"):
            shutil.copy(model.my.core_path(key), self.tmp / "recipes" / f"{key}.yaml")
        self.ws = model.Workspace(self.tmp, self.tmp / "recipes" / "recipes.yaml",
                                  self.tmp / "recipes" / "datadictionary.yaml", self.tmp / "recipes" / "template.yaml")
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
        self.root.update()
        window = self.view._last_builder
        window.act(lambda: window.b.set_from_table("EncounterFact"))
        window.b.name = "Visits"
        window.commit()
        self.root.update()
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


class SplitAndFactViewTests(ViewTest):
    def test_the_splitter_kinds_are_offered_by_whether_there_is_a_pk(self):
        self.view.show_section("splitters")
        text = " ".join(str(w.cget("text")) for w in widgets(self.view.body.inner) if isinstance(w, ttk.Label))
        self.assertIn("Choose a PK Table first", text)
        self.open("Celiac_intake.yaml")
        self.view.show_section("splitters")
        combos = [w for w in widgets(self.view.body.inner) if isinstance(w, ttk.Combobox)]
        self.assertTrue(any("PatientDurableKey" in w.cget("values") for w in combos))

    def test_a_splitter_added_in_the_view_says_its_kind(self):
        self.open("Celiac_intake.yaml")
        self.view.edit(lambda: self.view.draft.add_pieces_by_column("Sex"), rerender=True)
        self.view.edit(lambda: self.view.draft.add_separate("Race"), rerender=True)
        self.view.show_section("splitters")
        self.root.update()
        titles = [w.cget("text") for w in widgets(self.view.body.inner) if isinstance(w, ttk.LabelFrame)]
        self.assertIn("Pieces of one table: by Sex", titles)
        self.assertIn("Separate tables: Race", titles)

    def test_the_binding_picker_binds_what_is_chosen(self):
        self.open("AllCohort_intake.yaml")
        self.view.show_section("fact")
        index = next(i for i, c in self.view.draft.fact_tables() if c.get("name") == "OtherHospitalizations")
        self.view.draft.bind(index, "HospitalICDTable", "")
        self.view.render()
        picker = next(w for w in widgets(self.view.body.inner) if isinstance(w, ttk.Combobox)
                      and w.cget("values") and str(w.cget("values")[0]) == "(not bound)")
        choice = next(v for v in picker.cget("values") if str(v).startswith("HospitalICDTable"))
        picker.set(choice)
        picker.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(self.view.draft.doc["cohorts"][index]["vars"]["HospitalICDTable"], "HospitalICDTable")

    def test_a_fact_table_moves_by_number(self):
        self.open("Celiac_intake.yaml")
        names = [c.get("name") for _, c in self.view.draft.fact_tables()]
        last = self.view.draft.fact_tables()[-1][0]
        self.view.edit(lambda: self.view.draft.move_fact_table(last, 1), rerender=True)
        self.assertEqual([c.get("name") for _, c in self.view.draft.fact_tables()], [names[-1]] + names[:-1])


class ValidateAndExportViewTests(ViewTest):
    def test_messages_are_listed_by_kind_and_one_goes_to_its_field(self):
        self.open("IBD_Ancestry_intake.yaml")
        self.pump()
        panel = self.view.validate_panel
        rows = [panel.tree.item(i) for i in panel.tree.get_children()]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["text"], "Error")
        self.assertIn("missing_variable", rows[0]["values"][1])
        first = panel.tree.get_children()[0]
        panel.tree.selection_set(first)
        self.root.update()
        self.assertTrue(panel.fix.cget("text").startswith("Fix:"))
        panel.go()
        self.root.update()
        self.assertEqual(self.view.section_key, "pk")
        self.assertEqual(self.view.tabs.select(), str(self.view.builder_tab))
        marks = [w.cget("text") for w in widgets(self.view.body.inner) if isinstance(w, ttk.Label)]
        self.assertTrue(any(str(text).startswith("Validate points here") for text in marks))

    def test_the_mark_goes_once_no_message_points_there(self):
        self.open("IBD_Ancestry_intake.yaml")
        self.pump()
        panel = self.view.validate_panel
        first = panel.tree.get_children()[0]
        message = panel.messages[first]
        panel.tree.selection_set(first)
        panel.go()
        self.root.update()
        name = re.search(r"variable `(\w+)`", message.text).group(1)
        self.view.edit(lambda: self.view.draft.set_var(message.field.index, name, "K50%"))
        self.pump()
        marks = [w.cget("text") for w in widgets(self.view.body.inner) if isinstance(w, ttk.Label)]
        self.assertFalse(any(str(text).startswith("Validate points here") for text in marks), marks)

    def test_the_steps_show_pending(self):
        self.open("Celiac_intake.yaml")
        index = self.view.draft.add_supporting("csv", "Later", "csv/later.csv", pending_transfer=True)
        self.view.changed()
        self.pump()
        steps = [w.cget("text") for w in self.view.validate_panel.steps.winfo_children()]
        self.assertIn("… Uploads", steps)
        self.assertIn(index, [i for i, _ in self.view.draft.supporting()])

    def test_exports_show_a_saved_intakes_three_files_and_the_queue(self):
        self.open("Celiac_intake.yaml")
        self.view.exports_panel.refresh()
        boxes = [w for w in widgets(self.view.exports_tab) if isinstance(w, scrolledtext.ScrolledText)]
        self.assertEqual(len(boxes), 3)
        self.assertIn("transfer:", boxes[1].get("1.0", "end"))
        if model.queue_available():
            self.view.exports_panel.queue("add", "Celiac_intake.yaml")
            self.assertEqual(model.queue_state(self.ws)["queue"][0]["name"], "Celiac_intake.yaml")

    def test_make_bundle_shows_its_content_id_and_keeps_it(self):
        if not model.queue_available():
            self.skipTest("the bundle builder is not here")
        import bundle_pullmanager as bp
        from unittest import mock

        self.open("Celiac_intake.yaml")
        model.queue_add("Celiac_intake.yaml", self.ws.temp_dir)
        dist = self.tmp / "dist"
        with mock.patch.object(bp, "DEFAULT_OUTPUT", dist / "bundle.py"), \
                mock.patch.object(bp, "WITH_YAMLS_OUTPUT", dist / "bundle_with_yamls.py"):
            self.view.exports_panel.make_bundle()
        ok, lines, content_id = self.view.exports_panel.last_bundle
        self.assertTrue(ok, lines)
        self.assertTrue((dist / "bundle_with_yamls.py").is_file())
        self.assertFalse((dist / "bundle.py").exists())
        self.assertIn(f"bundle_with_yamls.py {content_id}", (dist / "content_id.txt").read_text(encoding="utf-8"))
        self.view.exports_panel.refresh()
        shown = [str(w.cget("text")) for w in widgets(self.view.exports_tab) if isinstance(w, ttk.Label)]
        self.assertIn(f"content_id: {content_id}", shown)

    def test_on_the_vm_side_no_pending_checkbox_is_offered(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("supporting")
        texts = lambda: [str(w.cget("text")) for w in widgets(self.view.body.inner) if isinstance(w, ttk.Checkbutton)]
        self.assertTrue(any("Pending transfer" in t for t in texts()))
        self.ws.vm_side = True
        self.view.render()
        self.assertFalse(any("Pending transfer" in t for t in texts()))
        self.view.show_section("pk")
        self.assertFalse(any("Pending transfer" in t for t in texts()))

    def test_an_unsaved_draft_says_to_save_first(self):
        self.open("Celiac_intake.yaml")
        self.view.edit(lambda: setattr(self.view.draft, "project_db", "PROJECTD9"))
        self.view.exports_panel.refresh()
        text = " ".join(str(w.cget("text")) for w in widgets(self.view.exports_tab) if isinstance(w, ttk.Label))
        self.assertIn("Save the draft", text)


class InlineBuilderTests(ViewTest):
    """D110: a fact table is added in place, at the top of Fact Tables."""

    def test_the_add_panel_comes_first_and_a_dictionary_table_opens_its_form_there(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("fact")
        self.root.update()
        order = [str(w.cget("text")) for w in widgets(self.view.body.inner)
                 if isinstance(w, (ttk.Label, ttk.LabelFrame))]
        self.assertLess(order.index("Add a fact table"), next(i for i, l in enumerate(order) if l.startswith("1. ")))
        pick = next(w for w in widgets(self.view.body.inner) if isinstance(w, ttk.Combobox)
                    and "EncounterFact" in w.cget("values"))
        pick.set("EncounterFact")
        pick.event_generate("<<ComboboxSelected>>")
        self.root.update()
        panel = self.view._last_builder
        self.assertEqual(panel.b.from_table, "EncounterFact")
        self.assertTrue(panel.win.winfo_ismapped())
        self.assertFalse([w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)])
        panel.b.name = "Visits"
        panel.commit()
        self.root.update()
        self.assertEqual(self.view.draft.fact_tables()[-1][1]["name"], "Visits")
        self.assertIsNone(self.view.inline_builder)

    def test_cancel_closes_the_form_and_adds_nothing(self):
        self.open("Celiac_intake.yaml")
        before = len(self.view.draft.doc["cohorts"])
        open_table_builder(self.view, None, table="EncounterFact")
        self.root.update()
        self.view._last_builder.cancel()
        self.root.update()
        self.assertIsNone(self.view.inline_builder)
        self.assertEqual(len(self.view.draft.doc["cohorts"]), before)

    def test_a_pk_from_the_dictionary_is_built_in_the_pk_section(self):
        self.open("Celiac_intake.yaml")
        open_table_builder(self.view, None, pk=True, table="PatientDim", name="People")
        self.root.update()
        self.assertEqual(self.view.section_key, "pk")
        self.view._last_builder.commit()
        self.root.update()
        pk = self.view.draft.pk()
        self.assertEqual((pk.kind, pk.name), ("dictionary", "People"))


class FilterViewTests(ViewTest):
    """D105: where lines by column and joins, for prefabricated tables too."""

    def filters_box(self) -> Any:
        return next(w for w in widgets(self.view.body.inner)
                    if isinstance(w, ttk.LabelFrame) and w.cget("text") == "Filters added to this table")

    def test_a_where_by_value_is_added_to_a_prefabricated_table(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("fact")
        self.root.update()
        box = self.filters_box()
        combos = [w for w in widgets(box) if isinstance(w, ttk.Combobox)]
        column, mode = combos[0], combos[1]
        column.set(column.cget("values")[0])
        self.assertEqual(mode.get(), "=")
        mode.set("LIKE")
        self.root.update()
        entry = next(w for w in widgets(box) if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox))
        entry.insert(0, "K50%")
        next(w for w in widgets(box) if isinstance(w, ttk.Button) and w.cget("text") == "Add where").invoke()
        self.root.update()
        index = self.view.draft.fact_tables()[0][0]
        added = self.view.draft.added_lines(index, "where")
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].endswith("LIKE 'K50%'"), added)
        labels = [str(w.cget("text")) for w in widgets(self.view.body.inner) if isinstance(w, ttk.Label)]
        self.assertIn(f"where: {added[0]}", labels)

    def test_between_takes_a_lower_and_a_higher_value(self):
        # Infant_RSV: a date range typed as one value came out as `= 'BETWEEN ...'`.
        self.open("Celiac_intake.yaml")
        self.view.show_section("fact")
        self.root.update()
        box = self.filters_box()
        combos = [w for w in widgets(box) if isinstance(w, ttk.Combobox)]
        combos[0].set(combos[0].cget("values")[0])
        combos[1].set("BETWEEN")
        self.root.update()
        entries = [w for w in widgets(box) if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox)
                   and w.winfo_ismapped()]
        self.assertEqual(len(entries), 2)
        entries[0].insert(0, "{{min_date_key}}")
        entries[1].insert(0, "{{max_date_key}}")
        next(w for w in widgets(box) if isinstance(w, ttk.Button) and w.cget("text") == "Add where").invoke()
        self.root.update()
        index = self.view.draft.fact_tables()[0][0]
        self.assertTrue(self.view.draft.added_lines(index, "where")[-1].endswith(
            " BETWEEN {{min_date_key}} AND {{max_date_key}}"), self.view.draft.added_lines(index, "where"))

    def test_in_supporting_table_offers_the_tables_and_their_columns(self):
        self.open("Celiac_intake.yaml")
        self.view.show_section("fact")
        self.root.update()
        box = self.filters_box()
        mode = [w for w in widgets(box) if isinstance(w, ttk.Combobox)][1]
        mode.set("In supporting table")
        self.root.update()
        mapped = [w for w in widgets(box) if isinstance(w, ttk.Combobox) and w.winfo_ismapped()]
        tables = [w for w in mapped if "HospitalICDCodes" in w.cget("values")]
        self.assertTrue(tables)

    def test_a_written_condition_says_at_once_when_it_names_an_alias_the_table_lacks(self):
        self.open("Celiac_intake.yaml")
        open_table_builder(self.view, None, table="EncounterFact")
        self.root.update()
        window = self.view._last_builder
        next(w for w in widgets(window.win) if isinstance(w, ttk.Button)
             and w.cget("text") == "Add a written condition").invoke()
        self.root.update()
        window = self.view._last_builder
        where = next(w for w in widgets(window.win) if isinstance(w, ttk.LabelFrame) and w.cget("text") == "Where")
        entry = next(w for w in widgets(where) if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox))

        def shown() -> list[str]:
            return [str(w.cget("text")) for w in widgets(where) if isinstance(w, ttk.Label) and "does not define" in str(w.cget("text"))]

        entry.insert(0, "p.IsValid = 1")
        self.root.update()
        self.assertTrue(shown() and "`p`" in shown()[0], shown())
        entry.delete(0, "end")
        entry.insert(0, "ef.DateKey > 20200101")
        self.root.update()
        self.assertEqual(shown(), [])

    def test_the_match_follows_this_tables_column_too(self):
        # "number match" stayed after this table's column became FourthRace.
        self.open("Celiac_intake.yaml")
        open_table_builder(self.view, None, table="EncounterFact")
        self.root.update()
        joins = next(w for w in widgets(self.view._last_builder.win)
                     if isinstance(w, ttk.LabelFrame) and w.cget("text") == "Joins")
        combos = [w for w in widgets(joins) if isinstance(w, ttk.Combobox)]
        _, column, _, other, other_column = combos[:5]
        other.set(next(v for v in other.cget("values")))
        other.event_generate("<<ComboboxSelected>>")
        self.root.update()
        builder = self.view._last_builder.b
        names = [builder.source_column(c) for c in builder.columns]
        texts = {}
        for name in names:
            column.set(name)
            for candidate in other_column.cget("values"):
                other_column.set(candidate)
                other_column.event_generate("<<ComboboxSelected>>")
                self.root.update()
                label = next(w for w in widgets(joins) if isinstance(w, ttk.Label) and
                             ("match" in str(w.cget("text")) or " vs " in str(w.cget("text"))
                              or "no declared" in str(w.cget("text"))))
                texts[(name, candidate)] = str(label.cget("text"))
        good = next(k for k, v in texts.items() if v.endswith("match"))
        bad = next(k for k, v in texts.items() if " vs " in v and k[1] == good[1])
        column.set(good[0]); other_column.set(good[1]); other_column.event_generate("<<ComboboxSelected>>")
        self.root.update()
        column.set(bad[0])  # only this table's column changes
        self.root.update()
        label = [str(w.cget("text")) for w in widgets(joins) if isinstance(w, ttk.Label)
                 and (" vs " in str(w.cget("text")) or str(w.cget("text")).endswith("match"))]
        self.assertTrue(label and " vs " in label[0], label)

    def test_the_builder_offers_the_join_operator(self):
        self.open("Celiac_intake.yaml")
        open_table_builder(self.view, None, table="EncounterFact")
        self.root.update()
        window = self.view._last_builder
        combos = [w for w in widgets(window.win) if isinstance(w, ttk.Combobox)]
        self.assertTrue(any(tuple(w.cget("values")) == model.JOIN_OPERATORS for w in combos))


def run_tdd(verbosity: int = 2) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(case)
                                for case in (SectionViewTests, SplitAndFactViewTests,
                                             ValidateAndExportViewTests, FilterViewTests,
                                             InlineBuilderTests)])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if _TEST_ROOT and _TEST_ROOT[0] is not None:
        _TEST_ROOT[0].destroy()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--tdd" in sys.argv[1:]:
        raise SystemExit(run_tdd())
    print(__doc__)
