#!/usr/bin/env python3
"""
A window for the space in one Projects database (PROJECTD...).

Shows every table with its rows and size, how full the data and log files
are, and what, if anything, is keeping the log from reusing its space. Drop
one table (double-click it), the selected tables, or all of them; each drop
is committed on its own, and the view refreshes after. Refresh shows the
sizes again.

A table another session has locked (an SSMS window still running, or holding
an open transaction, or a pull) is not waited on forever: after
LOCK_TIMEOUT_S it is reported as blocked and skipped.

Needs pyodbc. The server, database and driver start from PULLMANAGER_*
settings in the environment or a .env, as Pullmanager's do.
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


class App:
    def __init__(self, root, database: str = "") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.messages: queue.Queue = queue.Queue()
        self.busy = False
        start = settings()
        root.title("Clear Projects database")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        self.server = tk.StringVar(value=start["server"])
        self.database = tk.StringVar(value=database or start["database"])
        self.driver = tk.StringVar(value=start["driver"])
        for row, (label, var, width) in enumerate([("Server", self.server, 20),
                                                    ("Database", self.database, 24),
                                                    ("ODBC driver", self.driver, 32)]):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(fields, textvariable=var, width=width).grid(row=row, column=1, sticky="w", pady=2)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        self.buttons = []
        for text, command in [("Refresh", self.on_refresh),
                              ("Drop selected", self.on_drop_selected),
                              ("Drop all...", self.on_drop_all),
                              ("Open transactions", self.on_open_transactions),
                              ("Free log", self.on_free_log)]:
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)

        self.space_text = tk.StringVar(value="Press Refresh.")
        ttk.Label(frame, textvariable=self.space_text, justify="left").pack(anchor="w", pady=(0, 6))

        panes = ttk.PanedWindow(frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        table_frame = ttk.Frame(panes)
        self.tree = ttk.Treeview(table_frame, columns=("mb", "rows"), selectmode="extended", height=16)
        self.tree.heading("#0", text="Table (double-click to drop)")
        self.tree.heading("mb", text="MB")
        self.tree.heading("rows", text="Rows")
        self.tree.column("#0", width=420)
        self.tree.column("mb", width=100, anchor="e")
        self.tree.column("rows", width=140, anchor="e")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        panes.add(table_frame, weight=3)

        self.log = tk.Text(panes, width=100, height=10, wrap="word")
        panes.add(self.log, weight=1)
        root.after(100, self.pump)

    # -- plumbing -----------------------------------------------------------

    def say(self, text: str) -> None:
        self.messages.put(("say", text))

    def pump(self) -> None:
        while not self.messages.empty():
            kind, payload = self.messages.get()
            if kind == "say":
                self.log.insert("end", payload + "\n")
                self.log.see("end")
            elif kind == "tables":
                self.tree.delete(*self.tree.get_children())
                for schema, table, rows, size_mb in payload:
                    self.tree.insert("", "end", iid=f"{schema}\x00{table}", text=f"{schema}.{table}",
                                     values=(f"{size_mb:,}", f"{rows:,}"))
            elif kind == "space":
                self.space_text.set(payload)
            elif kind == "idle":
                for button in self.buttons:
                    button.state(["!disabled"])
        self.root.after(100, self.pump)

    def database_name(self) -> str | None:
        from tkinter import messagebox
        try:
            return check_database(self.database.get())
        except ValueError as exc:
            messagebox.showerror("Database", str(exc))
            return None

    def run(self, work, refresh: bool = True) -> None:
        """Do work on a fresh connection off the Tk thread, then show the sizes again."""
        database = self.database_name()
        if self.busy or database is None:
            return
        self.busy = True
        for button in self.buttons:
            button.state(["disabled"])
        server, driver = self.server.get().strip(), self.driver.get().strip()

        def body() -> None:
            try:
                connection = connect(server, database, driver)
                try:
                    if work is not None:
                        work(connection)
                    if refresh:
                        tables = list_tables(connection)
                        total = sum(t[3] for t in tables)
                        self.messages.put(("tables", tables))
                        self.messages.put(("space", f"{database}: {len(tables)} table(s), "
                                                    f"{total:,} MB\n" + "\n".join(space(connection))))
                finally:
                    connection.close()
            except Exception as exc:  # noqa: BLE001 - shown to the user
                self.say(f"ERROR: {exc}")
            finally:
                self.busy = False
                self.messages.put(("idle", None))

        threading.Thread(target=body, daemon=True).start()

    def drop(self, tables: list[tuple[str, str]]) -> None:
        def work(connection) -> None:
            dropped, failures = drop_tables(connection, tables, self.say)
            self.say(f"Dropped {dropped} table(s); {len(failures)} failed. "
                     "Space from a big table can take a minute to show as free: Refresh.")
        self.run(work)

    # -- buttons --------------------------------------------------------------

    def on_refresh(self) -> None:
        self.run(None)

    def selected(self) -> list[tuple[str, str]]:
        return [tuple(iid.split("\x00", 1)) for iid in self.tree.selection()]

    def on_drop_selected(self) -> None:
        from tkinter import messagebox
        tables = self.selected()
        if not tables:
            messagebox.showinfo("Drop selected", "Select one or more tables first.")
            return
        names = "\n".join(f"  {s}.{t}" for s, t in tables[:15])
        more = f"\n  ... and {len(tables) - 15} more" if len(tables) > 15 else ""
        if messagebox.askyesno("Drop selected", f"Drop {len(tables)} table(s)?\n\n{names}{more}"):
            self.drop(tables)

    def on_double_click(self, event) -> None:
        from tkinter import messagebox
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        schema, table = iid.split("\x00", 1)
        if messagebox.askyesno("Drop table", f"Drop {schema}.{table}?"):
            self.drop([(schema, table)])

    def on_drop_all(self) -> None:
        from tkinter import messagebox, simpledialog
        database = self.database_name()
        if database is None:
            return
        typed = simpledialog.askstring("Drop all tables", f"This drops every table in {database}.\n"
                                                          f"Type {database} to go ahead:", parent=self.root)
        if typed is None:
            return
        if typed.strip().upper() != database.upper():
            messagebox.showinfo("Not dropped", "The name did not match; nothing was dropped.")
            return

        def work(connection) -> None:
            tables = [(s, t) for s, t, _, _ in list_tables(connection)]
            dropped, failures = drop_tables(connection, tables, self.say)
            self.say(f"Dropped {dropped} table(s); {len(failures)} failed. "
                     "Space from a big table can take a minute to show as free: Refresh.")
        self.run(work)

    def on_open_transactions(self) -> None:
        def work(connection) -> None:
            try:
                lines = open_transactions(connection)
            except Exception as exc:  # noqa: BLE001 - usually a permission
                lines = [f"Could not ask (DBCC OPENTRAN needs db_owner): {exc}",
                         "Instead, run SELECT @@TRANCOUNT in each SSMS tab on this database; "
                         "one that says more than 0 holds a transaction: COMMIT or close it."]
            for line in lines:
                self.say(line)
        self.run(work, refresh=False)

    def on_free_log(self) -> None:
        def work(connection) -> None:
            try:
                free_log(connection)
                self.say("Checkpoint run: log space up to the oldest open transaction is free for reuse.")
            except Exception as exc:  # noqa: BLE001 - usually a permission
                self.say(f"Could not run CHECKPOINT: {exc}. SQL Server runs one by itself "
                         "every minute or so; Refresh shortly.")
        self.run(work)


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


def main(argv: list[str]) -> int:
    import tkinter as tk
    root = tk.Tk()
    App(root, database=argv[0] if argv else "")
    add_credit(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
