"""`python rsv <command>`."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

from .config import PACKAGE_DIR, RsvError, Settings, find_source, load_settings

USAGE = """python rsv <command>

  build                 make the analysis parquets and the build page
  report [section]      one page: every metric, for everyone or a section
  compare <grouping> [section]
                        the metrics side by side, by a grouping
  admission [section]   race, ethnicity, SVI, financial class
                        against admission and ICU
  all                   build, then a first run of every page
  sections              the sections and groupings in settings.yaml
  keys                  write the follow-up pull's PK file
  test                  run the tests

  --sp                  read SneakPeek only (to prove the pull works)
"""

KEY_COLUMNS = ["EdVisitKey", "EncounterKey", "PatientDurableKey", "HospitalAdmissionKey", "ArrivalInstant",
               "DepartureInstant"]


def show(page_path: Path, text: str, quiet: bool = False) -> None:
    if not quiet:
        print(text)
    print(f"Page written to {page_path}")


def cmd_build(settings: Settings, quiet: bool = False) -> None:
    from .build import build, build_page
    from .page import data_version
    built = build(settings)
    page = build_page(settings, built, data_version(settings.analysis_folder / "visits.parquet"))
    path = page.write("build")
    show(path, page.render(), quiet)
    print(f"Parquets written to {settings.analysis_folder}")


def cmd_sections(settings: Settings) -> None:
    print("Sections (python rsv report <section>):")
    for name, expression in (settings.get("sections") or {}).items():
        print(f"  {name:<18} {expression}")
    print("Groupings (python rsv compare <grouping>):")
    print("  " + ", ".join(settings.get("groupings") or []))


def cmd_keys(settings: Settings) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq
    source = find_source(settings, "visits")
    if not source.found:
        raise RsvError(f"No visits table with rows ({'; '.join(source.tried)}). Check `pull_folder` in settings.yaml.")
    table = pq.read_table(source.path, columns=KEY_COLUMNS).to_pandas()
    table = table.drop_duplicates("EdVisitKey").sort_values("EdVisitKey")
    missing = table["DepartureInstant"].isna()
    table.loc[missing, "DepartureInstant"] = table.loc[missing, "ArrivalInstant"]
    schema = pa.schema([("EdVisitKey", pa.int64()), ("EncounterKey", pa.int64()), ("PatientDurableKey", pa.int64()),
                        ("HospitalAdmissionKey", pa.int64()), ("ArrivalInstant", pa.timestamp("us")),
                        ("DepartureInstant", pa.timestamp("us"))])
    out = settings.root / settings["keys_file"]
    pq.write_table(pa.Table.from_pandas(table, schema=schema, preserve_index=False), out)
    print(f"{len(table):,} visits written to {out}")
    if missing.any():
        print(f"{int(missing.sum()):,} had no departure time; their arrival stands in for it.")
    return out


def cmd_all(settings: Settings) -> None:
    from . import analysis
    cmd_build(settings, quiet=True)
    visits = analysis.load(settings)
    pages = [analysis.report(settings, None, visits), analysis.admission(settings, None, visits)]
    for grouping in settings.get("first_run_compare") or []:
        pages.append(analysis.compare(settings, grouping, None, visits))
    for name in settings.get("sections") or {}:
        pages.append(analysis.report(settings, name, visits))
    build_text = (settings.pages_folder / "build.txt").read_text(encoding="utf-8")
    texts = [build_text]
    for page in pages:
        name = page.command.replace(" --sp", "").replace(" ", "_")
        page.write(name)
        texts.append(page.render())
    combined = settings.pages_folder / "first_run.txt"
    combined.write_text("\n".join(texts), encoding="utf-8")
    print(f"{len(texts)} pages written to {settings.pages_folder}; all of them in {combined.name}")


def cmd_test() -> int:
    suite = unittest.defaultTestLoader.discover(str(PACKAGE_DIR / "tests"), top_level_dir=str(PACKAGE_DIR.parent))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sp = "--sp" in argv
    argv = [a for a in argv if a != "--sp"]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command in ("test", "--tdd"):
            return cmd_test()
        settings = load_settings(sp=sp)
        from . import analysis
        if command == "build":
            cmd_build(settings)
        elif command == "report":
            page = analysis.report(settings, rest[0] if rest else None)
            show(page.write("report" + (f"_{rest[0]}" if rest else "")), page.render())
        elif command == "compare":
            if not rest:
                raise RsvError("Name a grouping: python rsv compare <grouping> [section]. "
                               f"Groupings: {', '.join(settings.get('groupings') or [])}")
            page = analysis.compare(settings, rest[0], rest[1] if len(rest) > 1 else None)
            show(page.write("compare_" + "_".join(rest)), page.render())
        elif command == "admission":
            page = analysis.admission(settings, rest[0] if rest else None)
            show(page.write("admission" + (f"_{rest[0]}" if rest else "")), page.render())
        elif command == "all":
            cmd_all(settings)
        elif command == "sections":
            cmd_sections(settings)
        elif command == "keys":
            cmd_keys(settings)
        else:
            print(f"No command `{command}`.\n")
            print(USAGE)
            return 2
    except RsvError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0
