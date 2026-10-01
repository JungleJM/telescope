#!/usr/bin/env python3
"""Apply the VM's dictionary audit to the data dictionary (D155, D161).

    python3 scope.py dictionary-fix runs/dictionary_audit.yaml
    python3 scripts/dictionary_fix.py --tdd     # its tests

The audit (`--audit-dictionary`, or Audit dictionary in Run, on the VM) lists
what the dictionary names and Cosmos lacks, and each column whose type or
nullability differs from Cosmos's. Transcribed here, this removes each listed
column and table from the dictionary and writes in Cosmos's types and
nullability, by their lines, so every other line and comment stays as
written (a type's annotation too: `int (DateKey)` becomes `bigint (DateKey)`).
It lists every YAML under `YAMLs/` that names a changed column: a removed one
to be fixed, a retyped one to be exported again, and one whose nullability
changed to have its own `nullable:` checked, since the Author view copied it.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE_INDENT, COLUMN_INDENT = 2, 6


def dictionary_path(root: Path = ROOT) -> Path:
    """Where datascope.json says the dictionary is (D111), else its default."""
    config = root / "datascope.json"
    if config.is_file():
        named = json.loads(config.read_text(encoding="utf-8")).get("datadictionary")
        if named:
            return root / named
    return root / "reference" / "datadictionary.yaml"


def read_audit(text: str) -> tuple[list[str], dict[str, list[str]], dict[str, dict[str, str]],
                                   dict[str, dict[str, bool]]]:
    """The tables and the columns (by table) the audit says to remove, and the
    types and nullability (by table and column) it says to write in."""
    import yaml

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return [], {}, {}, {}  # "the dictionary matches Cosmos"
    tables = [str(t) for t in data.get("tables_not_found") or []]
    columns = {str(t): [str(c) for c in (cols or [])]
               for t, cols in (data.get("columns_not_in_cosmos") or {}).items()}
    types = {str(t): {str(c): str(v) for c, v in (cols or {}).items()}
             for t, cols in (data.get("types_wrong") or {}).items()}
    nullable = {str(t): {str(c): bool(v) for c, v in (cols or {}).items()}
                for t, cols in (data.get("nullable_wrong") or {}).items()}
    return tables, columns, types, nullable


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def block_end(lines: list[str], start: int, level: int) -> int:
    """The line after the block that opens at `start`: the next line that is
    not blank, not a comment inside it, and indented no deeper than `level`."""
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and indent(line) <= level:
            break
        end += 1
    # Blank lines and comments just before the next block belong to it.
    while end > start + 1 and (not lines[end - 1].strip()
                               or (lines[end - 1].lstrip().startswith("#")
                                   and indent(lines[end - 1]) <= level)):
        end -= 1
    return end


def find_table(lines: list[str], table: str) -> int | None:
    for number, line in enumerate(lines):
        if indent(line) == TABLE_INDENT and line.strip() == f"{table}:":
            return number
    return None


def remove(text: str, tables: list[str], columns: dict[str, list[str]]) -> tuple[str, list[str], list[str]]:
    """The dictionary without them; what was removed, and what was not found."""
    lines = text.splitlines(keepends=True)
    removed, absent = [], []
    for table, names in columns.items():
        start = find_table(lines, table)
        if start is None:
            absent.extend(f"{table}.{name}" for name in names)
            continue
        stop = block_end(lines, start, TABLE_INDENT)
        for name in names:
            found = next((n for n in range(start, stop) if indent(lines[n]) == COLUMN_INDENT
                          and lines[n].strip() == f"{name}:"), None)
            if found is None:
                absent.append(f"{table}.{name}")
                continue
            end = block_end(lines, found, COLUMN_INDENT)
            del lines[found:end]
            stop -= end - found
            removed.append(f"{table}.{name}")
    for table in tables:
        start = find_table(lines, table)
        if start is None:
            absent.append(table)
            continue
        # The comment lines just above a table are its own.
        first = start
        while first > 0 and lines[first - 1].lstrip().startswith("#") and indent(lines[first - 1]) == TABLE_INDENT:
            first -= 1
        del lines[first:block_end(lines, start, TABLE_INDENT)]
        removed.append(table)
    return "".join(lines), removed, absent


# A type's own words, before any annotation: `nvarchar(300)`, `int`.
_TYPE = re.compile(r"^(?P<lead>\s*type:\s*)(?P<type>[A-Za-z][\w/]*(?:\s*\(\s*(?:\d+\s*(?:,\s*\d+)?|max)\s*\))?)",
                   re.IGNORECASE)
FIELD_INDENT = 8


def find_column(lines: list[str], table: str, column: str) -> tuple[int, int] | None:
    """Where a column's block starts and ends."""
    start = find_table(lines, table)
    if start is None:
        return None
    stop = block_end(lines, start, TABLE_INDENT)
    found = next((n for n in range(start, stop) if indent(lines[n]) == COLUMN_INDENT
                  and lines[n].strip() == f"{column}:"), None)
    if found is None:
        return None
    return found, block_end(lines, found, COLUMN_INDENT)


def rewrite(text: str, types: dict[str, dict[str, str]], nullable: dict[str, dict[str, bool]]
            ) -> tuple[str, list[str], list[str]]:
    """The dictionary with Cosmos's types and nullability written in; what
    changed (`Table.Column: old -> new`), and what was not found."""
    lines = text.splitlines(keepends=True)
    changed, absent = [], []

    def field(start: int, end: int, name: str) -> int | None:
        return next((n for n in range(start + 1, end) if indent(lines[n]) == FIELD_INDENT
                     and lines[n].lstrip().startswith(f"{name}:")), None)

    for table in dict.fromkeys([*types, *nullable]):
        for column in dict.fromkeys([*types.get(table, {}), *nullable.get(table, {})]):
            span = find_column(lines, table, column)
            if span is None:
                absent.append(f"{table}.{column}")
                continue
            start, end = span
            if column in types.get(table, {}):
                new = types[table][column]
                at = field(start, end, "type")
                if at is None:
                    lines.insert(start + 1, f"{' ' * FIELD_INDENT}type: {new}\n")
                    end += 1
                    changed.append(f"{table}.{column}: type (none) -> {new}")
                else:
                    match = _TYPE.match(lines[at])
                    old = match.group("type") if match else lines[at].split(":", 1)[1].strip()
                    lines[at] = (match.group("lead") + new + lines[at][match.end():] if match
                                 else f"{' ' * FIELD_INDENT}type: {new}\n")
                    changed.append(f"{table}.{column}: type {old} -> {new}")
            if column in nullable.get(table, {}):
                new = "true" if nullable[table][column] else "false"
                at = field(start, end, "nullable")
                if at is None:
                    after = field(start, end, "type")
                    lines.insert((after if after is not None else start) + 1,
                                 f"{' ' * FIELD_INDENT}nullable: {new}\n")
                    changed.append(f"{table}.{column}: nullable (none) -> {new}")
                else:
                    old = lines[at].split(":", 1)[1].strip()
                    lines[at] = f"{' ' * FIELD_INDENT}nullable: {new}\n"
                    changed.append(f"{table}.{column}: nullable {old} -> {new}")
    return "".join(lines), changed, absent


def usages(root: Path, tables: list[str], columns: dict[str, list[str]]) -> list[str]:
    """`file:line: text` for every YAML under YAMLs/ naming a removed name."""
    patterns = [re.compile(rf"\b\w+\.{re.escape(c)}\b") for names in columns.values() for c in names]
    patterns += [re.compile(rf"\b{re.escape(t)}\b") for t in tables]
    found = []
    folder = root / "YAMLs"
    for path in sorted(folder.rglob("*.yaml")) if folder.is_dir() else []:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if any(p.search(line) for p in patterns):
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return found


def main(argv: list[str], root: Path = ROOT) -> int:
    if not argv:
        print(__doc__)
        return 1
    audit = Path(argv[0])
    if not audit.is_absolute():
        audit = root / audit
    tables, columns, types, nullable = read_audit(audit.read_text(encoding="utf-8"))
    if not tables and not columns and not types and not nullable:
        print("The audit lists nothing to change: the dictionary matches Cosmos.")
        return 0
    path = dictionary_path(root)
    text, changed, absent = rewrite(path.read_text(encoding="utf-8"), types, nullable)
    text, removed, gone = remove(text, tables, columns)
    absent += gone
    path.write_text(text, encoding="utf-8")
    where = path.relative_to(root).as_posix()
    if removed:
        print(f"Removed from {where}: {len(removed)}")
        for name in removed:
            print(f"  {name}")
    if changed:
        print(f"Written into {where}, as Cosmos has them: {len(changed)}")
        for name in changed:
            print(f"  {name}")
    if absent:
        print(f"Not in the dictionary (already gone, or spelled otherwise): {', '.join(absent)}")
    sections = (
        ("Still named here (a same-named column of another table shows too); "
         "fix each and export it again:", usages(root, tables, columns)),
        ("Naming a retyped column: export each again, and its types follow the dictionary:",
         usages(root, [], {t: list(c) for t, c in types.items()})),
        ("Naming a column whose nullability changed: the Author view copied `nullable:` into "
         "each, so check its own (`false` filters out the rows where it is null):",
         usages(root, [], {t: list(c) for t, c in nullable.items()})),
    )
    for heading, found in sections:
        if found:
            print(heading)
            for line in found:
                print(f"  {line}")
    if not any(found for _, found in sections):
        print("No YAML under YAMLs/ names them.")
    return 0


DICTIONARY = """\
DataDictionary:
  # Checked.
  MedicationDispenseFact:
    description: >
      Dispenses.
    columns:
      RawCodeValue_X:
        type: string
        description: >
          Raw code value.
      ReadyToDispenseDateKey:
        type: integer (DateKey)
        nullable: true
        description: >
          DateKey representing when the medication became ready to dispense;

          a second paragraph.
      RefillsRemaining_X:
        type: integer
    standard_where:
      - "_IsDeleted = 0"

  # A table Cosmos lacks.
  GoneTable:
    columns:
      A:
        type: int

  PatientDim:
    columns:
      DurableKey:
        type: bigint
# Tables still to check are listed here.
"""

AUDIT = """\
# dictionary audit, 2026-10-01 09:12, bundle abcd1234, database COSMOS: 3 tables, 2 wrong
tables_not_found:  # the dictionary names them; Cosmos has no such table
  - GoneTable
columns_not_in_cosmos:  # remove these from the dictionary
  MedicationDispenseFact: [ReadyToDispenseDateKey]  # near: nothing
"""


class FixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "reference").mkdir()
        self.dictionary = self.root / "reference" / "datadictionary.yaml"
        self.dictionary.write_text(DICTIONARY, encoding="utf-8")
        (self.root / "audit.yaml").write_text(AUDIT, encoding="utf-8")
        (self.root / "YAMLs" / "temp").mkdir(parents=True)
        (self.root / "YAMLs" / "temp" / "Crohns_intake.yaml").write_text(
            "columns:\n  - source: mdf.ReadyToDispenseDateKey\n  - source: mdf.RawCodeValue_X\n",
            encoding="utf-8")

    def run_fix(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["audit.yaml"], self.root)
        return code, out.getvalue()

    def test_the_listed_column_and_table_go_and_nothing_else(self):
        self.run_fix()
        import yaml

        text = self.dictionary.read_text(encoding="utf-8")
        data = yaml.safe_load(text)["DataDictionary"]
        self.assertEqual(sorted(data), ["MedicationDispenseFact", "PatientDim"])
        self.assertEqual(list(data["MedicationDispenseFact"]["columns"]),
                         ["RawCodeValue_X", "RefillsRemaining_X"])
        self.assertEqual(data["MedicationDispenseFact"]["standard_where"], ["_IsDeleted = 0"])
        self.assertIn("  # Checked.\n", text)
        self.assertNotIn("A table Cosmos lacks", text)
        self.assertTrue(text.endswith("# Tables still to check are listed here.\n"))

    def test_it_names_the_yamls_still_using_a_removed_column(self):
        code, out = self.run_fix()
        self.assertEqual(code, 0)
        self.assertIn("YAMLs/temp/Crohns_intake.yaml:2: - source: mdf.ReadyToDispenseDateKey", out)
        self.assertNotIn("RawCodeValue_X", out.split("Still named here")[1])

    def test_a_clean_audit_changes_nothing(self):
        (self.root / "audit.yaml").write_text(
            "# dictionary audit, ...: 3 tables, 0 wrong\nthe dictionary matches Cosmos\n", encoding="utf-8")
        code, out = self.run_fix()
        self.assertIn("nothing to change", out)
        self.assertEqual(self.dictionary.read_text(encoding="utf-8"), DICTIONARY)

    def test_cosmoss_types_and_nullability_are_written_in_and_nothing_else(self):
        (self.root / "audit.yaml").write_text(
            "# dictionary audit, ...: 3 tables, 5 wrong\n"
            "types_wrong:  # Cosmos's type; dictionary-fix writes it in\n"
            "  MedicationDispenseFact: {RawCodeValue_X: nvarchar(300), "
            "ReadyToDispenseDateKey: bigint, RefillsRemaining_X: \"numeric(19,4)\"}\n"
            "nullable_wrong:  # Cosmos's nullability; dictionary-fix writes it in\n"
            "  MedicationDispenseFact: {ReadyToDispenseDateKey: false, RefillsRemaining_X: false}\n",
            encoding="utf-8")
        _, out = self.run_fix()
        import yaml

        text = self.dictionary.read_text(encoding="utf-8")
        columns = yaml.safe_load(text)["DataDictionary"]["MedicationDispenseFact"]["columns"]
        self.assertEqual(columns["RawCodeValue_X"]["type"], "nvarchar(300)")
        self.assertEqual(columns["ReadyToDispenseDateKey"]["type"], "bigint (DateKey)",
                         "the annotation stays")
        self.assertIs(columns["ReadyToDispenseDateKey"]["nullable"], False)
        self.assertEqual(columns["RefillsRemaining_X"],
                         {"type": "numeric(19,4)", "nullable": False})
        self.assertIn("\n          a second paragraph.\n", text)
        self.assertEqual(text.replace("nvarchar(300)", "string").replace("bigint (DateKey)", "integer (DateKey)")
                         .replace("        nullable: false\n", "        nullable: true\n", 1)
                         .replace("numeric(19,4)\n        nullable: false\n", "integer\n"),
                         DICTIONARY, "every other line as written")
        self.assertIn("MedicationDispenseFact.ReadyToDispenseDateKey: type integer -> bigint", out)
        self.assertIn("MedicationDispenseFact.RefillsRemaining_X: nullable (none) -> false", out)
        retyped = out.split("Naming a retyped column")[1].split("Naming a column whose")[0]
        self.assertIn("YAMLs/temp/Crohns_intake.yaml:2: - source: mdf.ReadyToDispenseDateKey", retyped)
        self.assertIn("YAMLs/temp/Crohns_intake.yaml:3: - source: mdf.RawCodeValue_X", retyped)
        self.assertIn("mdf.ReadyToDispenseDateKey", out.split("Naming a column whose")[1])

    def test_a_retyped_column_and_a_removed_one_in_the_same_table(self):
        (self.root / "audit.yaml").write_text(
            AUDIT + "types_wrong:\n  MedicationDispenseFact: {RefillsRemaining_X: smallint}\n",
            encoding="utf-8")
        self.run_fix()
        import yaml

        columns = yaml.safe_load(self.dictionary.read_text(encoding="utf-8"))[
            "DataDictionary"]["MedicationDispenseFact"]["columns"]
        self.assertEqual(list(columns), ["RawCodeValue_X", "RefillsRemaining_X"])
        self.assertEqual(columns["RefillsRemaining_X"]["type"], "smallint")

    def test_a_name_not_in_the_dictionary_is_said(self):
        (self.root / "audit.yaml").write_text(
            "columns_not_in_cosmos:\n  PatientDim: [NoSuchColumn]\n", encoding="utf-8")
        _, out = self.run_fix()
        self.assertIn("PatientDim.NoSuchColumn", out)
        self.assertEqual(self.dictionary.read_text(encoding="utf-8"), DICTIONARY)


if __name__ == "__main__":
    if "--tdd" in sys.argv:
        sys.argv = [sys.argv[0]]
        unittest.main(verbosity=1)
    raise SystemExit(main(sys.argv[1:]))
