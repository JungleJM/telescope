"""The run scan: what each pull built, against what it packaged (D152).

Errors a batch hid (D151) left tables `done` with fewer rows than Cosmos
built, or none. This reads every pull under the runs folder, without a
database, and writes `runs/run_scan.yaml` with only what did not check out,
short enough to be copied off the VM by screenshot.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config
from .artifacts import PARQUET_FOLDERS, file_name, parquets_folder, plan
from .manifest import Manifest, ManifestError
from .models import DONE
from .yaml_io import load_yaml

SCAN_FILENAME = "run_scan.yaml"


def scan_manifest(manifest: Manifest) -> dict[str, Any]:
    """One pull's problems, by kind; empty when it checks out."""
    problems: dict[str, Any] = {}
    built: dict[str, int] = {}  # table -> rows Cosmos built, as the manifest recorded
    for session in manifest.sessions:
        for phase in session.phases:
            if phase.name != "pk" or phase.status != DONE:
                continue
            for dest, rows in (phase.outputs.get("table_rows") or {}).items():
                built[dest] = built.get(dest, 0) + int(rows or 0)
            cosmos = phase.outputs.get("cosmos_rows")
            sampled = phase.outputs.get("control_sample") or {}
            if cosmos is not None and not sampled:
                kept = (phase.outputs.get("table_rows") or {}).get(session.pk_table)
                if kept is not None and int(kept) < int(cosmos):
                    problems.setdefault("lost_rows", {})[session.pk_table] = (
                        f"Cosmos built {int(cosmos):,}, {int(kept):,} landed")
            mult = float(sampled.get("row_mult") or 0)
            for label, counts in (sampled.get("per_batch") or {}).items():
                cases, kept = int(counts.get("cases") or 0), int(counts.get("controls") or 0)
                if kept < int(cases * mult):
                    problems.setdefault("short_controls", {}).setdefault(session.pk_table, []).append(
                        f"{label} kept {kept:,} for {cases:,} cases ({mult:g}x)")
        for run in session.runs:
            if run.status != DONE:
                continue
            counted = run.outputs.get("table_rows") or {}
            for dest, rows in counted.items():
                built[dest] = built.get(dest, 0) + int(rows or 0)
            for dest in run_tables(manifest, run):
                if dest not in counted:
                    problems.setdefault("no_count", []).append(f"{dest} ({run.label})")
    finished = finished_tables(manifest)
    empty = sorted(dest for dest in finished if built.get(dest) == 0)
    if empty:
        problems["empty"] = empty
    compare_parquets(manifest, built, finished, problems)
    return problems


def run_tables(manifest: Manifest, run: Any) -> list[str]:
    """The tables a run's document says it makes."""
    try:
        doc = load_yaml(manifest.resolve(run)) or {}
    except (OSError, ValueError):
        return []
    return [str(c["dest_table"]) for c in doc.get("cohorts") or []
            if isinstance(c, dict) and c.get("dest_table")]


def finished_tables(manifest: Manifest) -> set[str]:
    """The tables Artifacts would package: those whose steps are all done."""
    try:
        return {spec.dest for spec in plan(manifest).tables if spec.kind in ("pk", "run")}
    except (OSError, ValueError, ManifestError):
        return set()


def compare_parquets(manifest: Manifest, built: dict[str, int], finished: set[str],
                     problems: dict[str, Any]) -> None:
    folder = parquets_folder(manifest.path)
    # Artifacts ends by writing contents.md; the PK's parquet comes earlier (D87).
    packaged = (folder / "contents.md").is_file()
    if not packaged and finished:
        problems["not_packaged"] = "Artifacts has not run; only parquets already written are compared"
    if not any((folder / name).is_dir() for name in PARQUET_FOLDERS):
        return
    try:
        import pyarrow.parquet as pq
    except ImportError:
        problems["not_compared"] = "this Python lacks pyarrow, so the parquets were not read"
        return
    for spec in plan(manifest).tables:
        if spec.kind not in ("pk", "run") or spec.dest not in built:
            continue
        paths = [folder / spec.folder / file_name(spec.dest, part.label) for part in spec.parts]
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            # Before Artifacts only the PK's parquet exists, which is no fault.
            if packaged:
                problems.setdefault("no_parquet", []).extend(missing)
            continue
        rows = sum(pq.read_metadata(path).num_rows for path in paths)
        if rows < built[spec.dest]:
            problems.setdefault("lost_rows", {})[spec.dest] = (
                f"built {built[spec.dest]:,}, parquets {rows:,}")


def scan_runs(home: Path) -> tuple[Path, str, int]:
    """Scan every pull; write the report. Returns its path, text and problem count."""
    from .pulls import find_pulls

    found: dict[str, dict[str, Any]] = {}
    pulls = find_pulls(home)
    for pull in pulls:
        try:
            problems = scan_manifest(Manifest.load(pull.manifest))
        except (ManifestError, OSError, ValueError) as exc:
            problems = {"unreadable": str(exc)}
        if problems:
            found[pull.name] = problems
    count = sum(count_problems(p) for p in found.values())
    bundle = config.bundle_id()
    header = (f"# run scan, {time.strftime('%Y-%m-%d %H:%M')}"
              f"{f', bundle {bundle}' if bundle else ''}: {len(pulls)} pulls, {count} problems")
    lines = [header]
    if not found:
        lines.append("every pull checks out" if pulls else "no pulls under runs/")
    for name, problems in found.items():
        lines.append(f"{name}:")
        for kind, value in problems.items():
            lines.extend(yaml_lines(kind, value, "  "))
    text = "\n".join(lines) + "\n"
    path = config.runs_dir(home) / SCAN_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text, count


def count_problems(problems: dict[str, Any]) -> int:
    total = 0
    for value in problems.values():
        if isinstance(value, dict):
            total += sum(len(v) if isinstance(v, list) else 1 for v in value.values())
        elif isinstance(value, list):
            total += len(value)
        else:
            total += 1
    return total


def yaml_lines(key: str, value: Any, indent: str) -> list[str]:
    """Plain YAML, quoted only where a value needs it."""
    if isinstance(value, dict):
        lines = [f"{indent}{key}:"]
        for inner, item in value.items():
            lines.extend(yaml_lines(inner, item, indent + "  "))
        return lines
    if isinstance(value, list):
        return [f"{indent}{key}:"] + [f"{indent}  - {scalar(item)}" for item in value]
    return [f"{indent}{key}: {scalar(value)}"]


def scalar(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in ":#{}[]&*!|>'\"%@`") or text != text.strip():
        return json.dumps(text)
    return text
