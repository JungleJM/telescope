"""Run as `python rsv <command>` from the folder that holds `rsv`."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]
    from rsv.cli import main
else:
    from .cli import main

raise SystemExit(main())
