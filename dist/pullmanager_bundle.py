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
  "content_id": "87bdb9b38d36c9c20776fba08703e497da9b5c74c05ac7f46a587660214c5088",
  "file_count": 15,
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
      "sha256": "3cb1797c796b584993ddab8ea2b56b3e7a80965fb7ec633bf2dfce4f327e1deb",
      "size": 2303
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
# === BEGIN FILE: pullmanager/cli.py SHA256: 3cb1797c796b584993ddab8ea2b56b3e7a80965fb7ec633bf2dfce4f327e1deb SIZE: 2303 ===
# """Command line entry point.
#
# Phase 2 scope: load and inspect a manifest. Execution arrives in Phase 5.
# """
#
# from __future__ import annotations
#
# import argparse
# import sys
#
# from . import __version__
# from .manifest import Manifest, ManifestError
#
#
# def summarize(manifest: Manifest) -> None:
#     project = manifest.project
#     print(f"Project:  {project.get('name')}  ({project.get('project_db')})")
#     print(f"Manifest: {manifest.path}")
#     print(f"Sessions: {len(manifest.sessions)}")
#     print()
#     for session in manifest.sessions:
#         pk_source = next(
#             (phase.pk_source for phase in session.phases if phase.pk_source), None
#         )
#         kind = pk_source.get("kind") if pk_source else "none"
#         print(f"  {session.session_id}  [{session.status}]  pk={session.pk_table} ({kind})")
#         for phase in session.phases:
#             print(f"    phase {phase.name:<15} {phase.status:<8} {phase.yaml}")
#         for run in session.runs:
#             batch = run.batch.get("name") if run.batch else "-"
#             print(f"    run   {batch:<15} {run.status:<8} {run.yaml}")
#         print()
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
#         "--tdd",
#         nargs="?",
#         const="__all__",
#         metavar="GROUP",
#         help="Run the embedded test suite, optionally limited to one module (e.g. manifest).",
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
#     try:
#         manifest = Manifest.load(args.manifest)
#     except ManifestError as exc:
#         print(f"ERROR {exc}", file=sys.stderr)
#         return 1
#
#     summarize(manifest)
#     return 0
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager/cli.py ===
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
