#!/usr/bin/env python3
"""Build dist/bundle.py, the one file that carries Pullmanager to the VM.

    python3 makebundle.py          # build it, and print its content_id
    python3 makebundle.py queue    # dist/bundle_with_yamls.py, with the queued YAMLs (D106)
    python3 makebundle.py --tdd    # the bundle's own tests

A shortcut for scripts/bundle_pullmanager.py, which it runs with the same
arguments (D64).
"""

import runpy
import sys
from pathlib import Path

BUILDER = Path(__file__).resolve().parent / "scripts" / "bundle_pullmanager.py"
sys.argv[0] = str(BUILDER)
runpy.run_path(str(BUILDER), run_name="__main__")
