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
from .manifest import Manifest, ManifestError
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
        for run in session.runs:
            batch = run.batch.get("name") if run.batch else "-"
            print(f"    run   {batch:<15} {run.status:<8} {run.yaml}")
        print()


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


def dry_run(manifest: Manifest, args: argparse.Namespace) -> int:
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
        try:
            if pull_lock.replaced:
                stale = pull_lock.replaced
                print(
                    f"Took over a stale lock: {stale.holder()} stopped without cleaning up "
                    f"(last heartbeat {clock_time(stale.heartbeat)})."
                )
            return _execute(manifest, args, connect_fn)
        except Exception:
            # Each step records its own failure; this is anything else, which
            # used to reach only the console, and vanish when it closed.
            print("ERROR Execute stopped on an error it did not expect. What it was working "
                  "on stays 'running', and the next --execute pulls it again:", file=sys.stderr)
            traceback.print_exc()
            return 1
        except KeyboardInterrupt:
            print(
                "\nStopped (Ctrl+C). Whatever it was working on stays 'running' in the "
                "manifest; the next --execute pulls it again."
            )
            return 130
        finally:
            pull_lock.release()


def artifacts(manifest: Manifest, args: argparse.Namespace, connect_fn=None) -> int:
    """Package the pull's finished tables (D72), then describe them (D73, D75)."""
    import time

    from .artifacts import ArtifactError, package, parquets_folder, seconds_text, size_text
    from .db import DatabaseError, Settings, connect, find_env_file, load_env_file
    from .lock import held_message, live_lock, pull_name

    held = live_lock(manifest.path)
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


def _execute(manifest: Manifest, args: argparse.Namespace, connect_fn=None) -> int:
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
        print(f"  epoch {report.epoch} on {report.linked_server}")
        for label in report.completed:
            print(f"  done     {label}")
        for label in report.skipped:
            print(f"  skipped  {label}")
        for label, message in report.failed:
            print(f"  FAILED   {label}: {message}")
        for warning in report.warnings:
            print(f"  warning  {warning}")
        for line in width_notes(report.widths):
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
    parser.add_argument("--version", action="version", version=f"pullmanager {__version__}")
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
        "--running",
        action="store_true",
        help="List every pull under runs/, whether it is executing, and its command.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the desktop launcher (also what no arguments does). Uses tkinter, "
             "which ships with Python.",
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
            from .gui import main as gui_main
        except ImportError as exc:
            print(
                f"ERROR the launcher needs tkinter, which this Python lacks ({exc}). "
                "Everything it does is also available as --dry-run and --execute.",
                file=sys.stderr,
            )
            return 1
        return gui_main()

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
