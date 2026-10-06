"""Tests for the bundle builder and extractor.

Run with: python3 scripts/bundle_pullmanager.py --tdd
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bundle_extractor import (  # noqa: E402
    MANIFEST_FILENAME,
    BundleError,
    decode_payload_lines,
    extract,
    parse_payload,
    read_bundle,
    safe_relpath,
)
from bundle_pullmanager import (  # noqa: E402
    RECIPES_PATH,
    COMPANION_FILES,
    SOURCE_ROOT,
    build,
    bundled_files,
    encode_payload_lines,
    render_bundle,
    shipped_text,
    source_files,
)

# Published path -> the file it came from. Companion files live outside the
# source tree, so a published path no longer implies SOURCE_ROOT / path.
SOURCES = {published: source for source, published, _policy in bundled_files()}


def shipped_bytes(published: str) -> bytes:
    """A file as the bundle carries it, which is not always its source's bytes (D200)."""
    return shipped_text(SOURCES[published], "replace").encode("utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent

MODELS_SECTION = re.compile(
    r"^# === BEGIN FILE: pullmanager/models\.py.*?^# === END FILE: pullmanager/models\.py ===\n",
    re.S | re.M,
)


class BundleTestCase(unittest.TestCase):
    """Builds a real bundle into a scratch directory for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bundle = self.tmp / "bundle.py"
        build(self.bundle, SOURCE_ROOT)

    def rewrite_bundle(self, text: str) -> None:
        self.bundle.write_text(text, encoding="utf-8")

    def bundle_text(self) -> str:
        return self.bundle.read_text(encoding="utf-8")

    def assertBundleError(self, fragment, callable_, *args, **kwargs):
        with self.assertRaises(BundleError) as caught:
            callable_(*args, **kwargs)
        self.assertIn(fragment.lower(), str(caught.exception).lower())


class PayloadEncodingTests(unittest.TestCase):
    def test_roundtrips_exactly(self):
        for text in ("", "a", "a\n", "a\nb\n", "\n\n", "x = 1\n\ny = 2\n", "trailing   \n"):
            with self.subTest(text=text):
                self.assertEqual(decode_payload_lines(encode_payload_lines(text), "t.py"), text)

    def test_blank_lines_carry_no_trailing_space(self):
        # A trailing space would be silently eaten by whitespace-stripping
        # editors and break the hash.
        self.assertEqual(encode_payload_lines("a\n\nb"), ["# a", "#", "# b"])

    def test_marker_lookalikes_cannot_forge_a_section(self):
        sha = "0" * 64
        hostile = f"# === BEGIN FILE: evil.py SHA256: {sha} SIZE: 1 ===\nreal code\n"
        self.assertEqual(parse_payload("\n".join(encode_payload_lines(hostile))), [])

    def test_rejects_a_non_comment_payload_line(self):
        with self.assertRaises(BundleError):
            decode_payload_lines(["# fine", "not a comment"], "t.py")


class SafePathTests(unittest.TestCase):
    def test_accepts_normal_relative_path(self):
        self.assertEqual(safe_relpath("pullmanager/models.py"), "pullmanager/models.py")

    def test_rejects_unsafe_paths(self):
        cases = [
            ("/etc/passwd", "absolute"),
            ("../../etc/passwd", "traversal"),
            ("pullmanager/../../x.py", "traversal"),
            ("C:/Windows/system32", "drive"),
            ("pullmanager\\models.py", "backslash"),
            ("", "empty"),
            ("  spaced/x.py  ", "padded"),
        ]
        for raw, fragment in cases:
            with self.subTest(path=raw):
                with self.assertRaises(BundleError) as caught:
                    safe_relpath(raw)
                self.assertIn(fragment.lower(), str(caught.exception).lower())


class BuildTests(BundleTestCase):
    def test_includes_every_source_file(self):
        sections, manifest = read_bundle(self.bundle)
        self.assertEqual({section["path"] for section in sections}, set(SOURCES))
        self.assertEqual(manifest["file_count"], len(SOURCES))

    def test_carries_yaml_manager_and_its_data(self):
        # The split step runs on the VM, so YAML Manager and the files it
        # reads travel with the runtime.
        sections, _ = read_bundle(self.bundle)
        published = {section["path"] for section in sections}
        for _, expected, _policy in COMPANION_FILES:
            with self.subTest(path=expected):
                self.assertIn(expected, published)

    def test_carries_the_stock_files_artifacts_copies(self):
        # D89: HOW_TO.md is not Python, and still has to reach the VM.
        sections, _ = read_bundle(self.bundle)
        published = {section["path"] for section in sections}
        self.assertIn("stock/HOW_TO.md", published)
        self.assertIn("utils/client/viewparquets.py", published)
        self.assertIn("utils/manager/clear_projects_db.py", published)
        self.assertIn("stock/stock.yaml", published)

    def test_companion_paths_let_makeyaml_find_its_own_defaults(self):
        # makeYaml's defaults are beside the code (D111): the dictionary must be
        # published where its default looks, or it breaks once extracted.
        import makeYaml

        published = {p for _, p, _policy in COMPANION_FILES}
        self.assertIn("scripts/makeYaml.py", published)
        self.assertIn(makeYaml.CORE_DEFAULTS["datadictionary"], published)

    def test_the_transcription_viewer_is_held_back(self):
        # D200: it stays on the Mac, and nothing shipped names it.
        sections, _ = read_bundle(self.bundle)
        published = {section["path"] for section in sections}
        self.assertTrue((SOURCE_ROOT / "utils" / "client" / "transcription_viewer.py").is_file())
        self.assertNotIn("utils/client/transcription_viewer.py", published)
        for section in sections:
            with self.subTest(path=section["path"]):
                self.assertIsNone(re.search(r"transcri|screenshot", section["content"], re.IGNORECASE))

    def test_the_shipped_dictionary_is_the_same_data_without_screenshot_notes(self):
        # D200: comments dropped and descriptions reworded; tables, columns and
        # types as the Mac's.
        import makeYaml

        source = makeYaml.load_yaml(SOURCES["reference/datadictionary.yaml"])
        target = self.tmp / "dictionary.yaml"
        target.write_bytes(shipped_bytes("reference/datadictionary.yaml"))
        shipped = makeYaml.load_yaml(target)
        self.assertEqual(set(shipped), set(source))
        for table, entry in source.items():
            if isinstance(entry, dict) and isinstance(entry.get("columns"), dict):
                with self.subTest(table=table):
                    self.assertEqual(
                        {name: col.get("type") for name, col in entry["columns"].items()},
                        {name: col.get("type") for name, col in shipped[table]["columns"].items()},
                    )
        self.assertIn("(the rest is not recorded)", target.read_text(encoding="utf-8"))

    def test_a_build_that_would_mention_screenshots_stops_naming_the_line(self):
        root = self.tmp / "src"
        (root / "pullmanager").mkdir(parents=True)
        (root / "pullmanager" / "notes.py").write_text("# Copied off by screenshot.\n", encoding="utf-8")
        with self.assertRaises(BundleError) as caught:
            render_bundle(root)
        self.assertIn("pullmanager/notes.py:1", str(caught.exception))

    def test_rebuild_is_byte_identical(self):
        self.assertEqual(render_bundle(), render_bundle())

    def test_bundle_is_valid_python(self):
        ast.parse(self.bundle_text())

    def test_payload_matches_source_bytes(self):
        sections, _ = read_bundle(self.bundle)
        for section in sections:
            with self.subTest(path=section["path"]):
                self.assertEqual(
                    section["content"].encode("utf-8"),
                    shipped_bytes(section["path"]),
                )


class ExtractionPolicyTests(BundleTestCase):
    """A re-extraction must never quietly destroy work done on the VM."""

    def extract_twice(self, rel, edited):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        (target / rel).write_text(edited, encoding="utf-8")
        extract(self.bundle, target)
        return target

    def test_recipes_travel_for_now(self):
        # D187: recipes.yaml ships where makeYaml's default finds it, as the
        # Mac's copy, so Author on the VM lists the same recipes.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        shipped = target / "reference" / "recipes.yaml"
        self.assertEqual(shipped.read_bytes(), RECIPES_PATH.read_bytes())

    def test_the_template_package_list_and_walkthrough_travel(self):
        # D199: what a developer on the VM reads beside the code, and the
        # template a new draft starts from, each where makeYaml looks for it.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        import makeYaml

        for key in ("template", "vm_plugins"):
            with self.subTest(core=key):
                shipped = target / makeYaml.CORE_DEFAULTS[key]
                self.assertEqual(shipped.read_bytes(), makeYaml.core_path(key).read_bytes())
        self.assertEqual(
            (target / "HowThisRepoWorks.md").read_bytes(),
            (REPO_ROOT / "HowThisRepoWorks.md").read_bytes(),
        )

    def test_authoring_stays_on_the_mac(self):
        # D49: the browser UI does not travel (recipes do, D187; the template
        # does, D199).
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        for rel in (
            "YAMLs/recipes.yaml",
            "YAMLs/template.yaml",
            "YAMLs/template.yaml.example",
            "scripts/yamlmanager.py",
            "scripts/yamlmanager_backend.py",
        ):
            with self.subTest(path=rel):
                self.assertFalse((target / rel).exists())

    def pretend_previous_release_shipped(self, target: Path, rel: str, content: str) -> None:
        """Make `target` look like an extraction of a bundle that shipped `rel`."""
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(content, encoding="utf-8")
        record = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        record["files"] = [e for e in record["files"] if e["path"] != rel] + [{
            "path": rel,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size": len(content.encode("utf-8")),
        }]
        (target / MANIFEST_FILENAME).write_text(json.dumps(record), encoding="utf-8")

    def test_an_edited_file_the_bundle_stops_shipping_is_kept(self):
        # The update that dropped recipes.yaml must not take a VM edit with it.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        self.pretend_previous_release_shipped(target, "YAMLs/recipes.yaml", "recipes: []\n")
        (target / "YAMLs/recipes.yaml").write_text("# edited on the VM\n", encoding="utf-8")
        extract(self.bundle, target)
        self.assertFalse((target / "YAMLs/recipes.yaml").exists())
        self.assertEqual(
            (target / "YAMLs/recipes.yaml.local").read_text(encoding="utf-8"),
            "# edited on the VM\n",
        )

    def test_an_untouched_file_the_bundle_stops_shipping_is_removed(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        self.pretend_previous_release_shipped(target, "YAMLs/recipes.yaml", "recipes: []\n")
        extract(self.bundle, target)
        self.assertFalse((target / "YAMLs/recipes.yaml").exists())
        self.assertFalse((target / "YAMLs/recipes.yaml.local").exists())

    def test_a_file_that_only_changed_between_releases_leaves_no_local_copy(self):
        # Changed upstream, never touched here: not an edit, nothing to keep.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        self.pretend_previous_release_shipped(target, "reference/datadictionary.yaml", "# last release\n")
        extract(self.bundle, target)
        self.assertFalse((target / "reference/datadictionary.yaml.local").exists())
        self.assertEqual(
            (target / "reference/datadictionary.yaml").read_bytes(),
            shipped_bytes("reference/datadictionary.yaml"),
        )

    def test_a_kept_copy_survives_the_next_update(self):
        target = self.extract_twice("reference/datadictionary.yaml", "# edited on the VM\n")
        extract(self.bundle, target)
        self.assertEqual(
            (target / "reference/datadictionary.yaml.local").read_text(encoding="utf-8"),
            "# edited on the VM\n",
        )

    def test_unbundled_files_inside_the_tree_do_not_survive(self):
        # The extracted tree is wholly managed: it is swapped, not merged. A
        # file of your own placed inside it is gone on the next update, which
        # is why your templates belong beside the tree rather than in it.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        stray = target / "reference" / "UCPatients.yaml"
        stray.write_text("# my pull\n", encoding="utf-8")
        extract(self.bundle, target)
        self.assertFalse(stray.exists())

    def test_files_beside_the_tree_are_untouched(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        mine = self.tmp / "YAMLs"
        mine.mkdir(exist_ok=True)
        (mine / "UCPatients.yaml").write_text("# my pull\n", encoding="utf-8")
        extract(self.bundle, target)
        self.assertEqual((mine / "UCPatients.yaml").read_text(encoding="utf-8"), "# my pull\n")

    def test_a_replaced_file_is_updated_but_the_old_one_is_kept(self):
        target = self.extract_twice("reference/datadictionary.yaml", "# edited on the VM\n")
        shipped = shipped_bytes("reference/datadictionary.yaml")
        self.assertEqual((target / "reference/datadictionary.yaml").read_bytes(), shipped)
        self.assertEqual(
            (target / "reference/datadictionary.yaml.local").read_text(encoding="utf-8"),
            "# edited on the VM\n",
        )

    def test_unmodified_files_leave_no_local_copy(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        extract(self.bundle, target)
        self.assertEqual(list(target.rglob("*.local")), [])

    def test_code_is_always_replaced(self):
        target = self.extract_twice("pullmanager/models.py", "# tampered\n")
        self.assertEqual(
            (target / "pullmanager/models.py").read_bytes(),
            SOURCES["pullmanager/models.py"].read_bytes(),
        )


class TamperTests(BundleTestCase):
    def test_detects_modified_payload(self):
        self.rewrite_bundle(
            self.bundle_text().replace("# MANIFEST_VERSION = 1", "# MANIFEST_VERSION = 2", 1)
        )
        self.assertBundleError("mismatch", read_bundle, self.bundle)

    def test_detects_dropped_section(self):
        self.rewrite_bundle(MODELS_SECTION.sub("", self.bundle_text()))
        self.assertBundleError("no payload section", read_bundle, self.bundle)

    def test_detects_duplicate_section(self):
        text = self.bundle_text()
        match = MODELS_SECTION.search(text)
        self.assertIsNotNone(match)
        self.rewrite_bundle(text + match.group(0))
        self.assertBundleError("duplicate", read_bundle, self.bundle)

    def test_detects_section_absent_from_manifest(self):
        body = "print('surprise')\n"
        raw = body.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        extra = (
            f"# === BEGIN FILE: pullmanager/extra.py SHA256: {sha} SIZE: {len(raw)} ===\n"
            + "\n".join(encode_payload_lines(body))
            + "\n# === END FILE: pullmanager/extra.py ===\n"
        )
        self.rewrite_bundle(self.bundle_text() + extra)
        self.assertBundleError("absent from its manifest", read_bundle, self.bundle)

    def test_detects_size_lie(self):
        self.rewrite_bundle(re.sub(r'("size": )\d+', r"\g<1>999999", self.bundle_text(), count=1))
        self.assertBundleError("size mismatch", read_bundle, self.bundle)

    def test_detects_unterminated_section(self):
        self.rewrite_bundle(
            self.bundle_text().replace("# === END FILE: pullmanager/models.py ===\n", "", 1)
        )
        self.assertBundleError("unterminated section", read_bundle, self.bundle)

    def test_detects_mismatched_end_marker(self):
        self.rewrite_bundle(
            self.bundle_text().replace(
                "# === END FILE: pullmanager/models.py ===",
                "# === END FILE: pullmanager/cli.py ===",
                1,
            )
        )
        self.assertBundleError("does not match", read_bundle, self.bundle)

    def test_detects_changed_bundle_code(self):
        # The prelude verifies and extracts; a change to it must not pass as
        # the same bundle (D64).
        self.rewrite_bundle(self.bundle_text().replace(
            'DEFAULT_TARGET = "pullmanager_runtime"', 'DEFAULT_TARGET = "elsewhere"', 1
        ))
        self.assertBundleError("own code", read_bundle, self.bundle)

    def test_the_id_changes_with_the_bundle_code(self):
        from bundle_pullmanager import render_bundle
        import bundle_pullmanager

        before = read_bundle(self.bundle)[1]["content_id"]
        original = bundle_pullmanager.BUNDLE_HEADER
        bundle_pullmanager.BUNDLE_HEADER = original + "# a change to the bundle's own code\n"
        self.addCleanup(setattr, bundle_pullmanager, "BUNDLE_HEADER", original)
        self.rewrite_bundle(render_bundle(SOURCE_ROOT))
        self.assertNotEqual(read_bundle(self.bundle)[1]["content_id"], before)

    def test_requires_the_embedded_manifest_block(self):
        self.rewrite_bundle(
            re.sub(r"^BUNDLE_MANIFEST_JSON = r'''.*?'''$", "", self.bundle_text(), flags=re.S | re.M)
        )
        self.assertBundleError("BUNDLE_MANIFEST_JSON", read_bundle, self.bundle)


class ExtractionTests(BundleTestCase):
    def test_writes_every_file_plus_its_manifest(self):
        target = self.tmp / "runtime"
        written = extract(self.bundle, target)
        self.assertEqual(len(written), len(SOURCES))
        self.assertTrue((target / MANIFEST_FILENAME).is_file())
        for rel in written:
            with self.subTest(path=rel):
                self.assertTrue((target / rel).is_file())

    def test_extracted_bytes_match_sources(self):
        target = self.tmp / "runtime"
        for rel in extract(self.bundle, target):
            with self.subTest(path=rel):
                self.assertEqual((target / rel).read_bytes(), shipped_bytes(rel))

    def test_leaves_no_scratch_directories(self):
        extract(self.bundle, self.tmp / "runtime")
        self.assertEqual(
            sorted(path.name for path in self.tmp.iterdir()),
            ["bundle.py", "runtime"],
        )

    def test_replaces_a_previous_extraction(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        stale = target / "pullmanager" / "stale.py"
        stale.write_text("# left over from an older bundle\n", encoding="utf-8")
        extract(self.bundle, target)
        self.assertFalse(stale.exists())

    def test_refuses_a_foreign_directory_without_force(self):
        target = self.tmp / "my_work"
        target.mkdir()
        keeper = target / "important.txt"
        keeper.write_text("do not delete\n", encoding="utf-8")
        self.assertBundleError("--force", extract, self.bundle, target)
        self.assertTrue(keeper.is_file())

    def test_force_replaces_a_foreign_directory(self):
        target = self.tmp / "my_work"
        target.mkdir()
        (target / "important.txt").write_text("expendable\n", encoding="utf-8")
        extract(self.bundle, target, force=True)
        self.assertFalse((target / "important.txt").exists())
        self.assertTrue((target / MANIFEST_FILENAME).is_file())

    def test_failed_extraction_leaves_the_previous_runtime_intact(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        good = (target / "pullmanager" / "models.py").read_bytes()

        self.rewrite_bundle(
            self.bundle_text().replace("# MANIFEST_VERSION = 1", "# MANIFEST_VERSION = 99", 1)
        )
        self.assertBundleError("mismatch", extract, self.bundle, target)
        self.assertEqual((target / "pullmanager" / "models.py").read_bytes(), good)

    def test_refuses_a_file_as_target(self):
        target = self.tmp / "afile"
        target.write_text("not a directory\n", encoding="utf-8")
        self.assertBundleError("not a directory", extract, self.bundle, target)


class LauncherTests(BundleTestCase):
    """D63, D123: `scope.py` in the working folder runs the extracted copy."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "QueryGenerator"
        self.work.mkdir()
        shutil.copyfile(self.bundle, self.work / "bundle.py")

    def run_python(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args], capture_output=True, text=True, cwd=self.work
        )

    def run_bundle_answering(self, answer, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(self.work / "bundle.py"), *args],
            input=answer, capture_output=True, text=True, cwd=cwd or self.work,
        )

    def test_bundle_alone_shows_the_id_then_extracts_on_yes(self):
        # D64: one step on the VM: look at the number, answer y.
        run = self.run_bundle_answering("y\n")
        self.assertEqual(run.returncode, 0, run.stderr)
        before_prompt = run.stdout.split("[y/N]")[0]
        self.assertIn("content_id: ", before_prompt)
        self.assertTrue((self.work / "pullmanager_runtime" / "pullmanager.py").is_file())
        self.assertTrue((self.work / "scope.py").is_file())
        self.assertNotIn("extracted  ", run.stdout)

    def test_anything_but_yes_extracts_nothing(self):
        for answer in ("n\n", "\n", ""):
            with self.subTest(answer=answer):
                run = self.run_bundle_answering(answer)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("Nothing extracted", run.stdout)
                self.assertFalse((self.work / "pullmanager_runtime").exists())
                self.assertFalse((self.work / "scope.py").exists())

    def test_it_extracts_beside_itself_wherever_it_is_run_from(self):
        run = self.run_bundle_answering("y\n", cwd=self.tmp)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.work / "pullmanager_runtime").is_dir())
        self.assertFalse((self.tmp / "pullmanager_runtime").exists())

    # ------------------------------------------------------------ D147

    def lock(self, pull: str, age: float = 5, split: bool = False) -> None:
        import time

        folder = self.work / "runs" / pull / ("split" if split else "")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pullmanifest.lock").write_text(json.dumps(
            {"pid": 4242, "machine": "VM", "started": time.time() - 600,
             "heartbeat": time.time() - age}), encoding="utf-8")

    def test_it_refuses_while_a_pull_executes_in_either_layout(self):
        # A pull executing from the old software would keep running it.
        for split in (False, True):
            with self.subTest(old_layout=split):
                shutil.rmtree(self.work / "runs", ignore_errors=True)
                self.lock("UC_Visits", split=split)
                run = self.run_bundle_answering("y\n")
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("UC_Visits is executing now", run.stderr)
                self.assertNotIn("[y/N]", run.stdout)  # refused before asking
                self.assertFalse((self.work / "pullmanager_runtime").exists())
                self.assertFalse((self.work / "scope.py").exists())

    def test_a_stale_lock_does_not_stop_it(self):
        self.lock("UC_Visits", age=121)
        run = self.run_bundle_answering("y\n")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.work / "pullmanager_runtime" / "pullmanager.py").is_file())

    def test_it_says_to_close_open_windows_and_what_it_removed(self):
        first = self.run_bundle_answering("y\n")
        self.assertIn("Close the app and the utilities first", first.stdout.split("[y/N]")[0])
        self.assertNotIn("Removed the previous", first.stdout)  # nothing there before
        stale = self.work / "pullmanager_runtime" / "pullmanager" / "leftover.py"
        stale.write_text("# from an older version\n", encoding="utf-8")
        second = self.run_bundle_answering("y\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Removed the previous pullmanager_runtime (", second.stdout)
        self.assertFalse(stale.exists())

    def test_the_version_names_the_bundle(self):
        _, manifest = read_bundle(self.bundle)
        self.run_bundle_answering("y\n")
        run = self.run_python("scope.py", "--version")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f"bundle {manifest['content_id'][:8]}", run.stdout)

    def test_its_lock_rule_is_pullmanagers(self):
        import bundle_extractor

        sys.path.insert(0, str(SOURCE_ROOT))
        from pullmanager import lock

        self.assertEqual(bundle_extractor.PULL_LOCK_FILENAME, lock.lock_path(Path("pullmanifest.yaml")).name)
        self.assertEqual(bundle_extractor.PULL_LOCK_STALE_SECONDS, lock.STALE_SECONDS)

    def test_a_tampered_bundle_asks_nothing(self):
        text = (self.work / "bundle.py").read_text(encoding="utf-8")
        (self.work / "bundle.py").write_text(
            text.replace("# MANIFEST_VERSION = 1", "# MANIFEST_VERSION = 99", 1), encoding="utf-8"
        )
        run = self.run_bundle_answering("y\n")
        self.assertEqual(run.returncode, 2)
        self.assertNotIn("[y/N]", run.stdout)
        self.assertFalse((self.work / "pullmanager_runtime").exists())

    def test_makebundle_builds_the_bundle(self):
        out = self.tmp / "made" / "bundle.py"
        run = subprocess.run(
            # --no-queue: a test must never carry, or empty, the real queue.
            [sys.executable, str(REPO_ROOT / "makebundle.py"), "--out", str(out), "--no-queue"],
            capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("content_id: ", run.stdout)
        self.assertEqual(out.read_bytes(), self.bundle.read_bytes())

    def test_extract_with_no_folder_uses_the_default_and_writes_the_launcher(self):
        run = self.run_python("bundle.py", "--extract")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.work / "pullmanager_runtime" / "pullmanager.py").is_file())
        self.assertIn("scope.py", run.stdout)
        version = self.run_python("scope.py", "--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("pullmanager", version.stdout)

    def test_the_launcher_works_beside_a_folder_of_its_own_name(self):
        # Extracted as `pullmanager`, the folder shares the launcher's name.
        self.assertEqual(self.run_python("bundle.py", "--extract", "pullmanager").returncode, 0)
        version = self.run_python("scope.py", "--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("pullmanager", version.stdout)

    def test_the_launcher_is_rewritten_by_every_extraction(self):
        self.run_python("bundle.py", "--extract")
        (self.work / "scope.py").write_text("print('edited')\n", encoding="utf-8")
        self.run_python("bundle.py", "--extract", "elsewhere")
        text = (self.work / "scope.py").read_text(encoding="utf-8")
        self.assertIn("'elsewhere'", text)
        self.assertNotIn("edited", text)

    def test_the_old_launcher_goes_but_a_pullmanager_py_of_your_own_stays(self):
        from bundle_extractor import LAUNCHER_TEMPLATE

        old = self.work / "pullmanager.py"
        old.write_text(LAUNCHER_TEMPLATE.format(folder="pullmanager_runtime"), encoding="utf-8")
        run = self.run_python("bundle.py", "--extract")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(old.exists())
        self.assertTrue((self.work / "scope.py").is_file())
        old.write_text("print('mine')\n", encoding="utf-8")
        self.run_python("bundle.py", "--extract")
        self.assertEqual(old.read_text(encoding="utf-8"), "print('mine')\n")

    def test_utils_py_is_written_beside_scope_py_and_lists_the_utilities(self):
        # D124: `python utils.py` on the VM; the viewer moved to utils/.
        self.assertEqual(self.run_python("bundle.py", "--extract").returncode, 0)
        listed = self.run_python("utils.py", "--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("viewparquets.py", listed.stdout.split())
        # Held back for now (D200).
        self.assertNotIn("transcription_viewer.py", listed.stdout.split())

    def test_a_missing_folder_says_to_extract_again(self):
        self.run_python("bundle.py", "--extract")
        shutil.rmtree(self.work / "pullmanager_runtime")
        run = self.run_python("scope.py", "--version")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("python bundle.py --extract pullmanager_runtime", run.stderr)


class EndToEndTests(BundleTestCase):
    def run_python(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args], capture_output=True, text=True
        )

    def test_extracted_runtime_passes_its_own_tests(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        proc = self.run_python(str(target / "pullmanager.py"), "--tdd")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("OK", proc.stderr + proc.stdout)

    def test_bundle_cli_verifies_and_extracts(self):
        verify = self.run_python(str(self.bundle), "--verify-bundle")
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("verified", verify.stdout)

        target = self.tmp / "runtime"
        run = self.run_python(str(self.bundle), "--extract", str(target))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((target / "pullmanager" / "manifest.py").is_file())

    def test_bundle_cli_reports_tampering_without_extracting(self):
        self.rewrite_bundle(
            self.bundle_text().replace("# MANIFEST_VERSION = 1", "# MANIFEST_VERSION = 7", 1)
        )
        target = self.tmp / "runtime"
        proc = self.run_python(str(self.bundle), "--extract", str(target))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BUNDLE ERROR", proc.stderr)
        self.assertFalse(target.exists())

    def test_runtime_reads_a_freshly_generated_split_manifest(self):
        template = REPO_ROOT / "YAMLs" / "manager_test_cases" / "01_valid_basic.yaml"
        recipes = RECIPES_PATH
        self.assertTrue(template.is_file(), f"fixture template missing: {template}")

        split_dir = self.tmp / "split"
        gen = self.run_python(
            str(REPO_ROOT / "scripts" / "makeYaml.py"),
            "--template", str(template),
            "--recipes", str(recipes),
            "--export-split",
            "--out-dir", str(split_dir),
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)

        target = self.tmp / "runtime"
        extract(self.bundle, target)
        proc = self.run_python(
            str(target / "pullmanager.py"), str(split_dir / "pullmanifest.yaml")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Sessions:", proc.stdout)

    def test_the_apps_author_adjusts_a_transfer_yaml_on_the_vm(self):
        # D93, D94: in the extracted tree the model opens a transfer YAML from
        # the working folder, saves the change as an intake, and exports the
        # transfer again beside scope.py. It finds the shipped recipes (D187).
        work = self.tmp / "work"
        shutil.copytree(REPO_ROOT / "YAMLs" / "manager_test_cases", work)
        export = self.run_python(
            str(REPO_ROOT / "scripts" / "makeYaml.py"),
            "--template", str(work / "01_valid_basic.yaml"),
            "--recipes", str(RECIPES_PATH),
            "--export-transfer", "--out", str(work / "Basic_transfer.yaml"),
        )
        self.assertEqual(export.returncode, 0, export.stdout + export.stderr)
        target = work / "pullmanager_runtime"
        extract(self.bundle, target)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "import yamlmanager_model as m\n"
            "ws = m.Workspace.default()\n"
            "print('home', ws.home.name, 'recipes', ws.has_recipes)\n"
            "d = m.Draft.open(ws, ws.home / 'Basic_transfer.yaml')\n"
            "d.project_db = 'PROJECTD777'\n"
            "saved = d.save(); print('saved', saved.ok, saved.path.relative_to(ws.home).as_posix())\n"
            "ok, message, path = d.export_transfer()\n"
            "print('exported', ok, path.relative_to(ws.home).as_posix() if path else message)\n"
        )
        proc = subprocess.run([sys.executable, "-c", script, str(target / "scripts")],
                              capture_output=True, text=True, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("home work recipes True", proc.stdout)
        # D162: on the VM the saved file is the blueprint Run takes, and the
        # older copy it was opened from is moved aside, so one copy is left.
        self.assertIn("saved True YAMLs/temp/Manager_Valid_Basic_blueprint.yaml", proc.stdout)
        exported = proc.stdout.split("exported True ")[-1].strip()
        self.assertEqual(exported, "YAMLs/temp/Manager_Valid_Basic_blueprint.yaml", proc.stdout)
        self.assertIn("PROJECTD777", (work / exported).read_text(encoding="utf-8"))
        self.assertFalse((work / "Basic_transfer.yaml").exists())
        self.assertTrue((work / "YAMLs" / "temp" / "replaced" / "Basic_transfer.yaml").is_file())

    def test_the_vm_pathway_from_a_transfer_yaml(self):
        # D49 end to end. On the Mac: export a transfer YAML. On the VM, with
        # no recipes anywhere (the shipped copy removed, D187): split it from
        # the working directory by a typed relative path, then dry-run the
        # manifest.
        work = self.tmp / "work"
        shutil.copytree(REPO_ROOT / "YAMLs" / "manager_test_cases", work)
        export = self.run_python(
            str(REPO_ROOT / "scripts" / "makeYaml.py"),
            "--template", str(work / "02_valid_multipliers_batching.yaml"),
            "--recipes", str(RECIPES_PATH),
            "--export-transfer", "--out", str(work / "IBD_transfer.yaml"),
        )
        self.assertEqual(export.returncode, 0, export.stdout + export.stderr)

        target = self.tmp / "runtime"
        extract(self.bundle, target)
        (target / "reference" / "recipes.yaml").unlink()
        self.assertFalse((target / "YAMLs" / "recipes.yaml").exists())
        split = subprocess.run(
            [sys.executable, str(target / "scripts" / "makeYaml.py"),
             "--template", "IBD_transfer.yaml", "--export-split", "--out-dir", "split"],
            capture_output=True, text=True, cwd=work,
        )
        self.assertEqual(split.returncode, 0, split.stdout + split.stderr)
        dry = subprocess.run(
            [sys.executable, str(target / "pullmanager.py"), "--dry-run",
             "split/pullmanifest.yaml", "--out-dir", "sql"],
            capture_output=True, text=True, cwd=work,
        )
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertTrue(any((work / "sql").rglob("*.sql")))

    def test_a_template_that_uses_recipes_validates_with_the_shipped_recipes(self):
        # D187: with recipes.yaml shipped, a template naming recipes validates
        # on the VM, with no --recipes typed.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        work = self.tmp / "work"
        shutil.copytree(REPO_ROOT / "YAMLs" / "manager_test_cases", work)
        proc = subprocess.run(
            [sys.executable, str(target / "scripts" / "makeYaml.py"),
             "--template", "01_valid_basic.yaml", "--validate"],
            capture_output=True, text=True, cwd=work,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("recipes_not_found", proc.stdout)

    def test_a_template_that_still_uses_recipes_is_refused_with_a_pointer(self):
        # Without a recipes file (the shipped copy removed), still refused.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        (target / "reference" / "recipes.yaml").unlink()
        work = self.tmp / "work"
        shutil.copytree(REPO_ROOT / "YAMLs" / "manager_test_cases", work)
        proc = subprocess.run(
            [sys.executable, str(target / "scripts" / "makeYaml.py"),
             "--template", "01_valid_basic.yaml", "--validate"],
            capture_output=True, text=True, cwd=work,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("recipes_not_found", proc.stdout)
        self.assertIn("--export-transfer", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_running_without_a_template_says_what_to_pass(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        proc = self.run_python(str(target / "scripts" / "makeYaml.py"), "--validate")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--export-transfer", proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_runtime_reads_a_batch_product_manifest(self):
        template = REPO_ROOT / "YAMLs" / "manager_test_cases" / "02_valid_multipliers_batching.yaml"
        recipes = RECIPES_PATH
        split_dir = self.tmp / "split"
        gen = self.run_python(
            str(REPO_ROOT / "scripts" / "makeYaml.py"),
            "--template", str(template),
            "--recipes", str(recipes),
            "--export-split",
            "--out-dir", str(split_dir),
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)

        target = self.tmp / "runtime"
        extract(self.bundle, target)
        proc = self.run_python(
            str(target / "pullmanager.py"), str(split_dir / "pullmanifest.yaml")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # state[LA, MS] x sex[Female, Male] is four runs per session.
        self.assertIn("LA-Female", proc.stdout)
        self.assertIn("MS-Male", proc.stdout)


class TransferYamlTests(unittest.TestCase):
    """makebundle.py yaml=...: transfer YAMLs travel in the bundle and land
    beside scope.py on the VM, ready to run."""

    TRANSFER = "transfer:\n  from_template: IBD_Ancestry_temp.yaml\nproject_folder: IBD Ancestry\n" \
               "upload_cohorts:\n- name: Meds\n  file_loc: data/Meds/ibd/IBD_Meds.parquet\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.ibd = self.repo / "IBD_Ancestry_transfer.yaml"
        self.ibd.write_text(self.TRANSFER, encoding="utf-8")
        (self.repo / "Celiac_transfer.yaml").write_text("project_folder: Celiac\n", encoding="utf-8")
        self.vm = self.tmp / "vm"
        self.vm.mkdir()
        self.bundle = self.vm / "bundle.py"

    def build_with(self, *names):
        from bundle_pullmanager import find_transfer

        return build(self.bundle, SOURCE_ROOT, [find_transfer(n, self.repo) for n in names])[1]

    def unpack_quietly(self):
        import contextlib
        import io

        from bundle_extractor import unpack

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            unpack(self.bundle, self.vm / "pullmanager_runtime", quiet=True)
        return out.getvalue()

    def test_each_named_transfer_lands_beside_pullmanager_py(self):
        self.build_with("IBD_Ancestry", "Celiac.yaml")
        out = self.unpack_quietly()
        self.assertEqual((self.vm / "IBD_Ancestry_transfer.yaml").read_bytes(), self.ibd.read_bytes())
        self.assertTrue((self.vm / "Celiac_transfer.yaml").is_file())
        self.assertTrue((self.vm / "scope.py").is_file())
        # Not inside the extracted folder, which every update replaces.
        self.assertEqual(list((self.vm / "pullmanager_runtime").rglob("*_transfer.yaml")), [])
        self.assertIn("IBD_Ancestry_transfer.yaml", out)

    def test_a_different_copy_already_there_is_kept_aside(self):
        # Edited on the VM, say: not simply destroyed.
        (self.vm / "IBD_Ancestry_transfer.yaml").write_text("edited on the VM\n", encoding="utf-8")
        self.build_with("IBD_Ancestry")
        out = self.unpack_quietly()
        self.assertEqual((self.vm / "IBD_Ancestry_transfer.yaml.local").read_text(encoding="utf-8"),
                         "edited on the VM\n")
        self.assertEqual((self.vm / "IBD_Ancestry_transfer.yaml").read_bytes(), self.ibd.read_bytes())
        self.assertIn("kept as IBD_Ancestry_transfer.yaml.local", out)

    def test_the_same_copy_is_not_set_aside(self):
        self.build_with("IBD_Ancestry")
        self.unpack_quietly()
        self.unpack_quietly()
        self.assertFalse((self.vm / "IBD_Ancestry_transfer.yaml.local").exists())

    def test_it_is_verified_like_every_other_file(self):
        self.build_with("IBD_Ancestry")
        text = self.bundle.read_text(encoding="utf-8").replace("from_template: IBD", "from_template: XBD")
        self.bundle.write_text(text, encoding="utf-8")
        with self.assertRaises(BundleError):
            self.unpack_quietly()
        self.assertFalse((self.vm / "IBD_Ancestry_transfer.yaml").exists())

    def test_the_content_id_says_which_transfers_it_carries(self):
        plain = build(self.tmp / "plain.py", SOURCE_ROOT)[1]["content_id"]
        self.assertNotEqual(self.build_with("IBD_Ancestry")["content_id"], plain)

    def test_names_are_read_however_they_are_typed(self):
        from bundle_pullmanager import find_transfer, transfer_names

        # The shell splits "yaml=IBD_Ancestry, Celiac.yaml" at the space.
        self.assertEqual(transfer_names(["yaml=IBD_Ancestry,", "Celiac.yaml"]), ["IBD_Ancestry", "Celiac.yaml"])
        self.assertEqual(transfer_names(["yaml=IBD_Ancestry,Celiac"]), ["IBD_Ancestry", "Celiac"])
        for name in ("IBD_Ancestry", "IBD_Ancestry.yaml", "IBD_Ancestry_transfer.yaml", "ibd_ancestry"):
            with self.subTest(name=name):
                self.assertEqual(find_transfer(name, self.repo).name, "IBD_Ancestry_transfer.yaml")

    def test_a_missing_transfer_lists_the_ones_there_are(self):
        from bundle_pullmanager import find_transfer

        with self.assertRaises(BundleError) as caught:
            find_transfer("Crohns", self.repo)
        self.assertIn("No Crohns_blueprint.yaml", str(caught.exception))
        self.assertIn("Celiac_transfer.yaml, IBD_Ancestry_transfer.yaml", str(caught.exception))

    def test_makebundle_takes_yaml_equals(self):
        out = self.tmp / "out.py"
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "makebundle.py"), "--out", str(out), "yaml=Nope", "--no-queue"],
            capture_output=True, text=True, cwd=self.tmp,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("No Nope_blueprint.yaml", proc.stderr)
        self.assertFalse(out.exists())

    def blueprint(self) -> Path:
        path = self.repo / "IBD_Ancestry_blueprint.yaml"
        path.write_text(self.TRANSFER.replace("from_template: IBD_Ancestry_temp.yaml",
                                              "from_template: IBD_Ancestry_intake.yaml"), encoding="utf-8")
        return path

    def test_a_blueprint_is_found_before_a_transfer_yaml_of_the_same_project(self):
        from bundle_pullmanager import find_transfer

        self.blueprint()
        for name in ("IBD_Ancestry", "IBD_Ancestry_blueprint.yaml", "IBD_Ancestry_transfer"):
            with self.subTest(name=name):
                self.assertEqual(find_transfer(name, self.repo).name, "IBD_Ancestry_blueprint.yaml")

    def test_a_blueprint_lands_in_yamls_temp_and_its_upload_still_resolves(self):
        # D162: the VM's one working copy is in YAMLs/temp; its file_loc, written
        # from the repository root, must reach the same file from there (D103).
        self.blueprint()
        self.build_with("IBD_Ancestry_blueprint")
        self.unpack_quietly()
        placed = self.vm / "YAMLs" / "temp" / "IBD_Ancestry_blueprint.yaml"
        self.assertTrue(placed.is_file())
        self.assertFalse((self.vm / "IBD_Ancestry_blueprint.yaml").exists())
        import makeYaml

        loc = makeYaml.load_yaml(placed)["upload_cohorts"][0]["file_loc"]
        self.assertEqual((placed.parent / loc).resolve(),
                         (self.vm / "data/Meds/ibd/IBD_Meds.parquet").resolve())

    def test_older_copies_of_the_project_are_moved_to_replaced(self):
        # D162: a transfer YAML beside scope.py, an intake saved on the VM and a
        # changed working blueprint all go to replaced/; another project's stay.
        self.blueprint()
        temp = self.vm / "YAMLs" / "temp"
        temp.mkdir(parents=True)
        (self.vm / "IBD_Ancestry_transfer.yaml").write_text("old transfer\n", encoding="utf-8")
        (temp / "IBD_Ancestry_intake.yaml").write_text("saved on the VM\n", encoding="utf-8")
        (temp / "IBD_Ancestry_blueprint.yaml").write_text("changed on the VM\n", encoding="utf-8")
        (self.vm / "Celiac_transfer.yaml").write_text("another pull\n", encoding="utf-8")
        self.build_with("IBD_Ancestry_blueprint")
        out = self.unpack_quietly()
        replaced = temp / "replaced"
        self.assertEqual((replaced / "IBD_Ancestry_transfer.yaml").read_text(encoding="utf-8"), "old transfer\n")
        self.assertEqual((replaced / "IBD_Ancestry_intake.yaml").read_text(encoding="utf-8"), "saved on the VM\n")
        self.assertEqual((replaced / "IBD_Ancestry_blueprint.yaml").read_text(encoding="utf-8"),
                         "changed on the VM\n")
        self.assertFalse((self.vm / "IBD_Ancestry_transfer.yaml").exists())
        self.assertFalse((temp / "IBD_Ancestry_intake.yaml").exists())
        self.assertIn("from_template", (temp / "IBD_Ancestry_blueprint.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.vm / "Celiac_transfer.yaml").is_file())
        self.assertIn("YAMLs/temp/replaced/IBD_Ancestry_blueprint.yaml", out)

    def test_the_same_blueprint_again_sets_nothing_aside(self):
        self.blueprint()
        self.build_with("IBD_Ancestry_blueprint")
        self.unpack_quietly()
        self.unpack_quietly()
        self.assertFalse((self.vm / "YAMLs" / "temp" / "replaced").exists())

    def test_it_says_which_upload_files_to_carry(self):
        from bundle_pullmanager import upload_locations

        self.assertEqual(upload_locations(self.ibd), ["data/Meds/ibd/IBD_Meds.parquet"])


class QueueTests(unittest.TestCase):
    """D91: `makebundle.py queue` exports every queued temp and carries it."""

    TEMP = (
        "cosmos_vars: {{project_db: PROJECTD93A5E7, cosmos_db: Dual}}\n"
        "run_vars: {{min_date_key: 19900101, max_date_key: 20260601}}\n"
        "project_vars: {{project_folder: {folder}}}\n"
        "cohorts:\n"
        "  - {{recipe: PatientWithDx, name: Patients, vars: {{ICD_Value: {code}}}}}\n"
        "  - {{recipe: IndexDiagnosis, name: IndexDiagnosis}}\n"
    )

    def setUp(self):
        from bundle_pullmanager import write_queue

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.temps = self.tmp / "temp"
        self.temps.mkdir()
        self.out = self.tmp / "root"
        for folder, code in (("Celiac", "K90.0"), ("IBD Ancestry", '"K50.%"')):
            (self.temps / f"{folder.replace(' ', '_')}_temp.yaml").write_text(
                self.TEMP.format(folder=folder, code=code), encoding="utf-8")
        write_queue(["Celiac_temp.yaml", "IBD_Ancestry_temp.yaml"], self.temps)

    def test_each_queued_temp_is_exported_and_carried(self):
        from bundle_pullmanager import export_queue

        exported = export_queue(self.temps, self.out)
        self.assertEqual([t.name for _, t in exported],
                         ["Celiac_blueprint.yaml", "IBD_Ancestry_blueprint.yaml"])
        self.assertIn("K90.0", (self.out / "Celiac_blueprint.yaml").read_text(encoding="utf-8"))
        bundle = self.tmp / "bundle.py"
        build(bundle, SOURCE_ROOT, [t for _, t in exported])
        sections, _ = read_bundle(bundle)
        published = {section["path"] for section in sections}
        self.assertIn("root/Celiac_blueprint.yaml", published)
        self.assertIn("root/IBD_Ancestry_blueprint.yaml", published)

    def build(self, **options):
        import bundle_pullmanager as bp
        from unittest import mock

        dist = self.tmp / "dist"
        with mock.patch.object(bp, "DEFAULT_OUTPUT", dist / "bundle.py"), \
                mock.patch.object(bp, "YAMLS_ONLY_OUTPUT", dist / "yamls_to_transfer.py"):
            return bp.build_bundle([], True, queue_folder=self.temps, export_dir=self.out, **options)

    def test_bundle_py_carries_the_queue_and_empties_it(self):
        # D122: one makebundle.py; the queued pulls go with the software.
        from bundle_pullmanager import read_queue

        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "bundle_with_yamls.py").write_text("# D106's\n", encoding="utf-8")
        (dist / "content_id.txt").write_text("bundle_with_yamls.py abc\n", encoding="utf-8")
        bundle, manifest, said = self.build()
        self.assertEqual(bundle.name, "bundle.py")
        published = {s["path"] for s in read_bundle(bundle)[0]}
        self.assertIn("root/Celiac_blueprint.yaml", published)
        self.assertIn("pullmanager/__init__.py", published)
        self.assertEqual(read_queue(self.temps), [])
        self.assertFalse((dist / "bundle_with_yamls.py").exists())
        self.assertEqual((dist / "content_id.txt").read_text(encoding="utf-8").splitlines(),
                         [f"bundle.py {manifest['content_id']}"])
        self.assertIn(f"content_id: {manifest['content_id']}", said)

    def test_with_nothing_queued_it_is_the_runtime_alone(self):
        from bundle_pullmanager import write_queue

        write_queue([], self.temps)
        bundle, _, said = self.build()
        self.assertFalse([s for s in read_bundle(bundle)[0] if s["path"].startswith("root/")])
        self.assertIn("Nothing was queued, so it carries the runtime alone.", said)

    def test_yamls_only_carries_the_transfers_and_no_software(self):
        bundle, _, _ = self.build(yamls_only=True)
        self.assertEqual(bundle.name, "yamls_to_transfer.py")
        self.assertEqual(sorted(s["path"] for s in read_bundle(bundle)[0]),
                         ["root/Celiac_blueprint.yaml", "root/IBD_Ancestry_blueprint.yaml"])

    def test_yamls_only_with_nothing_to_carry_is_refused(self):
        from bundle_pullmanager import write_queue

        write_queue([], self.temps)
        with self.assertRaises(BundleError) as caught:
            self.build(yamls_only=True)
        self.assertIn("Nothing to carry", str(caught.exception))

    def test_extracting_yamls_only_places_them_and_leaves_the_runtime(self):
        import contextlib
        import io

        from bundle_extractor import unpack

        full = self.tmp / "full.py"
        build(full, SOURCE_ROOT, [])
        vm = self.tmp / "vm"
        vm.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            unpack(full, vm / "pullmanager_runtime", quiet=True)
        runtime_manifest = (vm / "pullmanager_runtime" / ".bundle-manifest.json").read_bytes()
        (vm / "pullmanager_runtime" / "mine.txt").write_text("kept\n", encoding="utf-8")
        scope = (vm / "scope.py").read_bytes()
        yamls, _, _ = self.build(yamls_only=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            unpack(yamls, vm / "pullmanager_runtime", quiet=True)
        self.assertTrue((vm / "YAMLs" / "temp" / "Celiac_blueprint.yaml").is_file())
        self.assertTrue((vm / "YAMLs" / "temp" / "IBD_Ancestry_blueprint.yaml").is_file())
        self.assertEqual((vm / "pullmanager_runtime" / ".bundle-manifest.json").read_bytes(), runtime_manifest)
        self.assertTrue((vm / "pullmanager_runtime" / "mine.txt").is_file())
        self.assertEqual((vm / "scope.py").read_bytes(), scope)
        self.assertIn("left as it is", out.getvalue())

    def test_a_queued_temp_that_does_not_validate_stops_the_build_naming_it(self):
        from bundle_pullmanager import export_queue

        (self.temps / "Celiac_temp.yaml").write_text(
            self.TEMP.format(folder="Celiac", code="K90.0").replace("recipe: IndexDiagnosis", "recipe: NoSuchRecipe"),
            encoding="utf-8")
        with self.assertRaises(BundleError) as caught:
            export_queue(self.temps, self.out)
        self.assertIn("Celiac_temp.yaml", str(caught.exception))
        self.assertNotIn("IBD_Ancestry_temp.yaml", str(caught.exception))
        self.assertFalse((self.out / "Celiac_blueprint.yaml").exists())

    def test_a_queued_temp_that_is_gone_is_named(self):
        from bundle_pullmanager import export_queue, write_queue

        write_queue(["Gone_temp.yaml"], self.temps)
        with self.assertRaises(BundleError) as caught:
            export_queue(self.temps, self.out)
        self.assertIn("Gone_temp.yaml: not in", str(caught.exception))

    def test_an_empty_queue_says_how_to_fill_it(self):
        from bundle_pullmanager import export_queue, write_queue

        write_queue([], self.temps)
        with self.assertRaises(BundleError) as caught:
            export_queue(self.temps, self.out)
        self.assertIn("Save & Refresh", str(caught.exception))


def run(group: str | None = None, verbosity: int = 2) -> int:
    loader = unittest.TestLoader()
    module = sys.modules[__name__]
    if group:
        suite = unittest.TestSuite()
        matched = [
            name
            for name in dir(module)
            if name.lower().startswith(group.lower()) and name.endswith("Tests")
        ]
        if not matched:
            available = sorted(n for n in dir(module) if n.endswith("Tests"))
            print(f"No test class matching {group!r}. Available: " + ", ".join(available))
            return 1
        for name in matched:
            suite.addTests(loader.loadTestsFromTestCase(getattr(module, name)))
    else:
        suite = loader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1
