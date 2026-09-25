#!/usr/bin/env python3
"""Launcher for the extracted Pullmanager runtime.

Sits beside the `pullmanager/` package so the VM can run:

    python pullmanager_runtime/pullmanager.py runs/<project>/split/pullmanifest.yaml
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pullmanager.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
