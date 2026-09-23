#!/usr/bin/env python3
"""Build a single-file, self-extracting Pullmanager bundle.

The VM cannot pull from git, so development happens as normal modules under
`scripts/pullmanager_src/` and ships as one generated file:

    python3 scripts/bundle_pullmanager.py            # build dist/pullmanager_bundle.py
    python3 scripts/bundle_pullmanager.py --tdd      # run bundle/extractor tests

On the VM:

    python pullmanager_bundle.py --verify-bundle
    python pullmanager_bundle.py --extract ./pullmanager_runtime
    python ./pullmanager_runtime/pullmanager.py split/pullmanifest.yaml

Bundles are deterministic: the same sources always produce byte-identical
output, so a rebuild with no source changes leaves git clean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bundle_extractor import (  # noqa: E402
    BundleError,
    compute_content_id,
    read_bundle,
    safe_relpath,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
SOURCE_ROOT = SCRIPTS_DIR / "pullmanager_src"
EXTRACTOR_PATH = SCRIPTS_DIR / "bundle_extractor.py"
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "pullmanager_bundle.py"

# The VM needs more than the runtime. The split step runs there, so YAML
# Manager and the data it reads travel too. Published paths are chosen so
# makeYaml's own default paths resolve inside the extracted tree without it
# knowing it was bundled: it expects <root>/scripts/makeYaml.py alongside
# <root>/YAMLs/.
COMPANION_FILES: tuple[tuple[Path, str], ...] = (
    (REPO_ROOT / "scripts" / "makeYaml.py", "scripts/makeYaml.py"),
    (REPO_ROOT / "YAMLs" / "recipes.yaml", "YAMLs/recipes.yaml"),
    (REPO_ROOT / "YAMLs" / "datadictionary.yaml", "YAMLs/datadictionary.yaml"),
    (REPO_ROOT / "YAMLs" / "template.yaml", "YAMLs/template.yaml"),
    (REPO_ROOT / ".env.example", ".env.example"),
)

BUNDLE_FORMAT_VERSION = 1
FUTURE_IMPORT = "from __future__ import annotations"

BUNDLE_HEADER = '''#!/usr/bin/env python3
"""Pullmanager bundle - GENERATED FILE, DO NOT EDIT.

Built by scripts/bundle_pullmanager.py from scripts/pullmanager_src/.
To change anything here, edit the source module and rebuild the bundle.

    python pullmanager_bundle.py --verify-bundle
    python pullmanager_bundle.py --list
    python pullmanager_bundle.py --extract ./pullmanager_runtime
"""
'''

BUNDLE_FOOTER = '''

if __name__ == "__main__":
    raise SystemExit(bundle_main())
'''


def source_files(root: Path = SOURCE_ROOT) -> list[Path]:
    """Every .py file that should ship, sorted for deterministic output."""
    files = [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def read_source(path: Path) -> str:
    text = path.read_bytes().decode("utf-8")
    if "\r" in text:
        raise BundleError(
            f"{path} contains carriage returns. Normalize it to LF before bundling."
        )
    return text


def encode_payload_lines(text: str) -> list[str]:
    return ["# " + line if line else "#" for line in text.split("\n")]


def bundled_files(root: Path = SOURCE_ROOT) -> list[tuple[Path, str]]:
    """Every file the bundle carries, as (source, published path)."""
    files = source_files(root)
    if not files:
        raise BundleError(f"No Python sources found under {root}")
    pairs = [(path, path.relative_to(root).as_posix()) for path in files]
    for source, published in COMPANION_FILES:
        if not source.is_file():
            raise BundleError(f"Companion file missing: {source}")
        pairs.append((source, published))
    seen: set[str] = set()
    for _, published in pairs:
        if published in seen:
            raise BundleError(f"Two files would publish to {published}")
        seen.add(published)
    return sorted(pairs, key=lambda pair: pair[1])


def build_sections(root: Path = SOURCE_ROOT) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    lines: list[str] = []
    for path, published in bundled_files(root):
        rel = safe_relpath(published)
        text = read_source(path)
        raw = text.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        entries.append({"path": rel, "sha256": sha, "size": len(raw)})
        lines.append(f"# === BEGIN FILE: {rel} SHA256: {sha} SIZE: {len(raw)} ===")
        lines.extend(encode_payload_lines(text))
        lines.append(f"# === END FILE: {rel} ===")
    return entries, lines


def extractor_prelude() -> str:
    """The extractor source, minus its shebang and module docstring."""
    text = read_source(EXTRACTOR_PATH)
    marker = text.find(FUTURE_IMPORT)
    if marker == -1:
        raise BundleError(
            f"{EXTRACTOR_PATH} must contain `{FUTURE_IMPORT}` so it can be inlined."
        )
    return text[marker:]


def render_bundle(root: Path = SOURCE_ROOT) -> str:
    entries, payload_lines = build_sections(root)
    manifest = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "content_id": compute_content_id(entries),
        "file_count": len(entries),
        "files": entries,
    }
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
    if "'''" in manifest_json:
        raise BundleError("Bundle manifest JSON contains a triple quote; cannot embed safely.")

    parts = [
        BUNDLE_HEADER,
        extractor_prelude().rstrip("\n"),
        "\n\n",
        f"BUNDLE_MANIFEST_JSON = r'''{manifest_json}'''\n",
        BUNDLE_FOOTER,
        "\n",
        "\n".join(payload_lines),
        "\n",
    ]
    return "".join(parts)


def build(output: Path = DEFAULT_OUTPUT, root: Path = SOURCE_ROOT) -> tuple[Path, dict]:
    text = render_bundle(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    _, manifest = read_bundle(output)
    return output, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bundle_pullmanager.py",
        description="Build the single-file Pullmanager bundle.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="Bundle output path.")
    parser.add_argument("--src", default=str(SOURCE_ROOT), help="Source tree to bundle.")
    parser.add_argument("--verify", metavar="BUNDLE", help="Verify an existing bundle and exit.")
    parser.add_argument(
        "--tdd",
        nargs="?",
        const="__all__",
        metavar="GROUP",
        help="Run the bundler test suite, optionally limited to one group.",
    )
    args = parser.parse_args(argv)

    if args.tdd is not None:
        from bundle_tests import run as run_tdd

        return run_tdd(None if args.tdd == "__all__" else args.tdd)

    try:
        if args.verify:
            sections, manifest = read_bundle(Path(args.verify))
            print(f"OK  {len(sections)} files verified")
            print(f"content_id: {manifest['content_id']}")
            return 0

        output, manifest = build(Path(args.out), Path(args.src))
        size_kb = output.stat().st_size / 1024
        print(f"Wrote {output}  ({manifest['file_count']} files, {size_kb:.1f} KiB)")
        print(f"content_id: {manifest['content_id']}")
    except BundleError as exc:
        print(f"BUNDLE ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
