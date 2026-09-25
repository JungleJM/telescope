#!/usr/bin/env python3
"""Self-extraction logic for a Pullmanager bundle.

This module is inlined verbatim as the prelude of every generated bundle, so it
must stay dependency-free (standard library only) and must not import from the
`pullmanager` package it carries.

Bundle layout:

    <this prelude>
    BUNDLE_MANIFEST_JSON = r'''{"files": [...], "content_id": "..."}'''
    # === BEGIN FILE: pullmanager/models.py SHA256: <64 hex> SIZE: <bytes> ===
    # <source line>
    # === END FILE: pullmanager/models.py ===

Payload lines are comment-prefixed, which keeps the bundle valid, readable
Python and makes marker forgery impossible: a source line that itself looks
like a marker encodes to `# # === ...`, which no longer matches the anchored
marker pattern.
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

# Where --extract puts the runtime when no folder is named, and the launcher it
# writes beside that folder so `python pullmanager.py` works from there (D63).
DEFAULT_TARGET = "pullmanager_runtime"
LAUNCHER_NAME = "pullmanager.py"
LAUNCHER_TEMPLATE = '''#!/usr/bin/env python3
"""Runs Pullmanager from {folder}, the folder bundle.py extracted beside this file.

Written by `python bundle.py --extract`, and rewritten by every extraction, so
it is not for editing. With no arguments it opens the launcher window; anything
else goes to Pullmanager as typed (--dry-run, --execute, --tdd, a manifest).
"""

import runpy
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parent / {folder!r} / "pullmanager.py"
if not ENTRY.is_file():
    sys.exit(f"No Pullmanager at {{ENTRY}}. Extract it again: python bundle.py --extract {folder}")
sys.argv[0] = str(ENTRY)
runpy.run_path(str(ENTRY), run_name="__main__")
'''

# What a re-extraction does to a file that is already there. Everything
# bundled is managed and gets updated; a locally modified copy is set aside
# rather than overwritten.
POLICY_REPLACE = "replace"


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


def previous_extraction_hashes(target: Path) -> dict[str, str]:
    """Path to SHA-256 of every file the previous extraction wrote, or {}."""
    try:
        recorded = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        return {
            str(entry["path"]): str(entry["sha256"])
            for entry in recorded.get("files", [])
            if isinstance(entry, dict) and "path" in entry and "sha256" in entry
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


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

    previous_hashes = previous_extraction_hashes(target)
    shipped_paths = {section["path"] for section in sections}
    preserved: list[str] = []
    dropped_kept: list[str] = []
    dropped: list[str] = []
    carried: list[str] = []

    def edited_here(rel: str, current: bytes, shipped: bytes | None) -> bool:
        """Was this file changed on this machine since it was extracted?

        Judged against the hash the previous extraction recorded, so a file
        that merely changed between releases is not mistaken for an edit.
        With no record (a first or forced extraction), any difference from
        what is about to be shipped counts, to be safe.
        """
        recorded = previous_hashes.get(rel)
        if recorded is not None:
            return hashlib.sha256(current).hexdigest() != recorded
        return shipped is None or current != shipped

    try:
        for section in sections:
            rel = section["path"]
            out_path = staging / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            existing = target / rel
            shipped = section["content"].encode("utf-8")

            if existing.is_file():
                current = existing.read_bytes()
                if current != shipped and edited_here(rel, current, shipped):
                    # Replaced, but an edit made here is not simply destroyed.
                    aside = staging / (rel + ".local")
                    aside.parent.mkdir(parents=True, exist_ok=True)
                    aside.write_bytes(current)
                    preserved.append(rel)

            # Explicit bytes so Windows does not translate newlines and break hashes.
            out_path.write_bytes(shipped)

        # A file the previous bundle shipped and this one does not: removed,
        # unless it was edited here, in which case it is kept aside like a
        # replaced file. Dropping a file must not be a way to lose work.
        for rel in sorted(set(previous_hashes) - shipped_paths):
            existing = target / rel
            if not existing.is_file():
                continue
            if edited_here(rel, existing.read_bytes(), None):
                aside = staging / (rel + ".local")
                aside.parent.mkdir(parents=True, exist_ok=True)
                aside.write_bytes(existing.read_bytes())
                dropped_kept.append(rel)
            else:
                dropped.append(rel)

        # Copies set aside by earlier updates stay until the user deletes them.
        if target.is_dir():
            for old_local in sorted(target.rglob("*.local")):
                rel = old_local.relative_to(target).as_posix()
                destination = staging / rel
                if destination.exists():
                    continue  # a newer copy of the same file was just set aside
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_local, destination)
                carried.append(rel)

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

    for rel in preserved:
        print(f"replaced   {rel}  (your previous copy saved as {rel}.local)")
    for rel in dropped_kept:
        print(f"no longer shipped   {rel}  (your edited copy kept as {rel}.local)")
    for rel in dropped:
        print(f"no longer shipped   {rel}  (removed; it had not been edited)")
    for rel in carried:
        print(f"kept       {rel}  (set aside by an earlier update; delete it when done)")
    return [section["path"] for section in sections]


def write_launcher(target: Path) -> Path:
    """Write `pullmanager.py` beside the extracted folder, pointing into it (D63).

    Always rewritten, whatever is there: it is a generated shortcut, and the
    folder it points at may have been extracted under another name this time.
    """
    target = Path(target).resolve()
    launcher = target.parent / LAUNCHER_NAME
    launcher.write_bytes(LAUNCHER_TEMPLATE.format(folder=target.name).encode("utf-8"))
    return launcher


def bundle_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Self-extracting Pullmanager bundle.",
    )
    parser.add_argument("--verify-bundle", action="store_true", help="Verify this bundle and exit.")
    parser.add_argument("--list", action="store_true", help="List the files this bundle carries.")
    parser.add_argument(
        "--extract",
        metavar="DIR",
        nargs="?",
        const=DEFAULT_TARGET,
        help=f"Verify, then extract into DIR (default {DEFAULT_TARGET}), and write "
        f"{LAUNCHER_NAME} beside it.",
    )
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
            launcher = write_launcher(Path(args.extract))
            print(f"Wrote {launcher}: `python {LAUNCHER_NAME}` there opens the launcher.")
    except BundleError as exc:
        print(f"BUNDLE ERROR: {exc}", file=sys.stderr)
        return 2

    return 0
