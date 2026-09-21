#!/usr/bin/env python3
"""Compatibility wrapper for the renamed YAML Manager UI."""

from __future__ import annotations

from yamlmanager import main


if __name__ == "__main__":
    raise SystemExit(main())
