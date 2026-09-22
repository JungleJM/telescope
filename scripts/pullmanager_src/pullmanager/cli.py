"""Command line entry point.

Phase 2 scope: load and inspect a manifest. Execution arrives in Phase 5.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .manifest import Manifest, ManifestError


def summarize(manifest: Manifest) -> None:
    project = manifest.project
    print(f"Project:  {project.get('name')}  ({project.get('project_db')})")
    print(f"Manifest: {manifest.path}")
    print(f"Sessions: {len(manifest.sessions)}")
    print()
    for session in manifest.sessions:
        pk_source = next(
            (phase.pk_source for phase in session.phases if phase.pk_source), None
        )
        kind = pk_source.get("kind") if pk_source else "none"
        print(f"  {session.session_id}  [{session.status}]  pk={session.pk_table} ({kind})")
        for phase in session.phases:
            print(f"    phase {phase.name:<15} {phase.status:<8} {phase.yaml}")
        for run in session.runs:
            batch = run.batch.get("name") if run.batch else "-"
            print(f"    run   {batch:<15} {run.status:<8} {run.yaml}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pullmanager",
        description="Manifest-driven executor for YAML Manager split pull folders.",
    )
    parser.add_argument("manifest", nargs="?", help="Path to pullmanifest.yaml")
    parser.add_argument("--version", action="version", version=f"pullmanager {__version__}")
    parser.add_argument(
        "--tdd",
        nargs="?",
        const="__all__",
        metavar="GROUP",
        help="Run the embedded test suite, optionally limited to one module (e.g. manifest).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tdd is not None:
        from .tests import run as run_tests

        return run_tests(None if args.tdd == "__all__" else args.tdd)

    if not args.manifest:
        parser.print_help()
        return 1

    try:
        manifest = Manifest.load(args.manifest)
    except ManifestError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    summarize(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
