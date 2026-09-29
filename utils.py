#!/usr/bin/env python3
"""The utilities window (D124): a button for each script in the utils folder.

    python3 utils.py           # the window
    python3 utils.py --list    # the scripts it offers

The utilities live in scripts/pullmanager_src/utils/ and ship in every bundle;
on the VM, extraction writes a utils.py like this one beside scope.py.
"""

import runpy
import sys
from pathlib import Path

ENTRY = Path(__file__).resolve().parent / "scripts" / "pullmanager_src" / "utilities.py"
sys.argv[0] = str(ENTRY)
runpy.run_path(str(ENTRY), run_name="__main__")
