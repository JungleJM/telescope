#!/usr/bin/env python3
"""Open YAML Manager, the browser UI for building templates and recipes.

    python3 yamlmgr.py          # serve it and open it in the browser
    python3 yamlmgr.py --tdd    # the UI's own tests

A shortcut for scripts/yamlmanager.py, which it runs with the same arguments,
from the repository root, so its default paths (YAMLs/template.yaml,
YAMLs/recipes.yaml) are found wherever it is started from.
"""

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI = ROOT / "scripts" / "yamlmanager.py"
PATH_OPTIONS = ("--template", "--recipes", "--datadictionary", "--out", "--out-dir")
if not any(arg.split("=")[0] in PATH_OPTIONS for arg in sys.argv[1:]):
    os.chdir(ROOT)  # the default paths are the root's; typed paths keep their meaning
sys.argv[0] = str(UI)
runpy.run_path(str(UI), run_name="__main__")
