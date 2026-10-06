#!/usr/bin/env python3
"""Build a single-file, self-extracting Pullmanager bundle.

The VM cannot pull from git, so development happens as normal modules under
`scripts/pullmanager_src/` and ships as one generated file:

    python3 makebundle.py                            # dist/bundle.py, carrying the queue (D122)
    python3 makebundle.py yaml=IBD_Ancestry,Celiac   # and those transfer YAMLs too
    python3 makebundle.py --yamls-only               # dist/yamls_to_transfer.py: the YAMLs alone
    python3 scripts/bundle_pullmanager.py --tdd      # run bundle/extractor tests

On the VM:

    python bundle.py          # verify, show the content_id, y to extract (D64)
    python scope.py           # the app (D123)

Bundles are deterministic: the same sources always produce byte-identical
output, so a rebuild with no source changes leaves git clean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bundle_extractor import (  # noqa: E402
    ROOT_POLICY,
    ROOT_PREFIX,
    BundleError,
    compute_content_id,
    read_bundle,
    safe_relpath,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
SOURCE_ROOT = SCRIPTS_DIR / "pullmanager_src"
EXTRACTOR_PATH = SCRIPTS_DIR / "bundle_extractor.py"
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "bundle.py"
# The queued pulls' transfer YAMLs alone, for when the software need not change
# (D122); extracting it leaves the runtime as it is.
YAMLS_ONLY_OUTPUT = REPO_ROOT / "dist" / "yamls_to_transfer.py"
# What D106 built beside bundle.py; bundle.py carries the queue now (D122).
RETIRED_OUTPUT_NAME = "bundle_with_yamls.py"
# Each build's content_id, one line per bundle file (D106).
CONTENT_ID_NAME = "content_id.txt"

# The VM needs more than the runtime. The split step runs there, so makeYaml
# and the data dictionary it validates against travel too. Published paths are
# chosen so makeYaml's own default paths resolve inside the extracted tree
# without it knowing it was bundled: it expects <root>/scripts/makeYaml.py
# alongside <root>/YAMLs/.
# Everything bundled is managed, and a re-extraction updates it. A locally
# modified copy is kept aside as <name>.local first, so an edit made on the VM
# is never simply destroyed -- which also suits hand-patching a file there and
# copying it back.
#
# The browser UI stays on the Mac (D49): the VM cannot open it, and the app's
# Author half adjusts blueprints there (D94). recipes.yaml travels, so Author on
# the VM can add recipes (D187); so do the template a new draft starts from and
# the VM's package list, and HowThisRepoWorks.md, the walkthrough for a
# developer reading the code on the VM (D199). Nothing else the user authors is
# bundled.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import makeYaml  # noqa: E402  the core files' places (D111)
import bundle_scrub  # noqa: E402  what the bundle says about itself (D200, D201)

COMPANION_FILES: tuple[tuple[Path, str, str], ...] = (
    (REPO_ROOT / "scripts" / "makeYaml.py", "scripts/makeYaml.py", "replace"),
    # The app's Author half (D93): the model and its tkinter view, beside the
    # makeYaml they use, where the launcher finds them.
    (REPO_ROOT / "scripts" / "yamlmanager_model.py", "scripts/yamlmanager_model.py", "replace"),
    (REPO_ROOT / "scripts" / "yamlmanager_tk.py", "scripts/yamlmanager_tk.py", "replace"),
    # Authored on the Mac and flowing one way, so the shipped copy wins. Read
    # from where datascope.json says; published where makeYaml's default
    # finds it in the extracted tree (D111).
    (makeYaml.core_path("datadictionary"), "reference/datadictionary.yaml", "replace"),
    # For now (D187): recipes, so Author on the VM lists and adds them.
    (makeYaml.core_path("recipes"), "reference/recipes.yaml", "replace"),
    # D199: the template a new draft starts from, the VM's package list, and
    # the walkthrough of how a template becomes SQL.
    (makeYaml.core_path("template"), "reference/template.yaml", "replace"),
    (makeYaml.core_path("vm_plugins"), "reference/DSVM Plugins.yaml", "replace"),
    (REPO_ROOT / "HowThisRepoWorks.md", "HowThisRepoWorks.md", "replace"),
)
# .env is deliberately not shipped. Both hosts are DNS aliases with defaults
# and the database names come from the manifest, so there is nothing to
# configure; shipping an example would only suggest otherwise.

# Transfer YAMLs a bundle can carry (`yaml=`), from the repository root,
# where `makeYaml --export-transfer` writes them. Each is extracted beside
# pullmanager.py on the VM, ready to run.
TRANSFER_SUFFIX = "_transfer.yaml"
# What makeYaml exports now (D162); placed in YAMLs/temp/ on the VM.
BLUEPRINT_SUFFIX = "_blueprint.yaml"
PULL_SUFFIXES = (BLUEPRINT_SUFFIX, TRANSFER_SUFFIX)

# The bundle queue (D91): temps YAML Manager's Save & Refresh queued, one file
# name per line. `makebundle.py queue` exports each one's transfer YAML to the
# repository root and carries it, as `yaml=` does.
TEMP_DIR = REPO_ROOT / "YAMLs" / "temp"
QUEUE_NAME = "bundle_queue.txt"
QUEUE_HEADER = (
    "# Temps queued for the bundle, one per line (D91). YAML Manager's Save & Refresh\n"
    "# adds to it and its Builder > Exports edits it; python3 makebundle.py carries it (D122).\n"
)
RECIPES_PATH = makeYaml.core_path("recipes")

BUNDLE_FORMAT_VERSION = 1
FUTURE_IMPORT = "from __future__ import annotations"

BUNDLE_HEADER = '''#!/usr/bin/env python3
"""Pullmanager bundle - GENERATED FILE, DO NOT EDIT.

Built by scripts/bundle_pullmanager.py from scripts/pullmanager_src/.
To change anything here, edit the source module and rebuild the bundle.

    python bundle.py                   # verify, show the content_id, y to extract
    python scope.py                    # then: the app
    python bundle.py --verify-bundle   # or step by step: --list, --extract [DIR]
"""
'''

BUNDLE_FOOTER = '''

if __name__ == "__main__":
    raise SystemExit(bundle_main())
'''


# Files Artifacts copies into every pull's folder, kept where the user edits
# them (D89): every file in it ships, not only Python.
STOCK_DIR = "stock"

# For now (D200), the bundle says nothing of screenshots or of transcribing
# them: the transcription viewer stays on the Mac, the dictionary's copy drops
# its comments and rewords what its descriptions say about screenshots, and a
# build that would still carry either word stops, naming each line. To ship
# the viewer again: remove it from HELD_BACK, set NO_SCREENSHOT_MENTIONS to
# False, and revert the commit that removed Multi-column view (D200).
HELD_BACK = ("utils/client/transcription_viewer.py",)
NO_SCREENSHOT_MENTIONS = True
# D201: the bundle's prose says nothing of bundling, extraction or the Mac;
# bundle_scrub rewords what a VM user sees and drops the rest. False ships the
# Mac's text as it is.
VM_ONLY_PROSE = True
SCREENSHOT_WORDS = re.compile(r"transcri|screenshot", re.IGNORECASE)
# Across line breaks, since descriptions are folded YAML.
SCREENSHOT_REWORDS = (
    (r"\(the\s+rest\s+is\s+cut\s+off\s+in\s+the\s+screenshot\)", "(the rest is not recorded)"),
    (r"description\s+cut\s+off\s+in\s+the\s+screenshot", "description not recorded"),
    (r"Overview\s+tab\s+not\s+screenshotted;\s+not\s+yet\s+transcribed\.", "Overview tab not yet recorded."),
    (r"not\s+yet\s+transcribed", "not yet recorded"),
)


def source_files(root: Path = SOURCE_ROOT) -> list[Path]:
    """Every .py file that should ship, and every stock file, sorted for
    deterministic output."""
    files = [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
        and path.relative_to(root).as_posix() not in HELD_BACK
    ]
    stock = root / STOCK_DIR
    if stock.is_dir():
        files += [path for path in stock.iterdir()
                  if path.is_file() and path.suffix != ".py" and not path.name.startswith(".")]
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


def transfer_names(tokens: list[str]) -> list[str]:
    """`yaml=IBD_Ancestry,Celiac.yaml` as the names it lists.

    The shell splits `yaml=IBD_Ancestry, Celiac.yaml` at the space, so the
    words after `yaml=` belong to it too, until the next option.
    """
    names: list[str] = []
    collecting = False
    for token in tokens:
        if token.startswith("yaml=") or token.startswith("--yaml="):
            collecting = True
            token = token.split("=", 1)[1]
        elif token.startswith("-"):
            collecting = False
            continue
        elif not collecting:
            raise BundleError(f"Unexpected argument {token!r}. Transfer YAMLs are given as yaml=NAME,NAME.")
        names += [part.strip() for part in token.split(",") if part.strip()]
    return names


def find_transfer(name: str, folder: Path = REPO_ROOT) -> Path:
    """`IBD_Ancestry`, `IBD_Ancestry.yaml` or the full file name, as
    `<folder>/IBD_Ancestry_blueprint.yaml`, else its older name
    `IBD_Ancestry_transfer.yaml` (D162). Nothing else is looked for."""
    stem = name.strip()
    for ending in (".yaml", ".yml"):
        if stem.lower().endswith(ending):
            stem = stem[: -len(ending)]
    for suffix in ("_blueprint", "_transfer"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # Listed rather than looked up, so the file's own spelling comes back.
    available = sorted(path for suffix in PULL_SUFFIXES for path in folder.glob(f"*{suffix}"))
    for suffix in PULL_SUFFIXES:
        wanted = f"{stem}{suffix}"
        for path in available:
            if path.name == wanted:
                return path
        for path in available:
            if path.name.lower() == wanted.lower():
                return path
    there = ", ".join(path.name for path in available) or "none"
    raise BundleError(
        f"No {stem}{BLUEPRINT_SUFFIX} in {folder}. Blueprints there: {there}. Export it first: "
        f"python3 scripts/makeYaml.py --template <template> --export-transfer"
    )


def shipped_text(path: Path, policy: str, published: str | None = None) -> str:
    """A file as the bundle carries it. A blueprint is placed in YAMLs/temp/
    on the VM (D162), two folders below the one it was exported to, so each
    relative `file_loc` gains `../../` to reach the same file (D103)."""
    text = read_source(path)
    if NO_SCREENSHOT_MENTIONS:
        text = without_screenshots(path, text)
    if VM_ONLY_PROSE and policy != ROOT_POLICY:
        try:
            text = bundle_scrub.scrub(published or path.name, text)
        except bundle_scrub.ScrubError as exc:
            raise BundleError(str(exc)) from exc
    if policy != ROOT_POLICY or not path.name.endswith(BLUEPRINT_SUFFIX):
        return text
    import makeYaml

    doc = makeYaml.load_yaml(path)
    uploads = doc.get("upload_cohorts") if isinstance(doc, dict) else None
    relative = [u for u in uploads or [] if isinstance(u, dict) and u.get("file_loc")
                and not Path(str(u["file_loc"])).is_absolute()]
    if not relative:
        return text
    for upload in relative:
        upload["file_loc"] = makeYaml.repoint_file_loc(str(upload["file_loc"]), path.parent,
                                                       path.parent / "YAMLs" / "temp")
    return makeYaml.dump_yaml_text(doc)


def without_screenshots(path: Path, text: str) -> str:
    """The dictionary without its comments, and any YAML's descriptions
    without what they say about screenshots (D200)."""
    if path.suffix not in (".yaml", ".yml"):
        return text
    if path.resolve() == makeYaml.core_path("datadictionary").resolve():
        text = "".join(line for line in text.splitlines(keepends=True)
                       if not line.lstrip().startswith("#"))
    for pattern, words in SCREENSHOT_REWORDS:
        text = re.sub(pattern, words, text)
    return text


def screenshot_mentions(published: str, text: str) -> list[str]:
    """Each line that still names screenshots or transcribing (D200)."""
    return [f"{published}:{number}: {line.strip()[:100]}"
            for number, line in enumerate(text.splitlines(), 1) if SCREENSHOT_WORDS.search(line)]


def read_queue(folder: Path = TEMP_DIR) -> list[str]:
    """The queued temps' file names, in the order queued, each once."""
    try:
        lines = (folder / QUEUE_NAME).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    names: list[str] = []
    for line in lines:
        name = line.strip()
        if name and not name.startswith("#") and name not in names:
            names.append(name)
    return names


def write_queue(names: list[str], folder: Path = TEMP_DIR) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / QUEUE_NAME).write_text(QUEUE_HEADER + "".join(f"{n}\n" for n in names), encoding="utf-8")


def queue_add(name: str, folder: Path = TEMP_DIR) -> bool:
    """Queue a temp by file name; False if it was already queued."""
    names = read_queue(folder)
    if name in names:
        return False
    write_queue(names + [name], folder)
    return True


def queue_remove(name: str, folder: Path = TEMP_DIR) -> bool:
    names = read_queue(folder)
    if name not in names:
        return False
    write_queue([n for n in names if n != name], folder)
    return True


def export_queue(folder: Path = TEMP_DIR, out_dir: Path | None = None) -> list[tuple[Path, Path]]:
    """Each queued temp's transfer YAML, written (to the repository root unless
    `out_dir`), as (temp, transfer) pairs. Any temp that does not validate
    stops the build, with every one that failed named."""
    import makeYaml

    names = read_queue(folder)
    if not names:
        raise BundleError(
            f"The bundle queue ({folder / QUEUE_NAME}) is empty. Save & Refresh a temp in "
            "YAML Manager, or add one in its Builder's Exports."
        )
    exported: list[tuple[Path, Path]] = []
    problems: list[str] = []
    for name in names:
        temp = folder / name
        if not temp.is_file():
            problems.append(f"{name}: not in {folder}. Remove it from the queue (Builder > Exports) "
                            "or put the file back.")
            continue
        result = makeYaml.build_transfer(
            template_path=temp, recipes_path=RECIPES_PATH, write=True, output_dir=out_dir
        )
        if result.errors:
            first = result.errors[0]
            problems.append(f"{name}: {len(result.errors)} error(s), the first [{first.code}] "
                            f"{first.message} Open it in YAML Manager to see them all.")
            continue
        exported.append((temp, Path(result.output_path)))
    if problems:
        raise BundleError("Queued temps that cannot be exported, so nothing was built:\n  "
                          + "\n  ".join(problems))
    return exported


def bundled_files(
    root: Path = SOURCE_ROOT, transfers: list[Path] | None = None, yamls_only: bool = False
) -> list[tuple[Path, str, str]]:
    """Every file the bundle carries, as (source, published path, policy)."""
    triples: list[tuple[Path, str, str]] = []
    if not yamls_only:
        files = source_files(root)
        if not files:
            raise BundleError(f"No Python sources found under {root}")
        triples = [(path, path.relative_to(root).as_posix(), "replace") for path in files]
        for source, published, policy in COMPANION_FILES:
            if not source.is_file():
                raise BundleError(f"Companion file missing: {source}")
            triples.append((source, published, policy))
    for source in transfers or []:
        triples.append((source, f"{ROOT_PREFIX}{source.name}", ROOT_POLICY))
    seen: set[str] = set()
    for _, published, _policy in triples:
        if published in seen:
            raise BundleError(f"Two files would publish to {published}")
        seen.add(published)
    return sorted(triples, key=lambda item: item[1])


def build_sections(
    root: Path = SOURCE_ROOT, transfers: list[Path] | None = None, yamls_only: bool = False
) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    lines: list[str] = []
    mentions: list[str] = []
    for path, published, policy in bundled_files(root, transfers, yamls_only):
        rel = safe_relpath(published)
        text = shipped_text(path, policy, rel)
        if VM_ONLY_PROSE and policy != ROOT_POLICY:
            mentions += bundle_scrub.mentions(rel, text)
        elif NO_SCREENSHOT_MENTIONS:
            mentions += screenshot_mentions(rel, text)
        raw = text.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        entries.append({"path": rel, "sha256": sha, "size": len(raw), "policy": policy})
        lines.append(f"# === BEGIN FILE: {rel} SHA256: {sha} SIZE: {len(raw)} ===")
        lines.extend(encode_payload_lines(text))
        lines.append(f"# === END FILE: {rel} ===")
    if mentions:
        raise BundleError(
            "The bundle would mention screenshots or transcribing (D200), or in its prose "
            "bundling, extraction or the Mac (D201), which it must not for now. Reword each "
            "line, or add a rewording to SCREENSHOT_REWORDS in scripts/bundle_pullmanager.py "
            "or VM_WORDING in scripts/bundle_scrub.py:\n  " + "\n  ".join(mentions))
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


def render_bundle(root: Path = SOURCE_ROOT, transfers: list[Path] | None = None,
                  yamls_only: bool = False) -> str:
    entries, payload_lines = build_sections(root, transfers, yamls_only)
    prelude = BUNDLE_HEADER + extractor_prelude().rstrip("\n") + "\n\n"
    prelude_sha256 = hashlib.sha256(prelude.encode("utf-8")).hexdigest()
    manifest = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "content_id": compute_content_id(entries, prelude_sha256),
        "file_count": len(entries),
        "files": entries,
        "prelude_sha256": prelude_sha256,
    }
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
    if "'''" in manifest_json:
        raise BundleError("Bundle manifest JSON contains a triple quote; cannot embed safely.")

    parts = [
        prelude,
        f"BUNDLE_MANIFEST_JSON = r'''{manifest_json}'''\n",
        BUNDLE_FOOTER,
        "\n",
        "\n".join(payload_lines),
        "\n",
    ]
    return "".join(parts)


def build(
    output: Path = DEFAULT_OUTPUT, root: Path = SOURCE_ROOT, transfers: list[Path] | None = None,
    yamls_only: bool = False,
) -> tuple[Path, dict]:
    text = render_bundle(root, transfers, yamls_only)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    _, manifest = read_bundle(output)
    return output, manifest


def record_content_id(output: Path, content_id: str) -> Path:
    """Write `<bundle file> <content_id>` into content_id.txt beside the bundle,
    replacing that file's line and keeping the others'."""
    path = output.parent / CONTENT_ID_NAME
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept = [line for line in lines if line.split(" ", 1)[0] != output.name and line.strip()]
    path.write_text("\n".join(sorted(kept + [f"{output.name} {content_id}"])) + "\n", encoding="utf-8")
    return path


def retire_old_output(output: Path) -> str | None:
    """Remove a bundle_with_yamls.py left in dist/ by D106, and its content_id line."""
    old = output.parent / RETIRED_OUTPUT_NAME
    if not old.is_file():
        return None
    old.unlink()
    ids = output.parent / CONTENT_ID_NAME
    if ids.is_file():
        kept = [line for line in ids.read_text(encoding="utf-8").splitlines()
                if line.split(" ", 1)[0] != RETIRED_OUTPUT_NAME and line.strip()]
        ids.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
    return f"Removed {old}: bundle.py carries the queue now (D122)."


def build_bundle(names: list[str], queue: bool, output: Path | None = None,
                 root: Path = SOURCE_ROOT, queue_folder: Path | None = None,
                 export_dir: Path | None = None, yamls_only: bool = False) -> tuple[Path, dict, list[str]]:
    """Export and carry what is asked (named transfer YAMLs, and with `queue`
    whatever is queued) in bundle.py, or with `yamls_only` in
    yamls_to_transfer.py without the runtime (D122). Records its content_id and
    empties the queue it carried. Returns (bundle, manifest, what was done)."""
    said: list[str] = []
    folder = queue_folder or TEMP_DIR
    transfers = [find_transfer(name) for name in transfer_names(names)]
    carried_queue = bool(queue and read_queue(folder))
    if carried_queue:
        for temp, transfer in export_queue(folder, export_dir):
            shown = temp.relative_to(REPO_ROOT).as_posix() if temp.is_relative_to(REPO_ROOT) else str(temp)
            said.append(f"Exported {transfer.name} from {shown}")
            if transfer not in transfers:
                transfers.append(transfer)
    if yamls_only and not transfers:
        raise BundleError(f"Nothing to carry: the bundle queue ({folder / QUEUE_NAME}) is empty and no "
                          "yaml= was named. Queue an intake (Save, or Builder > Exports) first.")
    if output is None:
        output = YAMLS_ONLY_OUTPUT if yamls_only else DEFAULT_OUTPUT
    output, manifest = build(output, root, transfers, yamls_only)
    record_content_id(output, manifest["content_id"])
    retired = retire_old_output(output)
    size_kb = output.stat().st_size / 1024
    said.append(f"Wrote {output}  ({manifest['file_count']} files, {size_kb:.1f} KiB)")
    said.append(f"content_id: {manifest['content_id']}")
    if retired:
        said.append(retired)
    if yamls_only:
        said.append("It carries no runtime: extracting it leaves the VM's software as it is.")
    elif not transfers:
        said.append("Nothing was queued, so it carries the runtime alone.")
    for transfer in transfers:
        where = ("YAMLs/temp/ beside scope.py on the VM, its project's one working copy (D162)"
                 if transfer.name.endswith(BLUEPRINT_SUFFIX) else "beside scope.py on the VM")
        said.append(f"Carries {transfer.name}: extracted into {where}.")
        for upload in upload_locations(transfer):
            said.append(f"  It reads {upload}: copy that to the VM at that path beside scope.py, "
                        "unless it is there already.")
    if carried_queue:
        write_queue([], folder)
        said.append("The queue is emptied; save an intake, or add it in Exports, to queue it again.")
    said.append(f"Copy {output.name} to the VM and run `python {output.name}` there.")
    return output, manifest, said


def upload_locations(transfer: Path) -> list[str]:
    """The `file_loc` of each upload a transfer YAML reads, which travel separately."""
    text = transfer.read_text(encoding="utf-8")
    return [value.strip().strip("'\"") for value in re.findall(r"^\s*file_loc:\s*(.+?)\s*$", text, re.M)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bundle_pullmanager.py",
        description="Build the single-file Pullmanager bundle.",
    )
    parser.add_argument("--out", default=None,
                        help="Bundle output path. Default: dist/bundle.py, or dist/yamls_to_transfer.py "
                             "with --yamls-only (D122).")
    parser.add_argument("--yamls-only", action="store_true",
                        help="Carry the queued (and named) transfer YAMLs without the runtime, in "
                             "dist/yamls_to_transfer.py; extracting it leaves the VM's software alone.")
    parser.add_argument("--no-queue", action="store_true",
                        help="Leave the queue out, and in place: the runtime and any yaml= alone.")
    parser.add_argument("--src", default=str(SOURCE_ROOT), help="Source tree to bundle.")
    parser.add_argument("--verify", metavar="BUNDLE", help="Verify an existing bundle and exit.")
    parser.add_argument(
        "yaml",
        nargs="*",
        metavar="yaml=NAME,NAME",
        help="Transfer YAMLs to carry, by project name: yaml=IBD_Ancestry,Celiac finds "
             "IBD_Ancestry_blueprint.yaml and Celiac_blueprint.yaml at the repository "
             "root. Each is extracted into YAMLs/temp/ beside scope.py on the VM. Every intake queued in "
             "YAMLs/temp/bundle_queue.txt is carried too, unless --no-queue (D122).",
    )
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

        # `queue` was D106's way to carry the queue; it is carried by default now.
        named = [token for token in args.yaml if token != "queue"]
        _, _, said = build_bundle(named, not args.no_queue, Path(args.out) if args.out else None,
                                  Path(args.src), yamls_only=args.yamls_only)
        for line in said:
            print(line)
    except BundleError as exc:
        print(f"BUNDLE ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
