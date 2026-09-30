#!/usr/bin/env python3
"""
A small Tkinter Parquet viewer.

Runtime requirement:
    One of these must be installed in the Python environment:
      - duckdb
      - pyarrow
      - pandas with a parquet engine available

Tkinter ships with most Python installs, but Parquet decoding does not. This
file intentionally avoids app plugins or extra project files so it can be copied
as plain text onto a VM.
"""

from __future__ import annotations

import math
import os
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable


MAX_CELL_CHARS = 500
DEFAULT_PAGE_SIZE = 1000
CLOSE_TAB_PIXELS = 24


@dataclass
class TableData:
    path: str
    columns: list[str]
    rows: list[tuple[Any, ...]]

    @property
    def name(self) -> str:
        return os.path.basename(self.path) or self.path


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


def load_with_pyarrow(path: str) -> TableData:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    columns = [str(name) for name in table.column_names]
    data = table.to_pylist()
    rows = [tuple(row.get(column) for column in columns) for row in data]
    return TableData(path=path, columns=columns, rows=rows)


def load_with_pandas(path: str) -> TableData:
    import pandas as pd

    frame = pd.read_parquet(path)
    columns = [str(name) for name in frame.columns]
    rows = [tuple(row) for row in frame.itertuples(index=False, name=None)]
    return TableData(path=path, columns=columns, rows=rows)


def load_with_duckdb(path: str) -> TableData:
    import duckdb

    frame = duckdb.read_parquet(path).df()
    columns = [str(name) for name in frame.columns]
    rows = [tuple(row) for row in frame.itertuples(index=False, name=None)]
    return TableData(path=path, columns=columns, rows=rows)


def load_parquet(path: str) -> TableData:
    errors: list[str] = []
    loaders: list[tuple[str, Callable[[str], TableData]]] = [
        ("duckdb", load_with_duckdb),
        ("pyarrow", load_with_pyarrow),
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


# Who made this window, at its foot (D145). Packed ahead of the window's
# contents, so a small window squeezes them and never this.
CREDIT = "Designed and built by Jason Mathias"


def add_credit(root) -> None:
    from tkinter import ttk

    label = ttk.Label(root, text=CREDIT, foreground="#8c959f", font="TkSmallCaptionFont")
    placed = {"side": "bottom", "anchor": "e", "padx": 8, "pady": (0, 2)}
    slaves = root.pack_slaves()
    if slaves:
        placed["before"] = slaves[0]
    label.pack(**placed)


class DataTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, table: TableData) -> None:
        super().__init__(parent)
        self.table = table
        self.view_rows = list(table.rows)
        self.page_size = DEFAULT_PAGE_SIZE
        self.page = 0
        self.sort_column: int | None = None
        self.sort_descending = False

        self._build_toolbar()
        self._build_tree()
        self._refresh_tree()

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

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
        if not self.view_rows:
            return 1
        return math.ceil(len(self.view_rows) / self.page_size)

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())

        start = self.page * self.page_size
        end = min(start + self.page_size, len(self.view_rows))
        for row in self.view_rows[start:end]:
            self.tree.insert("", tk.END, values=[stringify(value) for value in row])

        sort_text = ""
        if self.sort_column is not None:
            direction = "desc" if self.sort_descending else "asc"
            sort_text = f" | sorted by {self.table.columns[self.sort_column]} {direction}"

        self.summary.configure(
            text=(
                f"{self.table.name} | {len(self.table.rows):,} rows | "
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

        self.view_rows.sort(
            key=lambda row: sort_key(row[column_index]),
            reverse=self.sort_descending,
        )
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
    def __init__(self, initial_files: Iterable[str] = ()) -> None:
        super().__init__()
        self.title("Parquet Viewer")
        self.geometry("1100x720")
        self.minsize(800, 480)

        self.tabs: list[DataTab] = []
        self.active_notebook: ttk.Notebook | None = None
        self.right_notebook: ttk.Notebook | None = None

        self._build_menu()
        self._build_layout()
        add_credit(self)

        for path in initial_files:
            self.open_file(path)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Open Parquet...", accelerator="Ctrl+O", command=self.pick_files)
        file_menu.add_command(label="Close Tab", accelerator="Ctrl+W", command=self.close_active_tab)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)

        menu.add_cascade(label="File", menu=file_menu)
        self.configure(menu=menu)
        self.bind("<Control-o>", lambda _event: self.pick_files())
        self.bind("<Control-w>", lambda _event: self.close_active_tab())

    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(toolbar, text="Open Parquet...", command=self.pick_files).pack(side=tk.LEFT)
        self.add_view_button = ttk.Button(toolbar, text="Add View", command=self.add_second_view)
        self.add_view_button.pack(side=tk.LEFT, padx=(8, 0))
        self.status = ttk.Label(toolbar, text="Open one or more .parquet files to begin.")
        self.status.pack(side=tk.LEFT, padx=(12, 0))

        self.pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.left_notebook = ttk.Notebook(self.pane)
        self.pane.add(self.left_notebook, weight=1)
        self.active_notebook = self.left_notebook
        self.left_notebook.bind("<<NotebookTabChanged>>", self._mark_active_notebook, add="+")
        self.left_notebook.bind("<Button-1>", self._handle_tab_click, add="+")

    def _mark_active_notebook(self, event: tk.Event) -> None:
        widget = event.widget
        if isinstance(widget, ttk.Notebook):
            self.active_notebook = widget

    def add_second_view(self) -> None:
        if self.right_notebook is not None:
            self.active_notebook = self.right_notebook
            return

        self.right_notebook = ttk.Notebook(self.pane)
        self.right_notebook.bind("<<NotebookTabChanged>>", self._mark_active_notebook, add="+")
        self.right_notebook.bind("<Button-1>", self._handle_tab_click, add="+")
        self.pane.add(self.right_notebook, weight=1)
        self.active_notebook = self.right_notebook
        self.add_view_button.configure(state=tk.DISABLED)
        self.status.configure(text="Second view added. Open files load into the selected view.")

    def _handle_tab_click(self, event: tk.Event) -> str | None:
        notebook = event.widget
        if not isinstance(notebook, ttk.Notebook):
            return None

        try:
            tab_index = notebook.index(f"@{event.x},{event.y}")
            x, _y, width, _height = notebook.bbox(tab_index)
        except tk.TclError:
            return None

        if event.x < x + width - CLOSE_TAB_PIXELS:
            return None

        self._close_tab(notebook, tab_index)
        return "break"

    def close_active_tab(self) -> None:
        notebook = self.active_notebook
        if notebook is None or not notebook.tabs():
            return

        try:
            tab_index = notebook.index(notebook.select())
        except tk.TclError:
            return

        self._close_tab(notebook, tab_index)

    def _close_tab(self, notebook: ttk.Notebook, tab_index: int) -> None:
        tab_widget_name = notebook.tabs()[tab_index]
        tab_widget = self.nametowidget(tab_widget_name)
        notebook.forget(tab_index)
        if isinstance(tab_widget, DataTab) and tab_widget in self.tabs:
            self.tabs.remove(tab_widget)
        tab_widget.destroy()
        self.status.configure(text=f"Loaded {len(self.tabs):,} file(s).")

    def pick_files(self) -> None:
        # Beside a pull's files (Artifacts copies this script there), start in
        # its COSMOS parquets folder (D142).
        beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cosmos_parquets")
        paths = filedialog.askopenfilenames(
            title="Open Parquet file",
            initialdir=beside if os.path.isdir(beside) else None,
            filetypes=[
                ("Parquet files", "*.parquet *.parq"),
                ("All files", "*.*"),
            ],
        )
        for path in paths:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        try:
            table = load_parquet(path)
        except Exception as exc:
            messagebox.showerror("Could not open Parquet", str(exc))
            return

        target = self.active_notebook or self.left_notebook
        tab = DataTab(target, table)
        self.tabs.append(tab)
        target.add(tab, text=f"{table.name}  x")
        target.select(tab)
        self.active_notebook = target
        self.status.configure(text=f"Loaded {len(self.tabs):,} file(s).")


def main() -> int:
    paths = [path for path in sys.argv[1:] if path.lower().endswith((".parquet", ".parq"))]
    app = ParquetViewer(paths)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
