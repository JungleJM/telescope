#!/usr/bin/env python3
"""Datascope's front door (D112, D123): the app, the tests, and Pullmanager's commands.

    python3 scope.py                         # the app: Author and Run
    python3 scope.py test                    # every test suite
    python3 scope.py images                  # delete pasted images no document mentions (D132)
    python3 scope.py dictionary-fix <audit>  # apply the VM's dictionary audit (D155)
    python3 scope.py --execute IBD_Ancestry  # anything else goes to Pullmanager

The VM's `scope.py`, written by extraction, does the same but for `test`.

It runs from the repository root wherever it is started, so datascope.json,
the core files in reference/ and the runs folder are found (D111); a path typed
after it is read from the root too.
"""

import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PULLMANAGER = ROOT / "scripts" / "pullmanager_src" / "pullmanager.py"
SUITES = (
    "scripts/makeYaml.py",
    "scripts/pullmanager_src/pullmanager.py",
    "scripts/bundle_pullmanager.py",
    "scripts/yamlmanager_model.py",
    "scripts/yamlmanager_tk.py",
    "scripts/tidy_images.py",
    "scripts/dictionary_fix.py",
    "studies/infant_rsv/rsv",
    "studies/infant_rsv/make_rsv_bundle.py",
)


def run_tests() -> int:
    """Every suite, one after another; a line each at the end."""
    results = []
    for suite in SUITES:
        print(f"=== {suite}", flush=True)
        code = subprocess.run([sys.executable, suite, "--tdd"], cwd=ROOT).returncode
        results.append((suite, code))
    print()
    for suite, code in results:
        print(f"{'OK    ' if code == 0 else 'FAILED'} {suite}")
    return 0 if all(code == 0 for _, code in results) else 1


def main(argv: list[str]) -> int:
    os.chdir(ROOT)
    if argv[:1] == ["test"]:
        return run_tests()
    if argv[:1] == ["dictionary-fix"]:
        sys.argv = [str(ROOT / "scripts" / "dictionary_fix.py"), *argv[1:]]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    if argv[:1] == ["images"]:
        sys.argv = [str(ROOT / "scripts" / "tidy_images.py")]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    sys.argv = [str(PULLMANAGER), *argv]
    runpy.run_path(str(PULLMANAGER), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
