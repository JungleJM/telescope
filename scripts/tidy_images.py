#!/usr/bin/env python3
"""Delete pasted images no document mentions (D132).

    python3 scope.py images                  # what "update docs" runs last
    python3 scripts/tidy_images.py --tdd     # its tests

Quarto's visual editor puts a pasted image in `images/` beside the document,
named `paste-<n>.png`. A pasted image in an `images/` folder under `plan/` is
kept while any Markdown or Quarto document in the repository names it, by its
path from that document or from the root; otherwise it is deleted. So a
deleted document takes its pictures with it, and an image not named `paste-`
is never touched.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DOCUMENT_SUFFIXES = {".md", ".qmd"}
SKIPPED_FOLDERS = {".git", "cleanup", "dist", "node_modules"}


def pasted_images(root: Path) -> list[Path]:
    """Every `paste-*` image inside an `images/` folder under plan/."""
    plan = root / "plan"
    if not plan.is_dir():
        return []
    return sorted(
        path for path in plan.rglob("paste-*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        and "images" in path.relative_to(plan).parts[:-1]
    )


def documents(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.suffix in DOCUMENT_SUFFIXES and path.is_file()
        and not SKIPPED_FOLDERS.intersection(path.relative_to(root).parts)
    )


def spellings(image: Path, start: Path) -> set[str]:
    """How a document in `start` could name the image: plain or URL-quoted."""
    path = Path(os.path.relpath(image, start)).as_posix()
    return {path, quote(path)}


def unmentioned(root: Path) -> list[Path]:
    images = pasted_images(root)
    if not images:
        return []
    texts = [(doc.parent, doc.read_text(encoding="utf-8", errors="replace")) for doc in documents(root)]
    def mentioned(image: Path) -> bool:
        from_root = spellings(image, root)
        return any(name in text for folder, text in texts for name in from_root | spellings(image, folder))

    return [image for image in images if not mentioned(image)]


def tidy(root: Path) -> list[Path]:
    """Delete the unmentioned pasted images; return them, relative to root."""
    gone = unmentioned(root)
    for image in gone:
        image.unlink()
    return [image.relative_to(root) for image in gone]


def main(argv: list[str]) -> int:
    if argv[:1] == ["--tdd"]:
        result = unittest.main(module=__name__, argv=[sys.argv[0]], exit=False).result
        return 0 if result.wasSuccessful() else 1
    gone = tidy(ROOT)
    for path in gone:
        print(f"Deleted {path.as_posix()}")
    if not gone:
        print("No pasted image is unmentioned.")
    return 0


class TidyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "plan" / "images").mkdir(parents=True)

    def write(self, relative: str, text: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_mentioned_paste_stays_and_an_orphan_goes(self) -> None:
        kept = self.write("plan/images/paste-1.png")
        orphan = self.write("plan/images/paste-2.png")
        self.write("plan/tasklist.qmd", "Here: ![](images/paste-1.png)\n")
        self.assertEqual(tidy(self.root), [Path("plan/images/paste-2.png")])
        self.assertTrue(kept.exists())
        self.assertFalse(orphan.exists())

    def test_a_deleted_document_takes_its_pictures(self) -> None:
        image = self.write("plan/commemorating/images/paste-1.png")
        doc = self.write("plan/commemorating/history.qmd", "![](images/paste-1.png)")
        tidy(self.root)
        self.assertTrue(image.exists())
        doc.unlink()
        tidy(self.root)
        self.assertFalse(image.exists())

    def test_the_same_name_beside_another_document_does_not_keep_it(self) -> None:
        # plan/tasklist.qmd's images/paste-1.png is plan/images/paste-1.png, not this one.
        other = self.write("plan/commemorating/images/paste-1.png")
        self.write("plan/images/paste-1.png")
        self.write("plan/tasklist.qmd", "![](images/paste-1.png)")
        tidy(self.root)
        self.assertFalse(other.exists())
        self.assertTrue((self.root / "plan/images/paste-1.png").exists())

    def test_named_from_the_root_or_url_quoted_or_by_html(self) -> None:
        by_root = self.write("plan/images/paste-1.png")
        quoted = self.write("plan/images/my notes/paste-2.png")
        html = self.write("plan/images/paste-3.png")
        self.write(".claude/CLAUDE.md", "See plan/images/paste-1.png.")
        self.write("plan/a.md", '![](images/my%20notes/paste-2.png)\n<img src="images/paste-3.png">')
        self.assertEqual(tidy(self.root), [])
        self.assertTrue(by_root.exists() and quoted.exists() and html.exists())

    def test_only_pastes_in_an_images_folder_under_plan(self) -> None:
        reference = self.write("plan/images/diagram.png")
        loose = self.write("plan/paste-1.png")
        outside = self.write("images/paste-1.png")
        other = self.write("plan/images/RSV/RSV_full.yaml")
        self.assertEqual(tidy(self.root), [])
        self.assertTrue(all(p.exists() for p in (reference, loose, outside, other)))

    def test_a_mention_in_a_skipped_folder_does_not_count(self) -> None:
        image = self.write("plan/images/paste-1.png")
        self.write("cleanup/runs/notes.md", "plan/images/paste-1.png")
        tidy(self.root)
        self.assertFalse(image.exists())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
