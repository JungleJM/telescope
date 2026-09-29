#!/usr/bin/env python3
"""Build the bundle that carries Datascope, and the queued pulls, to the VM.

    python3 makebundle.py               # dist/bundle.py: the runtime and every queued pull (D122)
    python3 makebundle.py --yamls-only  # dist/yamls_to_transfer.py: the queued pulls alone
    python3 makebundle.py --no-queue    # the runtime alone, the queue left as it is
    python3 makebundle.py --tdd         # the bundle's own tests

A shortcut for scripts/bundle_pullmanager.py, which it runs with the same
arguments (D64).
"""

import runpy
import sys
from pathlib import Path

BUILDER = Path(__file__).resolve().parent / "scripts" / "bundle_pullmanager.py"
sys.argv[0] = str(BUILDER)
runpy.run_path(str(BUILDER), run_name="__main__")
