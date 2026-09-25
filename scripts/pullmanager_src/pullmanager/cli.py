"""Command line entry point: summarize, preview (--dry-run) or execute a pull."""

from __future__ import annotations

import argparse
import sys
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
        print()
    failures = [r for r in reports if r is None or not r.ok]
    ran = len(reports)
    print(f"{ran - len(failures)}/{ran} session(s) run completed; "
          f"{len(manifest.sessions) - ran} had nothing to pull.")
    print(f"Manifest updated: {manifest.path}")
    return 1 if failures or idle_failures else 0


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(raw)
    if not raw:
        # The launcher is what is run most (D63); the commands take arguments.
        args.gui = True

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

    if args.execute and not args.dry_run:
        # D66: a project's name finds its manifest; no name lists the pulls.
        if not args.manifest:
            for line in listing():
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
        if args.execute:
            return execute(manifest, args)
        summarize(manifest)
    except (ManifestError, PlanError, NamingError, NormalizationError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
