#!/usr/bin/env python3
"""Build dist/rsv_bundle.py: the Infant RSV analysis, in one file for the VM (D209).

    python3 studies/infant_rsv/make_rsv_bundle.py          # dist/rsv_bundle.py
    python3 studies/infant_rsv/make_rsv_bundle.py --out F  # somewhere else
    python3 studies/infant_rsv/make_rsv_bundle.py --tdd    # its tests

On the VM, beside scope.py: `python rsv_bundle.py` checks every file against
its hash and installs the folder `rsv`, then `python rsv build`. An edited
`rsv/settings.yaml` is kept; the new one is written beside it as
`settings.new.yaml`. Like Scope's bundle (D200-D203), nothing it carries may
say how it got there: the build stops on such a line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SOURCE = HERE / "rsv"
DEFAULT_OUT = REPO / "dist" / "rsv_bundle.py"
sys.path.insert(0, str(REPO / "scripts"))
import bundle_scrub  # noqa: E402

BEGIN = "# === BEGIN FILE: {path} SHA256: {sha} SIZE: {size} ==="
END = "# === END FILE: {path} ==="

INSTALLER = r'''#!/usr/bin/env python3
"""Infant RSV analysis.

    python rsv_bundle.py           # check every file, then install the folder rsv here
    python rsv_bundle.py --yes     # without asking
    python rsv_bundle.py --check   # check only

Then: python rsv build
"""

import hashlib
import json
import re
import sys
from pathlib import Path

BEGIN_RE = re.compile(r"^# === BEGIN FILE: (?P<path>[A-Za-z0-9_./-]+) SHA256: (?P<sha>[0-9a-f]{64}) SIZE: (?P<size>\d+) ===$")
END_RE = re.compile(r"^# === END FILE: (?P<path>[A-Za-z0-9_./-]+) ===$")
RECORD = ".installed.json"
KEEP_IF_EDITED = ("settings.yaml",)


def payload(text):
    files, current, lines = {}, None, []
    for line in text.splitlines():
        begin = BEGIN_RE.match(line)
        if begin:
            current, lines = begin.groupdict(), []
            continue
        end = END_RE.match(line)
        if end and current:
            if end["path"] != current["path"]:
                raise SystemExit(f"Damaged file: {current['path']} has no end of its own.")
            body = "".join(l[2:] + "\n" if l.startswith("# ") else "\n" for l in lines)
            data = body.encode("utf-8")
            if hashlib.sha256(data).hexdigest() != current["sha"] or len(data) != int(current["size"]):
                raise SystemExit(f"Damaged file: {current['path']} does not match its hash. Copy rsv_bundle.py again.")
            files[current["path"]] = data
            current = None
            continue
        if current is not None:
            lines.append(line)
    if current is not None:
        raise SystemExit(f"Damaged file: {current['path']} is cut off. Copy rsv_bundle.py again.")
    declared = json.loads(MANIFEST)["files"]
    if sorted(files) != sorted(declared):
        raise SystemExit("Damaged file: its files do not match its list. Copy rsv_bundle.py again.")
    return files


def sha(data):
    return hashlib.sha256(data).hexdigest()


def install(files, folder):
    target = folder / "rsv"
    target.mkdir(parents=True, exist_ok=True)
    record_path = target / RECORD
    before = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    kept = []
    for path, data in sorted(files.items()):
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        if path in KEEP_IF_EDITED and out.is_file():
            current = sha(out.read_bytes())
            if current != before.get(path, current) and current != sha(data):
                new = out.with_name(out.stem + ".new" + out.suffix)
                new.write_bytes(data)
                kept.append((out, new))
                continue
        out.write_bytes(data)
    for path in sorted(set(before) - set(files)):
        stale = target / path
        if stale.is_file():
            stale.unlink()
    record = {path: sha(data) for path, data in files.items()}
    for out, _new in kept:
        record[out.relative_to(target).as_posix()] = before.get(out.relative_to(target).as_posix())
    record_path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return target, kept


def main(argv):
    text = Path(__file__).read_text(encoding="utf-8")
    files = payload(text)
    version = json.loads(MANIFEST)["version"]
    print(f"Infant RSV analysis, version {version}: {len(files)} files, every one matches its hash.")
    if "--check" in argv:
        return 0
    folder = Path(__file__).resolve().parent
    if "--yes" not in argv:
        answer = input(f"Install into {folder / 'rsv'}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing installed.")
            return 1
    target, kept = install(files, folder)
    print(f"Installed into {target}")
    for out, new in kept:
        print(f"Your edited {out.name} is kept; the new one is {new.name}. Compare them and merge what you want.")
    print("Next: python rsv build")
    return 0
'''

FOOTER = '''

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


class BundleError(Exception):
    pass


def source_files(root: Path = SOURCE) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                  and p.suffix in (".py", ".yaml"))


def render(root: Path = SOURCE) -> str:
    entries, payload, problems = [], [], []
    for path in source_files(root):
        published = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        problems += bundle_scrub.mentions(f"rsv/{published}", text)
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        entries.append({"path": published, "sha256": digest})
        payload.append(BEGIN.format(path=published, sha=digest, size=len(data)))
        payload += [f"# {line}" if line else "#" for line in text.split("\n")[:-1]]
        payload.append(END.format(path=published))
    if problems:
        raise BundleError("What ships must not say how it got there (D200-D203). Reword:\n  " + "\n  ".join(problems))
    version = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:8]
    manifest = json.dumps({"version": version, "files": [e["path"] for e in entries]}, sort_keys=True)
    return INSTALLER + f"\nMANIFEST = r'''{manifest}'''\n" + FOOTER + "\n" + "\n".join(payload) + "\n"


def build(out: Path = DEFAULT_OUT, root: Path = SOURCE) -> str:
    text = render(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return json.loads(text.split("MANIFEST = r'''", 1)[1].split("'''", 1)[0])["version"]


# ---------------------------------------------------------------- tests

class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rsv_bundle_"))
        self.out = self.tmp / "rsv_bundle.py"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bundle(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.out), *args], capture_output=True, text=True, cwd=self.tmp)

    def test_installs_every_file_as_it_is(self):
        build(self.out)
        result = self.run_bundle("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in source_files():
            installed = self.tmp / "rsv" / path.relative_to(SOURCE)
            self.assertEqual(installed.read_bytes().rstrip(b"\n"), path.read_bytes().rstrip(b"\n"), str(path))

    def test_installed_package_runs(self):
        build(self.out)
        self.run_bundle("--yes")
        result = subprocess.run([sys.executable, "rsv", "help"], capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python rsv <command>", result.stdout)

    def test_edited_settings_kept(self):
        build(self.out)
        self.run_bundle("--yes")
        settings = self.tmp / "rsv" / "settings.yaml"
        settings.write_text(settings.read_text() + "\n# mine\n")
        source = self.tmp / "src"
        import shutil
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "settings.yaml").write_text((source / "settings.yaml").read_text() + "\n# newer\n")
        build(self.out, source)
        result = self.run_bundle("--yes")
        self.assertIn("is kept", result.stdout)
        self.assertIn("# mine", settings.read_text())
        self.assertIn("# newer", (self.tmp / "rsv" / "settings.new.yaml").read_text())

    def test_unedited_settings_replaced(self):
        build(self.out)
        self.run_bundle("--yes")
        source = self.tmp / "src"
        import shutil
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "settings.yaml").write_text((source / "settings.yaml").read_text() + "\n# newer\n")
        build(self.out, source)
        self.run_bundle("--yes")
        self.assertIn("# newer", (self.tmp / "rsv" / "settings.yaml").read_text())
        self.assertFalse((self.tmp / "rsv" / "settings.new.yaml").exists())

    def test_damaged_file_refused(self):
        build(self.out)
        text = self.out.read_text().replace("# def summarize", "# def summarise", 1)
        self.out.write_text(text)
        result = self.run_bundle("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match its hash", result.stdout + result.stderr)

    def test_same_sources_same_bytes(self):
        self.assertEqual(render(), render())

    def test_refuses_delivery_wording(self):
        import shutil
        source = self.tmp / "src"
        shutil.copytree(SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "extra.py").write_text('"""Copied over from the Mac."""\n')
        with self.assertRaises(BundleError):
            render(source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tdd", action="store_true")
    args = parser.parse_args(argv)
    if args.tdd:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(BundleTests)
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    try:
        version = build(args.out)
    except BundleError as exc:
        print(exc)
        return 1
    print(f"Wrote {args.out} (version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
