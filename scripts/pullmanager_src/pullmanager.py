#!/usr/bin/env python3
"""Launcher for the extracted Pullmanager runtime.

Sits beside the `pullmanager/` package. On the VM it is reached through the
`scope.py` that `bundle.py --extract` writes into the working folder:

    python scope.py                        # the app (D123)
    python scope.py --tdd
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pullmanager.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
