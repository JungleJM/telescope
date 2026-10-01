"""Command line entry point: summarize, preview (--dry-run) or execute a pull."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from . import __version__
from .executor import (
    PlanError,
    excluded_units,
    plan,
    session_has_work,
    session_resumes,
    session_units,
    write_sql,
)
from .manifest import (
    FINISHED,
    FINISHED_WITH_ERRORS,
    STOPPED_BY_USER,
    STOPPED_WITH_ERRORS,
    Manifest,
    ManifestError,
)
from .models import FAILED
from .naming import NamingError
from .normalize import NormalizationError
from .pulls import PullNotFound, execute_command, listing, resolve, shown


def summarize(manifest: Manifest) -> None:
    project = manifest.project
    print(f"Project:  {project.get('name')}  ({project.get('project_db')})")
    print(f"Manifest: {manifest.path}")
    print(f"Sessions: {len(manifest.sessions)}")
    print()
    for session in manifest.sessions:
        pk_source = next((p.pk_source for p in session.phases if p.pk_source), None)
        kind = pk_source.get("kind") if pk_source else "none"
        epoch = session.epoch or "-"
        print(f"  {session.session_id}  [{session.status}]  pk={session.pk_table} ({kind})")
        print(f"    epoch {epoch}")
        print(f"    next --execute: {next_step(manifest, session)}")
        for phase in session.phases:
            print(f"    phase {phase.name:<15} {phase.status:<8} {phase.yaml}")
            for line in table_lines(phase.outputs.get("table_rows"), "            "):
                print(line)
        for run in session.runs:
            batch = run.batch.get("name") if run.batch else "-"
            if run.group:
                batch = f"{run.group} {batch}" if run.batch else run.group
            print(f"    run   {batch:<15} {run.status:<8} {run.yaml}")
            for line in table_lines(run.outputs.get("table_rows"), "            "):
                print(line)
        print()


def table_lines(tables: dict[str, int] | None, indent: str) -> list[str]:
    """One line per table with its rows, aligned; never a total (D137)."""
    if not tables:
        return []
    width = max(len(str(name)) for name in tables)
    size = max(len(f"{rows:,}") for rows in tables.values())
    return [f"{indent}{str(name):<{width}}  {f'{rows:,}':>{size}} rows"
            for name, rows in tables.items()]


def next_step(manifest: Manifest, session, retry_failed: bool = False) -> str:
    if not session_has_work(manifest, session, retry_failed=retry_failed):
        if any(run.status == FAILED for run in session.runs):
            return "nothing, until --retry-failed reopens its failed runs"
        return "nothing left to pull (--repull pulls it again)"
    if session_resumes(session):
        todo = sum(
            1 for kind, _, _, execute, _ in session_units(manifest, session, retry_failed=retry_failed)
            if kind == "run" and execute
        )
        return f"resumes: {todo} run(s) to pull, finished ones kept"
    return "starts over"


def repull_chosen(manifest: Manifest, args: argparse.Namespace) -> bool:
    """`--repull-session`: those sessions start over, and each case's controls
    (D158). `all` is `--repull`. False when a name is not in the pull."""
    from .executor import UnknownSession, reset_sessions, sessions_to_repull

    names = [str(n).strip() for n in getattr(args, "repull_session", None) or [] if str(n).strip()]
    if not names:
        return True
    if any(name.lower() == "all" for name in names):
        args.repull = True
        return True
    try:
        chosen, notes = sessions_to_repull(manifest, names)
    except UnknownSession as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return False
    reset_sessions(manifest, chosen, "re-pulled: --repull-session")
    print("--repull-session: " + ", ".join(s.session_id for s in chosen)
          + " start over; the other sessions are kept.")
    for note in notes:
        print(f"  {note}")
    return True


def dry_run(manifest: Manifest, args: argparse.Namespace) -> int:
    if not repull_chosen(manifest, args):
        return 1
    if args.repull:
        # In memory only: a dry run never writes the manifest.
        manifest.reset_all("re-pulled: --repull")
    units = plan(
        manifest,
        linked_server=args.linked_server,
        retry_failed=args.retry_failed,
        include_settled=args.all,
    )
    left_out = excluded_units(manifest, retry_failed=args.retry_failed)
    failures = [row for row in left_out if row[1] == "failed"]

    if not units:
        print("Nothing to do.")
        print("Every phase and run is either complete for this session or deliberately")
        print("skipped. Use --retry-failed to reopen failures, or --all to render")
        print("everything regardless of status.")
        _report_exclusions(left_out, failures)
        return 0

    total_blocks = 0
    total_notes = 0
    for unit in units:
        server, local = len(unit.server_blocks), len(unit.local_blocks)
        total_blocks += server + local
        total_notes += len(unit.notes)
        print(f"{unit.unit_id}  [{unit.node.status}]  server={server} local={local}")
        print(f"    why:  {unit.reason}")
        for note in unit.notes:
            print(f"    note: {note}")
        if args.verbose:
            for block in unit.blocks:
                print(f"    {block.side:<6} {block.block_id}")

    print()
    for session in manifest.sessions:
        print(f"{session.session_id}: {next_step(manifest, session, args.retry_failed)}")
    print(f"Linked server placeholder: {args.linked_server}")
    _report_exclusions(left_out, failures)

    if args.out_dir:
        write_sql(units, Path(args.out_dir))
    print()
    for line in preview_statement(manifest, units, total_blocks, total_notes, args.out_dir):
        print(line)
    return 0


def preview_statement(manifest, units, blocks, notes, out_dir) -> list[str]:
    """Everything needed next, as the preview's last words (D71)."""
    lines = [
        f"Preview finished: {len(units)} unit(s), {blocks} SQL block(s), 0 errors, "
        f"{notes} note(s). Nothing was pulled."
    ]
    if out_dir:
        lines.append(f"SQL written to {shown(Path(out_dir))} for reading; Execute does not need it.")
    else:
        lines.append(
            "No SQL was written; add --out-dir <folder> to write it for reading. "
            "Execute does not need it."
        )
    command, folder = execute_command(manifest.path)
    lines.append(f"To pull it: press Execute, or in a terminal in {folder} run:")
    lines.append(f"    {command}")
    return lines


def _report_exclusions(left_out, failures) -> None:
    if not left_out:
        return
    print(f"\nExcluded {len(left_out)} unit(s):")
    for label, status, reason in left_out:
        print(f"  {label:<40} [{status}]  {reason}")
    if failures:
        print(
            f"\n{len(failures)} unit(s) failed previously and are NOT included. Rebuilding "
            "the\nserver side without them would finish with nothing transferred. Fix the "
            "cause,\nthen add --retry-failed."
        )


def execute(manifest: Manifest, args: argparse.Namespace, connect_fn=None) -> int:
    """Pull the manifest, writing a log (D68) and holding its lock (D67)."""
    from datetime import datetime

    from .lock import LockHeld, PullLock, clock_time, pull_name
    from .runlog import execute_log

    quick_edit_off()
    with execute_log(manifest.path) as log:
        print(f"Execute {pull_name(manifest.path)}: {manifest.path}")
        print(f"Started {datetime.now():%Y-%m-%d %H:%M:%S}, process {os.getpid()}. "
              f"Also written to {shown(log)}")
        pull_lock = PullLock(manifest.path, log=log.resolve())
        try:
            pull_lock.acquire()
        except LockHeld as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 1
        # How it ends goes into the manifest (D140), so Run can tell a
        # finished pull from one stopped by hand or by an error. A killed
        # process records nothing, and its empty `ended_at` says so.
        code, how = 1, STOPPED_WITH_ERRORS
        try:
            record_execute(manifest.execute_started)
            if pull_lock.replaced:
                stale = pull_lock.replaced
                print(
                    f"Took over a stale lock: {stale.holder()} stopped without cleaning up "
                    f"(last heartbeat {clock_time(stale.heartbeat)})."
                )
            pulled: list[str] = []
            code = _execute(manifest, args, connect_fn, pulled)
            how = FINISHED if code == 0 else FINISHED_WITH_ERRORS
            package_after_pull(manifest, args, connect_fn, code, bool(pulled))
            return code
        except Exception:
            # Each step records its own failure; this is anything else, which
            # used to reach only the console, and vanish when it closed.
            print("ERROR Execute stopped on an error it did not expect. What it was working "
                  "on stays 'running', and the next --execute pulls it again:", file=sys.stderr)
            traceback.print_exc()
            code, how = 1, STOPPED_WITH_ERRORS
            return 1
        except KeyboardInterrupt:
            print(
                "\nStopped (Ctrl+C). Whatever it was working on stays 'running' in the "
                "manifest; the next --execute pulls it again."
            )
            code, how = 130, STOPPED_BY_USER
            return 130
        finally:
            record_execute(manifest.execute_ended, code, how)
            pull_lock.release()


def package_after_pull(manifest: Manifest, args: argparse.Namespace, connect_fn, code: int,
                       pulled: bool) -> None:
    """A clean pull packages itself, holding its lock (D141).

    Clean: Execute ended with nothing failed and every session done, having
    pulled something (an Execute with nothing left to pull packages nothing). If
    Artifacts then fails, the exit code stays the pull's: its tables are safe
    in Projects, and Artifacts can be run again.
    """
    from .pulls import sessions_state

    progress, _ = sessions_state(manifest)
    if code == 0 and not pulled:
        return  # nothing left to pull: packaged already, or Artifacts does it
    print()
    if code != 0 or progress != "finished":
        print(f"Not packaged: the pull did not finish cleanly ({progress}). Retry failed "
              "finishes it; Artifacts packages the tables that are finished.")
        return
    print("The pull finished cleanly: packaging it (Artifacts).")
    try:
        packaged = artifacts(manifest, args, connect_fn, own_lock=True)
    except Exception:  # noqa: BLE001 - the pull is done; say so and go on
        traceback.print_exc()
        packaged = None
    if packaged != 0:
        print("WARNING Artifacts did not write every table (above). The pull itself is safe "
              "in Projects and its exit code is unchanged: run Artifacts again once the "
              "cause is fixed.", file=sys.stderr)
        return
    retire_working_blueprint(manifest)


def retire_working_blueprint(manifest: Manifest) -> Path | None:
    """Once packaged, the working blueprint in `YAMLs/temp` goes: its record is
    the copy in the run folder (D162). Kept, and said, if it was changed after
    the split. Returns the file removed."""
    import hashlib

    from .pulls import BLUEPRINT_SUFFIX, record_blueprint, run_folder

    source = manifest.source or {}
    written = str(source.get("template") or "")
    if not written:
        return None
    path = Path(written)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not (path.is_file() and path.name.endswith(BLUEPRINT_SUFFIX) and path.parent.name == "temp"):
        return None
    record = record_blueprint(run_folder(manifest.path)) if manifest.path else None
    expected = source.get("template_sha256")
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected is None and record is not None:
        expected = hashlib.sha256(record.read_bytes()).hexdigest()
    if current != expected:
        print(f"Kept {path.name} in YAMLs/temp: it was changed after this pull was split. "
              "Run it again, or delete it if the change is not wanted.")
        return None
    try:
        path.unlink()
    except OSError as exc:
        print(f"warning  could not remove {path} ({exc}); delete it by hand.", file=sys.stderr)
        return None
    where = f" {record}" if record is not None else " the run folder"
    print(f"Removed {path.name} from YAMLs/temp: the pull is packaged, and its blueprint is "
          f"kept in{where}. Author opens it there.")
    return path


def record_execute(method, *args) -> None:
    """Write how Execute began or ended; a manifest that cannot be written
    must not hide why the pull stopped."""
    try:
        method(*args)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        print(f"warning  could not record how Execute ended in the manifest: {exc}",
              file=sys.stderr)


def artifacts(manifest: Manifest, args: argparse.Namespace, connect_fn=None, *,
              own_lock: bool = False) -> int:
    """Back the pull up (D149), then package it (D72): nothing is replaced
    without a copy. Where the backup went instead, said last, as a warning."""
    from .backup import backup_pull, has_parquets
    from .lock import held_message, live_lock
    from .pulls import run_folder

    held = None if own_lock else live_lock(manifest.path)
    if held is not None:
        print(f"ERROR {held_message(held, manifest.path)} Package it once it has finished.",
              file=sys.stderr)
        return 1
    folder = run_folder(manifest.path)
    saved = None
    if has_parquets(folder):
        try:
            saved = backup_pull(folder, Path.cwd())
        except OSError as exc:
            print(f"ERROR the pull could not be backed up ({exc}), so its parquets were not "
                  "replaced. Free space, or choose another backup folder in Run, then run "
                  "Artifacts again.", file=sys.stderr)
            return 1
        print(saved.line())
    code = _package(manifest, args, connect_fn, own_lock=own_lock)
    if saved is not None and saved.fell_back:
        print(f"WARNING Backed up to {saved.destination}, not the backup folder: "
              f"{saved.fell_back}. Choose or reconnect the backup folder in Run, then "
              "Back up all.", file=sys.stderr)
    return code


def backup_all(cwd: Path | None = None) -> int:
    """Back up every pull under the runs folder (D149), skipping one that is
    executing, and say where each went."""
    from .backup import backup_pull
    from .pulls import find_pulls, run_folder

    home = Path(cwd or Path.cwd())
    pulls = find_pulls(home)
    if not pulls:
        print("There are no pulls to back up.")
        return 0
    failed = 0
    fell_back = []
    for pull in pulls:
        if pull.lock is not None:
            print(f"{pull.name}: skipped, executing now; back it up once it has finished.")
            continue
        try:
            saved = backup_pull(run_folder(pull.manifest), home)
        except OSError as exc:
            print(f"ERROR {pull.name} could not be backed up: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(saved.line())
        if saved.fell_back:
            fell_back.append(saved)
    if fell_back:
        print(f"WARNING {len(fell_back)} pull(s) went to {fell_back[0].destination.parent}, not the "
              f"backup folder: {fell_back[0].fell_back}.", file=sys.stderr)
    return 1 if failed else 0


def scan_runs(cwd: Path | None = None) -> int:
    """Write and show the run scan (D152); exit 1 when something did not check out."""
    from .scan import scan_runs as scan

    path, text, count = scan(Path(cwd or Path.cwd()))
    print(text, end="")
    print(f"Written to {path}")
    return 1 if count else 0


def audit_dictionary(args: argparse.Namespace, connect_fn=None, cwd: Path | None = None) -> int:
    """Write and show the dictionary audit (D155); exit 1 when something is wrong."""
    from . import config
    from .audit import audit_dictionary as audit, write_report
    from .contents import dictionary_path, load_dictionary
    from .db import DatabaseError, Settings, connect, load_env_file
    from .normalize import cosmos_database

    path = dictionary_path()
    dictionary = load_dictionary(path)
    if not dictionary:
        print(f"ERROR no data dictionary found (looked at {path}).", file=sys.stderr)
        return 1
    try:
        load_env_file(args.env)
    except DatabaseError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    settings = Settings.from_env()
    database = cosmos_database(None)
    print(f"Auditing {path} against {database}: {len(dictionary)} tables ...", flush=True)
    try:
        connection = (connect_fn or connect)(
            settings.cosmos_connection_string(database),
            login_timeout=settings.login_timeout, query_timeout=settings.query_timeout,
        )
    except DatabaseError as exc:
        print(f"ERROR could not connect to Cosmos: {exc}", file=sys.stderr)
        return 1
    try:
        result = audit(connection, dictionary, database)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    home = Path(cwd or Path.cwd())
    written, text = write_report(result, config.runs_dir(home), config.bundle_id())
    print(text, end="")
    print(f"Written to {written}")
    return 1 if result.wrong else 0


def _package(manifest: Manifest, args: argparse.Namespace, connect_fn=None, *,
             own_lock: bool = False) -> int:
    """Package the pull's finished tables (D72), then describe them (D73, D75).

    `own_lock`: called by the Execute that holds the pull's lock (D141),
    which is not another Execute to wait for.
    """
    import time

    from .artifacts import ArtifactError, package, parquets_folder, seconds_text, size_text
    from .db import DatabaseError, Settings, connect, find_env_file, load_env_file
    from .lock import held_message, live_lock, pull_name

    held = None if own_lock else live_lock(manifest.path)
    if held is not None:
        print(f"ERROR {held_message(held, manifest.path)} Package it once it has finished.",
              file=sys.stderr)
        return 1
    try:
        load_env_file(args.env)
    except DatabaseError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    settings = Settings.from_env()
    project_db = str(manifest.project.get("project_db") or "")
    out = parquets_folder(manifest.path)
    started = time.monotonic()
    print(f"Artifacts {pull_name(manifest.path)}: {shown(out)}")
    try:
        connection = (connect_fn or connect)(
            settings.projects_connection_string(project_db),
            login_timeout=settings.login_timeout,
            query_timeout=settings.query_timeout,
        )
    except DatabaseError as exc:
        print(f"ERROR could not connect to Projects: {exc}", file=sys.stderr)
        return 1
    try:
        result = package(manifest, connection, out)
    except ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a driver error, naming the table in its SQL
        print(f"ERROR packaging stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            connection.close()
        except Exception:
            pass
    for dest, why in result.left_out:
        print(f"  left out {dest}: {why}")
    from .contents import render
    from .pulls import run_folder

    from .loaders import write_loaders

    run_dir = run_folder(manifest.path)
    contents = run_dir / "contents.md"
    contents.write_text(render(manifest, result), encoding="utf-8")
    others = [contents, *write_loaders(run_dir, out, pull_name(manifest.path))]
    for path in others[1:]:
        print(f"  wrote    {shown(path)}")
    print(f"  wrote    {shown(contents)}")

    # Everything this packaging made, in one place (D88).
    parts = [part for spec in result.tables for part in spec.parts]
    print()
    print(f"Files written, in {shown(run_dir)}:")
    for part in parts:
        print(f"  {part.path.relative_to(run_dir).as_posix()}  {part.rows:,} rows, {size_text(part.path)}")
    for path in others:
        print(f"  {path.relative_to(run_dir).as_posix()}")
    if result.failed:
        print()
        print("Failed (not packaged; the rest were):")
        for dest, why in result.failed:
            print(f"  {dest}: {why}")
    print()
    print(
        f"Artifacts finished in {seconds_text(time.monotonic() - started)}: "
        f"{len(result.tables)} table(s) in {len(parts)} parquet file(s), "
        f"{sum(part.rows for part in parts):,} rows, {len(result.left_out)} left out, "
        f"{len(result.failed)} failed. In {shown(run_dir)}: contents.md describes "
        "them, HOW_TO.md says how to open them."
    )
    if result.failed:
        print("Run Artifacts again once the cause is fixed: each run replaces the last.")
        return 1
    return 0


def keep_open(code: int, input_fn=input) -> None:
    """Hold the console window Execute runs in until `exit` is typed (D68).

    Only `exit` closes it, so an Enter pressed by accident does not lose the
    output. The pull is over by now and its lock released.
    """
    print()
    print(
        f"Safe to close: the pull has finished (exit code {code}). "
        "Type exit and press Enter to close this window."
    )
    while True:
        try:
            answer = input_fn("> ")
        except (EOFError, KeyboardInterrupt):
            return
        if answer.strip().lower() == "exit":
            return


def set_console_title(text: str) -> None:
    """Name the console window, on Windows; elsewhere nothing."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


# Windows console input modes (SetConsoleMode).
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


def quick_edit_off(kernel32=None) -> bool:
    """Turn off QuickEdit in this console window, on Windows (D143).

    With it on, a click in the window starts a selection ("Select" in the
    title) and every print waits until it is cleared, so a pull that has
    finished its work hangs before its summary, holding its lock. Returns
    whether it was turned off; anywhere else, or with no console, nothing.
    """
    if kernel32 is None:
        if os.name != "nt":
            return False
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
        except Exception:
            return False
    try:
        import ctypes

        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a console: redirected, or a terminal of its own
        wanted = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        return bool(kernel32.SetConsoleMode(handle, wanted))
    except Exception:
        return False


def _execute(manifest: Manifest, args: argparse.Namespace, connect_fn=None,
             pulled: list | None = None) -> int:
    """Every session with work left, in turn. `pulled` gets each step this
    Execute completed, which says whether there is anything new to package."""
    from . import refresh
    from .db import DatabaseError, Settings, connect, find_env_file, load_env_file
    from .normalize import cosmos_database
    from .session import SessionError, SessionRunner

    try:
        loaded = load_env_file(args.env)
    except DatabaseError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    if loaded:
        print(f"Loaded {len(loaded)} setting(s) from {find_env_file(args.env)}")
    elif not args.env:
        print("No .env found; using environment variables and defaults.")

    settings = Settings.from_env()
    connect_fn = connect_fn or connect
    if not repull_chosen(manifest, args):
        return 1
    if args.repull:
        manifest.reset_all("re-pulled: --repull")
        print("--repull: every session starts over, finished work included.")

    # D51: before anything is skipped as finished, make sure Cosmos has not
    # been rebuilt since it was pulled.
    try:
        probe = connect_fn(
            settings.cosmos_connection_string(cosmos_database(None)),
            login_timeout=settings.login_timeout,
            query_timeout=settings.query_timeout,
        )
    except DatabaseError as exc:
        print(f"ERROR could not connect to Cosmos: {exc}", file=sys.stderr)
        return 1
    try:
        stamps = refresh.read_stamps(probe)
    except Exception as exc:
        stamps = {}
        print(f"WARNING could not read Cosmos's create_date ({exc}); a refresh cannot be detected.")
    finally:
        try:
            probe.close()
        except Exception:
            pass
    for line in refresh.reconcile(manifest, stamps, refresh.databases_used(manifest)):
        print(line)
    manifest.save()

    reports = []
    idle_failures = 0
    for session in manifest.sessions:
        print(f"=== {session.session_id} ===")
        if not session_has_work(manifest, session, retry_failed=args.retry_failed):
            print(f"  {next_step(manifest, session, args.retry_failed)}")
            print()
            if any(run.status == FAILED for run in session.runs):
                idle_failures += 1
            continue
        runner = SessionRunner(
            manifest, session, settings, connect_fn=connect_fn, retry_failed=args.retry_failed
        )
        try:
            with runner:
                report = runner.execute()
        except (DatabaseError, SessionError) as exc:
            print(f"  could not open the session: {exc}", file=sys.stderr)
            # Sessions have independent connections and PKs, so the next one
            # still gets its chance.
            reports.append(None)
            continue
        reports.append(report)
        if pulled is not None:
            pulled.extend(report.completed)
        print(f"  epoch {report.epoch} on {report.linked_server}")
        for label in report.completed:
            print(f"  done     {label}")
            for line in table_lines(report.tables.get(label), "             "):
                print(line)
        for label in report.skipped:
            print(f"  skipped  {label}")
        for label, message in report.failed:
            print(f"  FAILED   {label}: {message}")
            landed = table_lines(report.tables.get(label), "             ")
            if landed:
                print("           landed before it failed (a retry pulls them again):")
                for line in landed:
                    print(line)
        for warning in report.warnings:
            print(f"  warning  {warning}")
        for line in width_notes(report.widths):
            print(line)
        for line in per_key_notes(report.per_key):
            print(line)
        print()
    failures = [r for r in reports if r is None or not r.ok]
    ran = len(reports)
    print(f"{ran - len(failures)}/{ran} session(s) run completed; "
          f"{len(manifest.sessions) - ran} had nothing to pull.")
    print(f"Manifest updated: {manifest.path}")
    return 1 if failures or idle_failures else 0


def width_notes(widths: dict[tuple[str, str], list]) -> list[str]:
    """One table per session of the widest value in each text column (D70).

    Across all of the session's batches, once, after its warnings: notes, not
    warnings, since widths are measured and never applied (D34).
    """
    if not widths:
        return []
    header = ("Table", "Column", "Declared", "Widest")
    rows = [(dest, column, declared or "-", str(widest))
            for (dest, column), (declared, widest) in widths.items()]
    size = [max(len(row[i]) for row in (header, *rows)) for i in range(4)]
    lines = ["  note     Column widths: the widest value stored in each text column, "
             "across the session's batches. Measured, not applied."]
    for row in (header, *rows):
        cells = [row[i].ljust(size[i]) for i in range(3)] + [row[3].rjust(size[3])]
        lines.append("           " + "  ".join(cells))
    return lines


def per_key_notes(per_key: dict[tuple[str, str], dict]) -> list[str]:
    """Each landed table's rows per join key, measured in Projects (D157)."""
    from .perkey import shown

    if not per_key:
        return []
    header = ("Step", "Table", "Key", "Keys", "Median", "P90", "Max")
    rows = [(step.split("/")[-1], dest, str(m.get("key", "")), shown(m.get("keys")),
             shown(m.get("median")), shown(m.get("p90")), shown(m.get("max")))
            for (step, dest), m in per_key.items()]
    size = [max(len(row[i]) for row in (header, *rows)) for i in range(7)]
    lines = ["  note     Rows per join key: how many rows each key brought, in each table "
             "each step landed. 1, 1, 1 is one row per key."]
    for row in (header, *rows):
        cells = [row[i].ljust(size[i]) for i in range(3)] + [row[i].rjust(size[i]) for i in range(3, 7)]
        lines.append("           " + "  ".join(cells))
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pullmanager",
        description="Manifest-driven executor for YAML Manager split pull folders.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        help="Path to pullmanifest.yaml. With --execute, a project's name will do: "
             "IBD_Ancestry means runs/IBD_Ancestry/split/pullmanifest.yaml.",
    )
    from .config import bundle_id

    found = bundle_id()
    parser.add_argument("--version", action="version",
                        version=f"pullmanager {__version__}" + (f", bundle {found}" if found else ""))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the SQL each phase implies without touching a database.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the manifest against live connections, updating it as it goes. "
             "Takes a project's name or a manifest; with neither, lists the pulls.",
    )
    parser.add_argument(
        "--artifacts",
        action="store_true",
        help="Package a pull's finished tables as parquets in runs/<project>/, with "
             "contents.md and load scripts. Takes a project's name or a manifest.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="After the command, keep the window open until exit is typed. The "
             "launcher's Execute uses it for the console window it opens.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Back up every pull under runs/ to the backup folder Run names, or to runs/backup "
             "when it cannot be reached (D149).",
    )
    parser.add_argument(
        "--scan-runs",
        action="store_true",
        help="Compare what every pull under runs/ built with what it packaged, and write "
             "what does not check out to runs/run_scan.yaml (D152).",
    )
    parser.add_argument(
        "--audit-dictionary",
        action="store_true",
        help="Ask Cosmos for every dictionary table's columns and write what the dictionary "
             "lists and Cosmos lacks to runs/dictionary_audit.yaml (D155).",
    )
    parser.add_argument(
        "--running",
        action="store_true",
        help="List every pull under runs/, whether it is executing, and its command.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the app: Author and Run in one window (D93; also what no arguments "
             "does). Uses tkinter, which ships with Python.",
    )
    parser.add_argument("--out-dir", default=None, help="Write rendered SQL here (dry run).")
    parser.add_argument(
        "--linked-server",
        default=None,
        help="Cosmos instance to render OPENQUERY against. Captured per connection at "
             "run time; supply one only for a dry run.",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Path to a .env holding host and database names. Searched in the working "
             "directory and beside the runtime when not given.",
    )
    parser.add_argument("--retry-failed", action="store_true", help="Reopen failed work.")
    parser.add_argument(
        "--repull",
        action="store_true",
        help="Start every session over, finished work included. Without it, finished "
             "sessions are skipped and unfinished ones resume.",
    )
    parser.add_argument(
        "--repull-session",
        action="append",
        metavar="SESSION",
        help="Start this session over, finished work included; repeat for more. A case "
             "brings its controls; `all` is --repull (D158).",
    )
    parser.add_argument("--all", action="store_true", help="Include already-settled work.")
    parser.add_argument("-v", "--verbose", action="store_true", help="List every SQL block.")
    parser.add_argument(
        "--tdd",
        nargs="?",
        const="__all__",
        metavar="MODULE",
        help="Run the embedded test suite, optionally limited to one module.",
    )
    return parser


def main(argv: list[str] | None = None, input_fn=input) -> int:
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(raw)
    if not raw:
        # The launcher is what is run most (D63); the commands take arguments.
        args.gui = True
    if args.keep_open:
        set_console_title(f"Pullmanager: executing {args.manifest or ''}".rstrip())
    try:
        code = dispatch(parser, args)
    except Exception:
        traceback.print_exc()  # still keep the window open to read it
        code = 1
    if args.keep_open:
        keep_open(code, input_fn)
    return code


def dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:

    if args.tdd is not None:
        from .tests import run as run_tests

        return run_tests(None if args.tdd == "__all__" else args.tdd)

    if args.gui:
        try:
            from .app import main as gui_main
        except ImportError as exc:
            print(
                f"ERROR the launcher needs tkinter, which this Python lacks ({exc}). "
                "Everything it does is also available as --dry-run and --execute.",
                file=sys.stderr,
            )
            return 1
        return gui_main()

    if args.backup:
        return backup_all()

    if args.scan_runs:
        return scan_runs()

    if args.audit_dictionary:
        return audit_dictionary(args)

    if args.running:
        for line in listing(heading=f"Pulls under {Path('runs')}{os.sep}:"):
            print(line)
        return 0

    if (args.execute or args.artifacts) and not args.dry_run:
        # D66: a project's name finds its manifest; no name lists the pulls.
        if not args.manifest:
            for line in listing(option="--artifacts" if args.artifacts else "--execute"):
                print(line)
            return 1
        try:
            args.manifest = str(resolve(args.manifest))
        except PullNotFound as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 1

    if not args.manifest:
        parser.print_help()
        return 1

    if args.linked_server is None:
        from .executor import DRY_RUN_LINKED_SERVER

        args.linked_server = DRY_RUN_LINKED_SERVER

    try:
        manifest = Manifest.load(args.manifest)
        if args.dry_run and args.execute:
            print("--dry-run and --execute are mutually exclusive.", file=sys.stderr)
            return 1
        if args.dry_run:
            return dry_run(manifest, args)
        if args.artifacts:
            return artifacts(manifest, args)
        if args.execute:
            return execute(manifest, args)
        summarize(manifest)
    except (ManifestError, PlanError, NamingError, NormalizationError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
