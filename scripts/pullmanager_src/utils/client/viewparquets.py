#!/usr/bin/env python3
"""
A small Tkinter Parquet viewer (D89, D146).

Opened from the utilities window, it lists the pulls under runs/ that have run
or are running, with the words Run shows; choose one, then its Cosmos,
Cosmos_SneakPeek or Uploads parquets, then up to two tables side by side.
A copy of this file sits in every pull's folder (Artifacts puts it there), for
someone who has only that folder: it opens on that pull. Browse... opens any
parquet anywhere.

Runtime requirement:
    One of these must be installed in the Python environment:
      - pyarrow (the VM's; tables are kept in Arrow, only the page shown is Python)
      - duckdb, or pandas with a parquet engine (the whole file is read)

Tkinter ships with most Python installs, but Parquet decoding does not. This
file intentionally avoids app plugins or extra project files so it can be copied
as plain text onto a VM.
"""

from __future__ import annotations

import math
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable


MAX_CELL_CHARS = 500
DEFAULT_PAGE_SIZE = 1000
TABLE_BUTTONS_PER_ROW = 4
# A pull's parquet folders (D142), and the button each gets.
PARQUET_FOLDERS = (
    ("Cosmos", "cosmos_parquets"),
    ("Cosmos_SneakPeek", "sneakpeek_parquets"),
    ("Uploads", "uploads_parquets"),
)
SHOWN_MARK = "● "  # before a shown table's name on its button


def stringify(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[: MAX_CELL_CHARS - 3] + "..."
    return text


def sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, "")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return (1, "")
        return (0, value)
    return (0, str(value).casefold())


# ---------------------------------------------------------------- the tables

class Table:
    """One parquet as the viewer needs it: its columns, how many rows, and
    one page of them at a time."""

    def __init__(self, path: str, columns: list[str], row_count: int) -> None:
        self.path = path
        self.columns = columns
        self.row_count = row_count

    @property
    def name(self) -> str:
        return os.path.basename(self.path) or self.path

    def page(self, offset: int, size: int) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def sorted(self, column: int, descending: bool) -> "Table":
        raise NotImplementedError


class ArrowTable(Table):
    """Kept in Arrow: only the page on screen becomes Python values (D146), so
    a large table opens without turning every value into an object."""

    def __init__(self, path: str, table: Any) -> None:
        super().__init__(path, [str(name) for name in table.column_names], table.num_rows)
        self.table = table

    def page(self, offset: int, size: int) -> list[tuple[Any, ...]]:
        chunk = self.table.slice(offset, size).to_pydict()
        return list(zip(*(chunk[name] for name in self.table.column_names))) if chunk else []

    def sorted(self, column: int, descending: bool) -> "Table":
        import pyarrow.compute as pc

        order = "descending" if descending else "ascending"
        indices = pc.sort_indices(self.table, sort_keys=[(self.table.column_names[column], order)],
                                  null_placement="at_end")
        return ArrowTable(self.path, self.table.take(indices))


class RowsTable(Table):
    """Read whole, where pyarrow is missing and duckdb or pandas read it."""

    def __init__(self, path: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        super().__init__(path, columns, len(rows))
        self.rows = rows

    def page(self, offset: int, size: int) -> list[tuple[Any, ...]]:
        return self.rows[offset:offset + size]

    def sorted(self, column: int, descending: bool) -> "Table":
        rows = sorted(self.rows, key=lambda row: sort_key(row[column]), reverse=descending)
        return RowsTable(self.path, self.columns, rows)


def load_with_pyarrow(path: str) -> Table:
    import pyarrow.parquet as pq

    return ArrowTable(path, pq.read_table(path, memory_map=True))


def load_with_pandas(path: str) -> Table:
    import pandas as pd

    frame = pd.read_parquet(path)
    columns = [str(name) for name in frame.columns]
    return RowsTable(path, columns, [tuple(row) for row in frame.itertuples(index=False, name=None)])


def load_with_duckdb(path: str) -> Table:
    import duckdb

    frame = duckdb.read_parquet(path).df()
    columns = [str(name) for name in frame.columns]
    return RowsTable(path, columns, [tuple(row) for row in frame.itertuples(index=False, name=None)])


def load_parquet(path: str) -> Table:
    errors: list[str] = []
    loaders: list[tuple[str, Callable[[str], Table]]] = [
        ("pyarrow", load_with_pyarrow),
        ("duckdb", load_with_duckdb),
        ("pandas", load_with_pandas),
    ]

    for name, loader in loaders:
        try:
            return loader(path)
        except ModuleNotFoundError as exc:
            errors.append(f"{name}: missing module {exc.name!r}")
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    detail = "\n".join(f"  - {line}" for line in errors)
    raise RuntimeError(
        "Could not read the Parquet file. Install duckdb, pyarrow, or pandas if possible:\n"
        "  python -m pip install duckdb pyarrow pandas\n\n"
        f"Tried:\n{detail}"
    )


def row_count(path: Path) -> int | None:
    """Its rows, from the file's own metadata: nothing is read."""
    try:
        import pyarrow.parquet as pq

        return pq.read_metadata(str(path)).num_rows
    except Exception:
        return None


# ------------------------------------------------------------------ the pulls

def runtime_pulls(cwd: Path | None = None) -> list[tuple[str, Path]] | None:
    """Each pull that has run or is running, as Run shows it (D140, D146),
    with its run folder; None where the runtime is not beside this file (the
    copy in a pull's folder). Run's own code reads them, so the two agree."""
    source = next((folder for folder in Path(__file__).resolve().parents
                   if (folder / "pullmanager" / "pulls.py").is_file()), None)
    if source is None:
        return None
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        from pullmanager import pulls
    except Exception:
        return None
    found = []
    for pull in pulls.find_pulls(Path(cwd or Path.cwd())):
        if pull.lock is None and not pull.outcome:
            continue  # not run yet: nothing to view
        label = f"{pull.name}: {pull.state}" if pull.lock else f"{pull.name}  ({pull.state})"
        found.append((label, pulls.run_folder(pull.manifest)))
    return found


def own_pull() -> Path | None:
    """The pull folder this copy sits in (in its utils/client/, D148), when it
    has parquet folders."""
    for here in list(Path(__file__).resolve().parents)[:3]:
        if any((here / folder).is_dir() for _, folder in PARQUET_FOLDERS):
            return here
    return None


def parquets_in(folder: Path) -> list[Path]:
    try:
        return sorted(folder.glob("*.parquet"), key=lambda path: path.name.lower())
    except OSError:
        return []


class Columns:
    """Which tables are on screen, left to right (D146): one fills the window,
    a second splits it, a third replaces the older of the two."""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self._picked: dict[str, int] = {}
        self._clock = 0

    def pick(self, path: str) -> int | None:
        """The column the table goes into; None when it is already shown."""
        if path in self.shown:
            return None
        self._clock += 1
        self._picked[path] = self._clock
        if len(self.shown) < 2:
            self.shown.append(path)
            return len(self.shown) - 1
        older = min(self.shown, key=lambda shown: self._picked[shown])
        index = self.shown.index(older)
        self.shown[index] = path
        return index

    def close(self, index: int) -> None:
        del self.shown[index]


# ---------------------------------------------------------------- the window

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


class DataColumn(ttk.Frame):
    """One table: its name, a close button, pages, and sorting by a heading."""

    def __init__(self, parent: tk.Misc, table: Table, on_close: Callable[[], None]) -> None:
        super().__init__(parent)
        self.table = table
        self.view = table
        self.page_size = DEFAULT_PAGE_SIZE
        self.page = 0
        self.sort_column: int | None = None
        self.sort_descending = False

        self._build_toolbar(on_close)
        self._build_tree()
        self._refresh_tree()

    def _build_toolbar(self, on_close: Callable[[], None]) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="✕", width=3, command=on_close).pack(side=tk.LEFT, padx=(0, 6))
        self.summary = ttk.Label(toolbar)
        self.summary.pack(side=tk.LEFT)

        nav = ttk.Frame(toolbar)
        nav.pack(side=tk.RIGHT)

        ttk.Button(nav, text="First", command=self.first_page).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(nav, text="Prev", command=self.prev_page).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(nav, text="Next", command=self.next_page).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(nav, text="Last", command=self.last_page).pack(side=tk.LEFT)

    def _build_tree(self) -> None:
        holder = ttk.Frame(self)
        holder.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.tree = ttk.Treeview(holder, columns=self.table.columns, show="headings")
        self.tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(holder, orient=tk.HORIZONTAL, command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        for index, column in enumerate(self.table.columns):
            self.tree.heading(
                column,
                text=column,
                command=lambda i=index: self.sort_by_column(i),
            )
            self.tree.column(column, width=150, minwidth=60, stretch=True)

    def page_count(self) -> int:
        if not self.view.row_count:
            return 1
        return math.ceil(self.view.row_count / self.page_size)

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())

        for row in self.view.page(self.page * self.page_size, self.page_size):
            self.tree.insert("", tk.END, values=[stringify(value) for value in row])

        sort_text = ""
        if self.sort_column is not None:
            direction = "desc" if self.sort_descending else "asc"
            sort_text = f" | sorted by {self.table.columns[self.sort_column]} {direction}"

        self.summary.configure(
            text=(
                f"{self.table.name} | {self.table.row_count:,} rows | "
                f"{len(self.table.columns):,} columns | page {self.page + 1:,}/{self.page_count():,}"
                f"{sort_text}"
            )
        )

    def sort_by_column(self, column_index: int) -> None:
        if self.sort_column == column_index:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column_index
            self.sort_descending = False
        try:
            self.view = self.table.sorted(column_index, self.sort_descending)
        except Exception as exc:  # a type Arrow cannot order, such as a nested one
            messagebox.showerror("Could not sort", f"{self.table.columns[column_index]}: {exc}")
            return
        self.page = 0
        self._refresh_tree()

    def first_page(self) -> None:
        self.page = 0
        self._refresh_tree()

    def prev_page(self) -> None:
        self.page = max(0, self.page - 1)
        self._refresh_tree()

    def next_page(self) -> None:
        self.page = min(self.page_count() - 1, self.page + 1)
        self._refresh_tree()

    def last_page(self) -> None:
        self.page = self.page_count() - 1
        self._refresh_tree()


class ParquetViewer(tk.Tk):
    def __init__(self, initial_files: Iterable[str] = (), cwd: Path | None = None) -> None:
        super().__init__()
        self.title("Parquet Viewer")
        self.geometry("1280x800")
        self.minsize(800, 480)

        self.cwd = Path(cwd or Path.cwd())
        self.pulls: dict[str, Path] = {}
        self.run_folder: Path | None = None
        self.folder: Path | None = None
        self.columns = Columns()
        self.data_columns: list[DataColumn] = []
        self.table_buttons: dict[str, ttk.Button] = {}

        self._build_layout()
        add_credit(self)
        self._start()

        for path in initial_files:
            self.open_table(str(path))

    # ------------------------------------------------------------- layout

    def _build_layout(self) -> None:
        style = ttk.Style(self)
        style.configure("Big.TButton", font=("TkDefaultFont", 12, "bold"), padding=(18, 8))

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(top, text="Pull").pack(side=tk.LEFT, padx=(0, 6))
        self.pull_pick = tk.StringVar()
        self.pull_box = ttk.Combobox(top, textvariable=self.pull_pick, state="readonly", width=70,
                                     postcommand=self._fill_pulls)
        self.pull_box.pack(side=tk.LEFT)
        self.pull_box.bind("<<ComboboxSelected>>", lambda _e: self.choose_pull(self.pull_pick.get()))
        self.pull_label = ttk.Label(top, text="")
        ttk.Button(top, text="Browse...", command=self.browse).pack(side=tk.RIGHT)

        folders = ttk.Frame(self)
        folders.pack(fill=tk.X, padx=8, pady=4)
        self.folder_buttons: dict[str, ttk.Button] = {}
        for label, folder in PARQUET_FOLDERS:
            button = ttk.Button(folders, text=label, state=tk.DISABLED,
                                style="TButton" if folder == "uploads_parquets" else "Big.TButton",
                                command=lambda f=folder: self.choose_folder(f))
            button.pack(side=tk.LEFT, padx=(0, 8))
            self.folder_buttons[folder] = button
        self.status = ttk.Label(folders, text="", foreground="#6e7781")
        self.status.pack(side=tk.LEFT, padx=(8, 0))

        self.tables_frame = ttk.Frame(self)
        self.tables_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self.pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    def _start(self) -> None:
        found = runtime_pulls(self.cwd)
        if found is None:
            # The copy in a pull's folder: that pull, no dropdown (D146).
            self.pull_box.pack_forget()
            self.pull_label.pack(side=tk.LEFT)
            own = own_pull()
            if own is not None:
                self.pull_label.configure(text=own.name)
                self.show_pull(own)
            else:
                self.pull_label.configure(text="Browse... to open a parquet.")
            return
        self._fill_pulls(found)
        self.status.configure(text="Choose a pull." if found else "No pull has run yet: Browse...")

    def _fill_pulls(self, found: list[tuple[str, Path]] | None = None) -> None:
        if found is None:
            found = runtime_pulls(self.cwd) or []
        self.pulls = dict(found)
        self.pull_box.configure(values=list(self.pulls))

    # ------------------------------------------------------------ choosing

    def choose_pull(self, label: str) -> None:
        folder = self.pulls.get(label)
        if folder is not None:
            self.show_pull(folder)

    def show_pull(self, run_folder: Path) -> None:
        """Its folder buttons, greyed where empty; the first with tables opens."""
        self.run_folder = run_folder
        first = None
        for _label, folder in PARQUET_FOLDERS:
            has = bool(parquets_in(run_folder / folder))
            self.folder_buttons[folder].configure(state=tk.NORMAL if has else tk.DISABLED)
            if has and first is None:
                first = folder
        if first is None:
            self._show_tables([])
            self.status.configure(text="No parquets yet: they appear when the pull is packaged.")
            return
        self.choose_folder(first)

    def choose_folder(self, folder: str) -> None:
        if self.run_folder is None:
            return
        self.folder = self.run_folder / folder
        tables = parquets_in(self.folder)
        self._show_tables(tables)
        label = next(label for label, name in PARQUET_FOLDERS if name == folder)
        self.status.configure(text=f"{label}: {len(tables)} table(s). Pick one, or two side by side.")

    def _show_tables(self, tables: list[Path]) -> None:
        for child in self.tables_frame.winfo_children():
            child.destroy()
        self.table_buttons = {}
        for index, path in enumerate(tables):
            rows = row_count(path)
            text = path.stem if rows is None else f"{path.stem}  {rows:,}"
            button = ttk.Button(self.tables_frame, text=text,
                                command=lambda p=str(path): self.open_table(p))
            button.grid(row=index // TABLE_BUTTONS_PER_ROW, column=index % TABLE_BUTTONS_PER_ROW,
                        sticky="ew", padx=(0, 6), pady=2)
            button.base_text = text
            self.table_buttons[str(path)] = button
        for column in range(TABLE_BUTTONS_PER_ROW):
            self.tables_frame.columnconfigure(column, weight=1)
        self._mark_shown()

    def _mark_shown(self) -> None:
        for path, button in self.table_buttons.items():
            shown = path in self.columns.shown
            button.configure(text=(SHOWN_MARK if shown else "") + button.base_text)

    # ------------------------------------------------------------- columns

    def open_table(self, path: str) -> None:
        """Into a column by the rule (D146): one fills, two split, a third
        replaces the older."""
        path = str(Path(path).resolve())
        if path in self.columns.shown:
            return
        try:
            table = load_parquet(path)
        except Exception as exc:
            messagebox.showerror("Could not open Parquet", str(exc))
            return
        index = self.columns.pick(path)
        column = DataColumn(self.pane, table, on_close=lambda p=path: self.close_table(p))
        if index < len(self.data_columns):
            old = self.data_columns[index]
            self.pane.insert(old, column, weight=1)
            self.pane.forget(old)
            old.destroy()
            self.data_columns[index] = column
        else:
            self.pane.add(column, weight=1)
            self.data_columns.append(column)
        self._mark_shown()

    def close_table(self, path: str) -> None:
        if path not in self.columns.shown:
            return
        index = self.columns.shown.index(path)
        self.columns.close(index)
        column = self.data_columns.pop(index)
        self.pane.forget(column)
        column.destroy()
        self._mark_shown()

    def shown_names(self) -> list[str]:
        """The tables on screen, left to right."""
        return [column.table.name for column in self.data_columns]

    def browse(self) -> None:
        start = self.folder if self.folder is not None and self.folder.is_dir() else None
        if start is None:
            own = own_pull()
            start = own / "cosmos_parquets" if own and (own / "cosmos_parquets").is_dir() else None
        paths = filedialog.askopenfilenames(
            title="Open Parquet file",
            initialdir=str(start) if start else None,
            filetypes=[
                ("Parquet files", "*.parquet *.parq"),
                ("All files", "*.*"),
            ],
        )
        for path in paths:
            self.open_table(path)


def main() -> int:
    paths = [path for path in sys.argv[1:] if path.lower().endswith((".parquet", ".parq"))]
    app = ParquetViewer(paths)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
