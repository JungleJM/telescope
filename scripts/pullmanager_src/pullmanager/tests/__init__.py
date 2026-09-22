"""Test suite for the Pullmanager runtime.

Uses stdlib unittest so it stays dependency-free and runs unchanged from an
extracted bundle on the VM:

    python pullmanager.py --tdd            # everything
    python pullmanager.py --tdd manifest   # one module
"""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path


def module_names() -> list[str]:
    """Discover sibling test modules by filename, so nothing needs registering."""
    return sorted(path.stem for path in Path(__file__).parent.glob("test_*.py"))


def build_suite(group: str | None = None) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    available = module_names()
    if group:
        name = group if group.startswith("test_") else f"test_{group}"
        if name not in available:
            raise LookupError(
                f"No test module {name!r}. Available: "
                + ", ".join(n.removeprefix("test_") for n in available)
            )
        available = [name]
    for name in available:
        module = importlib.import_module(f".{name}", package=__name__)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


def run(group: str | None = None, verbosity: int = 2) -> int:
    try:
        suite = build_suite(group)
    except LookupError as exc:
        print(exc)
        return 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1
