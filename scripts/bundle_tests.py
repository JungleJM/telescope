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
    COMPANION_FILES,
    SOURCE_ROOT,
    build,
    bundled_files,
    encode_payload_lines,
    render_bundle,
    source_files,
)

# Published path -> the file it came from. Companion files live outside the
# source tree, so a published path no longer implies SOURCE_ROOT / path.
SOURCES = {published: source for source, published, _policy in bundled_files()}

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

    def test_companion_paths_let_makeyaml_find_its_own_defaults(self):
        # makeYaml resolves YAMLs/ as a sibling of scripts/, so the published
        # layout has to preserve that or its defaults break once extracted.
        published = {p for _, p, _policy in COMPANION_FILES}
        self.assertIn("scripts/makeYaml.py", published)
        self.assertTrue(any(p.startswith("YAMLs/") for p in published))

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
                    SOURCES[section["path"]].read_bytes(),
                )


class ExtractionPolicyTests(BundleTestCase):
    """A re-extraction must never quietly destroy work done on the VM."""

    def extract_twice(self, rel, edited):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        (target / rel).write_text(edited, encoding="utf-8")
        extract(self.bundle, target)
        return target

    def test_recipes_and_authoring_stay_on_the_mac(self):
        # D49: the VM works from transfer YAMLs, so neither recipes nor the
        # browser UI nor a template to author from travel.
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
        self.pretend_previous_release_shipped(target, "YAMLs/datadictionary.yaml", "# last release\n")
        extract(self.bundle, target)
        self.assertFalse((target / "YAMLs/datadictionary.yaml.local").exists())
        self.assertEqual(
            (target / "YAMLs/datadictionary.yaml").read_bytes(),
            SOURCES["YAMLs/datadictionary.yaml"].read_bytes(),
        )

    def test_a_kept_copy_survives_the_next_update(self):
        target = self.extract_twice("YAMLs/datadictionary.yaml", "# edited on the VM\n")
        extract(self.bundle, target)
        self.assertEqual(
            (target / "YAMLs/datadictionary.yaml.local").read_text(encoding="utf-8"),
            "# edited on the VM\n",
        )

    def test_unbundled_files_inside_the_tree_do_not_survive(self):
        # The extracted tree is wholly managed: it is swapped, not merged. A
        # file of your own placed inside it is gone on the next update, which
        # is why your templates belong beside the tree rather than in it.
        target = self.tmp / "runtime"
        extract(self.bundle, target)
        stray = target / "YAMLs" / "UCPatients.yaml"
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
        target = self.extract_twice("YAMLs/datadictionary.yaml", "# edited on the VM\n")
        shipped = (SOURCES["YAMLs/datadictionary.yaml"]).read_bytes()
        self.assertEqual((target / "YAMLs/datadictionary.yaml").read_bytes(), shipped)
        self.assertEqual(
            (target / "YAMLs/datadictionary.yaml.local").read_text(encoding="utf-8"),
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
                self.assertEqual((target / rel).read_bytes(), SOURCES[rel].read_bytes())

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
    """D63: `pullmanager.py` in the working folder runs the extracted copy."""

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
        self.assertTrue((self.work / "pullmanager.py").is_file())
        self.assertNotIn("extracted  ", run.stdout)

    def test_anything_but_yes_extracts_nothing(self):
        for answer in ("n\n", "\n", ""):
            with self.subTest(answer=answer):
                run = self.run_bundle_answering(answer)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("Nothing extracted", run.stdout)
                self.assertFalse((self.work / "pullmanager_runtime").exists())
                self.assertFalse((self.work / "pullmanager.py").exists())

    def test_it_extracts_beside_itself_wherever_it_is_run_from(self):
        run = self.run_bundle_answering("y\n", cwd=self.tmp)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.work / "pullmanager_runtime").is_dir())
        self.assertFalse((self.tmp / "pullmanager_runtime").exists())

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
            [sys.executable, str(REPO_ROOT / "makebundle.py"), "--out", str(out)],
            capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("content_id: ", run.stdout)
        self.assertEqual(out.read_bytes(), self.bundle.read_bytes())

    def test_extract_with_no_folder_uses_the_default_and_writes_the_launcher(self):
        run = self.run_python("bundle.py", "--extract")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.work / "pullmanager_runtime" / "pullmanager.py").is_file())
        self.assertIn("pullmanager.py", run.stdout)
        version = self.run_python("pullmanager.py", "--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("pullmanager", version.stdout)

    def test_the_launcher_works_beside_a_folder_of_its_own_name(self):
        # Extracted as `pullmanager`, the folder shares the launcher's name.
        self.assertEqual(self.run_python("bundle.py", "--extract", "pullmanager").returncode, 0)
        version = self.run_python("pullmanager.py", "--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("pullmanager", version.stdout)

    def test_the_launcher_is_rewritten_by_every_extraction(self):
        self.run_python("bundle.py", "--extract")
        (self.work / "pullmanager.py").write_text("print('edited')\n", encoding="utf-8")
        self.run_python("bundle.py", "--extract", "elsewhere")
        text = (self.work / "pullmanager.py").read_text(encoding="utf-8")
        self.assertIn("'elsewhere'", text)
        self.assertNotIn("edited", text)

    def test_a_missing_folder_says_to_extract_again(self):
        self.run_python("bundle.py", "--extract")
        shutil.rmtree(self.work / "pullmanager_runtime")
        run = self.run_python("pullmanager.py", "--version")
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
        recipes = REPO_ROOT / "YAMLs" / "recipes.yaml"
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

    def test_the_vm_pathway_from_a_transfer_yaml(self):
        # D49 end to end. On the Mac: export a transfer YAML. On the VM, with
        # no recipes anywhere: split it from the working directory by a typed
        # relative path, then dry-run the manifest.
        work = self.tmp / "work"
        shutil.copytree(REPO_ROOT / "YAMLs" / "manager_test_cases", work)
        export = self.run_python(
            str(REPO_ROOT / "scripts" / "makeYaml.py"),
            "--template", str(work / "02_valid_multipliers_batching.yaml"),
            "--recipes", str(REPO_ROOT / "YAMLs" / "recipes.yaml"),
            "--export-transfer", "--out", str(work / "IBD_transfer.yaml"),
        )
        self.assertEqual(export.returncode, 0, export.stdout + export.stderr)

        target = self.tmp / "runtime"
        extract(self.bundle, target)
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

    def test_a_template_that_still_uses_recipes_is_refused_with_a_pointer(self):
        target = self.tmp / "runtime"
        extract(self.bundle, target)
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
        recipes = REPO_ROOT / "YAMLs" / "recipes.yaml"
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
