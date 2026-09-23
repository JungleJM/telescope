#!/usr/bin/env python3
"""Pullmanager bundle - GENERATED FILE, DO NOT EDIT.

Built by scripts/bundle_pullmanager.py from scripts/pullmanager_src/.
To change anything here, edit the source module and rebuild the bundle.

    python pullmanager_bundle.py --verify-bundle
    python pullmanager_bundle.py --list
    python pullmanager_bundle.py --extract ./pullmanager_runtime
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

BEGIN_RE = re.compile(
    r"^# === BEGIN FILE: (?P<path>\S+) SHA256: (?P<sha>[0-9a-f]{64}) SIZE: (?P<size>\d+) ===$"
)
END_RE = re.compile(r"^# === END FILE: (?P<path>\S+) ===$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")

MANIFEST_FILENAME = ".bundle-manifest.json"


class BundleError(Exception):
    """Raised when a bundle is malformed, tampered with, or unsafe."""


def safe_relpath(raw: str) -> str:
    """Validate an embedded path, refusing anything that could escape the target."""
    if not raw or raw.strip() != raw:
        raise BundleError(f"Empty or padded path in bundle: {raw!r}")
    if "\\" in raw:
        raise BundleError(f"Backslash in bundle path (use POSIX separators): {raw!r}")
    if raw.startswith("/"):
        raise BundleError(f"Absolute path in bundle: {raw!r}")
    if DRIVE_RE.match(raw):
        raise BundleError(f"Drive-qualified path in bundle: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BundleError(f"Path traversal or empty segment in bundle path: {raw!r}")
    return raw


def decode_payload_lines(lines: list[str], path: str) -> str:
    out: list[str] = []
    for line in lines:
        if line == "#":
            out.append("")
        elif line.startswith("# "):
            out.append(line[2:])
        else:
            raise BundleError(
                f"Corrupt payload line in {path!r} (expected a comment-prefixed line): {line!r}"
            )
    return "\n".join(out)


def parse_payload(text: str) -> list[dict]:
    """Pull every file section out of a bundle's text, in bundle order."""
    sections: list[dict] = []
    current: dict | None = None
    body: list[str] = []

    for lineno, line in enumerate(text.split("\n"), start=1):
        begin = BEGIN_RE.match(line)
        if begin:
            if current is not None:
                raise BundleError(
                    f"Line {lineno}: BEGIN for {begin.group('path')!r} inside unterminated "
                    f"section {current['path']!r}"
                )
            current = {
                "path": safe_relpath(begin.group("path")),
                "sha256": begin.group("sha"),
                "size": int(begin.group("size")),
            }
            body = []
            continue

        end = END_RE.match(line)
        if end:
            if current is None:
                raise BundleError(f"Line {lineno}: END for {end.group('path')!r} with no open section")
            if end.group("path") != current["path"]:
                raise BundleError(
                    f"Line {lineno}: END path {end.group('path')!r} does not match "
                    f"BEGIN path {current['path']!r}"
                )
            current["content"] = decode_payload_lines(body, current["path"])
            sections.append(current)
            current = None
            body = []
            continue

        if current is not None:
            body.append(line)

    if current is not None:
        raise BundleError(f"Unterminated file section: {current['path']!r}")
    return sections


def compute_content_id(entries: list[dict]) -> str:
    """Stable identity for a bundle's contents, independent of build time."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e["path"]):
        digest.update(f"{entry['path']}\0{entry['sha256']}\0{entry['size']}\0".encode("utf-8"))
    return digest.hexdigest()


def verify_sections(sections: list[dict], declared: list[dict]) -> None:
    """Cross-check payload sections against the bundle's embedded manifest."""
    seen: set[str] = set()
    for section in sections:
        if section["path"] in seen:
            raise BundleError(f"Duplicate file section in bundle: {section['path']!r}")
        seen.add(section["path"])

    declared_by_path = {entry["path"]: entry for entry in declared}
    if len(declared_by_path) != len(declared):
        raise BundleError("Embedded manifest lists the same path more than once.")

    missing = sorted(set(declared_by_path) - seen)
    if missing:
        raise BundleError(f"Bundle manifest lists files with no payload section: {', '.join(missing)}")

    extra = sorted(seen - set(declared_by_path))
    if extra:
        raise BundleError(f"Bundle carries payload sections absent from its manifest: {', '.join(extra)}")

    for section in sections:
        entry = declared_by_path[section["path"]]
        raw = section["content"].encode("utf-8")
        if len(raw) != entry["size"]:
            raise BundleError(
                f"Size mismatch for {section['path']!r}: manifest says {entry['size']}, "
                f"payload decodes to {len(raw)}"
            )
        if section["sha256"] != entry["sha256"]:
            raise BundleError(
                f"Marker/manifest hash disagreement for {section['path']!r}"
            )
        actual = hashlib.sha256(raw).hexdigest()
        if actual != entry["sha256"]:
            raise BundleError(
                f"SHA-256 mismatch for {section['path']!r}: expected {entry['sha256']}, got {actual}"
            )


def read_bundle(bundle_path: Path) -> tuple[list[dict], dict]:
    text = bundle_path.read_text(encoding="utf-8")
    match = re.search(r"^BUNDLE_MANIFEST_JSON = r'''(?P<json>.*?)'''$", text, re.S | re.M)
    if not match:
        raise BundleError("Bundle is missing its embedded BUNDLE_MANIFEST_JSON block.")
    try:
        manifest = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"Embedded bundle manifest is not valid JSON: {exc}") from exc

    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise BundleError("Embedded bundle manifest has no `files` list.")

    sections = parse_payload(text)
    verify_sections(sections, declared)

    expected_id = manifest.get("content_id")
    actual_id = compute_content_id(declared)
    if expected_id != actual_id:
        raise BundleError(
            f"Bundle content_id mismatch: manifest says {expected_id}, computed {actual_id}"
        )
    return sections, manifest


def extract(bundle_path: Path, target: Path, force: bool = False) -> list[str]:
    """Verify a bundle fully, then swap its contents into `target`."""
    sections, manifest = read_bundle(bundle_path)

    target = target.resolve()
    if target.exists():
        if not target.is_dir():
            raise BundleError(f"Extraction target exists and is not a directory: {target}")
        owned = (target / MANIFEST_FILENAME).is_file()
        if not owned and not force:
            raise BundleError(
                f"Refusing to replace {target}: it is not a previous bundle extraction "
                f"(no {MANIFEST_FILENAME}). Pass --force to overwrite it anyway."
            )

    staging = target.with_name(target.name + ".extract-tmp")
    previous = target.with_name(target.name + ".old-tmp")
    for scratch in (staging, previous):
        if scratch.exists():
            shutil.rmtree(scratch)

    try:
        for section in sections:
            out_path = staging / section["path"]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # Explicit bytes so Windows does not translate newlines and break hashes.
            out_path.write_bytes(section["content"].encode("utf-8"))

        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # Re-read from disk: proves what landed matches, not just what we held.
        for section in sections:
            written = (staging / section["path"]).read_bytes()
            if hashlib.sha256(written).hexdigest() != section["sha256"]:
                raise BundleError(f"Post-write verification failed for {section['path']!r}")

        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if previous.exists() and not target.exists():
            previous.rename(target)
        raise
    finally:
        if previous.exists() and target.exists():
            shutil.rmtree(previous, ignore_errors=True)

    return [section["path"] for section in sections]


def bundle_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Self-extracting Pullmanager bundle.",
    )
    parser.add_argument("--verify-bundle", action="store_true", help="Verify this bundle and exit.")
    parser.add_argument("--list", action="store_true", help="List the files this bundle carries.")
    parser.add_argument("--extract", metavar="DIR", help="Verify, then extract into DIR.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --extract to replace a directory that is not a previous extraction.",
    )
    args = parser.parse_args(argv)

    bundle_path = Path(__file__).resolve()

    if not (args.verify_bundle or args.list or args.extract):
        parser.print_help()
        return 1

    try:
        if args.verify_bundle or args.list:
            sections, manifest = read_bundle(bundle_path)
            if args.list:
                for section in sections:
                    print(f"{section['size']:>8}  {section['sha256'][:12]}  {section['path']}")
            if args.verify_bundle:
                print(f"OK  {len(sections)} files verified")
                print(f"content_id: {manifest['content_id']}")

        if args.extract:
            written = extract(bundle_path, Path(args.extract), force=args.force)
            for path in written:
                print(f"extracted  {path}")
            print(f"\nExtracted {len(written)} files to {Path(args.extract).resolve()}")
    except BundleError as exc:
        print(f"BUNDLE ERROR: {exc}", file=sys.stderr)
        return 2

    return 0

BUNDLE_MANIFEST_JSON = r'''{
  "bundle_format_version": 1,
  "content_id": "bbe771356c7f6872916607be8f81957566c36bc0471cadf553ad652b8cad8015",
  "file_count": 24,
  "files": [
    {
      "path": "pullmanager.py",
      "sha256": "ebdc02f9ba0685fc16b3aaea58c6563e0b91f3e69bcbce40bf953e414f787add",
      "size": 408
    },
    {
      "path": "pullmanager/__init__.py",
      "sha256": "2b6c4b6cedb40b106d374887a4180e7a03259c5fa7d8a66a6932b65ce73123c0",
      "size": 126
    },
    {
      "path": "pullmanager/__main__.py",
      "sha256": "307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7",
      "size": 79
    },
    {
      "path": "pullmanager/cli.py",
      "sha256": "44c18978b46a38ed06b4ea378204ae27fd210d28a67b8ed987c6db5f6782897a",
      "size": 6251
    },
    {
      "path": "pullmanager/db.py",
      "sha256": "0e2306de7b9f2a6ffebda577a21c24b4478a38e518eba9ea90de4187307756d9",
      "size": 9394
    },
    {
      "path": "pullmanager/executor.py",
      "sha256": "fd658d58f45a49875790f6a119cc8b160cd736fbce5f861331f5f2ae94dd79ea",
      "size": 8444
    },
    {
      "path": "pullmanager/local_sql.py",
      "sha256": "2effa5f5fc36cbafa0a72c872f3f3e362f6a5d8d9608c356fa6663f1697f3130",
      "size": 7093
    },
    {
      "path": "pullmanager/manifest.py",
      "sha256": "1ccaab95c26ba0f76151c64e58e2b5988e60558ae06a9ec1f4e082df23ca4508",
      "size": 11210
    },
    {
      "path": "pullmanager/models.py",
      "sha256": "2eb665cf240632931fdb1f07b9b4d6e04757c51fb188ea3d17c184c60cdf87be",
      "size": 2154
    },
    {
      "path": "pullmanager/naming.py",
      "sha256": "45595539e6045bcc4b431379e8adebb7556793d3246cf7921a79eb16e5afc7c4",
      "size": 3764
    },
    {
      "path": "pullmanager/normalize.py",
      "sha256": "dee580d39bedc3b6fd2bbcb5ed79c4a86e2914bc4b49558d9fc4c582fd89891e",
      "size": 7036
    },
    {
      "path": "pullmanager/server_sql.py",
      "sha256": "06a74220bcca9ee66fb6ee439272e09910bf3b3913826dec4c69707bebe4887d",
      "size": 7865
    },
    {
      "path": "pullmanager/sql.py",
      "sha256": "35ff5620f0075f8d5db26c0be551717f01a063769339c01e21ea445a5bd7f9d0",
      "size": 5255
    },
    {
      "path": "pullmanager/tests/__init__.py",
      "sha256": "4f70b04f739db1fd4fdae89aa3b2dd3ac8afea47a8dc6b8d5eb7fcda8ed717a2",
      "size": 1508
    },
    {
      "path": "pullmanager/tests/support.py",
      "sha256": "5c88b19c05ea4b105763db67d73e42d88cbe1c3897b2ece74c9f287b60403398",
      "size": 4541
    },
    {
      "path": "pullmanager/tests/test_db.py",
      "sha256": "1e39f445c46b0952540a09c0887b210ad566223b239be4ed607065ade5987be7",
      "size": 10152
    },
    {
      "path": "pullmanager/tests/test_executor.py",
      "sha256": "f1907b950db229ff68fbcb7c455df5dd7e9b735a2b130f71f20ca8b5164ce488",
      "size": 6609
    },
    {
      "path": "pullmanager/tests/test_manifest.py",
      "sha256": "78f8843188969abfa24793cbd298a3e24ede337d3ebb80a5a3a7c1c365f42ff7",
      "size": 14639
    },
    {
      "path": "pullmanager/tests/test_models.py",
      "sha256": "9293f87dcebe251868ceb465460b0067c9642790941c6e26250a6cb8d47044a8",
      "size": 2221
    },
    {
      "path": "pullmanager/tests/test_naming.py",
      "sha256": "52969558fa458ea931e5baf62abbb75426294cdc3cc854a8f867fefbad0c5d5f",
      "size": 5298
    },
    {
      "path": "pullmanager/tests/test_normalize.py",
      "sha256": "7f8df67188a3032b297e0c22ce7e20861ce4e526c1b710bab86b2bd3d56d2f10",
      "size": 5859
    },
    {
      "path": "pullmanager/tests/test_render.py",
      "sha256": "9ab4a4cb4c05f41e6f4a82247ac259568716dc17ac7488e9ea0372bcf816a9c8",
      "size": 8919
    },
    {
      "path": "pullmanager/tests/test_sql.py",
      "sha256": "70f3bfde2d04c0ab5dc3df2d063182f2684f908b04446d708049f1ee40c1cc35",
      "size": 5285
    },
    {
      "path": "pullmanager/yaml_io.py",
      "sha256": "dca04d852f7873c8abcac4d0e9f0f0e1883c96117a9bcacdf7766fbae4255c18",
      "size": 1844
    }
  ]
}'''


if __name__ == "__main__":
    raise SystemExit(bundle_main())

# === BEGIN FILE: pullmanager.py SHA256: ebdc02f9ba0685fc16b3aaea58c6563e0b91f3e69bcbce40bf953e414f787add SIZE: 408 ===
# #!/usr/bin/env python3
# """Launcher for the extracted Pullmanager runtime.
#
# Sits beside the `pullmanager/` package so the VM can run:
#
#     python pullmanager_runtime/pullmanager.py split/pullmanifest.yaml
# """
#
# import sys
# from pathlib import Path
#
# sys.path.insert(0, str(Path(__file__).resolve().parent))
#
# from pullmanager.cli import main  # noqa: E402
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager.py ===
# === BEGIN FILE: pullmanager/__init__.py SHA256: 2b6c4b6cedb40b106d374887a4180e7a03259c5fa7d8a66a6932b65ce73123c0 SIZE: 126 ===
# """Pullmanager: manifest-driven executor for YAML Manager split pull folders."""
#
# __version__ = "0.2.0"
#
# MANIFEST_VERSION = 1
#
# === END FILE: pullmanager/__init__.py ===
# === BEGIN FILE: pullmanager/__main__.py SHA256: 307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7 SIZE: 79 ===
# from .cli import main
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager/__main__.py ===
# === BEGIN FILE: pullmanager/cli.py SHA256: 44c18978b46a38ed06b4ea378204ae27fd210d28a67b8ed987c6db5f6782897a SIZE: 6251 ===
# """Command line entry point.
#
# Phase 5 scope: inspect a manifest and render the SQL it implies. Execution
# arrives in Phase 6.
# """
#
# from __future__ import annotations
#
# import argparse
# import sys
# from pathlib import Path
#
# from . import __version__
# from .executor import (
#     RESUME_FULL,
#     RESUME_PARTIAL,
#     PlanError,
#     excluded_units,
#     plan,
#     write_sql,
# )
# from .manifest import Manifest, ManifestError
# from .naming import NamingError
# from .normalize import NormalizationError
#
#
# def summarize(manifest: Manifest) -> None:
#     project = manifest.project
#     print(f"Project:  {project.get('name')}  ({project.get('project_db')})")
#     print(f"Manifest: {manifest.path}")
#     print(f"Sessions: {len(manifest.sessions)}")
#     print()
#     for session in manifest.sessions:
#         pk_source = next((p.pk_source for p in session.phases if p.pk_source), None)
#         kind = pk_source.get("kind") if pk_source else "none"
#         epoch = session.epoch or "-"
#         print(f"  {session.session_id}  [{session.status}]  pk={session.pk_table} ({kind})")
#         print(f"    epoch {epoch}")
#         for phase in session.phases:
#             stale = "  STALE" if phase.is_stale(session.epoch) else ""
#             print(f"    phase {phase.name:<15} {phase.status:<8} {phase.yaml}{stale}")
#         for run in session.runs:
#             batch = run.batch.get("name") if run.batch else "-"
#             stale = "  STALE" if run.is_stale(session.epoch) else ""
#             print(f"    run   {batch:<15} {run.status:<8} {run.yaml}{stale}")
#         print()
#
#
# def dry_run(manifest: Manifest, args: argparse.Namespace) -> int:
#     mode = RESUME_PARTIAL if args.resume_partial else RESUME_FULL
#     units = plan(
#         manifest,
#         linked_server=args.linked_server,
#         retry_failed=args.retry_failed,
#         include_settled=args.all,
#         mode=mode,
#     )
#     left_out = excluded_units(manifest, mode=mode, retry_failed=args.retry_failed)
#     failures = [row for row in left_out if row[1] == "failed"]
#
#     if not units:
#         print("Nothing to do.")
#         print("Every phase and run is either complete for this session or deliberately")
#         print("skipped. Use --retry-failed to reopen failures, or --all to render")
#         print("everything regardless of status.")
#         _report_exclusions(left_out, failures)
#         return 0
#
#     total_blocks = 0
#     for unit in units:
#         server, local = len(unit.server_blocks), len(unit.local_blocks)
#         total_blocks += server + local
#         print(f"{unit.unit_id}  [{unit.node.status}]  server={server} local={local}")
#         print(f"    why:  {unit.reason}")
#         for note in unit.notes:
#             print(f"    note: {note}")
#         if args.verbose:
#             for block in unit.blocks:
#                 print(f"    {block.side:<6} {block.block_id}")
#
#     print(f"\n{len(units)} unit(s), {total_blocks} SQL block(s).")
#     print(f"Resume mode: {mode}")
#     print(f"Linked server placeholder: {args.linked_server}")
#     print("Nothing was executed and the manifest was not modified.")
#     _report_exclusions(left_out, failures)
#
#     if args.out_dir:
#         written = write_sql(units, Path(args.out_dir))
#         print(f"\nWrote {len(written)} file(s) to {Path(args.out_dir).resolve()}")
#     return 0
#
#
# def _report_exclusions(left_out, failures) -> None:
#     if not left_out:
#         return
#     print(f"\nExcluded {len(left_out)} unit(s):")
#     for label, status, reason in left_out:
#         print(f"  {label:<40} [{status}]  {reason}")
#     if failures:
#         print(
#             f"\n{len(failures)} unit(s) failed previously and are NOT included. Rebuilding "
#             "the\nserver side without them would finish with nothing transferred. Fix the "
#             "cause,\nthen add --retry-failed."
#         )
#
#
# def build_parser() -> argparse.ArgumentParser:
#     parser = argparse.ArgumentParser(
#         prog="pullmanager",
#         description="Manifest-driven executor for YAML Manager split pull folders.",
#     )
#     parser.add_argument("manifest", nargs="?", help="Path to pullmanifest.yaml")
#     parser.add_argument("--version", action="version", version=f"pullmanager {__version__}")
#     parser.add_argument(
#         "--dry-run",
#         action="store_true",
#         help="Render the SQL each phase implies without touching a database.",
#     )
#     parser.add_argument("--out-dir", default=None, help="Write rendered SQL here (dry run).")
#     parser.add_argument(
#         "--linked-server",
#         default=None,
#         help="Cosmos instance to render OPENQUERY against. Captured per connection at "
#              "run time; supply one only for a dry run.",
#     )
#     parser.add_argument("--retry-failed", action="store_true", help="Reopen failed work.")
#     parser.add_argument(
#         "--resume-partial",
#         action="store_true",
#         help="Keep completed local transfers and replay only the server side. Server "
#              "state is gone either way; this trades a guard for not re-pulling.",
#     )
#     parser.add_argument("--all", action="store_true", help="Include already-settled work.")
#     parser.add_argument("-v", "--verbose", action="store_true", help="List every SQL block.")
#     parser.add_argument(
#         "--tdd",
#         nargs="?",
#         const="__all__",
#         metavar="MODULE",
#         help="Run the embedded test suite, optionally limited to one module.",
#     )
#     return parser
#
#
# def main(argv: list[str] | None = None) -> int:
#     parser = build_parser()
#     args = parser.parse_args(argv)
#
#     if args.tdd is not None:
#         from .tests import run as run_tests
#
#         return run_tests(None if args.tdd == "__all__" else args.tdd)
#
#     if not args.manifest:
#         parser.print_help()
#         return 1
#
#     if args.linked_server is None:
#         from .executor import DRY_RUN_LINKED_SERVER
#
#         args.linked_server = DRY_RUN_LINKED_SERVER
#
#     try:
#         manifest = Manifest.load(args.manifest)
#         if args.dry_run:
#             return dry_run(manifest, args)
#         summarize(manifest)
#     except (ManifestError, PlanError, NamingError, NormalizationError) as exc:
#         print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
#         return 1
#     return 0
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager/cli.py ===
# === BEGIN FILE: pullmanager/db.py SHA256: 0e2306de7b9f2a6ffebda577a21c24b4478a38e518eba9ea90de4187307756d9 SIZE: 9394 ===
# """Database adapter.
#
# pyodbc is imported lazily so the rest of the package -- planning, rendering,
# the dry run -- works on a machine without a driver.
#
# The behaviours here were harvested from the old generator rather than invented;
# see "Connection And Execution Facts" in pullmanager_contracts.md.
# """
#
# from __future__ import annotations
#
# import os
# from dataclasses import dataclass, field
# from typing import Any, Iterable, Iterator, Sequence
#
# DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
# DEFAULT_UPLOAD_CHUNK = 20_000
#
# # `GO` is a client batch separator, not T-SQL. The driver rejects it.
# BATCH_SEPARATOR = "GO"
#
#
# class DatabaseError(RuntimeError):
#     """A SQL failure, carrying the server messages that explain it."""
#
#     def __init__(self, message: str, server_messages: Sequence[str] = ()):
#         super().__init__(message)
#         self.server_messages = list(server_messages)
#
#     def __str__(self) -> str:
#         base = super().__str__()
#         if not self.server_messages:
#             return base
#         detail = "\n".join(f"  [SQL MESSAGE] {m}" for m in self.server_messages)
#         return f"{base}\n{detail}"
#
#
# @dataclass
# class Settings:
#     """Connection settings. Windows auth, so never credentials."""
#
#     cosmos_server: str = "COSMOS"
#     cosmos_database: str = "COSMOS"
#     projects_server: str = ""
#     projects_database: str = ""
#     driver: str = DEFAULT_DRIVER
#     login_timeout: int = 10
#     query_timeout: int = 0
#     upload_chunk: int = DEFAULT_UPLOAD_CHUNK
#
#     @classmethod
#     def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
#         source = os.environ if env is None else env
#
#         def get(name: str, fallback: str) -> str:
#             return str(source.get(f"PULLMANAGER_{name}", fallback) or fallback)
#
#         def get_int(name: str, fallback: int) -> int:
#             try:
#                 return int(source.get(f"PULLMANAGER_{name}", fallback))
#             except (TypeError, ValueError):
#                 return fallback
#
#         return cls(
#             cosmos_server=get("COSMOS_SERVER", "COSMOS"),
#             cosmos_database=get("COSMOS_DATABASE", "COSMOS"),
#             projects_server=str(source.get("PULLMANAGER_PROJECTS_SERVER", "") or ""),
#             projects_database=str(source.get("PULLMANAGER_PROJECTS_DATABASE", "") or ""),
#             driver=get("ODBC_DRIVER", DEFAULT_DRIVER),
#             login_timeout=get_int("LOGIN_TIMEOUT", 10),
#             query_timeout=get_int("QUERY_TIMEOUT", 0),
#             upload_chunk=get_int("UPLOAD_CHUNK", DEFAULT_UPLOAD_CHUNK),
#         )
#
#     def connection_string(self, server: str, database: str) -> str:
#         if not server:
#             raise DatabaseError("No server configured. Set it in .env.")
#         if not database:
#             raise DatabaseError("No database configured. Set it in .env.")
#         return (
#             f"Driver={{{self.driver}}};"
#             f"Server=tcp:{server};"
#             f"Database={database};"
#             "Trusted_Connection=yes;"
#         )
#
#     def cosmos_connection_string(self, database: str | None = None) -> str:
#         return self.connection_string(self.cosmos_server, database or self.cosmos_database)
#
#     def projects_connection_string(self, database: str | None = None) -> str:
#         return self.connection_string(
#             self.projects_server, database or self.projects_database
#         )
#
#
# @dataclass
# class ResultSet:
#     columns: list[str]
#     rows: list[tuple]
#
#     def as_dicts(self) -> list[dict[str, Any]]:
#         return [dict(zip(self.columns, row)) for row in self.rows]
#
#
# @dataclass
# class ExecutionResult:
#     result_sets: list[ResultSet] = field(default_factory=list)
#     messages: list[str] = field(default_factory=list)
#
#     def rows_of(self, *columns: str) -> list[dict[str, Any]]:
#         """Result sets carrying a declared telemetry shape."""
#         wanted = set(columns)
#         out: list[dict[str, Any]] = []
#         for result in self.result_sets:
#             if wanted.issubset(set(result.columns)):
#                 out.extend(result.as_dicts())
#         return out
#
#
# def split_batches(script: str) -> list[str]:
#     """Split on lines consisting solely of GO, which the driver cannot execute."""
#     batches: list[str] = []
#     current: list[str] = []
#     for line in script.splitlines():
#         if line.strip().upper() == BATCH_SEPARATOR:
#             if current:
#                 batches.append("\n".join(current))
#                 current = []
#             continue
#         current.append(line)
#     if current:
#         batches.append("\n".join(current))
#     return [b for b in batches if b.strip()]
#
#
# def collect_messages(cursor: Any) -> list[str]:
#     """Server PRINT output and nested engine errors.
#
#     This is what surfaces the inner error of a failed OPENQUERY; without it the
#     caller sees only a generic outer failure.
#     """
#     try:
#         raw = list(cursor.messages or [])
#     except Exception:
#         return []
#     return [" | ".join(str(part) for part in message) for message in raw]
#
#
# def drain(cursor: Any) -> list[ResultSet]:
#     """Consume every result set a batch produced.
#
#     A generated script interleaves DDL, inserts and telemetry SELECTs, so a
#     batch yields a mixture of row-producing and silent statements.
#     """
#     results: list[ResultSet] = []
#     while True:
#         if cursor.description is not None:
#             columns = [column[0] for column in cursor.description]
#             try:
#                 rows = list(cursor.fetchall())
#             except Exception:
#                 rows = []
#             results.append(ResultSet(columns=columns, rows=rows))
#         else:
#             try:
#                 cursor.fetchall()
#             except Exception:
#                 pass  # statement produced no rows
#         try:
#             if not cursor.nextset():
#                 break
#         except Exception:
#             break
#     return results
#
#
# def execute_script(connection: Any, script: str, *, label: str = "script") -> ExecutionResult:
#     """Run every batch of a script, failing fast with server messages."""
#     outcome = ExecutionResult()
#     batches = split_batches(script)
#     cursor = connection.cursor()
#     for index, batch in enumerate(batches, start=1):
#         try:
#             cursor.execute(batch)
#             outcome.messages.extend(collect_messages(cursor))
#             outcome.result_sets.extend(drain(cursor))
#         except Exception as exc:
#             messages = collect_messages(cursor)
#             raise DatabaseError(
#                 f"{label}: batch {index}/{len(batches)} failed: {exc}", messages
#             ) from exc
#     return outcome
#
#
# def capture_server_name(connection: Any) -> str:
#     """The Cosmos instance name, which changes on every connection."""
#     cursor = connection.cursor()
#     cursor.execute("SELECT @@SERVERNAME;")
#     row = cursor.fetchone()
#     if not row or not row[0]:
#         raise DatabaseError(
#             "Could not determine the Cosmos instance via @@SERVERNAME. Local SQL "
#             "cannot build OPENQUERY without it."
#         )
#     return str(row[0])
#
#
# def chunked(rows: Iterable[Sequence[Any]], size: int) -> Iterator[list[Sequence[Any]]]:
#     batch: list[Sequence[Any]] = []
#     for row in rows:
#         batch.append(row)
#         if len(batch) >= size:
#             yield batch
#             batch = []
#     if batch:
#         yield batch
#
#
# def bulk_insert(
#     connection: Any,
#     table: str,
#     columns: Sequence[str],
#     rows: Iterable[Sequence[Any]],
#     *,
#     chunk_size: int = DEFAULT_UPLOAD_CHUNK,
# ) -> int:
#     """Insert rows with parameter arrays rather than a literal VALUES list.
#
#     A table value constructor is capped at 1000 rows; parameter arrays are not,
#     because the statement stays single-row and only its bindings repeat. Values
#     are bound rather than interpolated, so quotes and NULLs need no escaping.
#
#     Chunked because fast_executemany allocates buffers from declared column
#     width times batch size, so memory grows with both.
#     """
#     if not columns:
#         raise DatabaseError(f"Cannot insert into {table}: no columns given.")
#     placeholders = ", ".join("?" for _ in columns)
#     column_list = ", ".join(f"[{c}]" for c in columns)
#     statement = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
#
#     cursor = connection.cursor()
#     try:
#         cursor.fast_executemany = True
#     except Exception:
#         pass  # older drivers fall back to per-row inserts
#
#     inserted = 0
#     for batch in chunked(rows, max(1, chunk_size)):
#         try:
#             cursor.executemany(statement, batch)
#         except Exception as exc:
#             raise DatabaseError(
#                 f"Bulk insert into {table} failed after {inserted} row(s): {exc}",
#                 collect_messages(cursor),
#             ) from exc
#         inserted += len(batch)
#     return inserted
#
#
# def connect(connection_string: str, *, login_timeout: int = 10, query_timeout: int = 0) -> Any:
#     """Open a connection. pyodbc is imported here so the package loads without it."""
#     try:
#         import pyodbc
#     except ImportError as exc:
#         raise DatabaseError(
#             "pyodbc is not installed. It is needed only to execute; planning, "
#             "rendering and --dry-run work without it."
#         ) from exc
#
#     connection = pyodbc.connect(connection_string, timeout=login_timeout)
#     if query_timeout:
#         connection.timeout = query_timeout
#     return connection
#
# === END FILE: pullmanager/db.py ===
# === BEGIN FILE: pullmanager/executor.py SHA256: fd658d58f45a49875790f6a119cc8b160cd736fbce5f861331f5f2ae94dd79ea SIZE: 8444 ===
# """Traversal and planning.
#
# Walks a manifest in order and produces the work a session implies. Nothing
# here touches a database; Phase 6 supplies an adapter that executes what this
# plans, so the ordering is testable on its own.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Iterator
#
# from . import local_sql, server_sql
# from .manifest import Manifest, Node, Phase, Run, Session
# from .models import BLOCKED, DONE, FAILED, RUNNING, SKIPPED
# from .normalize import normalize_bool
# from .sql import SqlBlock
# from .yaml_io import load_yaml
#
# DRY_RUN_LINKED_SERVER = "DRY-RUN-INSTANCE"
#
#
# class PlanError(ValueError):
#     """Raised when a manifest cannot be turned into work."""
#
#
# @dataclass
# class Unit:
#     """One phase or run, with the SQL it implies."""
#
#     session_id: str
#     node: Node
#     kind: str  # "setup" | "upload_cohorts" | "pk" | "run"
#     yaml_path: Path
#     server_blocks: list[SqlBlock] = field(default_factory=list)
#     local_blocks: list[SqlBlock] = field(default_factory=list)
#     notes: list[str] = field(default_factory=list)
#     reason: str = "pending"
#
#     @property
#     def unit_id(self) -> str:
#         return f"{self.session_id}/{self.kind}" if isinstance(self.node, Phase) else self.node.label
#
#     @property
#     def blocks(self) -> list[SqlBlock]:
#         return [*self.server_blocks, *self.local_blocks]
#
#
# def iter_units(manifest: Manifest, session: Session) -> Iterator[tuple[str, Node, Path]]:
#     """Phases in routine order, then runs in manifest order."""
#     for phase in session.phases:
#         yield phase.name, phase, manifest.resolve(phase)
#     for run in session.runs:
#         yield "run", run, manifest.resolve(run)
#
#
# RESUME_FULL = "full"
# RESUME_PARTIAL = "partial"
#
#
# def should_execute(
#     node: Node,
#     kind: str,
#     *,
#     current_epoch: str | None = None,
#     mode: str = RESUME_FULL,
#     retry_failed: bool = False,
# ) -> tuple[bool, str]:
#     """Decide whether one unit runs, and say why.
#
#     `done` does not mean "its output still exists". Global temps die with the
#     connection, so work completed under a previous epoch has left nothing on
#     the server even though the status still reads done. A run is different: its
#     durable result is rows in a Projects table, which survive, so it is the one
#     kind of unit a partial resume can skip.
#     """
#     status = node.status
#     if status == FAILED:
#         if retry_failed:
#             return True, "retrying a failure"
#         return False, "failed; --retry-failed reopens it"
#     if status == RUNNING:
#         return True, "interrupted while running"
#     if status == BLOCKED:
#         return True, "was blocked; upstream may succeed this time"
#     if status == SKIPPED:
#         return False, "skipped deliberately"
#     if status == DONE:
#         if not node.is_stale(current_epoch):
#             return False, "already done in this session"
#         if kind == "run" and mode == RESUME_PARTIAL:
#             return False, "done; its rows are in a Projects table and survive"
#         return True, "done under a previous connection; server state is gone"
#     return True, "pending"
#
#
# def session_cohorts(manifest: Manifest, session: Session) -> list[dict[str, Any]]:
#     """Every cohort the session will write, gathered for the setup shells."""
#     seen: dict[str, dict[str, Any]] = {}
#     for _, node, path in iter_units(manifest, session):
#         if not path.is_file():
#             continue
#         doc = load_yaml(path) or {}
#         for cohort in doc.get("cohorts") or []:
#             if not isinstance(cohort, dict) or not cohort.get("dest_table"):
#                 continue
#             if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#                 continue
#             seen.setdefault(str(cohort["dest_table"]), cohort)
#     return list(seen.values())
#
#
# def plan_unit(
#     manifest: Manifest,
#     session: Session,
#     kind: str,
#     node: Node,
#     path: Path,
#     linked_server: str,
# ) -> Unit:
#     """Render the SQL one phase or run implies."""
#     if not path.is_file():
#         raise PlanError(f"{node.label}: phase YAML not found at {path}")
#     doc = load_yaml(path) or {}
#     unit = Unit(session_id=session.session_id, node=node, kind=kind, yaml_path=path)
#
#     if kind == "setup":
#         unit.server_blocks = server_sql.render_setup(doc, unit.unit_id)
#         cohorts = session_cohorts(manifest, session)
#         unit.local_blocks = local_sql.render_setup(doc, cohorts, unit.unit_id)
#         unit.notes.append(
#             f"setup creates {len(cohorts)} destination table(s); runs append to them"
#         )
#         return unit
#
#     if kind == "upload_cohorts":
#         uploads = doc.get("upload_cohorts") or []
#         enabled = [
#             u for u in uploads
#             if isinstance(u, dict) and normalize_bool(u.get("push_this_cycle"), default=True)
#         ]
#         unit.notes.append(
#             f"{len(enabled)} upload cohort(s); uploaded through the client, since there is "
#             "no linked server from Cosmos back to Projects"
#             if enabled else "no upload cohorts"
#         )
#         return unit
#
#     server_blocks, notes = server_sql.render_phase(doc, unit.unit_id)
#     unit.server_blocks = server_blocks
#     unit.notes.extend(notes)
#     unit.local_blocks = local_sql.render_phase(doc, unit.unit_id, linked_server)
#     return unit
#
#
# def plan_session(
#     manifest: Manifest,
#     session: Session,
#     *,
#     linked_server: str = DRY_RUN_LINKED_SERVER,
#     retry_failed: bool = False,
#     include_settled: bool = False,
#     mode: str = RESUME_FULL,
#     current_epoch: str | None = None,
# ) -> list[Unit]:
#     """Every unit this session would execute, in order.
#
#     `current_epoch` defaults to None, which models opening a fresh connection:
#     anything completed under a previous epoch is stale.
#     """
#     units: list[Unit] = []
#     for kind, node, path in iter_units(manifest, session):
#         execute, reason = should_execute(
#             node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
#         )
#         if not execute and not include_settled:
#             continue
#         unit = plan_unit(manifest, session, kind, node, path, linked_server)
#         unit.reason = reason if execute else f"included anyway ({reason})"
#         units.append(unit)
#     return units
#
#
# def plan(
#     manifest: Manifest,
#     *,
#     linked_server: str = DRY_RUN_LINKED_SERVER,
#     retry_failed: bool = False,
#     include_settled: bool = False,
#     mode: str = RESUME_FULL,
# ) -> list[Unit]:
#     units: list[Unit] = []
#     for session in manifest.sessions:
#         units.extend(
#             plan_session(
#                 manifest,
#                 session,
#                 linked_server=linked_server,
#                 retry_failed=retry_failed,
#                 include_settled=include_settled,
#                 mode=mode,
#             )
#         )
#     return units
#
#
# def excluded_units(
#     manifest: Manifest,
#     *,
#     mode: str = RESUME_FULL,
#     retry_failed: bool = False,
#     current_epoch: str | None = None,
# ) -> list[tuple[str, str, str]]:
#     """Units the plan leaves out, as (id, status, reason).
#
#     Reported rather than silently dropped: a resume that rebuilds the server
#     side but omits the run that failed would finish with nothing transferred,
#     which should not look like success.
#     """
#     left_out: list[tuple[str, str, str]] = []
#     for session in manifest.sessions:
#         for kind, node, _ in iter_units(manifest, session):
#             execute, reason = should_execute(
#                 node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
#             )
#             if not execute:
#                 label = f"{session.session_id}/{kind}" if isinstance(node, Phase) else node.label
#                 left_out.append((label, node.status, reason))
#     return left_out
#
#
# def write_sql(units: list[Unit], out_dir: Path) -> list[Path]:
#     """Write every block to an inspectable file named after its manifest id."""
#     out_dir = Path(out_dir)
#     written: list[Path] = []
#     for unit in units:
#         for block in unit.blocks:
#             safe = block.block_id.replace("/", "__")
#             path = out_dir / unit.session_id / f"{safe}.{block.side}.sql"
#             path.parent.mkdir(parents=True, exist_ok=True)
#             path.write_text(block.sql.rstrip("\n") + "\n", encoding="utf-8")
#             written.append(path)
#     return written
#
# === END FILE: pullmanager/executor.py ===
# === BEGIN FILE: pullmanager/local_sql.py SHA256: 2effa5f5fc36cbafa0a72c872f3f3e362f6a5d8d9608c356fa6663f1697f3130 SIZE: 7093 ===
# """Projects-side SQL: destination tables and the transfer from Cosmos.
#
# Write mode is decided: the destination is dropped and created once per session
# in the setup phase, and every run appends. The old generator dropped inside
# each transfer block, which with batching leaves only the last batch.
# """
#
# from __future__ import annotations
#
# from typing import Any
#
# from .naming import destination, global_temp, local_staging
# from .normalize import normalize_bool
# from .sql import (
#     SqlBlock,
#     column_list,
#     column_names,
#     ddl_body,
#     quote_literal,
#     quote_name,
# )
#
# # Types whose stored length is worth measuring so templates can be tuned.
# _MEASURABLE = ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR")
#
#
# class LocalRenderError(ValueError):
#     """Raised when local SQL cannot be rendered."""
#
#
# def _columns(cohort: dict[str, Any]) -> list[dict[str, Any]]:
#     return [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
#
#
# def _remote_query(dest: str, columns: list[dict[str, Any]]) -> str:
#     """The inner query sent to the linked server, quoted for embedding."""
#     cols = ", ".join(quote_name(n) for n in column_names(columns))
#     inner = f"SELECT {cols} FROM {global_temp(dest)}"
#     return inner.replace("'", "''")
#
#
# def render_table_shell(cohort: dict[str, Any], project_db: str) -> str:
#     """Drop and create one destination table. Runs once per session."""
#     dest = cohort.get("dest_table")
#     if not dest:
#         raise LocalRenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
#     columns = _columns(cohort)
#     if not columns:
#         raise LocalRenderError(f"Cohort {dest!r} declares no columns.")
#     table = destination(project_db, dest)
#     return (
#         f"-- session table shell for {dest}\n"
#         f"DROP TABLE IF EXISTS {table};\n\n"
#         f"CREATE TABLE {table}\n(\n{ddl_body(columns)}\n);"
#     )
#
#
# def render_transfer(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
#     """Pull one cohort from its global temp into the destination table.
#
#     The slow OPENQUERY lands in a staging table outside the transaction, so no
#     lock is held while data crosses the linked server. Only the final insert is
#     transactional, which is what makes a retry after a partial failure safe.
#     """
#     if not linked_server:
#         raise LocalRenderError(
#             "No linked server supplied. The Cosmos instance name changes on every "
#             "connection and must come from the current session."
#         )
#     dest = str(cohort["dest_table"])
#     columns = _columns(cohort)
#     table = destination(project_db, dest)
#     staging = local_staging(dest)
#     cols = column_list(columns)
#
#     return (
#         f"-- transfer {global_temp(dest)} -> {table}\n"
#         f"DROP TABLE IF EXISTS {staging};\n\n"
#         f"SELECT {cols}\n"
#         f"INTO {staging}\n"
#         f"FROM OPENQUERY(\n"
#         f"    [{linked_server}],\n"
#         f"    '{_remote_query(dest, columns)}'\n"
#         f");\n\n"
#         f"BEGIN TRANSACTION;\n"
#         f"INSERT INTO {table} ({cols})\n"
#         f"SELECT {cols} FROM {staging};\n"
#         f"COMMIT TRANSACTION;"
#     )
#
#
# def render_row_counts(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
#     """Both sides of the transfer, so a mismatch is visible."""
#     dest = str(cohort["dest_table"])
#     table = destination(project_db, dest)
#     remote = f"SELECT 1 AS dummy FROM {global_temp(dest)}".replace("'", "''")
#     return (
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         "    'cosmos' AS [Side],\n"
#         "    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM OPENQUERY([{linked_server}], '{remote}');\n\n"
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         "    'projects' AS [Side],\n"
#         "    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM {table};"
#     )
#
#
# def render_length_probe(cohort: dict[str, Any]) -> str | None:
#     """Measure the widest value actually stored in each string column.
#
#     Reported rather than applied: the destination table is created in setup,
#     before any data exists, and sizing it from one batch would truncate a later
#     batch carrying a longer value.
#     """
#     dest = str(cohort["dest_table"])
#     staging = local_staging(dest)
#     measurable = [
#         c for c in _columns(cohort)
#         if str(c.get("type", "")).split("(")[0].strip().upper() in _MEASURABLE
#     ]
#     if not measurable:
#         return None
#     selects = [
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         f"    {quote_literal(str(c['name']))} AS [Column],\n"
#         f"    {quote_literal(str(c.get('type')))} AS [DeclaredType],\n"
#         f"    MAX(LEN({quote_name(str(c['name']))})) AS [MaxLength]\n"
#         f"FROM {staging}"
#         for c in measurable
#     ]
#     return "\n UNION ALL\n".join(selects) + ";"
#
#
# def render_setup(doc: dict[str, Any], cohorts: list[dict[str, Any]], block_prefix: str) -> list[SqlBlock]:
#     """One shell block per destination table for the whole session."""
#     project_db = doc.get("project_db")
#     if not project_db:
#         raise LocalRenderError("Phase document has no `project_db`.")
#     blocks = []
#     for cohort in cohorts:
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             continue
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/shell/{cohort['dest_table']}",
#                 side="local",
#                 sql=render_table_shell(cohort, str(project_db)),
#                 dest_table=str(cohort["dest_table"]),
#                 meta={"destination": destination(str(project_db), cohort["dest_table"])},
#             )
#         )
#     return blocks
#
#
# def render_phase(doc: dict[str, Any], block_prefix: str, linked_server: str) -> list[SqlBlock]:
#     """Transfer, counts and measurement for every cohort in a phase."""
#     project_db = doc.get("project_db")
#     if not project_db:
#         raise LocalRenderError("Phase document has no `project_db`.")
#     blocks: list[SqlBlock] = []
#     for cohort in doc.get("cohorts") or []:
#         if not isinstance(cohort, dict) or not cohort.get("dest_table"):
#             continue
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             continue
#         dest = str(cohort["dest_table"])
#         parts = [
#             render_transfer(cohort, str(project_db), linked_server),
#             render_row_counts(cohort, str(project_db), linked_server),
#         ]
#         probe = render_length_probe(cohort)
#         if probe:
#             parts.append(probe)
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/{dest}",
#                 side="local",
#                 sql="\n\n".join(parts),
#                 dest_table=dest,
#                 meta={
#                     "destination": destination(str(project_db), dest),
#                     "staging": local_staging(dest),
#                     "global_temp": global_temp(dest),
#                     "linked_server": linked_server,
#                 },
#             )
#         )
#     return blocks
#
# === END FILE: pullmanager/local_sql.py ===
# === BEGIN FILE: pullmanager/manifest.py SHA256: 1ccaab95c26ba0f76151c64e58e2b5988e60558ae06a9ec1f4e082df23ca4508 SIZE: 11210 ===
# """Load, mutate, and write back `pullmanifest.yaml`.
#
# The manifest is YAML Manager's plan on the way in and Pullmanager's status
# document on the way out. Nodes wrap the loaded mappings in place so keys this
# version does not understand survive a round trip untouched.
# """
#
# from __future__ import annotations
#
# import os
# from pathlib import Path
# from typing import Any, Iterator
#
# from . import MANIFEST_VERSION
# from .models import (
#     BLOCKED,
#     DONE,
#     FAILED,
#     PENDING,
#     RUNNING,
#     SETTLED_STATUSES,
#     SKIPPED,
#     duration_block,
#     new_epoch,
#     now_iso,
#     validate_status,
# )
# from .yaml_io import dump_yaml, load_yaml
#
# PHASE_ORDER = ("setup", "upload_cohorts", "pk")
#
#
# class ManifestError(ValueError):
#     """Raised when a manifest does not match the expected contract."""
#
#
# class Node:
#     """A manifest entry carrying status and timing fields."""
#
#     def __init__(self, data: dict[str, Any], label: str, session: "Session | None" = None):
#         self._data = data
#         self.label = label
#         self._session = session
#
#     @property
#     def data(self) -> dict[str, Any]:
#         return self._data
#
#     @property
#     def status(self) -> str:
#         return self._data.get("status", PENDING)
#
#     @status.setter
#     def status(self, value: str) -> None:
#         self._data["status"] = validate_status(value)
#
#     @property
#     def yaml(self) -> str | None:
#         return self._data.get("yaml")
#
#     @property
#     def rows(self) -> int | None:
#         return self._data.get("rows")
#
#     @property
#     def outputs(self) -> dict[str, Any]:
#         return self._data.setdefault("outputs", {})
#
#     @property
#     def error(self) -> dict[str, Any] | None:
#         return self._data.get("error")
#
#     @property
#     def note(self) -> str | None:
#         return self._data.get("note")
#
#     @property
#     def epoch(self) -> str | None:
#         """The server connection this node last completed under."""
#         return self._data.get("epoch")
#
#     def is_stale(self, current_epoch: str | None) -> bool:
#         """True when this finished under a connection that no longer exists.
#
#         Server-side output (global temps, uploaded tables) from a stale node is
#         gone even though the status still reads `done`. Local Projects tables
#         are permanent and survive regardless.
#         """
#         if self.status != DONE:
#             return False
#         return self.epoch is not None and self.epoch != current_epoch
#
#     def start(self) -> None:
#         self.status = RUNNING
#         self._data["started_at"] = now_iso()
#         self._data["finished_at"] = None
#         self._data["error"] = None
#         self._data.pop("duration", None)
#
#     def finish(self, rows: int | None = None, outputs: dict[str, Any] | None = None) -> None:
#         self.status = DONE
#         self._stamp_finish()
#         if rows is not None:
#             self._data["rows"] = rows
#         if outputs:
#             self.outputs.update(outputs)
#         self._data["error"] = None
#
#     def fail(self, message: str, detail: str | None = None) -> None:
#         self.status = FAILED
#         self._stamp_finish()
#         self._data["error"] = {"message": message, "detail": detail}
#
#     def skip(self, reason: str | None = None) -> None:
#         self._settle_without_running(SKIPPED, reason)
#
#     def block(self, reason: str | None = None) -> None:
#         self._settle_without_running(BLOCKED, reason)
#
#     def _settle_without_running(self, status: str, reason: str | None) -> None:
#         # Reasons go in `note`, not `error`: a skipped phase is not a failure,
#         # and an `error` block on it would read like one.
#         self.status = status
#         if reason:
#             self._data["note"] = reason
#
#     def _stamp_finish(self) -> None:
#         self._data["finished_at"] = now_iso()
#         if self._session is not None and self._session.epoch is not None:
#             self._data["epoch"] = self._session.epoch
#         duration = duration_block(self._data.get("started_at"), self._data["finished_at"])
#         if duration is not None:
#             self._data["duration"] = duration
#
#     def __repr__(self) -> str:
#         return f"<{type(self).__name__} {self.label} {self.status}>"
#
#
# class Phase(Node):
#     def __init__(self, name: str, data: dict[str, Any], session: "Session"):
#         super().__init__(data, f"{session.label}/{name}", session)
#         self.name = name
#
#     @property
#     def pk_source(self) -> dict[str, Any] | None:
#         return self._data.get("pk_source")
#
#
# class Run(Node):
#     def __init__(self, data: dict[str, Any], session: "Session"):
#         super().__init__(data, str(data.get("run_id")), session)
#
#     @property
#     def run_id(self) -> str:
#         return self._data["run_id"]
#
#     @property
#     def batch(self) -> dict[str, Any] | None:
#         return self._data.get("batch")
#
#
# class Session(Node):
#     def __init__(self, data: dict[str, Any]):
#         super().__init__(data, str(data.get("session_id")))
#         raw_phases = data.get("phases") or {}
#         unknown = set(raw_phases) - set(PHASE_ORDER)
#         if unknown:
#             raise ManifestError(
#                 f"Session {self.label!r} has unknown phase(s): {', '.join(sorted(unknown))}. "
#                 f"Expected only: {', '.join(PHASE_ORDER)}"
#             )
#         self.phases = [
#             Phase(name, raw_phases[name], self)
#             for name in PHASE_ORDER
#             if name in raw_phases
#         ]
#         self.runs = [Run(run, self) for run in data.get("runs") or []]
#
#     @property
#     def session_id(self) -> str:
#         return self._data["session_id"]
#
#     @property
#     def pk_table(self) -> str | None:
#         return self._data.get("pk_table")
#
#     @property
#     def runtime(self) -> dict[str, Any]:
#         """Facts discovered when the session's connection opened."""
#         return self._data.setdefault("runtime", {})
#
#     @property
#     def epoch(self) -> str | None:
#         return self.runtime.get("epoch")
#
#     def begin_epoch(self, linked_server: str | None = None) -> str:
#         """Open a new server connection scope for this session.
#
#         `linked_server` is always overwritten, never left in place: the Cosmos
#         instance name changes on every connection, so carrying the previous
#         one forward would point later SQL at a server that is no longer ours.
#         """
#         epoch = new_epoch()
#         self.runtime["epoch"] = epoch
#         self.runtime["opened_at"] = now_iso()
#         self.runtime["linked_server"] = linked_server
#         return epoch
#
#     def stale_children(self) -> list[Node]:
#         """Nodes marked done whose server-side output died with a past epoch."""
#         return [child for child in self.children if child.is_stale(self.epoch)]
#
#     @property
#     def children(self) -> list[Node]:
#         return [*self.phases, *self.runs]
#
#     def recompute_status(self) -> str:
#         statuses = [child.status for child in self.children]
#         if not statuses:
#             return self.status
#         if FAILED in statuses:
#             rolled = FAILED
#         elif RUNNING in statuses:
#             rolled = RUNNING
#         elif BLOCKED in statuses:
#             rolled = BLOCKED
#         elif all(status == SKIPPED for status in statuses):
#             rolled = SKIPPED
#         elif all(status in SETTLED_STATUSES for status in statuses):
#             rolled = DONE
#         elif any(status in SETTLED_STATUSES for status in statuses):
#             rolled = RUNNING
#         else:
#             rolled = PENDING
#         self.status = rolled
#         return rolled
#
#
# class Manifest:
#     def __init__(self, data: dict[str, Any], path: Path | None = None):
#         if not isinstance(data, dict):
#             raise ManifestError("Manifest root must be a mapping.")
#         version = data.get("manifest_version")
#         if version != MANIFEST_VERSION:
#             raise ManifestError(
#                 f"Unsupported manifest_version {version!r}. This Pullmanager reads version {MANIFEST_VERSION}."
#             )
#         raw_sessions = data.get("sessions")
#         if not isinstance(raw_sessions, list):
#             raise ManifestError("Manifest `sessions` must be a list.")
#         self._data = data
#         self.path = path
#         self.sessions = [Session(session) for session in raw_sessions]
#         self._validate()
#
#     def _validate(self) -> None:
#         seen_sessions: set[str] = set()
#         seen_runs: set[str] = set()
#         for session in self.sessions:
#             if not session.data.get("session_id"):
#                 raise ManifestError("Every session requires a `session_id`.")
#             if session.session_id in seen_sessions:
#                 raise ManifestError(f"Duplicate session_id {session.session_id!r}.")
#             seen_sessions.add(session.session_id)
#             validate_status(session.status)
#             for phase in session.phases:
#                 if not phase.yaml:
#                     raise ManifestError(f"Phase {phase.label!r} is missing its `yaml` path.")
#                 validate_status(phase.status)
#             for run in session.runs:
#                 if not run.data.get("run_id"):
#                     raise ManifestError(f"A run in session {session.session_id!r} is missing `run_id`.")
#                 if run.run_id in seen_runs:
#                     raise ManifestError(f"Duplicate run_id {run.run_id!r}.")
#                 seen_runs.add(run.run_id)
#                 if not run.yaml:
#                     raise ManifestError(f"Run {run.run_id!r} is missing its `yaml` path.")
#                 validate_status(run.status)
#
#     @property
#     def data(self) -> dict[str, Any]:
#         return self._data
#
#     @property
#     def project(self) -> dict[str, Any]:
#         return self._data.get("project") or {}
#
#     @property
#     def source(self) -> dict[str, Any]:
#         return self._data.get("source") or {}
#
#     @property
#     def root(self) -> Path:
#         """Directory the manifest's relative `yaml` paths resolve against."""
#         if self.path is None:
#             raise ManifestError("Manifest has no path; cannot resolve relative YAML paths.")
#         return self.path.parent
#
#     def resolve(self, node: Node) -> Path:
#         if not node.yaml:
#             raise ManifestError(f"Node {node.label!r} has no `yaml` path to resolve.")
#         return self.root / node.yaml
#
#     def iter_nodes(self) -> Iterator[tuple[Session, Node]]:
#         for session in self.sessions:
#             for child in session.children:
#                 yield session, child
#
#     @classmethod
#     def load(cls, path: str | Path) -> "Manifest":
#         path = Path(path)
#         if not path.is_file():
#             raise ManifestError(f"Manifest not found: {path}")
#         return cls(load_yaml(path), path=path)
#
#     def save(self, path: str | Path | None = None) -> Path:
#         """Write the manifest atomically so a crash cannot truncate it."""
#         target = Path(path) if path else self.path
#         if target is None:
#             raise ManifestError("No path to save manifest to.")
#         for session in self.sessions:
#             session.recompute_status()
#         tmp = target.with_name(target.name + ".tmp")
#         dump_yaml(self._data, tmp)
#         os.replace(tmp, target)
#         self.path = target
#         return target
#
# === END FILE: pullmanager/manifest.py ===
# === BEGIN FILE: pullmanager/models.py SHA256: 2eb665cf240632931fdb1f07b9b4d6e04757c51fb188ea3d17c184c60cdf87be SIZE: 2154 ===
# """Status vocabulary and timing helpers shared by manifest nodes."""
#
# from __future__ import annotations
#
# import uuid
# from datetime import datetime
#
# PENDING = "pending"
# RUNNING = "running"
# DONE = "done"
# FAILED = "failed"
# SKIPPED = "skipped"
# BLOCKED = "blocked"
#
# ALL_STATUSES = (PENDING, RUNNING, DONE, FAILED, SKIPPED, BLOCKED)
#
# # Statuses that mean "do not execute again on a plain resume".
# SETTLED_STATUSES = (DONE, SKIPPED)
#
#
# class StatusError(ValueError):
#     """Raised when an unknown status string is supplied."""
#
#
# def validate_status(status: str) -> str:
#     if status not in ALL_STATUSES:
#         raise StatusError(
#             f"Unknown status {status!r}. Expected one of: {', '.join(ALL_STATUSES)}"
#         )
#     return status
#
#
# def new_epoch() -> str:
#     """Identify one live server connection.
#
#     Global temp tables die with the connection, so work recorded under a
#     previous epoch is known to be gone from the server even though the manifest
#     still says `done`.
#     """
#     return f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
#
#
# def now_iso() -> str:
#     """Local timestamp with UTC offset, e.g. 2026-09-22T14:03:00-05:00."""
#     return datetime.now().astimezone().isoformat(timespec="seconds")
#
#
# def parse_iso(value: str) -> datetime:
#     return datetime.fromisoformat(value)
#
#
# def format_duration(seconds: float) -> str:
#     """Render a duration as "1h 2m 3s" / "5m 32s" / "45s"."""
#     total = int(round(seconds))
#     sign = "-" if total < 0 else ""
#     total = abs(total)
#     hours, remainder = divmod(total, 3600)
#     minutes, secs = divmod(remainder, 60)
#     if hours:
#         return f"{sign}{hours}h {minutes}m {secs}s"
#     if minutes:
#         return f"{sign}{minutes}m {secs}s"
#     return f"{sign}{secs}s"
#
#
# def duration_block(started_at: str | None, finished_at: str | None) -> dict | None:
#     """Build the manifest `duration` mapping from two ISO timestamps."""
#     if not started_at or not finished_at:
#         return None
#     seconds = (parse_iso(finished_at) - parse_iso(started_at)).total_seconds()
#     return {"seconds": int(round(seconds)), "display": format_duration(seconds)}
#
# === END FILE: pullmanager/models.py ===
# === BEGIN FILE: pullmanager/naming.py SHA256: 45595539e6045bcc4b431379e8adebb7556793d3246cf7921a79eb16e5afc7c4 SIZE: 3764 ===
# """Table naming rules.
#
# These come from the old generator and are invariants, not preferences: the
# server, the staging table and the destination must agree or a transfer fails.
# Every function here is idempotent, so applying a rule to an already-correct
# name is a no-op rather than a double prefix.
# """
#
# from __future__ import annotations
#
# import re
#
# GLOBAL_TEMP_PREFIX = "##JVM_"
# LOCAL_STAGING_PREFIX = "#Local_"
# DEFAULT_SCHEMA = "dbo"
#
# # A bare identifier, optionally bracketed: PatientDim, [PatientDim], ##JVM_X
# _IDENTIFIER = r"(?:\[[^\]]+\]|[A-Za-z_#@][\w@$#]*)"
# # A possibly-qualified reference, matched whole so `dbo.PatientDim` is never
# # mistaken for the bare identifier `dbo` and qualified a second time.
# _REFERENCE = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
# _FROM_OR_JOIN = re.compile(
#     rf"(?P<lead>\b(?:FROM|JOIN)\s+)(?P<table>{_REFERENCE})",
#     re.IGNORECASE,
# )
# _LEADING_TABLE = re.compile(rf"^(?P<ws>\s*)(?P<table>{_REFERENCE})")
#
#
# class NamingError(ValueError):
#     """Raised when a name cannot be derived."""
#
#
# def _require(dest_table: str | None) -> str:
#     if dest_table is None or not str(dest_table).strip():
#         raise NamingError("dest_table is required to derive a table name.")
#     return str(dest_table).strip()
#
#
# def base_name(dest_table: str | None) -> str:
#     """Strip any temp marker and generator prefix down to the bare name."""
#     name = _require(dest_table).lstrip("#")
#     for prefix in ("JVM_", "Local_"):
#         if name.upper().startswith(prefix.upper()):
#             name = name[len(prefix):]
#             break
#     return name
#
#
# def global_temp(dest_table: str | None) -> str:
#     """Cosmos session-scoped output: PKTable -> ##JVM_PKTable.
#
#     A dest_table that already starts with JVM_ is not prefixed twice, which is
#     the `##JVM_JVM_Foo` bug the old generator guarded against.
#     """
#     return GLOBAL_TEMP_PREFIX + base_name(dest_table)
#
#
# def local_staging(dest_table: str | None) -> str:
#     """Projects session-scoped staging: PKTable -> #Local_PKTable."""
#     return LOCAL_STAGING_PREFIX + base_name(dest_table)
#
#
# def destination(project_db: str | None, dest_table: str | None) -> str:
#     """Durable Projects table, always fully qualified.
#
#     Generated SQL gets run from tools with ambiguous database context, so the
#     destination never relies on USE.
#     """
#     if not project_db or not str(project_db).strip():
#         raise NamingError("project_db is required to qualify a destination table.")
#     return f"{str(project_db).strip()}.{DEFAULT_SCHEMA}.{base_name(dest_table)}"
#
#
# def is_temp_table(token: str) -> bool:
#     return token.lstrip("[").startswith("#")
#
#
# def is_schema_qualified(token: str) -> bool:
#     """True when a reference already names a schema.
#
#     Dots inside brackets do not count, so `[My.Table]` is still unqualified.
#     """
#     return "." in re.sub(r"\[[^\]]*\]", "", token)
#
#
# def qualify(token: str) -> str:
#     """Add the default schema to a bare table reference.
#
#     Left alone: temp tables, which live in tempdb and must stay unqualified,
#     and anything already carrying a schema, which is what prevents `dbo.dbo.`.
#     """
#     if is_temp_table(token) or is_schema_qualified(token):
#         return token
#     return f"{DEFAULT_SCHEMA}.{token}"
#
#
# def qualify_table_ref(ref: str) -> str:
#     """Qualify the leading table of a `from` entry: `PatientDim AS p`."""
#     match = _LEADING_TABLE.match(ref)
#     if not match:
#         return ref
#     table = match.group("table")
#     return ref[: match.start("table")] + qualify(table) + ref[match.end("table"):]
#
#
# def qualify_join_clause(clause: str) -> str:
#     """Qualify every table named after FROM or JOIN in a clause."""
#     return _FROM_OR_JOIN.sub(
#         lambda m: m.group("lead") + qualify(m.group("table")), clause
#     )
#
# === END FILE: pullmanager/naming.py ===
# === BEGIN FILE: pullmanager/normalize.py SHA256: dee580d39bedc3b6fd2bbcb5ed79c4a86e2914bc4b49558d9fc4c582fd89891e SIZE: 7036 ===
# """Compatibility rules for hand-authored cohort YAML.
#
# Each function returns its result alongside any notes worth surfacing, because
# the failure these rules guard against is silence: the old generator accepted
# only `dedup_keys` and, given `dedup_key`, emitted no deduplication at all and
# said nothing, changing row counts invisibly.
# """
#
# from __future__ import annotations
#
# from typing import Any
#
# from .naming import global_temp
#
# TRUTHY = {"true", "yes", "y", "1", "on", "t"}
# FALSY = {"false", "no", "n", "0", "off", "f", ""}
#
# DEAD_TEST_OPTIONS = {
#     "stop_at_for_non_pk_tables": (
#         "no longer used; row limits now apply only to the root PK cohort"
#     ),
#     "print_md": "reporting is the manifest's job; no markdown run report is written",
#     "printout_md": "reporting is the manifest's job; no markdown run report is written",
# }
#
#
# class NormalizationError(ValueError):
#     """Raised when a value cannot be interpreted."""
#
#
# def normalize_bool(value: Any, *, default: bool = False) -> bool:
#     """Accept the YAML and human spellings of a boolean."""
#     if value is None:
#         return default
#     if isinstance(value, bool):
#         return value
#     if isinstance(value, (int, float)):
#         return bool(value)
#     text = str(value).strip().lower()
#     if text in TRUTHY:
#         return True
#     if text in FALSY:
#         return False
#     raise NormalizationError(f"Cannot interpret {value!r} as a boolean.")
#
#
# def normalize_dedup_keys(cohort: dict[str, Any]) -> tuple[list[list[str]], list[str]]:
#     """Return dedup keys as a list of key sets, plus any notes.
#
#     Canonical form is `dedup_keys`, a list of lists. The legacy singular
#     `dedup_key` is accepted and normalized rather than rejected: refusing the
#     file only relocates the friction, while accepting it loudly removes the
#     silent-no-dedup outcome entirely.
#     """
#     notes: list[str] = []
#     raw = cohort.get("dedup_keys")
#     legacy = cohort.get("dedup_key")
#
#     if raw is not None and legacy is not None:
#         raise NormalizationError(
#             f"Cohort {cohort.get('dest_table') or cohort.get('name')!r} sets both "
#             "`dedup_keys` and legacy `dedup_key`. Keep only `dedup_keys`."
#         )
#
#     if raw is None and legacy is None:
#         return [], notes
#
#     if raw is None:
#         raw = legacy
#         notes.append(
#             f"`dedup_key` is legacy; normalized to `dedup_keys` for "
#             f"{cohort.get('dest_table') or cohort.get('name')!r}. Update the template."
#         )
#
#     if isinstance(raw, str):
#         key_sets = [[raw]]
#     elif isinstance(raw, list):
#         if not raw:
#             raise NormalizationError("`dedup_keys` was supplied but is empty.")
#         # A flat list is one key set; a list of lists is several.
#         if all(isinstance(item, list) for item in raw):
#             key_sets = [[str(col) for col in item] for item in raw]
#         elif any(isinstance(item, list) for item in raw):
#             raise NormalizationError(
#                 "`dedup_keys` mixes bare columns and key sets. Use either "
#                 "[[a, b]] for one key set or [[a], [b]] for two."
#             )
#         else:
#             key_sets = [[str(col) for col in raw]]
#     else:
#         raise NormalizationError(f"Cannot interpret dedup keys from {raw!r}.")
#
#     for key_set in key_sets:
#         if not key_set:
#             raise NormalizationError("`dedup_keys` contains an empty key set.")
#     return key_sets, notes
#
#
# def validate_dedup_columns(
#     key_sets: list[list[str]], cohort: dict[str, Any]
# ) -> list[str]:
#     """Check dedup keys name columns the cohort actually produces."""
#     produced = {
#         str(col.get("name"))
#         for col in cohort.get("columns") or []
#         if isinstance(col, dict) and col.get("name")
#     }
#     problems = []
#     for key_set in key_sets:
#         unknown = [col for col in key_set if col not in produced]
#         if unknown:
#             problems.append(
#                 f"dedup key(s) {', '.join(unknown)} are not columns of "
#                 f"{cohort.get('dest_table') or cohort.get('name')!r}"
#             )
#     return problems
#
#
# def dead_options(test_options: dict[str, Any] | None) -> list[str]:
#     """Notes for retired `test_options` keys that are still present."""
#     if not test_options:
#         return []
#     return [
#         f"`test_options.{key}` is ignored: {why}"
#         for key, why in DEAD_TEST_OPTIONS.items()
#         if key in test_options
#     ]
#
#
# def is_pk(cohort: dict[str, Any]) -> bool:
#     return str(cohort.get("type", "")).strip().lower() == "pk"
#
#
# def pk_cohorts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
#     return [c for c in cohorts if isinstance(c, dict) and is_pk(c)]
#
#
# def joined_generated_tables(cohort: dict[str, Any]) -> set[str]:
#     """Global temp names this cohort joins, found in its filter text."""
#     filter_block = cohort.get("filter") or {}
#     text_parts: list[str] = []
#     for key in ("from", "join", "where"):
#         value = filter_block.get(key)
#         if isinstance(value, str):
#             text_parts.append(value)
#         elif isinstance(value, list):
#             text_parts.extend(str(item) for item in value)
#     haystack = " ".join(text_parts).upper()
#     return {token for token in _global_temp_tokens(haystack)}
#
#
# def _global_temp_tokens(haystack: str) -> set[str]:
#     tokens: set[str] = set()
#     marker = "##JVM_"
#     start = haystack.find(marker)
#     while start != -1:
#         end = start + len(marker)
#         while end < len(haystack) and (haystack[end].isalnum() or haystack[end] == "_"):
#             end += 1
#         tokens.add(haystack[start:end])
#         start = haystack.find(marker, end)
#     return tokens
#
#
# def root_pk_cohorts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
#     """PK cohorts that do not depend on another PK cohort's global temp.
#
#     A chained PK (patients -> diagnosis events for those patients) has exactly
#     one root. Row limits apply there, because limiting a downstream PK as well
#     compounds the restriction into an unrepresentative sample.
#     """
#     pks = pk_cohorts(cohorts)
#     sibling_temps = {global_temp(c.get("dest_table")).upper() for c in pks if c.get("dest_table")}
#     roots = []
#     for cohort in pks:
#         own = global_temp(cohort.get("dest_table")).upper() if cohort.get("dest_table") else None
#         depends_on = joined_generated_tables(cohort) & sibling_temps
#         depends_on.discard(own)
#         if not depends_on:
#             roots.append(cohort)
#     return roots
#
#
# def root_pk_cohort(cohorts: list[dict[str, Any]]) -> dict[str, Any] | None:
#     """The single root PK cohort, or None when there is no PK at all."""
#     roots = root_pk_cohorts(cohorts)
#     if not roots:
#         return None
#     if len(roots) > 1:
#         names = ", ".join(str(c.get("dest_table") or c.get("name")) for c in roots)
#         raise NormalizationError(
#             f"Several PK cohorts depend on nothing ({names}), so the root PK is "
#             "ambiguous. Chain them, or mark one as canonical."
#         )
#     return roots[0]
#
# === END FILE: pullmanager/normalize.py ===
# === BEGIN FILE: pullmanager/server_sql.py SHA256: 06a74220bcca9ee66fb6ee439272e09910bf3b3913826dec4c69707bebe4887d SIZE: 7865 ===
# """Cosmos-side SQL.
#
# Renders one block per cohort, addressed by manifest id. Nothing downstream
# searches SQL text to decide what to run.
# """
#
# from __future__ import annotations
#
# import re
#
# from typing import Any
#
# from .naming import global_temp
# from .normalize import (
#     normalize_bool,
#     normalize_dedup_keys,
#     root_pk_cohort,
#     validate_dedup_columns,
# )
# from .sql import (
#     SqlBlock,
#     column_list,
#     column_names,
#     ddl_body,
#     non_null_predicates,
#     quote_literal,
#     quote_name,
#     render_source_clause,
#     render_where,
#     where_entries,
# )
#
# SERVER_NAME_QUERY = "SELECT @@SERVERNAME AS CosmosServerName;"
#
# _PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
#
#
# class RenderError(ValueError):
#     """Raised when a cohort cannot be rendered."""
#
#
# def top_clause(cohort: dict[str, Any], doc: dict[str, Any], root: dict[str, Any] | None) -> str:
#     """`TOP (n)`, applied to the root PK cohort only.
#
#     Limiting a downstream PK as well compounds the restriction: 500 patients
#     and then 500 of their events is not 500 patients' worth of events.
#     """
#     options = doc.get("test_options") or {}
#     if not normalize_bool(options.get("smallset") or options.get("smallest")):
#         return ""
#     if root is None or cohort is not root:
#         return ""
#     limit = options.get("stop_at_for_pk_table")
#     try:
#         limit = int(limit)
#     except (TypeError, ValueError):
#         return ""
#     return f"TOP ({limit}) " if limit > 0 else ""
#
#
# def cohort_predicates(cohort: dict[str, Any]) -> list[str]:
#     columns = cohort.get("columns") or []
#     return where_entries(cohort.get("filter") or {}) + non_null_predicates(columns)
#
#
# def render_select(cohort: dict[str, Any], top: str, inner_indent: str = "    ") -> str:
#     columns = cohort.get("columns") or []
#     projections = [
#         f"{inner_indent}{column['source']} AS {quote_name(str(column['name']))}"
#         for column in columns
#         if isinstance(column, dict) and column.get("name") and column.get("source")
#     ]
#     parts = [f"SELECT {top}".rstrip(), ",\n".join(projections)]
#     source = render_source_clause(cohort.get("filter") or {})
#     if source:
#         parts.append(source)
#     predicates = cohort_predicates(cohort)
#     if predicates:
#         parts.append("WHERE")
#         parts.append(render_where(predicates, inner_indent))
#     return "\n".join(parts)
#
#
# def render_dedup_select(
#     cohort: dict[str, Any], key_sets: list[list[str]], top: str
# ) -> tuple[str, list[str]]:
#     """Wrap the projection in ROW_NUMBER and keep one row per key set.
#
#     Returns the SQL and any notes. Deduplication is always visible in the
#     output: the old generator could silently emit none at all.
#     """
#     notes: list[str] = []
#     keys = [quote_name(k) for k in key_sets[0]]
#     if len(key_sets) > 1:
#         notes.append(
#             f"Only the first dedup key set {key_sets[0]} is applied; "
#             f"{len(key_sets) - 1} further set(s) were declared."
#         )
#     order = cohort.get("dedup_order") or cohort.get("order_by")
#     if order:
#         order_sql = order if isinstance(order, str) else ", ".join(str(o) for o in order)
#     else:
#         order_sql = ", ".join(keys)
#         notes.append(
#             f"No dedup ordering supplied for {cohort.get('dest_table')!r}; ordering by the "
#             "key columns, so the surviving row among duplicates is arbitrary but stable."
#         )
#     inner = render_select(cohort, top="", inner_indent="        ")
#     inner = inner.replace(
#         "SELECT\n",
#         "SELECT\n"
#         f"        ROW_NUMBER() OVER (PARTITION BY {', '.join(keys)} ORDER BY {order_sql}) AS [_dedup_rn],\n",
#         1,
#     )
#     cols = column_list(cohort.get("columns") or [])
#     sql = (
#         f"SELECT {top}{cols}\n"
#         f"FROM (\n"
#         f"{_indent(inner, '    ')}\n"
#         f") AS [_deduped]\n"
#         f"WHERE [_deduped].[_dedup_rn] = 1"
#     )
#     return sql, notes
#
#
# def _indent(text: str, prefix: str) -> str:
#     return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())
#
#
# def render_cohort(
#     cohort: dict[str, Any], doc: dict[str, Any], root: dict[str, Any] | None
# ) -> tuple[str, list[str]]:
#     """DDL plus population for one cohort's global temp table."""
#     dest = cohort.get("dest_table")
#     if not dest:
#         raise RenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
#     columns = [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
#     if not columns:
#         raise RenderError(f"Cohort {dest!r} declares no columns.")
#     names = column_names(columns)
#     if len(names) != len(set(names)):
#         duplicates = sorted({n for n in names if names.count(n) > 1})
#         raise RenderError(f"Cohort {dest!r} declares duplicate column(s): {', '.join(duplicates)}")
#
#     notes: list[str] = []
#     temp = global_temp(dest)
#     top = top_clause(cohort, doc, root)
#
#     key_sets, dedup_notes = normalize_dedup_keys(cohort)
#     notes.extend(dedup_notes)
#     if key_sets:
#         problems = validate_dedup_columns(key_sets, cohort)
#         if problems:
#             raise RenderError("; ".join(problems))
#         body, more = render_dedup_select(cohort, key_sets, top)
#         notes.extend(more)
#     else:
#         body = render_select(cohort, top)
#
#     sql = (
#         f"-- cohort {cohort.get('name')!r} -> {temp}\n"
#         f"DROP TABLE IF EXISTS {temp};\n\n"
#         f"CREATE TABLE {temp}\n(\n{ddl_body(columns)}\n);\n\n"
#         f"INSERT INTO {temp} ({column_list(columns)})\n"
#         f"{body};\n\n"
#         f"{render_cohort_telemetry(cohort, temp)}"
#     )
#     # Variable substitution is YAML Manager's job and has already happened by
#     # the time a split YAML reaches us. A placeholder surviving to here would
#     # render as invalid T-SQL, so fail with the name rather than emit it.
#     leftover = sorted({m.group(1) for m in _PLACEHOLDER.finditer(sql)})
#     if leftover:
#         raise RenderError(
#             f"Cohort {dest!r} still contains unsubstituted placeholder(s): "
#             f"{', '.join(leftover)}. Render from split YAML, not a raw template."
#         )
#     return sql, notes
#
#
# def render_cohort_telemetry(cohort: dict[str, Any], temp: str) -> str:
#     """A declared telemetry shape, not column names to be scraped."""
#     return (
#         "SELECT\n"
#         f"    {quote_literal(cohort.get('name'))} AS [CohortName],\n"
#         f"    {quote_literal(cohort.get('dest_table'))} AS [DestTable],\n"
#         f"    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM {temp};"
#     )
#
#
# def render_phase(doc: dict[str, Any], block_prefix: str) -> tuple[list[SqlBlock], list[str]]:
#     """Render every cohort in one phase document."""
#     cohorts = [c for c in doc.get("cohorts") or [] if isinstance(c, dict)]
#     root = root_pk_cohort(cohorts)
#     blocks: list[SqlBlock] = []
#     notes: list[str] = []
#     for cohort in cohorts:
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             notes.append(f"Skipping {cohort.get('dest_table')!r}: pull_this_cycle is false.")
#             continue
#         sql, cohort_notes = render_cohort(cohort, doc, root)
#         notes.extend(cohort_notes)
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/{cohort['dest_table']}",
#                 side="server",
#                 sql=sql,
#                 dest_table=str(cohort["dest_table"]),
#                 meta={"global_temp": global_temp(cohort["dest_table"])},
#             )
#         )
#     return blocks, notes
#
#
# def render_setup(doc: dict[str, Any], block_prefix: str) -> list[SqlBlock]:
#     """Capture the runtime instance name; it changes on every connection."""
#     return [
#         SqlBlock(
#             block_id=f"{block_prefix}/server-identity",
#             side="server",
#             sql=SERVER_NAME_QUERY,
#             meta={"captures": "linked_server"},
#         )
#     ]
#
# === END FILE: pullmanager/server_sql.py ===
# === BEGIN FILE: pullmanager/sql.py SHA256: 35ff5620f0075f8d5db26c0be551717f01a063769339c01e21ea445a5bd7f9d0 SIZE: 5255 ===
# """Shared SQL construction helpers.
#
# The delicate part is the WHERE builder. Authors write predicates as a list of
# lines, and a line may continue a parenthesised boolean group started by the
# previous one, so `AND` cannot simply be inserted between entries.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from typing import Any
#
# from .naming import qualify_join_clause, qualify_table_ref
# from .normalize import normalize_bool
#
# # A line that already begins with one of these continues the previous
# # predicate, so prefixing `AND` would produce invalid SQL.
# CONTINUATION_PREFIXES = ("AND", "OR", ")", "--")
#
#
# @dataclass
# class SqlBlock:
#     """One executable unit, addressed by manifest id rather than by text."""
#
#     block_id: str
#     side: str  # "server" or "local"
#     sql: str
#     dest_table: str | None = None
#     meta: dict[str, Any] = field(default_factory=dict)
#
#     def __repr__(self) -> str:
#         return f"<SqlBlock {self.side} {self.block_id}>"
#
#
# def quote_literal(value: Any) -> str:
#     """Render a Python value as a T-SQL literal."""
#     if value is None:
#         return "NULL"
#     if isinstance(value, bool):
#         return "1" if value else "0"
#     if isinstance(value, (int, float)):
#         return str(value)
#     return "'" + str(value).replace("'", "''") + "'"
#
#
# def quote_name(name: str) -> str:
#     return f"[{name}]"
#
#
# def column_names(columns: list[dict[str, Any]]) -> list[str]:
#     return [str(c["name"]) for c in columns if isinstance(c, dict) and c.get("name")]
#
#
# def column_list(columns: list[dict[str, Any]], indent: str = "") -> str:
#     return ", ".join(quote_name(n) for n in column_names(columns))
#
#
# def is_nullable(column: dict[str, Any]) -> bool:
#     return normalize_bool(column.get("nullable"), default=True)
#
#
# def ddl_body(columns: list[dict[str, Any]]) -> str:
#     """The column definitions inside a CREATE TABLE."""
#     lines = []
#     for column in columns:
#         if not isinstance(column, dict) or not column.get("name"):
#             continue
#         null = "NULL" if is_nullable(column) else "NOT NULL"
#         lines.append(f"    {quote_name(str(column['name']))} {column.get('type', 'VARCHAR(900)')} {null}")
#     return ",\n".join(lines)
#
#
# def from_entries(filter_block: dict[str, Any]) -> list[str]:
#     """`from` may be a string or a list; the old renderer tolerated both."""
#     value = (filter_block or {}).get("from")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def join_entries(filter_block: dict[str, Any]) -> list[str]:
#     value = (filter_block or {}).get("join")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def where_entries(filter_block: dict[str, Any]) -> list[str]:
#     value = (filter_block or {}).get("where")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def non_null_predicates(columns: list[dict[str, Any]]) -> list[str]:
#     """`IS NOT NULL` for every column the cohort declares as non-nullable.
#
#     Without these a NOT NULL destination column rejects the insert partway
#     through, after the expensive part of the pull has already run.
#     """
#     predicates = []
#     for column in columns:
#         if not isinstance(column, dict) or is_nullable(column):
#             continue
#         source = str(column.get("source") or "").strip()
#         if source:
#             predicates.append(f"{source} IS NOT NULL")
#     return predicates
#
#
# def render_where(predicates: list[str], indent: str = "    ") -> str:
#     """Join predicates with AND, leaving continuation lines alone.
#
#     A grouped code list authored as separate list entries must survive intact:
#
#         ( dt.Value LIKE 'K50.%'      ->  AND ( dt.Value LIKE 'K50.%'
#           OR dt.Value = 'K50'        ->      OR dt.Value = 'K50'
#         )                            ->      )
#     """
#     rendered: list[str] = []
#     first = True
#     for raw in predicates:
#         for line in str(raw).splitlines() or [""]:
#             stripped = line.strip()
#             if not stripped:
#                 continue
#             continues = stripped.upper().startswith(CONTINUATION_PREFIXES)
#             if first and not continues:
#                 rendered.append(f"{indent}{stripped}")
#                 first = False
#             elif continues:
#                 rendered.append(f"{indent}{stripped}")
#             else:
#                 rendered.append(f"{indent}AND {stripped}")
#                 first = False
#     return "\n".join(rendered)
#
#
# def render_source_clause(filter_block: dict[str, Any], indent: str = "") -> str:
#     """FROM and JOIN lines, schema-qualified consistently."""
#     lines: list[str] = []
#     froms = from_entries(filter_block)
#     if froms:
#         lines.append(f"{indent}FROM {qualify_table_ref(froms[0]).strip()}")
#         for extra in froms[1:]:
#             lines.append(f"{indent}    , {qualify_table_ref(extra).strip()}")
#     for join in join_entries(filter_block):
#         lines.append(f"{indent}{qualify_join_clause(join.strip())}")
#     return "\n".join(lines)
#
# === END FILE: pullmanager/sql.py ===
# === BEGIN FILE: pullmanager/tests/__init__.py SHA256: 4f70b04f739db1fd4fdae89aa3b2dd3ac8afea47a8dc6b8d5eb7fcda8ed717a2 SIZE: 1508 ===
# """Test suite for the Pullmanager runtime.
#
# Uses stdlib unittest so it stays dependency-free and runs unchanged from an
# extracted bundle on the VM:
#
#     python pullmanager.py --tdd            # everything
#     python pullmanager.py --tdd manifest   # one module
# """
#
# from __future__ import annotations
#
# import importlib
# import unittest
# from pathlib import Path
#
#
# def module_names() -> list[str]:
#     """Discover sibling test modules by filename, so nothing needs registering."""
#     return sorted(path.stem for path in Path(__file__).parent.glob("test_*.py"))
#
#
# def build_suite(group: str | None = None) -> unittest.TestSuite:
#     loader = unittest.TestLoader()
#     suite = unittest.TestSuite()
#     available = module_names()
#     if group:
#         name = group if group.startswith("test_") else f"test_{group}"
#         if name not in available:
#             raise LookupError(
#                 f"No test module {name!r}. Available: "
#                 + ", ".join(n.removeprefix("test_") for n in available)
#             )
#         available = [name]
#     for name in available:
#         module = importlib.import_module(f".{name}", package=__name__)
#         suite.addTests(loader.loadTestsFromModule(module))
#     return suite
#
#
# def run(group: str | None = None, verbosity: int = 2) -> int:
#     try:
#         suite = build_suite(group)
#     except LookupError as exc:
#         print(exc)
#         return 1
#     result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
#     return 0 if result.wasSuccessful() else 1
#
# === END FILE: pullmanager/tests/__init__.py ===
# === BEGIN FILE: pullmanager/tests/support.py SHA256: 5c88b19c05ea4b105763db67d73e42d88cbe1c3897b2ece74c9f287b60403398 SIZE: 4541 ===
# """Shared fixture for the runtime tests."""
#
# from __future__ import annotations
#
# import copy
# from pathlib import Path
# from typing import Any
#
# from ..manifest import Manifest
#
#
# def _node(yaml_path: str, **extra: Any) -> dict[str, Any]:
#     node: dict[str, Any] = {
#         "yaml": yaml_path,
#         "status": "pending",
#         "started_at": None,
#         "finished_at": None,
#         "rows": None,
#         "outputs": {},
#         "error": None,
#     }
#     node.update(extra)
#     return node
#
#
# # Shaped exactly like real `makeYaml.py --export-split` output: one generated-PK
# # session with two batch-product runs, one uploaded-PK session with a single run.
# SAMPLE_MANIFEST: dict[str, Any] = {
#     "manifest_version": 1,
#     "project": {
#         "name": "Manager Valid Multipliers",
#         "project_folder": "Manager Valid Multipliers",
#         "project_db": "PROJECTD33A929",
#         "created_by": "yamlmanager",
#     },
#     "source": {
#         "template": "YAMLs/manager_test_cases/02_valid_multipliers_batching.yaml",
#         "recipes": "YAMLs/recipes.yaml",
#     },
#     "sessions": [
#         {
#             "session_id": "UCblackPatients",
#             "cohort": "UCblackPatients",
#             "pk_table": "UCblackPatients",
#             "status": "pending",
#             "phases": {
#                 "setup": _node("sessions/UCblackPatients/setup.yaml"),
#                 "upload_cohorts": _node("sessions/UCblackPatients/upload_cohorts.yaml"),
#                 "pk": _node(
#                     "sessions/UCblackPatients/pk.yaml",
#                     pk_source={"kind": "generated", "table": "UCblackPatients"},
#                 ),
#             },
#             "runs": [
#                 _node(
#                     "sessions/UCblackPatients/runs/LA-Female.yaml",
#                     run_id="UCblackPatients__LA-Female",
#                     batch={
#                         "name": "LA-Female",
#                         "dimensions": [
#                             {
#                                 "name": "state",
#                                 "kind": "column_values",
#                                 "column": "StateOrProvinceAbbreviation",
#                                 "value": "LA",
#                             },
#                             {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Female"},
#                         ],
#                         "runtime": [
#                             {
#                                 "name": "chunk",
#                                 "kind": "row_chunk",
#                                 "rows_per_batch": 2000,
#                                 "applies_to": "PKTable",
#                             }
#                         ],
#                     },
#                 ),
#                 _node(
#                     "sessions/UCblackPatients/runs/LA-Male.yaml",
#                     run_id="UCblackPatients__LA-Male",
#                     batch={
#                         "name": "LA-Male",
#                         "dimensions": [
#                             {
#                                 "name": "state",
#                                 "kind": "column_values",
#                                 "column": "StateOrProvinceAbbreviation",
#                                 "value": "LA",
#                             },
#                             {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Male"},
#                         ],
#                         "runtime": [],
#                     },
#                 ),
#             ],
#             "multiplier": {"session_label": "UCblackPatients"},
#         },
#         {
#             "session_id": "ClientList",
#             "cohort": "ClientList",
#             "pk_table": "ClientPatientList",
#             "status": "pending",
#             "phases": {
#                 "setup": _node("sessions/ClientList/setup.yaml"),
#                 "upload_cohorts": _node("sessions/ClientList/upload_cohorts.yaml"),
#                 "pk": _node(
#                     "sessions/ClientList/pk.yaml",
#                     pk_source={
#                         "kind": "uploaded_cohort",
#                         "upload_name": "ClientPatientList",
#                         "table": "ClientPatientList",
#                         "key_columns": ["PatientDurableKey"],
#                     },
#                 ),
#             },
#             "runs": [_node("sessions/ClientList/runs/run.yaml", run_id="ClientList__run")],
#         },
#     ],
# }
#
#
# def sample_manifest() -> Manifest:
#     return Manifest(copy.deepcopy(SAMPLE_MANIFEST), path=Path("split/pullmanifest.yaml"))
#
# === END FILE: pullmanager/tests/support.py ===
# === BEGIN FILE: pullmanager/tests/test_db.py SHA256: 1e39f445c46b0952540a09c0887b210ad566223b239be4ed607065ade5987be7 SIZE: 10152 ===
# """Adapter behaviour, exercised against a fake cursor.
#
# pyodbc is not installed on the development machine and there is no database to
# reach, so the DB-API interactions are faked. What is tested is the logic the
# old generator got right and that is easy to get wrong: GO splitting, draining
# every result set, and keeping server messages on the failure path.
# """
#
# from __future__ import annotations
#
# import unittest
#
# from ..db import (
#     DEFAULT_DRIVER,
#     DatabaseError,
#     ResultSet,
#     Settings,
#     bulk_insert,
#     capture_server_name,
#     chunked,
#     collect_messages,
#     drain,
#     execute_script,
#     split_batches,
# )
#
#
# class FakeCursor:
#     """Enough of the DB-API to exercise the drain loop.
#
#     `statements` is one entry per statement in the current batch: None for a
#     statement that returns no rows, or (columns, rows) for one that does.
#     """
#
#     def __init__(self, statements=None, messages=(), fail_on=None):
#         self._template = statements if statements is not None else [None]
#         self._messages = list(messages)
#         self._fail_on = fail_on
#         self.executed: list[str] = []
#         self.executemany_calls: list[tuple[str, list]] = []
#         self.fast_executemany = False
#         self._pending: list = []
#         self._current = None
#
#     @property
#     def messages(self):
#         return self._messages
#
#     def execute(self, sql, params=None):
#         self.executed.append(sql)
#         if self._fail_on is not None and self._fail_on in sql:
#             raise RuntimeError("simulated SQL failure")
#         self._pending = list(self._template)
#         self._advance()
#
#     def executemany(self, sql, seq):
#         self.executemany_calls.append((sql, list(seq)))
#
#     def _advance(self):
#         self._current = self._pending.pop(0) if self._pending else None
#
#     @property
#     def description(self):
#         if self._current is None:
#             return None
#         return [(name,) for name in self._current[0]]
#
#     def fetchall(self):
#         if self._current is None:
#             raise RuntimeError("no result set")
#         return list(self._current[1])
#
#     def fetchone(self):
#         if self._current is None:
#             return None
#         rows = self._current[1]
#         return rows[0] if rows else None
#
#     def nextset(self):
#         if not self._pending:
#             return False
#         self._advance()
#         return True
#
#
# class FakeConnection:
#     def __init__(self, cursor):
#         self._cursor = cursor
#         self.committed = False
#         self.closed = False
#
#     def cursor(self):
#         return self._cursor
#
#     def commit(self):
#         self.committed = True
#
#     def close(self):
#         self.closed = True
#
#
# class SettingsTests(unittest.TestCase):
#     def test_connection_string_matches_the_shape_in_use(self):
#         settings = Settings(cosmos_server="COSMOS", cosmos_database="COSMOS")
#         self.assertEqual(
#             settings.cosmos_connection_string(),
#             "Driver={ODBC Driver 17 for SQL Server};Server=tcp:COSMOS;"
#             "Database=COSMOS;Trusted_Connection=yes;",
#         )
#
#     def test_windows_auth_means_no_credentials_appear(self):
#         rendered = Settings(projects_server="S", projects_database="D").projects_connection_string()
#         self.assertIn("Trusted_Connection=yes", rendered)
#         for secret in ("UID=", "PWD=", "Password"):
#             self.assertNotIn(secret, rendered)
#
#     def test_reads_environment_with_defaults(self):
#         settings = Settings.from_env({})
#         self.assertEqual(settings.driver, DEFAULT_DRIVER)
#         self.assertEqual(settings.cosmos_server, "COSMOS")
#
#         settings = Settings.from_env({
#             "PULLMANAGER_COSMOS_SERVER": "OTHER",
#             "PULLMANAGER_UPLOAD_CHUNK": "500",
#         })
#         self.assertEqual(settings.cosmos_server, "OTHER")
#         self.assertEqual(settings.upload_chunk, 500)
#
#     def test_nonsense_numbers_fall_back(self):
#         self.assertEqual(Settings.from_env({"PULLMANAGER_UPLOAD_CHUNK": "lots"}).upload_chunk, 20_000)
#
#     def test_missing_server_or_database_is_refused(self):
#         with self.assertRaises(DatabaseError):
#             Settings(projects_server="", projects_database="D").projects_connection_string()
#         with self.assertRaises(DatabaseError):
#             Settings(projects_server="S", projects_database="").projects_connection_string()
#
#
# class BatchSplitTests(unittest.TestCase):
#     def test_splits_on_go(self):
#         self.assertEqual(split_batches("SELECT 1\nGO\nSELECT 2"), ["SELECT 1", "SELECT 2"])
#
#     def test_go_is_case_insensitive_and_may_be_indented(self):
#         self.assertEqual(len(split_batches("SELECT 1\n  go  \nSELECT 2")), 2)
#
#     def test_go_inside_a_statement_is_not_a_separator(self):
#         self.assertEqual(len(split_batches("SELECT 'GO' AS x")), 1)
#
#     def test_empty_batches_are_dropped(self):
#         self.assertEqual(split_batches("GO\n\nGO\nSELECT 1\nGO\n"), ["SELECT 1"])
#
#     def test_script_without_go_is_one_batch(self):
#         self.assertEqual(len(split_batches("SELECT 1\nSELECT 2")), 1)
#
#
# class DrainTests(unittest.TestCase):
#     def test_collects_every_result_set(self):
#         cursor = FakeCursor([(["A"], [(1,)]), (["B"], [(2,), (3,)])])
#         cursor.execute("x")
#         results = drain(cursor)
#         self.assertEqual([r.columns for r in results], [["A"], ["B"]])
#         self.assertEqual(results[1].rows, [(2,), (3,)])
#
#     def test_skips_statements_that_return_nothing(self):
#         # A generated script interleaves DDL and inserts with telemetry SELECTs.
#         cursor = FakeCursor([None, (["A"], [(1,)]), None])
#         cursor.execute("x")
#         results = drain(cursor)
#         self.assertEqual(len(results), 1)
#         self.assertEqual(results[0].columns, ["A"])
#
#     def test_result_rows_convert_to_dicts(self):
#         self.assertEqual(
#             ResultSet(["A", "B"], [(1, 2)]).as_dicts(), [{"A": 1, "B": 2}]
#         )
#
#
# class ExecuteScriptTests(unittest.TestCase):
#     def test_runs_every_batch(self):
#         cursor = FakeCursor([(["A"], [(1,)])])
#         outcome = execute_script(FakeConnection(cursor), "SELECT 1\nGO\nSELECT 2")
#         self.assertEqual(len(cursor.executed), 2)
#         self.assertEqual(len(outcome.result_sets), 2)
#
#     def test_failure_names_the_batch_and_keeps_server_messages(self):
#         # Without the messages, a failed OPENQUERY reports only a generic outer
#         # error and the actual cause is lost.
#         cursor = FakeCursor(
#             messages=[("42000", "OLE DB provider returned message: Login timeout expired")],
#             fail_on="BOOM",
#         )
#         with self.assertRaises(DatabaseError) as caught:
#             execute_script(FakeConnection(cursor), "SELECT 1\nGO\nBOOM\nGO\nSELECT 3", label="pk")
#         text = str(caught.exception)
#         self.assertIn("batch 2/3", text)
#         self.assertIn("pk", text)
#         self.assertIn("Login timeout expired", text)
#
#     def test_telemetry_is_selected_by_declared_shape(self):
#         cursor = FakeCursor([
#             (["Unrelated"], [("x",)]),
#             (["DestTable", "RowCount"], [("PKTable", 12345)]),
#         ])
#         outcome = execute_script(FakeConnection(cursor), "SELECT 1")
#         self.assertEqual(
#             outcome.rows_of("DestTable", "RowCount"),
#             [{"DestTable": "PKTable", "RowCount": 12345}],
#         )
#
#     def test_messages_are_formatted_as_text(self):
#         cursor = FakeCursor(messages=[("01000", "row count"), ("01000", "done")])
#         self.assertEqual(collect_messages(cursor), ["01000 | row count", "01000 | done"])
#
#
# class ServerNameTests(unittest.TestCase):
#     def test_captures_the_instance_name(self):
#         cursor = FakeCursor([(["CosmosServerName"], [("et4003vpdsql032",)])])
#         self.assertEqual(capture_server_name(FakeConnection(cursor)), "et4003vpdsql032")
#
#     def test_absent_name_is_a_hard_error(self):
#         # Local SQL cannot build OPENQUERY without it, so a silent fallback
#         # would aim the transfer at nothing.
#         with self.assertRaises(DatabaseError):
#             capture_server_name(FakeConnection(FakeCursor([(["x"], [])])))
#
#
# class BulkInsertTests(unittest.TestCase):
#     def rows(self, count):
#         return [(i,) for i in range(count)]
#
#     def test_uses_parameter_binding_not_a_values_list(self):
#         # A table value constructor is capped at 1000 rows; parameter arrays
#         # are not, because the statement stays single-row.
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(5))
#         statement, batch = cursor.executemany_calls[0]
#         self.assertEqual(statement, "INSERT INTO ##JVM_X ([Key]) VALUES (?)")
#         self.assertEqual(len(batch), 5)
#
#     def test_enables_fast_executemany(self):
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(1))
#         self.assertTrue(cursor.fast_executemany)
#
#     def test_chunks_beyond_the_thousand_row_limit(self):
#         cursor = FakeCursor()
#         inserted = bulk_insert(
#             FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(2500), chunk_size=1000
#         )
#         self.assertEqual(inserted, 2500)
#         self.assertEqual([len(b) for _, b in cursor.executemany_calls], [1000, 1000, 500])
#
#     def test_multi_column_placeholders(self):
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "T", ["A", "B", "C"], [(1, 2, 3)])
#         self.assertEqual(cursor.executemany_calls[0][0], "INSERT INTO T ([A], [B], [C]) VALUES (?, ?, ?)")
#
#     def test_no_columns_is_refused(self):
#         with self.assertRaises(DatabaseError):
#             bulk_insert(FakeConnection(FakeCursor()), "T", [], [])
#
#     def test_empty_input_inserts_nothing(self):
#         cursor = FakeCursor()
#         self.assertEqual(bulk_insert(FakeConnection(cursor), "T", ["A"], []), 0)
#         self.assertEqual(cursor.executemany_calls, [])
#
#
# class ChunkTests(unittest.TestCase):
#     def test_splits_into_even_chunks_with_a_remainder(self):
#         self.assertEqual([len(c) for c in chunked(range(7), 3)], [3, 3, 1])
#
#     def test_empty_input_yields_nothing(self):
#         self.assertEqual(list(chunked([], 3)), [])
#
# === END FILE: pullmanager/tests/test_db.py ===
# === BEGIN FILE: pullmanager/tests/test_executor.py SHA256: f1907b950db229ff68fbcb7c455df5dd7e9b735a2b130f71f20ca8b5164ce488 SIZE: 6609 ===
# """Traversal order and resume policy."""
#
# from __future__ import annotations
#
# import shutil
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..executor import (
#     RESUME_FULL,
#     RESUME_PARTIAL,
#     PlanError,
#     excluded_units,
#     iter_units,
#     plan,
#     plan_session,
#     session_cohorts,
#     should_execute,
#     write_sql,
# )
# from ..manifest import Manifest
# from .support import sample_manifest
#
# FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
#
#
# class TraversalTests(unittest.TestCase):
#     def test_phases_come_in_routine_order_then_runs(self):
#         manifest = sample_manifest()
#         kinds = [kind for kind, _, _ in iter_units(manifest, manifest.sessions[0])]
#         self.assertEqual(kinds, ["setup", "upload_cohorts", "pk", "run", "run"])
#
#     def test_runs_keep_manifest_order(self):
#         manifest = sample_manifest()
#         labels = [
#             node.label for kind, node, _ in iter_units(manifest, manifest.sessions[0])
#             if kind == "run"
#         ]
#         self.assertEqual(labels, ["UCblackPatients__LA-Female", "UCblackPatients__LA-Male"])
#
#
# class ShouldExecuteTests(unittest.TestCase):
#     def node(self, status, epoch=None):
#         manifest = sample_manifest()
#         node = manifest.sessions[0].phases[0]
#         node.data["status"] = status
#         if epoch:
#             node.data["epoch"] = epoch
#         return node
#
#     def test_pending_runs(self):
#         run, _ = should_execute(self.node("pending"), "setup")
#         self.assertTrue(run)
#
#     def test_skipped_does_not_run(self):
#         run, why = should_execute(self.node("skipped"), "setup")
#         self.assertFalse(run)
#         self.assertIn("deliberately", why)
#
#     def test_interrupted_running_is_resumed(self):
#         run, why = should_execute(self.node("running"), "setup")
#         self.assertTrue(run)
#         self.assertIn("interrupted", why)
#
#     def test_blocked_is_retried(self):
#         run, _ = should_execute(self.node("blocked"), "setup")
#         self.assertTrue(run)
#
#     def test_failed_needs_an_explicit_retry(self):
#         node = self.node("failed")
#         self.assertFalse(should_execute(node, "run")[0])
#         self.assertTrue(should_execute(node, "run", retry_failed=True)[0])
#
#     def test_done_in_this_session_is_skipped(self):
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "setup", current_epoch="e1")
#         self.assertFalse(run)
#         self.assertIn("already done", why)
#
#     def test_done_under_a_previous_connection_replays(self):
#         # Global temps died with that connection, so the status is true but the
#         # output is gone.
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "setup", current_epoch="e2")
#         self.assertTrue(run)
#         self.assertIn("server state is gone", why)
#
#     def test_partial_resume_keeps_completed_runs(self):
#         # A run's durable result is rows in a Projects table, which survive.
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "run", current_epoch="e2", mode=RESUME_PARTIAL)
#         self.assertFalse(run)
#         self.assertIn("survive", why)
#
#     def test_partial_resume_still_replays_phases(self):
#         node = self.node("done", epoch="e1")
#         run, _ = should_execute(node, "pk", current_epoch="e2", mode=RESUME_PARTIAL)
#         self.assertTrue(run)
#
#     def test_manifest_without_epochs_is_not_treated_as_stale(self):
#         node = self.node("done")
#         self.assertFalse(should_execute(node, "setup", current_epoch="e9")[0])
#
#
# class PlanningTests(unittest.TestCase):
#     def setUp(self):
#         if not FIXTURES.is_dir():
#             self.skipTest(f"fixtures not found at {FIXTURES}")
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.root = Path(self._tmp.name) / "split"
#         shutil.copytree(FIXTURES, self.root)
#         self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
#
#     def test_plans_every_unit_of_a_fresh_manifest(self):
#         units = plan(self.manifest)
#         self.assertEqual(
#             [u.kind for u in units], ["setup", "upload_cohorts", "pk", "run"]
#         )
#
#     def test_setup_creates_a_shell_for_every_destination(self):
#         setup = plan(self.manifest)[0]
#         shells = [b for b in setup.local_blocks if "shell" in b.block_id]
#         self.assertEqual(len(shells), len(session_cohorts(self.manifest, self.manifest.sessions[0])))
#         self.assertTrue(all("CREATE TABLE" in b.sql for b in shells))
#
#     def test_session_cohorts_are_deduplicated(self):
#         names = [c["dest_table"] for c in session_cohorts(self.manifest, self.manifest.sessions[0])]
#         self.assertEqual(len(names), len(set(names)))
#
#     def test_run_units_render_both_sides(self):
#         run = [u for u in plan(self.manifest) if u.kind == "run"][0]
#         self.assertTrue(run.server_blocks)
#         self.assertTrue(run.local_blocks)
#
#     def test_completed_work_is_excluded_and_reported(self):
#         session = self.manifest.sessions[0]
#         session.begin_epoch(linked_server="ls")
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].start()
#         session.runs[0].fail("boom")
#         self.manifest.save()
#
#         reloaded = Manifest.load(self.root / "pullmanifest.yaml")
#         left_out = excluded_units(reloaded)
#         statuses = {label.split("/")[-1]: status for label, status, _ in left_out}
#         self.assertIn("failed", statuses.values())
#
#         units = plan(reloaded)
#         # A new connection means the phases replay even though they are done.
#         self.assertTrue(any(u.kind == "pk" for u in units))
#
#     def test_missing_phase_yaml_is_refused(self):
#         (self.root / "sessions" / "Patients" / "pk.yaml").unlink()
#         with self.assertRaises(PlanError):
#             plan(self.manifest)
#
#     def test_writes_one_file_per_block(self):
#         units = plan(self.manifest)
#         with tempfile.TemporaryDirectory() as out:
#             written = write_sql(units, Path(out))
#             self.assertEqual(len(written), sum(len(u.blocks) for u in units))
#             self.assertTrue(all(p.read_text(encoding="utf-8").strip() for p in written))
#             self.assertTrue(all(p.suffix == ".sql" for p in written))
#
#     def test_plan_is_empty_when_nothing_needs_doing(self):
#         session = self.manifest.sessions[0]
#         for child in session.children:
#             child.skip("not wanted")
#         self.assertEqual(plan_session(self.manifest, session), [])
#
# === END FILE: pullmanager/tests/test_executor.py ===
# === BEGIN FILE: pullmanager/tests/test_manifest.py SHA256: 78f8843188969abfa24793cbd298a3e24ede337d3ebb80a5a3a7c1c365f42ff7 SIZE: 14639 ===
# """Manifest loading, validation, status transitions, and round-tripping."""
#
# from __future__ import annotations
#
# import copy
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..manifest import Manifest, ManifestError
# from ..models import BLOCKED, DONE, FAILED, PENDING, RUNNING, SKIPPED
# from ..yaml_io import dump_yaml
# from .support import SAMPLE_MANIFEST, sample_manifest
#
#
# class TempDirTestCase(unittest.TestCase):
#     def setUp(self):
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.tmp = Path(self._tmp.name)
#         self.manifest_path = self.tmp / "pullmanifest.yaml"
#
#
# class LoadTests(unittest.TestCase):
#     def test_reads_sessions_phases_and_runs(self):
#         manifest = sample_manifest()
#         self.assertEqual(len(manifest.sessions), 2)
#         self.assertEqual(manifest.sessions[0].session_id, "UCblackPatients")
#         self.assertEqual(
#             [phase.name for phase in manifest.sessions[0].phases],
#             ["setup", "upload_cohorts", "pk"],
#         )
#         self.assertEqual(len(manifest.sessions[0].runs), 2)
#
#     def test_source_is_template_and_recipes(self):
#         # The design prose said `source.preyaml`; makeYaml emits template +
#         # recipes. Pin the real contract so drift shows up here.
#         self.assertEqual(sorted(sample_manifest().source), ["recipes", "template"])
#
#     def test_exposes_both_pk_source_kinds(self):
#         manifest = sample_manifest()
#         generated = manifest.sessions[0].phases[2]
#         uploaded = manifest.sessions[1].phases[2]
#         self.assertEqual(generated.pk_source["kind"], "generated")
#         self.assertEqual(uploaded.pk_source["kind"], "uploaded_cohort")
#         self.assertEqual(uploaded.pk_source["key_columns"], ["PatientDurableKey"])
#
#     def test_resolves_yaml_paths_against_manifest_directory(self):
#         manifest = sample_manifest()
#         setup = manifest.sessions[0].phases[0]
#         self.assertEqual(manifest.resolve(setup), Path("split/sessions/UCblackPatients/setup.yaml"))
#
#
# class ValidationTests(unittest.TestCase):
#     def assertRejects(self, data, fragment):
#         with self.assertRaises(ManifestError) as caught:
#             Manifest(data)
#         self.assertIn(fragment.lower(), str(caught.exception).lower())
#
#     def test_rejects_unsupported_version(self):
#         self.assertRejects({"manifest_version": 99, "sessions": []}, "manifest_version")
#
#     def test_rejects_missing_sessions_list(self):
#         self.assertRejects({"manifest_version": 1}, "sessions")
#
#     def test_rejects_non_mapping_root(self):
#         with self.assertRaises(ManifestError):
#             Manifest(["not", "a", "mapping"])
#
#     def test_rejects_unknown_phase_name(self):
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {"teardown": {"yaml": "x.yaml", "status": "pending"}},
#                         "runs": [],
#                     }
#                 ],
#             },
#             "unknown phase",
#         )
#
#     def test_rejects_duplicate_run_id(self):
#         run = {"run_id": "R", "yaml": "r.yaml", "status": "pending"}
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {},
#                         "runs": [dict(run), dict(run)],
#                     }
#                 ],
#             },
#             "duplicate run_id",
#         )
#
#     def test_rejects_duplicate_session_id(self):
#         session = {"session_id": "S", "status": "pending", "phases": {}, "runs": []}
#         self.assertRejects(
#             {"manifest_version": 1, "sessions": [dict(session), dict(session)]},
#             "duplicate session_id",
#         )
#
#     def test_rejects_phase_without_yaml_path(self):
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {"setup": {"status": "pending"}},
#                         "runs": [],
#                     }
#                 ],
#             },
#             "missing its `yaml`",
#         )
#
#     def test_rejects_unknown_status_value(self):
#         data = copy.deepcopy(SAMPLE_MANIFEST)
#         data["sessions"][0]["phases"]["setup"]["status"] = "finished"
#         with self.assertRaises(Exception):
#             Manifest(data)
#
#
# class TransitionTests(unittest.TestCase):
#     def test_start_marks_running_and_stamps_start(self):
#         phase = sample_manifest().sessions[0].phases[0]
#         phase.start()
#         self.assertEqual(phase.status, RUNNING)
#         self.assertIsNotNone(phase.data["started_at"])
#         self.assertIsNone(phase.data["finished_at"])
#
#     def test_finish_records_rows_outputs_and_duration(self):
#         phase = sample_manifest().sessions[0].phases[2]
#         phase.start()
#         phase.finish(rows=12345, outputs={"global_temp": "##JVM_UCblackPatients"})
#         self.assertEqual(phase.status, DONE)
#         self.assertEqual(phase.rows, 12345)
#         self.assertEqual(phase.outputs["global_temp"], "##JVM_UCblackPatients")
#         self.assertIsNotNone(phase.data["finished_at"])
#         self.assertGreaterEqual(phase.data["duration"]["seconds"], 0)
#
#     def test_fail_records_message_and_detail(self):
#         run = sample_manifest().sessions[0].runs[0]
#         run.start()
#         run.fail("OPENQUERY failed", detail="Login timeout expired")
#         self.assertEqual(run.status, FAILED)
#         self.assertEqual(run.error["message"], "OPENQUERY failed")
#         self.assertEqual(run.error["detail"], "Login timeout expired")
#
#     def test_retry_clears_previous_error_and_duration(self):
#         run = sample_manifest().sessions[0].runs[0]
#         run.start()
#         run.fail("boom")
#         run.start()
#         self.assertEqual(run.status, RUNNING)
#         self.assertIsNone(run.error)
#         self.assertNotIn("duration", run.data)
#
#     def test_skip_and_block_use_note_not_error(self):
#         # A skipped phase is not a failure; an `error` block would read like one.
#         for action, expected in (("skip", SKIPPED), ("block", BLOCKED)):
#             with self.subTest(action=action):
#                 run = sample_manifest().sessions[0].runs[1]
#                 getattr(run, action)("upstream PK failed")
#                 self.assertEqual(run.status, expected)
#                 self.assertEqual(run.note, "upstream PK failed")
#                 self.assertIsNone(run.error)
#
#
# class SessionRollupTests(unittest.TestCase):
#     def test_pending_while_untouched(self):
#         self.assertEqual(sample_manifest().sessions[0].recompute_status(), PENDING)
#
#     def test_running_when_partially_complete(self):
#         session = sample_manifest().sessions[0]
#         session.phases[0].start()
#         session.phases[0].finish()
#         self.assertEqual(session.recompute_status(), RUNNING)
#
#     def test_failure_outranks_every_other_state(self):
#         session = sample_manifest().sessions[0]
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].start()
#         session.runs[0].fail("nope")
#         session.runs[1].block("upstream failed")
#         self.assertEqual(session.recompute_status(), FAILED)
#
#     def test_done_when_every_child_is_done(self):
#         session = sample_manifest().sessions[1]
#         for child in session.children:
#             child.start()
#             child.finish()
#         self.assertEqual(session.recompute_status(), DONE)
#
#     def test_done_when_children_mix_done_and_skipped(self):
#         session = sample_manifest().sessions[1]
#         session.phases[0].start()
#         session.phases[0].finish()
#         session.phases[1].skip("no upload cohorts")
#         session.phases[2].start()
#         session.phases[2].finish()
#         session.runs[0].start()
#         session.runs[0].finish()
#         self.assertEqual(session.recompute_status(), DONE)
#
#     def test_skipped_only_when_everything_skipped(self):
#         session = sample_manifest().sessions[1]
#         for child in session.children:
#             child.skip()
#         self.assertEqual(session.recompute_status(), SKIPPED)
#
#     def test_blocked_when_blocking_is_the_worst_state(self):
#         session = sample_manifest().sessions[1]
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].block("upstream failed")
#         self.assertEqual(session.recompute_status(), BLOCKED)
#
#
# class EpochTests(unittest.TestCase):
#     """Global temps die with the connection; the epoch is how we know."""
#
#     def test_begin_epoch_records_connection_facts(self):
#         session = sample_manifest().sessions[0]
#         epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
#         self.assertEqual(session.epoch, epoch)
#         self.assertEqual(session.runtime["linked_server"], "et4003vpdsq1032")
#         self.assertIsNotNone(session.runtime["opened_at"])
#
#     def test_new_epoch_clears_a_stale_linked_server(self):
#         # The Cosmos instance name changes every connection, so a value from a
#         # previous epoch must never survive into the next one.
#         session = sample_manifest().sessions[0]
#         session.begin_epoch(linked_server="et4003vpdsql032")
#         session.begin_epoch()
#         self.assertIsNone(session.runtime["linked_server"])
#
#     def test_each_epoch_is_distinct(self):
#         session = sample_manifest().sessions[0]
#         self.assertNotEqual(session.begin_epoch(), session.begin_epoch())
#
#     def test_finishing_stamps_the_current_epoch(self):
#         session = sample_manifest().sessions[0]
#         epoch = session.begin_epoch()
#         phase = session.phases[2]
#         phase.start()
#         phase.finish(rows=12345)
#         self.assertEqual(phase.epoch, epoch)
#
#     def test_work_from_the_current_epoch_is_not_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         phase = session.phases[2]
#         phase.start()
#         phase.finish()
#         self.assertFalse(phase.is_stale(session.epoch))
#
#     def test_work_from_a_previous_epoch_is_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         for child in session.children:
#             child.start()
#             child.finish()
#
#         # Restarting the process opens a new connection; the old temps are gone.
#         session.begin_epoch()
#         self.assertEqual(len(session.stale_children()), len(session.children))
#         self.assertTrue(session.phases[2].is_stale(session.epoch))
#
#     def test_unfinished_work_is_never_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         self.assertFalse(session.phases[0].is_stale(session.epoch))
#         session.runs[0].block("upstream failed")
#         self.assertFalse(session.runs[0].is_stale(session.epoch))
#
#     def test_manifest_without_epochs_is_never_stale(self):
#         # Manifests written before epochs existed must not be read as stale.
#         session = sample_manifest().sessions[0]
#         phase = session.phases[0]
#         phase.start()
#         phase.finish()
#         self.assertIsNone(phase.epoch)
#         self.assertFalse(phase.is_stale("some-new-epoch"))
#
#
# class RoundTripTests(TempDirTestCase):
#     def saved_manifest(self) -> Manifest:
#         manifest = sample_manifest()
#         manifest.path = self.manifest_path
#         return manifest
#
#     def test_status_and_rows_survive_save_and_reload(self):
#         manifest = self.saved_manifest()
#         manifest.sessions[0].phases[0].start()
#         manifest.sessions[0].phases[0].finish(rows=42)
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path)
#         setup = reloaded.sessions[0].phases[0]
#         self.assertEqual(setup.status, DONE)
#         self.assertEqual(setup.rows, 42)
#         self.assertEqual(reloaded.sessions[0].status, RUNNING)
#
#     def test_unknown_keys_survive(self):
#         manifest = self.saved_manifest()
#         manifest.data["future_field"] = {"added_by": "a later yamlmanager"}
#         manifest.sessions[0].runs[0].data["parquet_hint"] = "keep me"
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path)
#         self.assertEqual(reloaded.data["future_field"], {"added_by": "a later yamlmanager"})
#         self.assertEqual(reloaded.sessions[0].runs[0].data["parquet_hint"], "keep me")
#
#     def test_batch_product_and_multiplier_context_survive(self):
#         manifest = self.saved_manifest()
#         manifest.save()
#
#         run = Manifest.load(self.manifest_path).sessions[0].runs[0]
#         self.assertEqual(run.batch["name"], "LA-Female")
#         self.assertEqual(
#             [dim["value"] for dim in run.batch["dimensions"]],
#             ["LA", "Female"],
#         )
#         self.assertEqual([dim["name"] for dim in run.batch["runtime"]], ["chunk"])
#
#     def test_save_leaves_no_temp_file_behind(self):
#         self.saved_manifest().save()
#         self.assertEqual(
#             sorted(path.name for path in self.tmp.iterdir()),
#             ["pullmanifest.yaml"],
#         )
#
#     def test_epoch_survives_save_and_reload(self):
#         manifest = self.saved_manifest()
#         session = manifest.sessions[0]
#         epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
#         session.phases[0].start()
#         session.phases[0].finish()
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path).sessions[0]
#         self.assertEqual(reloaded.epoch, epoch)
#         self.assertEqual(reloaded.runtime["linked_server"], "et4003vpdsq1032")
#         self.assertEqual(reloaded.phases[0].epoch, epoch)
#         self.assertFalse(reloaded.phases[0].is_stale(epoch))
#         self.assertTrue(reloaded.phases[0].is_stale("a-later-epoch"))
#
#     def test_load_rejects_missing_file(self):
#         with self.assertRaises(ManifestError):
#             Manifest.load(self.tmp / "nope.yaml")
#
#     def test_bare_yaml_nulls_load_as_none(self):
#         # makeYaml writes empty values as bare `key:`; they must come back as
#         # None rather than the string "None".
#         dump_yaml(SAMPLE_MANIFEST, self.manifest_path)
#         self.assertIn("started_at:", self.manifest_path.read_text(encoding="utf-8"))
#         reloaded = Manifest.load(self.manifest_path)
#         self.assertIsNone(reloaded.sessions[0].phases[0].data["started_at"])
#
# === END FILE: pullmanager/tests/test_manifest.py ===
# === BEGIN FILE: pullmanager/tests/test_models.py SHA256: 9293f87dcebe251868ceb465460b0067c9642790941c6e26250a6cb8d47044a8 SIZE: 2221 ===
# """Duration formatting and the status vocabulary."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..models import (
#     ALL_STATUSES,
#     SETTLED_STATUSES,
#     DONE,
#     SKIPPED,
#     StatusError,
#     duration_block,
#     format_duration,
#     validate_status,
# )
#
#
# class FormatDurationTests(unittest.TestCase):
#     def test_renders_expected_shapes(self):
#         cases = [
#             (0, "0s"),
#             (45, "45s"),
#             (60, "1m 0s"),
#             (132, "2m 12s"),
#             (332, "5m 32s"),
#             (3600, "1h 0m 0s"),
#             (3723, "1h 2m 3s"),
#         ]
#         for seconds, expected in cases:
#             with self.subTest(seconds=seconds):
#                 self.assertEqual(format_duration(seconds), expected)
#
#     def test_rounds_fractional_seconds(self):
#         self.assertEqual(format_duration(45.4), "45s")
#         self.assertEqual(format_duration(45.6), "46s")
#
#
# class DurationBlockTests(unittest.TestCase):
#     def test_builds_block_from_timestamps(self):
#         self.assertEqual(
#             duration_block("2026-09-22T14:03:00-05:00", "2026-09-22T14:08:32-05:00"),
#             {"seconds": 332, "display": "5m 32s"},
#         )
#
#     def test_handles_offset_differences(self):
#         # Same instant expressed in two zones is a zero-length duration.
#         self.assertEqual(
#             duration_block("2026-09-22T14:00:00-05:00", "2026-09-22T15:00:00-04:00"),
#             {"seconds": 0, "display": "0s"},
#         )
#
#     def test_returns_none_without_both_timestamps(self):
#         self.assertIsNone(duration_block(None, "2026-09-22T14:08:32-05:00"))
#         self.assertIsNone(duration_block("2026-09-22T14:03:00-05:00", None))
#         self.assertIsNone(duration_block(None, None))
#
#
# class StatusTests(unittest.TestCase):
#     def test_accepts_every_known_status(self):
#         for status in ALL_STATUSES:
#             with self.subTest(status=status):
#                 self.assertEqual(validate_status(status), status)
#
#     def test_rejects_unknown_status(self):
#         with self.assertRaises(StatusError):
#             validate_status("finished")
#
#     def test_settled_statuses_are_the_ones_resume_skips(self):
#         self.assertEqual(set(SETTLED_STATUSES), {DONE, SKIPPED})
#
# === END FILE: pullmanager/tests/test_models.py ===
# === BEGIN FILE: pullmanager/tests/test_naming.py SHA256: 52969558fa458ea931e5baf62abbb75426294cdc3cc854a8f867fefbad0c5d5f SIZE: 5298 ===
# """Naming invariants: server, staging and destination must agree."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..naming import (
#     NamingError,
#     base_name,
#     destination,
#     global_temp,
#     is_schema_qualified,
#     is_temp_table,
#     local_staging,
#     qualify,
#     qualify_join_clause,
#     qualify_table_ref,
# )
#
#
# class GlobalTempTests(unittest.TestCase):
#     def test_prefixes_a_plain_name(self):
#         self.assertEqual(global_temp("PKTable"), "##JVM_PKTable")
#
#     def test_never_doubles_the_jvm_prefix(self):
#         # The old generator's ##JVM_JVM_Foo bug.
#         for given in ("JVM_PKTable", "jvm_PKTable", "##JVM_PKTable"):
#             with self.subTest(given=given):
#                 self.assertEqual(global_temp(given), "##JVM_PKTable")
#
#     def test_is_idempotent(self):
#         once = global_temp("PKTable")
#         self.assertEqual(global_temp(once), once)
#
#
# class LocalStagingTests(unittest.TestCase):
#     def test_prefixes_a_plain_name(self):
#         self.assertEqual(local_staging("PKTable"), "#Local_PKTable")
#
#     def test_is_idempotent(self):
#         self.assertEqual(local_staging("#Local_PKTable"), "#Local_PKTable")
#
#     def test_derives_from_a_global_temp_name(self):
#         self.assertEqual(local_staging("##JVM_PKTable"), "#Local_PKTable")
#
#
# class DestinationTests(unittest.TestCase):
#     def test_is_always_fully_qualified(self):
#         self.assertEqual(
#             destination("PROJECTD93A5E7", "PKTable"),
#             "PROJECTD93A5E7.dbo.PKTable",
#         )
#
#     def test_strips_generator_prefixes(self):
#         self.assertEqual(
#             destination("PROJECTD93A5E7", "##JVM_PKTable"),
#             "PROJECTD93A5E7.dbo.PKTable",
#         )
#
#     def test_requires_a_project_db(self):
#         with self.assertRaises(NamingError):
#             destination("", "PKTable")
#
#
# class MissingNameTests(unittest.TestCase):
#     def test_blank_dest_table_is_rejected(self):
#         for given in (None, "", "   "):
#             with self.subTest(given=given):
#                 for fn in (base_name, global_temp, local_staging):
#                     with self.assertRaises(NamingError):
#                         fn(given)
#
#
# class PredicateTests(unittest.TestCase):
#     def test_identifies_temp_tables(self):
#         self.assertTrue(is_temp_table("#Local_X"))
#         self.assertTrue(is_temp_table("##JVM_X"))
#         self.assertFalse(is_temp_table("PatientDim"))
#
#     def test_dots_inside_brackets_do_not_qualify(self):
#         self.assertTrue(is_schema_qualified("dbo.PatientDim"))
#         self.assertFalse(is_schema_qualified("[My.Table]"))
#         self.assertTrue(is_schema_qualified("[dbo].[PatientDim]"))
#
#
# class QualifyTests(unittest.TestCase):
#     def test_adds_the_default_schema(self):
#         self.assertEqual(qualify("PatientDim"), "dbo.PatientDim")
#         self.assertEqual(qualify("[PatientDim]"), "dbo.[PatientDim]")
#
#     def test_never_produces_dbo_dbo(self):
#         for given in ("dbo.PatientDim", "[dbo].[PatientDim]", "COSMOS.dbo.PatientDim"):
#             with self.subTest(given=given):
#                 self.assertEqual(qualify(given), given)
#
#     def test_leaves_temp_tables_unqualified(self):
#         # Global temps live in tempdb; qualifying them would break the reference.
#         self.assertEqual(qualify("##JVM_PKTable2"), "##JVM_PKTable2")
#         self.assertEqual(qualify("#Local_PKTable"), "#Local_PKTable")
#
#
# class QualifyTableRefTests(unittest.TestCase):
#     def test_qualifies_a_from_entry_keeping_the_alias(self):
#         self.assertEqual(qualify_table_ref("PatientDim AS p"), "dbo.PatientDim AS p")
#
#     def test_is_idempotent(self):
#         once = qualify_table_ref("PatientDim AS p")
#         self.assertEqual(qualify_table_ref(once), once)
#
#     def test_leaves_generated_temps_alone(self):
#         self.assertEqual(
#             qualify_table_ref("##JVM_PKTable2 AS p"), "##JVM_PKTable2 AS p"
#         )
#
#
# class QualifyJoinClauseTests(unittest.TestCase):
#     def test_qualifies_the_joined_table(self):
#         self.assertEqual(
#             qualify_join_clause(
#                 "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"
#             ),
#             "INNER JOIN dbo.DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#         )
#
#     def test_handles_every_join_flavour(self):
#         for kind in ("INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "CROSS JOIN", "join"):
#             with self.subTest(kind=kind):
#                 self.assertIn(
#                     "dbo.EncounterFact",
#                     qualify_join_clause(f"{kind} EncounterFact AS e ON 1 = 1"),
#                 )
#
#     def test_leaves_generated_temps_alone(self):
#         clause = "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey"
#         self.assertEqual(qualify_join_clause(clause), clause)
#
#     def test_does_not_touch_the_on_predicate(self):
#         # Column references are aliases, not tables, and must be left alone.
#         clause = "INNER JOIN Foo AS f ON f.Bar = baz.Qux"
#         self.assertEqual(
#             qualify_join_clause(clause),
#             "INNER JOIN dbo.Foo AS f ON f.Bar = baz.Qux",
#         )
#
#     def test_is_idempotent(self):
#         once = qualify_join_clause("INNER JOIN Foo AS f ON 1 = 1")
#         self.assertEqual(qualify_join_clause(once), once)
#
# === END FILE: pullmanager/tests/test_naming.py ===
# === BEGIN FILE: pullmanager/tests/test_normalize.py SHA256: 7f8df67188a3032b297e0c22ce7e20861ce4e526c1b710bab86b2bd3d56d2f10 SIZE: 5859 ===
# """Compatibility rules for hand-authored cohort YAML."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..normalize import (
#     NormalizationError,
#     dead_options,
#     joined_generated_tables,
#     normalize_bool,
#     normalize_dedup_keys,
#     root_pk_cohort,
#     root_pk_cohorts,
#     validate_dedup_columns,
# )
#
# # The chained PK from inputSimple.yaml: a patient list, then diagnosis events
# # for those patients.
# PATIENTS = {
#     "name": "PKTable",
#     "type": "PK",
#     "dest_table": "PKTable2",
#     "columns": [{"name": "PatientDurableKey"}],
#     "filter": {"from": "PatientDim AS p", "join": []},
# }
# EVENTS = {
#     "name": "PKTable",
#     "type": "PK",
#     "dest_table": "PKTable",
#     "dedup_key": ["DiagnosisEventKey"],
#     "columns": [{"name": "DiagnosisEventKey"}, {"name": "PatientDurableKey"}],
#     "filter": {
#         "from": "DiagnosisEventFact AS def",
#         "join": [
#             "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#             "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
#         ],
#     },
# }
# FACT = {"name": "OtherDx", "type": "fact", "dest_table": "OtherDx", "filter": {}}
#
#
# class BooleanTests(unittest.TestCase):
#     def test_accepts_truthy_spellings(self):
#         for given in (True, 1, "true", "True", "YES", "y", "1", "on", "t"):
#             with self.subTest(given=given):
#                 self.assertTrue(normalize_bool(given))
#
#     def test_accepts_falsy_spellings(self):
#         for given in (False, 0, "false", "No", "n", "0", "off", ""):
#             with self.subTest(given=given):
#                 self.assertFalse(normalize_bool(given))
#
#     def test_none_takes_the_default(self):
#         self.assertFalse(normalize_bool(None))
#         self.assertTrue(normalize_bool(None, default=True))
#
#     def test_rejects_nonsense(self):
#         with self.assertRaises(NormalizationError):
#             normalize_bool("maybe")
#
#
# class DedupKeyTests(unittest.TestCase):
#     def test_legacy_singular_is_accepted_with_a_note(self):
#         # The old generator accepted only `dedup_keys` and silently skipped
#         # deduplication entirely, changing row counts with no warning.
#         keys, notes = normalize_dedup_keys(EVENTS)
#         self.assertEqual(keys, [["DiagnosisEventKey"]])
#         self.assertEqual(len(notes), 1)
#         self.assertIn("dedup_key", notes[0])
#
#     def test_canonical_form_produces_no_note(self):
#         keys, notes = normalize_dedup_keys({"dedup_keys": [["A", "B"], ["C"]]})
#         self.assertEqual(keys, [["A", "B"], ["C"]])
#         self.assertEqual(notes, [])
#
#     def test_flat_list_is_one_key_set(self):
#         keys, _ = normalize_dedup_keys({"dedup_keys": ["A", "B"]})
#         self.assertEqual(keys, [["A", "B"]])
#
#     def test_bare_string_is_one_key_set(self):
#         keys, _ = normalize_dedup_keys({"dedup_keys": "A"})
#         self.assertEqual(keys, [["A"]])
#
#     def test_absent_means_no_dedup(self):
#         keys, notes = normalize_dedup_keys({"dest_table": "X"})
#         self.assertEqual(keys, [])
#         self.assertEqual(notes, [])
#
#     def test_both_spellings_at_once_is_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_key": ["A"], "dedup_keys": [["A"]]})
#
#     def test_empty_is_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": []})
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": [[]]})
#
#     def test_mixed_shapes_are_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": ["A", ["B"]]})
#
#
# class DedupColumnTests(unittest.TestCase):
#     def test_keys_must_name_produced_columns(self):
#         self.assertEqual(validate_dedup_columns([["DiagnosisEventKey"]], EVENTS), [])
#
#     def test_unknown_column_is_reported(self):
#         problems = validate_dedup_columns([["Nope"]], EVENTS)
#         self.assertEqual(len(problems), 1)
#         self.assertIn("Nope", problems[0])
#
#
# class DeadOptionTests(unittest.TestCase):
#     def test_retired_options_are_named(self):
#         notes = dead_options({"printout_md": True, "stop_at_for_pk_table": 500})
#         self.assertEqual(len(notes), 1)
#         self.assertIn("printout_md", notes[0])
#
#     def test_live_options_are_silent(self):
#         self.assertEqual(dead_options({"stop_at_for_pk_table": 500}), [])
#         self.assertEqual(dead_options(None), [])
#
#
# class DependencyTests(unittest.TestCase):
#     def test_finds_joined_global_temps(self):
#         self.assertEqual(joined_generated_tables(EVENTS), {"##JVM_PKTABLE2"})
#
#     def test_reports_none_for_an_independent_cohort(self):
#         self.assertEqual(joined_generated_tables(PATIENTS), set())
#
#
# class RootPkTests(unittest.TestCase):
#     def test_chained_pks_have_one_root(self):
#         # Row limits apply here only; limiting the downstream PK too would
#         # compound 500 patients x 500 events into an unrepresentative sample.
#         root = root_pk_cohort([PATIENTS, EVENTS, FACT])
#         self.assertEqual(root["dest_table"], "PKTable2")
#
#     def test_order_does_not_matter(self):
#         root = root_pk_cohort([EVENTS, FACT, PATIENTS])
#         self.assertEqual(root["dest_table"], "PKTable2")
#
#     def test_single_pk_is_its_own_root(self):
#         self.assertEqual(root_pk_cohort([PATIENTS, FACT])["dest_table"], "PKTable2")
#
#     def test_no_pk_cohorts_gives_none(self):
#         self.assertIsNone(root_pk_cohort([FACT]))
#
#     def test_fact_cohorts_are_never_roots(self):
#         self.assertEqual(len(root_pk_cohorts([PATIENTS, EVENTS, FACT])), 1)
#
#     def test_ambiguous_roots_are_an_error(self):
#         other = dict(PATIENTS, dest_table="OtherPK", name="OtherPK")
#         with self.assertRaises(NormalizationError):
#             root_pk_cohort([PATIENTS, other])
#
# === END FILE: pullmanager/tests/test_normalize.py ===
# === BEGIN FILE: pullmanager/tests/test_render.py SHA256: 9ab4a4cb4c05f41e6f4a82247ac259568716dc17ac7488e9ea0372bcf816a9c8 SIZE: 8919 ===
# """Server and local SQL rendering, checked against the real fixtures."""
#
# from __future__ import annotations
#
# import unittest
# from pathlib import Path
#
# from .. import local_sql, server_sql
# from ..local_sql import LocalRenderError
# from ..server_sql import RenderError
#
# FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
#
#
# def pk_cohort(**overrides):
#     cohort = {
#         "name": "Patients",
#         "type": "PK",
#         "dest_table": "PKTable2",
#         "columns": [{"source": "p.DurableKey", "name": "PatientDurableKey",
#                      "type": "BIGINT", "nullable": False}],
#         "filter": {"from": "PatientDim AS p", "join": [], "where": ["p._IsDeleted = 0"]},
#     }
#     cohort.update(overrides)
#     return cohort
#
#
# def doc_with(*cohorts, **extra):
#     doc = {"project_db": "PROJECTD93A5E7", "cosmos_db": "COSMOS", "cohorts": list(cohorts)}
#     doc.update(extra)
#     return doc
#
#
# class ServerRenderTests(unittest.TestCase):
#     def render(self, doc, prefix="S/pk"):
#         return server_sql.render_phase(doc, prefix)
#
#     def test_creates_and_populates_the_global_temp(self):
#         blocks, _ = self.render(doc_with(pk_cohort()))
#         sql = blocks[0].sql
#         self.assertIn("DROP TABLE IF EXISTS ##JVM_PKTable2;", sql)
#         self.assertIn("CREATE TABLE ##JVM_PKTable2", sql)
#         self.assertIn("INSERT INTO ##JVM_PKTable2", sql)
#         self.assertEqual(blocks[0].block_id, "S/pk/PKTable2")
#         self.assertEqual(blocks[0].meta["global_temp"], "##JVM_PKTable2")
#
#     def test_adds_non_null_filters(self):
#         blocks, _ = self.render(doc_with(pk_cohort()))
#         self.assertIn("AND p.DurableKey IS NOT NULL", blocks[0].sql)
#
#     def test_top_applies_only_to_the_root_pk(self):
#         # A downstream PK joins the root's temp; limiting it too would compound
#         # the restriction into an unrepresentative sample.
#         downstream = pk_cohort(
#             dest_table="PKTable",
#             columns=[{"source": "d.Key", "name": "Key", "type": "BIGINT", "nullable": False}],
#             filter={"from": "DiagnosisEventFact AS d",
#                     "join": ["INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = d.PatientDurableKey"]},
#         )
#         doc = doc_with(pk_cohort(), downstream,
#                        test_options={"smallset": True, "stop_at_for_pk_table": 500})
#         blocks, _ = self.render(doc)
#         by_dest = {b.dest_table: b.sql for b in blocks}
#         self.assertIn("TOP (500)", by_dest["PKTable2"])
#         self.assertNotIn("TOP (", by_dest["PKTable"])
#
#     def test_no_top_without_smallset(self):
#         doc = doc_with(pk_cohort(), test_options={"stop_at_for_pk_table": 500})
#         blocks, _ = self.render(doc)
#         self.assertNotIn("TOP (", blocks[0].sql)
#
#     def test_dedup_renders_and_is_visible(self):
#         # The old generator accepted only `dedup_keys` and silently emitted no
#         # deduplication at all.
#         cohort = pk_cohort(dedup_key=["PatientDurableKey"])
#         blocks, notes = self.render(doc_with(cohort))
#         self.assertIn("ROW_NUMBER() OVER (PARTITION BY [PatientDurableKey]", blocks[0].sql)
#         self.assertIn("[_dedup_rn] = 1", blocks[0].sql)
#         self.assertTrue(any("legacy" in n for n in notes))
#         self.assertTrue(any("arbitrary but stable" in n for n in notes))
#
#     def test_dedup_key_naming_a_missing_column_is_refused(self):
#         with self.assertRaises(RenderError):
#             self.render(doc_with(pk_cohort(dedup_keys=[["NoSuchColumn"]])))
#
#     def test_unsubstituted_placeholder_is_refused(self):
#         cohort = pk_cohort(filter={"from": "PatientDim AS p",
#                                    "where": ["p.StartDateKey > {{min_date_key}}"]})
#         with self.assertRaises(RenderError) as caught:
#             self.render(doc_with(cohort))
#         self.assertIn("min_date_key", str(caught.exception))
#
#     def test_duplicate_column_names_are_refused(self):
#         cohort = pk_cohort(columns=[
#             {"source": "p.A", "name": "Dup", "type": "BIGINT"},
#             {"source": "p.B", "name": "Dup", "type": "BIGINT"},
#         ])
#         with self.assertRaises(RenderError):
#             self.render(doc_with(cohort))
#
#     def test_missing_dest_table_is_refused(self):
#         with self.assertRaises(RenderError):
#             self.render(doc_with(pk_cohort(dest_table=None)))
#
#     def test_disabled_cohorts_are_skipped_with_a_note(self):
#         blocks, notes = self.render(doc_with(pk_cohort(pull_this_cycle=False)))
#         self.assertEqual(blocks, [])
#         self.assertTrue(any("pull_this_cycle" in n for n in notes))
#
#     def test_setup_captures_the_runtime_instance_name(self):
#         blocks = server_sql.render_setup(doc_with(), "S/setup")
#         self.assertIn("@@SERVERNAME", blocks[0].sql)
#         self.assertEqual(blocks[0].meta["captures"], "linked_server")
#
#
# class LocalRenderTests(unittest.TestCase):
#     LINKED = "et4003vpdsql032"
#
#     def test_shell_drops_and_creates_the_destination(self):
#         blocks = local_sql.render_setup(doc_with(pk_cohort()), [pk_cohort()], "S/setup")
#         sql = blocks[0].sql
#         self.assertIn("DROP TABLE IF EXISTS PROJECTD93A5E7.dbo.PKTable2;", sql)
#         self.assertIn("CREATE TABLE PROJECTD93A5E7.dbo.PKTable2", sql)
#
#     def test_transfer_stages_then_inserts_in_a_transaction(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertIn("DROP TABLE IF EXISTS #Local_PKTable2;", sql)
#         self.assertIn("INTO #Local_PKTable2", sql)
#         self.assertIn(f"OPENQUERY(\n    [{self.LINKED}],", sql)
#         self.assertIn("BEGIN TRANSACTION;", sql)
#         self.assertIn("COMMIT TRANSACTION;", sql)
#         # The destination is created in setup, so a run only appends.
#         self.assertNotIn("CREATE TABLE PROJECTD93A5E7", sql)
#         self.assertNotIn("DROP TABLE IF EXISTS PROJECTD93A5E7", sql)
#
#     def test_slow_pull_happens_outside_the_transaction(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertLess(sql.index("OPENQUERY"), sql.index("BEGIN TRANSACTION"))
#
#     def test_captures_both_row_counts(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertIn("'cosmos' AS [Side]", sql)
#         self.assertIn("'projects' AS [Side]", sql)
#
#     def test_measures_string_column_lengths(self):
#         cohort = pk_cohort(columns=[
#             {"source": "p.Name", "name": "Name", "type": "VARCHAR(400)"},
#             {"source": "p.Key", "name": "Key", "type": "BIGINT"},
#         ])
#         sql = local_sql.render_phase(doc_with(cohort), "S/pk", self.LINKED)[0].sql
#         self.assertIn("MAX(LEN([Name]))", sql)
#         self.assertNotIn("MAX(LEN([Key]))", sql)
#
#     def test_no_length_probe_without_string_columns(self):
#         self.assertIsNone(local_sql.render_length_probe(pk_cohort()))
#
#     def test_missing_linked_server_is_refused(self):
#         # The instance name changes every connection, so a blank one means the
#         # session identity was never captured.
#         with self.assertRaises(LocalRenderError):
#             local_sql.render_phase(doc_with(pk_cohort()), "S/pk", "")
#
#     def test_missing_project_db_is_refused(self):
#         doc = doc_with(pk_cohort())
#         doc.pop("project_db")
#         with self.assertRaises(LocalRenderError):
#             local_sql.render_phase(doc, "S/pk", self.LINKED)
#
#
# class FixtureRenderTests(unittest.TestCase):
#     """Render the real split output rather than hand-built dictionaries."""
#
#     @classmethod
#     def setUpClass(cls):
#         if not FIXTURES.is_dir():
#             raise unittest.SkipTest(f"fixtures not found at {FIXTURES}")
#         from ..yaml_io import load_yaml
#         cls.load = staticmethod(load_yaml)
#
#     def phase(self, name):
#         return self.load(FIXTURES / "sessions" / "Patients" / name)
#
#     def test_pk_phase_renders(self):
#         blocks, _ = server_sql.render_phase(self.phase("pk.yaml"), "Patients/pk")
#         self.assertEqual([b.dest_table for b in blocks], ["Patients"])
#         self.assertIn("##JVM_Patients", blocks[0].sql)
#
#     def test_run_phase_renders_both_sides(self):
#         doc = self.phase("runs/run.yaml")
#         server, _ = server_sql.render_phase(doc, "Patients/run")
#         local = local_sql.render_phase(doc, "Patients/run", "et4003vpdsql032")
#         self.assertEqual([b.dest_table for b in server], [b.dest_table for b in local])
#         self.assertTrue(all(b.side == "server" for b in server))
#         self.assertTrue(all(b.side == "local" for b in local))
#
#     def test_block_ids_are_unique_and_addressable(self):
#         doc = self.phase("runs/run.yaml")
#         server, _ = server_sql.render_phase(doc, "Patients/run")
#         ids = [b.block_id for b in server]
#         self.assertEqual(len(ids), len(set(ids)))
#         self.assertTrue(all(b.dest_table in b.block_id for b in server))
#
# === END FILE: pullmanager/tests/test_render.py ===
# === BEGIN FILE: pullmanager/tests/test_sql.py SHA256: 70f3bfde2d04c0ab5dc3df2d063182f2684f908b04446d708049f1ee40c1cc35 SIZE: 5285 ===
# """SQL construction, with the WHERE builder as the main risk."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..sql import (
#     ddl_body,
#     from_entries,
#     is_nullable,
#     non_null_predicates,
#     quote_literal,
#     render_source_clause,
#     render_where,
#     where_entries,
# )
#
#
# class WhereBuilderTests(unittest.TestCase):
#     def render(self, predicates):
#         return [line.strip() for line in render_where(predicates).splitlines()]
#
#     def test_first_predicate_takes_no_and(self):
#         self.assertEqual(self.render(["a = 1"]), ["a = 1"])
#
#     def test_subsequent_predicates_take_and(self):
#         self.assertEqual(self.render(["a = 1", "b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_preserves_a_grouped_code_list(self):
#         # The K50/K51 filter, authored as separate list entries. Inserting AND
#         # before the OR lines or the closing paren would be invalid SQL.
#         self.assertEqual(
#             self.render([
#                 "dt.Type IN ('ICD-10-CM')",
#                 "( dt.Value LIKE 'K50.%'",
#                 "  OR dt.Value = 'K50'",
#                 "  OR dt.Value LIKE 'K51.%'",
#                 "  OR dt.Value = 'K51'",
#                 ")",
#             ]),
#             [
#                 "dt.Type IN ('ICD-10-CM')",
#                 "AND ( dt.Value LIKE 'K50.%'",
#                 "OR dt.Value = 'K50'",
#                 "OR dt.Value LIKE 'K51.%'",
#                 "OR dt.Value = 'K51'",
#                 ")",
#             ],
#         )
#
#     def test_does_not_double_an_explicit_and(self):
#         self.assertEqual(self.render(["a = 1", "AND b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_leading_or_is_left_alone(self):
#         self.assertEqual(self.render(["a = 1", "OR b = 2"]), ["a = 1", "OR b = 2"])
#
#     def test_comments_are_not_prefixed(self):
#         self.assertEqual(
#             self.render(["a = 1", "-- restrict to ICD-10", "b = 2"]),
#             ["a = 1", "-- restrict to ICD-10", "AND b = 2"],
#         )
#
#     def test_a_group_opening_the_clause_takes_no_and(self):
#         self.assertEqual(self.render(["( a = 1", "OR b = 2", ")"]), ["( a = 1", "OR b = 2", ")"])
#
#     def test_blank_entries_are_dropped(self):
#         self.assertEqual(self.render(["a = 1", "", "   ", "b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_multiline_entries_are_split(self):
#         self.assertEqual(
#             self.render(["a = 1\nAND b = 2", "c = 3"]),
#             ["a = 1", "AND b = 2", "AND c = 3"],
#         )
#
#     def test_case_insensitive_continuations(self):
#         self.assertEqual(self.render(["a = 1", "and b = 2", "or c = 3"]),
#                          ["a = 1", "and b = 2", "or c = 3"])
#
#
# class NonNullTests(unittest.TestCase):
#     def test_adds_a_predicate_for_each_non_nullable_column(self):
#         columns = [
#             {"source": "p.A", "name": "A", "nullable": False},
#             {"source": "p.B", "name": "B", "nullable": True},
#             {"source": "p.C", "name": "C"},  # absent means nullable
#         ]
#         self.assertEqual(non_null_predicates(columns), ["p.A IS NOT NULL"])
#
#     def test_accepts_boolean_spellings(self):
#         self.assertEqual(
#             non_null_predicates([{"source": "p.A", "name": "A", "nullable": "no"}]),
#             ["p.A IS NOT NULL"],
#         )
#
#     def test_absent_nullable_defaults_to_nullable(self):
#         self.assertTrue(is_nullable({"name": "A"}))
#
#
# class FilterShapeTests(unittest.TestCase):
#     def test_from_accepts_a_string_or_a_list(self):
#         self.assertEqual(from_entries({"from": "PatientDim AS p"}), ["PatientDim AS p"])
#         self.assertEqual(from_entries({"from": ["PatientDim AS p"]}), ["PatientDim AS p"])
#         self.assertEqual(from_entries({}), [])
#
#     def test_where_accepts_a_string_or_a_list(self):
#         self.assertEqual(where_entries({"where": "a = 1"}), ["a = 1"])
#         self.assertEqual(where_entries({"where": ["a = 1", "b = 2"]}), ["a = 1", "b = 2"])
#
#     def test_source_clause_qualifies_from_and_joins(self):
#         sql = render_source_clause({
#             "from": ["DiagnosisEventFact AS def"],
#             "join": [
#                 "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#                 "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
#             ],
#         })
#         self.assertIn("FROM dbo.DiagnosisEventFact AS def", sql)
#         self.assertIn("INNER JOIN dbo.DiagnosisTerminologyDim", sql)
#         # A generated temp lives in tempdb and must stay unqualified.
#         self.assertIn("INNER JOIN ##JVM_PKTable2 AS p", sql)
#
#
# class LiteralTests(unittest.TestCase):
#     def test_escapes_embedded_quotes(self):
#         self.assertEqual(quote_literal("HUMIRA(CF) CROHN'S STARTER"), "'HUMIRA(CF) CROHN''S STARTER'")
#
#     def test_renders_scalars(self):
#         self.assertEqual(quote_literal(None), "NULL")
#         self.assertEqual(quote_literal(42), "42")
#         self.assertEqual(quote_literal(True), "1")
#
#
# class DdlTests(unittest.TestCase):
#     def test_renders_nullability(self):
#         body = ddl_body([
#             {"name": "A", "type": "BIGINT", "nullable": False},
#             {"name": "B", "type": "VARCHAR(400)", "nullable": True},
#         ])
#         self.assertIn("[A] BIGINT NOT NULL", body)
#         self.assertIn("[B] VARCHAR(400) NULL", body)
#
# === END FILE: pullmanager/tests/test_sql.py ===
# === BEGIN FILE: pullmanager/yaml_io.py SHA256: dca04d852f7873c8abcac4d0e9f0f0e1883c96117a9bcacdf7766fbae4255c18 SIZE: 1844 ===
# """YAML load/dump for Pullmanager.
#
# Mirrors the backend selection in scripts/makeYaml.py so manifests round-trip
# through the same representation YAML Manager wrote them with.
# """
#
# from __future__ import annotations
#
# from pathlib import Path
# from typing import Any
#
#
# def _backend():
#     try:
#         from ruamel.yaml import YAML  # type: ignore
#
#         yaml = YAML()
#         yaml.preserve_quotes = True
#         yaml.default_flow_style = False
#         return "ruamel", yaml
#     except Exception:
#         pass
#
#     try:
#         import yaml  # type: ignore
#
#         return "pyyaml", yaml
#     except Exception:
#         return "none", None
#
#
# def _plain(value: Any) -> Any:
#     if isinstance(value, dict):
#         return {str(k): _plain(v) for k, v in value.items()}
#     if isinstance(value, list):
#         return [_plain(v) for v in value]
#     return value
#
#
# def load_yaml(path: str | Path) -> Any:
#     path = Path(path)
#     backend, mod = _backend()
#     if backend == "ruamel":
#         with path.open("r", encoding="utf-8") as handle:
#             return _plain(mod.load(handle))
#     if backend == "pyyaml":
#         with path.open("r", encoding="utf-8") as handle:
#             return mod.safe_load(handle)
#     raise RuntimeError("No YAML backend available. Install ruamel.yaml or pyyaml.")
#
#
# def dump_yaml(data: Any, path: str | Path) -> None:
#     path = Path(path)
#     path.parent.mkdir(parents=True, exist_ok=True)
#     backend, mod = _backend()
#     if backend == "ruamel":
#         with path.open("w", encoding="utf-8") as handle:
#             mod.dump(data, handle)
#         return
#     if backend == "pyyaml":
#         with path.open("w", encoding="utf-8") as handle:
#             mod.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
#         return
#     raise RuntimeError("No YAML backend available. Install ruamel.yaml or pyyaml.")
#
# === END FILE: pullmanager/yaml_io.py ===
