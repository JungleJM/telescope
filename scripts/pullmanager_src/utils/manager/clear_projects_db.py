#!/usr/bin/env python3
"""
A window for the space in the Projects databases (PROJECTD...).

Every database a pull may be given is a row with its free space, measured as
Execute measures it (D164), or why the login could not open it (D185).
Opening one lists its tables grouped by pull, by table prefix (D163), each
with its rows and size; the database's files, and what if anything keeps its
log from reusing space, show when it is selected. The buttons act on what is
selected: tables, a pull's tables, or a whole database (typed to confirm).
Each drop is committed on its own, and the view refreshes after. A database
not listed can still be added by name.

A table another session has locked (an SSMS window still running, or holding
an open transaction, or a pull) is not waited on forever: after
LOCK_TIMEOUT_S it is reported as blocked and skipped.

Needs pyodbc. The server and driver start from PULLMANAGER_* settings in the
environment or a .env, as Pullmanager's do.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

DEFAULT_SERVER = "PROJECTS"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
LOCK_TIMEOUT_S = 30

TABLES_SQL = """
SELECT s.name, t.name,
       (SELECT SUM(p.rows) FROM sys.partitions p
         WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)),
       (SELECT SUM(a.total_pages) * 8 / 1024 FROM sys.partitions p
          JOIN sys.allocation_units a ON a.container_id = p.partition_id
         WHERE p.object_id = t.object_id)
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY 4 DESC, 1, 2
"""

# Every key on or pointing at a table: either end stops a drop.
FOREIGN_KEYS_SQL = """
SELECT ps.name, pt.name, f.name, rs.name, rt.name
FROM sys.foreign_keys f
JOIN sys.tables  pt ON pt.object_id = f.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.tables  rt ON rt.object_id = f.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
"""

FILES_SQL = """
SELECT name, type_desc, size / 128, FILEPROPERTY(name, 'SpaceUsed') / 128,
       CASE WHEN max_size = -1 THEN NULL ELSE max_size / 128 END
FROM sys.database_files
"""

LOG_WAIT_SQL = """
SELECT recovery_model_desc, log_reuse_wait_desc FROM sys.databases WHERE name = DB_NAME()
"""

LOG_WAITS = {
    "NOTHING": "nothing: the log reuses its space.",
    "CHECKPOINT": "a checkpoint. Free log runs one.",
    "ACTIVE_TRANSACTION": ("an open transaction. Find it with Open transactions; end it by "
                           "running COMMIT (or ROLLBACK) in the SSMS tab that holds it, or "
                           "closing that tab, or closing SSMS. Then Free log."),
    "LOG_BACKUP": "a log backup, which only the server's admins can take.",
}


def settings(env: dict[str, str] | None = None, folders: list[Path] | None = None) -> dict[str, str]:
    """Server, database and driver: the environment, then the first .env found."""
    source = dict(os.environ if env is None else env)
    for folder in folders if folders is not None else [Path.cwd(), Path(__file__).resolve().parents[1]]:
        path = folder / ".env"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    source.setdefault(key.strip(), value.strip().strip("\"'"))
            break
    return {
        "server": source.get("PULLMANAGER_PROJECTS_SERVER") or DEFAULT_SERVER,
        "database": source.get("PULLMANAGER_PROJECTS_DATABASE") or "",
        "driver": source.get("PULLMANAGER_ODBC_DRIVER") or DEFAULT_DRIVER,
    }


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def check_database(name: str) -> str:
    name = name.strip()
    if not name.upper().startswith("PROJECTD"):
        raise ValueError(f"'{name}' is not a Projects database: its name must start with PROJECTD.")
    return name


def connect(server: str, database: str, driver: str):
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("pyodbc is not installed in this Python.") from exc
    connection = pyodbc.connect(
        f"Driver={{{driver}}};Server=tcp:{server};Database={database};Trusted_Connection=yes;",
        autocommit=True, timeout=15)
    connection.cursor().execute(f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_S * 1000}")
    return connection


def list_tables(connection) -> list[tuple[str, str, int, int]]:
    rows = connection.cursor().execute(TABLES_SQL).fetchall()
    return [(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in rows]


def space(connection) -> list[str]:
    """How full each file is, and what the log is waiting on."""
    cursor = connection.cursor()
    lines = []
    for name, kind, size, used, cap in cursor.execute(FILES_SQL).fetchall():
        cap_text = "unlimited" if cap is None else f"{cap:,} MB"
        lines.append(f"{name} ({kind}): {used or 0:,} of {size:,} MB used, cap {cap_text}")
    row = cursor.execute(LOG_WAIT_SQL).fetchone()
    if row:
        model, wait = row
        lines.append(f"Recovery {model}; the log is waiting on {LOG_WAITS.get(wait, wait)}")
    return lines


def open_transactions(connection) -> list[str]:
    """The oldest open transaction in this database, as DBCC OPENTRAN reports it."""
    cursor = connection.cursor()
    cursor.execute("DBCC OPENTRAN WITH TABLERESULTS, NO_INFOMSGS")
    rows = cursor.fetchall() if cursor.description else []
    if not rows:
        return ["No open transaction in this database."]
    return [f"{label}: {value}" for label, value in rows]


def free_log(connection) -> None:
    connection.cursor().execute("CHECKPOINT")


def drop_tables(connection, tables: list[tuple[str, str]], say) -> tuple[int, list[str]]:
    """Drop these tables, and first any key on or pointing at them.

    Each statement commits alone; one that fails is reported and the rest go
    on. Returns (tables dropped, failures).
    """
    cursor = connection.cursor()
    chosen = {(s.lower(), t.lower()) for s, t in tables}
    failures: list[str] = []
    for ps, pt, key, rs, rt in cursor.execute(FOREIGN_KEYS_SQL).fetchall():
        if (ps.lower(), pt.lower()) not in chosen and (rs.lower(), rt.lower()) not in chosen:
            continue
        try:
            cursor.execute(f"ALTER TABLE {quote(ps)}.{quote(pt)} DROP CONSTRAINT {quote(key)}")
            say(f"dropped key {key} on {ps}.{pt}")
        except Exception as exc:  # noqa: BLE001 - reported, and the rest go on
            failures.append(f"key {key} on {ps}.{pt}: {describe(exc)}")
            say(f"FAILED key {key} on {ps}.{pt}: {describe(exc)}")
    dropped = 0
    for schema, table in tables:
        say(f"dropping {schema}.{table} ...")
        try:
            cursor.execute(f"DROP TABLE {quote(schema)}.{quote(table)}")
        except Exception as exc:  # noqa: BLE001 - reported, and the rest go on
            failures.append(f"{schema}.{table}: {describe(exc)}")
            say(f"  FAILED: {describe(exc)}")
        else:
            dropped += 1
            say("  dropped")
    return dropped, failures


def describe(exc: BaseException) -> str:
    text = str(exc)
    if "1222" in text or "Lock request time out" in text:
        return (f"blocked: another session has held a lock on it for {LOCK_TIMEOUT_S}s. "
                "Stop or close any SSMS query window on this database (an open "
                "transaction keeps its locks until it ends), and any running pull, "
                "then try again.")
    return text


def listed_databases() -> list[str]:
    """The databases a pull may be given (D171), from Pullmanager's own list."""
    here = Path(__file__).resolve().parents[2]  # pullmanager_src, or the extracted tree
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from pullmanager.config import DEFAULT_PROJECTS_DATABASES
    except Exception:  # noqa: BLE001 - a database can still be added by name
        return []
    return list(DEFAULT_PROJECTS_DATABASES)


def free_mb(connection) -> float | None:
    """The data files' room to their caps, in MB, as Execute measures it
    (D164); None if one has no cap."""
    free = 0.0
    for _name, kind, _size, used, cap in connection.cursor().execute(FILES_SQL).fetchall():
        if str(kind).upper() != "ROWS":
            continue
        if cap is None:
            return None
        free += float(cap) - float(used or 0)
    return free


def pull_of(table: str) -> str:
    """A table's pull, by its prefix (D163): `ucvis_MedAdminHistory` is ucvis's."""
    prefix, sep, _rest = table.partition("_")
    return prefix if sep and prefix else "(no prefix)"


def by_pull(tables: list[tuple[str, str, int, int]]) -> list[tuple[str, list[tuple[str, str, int, int]]]]:
    """Tables grouped by pull, the largest pull first, each pull's largest table first."""
    groups: dict[str, list[tuple[str, str, int, int]]] = {}
    for row in tables:
        groups.setdefault(pull_of(row[1]), []).append(row)
    ordered = sorted(groups.items(), key=lambda item: (-sum(r[3] for r in item[1]), item[0].lower()))
    return [(pull, sorted(rows, key=lambda r: (-r[3], r[1].lower()))) for pull, rows in ordered]


class Catalog:
    """What Refresh found: per database, its free space or why it could not
    be opened, its tables, and its space lines."""

    def __init__(self) -> None:
        self.free: dict[str, float | None] = {}
        self.problem: dict[str, str] = {}
        self.tables: dict[str, list[tuple[str, str, int, int]]] = {}
        self.space: dict[str, list[str]] = {}

    def load(self, name: str, connect_fn) -> None:
        try:
            connection = connect_fn(name)
        except Exception as exc:  # noqa: BLE001 - shown on the database's row
            self.problem[name] = (str(exc).splitlines() or [type(exc).__name__])[0]
            return
        try:
            self.free[name] = free_mb(connection)
            self.tables[name] = list_tables(connection)
            self.space[name] = space(connection)
            self.problem.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            self.problem[name] = (str(exc).splitlines() or [type(exc).__name__])[0]
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def row_text(self, name: str) -> str:
        if name in self.problem:
            return f"could not be opened ({self.problem[name]})"
        if name not in self.tables:
            return "press Refresh"
        free = self.free.get(name)
        room = "no cap on its data files" if free is None else f"{free / 1024:,.1f} GB free"
        return f"{room}, {len(self.tables[name])} table(s)"


def targets(selection: list[str], catalog: Catalog) -> dict[str, list[tuple[str, str]]]:
    """The tables a selection means, per database: a table itself, a pull's
    row all its tables, a database's row every table in it (D185)."""
    chosen: dict[str, list[tuple[str, str]]] = {}

    def add(database: str, schema: str, table: str) -> None:
        tables = chosen.setdefault(database, [])
        if (schema, table) not in tables:
            tables.append((schema, table))

    for iid in selection:
        parts = iid.split("\x00")
        if parts[0] == "db":
            for schema, table, _rows, _mb in catalog.tables.get(parts[1], []):
                add(parts[1], schema, table)
        elif parts[0] == "pull":
            for schema, table, _rows, _mb in catalog.tables.get(parts[1], []):
                if pull_of(table) == parts[2]:
                    add(parts[1], schema, table)
        elif parts[0] == "table":
            add(parts[1], parts[2], parts[3])
    return chosen


class App:
    def __init__(self, root, database: str = "") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.messages: queue.Queue = queue.Queue()
        self.busy = False
        self.catalog = Catalog()
        start = settings()
        self.names = listed_databases()
        for extra in (database, start["database"]):
            if extra and extra.strip().upper() not in {n.upper() for n in self.names}:
                self.names.append(extra.strip().upper())
        self.open_first = database.strip().upper()
        root.title("Clear Projects database")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        self.server = tk.StringVar(value=start["server"])
        self.driver = tk.StringVar(value=start["driver"])
        self.other = tk.StringVar()
        for row, (label, var, width) in enumerate([("Server", self.server, 20),
                                                    ("ODBC driver", self.driver, 32)]):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(fields, textvariable=var, width=width).grid(row=row, column=1, sticky="w", pady=2)
        ttk.Label(fields, text="Another database").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=2)
        other = ttk.Frame(fields)
        other.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Entry(other, textvariable=self.other, width=24).pack(side="left")
        ttk.Button(other, text="Add", command=self.on_add_database).pack(side="left", padx=4)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        self.buttons = []
        for text, command in [("Refresh", self.on_refresh),
                              ("Drop selected", self.on_drop_selected),
                              ("Open transactions", self.on_open_transactions),
                              ("Free log", self.on_free_log)]:
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)

        self.space_text = tk.StringVar(value="Press Refresh. Select a database to see its files.")
        ttk.Label(frame, textvariable=self.space_text, justify="left").pack(anchor="w", pady=(0, 6))

        panes = ttk.PanedWindow(frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        table_frame = ttk.Frame(panes)
        self.tree = ttk.Treeview(table_frame, columns=("mb", "rows"), selectmode="extended", height=16)
        self.tree.heading("#0", text="Database, pull, table (double-click a table to drop it)")
        self.tree.heading("mb", text="MB")
        self.tree.heading("rows", text="Rows")
        self.tree.column("#0", width=520)
        self.tree.column("mb", width=100, anchor="e")
        self.tree.column("rows", width=140, anchor="e")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_space())
        panes.add(table_frame, weight=3)

        self.log = tk.Text(panes, width=100, height=10, wrap="word")
        panes.add(self.log, weight=1)
        self.fill_tree()
        root.after(100, self.pump)
        root.after(200, self.on_refresh)

    # -- plumbing -----------------------------------------------------------

    def say(self, text: str) -> None:
        self.messages.put(("say", text))

    def pump(self) -> None:
        while not self.messages.empty():
            kind, payload = self.messages.get()
            if kind == "say":
                self.log.insert("end", payload + "\n")
                self.log.see("end")
            elif kind == "loaded":
                self.fill_tree()
            elif kind == "idle":
                for button in self.buttons:
                    button.state(["!disabled"])
        self.root.after(100, self.pump)

    def fill_tree(self) -> None:
        """One closed row per database; under it its pulls, under each its tables."""
        opened = {iid for iid in self.tree.get_children() if self.tree.item(iid, "open")}
        if self.open_first:
            opened.add(f"db\x00{self.open_first}")
        self.tree.delete(*self.tree.get_children())
        for name in self.names:
            db = f"db\x00{name}"
            self.tree.insert("", "end", iid=db, text=f"{name}: {self.catalog.row_text(name)}",
                             open=db in opened)
            for pull, rows in by_pull(self.catalog.tables.get(name, [])):
                node = self.tree.insert(db, "end", iid=f"pull\x00{name}\x00{pull}",
                                        text=f"{pull} ({len(rows)} table(s))",
                                        values=(f"{sum(r[3] for r in rows):,}", f"{sum(r[2] for r in rows):,}"))
                for schema, table, count, mb in rows:
                    self.tree.insert(node, "end", iid=f"table\x00{name}\x00{schema}\x00{table}",
                                     text=f"{schema}.{table}", values=(f"{mb:,}", f"{count:,}"))
        self.show_space()

    def selected_database(self) -> str | None:
        for iid in self.tree.selection():
            return iid.split("\x00")[1]
        return None

    def show_space(self) -> None:
        name = self.selected_database()
        if name is None:
            return
        lines = self.catalog.space.get(name) or [self.catalog.row_text(name)]
        self.space_text.set(f"{name}: " + "\n".join(lines))

    def connector(self):
        server, driver = self.server.get().strip(), self.driver.get().strip()
        return lambda name: connect(server, check_database(name), driver)

    def run(self, work, databases: list[str] | None = None) -> None:
        """Do work off the Tk thread, then measure the databases again (all of
        them, or those named)."""
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.state(["disabled"])
        connect_fn = self.connector()
        names = list(databases or self.names)

        def body() -> None:
            try:
                if work is not None:
                    work(connect_fn)
                for name in names:
                    self.catalog.load(name, connect_fn)
                self.messages.put(("loaded", None))
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self.say(f"ERROR: {exc}")
            finally:
                self.busy = False
                self.messages.put(("idle", None))

        threading.Thread(target=body, daemon=True).start()

    def drop(self, chosen: dict[str, list[tuple[str, str]]]) -> None:
        def work(connect_fn) -> None:
            for database, tables in chosen.items():
                connection = connect_fn(database)
                try:
                    self.say(f"{database}:")
                    dropped, failures = drop_tables(connection, tables, self.say)
                finally:
                    connection.close()
                self.say(f"{database}: dropped {dropped} table(s); {len(failures)} failed. "
                         "Space from a big table can take a minute to show as free: Refresh.")
        self.run(work, list(chosen))

    # -- buttons --------------------------------------------------------------

    def on_refresh(self) -> None:
        self.run(None)

    def on_add_database(self) -> None:
        from tkinter import messagebox
        try:
            name = check_database(self.other.get()).upper()
        except ValueError as exc:
            messagebox.showerror("Database", str(exc))
            return
        if name not in {n.upper() for n in self.names}:
            self.names.append(name)
        self.other.set("")
        self.open_first = name
        self.fill_tree()
        self.run(None, [name])

    def on_drop_selected(self) -> None:
        from tkinter import messagebox, simpledialog
        selection = list(self.tree.selection())
        chosen = targets(selection, self.catalog)
        count = sum(len(t) for t in chosen.values())
        if not count:
            messagebox.showinfo("Drop selected", "Select tables, a pull or a database first.")
            return
        for database in sorted({iid.split("\x00")[1] for iid in selection if iid.startswith("db\x00")}):
            typed = simpledialog.askstring(
                "Drop every table", f"This drops every table in {database}.\n"
                                    f"Type {database} to go ahead:", parent=self.root)
            if typed is None or typed.strip().upper() != database.upper():
                messagebox.showinfo("Not dropped", "The name did not match; nothing was dropped.")
                return
        names = "\n".join(f"  {d}: {s}.{t}" for d, tables in chosen.items() for s, t in tables[:15])
        more = f"\n  ... and more" if count > 15 else ""
        if messagebox.askyesno("Drop selected", f"Drop {count} table(s)?\n\n{names}{more}"):
            self.drop(chosen)

    def on_double_click(self, event) -> None:
        from tkinter import messagebox
        iid = self.tree.identify_row(event.y)
        if not iid.startswith("table\x00"):
            return
        _kind, database, schema, table = iid.split("\x00")
        if messagebox.askyesno("Drop table", f"Drop {schema}.{table} from {database}?"):
            self.drop({database: [(schema, table)]})

    def one_database(self, action: str) -> str | None:
        from tkinter import messagebox
        name = self.selected_database()
        if name is None:
            messagebox.showinfo(action, "Select a database (or anything in it) first.")
        return name

    def on_open_transactions(self) -> None:
        database = self.one_database("Open transactions")
        if database is None:
            return

        def work(connect_fn) -> None:
            connection = connect_fn(database)
            try:
                lines = open_transactions(connection)
            except Exception as exc:  # noqa: BLE001 - usually a permission
                lines = [f"Could not ask (DBCC OPENTRAN needs db_owner): {exc}",
                         "Instead, run SELECT @@TRANCOUNT in each SSMS tab on this database; "
                         "one that says more than 0 holds a transaction: COMMIT or close it."]
            finally:
                connection.close()
            for line in lines:
                self.say(f"{database}: {line}")
        self.run(work, [database])

    def on_free_log(self) -> None:
        database = self.one_database("Free log")
        if database is None:
            return

        def work(connect_fn) -> None:
            connection = connect_fn(database)
            try:
                free_log(connection)
                self.say(f"{database}: checkpoint run: log space up to the oldest open transaction "
                         "is free for reuse.")
            except Exception as exc:  # noqa: BLE001 - usually a permission
                self.say(f"{database}: could not run CHECKPOINT: {exc}. SQL Server runs one by "
                         "itself every minute or so; Refresh shortly.")
            finally:
                connection.close()
        self.run(work, [database])


# Who made this window, at its foot (D145), and which bundle it is (D147).
# Packed ahead of the window's contents, so a small window squeezes them and
# never this.
CREDIT = "Designed and built by Jason Mathias"
VERSION = ""  # Artifacts writes the bundle here in the copy it puts in a pull's folder


def version_id() -> str:
    """The first 8 characters of the extracted bundle's content_id, from the
    .runtime-manifest.json above this file; empty when run from source."""
    import json
    from pathlib import Path

    for folder in Path(__file__).resolve().parents:
        manifest = folder / ".runtime-manifest.json"
        if manifest.is_file():
            try:
                return str(json.loads(manifest.read_text(encoding="utf-8"))["content_id"])[:8]
            except (OSError, ValueError, KeyError, TypeError):
                return ""
    return VERSION


def credit_text() -> str:
    found = version_id()
    return f"{CREDIT} \u00b7 version {found}" if found else CREDIT


def add_credit(root) -> None:
    from tkinter import ttk

    label = ttk.Label(root, text=credit_text(), foreground="#8c959f", font="TkSmallCaptionFont")
    placed = {"side": "bottom", "anchor": "e", "padx": 8, "pady": (0, 2)}
    slaves = root.pack_slaves()
    if slaves:
        placed["before"] = slaves[0]
    label.pack(**placed)


def main(argv: list[str]) -> int:
    import tkinter as tk
    root = tk.Tk()
    App(root, database=argv[0] if argv else "")
    add_credit(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
