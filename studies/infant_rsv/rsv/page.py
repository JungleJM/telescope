"""Plain-text pages: fixed width, small counts hidden, a check total at the foot."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RsvError, Settings

SUPPRESSED_PCT = "--"


def data_version(path: Path) -> str:
    """The first 8 hex of a file's SHA-256, so pages say which data made them."""
    if not path.is_file():
        return "none"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:8]


def fit(text: Any, width: int) -> str:
    """A label cut to `width`, ending in `~` when cut."""
    text = str(text)
    return text if len(text) <= width else text[: max(1, width - 1)] + "~"


def number(value: float | None, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:,.{digits}f}"


def p_text(p: float | None) -> str:
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return "p n/a"
    return "p<0.001" if p < 0.001 else f"p={p:.3f}"


class Page:
    """One page. Every count goes through `count`, which hides small ones and adds the rest to the check total."""

    def __init__(self, settings: Settings, title: str, command: str, section: str = "all visits",
                 section_filter: str = "", version: str = "") -> None:
        self.settings = settings
        self.width = int(settings.get("page_width", 48))
        self.min_cell = int(settings.get("min_cell", 0) or 0)
        self.title = title
        self.command = command
        self.section = section
        self.section_filter = section_filter
        self.version = version
        self.lines: list[str] = []
        self.check = 0

    def line(self, text: str = "") -> None:
        if len(text) > self.width:
            raise RsvError(f"Line wider than page_width {self.width}: {text!r}")
        self.lines.append(text.rstrip())

    def rule(self, char: str = "-") -> None:
        self.line(char * self.width)

    def heading(self, text: str, columns: str = "", at: int = 0) -> None:
        """A blank line, then the heading; `columns` is right-aligned to end at `at`."""
        self.line()
        if not columns:
            self.line(fit(text.upper(), self.width))
            return
        room = at - len(columns)
        self.line(f"{fit(text.upper(), room - 1):<{room}}{columns}")

    def small(self, n: int | float | None) -> bool:
        return n is not None and not (isinstance(n, float) and math.isnan(n)) and 0 < n < self.min_cell

    def count(self, n: int | float | None) -> str:
        if n is None or (isinstance(n, float) and math.isnan(n)):
            return "-"
        n = int(n)
        if self.small(n):
            return f"<{self.min_cell}"
        self.check += n
        return f"{n:,}"

    def pct(self, k: int | float | None, n: int | float | None) -> str:
        if k is None or n is None or not n:
            return "-"
        if self.small(k) or self.small(n) or self.small(n - k):
            return SUPPRESSED_PCT
        return f"{100.0 * k / n:.1f}"

    def enough(self, n: int) -> bool:
        """Whether a group is large enough to print a statistic of."""
        return not self.small(n)

    def render(self) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        head = [
            "=" * self.width,
            fit(f"RSV {self.title}", self.width),
            fit(f"section: {self.section}", self.width),
        ]
        if self.section_filter:
            head.append(fit(f"filter: {self.section_filter}", self.width))
        head.append(fit(f"data {self.version}  {stamp}", self.width))
        head.append(fit(f"cmd: python rsv {self.command}", self.width))
        if self.settings.sp:
            head.append(fit("SNEAKPEEK ONLY - NOT FOR ANALYSIS", self.width))
        if self.min_cell:
            head.append(fit(f"counts under {self.min_cell} hidden", self.width))
        head.append("=" * self.width)
        foot = ["=" * self.width, fit(f"check total {self.check:,}", self.width), "=" * self.width]
        return "\n".join(head + self.lines + foot) + "\n"

    def write(self, name: str) -> Path:
        folder = self.settings.pages_folder
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.txt"
        path.write_text(self.render(), encoding="utf-8")
        return path
