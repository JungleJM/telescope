#!/usr/bin/env python3
"""Pullmanager bundle - GENERATED FILE, DO NOT EDIT.

Built by scripts/bundle_pullmanager.py from scripts/pullmanager_src/.
To change anything here, edit the source module and rebuild the bundle.

    python pullmanager_bundle.py --verify-bundle
    python pullmanager_bundle.py --list
    python pullmanager_bundle.py --extract ./pullmanager_runtime
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

BEGIN_RE = re.compile(
    r"^# === BEGIN FILE: (?P<path>\S+) SHA256: (?P<sha>[0-9a-f]{64}) SIZE: (?P<size>\d+) ===$"
)
END_RE = re.compile(r"^# === END FILE: (?P<path>\S+) ===$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")

MANIFEST_FILENAME = ".bundle-manifest.json"

# What a re-extraction does to a file that is already there.
POLICY_REPLACE = "replace"
POLICY_SEED = "seed"


class BundleError(Exception):
    """Raised when a bundle is malformed, tampered with, or unsafe."""


def safe_relpath(raw: str) -> str:
    """Validate an embedded path, refusing anything that could escape the target."""
    if not raw or raw.strip() != raw:
        raise BundleError(f"Empty or padded path in bundle: {raw!r}")
    if "\\" in raw:
        raise BundleError(f"Backslash in bundle path (use POSIX separators): {raw!r}")
    if raw.startswith("/"):
        raise BundleError(f"Absolute path in bundle: {raw!r}")
    if DRIVE_RE.match(raw):
        raise BundleError(f"Drive-qualified path in bundle: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BundleError(f"Path traversal or empty segment in bundle path: {raw!r}")
    return raw


def decode_payload_lines(lines: list[str], path: str) -> str:
    out: list[str] = []
    for line in lines:
        if line == "#":
            out.append("")
        elif line.startswith("# "):
            out.append(line[2:])
        else:
            raise BundleError(
                f"Corrupt payload line in {path!r} (expected a comment-prefixed line): {line!r}"
            )
    return "\n".join(out)


def parse_payload(text: str) -> list[dict]:
    """Pull every file section out of a bundle's text, in bundle order."""
    sections: list[dict] = []
    current: dict | None = None
    body: list[str] = []

    for lineno, line in enumerate(text.split("\n"), start=1):
        begin = BEGIN_RE.match(line)
        if begin:
            if current is not None:
                raise BundleError(
                    f"Line {lineno}: BEGIN for {begin.group('path')!r} inside unterminated "
                    f"section {current['path']!r}"
                )
            current = {
                "path": safe_relpath(begin.group("path")),
                "sha256": begin.group("sha"),
                "size": int(begin.group("size")),
            }
            body = []
            continue

        end = END_RE.match(line)
        if end:
            if current is None:
                raise BundleError(f"Line {lineno}: END for {end.group('path')!r} with no open section")
            if end.group("path") != current["path"]:
                raise BundleError(
                    f"Line {lineno}: END path {end.group('path')!r} does not match "
                    f"BEGIN path {current['path']!r}"
                )
            current["content"] = decode_payload_lines(body, current["path"])
            sections.append(current)
            current = None
            body = []
            continue

        if current is not None:
            body.append(line)

    if current is not None:
        raise BundleError(f"Unterminated file section: {current['path']!r}")
    return sections


def compute_content_id(entries: list[dict]) -> str:
    """Stable identity for a bundle's contents, independent of build time."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e["path"]):
        digest.update(f"{entry['path']}\0{entry['sha256']}\0{entry['size']}\0".encode("utf-8"))
    return digest.hexdigest()


def verify_sections(sections: list[dict], declared: list[dict]) -> None:
    """Cross-check payload sections against the bundle's embedded manifest."""
    seen: set[str] = set()
    for section in sections:
        if section["path"] in seen:
            raise BundleError(f"Duplicate file section in bundle: {section['path']!r}")
        seen.add(section["path"])

    declared_by_path = {entry["path"]: entry for entry in declared}
    if len(declared_by_path) != len(declared):
        raise BundleError("Embedded manifest lists the same path more than once.")

    missing = sorted(set(declared_by_path) - seen)
    if missing:
        raise BundleError(f"Bundle manifest lists files with no payload section: {', '.join(missing)}")

    extra = sorted(seen - set(declared_by_path))
    if extra:
        raise BundleError(f"Bundle carries payload sections absent from its manifest: {', '.join(extra)}")

    for section in sections:
        entry = declared_by_path[section["path"]]
        raw = section["content"].encode("utf-8")
        if len(raw) != entry["size"]:
            raise BundleError(
                f"Size mismatch for {section['path']!r}: manifest says {entry['size']}, "
                f"payload decodes to {len(raw)}"
            )
        if section["sha256"] != entry["sha256"]:
            raise BundleError(
                f"Marker/manifest hash disagreement for {section['path']!r}"
            )
        actual = hashlib.sha256(raw).hexdigest()
        if actual != entry["sha256"]:
            raise BundleError(
                f"SHA-256 mismatch for {section['path']!r}: expected {entry['sha256']}, got {actual}"
            )


def read_bundle(bundle_path: Path) -> tuple[list[dict], dict]:
    text = bundle_path.read_text(encoding="utf-8")
    match = re.search(r"^BUNDLE_MANIFEST_JSON = r'''(?P<json>.*?)'''$", text, re.S | re.M)
    if not match:
        raise BundleError("Bundle is missing its embedded BUNDLE_MANIFEST_JSON block.")
    try:
        manifest = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"Embedded bundle manifest is not valid JSON: {exc}") from exc

    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise BundleError("Embedded bundle manifest has no `files` list.")

    sections = parse_payload(text)
    verify_sections(sections, declared)

    expected_id = manifest.get("content_id")
    actual_id = compute_content_id(declared)
    if expected_id != actual_id:
        raise BundleError(
            f"Bundle content_id mismatch: manifest says {expected_id}, computed {actual_id}"
        )
    return sections, manifest


def extract(bundle_path: Path, target: Path, force: bool = False) -> list[str]:
    """Verify a bundle fully, then swap its contents into `target`."""
    sections, manifest = read_bundle(bundle_path)

    target = target.resolve()
    if target.exists():
        if not target.is_dir():
            raise BundleError(f"Extraction target exists and is not a directory: {target}")
        owned = (target / MANIFEST_FILENAME).is_file()
        if not owned and not force:
            raise BundleError(
                f"Refusing to replace {target}: it is not a previous bundle extraction "
                f"(no {MANIFEST_FILENAME}). Pass --force to overwrite it anyway."
            )

    staging = target.with_name(target.name + ".extract-tmp")
    previous = target.with_name(target.name + ".old-tmp")
    for scratch in (staging, previous):
        if scratch.exists():
            shutil.rmtree(scratch)

    policies = {entry["path"]: entry.get("policy", POLICY_REPLACE) for entry in manifest["files"]}
    kept: list[str] = []
    preserved: list[str] = []

    try:
        for section in sections:
            rel = section["path"]
            out_path = staging / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            existing = target / rel
            policy = policies.get(rel, POLICY_REPLACE)
            shipped = section["content"].encode("utf-8")

            if existing.is_file():
                current = existing.read_bytes()
                if policy == POLICY_SEED:
                    # Yours once it exists. Carry it forward untouched.
                    out_path.write_bytes(current)
                    if current != shipped:
                        kept.append(rel)
                    continue
                if current != shipped:
                    # Replaced, but an edit made here is not simply destroyed.
                    aside = staging / (rel + ".local")
                    aside.parent.mkdir(parents=True, exist_ok=True)
                    aside.write_bytes(current)
                    preserved.append(rel)

            # Explicit bytes so Windows does not translate newlines and break hashes.
            out_path.write_bytes(shipped)

        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # Re-read from disk: proves what landed matches, not just what we held.
        # Files kept from a previous extraction are exempt, since they are
        # deliberately not the shipped bytes.
        for section in sections:
            if section["path"] in kept:
                continue
            written = (staging / section["path"]).read_bytes()
            if hashlib.sha256(written).hexdigest() != section["sha256"]:
                raise BundleError(f"Post-write verification failed for {section['path']!r}")

        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if previous.exists() and not target.exists():
            previous.rename(target)
        raise
    finally:
        if previous.exists() and target.exists():
            shutil.rmtree(previous, ignore_errors=True)

    for rel in kept:
        print(f"kept       {rel}  (yours; the shipped copy was not applied)")
    for rel in preserved:
        print(f"replaced   {rel}  (your previous copy saved as {rel}.local)")
    return [section["path"] for section in sections]


def bundle_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Self-extracting Pullmanager bundle.",
    )
    parser.add_argument("--verify-bundle", action="store_true", help="Verify this bundle and exit.")
    parser.add_argument("--list", action="store_true", help="List the files this bundle carries.")
    parser.add_argument("--extract", metavar="DIR", help="Verify, then extract into DIR.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --extract to replace a directory that is not a previous extraction.",
    )
    args = parser.parse_args(argv)

    bundle_path = Path(__file__).resolve()

    if not (args.verify_bundle or args.list or args.extract):
        parser.print_help()
        return 1

    try:
        if args.verify_bundle or args.list:
            sections, manifest = read_bundle(bundle_path)
            if args.list:
                for section in sections:
                    print(f"{section['size']:>8}  {section['sha256'][:12]}  {section['path']}")
            if args.verify_bundle:
                print(f"OK  {len(sections)} files verified")
                print(f"content_id: {manifest['content_id']}")

        if args.extract:
            written = extract(bundle_path, Path(args.extract), force=args.force)
            for path in written:
                print(f"extracted  {path}")
            print(f"\nExtracted {len(written)} files to {Path(args.extract).resolve()}")
    except BundleError as exc:
        print(f"BUNDLE ERROR: {exc}", file=sys.stderr)
        return 2

    return 0

BUNDLE_MANIFEST_JSON = r'''{
  "bundle_format_version": 1,
  "content_id": "e83f6a6d1b719d6120b226fe58b02117bfc4b53c336e4e34ea73a4d6801e525a",
  "file_count": 36,
  "files": [
    {
      "path": "YAMLs/datadictionary.yaml",
      "policy": "replace",
      "sha256": "25d874ec0b312bce8af471a5614b41026369980a27a1c5ea66c55ff6a202656c",
      "size": 97788
    },
    {
      "path": "YAMLs/recipes.yaml",
      "policy": "replace",
      "sha256": "974d2fed63e4b6449dc544cbc0559d3b5c4163ff6dfba28595b1cc65b770f3fb",
      "size": 12725
    },
    {
      "path": "YAMLs/template.yaml",
      "policy": "seed",
      "sha256": "4100e43555a828a435c6f6bbab33dc5231d40d956c70b8a5e0ae1cafa37966b8",
      "size": 8155
    },
    {
      "path": "pullmanager.py",
      "policy": "replace",
      "sha256": "ebdc02f9ba0685fc16b3aaea58c6563e0b91f3e69bcbce40bf953e414f787add",
      "size": 408
    },
    {
      "path": "pullmanager/__init__.py",
      "policy": "replace",
      "sha256": "2b6c4b6cedb40b106d374887a4180e7a03259c5fa7d8a66a6932b65ce73123c0",
      "size": 126
    },
    {
      "path": "pullmanager/__main__.py",
      "policy": "replace",
      "sha256": "307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7",
      "size": 79
    },
    {
      "path": "pullmanager/batches.py",
      "policy": "replace",
      "sha256": "dc90271523a5145924ab101adf6119f716d790d33395b68a3baef905a8f04e17",
      "size": 5412
    },
    {
      "path": "pullmanager/cli.py",
      "policy": "replace",
      "sha256": "bc2b9888907a0057e8c0cbda85ca2586e6e5240b684d03c13d1559906c1c8258",
      "size": 8869
    },
    {
      "path": "pullmanager/db.py",
      "policy": "replace",
      "sha256": "95bf2e090578a6160ac5259be25e6a7d3604df2a33eb9cbfc570774ecb547085",
      "size": 11608
    },
    {
      "path": "pullmanager/executor.py",
      "policy": "replace",
      "sha256": "fd658d58f45a49875790f6a119cc8b160cd736fbce5f861331f5f2ae94dd79ea",
      "size": 8444
    },
    {
      "path": "pullmanager/local_sql.py",
      "policy": "replace",
      "sha256": "2effa5f5fc36cbafa0a72c872f3f3e362f6a5d8d9608c356fa6663f1697f3130",
      "size": 7093
    },
    {
      "path": "pullmanager/manifest.py",
      "policy": "replace",
      "sha256": "1ccaab95c26ba0f76151c64e58e2b5988e60558ae06a9ec1f4e082df23ca4508",
      "size": 11210
    },
    {
      "path": "pullmanager/models.py",
      "policy": "replace",
      "sha256": "2eb665cf240632931fdb1f07b9b4d6e04757c51fb188ea3d17c184c60cdf87be",
      "size": 2154
    },
    {
      "path": "pullmanager/naming.py",
      "policy": "replace",
      "sha256": "873c90540e4fd526d86e65e9c918958c39be18384cadbce239de4fc2b23c6d45",
      "size": 4175
    },
    {
      "path": "pullmanager/normalize.py",
      "policy": "replace",
      "sha256": "dea303653e1cdc1504e1238ff1fc4ba21cb0e74cc2899f6c160512252a6471a7",
      "size": 8239
    },
    {
      "path": "pullmanager/server_sql.py",
      "policy": "replace",
      "sha256": "2bc6ce2982bcf2c8e76bf0e6a7a2458a340df252ad006aae19dbd6cba2a04a97",
      "size": 8743
    },
    {
      "path": "pullmanager/session.py",
      "policy": "replace",
      "sha256": "38807f146902c57d45d0227986e71f33df69bf3ee2bc1d29cdbfec3e0cea6675",
      "size": 14925
    },
    {
      "path": "pullmanager/sql.py",
      "policy": "replace",
      "sha256": "e894b41f2d51392c0690d2f9e7c04d1d617fb3288505a65fccfe22dbf03dbc2c",
      "size": 5320
    },
    {
      "path": "pullmanager/tests/__init__.py",
      "policy": "replace",
      "sha256": "4f70b04f739db1fd4fdae89aa3b2dd3ac8afea47a8dc6b8d5eb7fcda8ed717a2",
      "size": 1508
    },
    {
      "path": "pullmanager/tests/support.py",
      "policy": "replace",
      "sha256": "5c88b19c05ea4b105763db67d73e42d88cbe1c3897b2ece74c9f287b60403398",
      "size": 4541
    },
    {
      "path": "pullmanager/tests/test_batches.py",
      "policy": "replace",
      "sha256": "e9162918f0020a69ea8b94bb61a1d761863d05e752306e28aa0bdbfe914b3719",
      "size": 5122
    },
    {
      "path": "pullmanager/tests/test_db.py",
      "policy": "replace",
      "sha256": "e542b71d46f8493d9d45c1ec2d68d2adc73ebb180551a71e810f9a6ce784905c",
      "size": 13107
    },
    {
      "path": "pullmanager/tests/test_executor.py",
      "policy": "replace",
      "sha256": "f1907b950db229ff68fbcb7c455df5dd7e9b735a2b130f71f20ca8b5164ce488",
      "size": 6609
    },
    {
      "path": "pullmanager/tests/test_manifest.py",
      "policy": "replace",
      "sha256": "78f8843188969abfa24793cbd298a3e24ede337d3ebb80a5a3a7c1c365f42ff7",
      "size": 14639
    },
    {
      "path": "pullmanager/tests/test_models.py",
      "policy": "replace",
      "sha256": "9293f87dcebe251868ceb465460b0067c9642790941c6e26250a6cb8d47044a8",
      "size": 2221
    },
    {
      "path": "pullmanager/tests/test_naming.py",
      "policy": "replace",
      "sha256": "ec0c27d0b9eb3b614cb89acdf84d93b6bf8756d1b33d3fada9f65468251ea03e",
      "size": 6276
    },
    {
      "path": "pullmanager/tests/test_normalize.py",
      "policy": "replace",
      "sha256": "acf17645cde65424d49e02ecda95c2d65df2fc55943283c0a50f729fe7683814",
      "size": 7257
    },
    {
      "path": "pullmanager/tests/test_render.py",
      "policy": "replace",
      "sha256": "51b6f73d1d00c14d116353ae1a05df7e2be1307dfc2e7f51ba12706eed2f8c3f",
      "size": 10654
    },
    {
      "path": "pullmanager/tests/test_session.py",
      "policy": "replace",
      "sha256": "98a71c5383ac3eee9711dce20de58e349f78246f3bf021f7b018f2dc6b976a90",
      "size": 10227
    },
    {
      "path": "pullmanager/tests/test_sql.py",
      "policy": "replace",
      "sha256": "70f3bfde2d04c0ab5dc3df2d063182f2684f908b04446d708049f1ee40c1cc35",
      "size": 5285
    },
    {
      "path": "pullmanager/tests/test_uploads.py",
      "policy": "replace",
      "sha256": "433529c848a599c7348f5fa202e1b551fa9cb3dc2d66351e7cd37103a7d2f669",
      "size": 5626
    },
    {
      "path": "pullmanager/uploads.py",
      "policy": "replace",
      "sha256": "9698807e77ee5ecf179a2478f9fb4ae53e6b5d702caf8126a27cbc0b6fadb4e9",
      "size": 7077
    },
    {
      "path": "pullmanager/yaml_io.py",
      "policy": "replace",
      "sha256": "dca04d852f7873c8abcac4d0e9f0f0e1883c96117a9bcacdf7766fbae4255c18",
      "size": 1844
    },
    {
      "path": "scripts/makeYaml.py",
      "policy": "replace",
      "sha256": "fe1e7dc6b8814dfe844b8d5222eb3dd2300be1b3abf4cbdde8de3b4b98d47470",
      "size": 98604
    },
    {
      "path": "scripts/yamlmanager.py",
      "policy": "replace",
      "sha256": "f898ed308eec285127782aa66f305507691e722f9166e8363b321f44c9879f22",
      "size": 105884
    },
    {
      "path": "scripts/yamlmanager_backend.py",
      "policy": "replace",
      "sha256": "940c80eea5f71b870ff08d72fce4c405e4ac2950c1c624a7399104681a66f20f",
      "size": 2307
    }
  ]
}'''


if __name__ == "__main__":
    raise SystemExit(bundle_main())

# === BEGIN FILE: YAMLs/datadictionary.yaml SHA256: 25d874ec0b312bce8af471a5614b41026369980a27a1c5ea66c55ff6a202656c SIZE: 97788 ===
# DataDictionary:
#   PatientDim:
#     description: >
#       Core patient dimension table. One row per patient, carrying demographics,
#       vital status, and several social vulnerability and geography attributes.
#       Use PatientDim.DurableKey to join to facts (note: this is the patient durable key).
#     granularity: >
#       One row per patient (patient-level dimension).
#     columns:
#       AgeInYears:
#         type: integer
#         nullable: true
#         description: >
#           Patient's age in years at the time Cosmos snapshot was created or last updated.
#       BirthDate:
#         type: date/datetime
#         nullable: true
#         description: >
#           De-identified patient date of birth. Subject to date shifting per Cosmos
#           policies.
#       BirthDateAccuracy_X:
#         type: string
#         nullable: true
#         description: >
#           Categorical flag describing the precision of BirthDate (e.g., day/month/year
#           known).
#       EarliestPossibleBirthDate_X:
#         type: date/datetime
#         nullable: true
#         description: >
#           Lower bound date when only an age range or imprecise birth date is known.
#       BloodType_X:
#         type: string
#         nullable: true
#         description: >
#           ABO blood group if available (e.g., A, B, AB, O); de-identified and sparse.
#       BloodTypeAndRhesusFactor_X:
#         type: string
#         nullable: true
#         description: >
#           Combined ABO and Rh factor (e.g., A+, O?) if documented and mapped.
#       Country:
#         type: string
#         nullable: true
#         description: >
#           Country associated with the patient's primary address at last update.
#       DeathDate:
#         type: date/datetime
#         nullable: true
#         description: >
#           De-identified date of death when known. Empty for living or unknown status.
#       DurableKey:
#         type: bigint
#         nullable: false
#         description: >
#           Patient durable key, unique identifier for the patient in Cosmos.
#           This is the key you join to PatientDurableKey on fact tables.
#       Ethnicity:
#         type: string
#         nullable: true
#         description: >
#           Patient-reported or registration ethnicity category (e.g., Hispanic or Latino).
#       FifthRace:
#         type: string
#         nullable: true
#         description: >
#           Fifth race component for multi-racial patients; supports up to 5 race values.
#       FirstRace:
#         type: string
#         nullable: true
#         description: >
#           Primary race component; for single-race patients this is the sole race value.
#       FourthRace:
#         type: string
#         nullable: true
#         description: >
#           Fourth race component for multi-racial patients.
#       GenderIdentity:
#         type: string
#         nullable: true
#         description: >
#           Patient's recorded gender identity (e.g., woman, man, nonbinary).
#       HasPcp_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Flag indicating if a primary care provider is associated with the patient.
#       IsCurrent:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates whether the row is the current record for the patient.
#       IsValid:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates whether the record is considered valid for analytics use.
#       LastImmunizationQueryInstantUtc:
#         type: datetime (UTC)
#         nullable: true
#         description: >
#           Timestamp in UTC when immunization data was last queried or refreshed for this
#           patient.
#       MaritalStatus:
#         type: string
#         nullable: true
#         description: >
#           Patient's marital status as captured in registration (e.g., Married, Single).
#       MultiRacial:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that multiple race fields are populated for this patient.
#       PreferredLanguage:
#         type: string
#         nullable: true
#         description: >
#           Preferred language for care and communication (e.g., English, Spanish).
#       PreliminaryCauseOfDeathDiagnosisKey:
#         type: bigint (foreign key to DiagnosisDim/DiagnosisTerminologyDim)
#         nullable: true
#         description: >
#           Key referencing preliminary cause-of-death diagnosis, if coded.
#       PrimaryRUCA_X:
#         type: string
#         nullable: true
#         description: >
#           Primary Rural-Urban Commuting Area (RUCA) classification for patient's
#           residence.
#       ReliableSex:
#         type: string
#         nullable: true
#         description: >
#           Derived sex field cleaned for analytic use; more stable than raw sex
#           assignments.
#       RhesusFactor_X:
#         type: string
#         nullable: true
#         description: >
#           Rh factor (positive/negative) when available.
#       SecondRace:
#         type: string
#         nullable: true
#         description: >
#           Second race component for multi-racial patients.
#       Sex:
#         type: string
#         nullable: true
#         description: >
#           Administrative sex field used in general workflows (e.g., Female, Male).
#       SexAssignedAtBirth:
#         type: string
#         nullable: true
#         description: >
#           Sex assigned at birth when documented (e.g., female, male).
#       SourceComboKey_X:
#         type: bigint (foreign key to PatientSourceBridgeX/SourceDim)
#         nullable: true
#         description: >
#           Composite key linking to data source metadata for the patient record.
#       SourceCountry_X:
#         type: string
#         nullable: true
#         description: >
#           Country of the source organization contributing this patient's data.
#       StateOrProvince:
#         type: string
#         nullable: true
#         description: >
#           State or province of the patient's primary address.
#       StateOrProvinceAbbreviation:
#         type: string
#         nullable: true
#         description: >
#           Abbreviated state/province (e.g., WI, TX) based on primary address.
#       Status:
#         type: string
#         nullable: true
#         description: >
#           High-level patient status (e.g., active, inactive, deceased).
#       SviHouseholdCharacteristicsPctlRankByZip2020_X:
#         type: numeric
#         nullable: true
#         description: >
#           Social Vulnerability Index percentile rank (household characteristics) for
#           ZIP, 2020.
#       SviHouseholdCompositionPctlRankingByZip2018_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI percentile for household composition, ZIP-level, 2018 version.
#       SviHousingTypeTransportationPctlRankByZip2020_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI percentile for housing type & transportation, ZIP-level, 2020 version.
#       SviHousingTypeTransportationPctlRankingByZip2018_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI percentile for housing type & transportation, ZIP-level, 2018.
#       SviMinorityStatusLanguagePctlRankingByZip2018_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI percentile for minority & language status, ZIP-level, 2018.
#       SviOverallPctlRankByZip2020_X:
#         type: numeric
#         nullable: true
#         description: >
#           Overall SVI percentile rank for ZIP, 2020 ACS-based SVI.
#       SviOverallPctlRankingByZip2018_X:
#         type: numeric
#         nullable: true
#         description: >
#           Overall SVI percentile rank for ZIP, 2018 SVI.
#       SviRacialEthnicMinorityStatusPctlRankByZip2020_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI percentile for racial/ethnic minority status for ZIP, 2020.
#       SviSocioeconomicPctlRankByZip2020_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI socioeconomic percentile for ZIP, 2020 version.
#       SviSocioeconomicPctlRankingByZip2018_X:
#         type: numeric
#         nullable: true
#         description: >
#           SVI socioeconomic percentile for ZIP, 2018 version.
#       ThirdRace:
#         type: string
#         nullable: true
#         description: >
#           Third race component for multi-racial patients.
#       UseInCosmosAnalytics_X:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates whether the patient record is approved for use in Cosmos analytics.
#
#   EdVisitFact:
#     description: >
#       Emergency department (ED) visit fact table. One row per ED visit/contact,
#       linked to encounters via EncounterKey and to patients via PatientDurableKey.
#       Includes ED timing milestones, acuity, payor, and disposition.
#     granularity: >
#       One row per ED visit (ED-visit-level fact), usually one per ED encounter.
#     columns:
#       AcuityLevel:
#         type: string
#         nullable: true
#         description: >
#           ED triage acuity (e.g., ESI level) recorded at arrival.
#       AdmissionProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for the provider associated with admission decision from ED.
#       AgeKey:
#         type: bigint (foreign key to DurationDim/age dimension)
#         nullable: true
#         description: >
#           Age key representing patient age at the time of the ED visit.
#       ArrivalDateKey:
#         type: integer (DateKey)
#         nullable: false
#         description: >
#           DateKey for ED arrival date. Join to DateDim for calendar attributes.
#       ArrivalInstant:
#         type: datetime
#         nullable: false
#         description: >
#           Timestamp of arrival in ED, de-identified and time-shifted.
#       ArrivalMethod:
#         type: string
#         nullable: true
#         description: >
#           Method of arrival (e.g., walk-in, ambulance, transfer).
#       ArrivalTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           TimeOfDay key for ED arrival time; join to TimeOfDayDim for hour/minute
#           buckets.
#       AvsPrintDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when After-Visit Summary was printed, if applicable.
#       AvsPrintInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when the AVS was printed.
#       AvsPrintTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for AVS print time.
#       BedAssignedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when ED bed was assigned.
#       BedAssignedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when ED bed assignment occurred.
#       BedAssignedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for bed assignment.
#       BedRequestDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when bed request for admission/placement was placed.
#       BedRequestInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for bed request.
#       BedRequestTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for bed request.
#       BoardingHoursEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey marking the end of ED boarding period, if tracked.
#       BoardingHoursEndInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when boarding ended.
#       BoardingHoursEndTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for boarding end time.
#       BoardingHoursStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when boarding started.
#       BoardingHoursStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp marking start of boarding.
#       BoardingHoursStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for boarding start.
#       CodingComplete_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates whether ED visit coding is complete at source organization.
#       CodingStatus_X:
#         type: string
#         nullable: true
#         description: >
#           Categorical coding status (e.g., pending, complete).
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real rows.
#       CoverageComboKey_X:
#         type: bigint (foreign key to CoverageBridgeX/CoverageDim)
#         nullable: true
#         description: >
#           Composite coverage key for the ED visit's primary coverage.
#       DepartureDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when the patient physically left the ED.
#       DepartureInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of ED departure (may differ from disposition decision time).
#       DepartureTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for ED departure time.
#       DerivedEncounterStatus_X:
#         type: string
#         nullable: true
#         description: >
#           Derived status for the ED visit (e.g., completed, cancelled).
#       DischargeDisposition:
#         type: string
#         nullable: true
#         description: >
#           Disposition from ED (e.g., admitted, discharged home, left AMA).
#       DispositionDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey of disposition decision.
#       DispositionInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of disposition decision.
#       DispositionTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for disposition.
#       EdVisitKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for ED visit fact row.
#       EdGenericDispo:
#         type: string
#         nullable: true
#         description: >
#           Generalized disposition category, harmonized across organizations.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: false
#         description: >
#           Key linking the ED visit to its underlying encounter.
#       EncounterSourceComboKey_X:
#         type: bigint (foreign key to EncounterSourceBridge/SourceDim)
#         nullable: true
#         description: >
#           Composite source key describing origin of the encounter data.
#       FinancialClass:
#         type: string
#         nullable: true
#         description: >
#           Financial class category (e.g., commercial, Medicare).
#       FirstAttendingAssignedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first attending provider was assigned in ED.
#       FirstAttendingAssignedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when first attending was assigned.
#       FirstAttendingAssignedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for first attending assignment.
#       FirstCodeEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first resuscitation/code event ended, if documented.
#       FirstCodeEndInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp marking end of first code event.
#       FirstCodeEndTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for code end.
#       FirstCodeStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first code event started.
#       FirstCodeStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when first code started.
#       FirstCodeStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for code start.
#       FirstEdRegisteredNurseAssignedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first ED RN was assigned.
#       FirstEdRegisteredNurseAssignedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of first ED RN assignment.
#       FirstEdRegisteredNurseAssignedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for first RN assignment.
#       FirstEkgCompletedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first ECG/EKG was completed in ED.
#       FirstEkgCompletedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of first EKG completion.
#       FirstEkgCompletedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for first EKG completion.
#       FirstProviderContactDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first provider saw the patient in ED.
#       FirstProviderContactInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of first provider contact.
#       FirstProviderContactTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for provider contact.
#       FirstSedationEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for end of first recorded sedation episode.
#       FirstSedationEndInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for sedation end.
#       FirstSedationEndTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for sedation end.
#       FirstSedationStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for start of first recorded sedation episode.
#       FirstSedationStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of sedation start.
#       FirstSedationStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for sedation start.
#       FirstTraumaEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when first trauma bay period ended.
#       FirstTraumaEndInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for end of trauma bay period.
#       FirstTraumaEndTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for trauma end.
#       FirstTraumaStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when trauma bay period started.
#       FirstTraumaStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of trauma bay start.
#       FirstTraumaStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for trauma start.
#       HospitalAdmissionKey:
#         type: bigint (foreign key to HospitalAdmissionFact)
#         nullable: true
#         description: >
#           Links ED visit to a subsequent hospital admission record when applicable.
#       LeftAgainstMedicalAdvice:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates patient left against medical advice (AMA) from ED.
#       LeftWithoutBeingSeen:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates patient left ED without being seen by a provider.
#       ObservationStartedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when ED observation status began.
#       ObservationStartedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp marking start of observation.
#       ObservationStartedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for observation start.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Durable key identifying the patient associated with the ED visit.
#       PayorComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key identifying payor information associated with the ED visit.
#       PrimaryPayorKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Key for primary payor for the ED visit.
#       RegistrationCompleteDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when ED registration was completed.
#       RegistrationCompleteInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for registration completion.
#       RegistrationCompleteTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for registration completion.
#       RoomedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when patient was assigned to an ED room.
#       RoomedInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for room assignment.
#       RoomedTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for rooming time.
#       TriageCompleteDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when triage was completed.
#       TriageCompleteInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for triage completion.
#       TriageCompleteTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for triage completion.
#       TriageStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when triage started.
#       TriageStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for triage start.
#       TriageStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for triage start.
#       UnderObservation:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates whether visit included an ED observation phase.
#
#   DiagnosisEventFact:
#     description: >
#       Diagnosis event fact table. Rows represent diagnoses associated with encounters,
#       problem lists, or other contexts. Use DISTINCT EncounterKey or PatientDurableKey
#       for prevalence; rows are at the billing/event grain.
#     granularity: >
#       One row per diagnosis event per encounter/context at billing/event grain.
#     columns:
#       AgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key for patient at time of diagnosis event.
#       Chronic:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates diagnosis is flagged as chronic in the source system.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count; typically 1 for real diagnosis event rows.
#       DiagnosisEventKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the diagnosis event record.
#       DiagnosisKey:
#         type: bigint (foreign key to DiagnosisDim/DiagnosisTerminologyDim)
#         nullable: false
#         description: >
#           Key referencing the diagnosis concept; use DiagnosisTerminologyDim for code
#           details.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter associated with this diagnosis event; note that billing diagnoses
#           can repeat across linked encounters.
#       EndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing diagnosis end date or inactivation, when present.
#       EmergencyDepartmentDiagnosis:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates diagnosis was documented in the ED context.
#       IsPrimary:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates diagnosis is marked as primary or principal for the encounter.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient durable key associated with the diagnosis event.
#       SourceComboKey:
#         type: bigint (foreign key to DiagnosisEventSourceBridge/SourceDim)
#         nullable: true
#         description: >
#           Composite key linking to data source metadata for this diagnosis record.
#       StartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing when diagnosis became active or was first recorded.
#       Status:
#         type: string
#         nullable: true
#         description: >
#           Diagnosis status (e.g., active, resolved, ruled out) depending on context.
#       Type:
#         type: string
#         nullable: true
#         description: >
#           Diagnosis type (e.g., billing, problem list, admission, discharge), per source.
#
#   HospitalAdmissionFact:
#     description: >
#       Hospital admission fact table. One row per hospital admission episode,
#       including inpatient and some observation stays, linked to encounters and patients.
#       Contains admission/discharge timing, LOS, payor, and readmission flags.
#     granularity: >
#       One row per hospital admission episode (admission-level fact).
#     columns:
#       AdmissionDateKey:
#         type: integer (DateKey)
#         nullable: false
#         description: >
#           DateKey for hospital admission date; join to DateDim for calendar attributes.
#       AdmittingProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for provider recorded as admitting provider.
#       AgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key at time of admission.
#       CodingComplete_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates coding for the hospital admission is complete.
#       CodingStatus_X:
#         type: string
#         nullable: true
#         description: >
#           Categorical coding status (e.g., pending, complete) for the admission.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count; typically 1 for real hospital admission rows.
#       CoverageComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite coverage key representing coverage for the admission.
#       DaysSincePriorAdmission_X:
#         type: integer
#         nullable: true
#         description: >
#           Days between this admission and prior admission, when computable.
#       DepartmentKey:
#         type: bigint (foreign key to DepartmentDim)
#         nullable: true
#         description: >
#           Department representing admitting or primary inpatient department.
#       DerivedEncounterStatus_X:
#         type: string
#         nullable: true
#         description: >
#           Derived status of the admission encounter (e.g., completed, cancelled).
#       DischargeDepartmentKey_X:
#         type: bigint (foreign key to DepartmentDim)
#         nullable: true
#         description: >
#           Department key for the department from which the patient was discharged.
#       DischargeDisposition:
#         type: string
#         nullable: true
#         description: >
#           Discharge disposition (e.g., home, SNF, expired).
#       DischargeDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing discharge date.
#       DischargeInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of discharge; de-identified and time-shifted.
#       DischargeTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for discharge time.
#       DischargingProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for provider recorded as discharging provider.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: false
#         description: >
#           Encounter key linking to the underlying hospital encounter record.
#       EncounterSourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key describing origin of the encounter data.
#       EncounterType:
#         type: string
#         nullable: true
#         description: >
#           Type of encounter associated with this admission (e.g., inpatient,
#           observation).
#       FinancialClass:
#         type: string
#         nullable: true
#         description: >
#           Financial class for the admission (e.g., commercial, Medicaid).
#       HospitalAdmissionKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the hospital admission fact record.
#       InpatientAdmissionDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when the inpatient portion of the admission started (may differ from
#           hospital arrival).
#       InpatientAdmissionInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for inpatient admission start.
#       InpatientAdmissionTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for inpatient admission time.
#       InpatientLengthOfStayInDays:
#         type: integer
#         nullable: true
#         description: >
#           Length of stay in days for inpatient portion only.
#       LengthOfStayInDays:
#         type: integer
#         nullable: true
#         description: >
#           Total length of stay in days for the hospital admission episode.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient durable key associated with the hospital admission.
#       PayorComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite payor key associated with the admission.
#       PrimaryPayorKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Key identifying primary payor for the admission.
#       StartedAsHOV_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates admission started as a hospital outpatient visit (HOV) before
#           inpatient conversion.
#       StartedInED_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates admission started from an ED visit.
#       IsIndexAdmission_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Flag used in readmission logic to indicate index admission in a readmission
#           series.
#       IsUnplannedReadmission_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates admission is categorized as an unplanned readmission relative to a
#           prior stay.
#       IsPlannedReadmission_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates admission is categorized as a planned readmission.
#       ReadmissionCausedByKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Key referencing cause-of-readmission concept when mapped (e.g., another
#           admission or diagnosis).
#
#   LabComponentResultFact:
#     description: >
#       Lab component result fact table. One row per lab component result for a patient,
#       linked to specific lab tests and encounters. Numeric results are stored in
#       NumericValue with units in Unit; categorical/string results are stored in Value.
#       LabComponentKey = -1 indicates an unmapped component; exclude or handle separately.
#     granularity: >
#       One row per lab component result per patient per event.
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Flag indicating the row has been logically deleted in the source. Filter this
#           out for most analytic use cases.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Flag indicating the row was inferred rather than directly sourced; used for
#           lineage and data quality checks.
#       Abnormal:
#         type: string
#         nullable: true
#         description: >
#           Abnormality category for the result (e.g., high, low, normal), derived from
#           reference ranges and the raw value.
#       CollectionInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when the specimen was collected. De-identified and time-shifted.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator, typically 1 for real rows. Placeholder rows
#           have nonpositive keys and may carry NULL Count.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter during which the lab specimen/result was associated.
#       Flag:
#         type: string
#         nullable: true
#         description: >
#           Result flag as provided by the source (e.g., H, L, critical).
#       IsBlankOrUnsuccessfulAttempt:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the result represents a blank measurement or an unsuccessful attempt.
#       LabComponentKey:
#         type: bigint (foreign key to LabComponentDim)
#         nullable: false
#         description: >
#           Key identifying the lab component. Value -1 is the unspecified sentinel for
#           unmapped components and should be handled explicitly.
#       LabComponentResultKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the lab component result fact row.
#       LabTestKey_X:
#         type: bigint (foreign key to LabTestFact)
#         nullable: true
#         description: >
#           Key linking this component result to the parent lab test record.
#       NumericValue:
#         type: numeric
#         nullable: true
#         description: >
#           Numeric representation of the lab result when the raw value parses as a number.
#           Value is NULL for categorical/string results. Selecting on NumericValue
#           preferentially pulls abnormal results; use with care.
#       NumericBoundaryValue_X:
#         type: numeric
#         nullable: true
#         description: >
#           Numeric boundary value when the result uses inequality or thresholds (e.g.,
#           "<5").
#       NumericValueMaximum_X:
#         type: numeric
#         nullable: true
#         description: >
#           Maximum numeric value when the lab result represents a range.
#       NumericValueMinimum_X:
#         type: numeric
#         nullable: true
#         description: >
#           Minimum numeric value when the lab result represents a range.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient durable key associated with the lab result.
#       PrioritizedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for the prioritized result date (usually the result date); use for
#           date filtering and reporting.
#       PrioritizedInstant_X:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for the prioritized result date/time (result or collection, depending
#           on configuration).
#       ProcedureDurableKey:
#         type: bigint
#         nullable: true
#         description: >
#           Durable key for a procedure associated with the lab result, when mapped.
#       ReferenceValueHigh_X:
#         type: numeric
#         nullable: true
#         description: >
#           Upper bound of normal reference range for the numeric result.
#       ReferenceValueLow_X:
#         type: numeric
#         nullable: true
#         description: >
#           Lower bound of normal reference range for the numeric result.
#       ReferenceValueNormal_X:
#         type: string
#         nullable: true
#         description: >
#           Textual representation of the normal reference range or category.
#       ResultingLabDurableKey:
#         type: bigint (foreign key to LabDim)
#         nullable: true
#         description: >
#           Durable key identifying the lab facility or system that produced the result.
#       ResultInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when the result was finalized/reported. Often used as the main
#           result time for analyses.
#       SourceComboKey:
#         type: bigint (foreign key to LabComponentResultSourceBridge/SourceDim)
#         nullable: true
#         description: >
#           Composite source key for the lab result; use to trace origin system.
#       StructuredBoundaryOperator_X:
#         type: string
#         nullable: true
#         description: >
#           Encoded boundary operator for numeric results with thresholds (e.g., "<",
#           ">=").
#       Unit:
#         type: string
#         nullable: true
#         description: >
#           Unit of measure for NumericValue (e.g., mg/dL). For unitless values received
#           before Oct 2022 this may be "each"; later values may have NULL.
#       Value:
#         type: string
#         nullable: false
#         description: >
#           Categorical or string representation of the result when not numeric. For
#           numeric results, Value is the sentinel "*Not Applicable". Values of "*Masked in De-ID"
#           represent results masked during de-identification.
#
#   EncounterFact:
#     description: >
#       General encounter fact table. One row per encounter across settings, with
#       admission/discharge timing, department, encounter type, and coverage/payor
#       attributes. EncounterKey is the principal link to many other fact tables.
#     granularity: >
#       One row per encounter across settings (encounter-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the encounter record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the encounter record has been inferred rather than directly sourced.
#       AdmissionDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for encounter admission date (for inpatient/observation encounters).
#       AdmissionInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of admission; de-identified and time-shifted where applicable.
#       AdmittingProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for admitting provider associated with the encounter.
#       AgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key representing patient age at time of encounter.
#       ArrivalMeans:
#         type: string
#         nullable: true
#         description: >
#           Means of arrival, where applicable (e.g., ambulance, walk-in).
#       AttendingProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for attending provider for the encounter.
#       CodingComplete_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates coding for the encounter has been marked complete.
#       CodingStatus_X:
#         type: string
#         nullable: true
#         description: >
#           Categorical status of encounter coding (e.g., pending, complete, in progress).
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real encounter rows.
#       CoverageComboKey_X:
#         type: bigint (foreign key to CoverageBridgeX/CoverageDim)
#         nullable: true
#         description: >
#           Composite key representing coverage for the encounter.
#       Date:
#         type: date/datetime
#         nullable: true
#         description: >
#           General encounter date field (often the start date); use DateKey for filtering.
#       DateKey:
#         type: integer (DateKey)
#         nullable: false
#         description: >
#           Primary encounter date key; join to DateDim for calendar attributes.
#       DepartmentKey:
#         type: bigint (foreign key to DepartmentDim)
#         nullable: true
#         description: >
#           Department associated with the encounter (e.g., clinic, inpatient unit).
#       DerivedEncounterStatus:
#         type: string
#         nullable: true
#         description: >
#           Derived status of the encounter (e.g., completed, cancelled), harmonized
#           across organizations.
#       DerivedEncounterType_X:
#         type: string
#         nullable: true
#         description: >
#           Derived encounter type category (e.g., ED visit, inpatient, outpatient visit).
#       DischargeDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for discharge date or encounter end date.
#       DischargeDisposition:
#         type: string
#         nullable: true
#         description: >
#           Discharge disposition (e.g., home, SNF, expired, left AMA).
#       DischargeInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of discharge or encounter end.
#       DischargeProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Durable key for provider documented as discharging provider.
#       EncounterKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the encounter fact row. Used widely for linking other facts.
#       EndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing the encounter end date.
#       EndInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp representing the encounter end time.
#       IsEDVisit:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that the encounter is an ED visit. ED-specific detail is in
#           EdVisitFact.
#       IsHospitalAdmission:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the encounter represents a hospital admission episode.
#       IsHospitalOutpatientVisit:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates a hospital outpatient encounter (e.g., same-day surgery, diagnostic).
#       IsOutpatientFaceToFaceVisit:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates a face-to-face outpatient visit (clinic, office).
#       IsPregnant_X:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Derived flag indicating the patient was pregnant at time of encounter when
#           documentation supports it.
#       MarkedDoNotBillInsurance:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates encounter was flagged as not to be billed to insurance.
#       MarkedSelfPay:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates encounter was flagged as self-pay.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Durable key identifying the patient associated with the encounter.
#       PayorComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key representing payor information associated with the encounter.
#       PlaceOfServiceType_X:
#         type: string
#         nullable: true
#         description: >
#           Place of service category (e.g., office, inpatient hospital).
#       PrimaryCoverageFinancialClass_X:
#         type: string
#         nullable: true
#         description: >
#           Financial class derived from primary coverage for the encounter.
#       PrimaryPayorKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Key for the primary payor associated with the encounter.
#       ProviderDurableKey:
#         type: bigint (foreign key to ProviderDim)
#         nullable: true
#         description: >
#           Provider durable key; may reflect the primary provider associated with the
#           encounter.
#       SourceComboKey:
#         type: bigint (foreign key to EncounterSourceBridge/SourceDim)
#         nullable: true
#         description: >
#           Composite source key providing origin metadata for the encounter.
#       Type:
#         type: string
#         nullable: true
#         description: >
#           Encounter type as captured at the source (e.g., inpatient, outpatient,
#           emergency), prior to derived categorization.
#
#   LabComponentDim:
#     description: >
#       Lab component dimension table. One row per lab test component (e.g., specific
#       analyte/measurement). Contains naming, LOINC codes, units, and data type.
#     granularity: >
#       One row per lab component concept (component-level dimension).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the component definition.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the component record has been inferred.
#       Abbreviation:
#         type: string
#         nullable: true
#         description: >
#           Short abbreviation used for the component name in clinical workflows.
#       BaseName:
#         type: string
#         nullable: true
#         description: >
#           Base name representing the core analyte or concept underlying the component.
#       CommonName:
#         type: string
#         nullable: true
#         description: >
#           Common display name for the component as used in clinical practice.
#       DataType:
#         type: string
#         nullable: true
#         description: >
#           Data type expected for results of this component (e.g., numeric, string).
#       DefaultUnit:
#         type: string
#         nullable: true
#         description: >
#           Default unit for numeric results for this component (e.g., mg/dL).
#       LabComponentKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the lab component dimension row. Join from
#           LabComponentResultFact on LabComponentKey.
#       LoincCode:
#         type: string
#         nullable: true
#         description: >
#           LOINC code associated with the component, when mapped.
#       LoincName:
#         type: string
#         nullable: true
#         description: >
#           LOINC long/common name associated with the component.
#       Name:
#         type: string
#         nullable: true
#         description: >
#           Full display name for the component.
#       Subtype:
#         type: string
#         nullable: true
#         description: >
#           Component subtype used for further categorization within the type.
#       Type:
#         type: string
#         nullable: true
#         description: >
#           High-level component type (e.g., lab analyte, vital-related component).
#
#   LabComponentSetDim:
#     description: >
#       Lab component set dimension, representing value sets or curated lists of
#       lab components. Used to group components for cohorts or measure logic.
#     granularity: >
#       One row per lab component set membership (LabComponentSetKey, LabComponentKey).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the set.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the set record has been inferred.
#       DisplayName:
#         type: string
#         nullable: true
#         description: >
#           End-user-friendly display name for the component set.
#       LabComponentKey:
#         type: bigint (foreign key to LabComponentDim)
#         nullable: false
#         description: >
#           Component key that is a member of this set.
#       LabComponentSetKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the lab component set dimension row.
#       Name:
#         type: string
#         nullable: true
#         description: >
#           Internal name for the component set.
#       Trusted:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates whether the set is marked as trusted for use in analytics.
#       ValueSetEpicId:
#         type: string
#         nullable: true
#         description: >
#           Epic value set identifier for the component set when applicable.
#
#   BirthFact:
#     description: >
#       Birth fact table. One row per birth event, with linkages between mother and
#       baby, gestational age, delivery method, and labor/delivery timing and
#       characteristics.
#     granularity: >
#       One row per infant birth event (birth-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the birth record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the record has been inferred.
#       AntenatalSteroids:
#         type: string
#         nullable: true
#         description: >
#           Documentation of antenatal steroid use (e.g., yes/no or categorical detail).
#       AugmentationUsed:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that labor augmentation was used.
#       BabyDischargeWeight:
#         type: numeric
#         nullable: true
#         description: >
#           Baby's discharge weight, de-identified and potentially unit normalized.
#       BabyInpatientLengthOfStayInDays:
#         type: integer
#         nullable: true
#         description: >
#           Baby's inpatient length of stay in days.
#       BabyPatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: true
#         description: >
#           Durable key identifying the infant patient.
#       BirthAnesthesiaComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key referencing anesthesia categories used during birth.
#       BirthAugmentationComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key referencing labor augmentation methods.
#       BirthAugmentationIndicationComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key referencing indication(s) for augmentation.
#       BirthCervicalRipeningComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key representing cervical ripening methods.
#       BirthCesareanIndicationComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key representing indications for cesarean delivery.
#       BirthDateKey:
#         type: integer (DateKey)
#         nullable: false
#         description: >
#           DateKey for the birth date.
#       BirthEpisiotomyComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key representing episiotomy data.
#       BirthInductionComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key referencing induction methods.
#       BirthInductionIndicationComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key for indications for induction.
#       BirthInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of birth; de-identified and time-shifted.
#       BirthKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the birth fact record.
#       BirthLength:
#         type: numeric
#         nullable: true
#         description: >
#           Infant length at birth (e.g., centimeters).
#       BirthRuptureTypeComboKey:
#         type: bigint
#         nullable: true
#         description: >
#           Composite key representing type of membrane rupture associated with birth.
#       BirthTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for birth time.
#       BirthWeight:
#         type: numeric
#         nullable: true
#         description: >
#           Birth weight measure; often redundant with BirthWeightGrams but may use
#           different units.
#       BirthWeightGrams:
#         type: numeric
#         nullable: true
#         description: >
#           Birth weight in grams.
#       BreastMilkGiven:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates breast milk was given to the infant during the birth encounter.
#       CervicalRipeningDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when cervical ripening started.
#       CervicalRipeningInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when cervical ripening began.
#       CervicalRipeningTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for cervical ripening start time.
#       CesareanDelivery:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the delivery was cesarean.
#       CesareanExpected:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates whether a cesarean delivery was expected/planned.
#       CordClampDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when the umbilical cord was clamped.
#       CordClampInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of cord clamp.
#       CordClampTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for cord clamp time.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real birth records.
#       DeliveryMethod:
#         type: string
#         nullable: true
#         description: >
#           Delivery method category (e.g., spontaneous vaginal, assisted, cesarean).
#       DilationCompleteInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when cervical dilation reached completion.
#       DilationCompleteDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for dilation completion.
#       DilationCompleteTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for dilation completion.
#       EpiduralOrSpinalGiven:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates whether epidural or spinal anesthesia was given.
#       FirstStageLengthMinutes:
#         type: integer
#         nullable: true
#         description: >
#           Duration in minutes of first stage of labor.
#       ForcepsAttempted:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates forceps were attempted during delivery.
#       ForcepsDelivery:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates delivery ultimately used forceps.
#       GestationalAgeDays:
#         type: integer
#         nullable: true
#         description: >
#           Gestational age in days at birth.
#       GestationalAgeZeroDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing gestational age zero point (e.g., conception or
#           standardized reference).
#       HeadCircumference:
#         type: numeric
#         nullable: true
#         description: >
#           Infant head circumference measured at birth.
#       InductionUsed:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates labor induction was used.
#       LaborAttempted:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates labor was attempted (may be false for pre-labor cesarean).
#       LaborStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when labor started.
#       LaborStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when labor started.
#       LaborStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for labor start.
#       LaborType:
#         type: string
#         nullable: true
#         description: >
#           Categorical description of labor type (e.g., spontaneous, induced, augmented).
#       LivingStatus:
#         type: string
#         nullable: true
#         description: >
#           Infant living status at birth (e.g., liveborn, stillborn).
#       MotherAgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key representing mother's age at time of delivery.
#       MotherArrivalDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when mother arrived at the birthing facility.
#       MotherArrivalInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of mother's arrival at the birthing facility.
#       MotherArrivalTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for mother's arrival time.
#       MotherEncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter key for mother's delivery encounter.
#       MotherPatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: true
#         description: >
#           Durable key identifying the mother.
#       MultipleDeliveryCount:
#         type: integer
#         nullable: true
#         description: >
#           Number of infants delivered in a multiple birth (e.g., twins, triplets).
#       MultipleDeliveryOrder:
#         type: integer
#         nullable: true
#         description: >
#           Order of the infant within multiple delivery (e.g., first of twins).
#       NeonatalDemise:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates neonatal demise occurred.
#       NonBreastMilkGiven:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates non-breast milk was given to the infant.
#       PlacentaDeliveryDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when placenta was delivered.
#       PlacentaDeliveryInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of placenta delivery.
#       PlacentaDeliveryTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for placenta delivery.
#       PlacentaMethod:
#         type: string
#         nullable: true
#         description: >
#           Method of placenta delivery (e.g., spontaneous, manual removal).
#       PregnancyKey:
#         type: bigint (foreign key to PregnancyFact)
#         nullable: true
#         description: >
#           Key linking the birth to the pregnancy episode.
#       PresentationType:
#         type: string
#         nullable: true
#         description: >
#           Fetal presentation type (e.g., vertex, breech).
#       PresentationVertex:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates vertex presentation.
#       PushingStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when pushing started.
#       PushingStartInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when pushing began.
#       PushingStartTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for pushing start.
#       RuptureDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when membranes ruptured.
#       RuptureInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp of membrane rupture.
#       RuptureOfMembranesToDeliverySeconds:
#         type: integer
#         nullable: true
#         description: >
#           Duration in seconds between membrane rupture and delivery.
#       RuptureTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for rupture time.
#       SecondStageLengthMinutes:
#         type: integer
#         nullable: true
#         description: >
#           Duration in minutes of second stage of labor.
#       SeverePerinealLacerationOccurred:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates severe perineal laceration (e.g., 3rd/4th degree) occurred.
#       SkinToSkinDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when skin-to-skin contact first occurred.
#       SkinToSkinInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp when skin-to-skin contact began.
#       SkinToSkinTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for skin-to-skin start.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key describing origin of the birth record.
#       SpontaneousVaginalDelivery:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates delivery was spontaneous vaginal.
#       ThirdStageLengthMinutes:
#         type: integer
#         nullable: true
#         description: >
#           Duration in minutes of third stage of labor (placental).
#       TotalApgarFiveMinute:
#         type: integer
#         nullable: true
#         description: >
#           Total Apgar score at 5 minutes.
#       TotalApgarOneMinute:
#         type: integer
#         nullable: true
#         description: >
#           Total Apgar score at 1 minute.
#       TotalApgarTenMinute:
#         type: integer
#         nullable: true
#         description: >
#           Total Apgar score at 10 minutes.
#       VacuumAttempted:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates vacuum assistance was attempted.
#       VacuumDelivery:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates delivery ultimately used vacuum assistance.
#
#   MedicationOrderFact:
#     description: >
#       Medication order fact table. One row per medication order at the order grain.
#       Holds order-level fields like quantity, route, frequency, and indications.
#       MedicationKey = -1 for mixtures; use MedicationOrderComponentFact to identify
#       ingredients when mixtures are in scope.
#     granularity: >
#       One row per medication order (order-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the medication order.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the order record has been inferred.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; often 1 for real orders.
#       DepartmentKey:
#         type: bigint (foreign key to DepartmentDim)
#         nullable: true
#         description: >
#           Department associated with the order (e.g., ordering department).
#       DiscontinueReason:
#         type: string
#         nullable: true
#         description: >
#           Reason documented for discontinuing the order.
#       DoseUnit:
#         type: string
#         nullable: true
#         description: >
#           Unit for ordered dose (e.g., mg, mL, each). Unitless medications before Oct
#           2022 often use "each"; later data may use NULL.
#       DurationKey:
#         type: bigint (foreign key to DurationDim)
#         nullable: true
#         description: >
#           Key referencing the duration category of order when explicitly specified.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter associated with the order. For administrations in another context,
#           this may be the ordering encounter, not administering encounter.
#       EndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           Order end date key. Often represents discontinuation or system stop and is
#           not a reliable indicator of actual therapy stop.
#       FifthIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Fifth indication for use associated with the order, when documented.
#       FirstIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Primary indication for medication use as documented on the order.
#       FourthIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Fourth indication for use.
#       Frequency:
#         type: string
#         nullable: true
#         description: >
#           Ordered frequency (e.g., BID, q6h, PRN text). Values vary across orgs;
#           map carefully when computing daily dose.
#       IsOutpatientMode:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates order mode flagged as outpatient. Mode is not the same as care
#           setting; clinic-administered medications may be inpatient mode.
#       MedicationKey:
#         type: bigint (foreign key to MedicationDim)
#         nullable: false
#         description: >
#           Key to the medication record. Value -1 indicates mixture orders, which must
#           be resolved via MedicationOrderComponentFact for ingredients.
#       MedicationOrderKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the medication order fact record.
#       MinimumDose:
#         type: numeric
#         nullable: true
#         description: >
#           Minimum dose per administration, used for titrated or range orders.
#       Mode:
#         type: string
#         nullable: true
#         description: >
#           Order mode (e.g., inpatient, outpatient). Distinct from Type_X and care
#           setting.
#       MorphineEquivalentDailyDosage:
#         type: numeric
#         nullable: true
#         description: >
#           Computed morphine-equivalent daily dose for opioid orders when mapping is
#           available.
#       OrderedDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey when the order was placed/signed.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient associated with the medication order.
#       Quantity:
#         type: numeric
#         nullable: true
#         description: >
#           Total quantity ordered (usually per fill for outpatient orders).
#       RefillsWritten:
#         type: integer
#         nullable: true
#         description: >
#           Number of refills originally written on the order.
#       Route:
#         type: string
#         nullable: true
#         description: >
#           Route associated with the medication record for the order (e.g., oral, IV).
#           Use this to scope medications by route.
#       SecondIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Second indication for use.
#       SixthIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Sixth indication for use.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key providing origin metadata for the order.
#       StartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           Start date key for the order. Masked or defaulted for some patient-reported
#           and masked medications.
#       ThirdIndicationForUse:
#         type: string
#         nullable: true
#         description: >
#           Third indication for use.
#       Type_X:
#         type: string
#         nullable: true
#         description: >
#           Order type category (e.g., prescription, administered medication, historical).
#
#   MedicationDispenseFact:
#     description: >
#       Outpatient medication dispense fact table. Rows represent fills/dispenses
#       from ambulatory or external pharmacies. Includes days supply, NDC, refills,
#       and dispense timing. Only outpatient dispenses are present.
#     granularity: >
#       One row per outpatient dispense/fill event (dispense-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the dispense record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the record has been inferred.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; usually 1 for real dispenses.
#       DaysSupply:
#         type: integer
#         nullable: true
#         description: >
#           Number of days of therapy the dispense is expected to cover.
#       DispenseDataSourceType_X:
#         type: string
#         nullable: true
#         description: >
#           Source type for the dispense (e.g., Willow Dispense History vs external).
#           Use to distinguish internal vs external dispenses.
#       DoseUnit_X:
#         type: string
#         nullable: true
#         description: >
#           Dose unit representation for the dispense, when captured.
#       FilledDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing dispense/fill date (or claim filed date for external).
#       FilledInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for dispense/fill date. For mail order, this is not delivery date.
#       FilledTimeOfDayKey:
#         type: integer (TimeOfDayKey)
#         nullable: true
#         description: >
#           Time-of-day key for fill time.
#       FillNumber:
#         type: integer
#         nullable: true
#         description: >
#           Fill number (0 for initial, >0 for refills) for Willow dispenses; inconsistent
#           for external dispenses.
#       FirstDispense:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates this row represents the first dispense for the corresponding order
#           for Willow; less reliable for external.
#       Frequency_X:
#         type: string
#         nullable: true
#         description: >
#           Dispensed frequency text when carried at dispense level.
#       MedicationDispenseKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the dispense fact record.
#       MedicationKey:
#         type: bigint (foreign key to MedicationDim)
#         nullable: false
#         description: >
#           Medication associated with the dispense. Masked medications may appear under
#           generic "Masked Medication".
#       MedicationOrderKey:
#         type: bigint (foreign key to MedicationOrderFact)
#         nullable: true
#         description: >
#           Order key associated with the dispense. Reliable for Willow ambulatory;
#           inconsistent for external dispenses.
#       MedicationOrderWrittenDateKey_X:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey referencing when associated medication order was written.
#       MinimumDose_X:
#         type: numeric
#         nullable: true
#         description: >
#           Minimum dose per administration reflected in this dispense, if provided.
#       Mode:
#         type: string
#         nullable: true
#         description: >
#           Order mode associated with the dispense; may reflect outpatient/inpatient
#           mode, but external interfaces vary.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient associated with the dispense.
#       PrimaryComponentQuantity:
#         type: numeric
#         nullable: true
#         description: >
#           Quantity of primary component dispensed.
#       PrimaryComponentQuantityUnit:
#         type: string
#         nullable: true
#         description: >
#           Unit associated with primary component quantity.
#       PrimaryComponentUnformattedNdc:
#         type: string
#         nullable: true
#         description: >
#           Unformatted NDC representing potential package for the medication record;
#           not necessarily the exact NDC dispensed.
#       RawCodeSystem_X:
#         type: string
#         nullable: true
#         description: >
#           Original code system of the dispense record (e.g., NDC).
#       RawCodeValue_X:
#         type: string
#         nullable: true
#         description: >
#           Raw code value representing the product in the source system.
#       ReadyToDispenseDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing when the medication became ready to dispense; equivalent
#           to FilledDateKey for Willow; claim filed date for external dispenses.
#       RefillsRemaining_X:
#         type: integer
#         nullable: true
#         description: >
#           Remaining refills after this dispense. Reliable for Willow; external mappings
#           vary and can go negative when splits occur.
#       RefillsWritten_X:
#         type: integer
#         nullable: true
#         description: >
#           Number of refills written, as represented at the dispense level. Pre-2019
#           external data primarily uses this.
#       Route_X:
#         type: string
#         nullable: true
#         description: >
#           Route representation at dispense level when documented.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key for the dispense.
#       SupplyEndDateKey_X:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey approximating when the supply would end; often derived from days
#           supply and fill date.
#
#   MedicationAdministrationFact:
#     description: >
#       Medication administration fact table representing MAR actions. Contains
#       administrations and non-given actions except Due. Filter using
#       ActionIsMedAdministration or AdministrationAction values when selecting
#       given doses. Incomplete data for some encounters; union with
#       MedicationOrderComponentFact where Type_X = 'Administered Medication'
#       can improve completeness.
#     granularity: >
#       One row per MAR action for a medication order (administration-action-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the administration row.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates an inferred administration record.
#       AdministrationDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing administration date for the MAR action.
#       AdministrationDepartmentKey:
#         type: bigint (foreign key to DepartmentDim)
#         nullable: true
#         description: >
#           Department where the administration action occurred or was recorded.
#       AdministrationInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp for the administration action (e.g., Given, Held). De-identified.
#       AdministrationRoute:
#         type: string
#         nullable: true
#         description: >
#           Route documented at administration time (e.g., PO, IV). Some intraoperative
#           medications may have route only at administration.
#       AgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key representing age at administration time.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real MAR rows. Placeholder rows
#           have nonpositive keys and often NULL Count.
#       EncounterAdmissionInstant:
#         type: datetime
#         nullable: true
#         description: >
#           Timestamp representing admission associated with the encounter, carried
#           on the administration record.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter associated with the medication order; for administrations in a
#           different contact, this remains the ordering encounter.
#       MedicationAdministrationKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the medication administration fact row.
#       MedicationKey:
#         type: bigint (foreign key to MedicationDim)
#         nullable: false
#         description: >
#           Key to the medication record. Value -1 for mixture administrations; identify
#           ingredients from order components when mixtures are in scope.
#       MedicationOrderKey:
#         type: bigint (foreign key to MedicationOrderFact)
#         nullable: true
#         description: >
#           Links administration to its parent medication order. Never join directly to
#           MedicationOrderComponentFact; route through MedicationOrderFact.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient associated with the medication administration action.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key for the administration action, referencing origin system.
#
#   PregnancyFact:
#     description: >
#       Pregnancy episode fact table. One row per pregnancy episode, with
#       episode-level dates, parity/gravida counts, delivery outcomes, and
#       pre-/post-delivery anthropometrics. Linked to patients via PatientDurableKey
#       and to births via BirthFact.PregnancyKey.
#     granularity: >
#       One row per pregnancy episode per patient (pregnancy-episode-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the pregnancy episode record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the pregnancy record has been inferred rather than directly sourced.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real pregnancy episode rows.
#       EpisodeEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing end of the pregnancy episode (e.g., delivery, loss).
#       EpisodeStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing start of the pregnancy episode, often based on
#           estimated conception or first documentation.
#       HadCesarean:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that at least one delivery in the pregnancy was cesarean.
#       HadFetalDemise:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that fetal demise occurred during this pregnancy.
#       HadNeonatalDemise:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that neonatal demise occurred for at least one infant in this
#           pregnancy.
#       HasDelivery:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates that the pregnancy episode includes at least one delivery event.
#       HasDeliverySummary:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates a delivery summary record exists for this pregnancy.
#       IsHistorical:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates pregnancy is historical (documented retrospectively rather than
#           managed in real time).
#       LastDeliveryDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for the most recent delivery associated with this pregnancy.
#       LastDeliveryGestationalAge:
#         type: integer
#         nullable: true
#         description: >
#           Gestational age (e.g., in weeks or days) at the most recent delivery.
#       NumberOfFetuses:
#         type: integer
#         nullable: true
#         description: >
#           Number of fetuses associated with this pregnancy (e.g., singleton, twins).
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient associated with this pregnancy episode.
#       PostDeliveryBmi_X:
#         type: numeric
#         nullable: true
#         description: >
#           BMI measured post-delivery, derived by source logic and de-identified.
#       PreDeliveryBmi_X:
#         type: numeric
#         nullable: true
#         description: >
#           BMI measured pre-delivery, derived by source logic. Use carefully as
#           measurement timing may vary.
#       PregnancyAbortionCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of abortions (therapeutic or spontaneous) documented for this pregnancy
#           episode if tracked at episode level.
#       PregnancyEctopicCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of ectopic pregnancies associated with this episode context.
#       PregnancyEstimatedEndDate:
#         type: date/datetime
#         nullable: true
#         description: >
#           Estimated pregnancy end date (e.g., EDD); use PregnancyEstimatedEndDateKey
#           for filtering and reporting.
#       PregnancyEstimatedEndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for estimated pregnancy end date.
#       PregnancyEstimatedStartDate:
#         type: date/datetime
#         nullable: true
#         description: >
#           Estimated pregnancy start date (e.g., based on LMP or ultrasound).
#       PregnancyEstimatedStartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for estimated pregnancy start date.
#       PregnancyGravidaCount:
#         type: integer
#         nullable: true
#         description: >
#           Gravida count (number of prior pregnancies including current) at the time
#           of this pregnancy.
#       PregnancyKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the pregnancy episode fact record. Links to
#           BirthFact.PregnancyKey.
#       PregnancyParaCount:
#         type: integer
#         nullable: true
#         description: >
#           Para count (number of prior pregnancies resulting in viable births).
#       PregnancyPretermCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of prior preterm births associated with this pregnancy context.
#       PregnancyPriorFetalDemiseCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of prior fetal demises documented for the patient at the time of this
#           pregnancy.
#       PregnancyPriorLiveBirthCount:
#         type: integer
#         nullable: true
#         description: >
#           Number of prior live births documented at the time of this pregnancy.
#       PregnancySpontaneousAbortionCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of spontaneous abortions associated with this pregnancy context.
#       PregnancyStartAgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key representing the patient's age at pregnancy start.
#       PregnancyTermCount:
#         type: integer
#         nullable: true
#         description: >
#           Number of term births associated with this pregnancy episode.
#       PregnancyTherapeuticAbortionCount:
#         type: integer
#         nullable: true
#         description: >
#           Count of therapeutic abortions associated with this pregnancy context.
#       PregravidBmi:
#         type: numeric
#         nullable: true
#         description: >
#           BMI measured prior to pregnancy (pregravid), when documented.
#       PregravidWeight:
#         type: numeric
#         nullable: true
#         description: >
#           Weight prior to pregnancy (pregravid), de-identified and possibly unit
#           normalized.
#       PriorCesarean:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the patient has a history of prior cesarean delivery before this
#           pregnancy.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key representing origin metadata for the pregnancy record.
#       WorkingEstimatedDateOfDeliveryKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey for working estimated date of delivery (EDD) used during pregnancy
#           care.
#
#   DiagnosisTerminologyDim:
#     description: >
#       Diagnosis terminology dimension table. One row per diagnosis code representation
#       in a particular terminology (e.g., ICD-10-CM, SNOMED). Preferred source for
#       code-level details for diagnoses used in DiagnosisEventFact and other tables.
#     granularity: >
#       One row per diagnosis code representation in a terminology (diagnosis-code-level
#       dimension).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the diagnosis terminology record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the terminology record has been inferred.
#       DiagnosisKey:
#         type: bigint (foreign key to DiagnosisDim)
#         nullable: false
#         description: >
#           Key linking this terminology record to the core diagnosis concept in
#           DiagnosisDim.
#       DiagnosisTerminologyKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the diagnosis terminology dimension row.
#       DisplayString:
#         type: string
#         nullable: true
#         description: >
#           Human-readable display string for the diagnosis concept, often combining
#           name and code in a user-friendly format.
#       GroupedNameAndCode:
#         type: string
#         nullable: true
#         description: >
#           Grouped representation combining name and code for analytic grouping and
#           reporting.
#       NameAndCode:
#         type: string
#         nullable: true
#         description: >
#           Concatenation of diagnosis name and code, often used as a canonical label.
#       ReferenceBillingCode:
#         type: string
#         nullable: true
#         description: >
#           Primary billing code value for the diagnosis in the referenced terminology
#           (e.g., ICD-10-CM code).
#       Parent:
#         type: string
#         nullable: true
#         description: >
#           Parent concept identifier or hierarchy label within the terminology, used
#           for organizing related diagnoses.
#       TerminologyConceptKey:
#         type: bigint (foreign key to TerminologyConceptDim)
#         nullable: true
#         description: >
#           Key to the underlying terminology concept record.
#       Type:
#         type: string
#         nullable: true
#         description: >
#           Terminology type (e.g., ICD-10-CM, SNOMED CT, internal code set).
#       Value:
#         type: string
#         nullable: true
#         description: >
#           Raw value for the terminology representation; often the code or canonical
#           identifier.
#
#   DurationDim:
#     description: >
#       Duration dimension table. Encodes durations in days, weeks, months, years
#       and display strings. Used to represent duration fields as dimensional keys
#       (e.g., MedicationOrderFact.DurationKey).
#     granularity: >
#       One row per distinct duration concept (duration-level dimension).
#     columns:
#       DurationKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the duration dimension row.
#       Days:
#         type: integer
#         nullable: true
#         description: >
#           Duration expressed in days.
#       DaysDisplayString:
#         type: string
#         nullable: true
#         description: >
#           Human-readable display string for duration in days (e.g., "7 days").
#       DisplayString:
#         type: string
#         nullable: true
#         description: >
#           General display string representation for the duration, potentially combining
#           multiple units.
#       Months:
#         type: integer
#         nullable: true
#         description: >
#           Duration expressed in months.
#       MonthsDisplayString:
#         type: string
#         nullable: true
#         description: >
#           Human-readable display string for duration in months (e.g., "3 months").
#       Weeks:
#         type: integer
#         nullable: true
#         description: >
#           Duration expressed in weeks.
#       WeeksDisplayString:
#         type: string
#         nullable: true
#         description: >
#           Human-readable display string for duration in weeks (e.g., "2 weeks").
#       Years:
#         type: integer
#         nullable: true
#         description: >
#           Duration expressed in years.
#       YearsDisplayString:
#         type: string
#         nullable: true
#         description: >
#           Human-readable display string for duration in years (e.g., "1 year").
#
#   ProblemListFact:
#     description: >
#       Problem list fact table. One row per problem list entry for a patient,
#       containing diagnosis keys, dates, chronic flags, and status. Used for
#       longitudinal conditions separate from billing diagnoses.
#     granularity: >
#       One row per problem list entry per patient (problem-entry-level fact).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the problem list entry.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the problem list record has been inferred.
#       AgeKey:
#         type: bigint
#         nullable: true
#         description: >
#           Age key representing patient age at the time the problem was recorded.
#       Chronic:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the problem is flagged as chronic in the source system.
#       Count:
#         type: integer
#         nullable: false
#         description: >
#           Cosmos row count indicator; typically 1 for real problem list entries.
#       DiagnosisKey:
#         type: bigint (foreign key to DiagnosisDim/DiagnosisTerminologyDim)
#         nullable: false
#         description: >
#           Key referencing the diagnosis concept associated with the problem list entry.
#       EncounterKey:
#         type: bigint (foreign key to EncounterFact)
#         nullable: true
#         description: >
#           Encounter associated with initial documentation or modification of the problem.
#       EndDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing when the problem was resolved or inactivated.
#       IsCancerProblem:
#         type: boolean (flag)
#         nullable: true
#         description: >
#           Indicates the problem is classified as a cancer-related problem.
#       PatientDurableKey:
#         type: bigint (foreign key to PatientDim.DurableKey)
#         nullable: false
#         description: >
#           Patient associated with the problem list entry.
#       ProblemListKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the problem list fact record.
#       SourceComboKey_X:
#         type: bigint
#         nullable: true
#         description: >
#           Composite source key for the problem list record, used for origin metadata.
#       StartDateKey:
#         type: integer (DateKey)
#         nullable: true
#         description: >
#           DateKey representing when the problem was first recorded or became active.
#       Status:
#         type: string
#         nullable: true
#         description: >
#           Problem status (e.g., active, resolved, inactive).
#       Type:
#         type: string
#         nullable: true
#         description: >
#           Problem type category (e.g., medical, surgical, social), depending on local
#           configuration.
#
#   TerminologyConceptDim:
#     description: >
#       Terminology concept dimension table. One row per concept in a terminology
#       (e.g., a code or concept in ICD-10-CM, SNOMED CT, RxNorm, ATC, internal
#       value set). Serves as a generic concept backbone that other terminology-
#       specific dimensions (such as DiagnosisTerminologyDim or MedicationCodeDim)
#       reference via TerminologyConceptKey.
#     granularity: >
#       One row per terminology concept (concept-level dimension).
#     columns:
#       _IsDeleted:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Logical deletion flag for the terminology concept record.
#       _IsInferred:
#         type: boolean (flag)
#         nullable: false
#         description: >
#           Indicates the terminology concept record has been inferred rather than
#           directly sourced; useful for lineage and data quality checks.
#       Concept:
#         type: string
#         nullable: true
#         description: >
#           Concept identifier string as used within the terminology or value set
#           (for example, a code or concept ID).
#       Name:
#         type: string
#         nullable: true
#         description: >
#           Human-readable name or label for the terminology concept.
#       StandardName:
#         type: string
#         nullable: true
#         description: >
#           Standardized or normalized name for the concept, used to harmonize
#           naming across contributors and terminologies.
#       TerminologyConceptKey:
#         type: bigint
#         nullable: false
#         description: >
#           Primary key for the terminology concept dimension row. Referenced by
#           other terminology tables such as DiagnosisTerminologyDim and
#           TerminologyConceptSetDim.
#
# === END FILE: YAMLs/datadictionary.yaml ===
# === BEGIN FILE: YAMLs/recipes.yaml SHA256: 974d2fed63e4b6449dc544cbc0559d3b5c4163ff6dfba28595b1cc65b770f3fb SIZE: 12725 ===
# batching_recipes:
#   - name: state
#     description: Split each expanded PK table by patient state abbreviation.
#     kind: column_values
#     applies_to: PKTable
#     column: StateOrProvinceAbbreviation
#     values: all
#     separate_parquets: true
#
#   - name: sex
#     description: Split each expanded PK table by patient sex.
#     kind: column_values
#     applies_to: PKTable
#     column: Sex
#     values:
#       - Female
#       - Male
#     separate_parquets: true
#
#   - name: chunk
#     description: Split each expanded PK table into row-count chunks; downstream fact tables join to the matching PK chunk.
#     kind: row_chunk
#     applies_to: PKTable
#     rows_per_batch: required
#     separate_parquets: true
#
# recipes:
#   - name: PatientWithDx
#     description: First DiagnosisEvent of diseaseX for patient, with patient info (sex, birthdate etc) and Index Event info (Age at Diagnosis, ICD code, etc.)
#     type: PK
#     pull_this_cycle: true
#     dest_table: Patients
#     dedup_keys:
#       - [PatientDurableKey]
#     dedup_order_by:
#       - IndexDate
#     columns:
#       - source: dxf.PatientDurableKey
#         name: PatientDurableKey
#         type: BIGINT
#         nullable: false
#
#       - source: p.Sex
#         name: Sex
#         type: VARCHAR(50)
#         nullable: true
#
#       - source: dt.NameAndCode
#         name: ICDName
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dur.Years
#         name: AgeAtIndexDiagnosis
#         type: BIGINT
#         nullable: true
#
#       - source: p.BirthDate
#         name: BirthDate
#         type: DATE
#         nullable: true
#
#       - source: p.StateOrProvince
#         name: StateOrProvince
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.StateOrProvinceAbbreviation
#         name: StateOrProvinceAbbreviation
#         type: VARCHAR(300)
#         nullable: false
#
#       - source: p.Country
#         name: Country
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: dt.Value
#         name: ICDCode
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dxf.StartDateKey
#         name: IndexDate
#         type: INT
#         nullable: true
#
#       # Everything else:
#       - source: dxf.DiagnosisEventKey
#         name: DiagnosisEventKey
#         type: BIGINT
#         nullable: false
#
#       - source: dxf.EncounterKey
#         name: IndexEncounter
#         type: BIGINT
#         nullable: false
#
#       - source: p.DurableKey
#         name: DurableKey
#         type: BIGINT
#         nullable: false
#
#       - source: p.FirstRace
#         name: FirstRace
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.SecondRace
#         name: SecondRace
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.ThirdRace
#         name: ThirdRace
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.FourthRace
#         name: FourthRace
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.FifthRace
#         name: FifthRace
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: p.SviRacialEthnicMinorityStatusPctlRankByZip2020_X
#         name: SviRacialEthnicMinorityStatusPctlRankByZip2020_X
#         type: FLOAT
#         nullable: true
#
#       - source: p.SviOverallPctlRankByZip2020_X
#         name: SviOverallPctlRankByZip2020_X
#         type: FLOAT
#         nullable: true
#
#       - source: p.SviSocioeconomicPctlRankByZip2020_X
#         name: SviSocioeconomicPctlRankByZip2020_X
#         type: FLOAT
#         nullable: true
#
#     filter:
#       from:
#         - DiagnosisEventFact as dxf
#       join:
#         - "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey"
#         - "INNER JOIN PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey"
#         - "INNER JOIN DurationDim AS dur ON dur.DurationKey = dxf.AgeKey"
#       where:
#         - "dxf.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "dxf.StartDateKey >= 0"
#         - "dxf._IsDeleted = 0"
#         - "dt._IsDeleted = 0"
#         - "p.IsValid = 1"
#         - "p.IsCurrent = 1"
#         - "p.UseInCosmosAnalytics_X = 1"
#         - "dt.Type IN ('ICD-10-AM','ICD-10-CA','ICD-10-CM', 'ICD-9 CM')"
#         - "{{sql_condition('dt.Value', ICD_Value)}}"
#
#   - name: IndexDiagnosis
#     description: Patient's first time diagnoses with the condition, with date, age at diagnosis, event data
#     dedup_keys:
#       - [BillingCodeValue]
#     dedup_order_by: 
#       - IndexDate
#     pull_this_cycle: true 
#     type: fact
#     dest_table: OtherDiagnoses
#     columns:
#       - source: def.DiagnosisEventKey
#         name: DiagnosisEventKey
#         type: BIGINT
#         nullable: false
#
#       - source: tc.StandardName
#         name: BillingCodeType
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dt.Value
#         name: BillingCodeValue
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dt.NameAndCode
#         name: NameAndCode
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dur.Years
#         name: AgeAtDiagnosis
#         type: INT
#         nullable: true
#
#       - source: def.IsPrimary
#         name: IsPrimary
#         type: BIT
#         nullable: true
#
#       - source: dt.ReferenceBillingCode
#         name: IsReferenceBillingCode
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: def.Type
#         name: TypeOfDx
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: tc.Concept
#         name: TerminologyConcept
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: tc.Name
#         name: TerminologyName
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: def.Status
#         name: Status
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: def.StartDateKey
#         name: StartDateKey
#         type: INT
#         nullable: true
#
#       - source: def.EndDateKey
#         name: EndDateKey
#         type: INT
#         nullable: true
#
#       - source: def.PatientDurableKey
#         name: PatientDurableKey
#         type: BIGINT
#         nullable: false
#
#       - source: def.DiagnosisKey
#         name: DiagnosisKey
#         type: BIGINT
#         nullable: false
#
#       - source: def.EncounterKey
#         name: EncounterKey
#         type: BIGINT
#         nullable: false
#
#     filter:
#       from:
#         - DiagnosisEventFact AS def
#       join:
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.PatientDurableKey = def.PatientDurableKey AND pk.DiagnosisEventKey <> def.DiagnosisEventKey"
#         - "LEFT JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"
#         - "LEFT JOIN TerminologyConceptDim AS tc ON tc.TerminologyConceptKey = dt.TerminologyConceptKey"
#         - "INNER JOIN DurationDim AS dur ON dur.DurationKey = def.AgeKey"
#       where:
#         - "def._IsDeleted = 0"
#         - "dt._IsDeleted = 0"
#         - "def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM')"
#
#
#
#   - name: OtherDiagnoses
#     description: Patient's diagnoses besides the index diagnosis - one per diagnosis, not time-based. For seeing if they have other conditions
#     dedup_keys:
#       - [BillingCodeValue]
#     pull_this_cycle: true 
#     type: fact
#     dest_table: OtherDiagnoses
#     columns:
#       - source: def.DiagnosisEventKey
#         name: DiagnosisEventKey
#         type: BIGINT
#         nullable: false
#
#       - source: tc.StandardName
#         name: BillingCodeType
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dt.Value
#         name: BillingCodeValue
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dt.NameAndCode
#         name: NameAndCode
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dur.Years
#         name: AgeAtDiagnosis
#         type: INT
#         nullable: true
#
#       - source: def.IsPrimary
#         name: IsPrimary
#         type: BIT
#         nullable: true
#
#       - source: dt.ReferenceBillingCode
#         name: IsReferenceBillingCode
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: def.Type
#         name: TypeOfDx
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: tc.Concept
#         name: TerminologyConcept
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: tc.Name
#         name: TerminologyName
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: def.Status
#         name: Status
#         type: VARCHAR(100)
#         nullable: true
#
#       - source: def.StartDateKey
#         name: StartDateKey
#         type: INT
#         nullable: true
#
#       - source: def.EndDateKey
#         name: EndDateKey
#         type: INT
#         nullable: true
#
#       - source: def.PatientDurableKey
#         name: PatientDurableKey
#         type: BIGINT
#         nullable: false
#
#       - source: def.DiagnosisKey
#         name: DiagnosisKey
#         type: BIGINT
#         nullable: false
#
#       - source: def.EncounterKey
#         name: EncounterKey
#         type: BIGINT
#         nullable: false
#
#     filter:
#       from:
#         - DiagnosisEventFact AS def
#       join:
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.PatientDurableKey = def.PatientDurableKey AND pk.DiagnosisEventKey <> def.DiagnosisEventKey"
#         - "LEFT JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"
#         - "LEFT JOIN TerminologyConceptDim AS tc ON tc.TerminologyConceptKey = dt.TerminologyConceptKey"
#         - "INNER JOIN DurationDim AS dur ON dur.DurationKey = def.AgeKey"
#       where:
#         - "def._IsDeleted = 0"
#         - "dt._IsDeleted = 0"
#         - "def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM')"
#
#   - name: OtherHospitalizations
#     type: fact
#     pull_this_cycle: true
#     dest_table: OtherHospitalizations
#     dedup_keys:
#       - [InpatientEncounterKey]
#     columns:
#       - source: haf.HospitalAdmissionKey
#         name: HospitalAdmissionKey
#         type: BIGINT
#         nullable: false
#
#       - source: haf.EncounterKey
#         name: InpatientEncounterKey
#         type: BIGINT
#         nullable: false
#
#       - source: haf.PatientDurableKey
#         name: PatientDurableKey
#         type: BIGINT
#         nullable: false
#
#       - source: haf.AdmissionDateKey
#         name: AdmissionDateKey
#         type: INT
#         nullable: false
#
#       - source: haf.InpatientAdmissionInstant
#         name: InpatientAdmissionInstant
#         type: DATETIME2(7)
#         nullable: false
#
#       - source: haf.DischargeDateKey
#         name: DischargeDateKey
#         type: INT
#         nullable: true
#
#       - source: haf.DischargeInstant
#         name: DischargeInstant
#         type: DATETIME2(7)
#         nullable: true
#
#       - source: haf.DischargeDisposition
#         name: DischargeDisposition
#         type: VARCHAR(300)
#         nullable: true
#
#       - source: haf.InpatientLengthOfStayInDays
#         name: InpatientLengthOfStayInDays
#         type: INT
#         nullable: true
#
#       - source: haf.LengthOfStayInDays
#         name: LengthOfStayInDays
#         type: INT
#         nullable: true
#
#       - source: haf.DaysSincePriorAdmission_X
#         name: DaysSincePriorAdmission_X
#         type: INT
#         nullable: true
#
#       - source: haf.ReadmissionCausedByKey_X
#         name: ReadmissionCausedByKey_X
#         type: BIGINT
#         nullable: true
#
#       - source: haf.IsPlannedReadmission_X
#         name: IsPlannedReadmission_X
#         type: BIT
#         nullable: true
#
#       - source: haf.IsUnplannedReadmission_X
#         name: IsUnplannedReadmission_X
#         type: BIT
#         nullable: true
#
#       - source: haf.EncounterType
#         name: EncounterType
#         type: NVARCHAR(300)
#         nullable: true
#
#       - source: haf.FinancialClass
#         name: FinancialClass
#         type: VARCHAR(100)
#         nullable: true
#
#       # optional: ICD for the qualifying diagnosis
#       - source: dt.Value
#         name: ICDCode
#         type: VARCHAR(400)
#         nullable: true
#
#       - source: dt.NameAndCode
#         name: ICDName
#         type: VARCHAR(400)
#         nullable: true
#
#     filter:
#       from:
#         - HospitalAdmissionFact AS haf
#       join:
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.PatientDurableKey = haf.PatientDurableKey"
#         # Bring in diagnosis events on the same encounter
#         - "INNER JOIN DiagnosisEventFact AS dxf ON dxf.EncounterKey = haf.EncounterKey"
#         - "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey"
#         - "INNER JOIN ##JVM_{{HospitalICDTable}} AS ih ON ih.DiagnosisCode = dt.Value"
#       where:
#         - "haf._IsDeleted = 0"
#         - "haf.AdmissionDateKey IS NOT NULL"
#         - "haf.AdmissionDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "dxf._IsDeleted = 0"
#         - "dt._IsDeleted = 0"
#         - "dt.Type IN ('ICD-10-AM', 'ICD-10-CA', 'ICD-10-CM', 'ICD-9-CM')"
#
# === END FILE: YAMLs/recipes.yaml ===
# === BEGIN FILE: YAMLs/template.yaml SHA256: 4100e43555a828a435c6f6bbab33dc5231d40d956c70b8a5e0ae1cafa37966b8 SIZE: 8155 ===
# # Cosmos variables
# cosmos_vars:
#   project_db: PROJECTD33A929  #Must Start with 'PROJECTD...'
#   cosmos_db: Dual          # COSMOS or COSMOS_SneakPeek or Dual. 
#                            # Makes _sp multiplications if using SP, and will finish all _sp before any non_sp
#
# # Initial variables. Dates in format YYYYMMDD
# run_vars: 
#   min_date_key: 19900101
#   max_date_key: 20260601
#
# # Test Vars
# test_options:
#   smallset: false              # If true, will just make a table up to 'stop_at' number of rows for the PKTable
#   stop_at_for_pk_table: 10     # limits to SELECT TOP(value)
#   stop_at_for_non_pk_tables: 0 # 0 or blank means 'don't stop_at'
#   random_pk_sample: false      # For controls in multiplier will always be random
#   printout_md: true            # Can print out a Markdown of the run and its results (rows, time, etc). in the r_dump root
#
# # Data Science Variables.
# # Generates R scripts to run in R Studio to make Parquet files of what you just pulled
# project_vars:
#   project_folder: "Test Run"     # Folder within QueryGenerator. 
#
#
#
#
# multipliers: # for each multiplier, makes a copy of the cohort tables and prefixes title (eg CrohnsPatients). Cartesian - each multiplier stacks, so here it's BlackCrohns, WhiteUC, etc.
#   - name: IBDType
#     stage: during_build
#     levels:
#       - strat: UC
#         vars:
#           ICD_Value:
#             - K51.%
#       - strat: Crohns
#         vars:
#           ICD_Value:
#             - K50.%
#   - name: Race
#     stage: split_after_build
#     applies_to: PKTable
#     levels:
#       - strat: black
#         column: FirstRace
#         values:
#           - "Black %"
#       - strat: white
#         role: control
#         column: FirstRace
#         values:
#           - "White %"
#         row_mult: 4
# batching:  # splits by value in PK Tables, or by number. Suffixes title with splits (eg Patients_LA if louisiana pts)
#   - name: state
#     column: StateOrProvinceAbbreviation
#     values:
#       - LA
#       - MS
#       - GA
#       - NC
#     separate_parquets: true
#   - name: sex
#     column: Sex
#     values:
#       - Female
#       - Male
#     separate_parquets: true
#   - chunk: 2000 # if a number, splits into batches (not sure how to do yet)
#
# upload_cohorts:
#   - name: PKTable              # PROJECTS.<project_db>.dbo.(name))
#     dest_table: PKTable        # Cosmos global temp: ##JVM_PKTable
#     file_type: dbtable         # parquet, csv or dbtable (pulls from PROJECTS'.dbo.project_db)
#     push_this_cycle: true      # optional; defaults to true
#
#   - name: HospitalICDCodes
#     dest_table: HospitalICDCodes
#     file_type: csv
#     file_loc: "yamls/IBD/ED and Surgery/ICD-hosp.csv"
#     scope: global #global or per_group
#     push_this_cycle: true
#
# cohorts: #Use recipes premade or make your own tables. Multiplied and batched by the functions above
#   - recipe: PatientWithDx #looks at my recipes yaml and pulls those in with those names. Should fully import with variables placed 
#     name: Patients
#   - recipe: OtherHospitalizations
#
#   # Make as many cohorts as you need, each is its own table. 
#   # You will always need the type and nullable, from the Data Dictionary. Source tells you the table and the column you're deriving from
#
#   #Example Cohorts - will be ignored, just for the sake of being an example on the template
# example_cohorts: 
#   - name: Pts
#     type: example # PK is generally the index all the others check against. non-PK tables are 'fact'. 'example' means ignore. 
#     pull_this_cycle: false # if true, will become part of the cohort cycle. Sometimes you just want to update some and not others. 
#     dest_table: Patients #Optional - if not specified, it just uses the same name as name:
#     columns: #Try to have everything you'll need for any future cohort to find info needed. For instance,
#       # If later you need ED visit and vitals info, you likely need ArrivalDateKey, EncounterKey.
#       # If it's all in one table it's much easier to pull later data, even if it's not a "primary key"
#       - source: pk.x
#         name: # Name in the output table. If you leave alone, skill.md will fill with same name as (column)
#         type: # Filled by skill.mg
#         nullable: # Filled by skill.mg
#       - source: pk.x # repeat as necessary
#       - source: pk.x
#       - source: dt.x # data from a joined table, which you declare in the 'join:' section. 
#     # PK cohort: EdVisitFact + DiagnosisEventFact, no DurationDim, with pediatric age filter via AgeKey and ED primary diagnosis restriction.
#     filter:
#       from: # the general collection on Cosmos ('All ED visits' is EDVisitFacts, etc. Look at Data Dictionary for what it contains).
#          DiagnosisEventFact as pk
#       join: # You join another table for filtering or for getting other information -
#         - "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey" # for filtering by dt.value, dxf.StartDateKey
#         - "INNER JOIN PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey" # for getting more information about patient and adding it to table
#         - "INNER JOIN DurationDim AS dur ON dur.DurationKey = dxf.AgeKey" # Sometimes you just need one conversion. AgeKey doesn't give an age, it's a key that needs a translation to Years by DurationDim.
#         # likely INNER JOIN COSMOS.{cosmos_db}.dbo.(someFact) as sf.(CommonKey) = pk.(CommonKey)
#         # IF EMPTY MUST USE []
#       where:
#         # Your filtering logic. Date ranges, "Had RSV," "Diagnosed with IBD", etc.
#         # Will likely use your join to do some logic filtering here, as well as variables like date ranges
#         - "dxf.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "dxf.StartDateKey >= 0"
#         - "dxf._IsDeleted = 0"
#         - "dt._IsDeleted = 0"
#         - "p.IsValid = 1"
#         - "p.IsCurrent = 1"
#         - "p.UseInCosmosAnalytics_X = 1"
#         - "dt.Type IN ('ICD-10-AM','ICD-10-CA','ICD-10-CM', 'ICD-9 CM')"
#         - "{{sql_condition('dt.Value', ICD_Value)}}"
#
#   # You can also make custom ones - example below of things you'd need. 
#   - name: BirthFact #Flavor of what you want to pull - for instance, if it's a baby and you also want the mother's record
#     pull_this_cycle: true
#     type: example
#     dest_table: #PROJECTS-local table name. Cosmos will Pre-pend "JVM_" to Cosmos tables to make them unique.
#     vars: 
#       PKTable: "##JVM_Patients" # If you want to override teh standard cohort PKTable, this table exists
#     columns: # Each 'source' below is a column in your table, you'll define the anchor in 'filter' below.
#       # Example: "Patients" could be 'p', so 'p.AgeKey' would be the agekey var for that patient
#       - source: bpf.x
#         name:
#         type:
#         nullable:
#     filter:
#       from: PatientDim as p # the general collection on Cosmos.
#       join: #how you are using PKTable to get the data - where you use the joins to find the linked Dim/Fact
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.EncounterKey = p.DurableKey" # Joining another table at the key from PKTable and pulling data into a
#       where: #Logic for filtering things out ("AgeKey >18," etc.)
#   - name: MotherOfPatients #Flavor of what you want to pull - for instance, if it's a baby and you also want the mother's record
#     pull_this_cycle: true
#     type: example
#     dest_table: #PROJECTS-local table name. Cosmos will Pre-pend "JVM_" to Cosmos tables to make them unique.
#     vars: 
#       PKTable: "JVM_Patients" # If you want to have this one override the standard PKTable from the cohort, assuming a Mothers exists
#     columns: # Each 'source' below is a column in your table, you'll define the anchor in 'filter' below.
#       # Example: "Patients" could be 'p', so 'p.AgeKey' would be the agekey var for that patient
#       - source: bpf.x
#         name:
#         type:
#         nullable:
#     filter:
#       from: PatientDim as p # the general collection on Cosmos.
#       join: #how you are using PKTable to get the data - where you use the joins to find the linked Dim/Fact
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.EncounterKey = p.DurableKey" # Joining another table at the key from PKTable and pulling data into a
#       where: #Logic for filtering things out ("AgeKey >18," etc.)
#
# === END FILE: YAMLs/template.yaml ===
# === BEGIN FILE: pullmanager.py SHA256: ebdc02f9ba0685fc16b3aaea58c6563e0b91f3e69bcbce40bf953e414f787add SIZE: 408 ===
# #!/usr/bin/env python3
# """Launcher for the extracted Pullmanager runtime.
#
# Sits beside the `pullmanager/` package so the VM can run:
#
#     python pullmanager_runtime/pullmanager.py split/pullmanifest.yaml
# """
#
# import sys
# from pathlib import Path
#
# sys.path.insert(0, str(Path(__file__).resolve().parent))
#
# from pullmanager.cli import main  # noqa: E402
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager.py ===
# === BEGIN FILE: pullmanager/__init__.py SHA256: 2b6c4b6cedb40b106d374887a4180e7a03259c5fa7d8a66a6932b65ce73123c0 SIZE: 126 ===
# """Pullmanager: manifest-driven executor for YAML Manager split pull folders."""
#
# __version__ = "0.2.0"
#
# MANIFEST_VERSION = 1
#
# === END FILE: pullmanager/__init__.py ===
# === BEGIN FILE: pullmanager/__main__.py SHA256: 307299fda7b77d22c64cb51430ec75e5f59d78e9c948786afb3b2544fe45e4b7 SIZE: 79 ===
# from .cli import main
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager/__main__.py ===
# === BEGIN FILE: pullmanager/batches.py SHA256: dc90271523a5145924ab101adf6119f716d790d33395b68a3baef905a8f04e17 SIZE: 5412 ===
# """Turning a logical batch into the rows it selects.
#
# Batch membership is decided against the durable Projects copy of the PK table,
# never against Cosmos. That copy does not change under a refresh, so a batch
# means the same rows on a resume as it did on the night it first ran -- and a
# fresh run and a resume take the same path, so recovery is exercised nightly.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from typing import Any
#
# from .naming import destination
#
#
# class BatchError(ValueError):
#     """Raised when a batch cannot be turned into a selection."""
#
#
# @dataclass
# class BatchSelection:
#     """A parameterized SELECT over the local PK table."""
#
#     sql: str
#     params: list[Any] = field(default_factory=list)
#     description: str = ""
#
#
# def dimension_predicate(dimension: dict[str, Any]) -> tuple[str, list[Any]]:
#     column = dimension.get("column")
#     if not column:
#         raise BatchError(f"Batch dimension {dimension.get('name')!r} names no column.")
#     if dimension.get("is_other"):
#         excludes = dimension.get("excludes") or []
#         if not excludes:
#             raise BatchError(
#                 f"Batch dimension {dimension.get('name')!r} is the catch-all but lists "
#                 "nothing to exclude, so it would select every row."
#             )
#         placeholders = ", ".join("?" for _ in excludes)
#         # NULL is not 'not in' anything in SQL, so include it explicitly or the
#         # catch-all silently drops rows with no value.
#         return f"([{column}] NOT IN ({placeholders}) OR [{column}] IS NULL)", list(excludes)
#     if "value" not in dimension:
#         raise BatchError(f"Batch dimension {dimension.get('name')!r} has no value.")
#     return f"[{column}] = ?", [dimension["value"]]
#
#
# def chunk_clause(batch: dict[str, Any], key_columns: list[str]) -> tuple[str, str]:
#     """OFFSET/FETCH for a row chunk, plus the ordering that makes it stable.
#
#     A chunk is only reproducible if the ordering is a total order, which is why
#     the PK's uniqueness is verified before any batch runs.
#     """
#     runtime = batch.get("runtime") or []
#     chunks = [d for d in runtime if str(d.get("kind", "")).lower() == "row_chunk"]
#     if not chunks:
#         return "", ""
#     if len(chunks) > 1:
#         raise BatchError("More than one row_chunk dimension in a single batch.")
#     unresolved = [
#         d for d in runtime
#         if str(d.get("kind", "")).lower() == "column_values"
#     ]
#     if unresolved:
#         names = ", ".join(str(d.get("name")) for d in unresolved)
#         raise BatchError(
#             f"Batch dimension(s) {names} use `values: all`, which has to be resolved "
#             "against real data before the batch set is known. Not yet supported; "
#             "list the values explicitly in the template."
#         )
#     if not key_columns:
#         raise BatchError("Row chunking needs the PK key columns to order by.")
#     order = ", ".join(f"[{c}]" for c in key_columns)
#     size = chunks[0].get("rows_per_batch")
#     try:
#         size = int(size)
#     except (TypeError, ValueError):
#         raise BatchError(f"row_chunk has a non-numeric rows_per_batch: {size!r}") from None
#     if size <= 0:
#         raise BatchError(f"row_chunk rows_per_batch must be positive, got {size}.")
#     return order, str(size)
#
#
# def select_batch_rows(
#     project_db: str,
#     pk_table: str,
#     batch: dict[str, Any] | None,
#     key_columns: list[str],
#     *,
#     chunk_index: int = 0,
# ) -> BatchSelection:
#     """Every PK row belonging to one batch.
#
#     Whole rows, not just keys: batching selects on PK attributes such as Sex
#     and StateOrProvinceAbbreviation, and cohort joins may use them too.
#     """
#     table = destination(project_db, pk_table)
#     if not batch:
#         return BatchSelection(sql=f"SELECT * FROM {table};", description="whole PK table")
#
#     predicates: list[str] = []
#     params: list[Any] = []
#     described: list[str] = []
#     for dimension in batch.get("dimensions") or []:
#         clause, values = dimension_predicate(dimension)
#         predicates.append(clause)
#         params.extend(values)
#         described.append(
#             f"{dimension.get('column')}="
#             + ("other" if dimension.get("is_other") else str(dimension.get("value")))
#         )
#
#     sql = f"SELECT * FROM {table}"
#     if predicates:
#         sql += "\nWHERE " + "\n  AND ".join(predicates)
#
#     order, size = chunk_clause(batch, key_columns)
#     if order:
#         offset = chunk_index * int(size)
#         sql += f"\nORDER BY {order}\nOFFSET {offset} ROWS FETCH NEXT {size} ROWS ONLY"
#         described.append(f"rows {offset}-{offset + int(size)}")
#
#     return BatchSelection(
#         sql=sql + ";",
#         params=params,
#         description=", ".join(described) or "whole PK table",
#     )
#
#
# def count_batch_rows(project_db: str, pk_table: str, batch: dict[str, Any] | None) -> BatchSelection:
#     """How many PK rows a batch's predicate matches, before chunking."""
#     table = destination(project_db, pk_table)
#     predicates: list[str] = []
#     params: list[Any] = []
#     for dimension in (batch or {}).get("dimensions") or []:
#         clause, values = dimension_predicate(dimension)
#         predicates.append(clause)
#         params.extend(values)
#     sql = f"SELECT COUNT_BIG(1) FROM {table}"
#     if predicates:
#         sql += " WHERE " + " AND ".join(predicates)
#     return BatchSelection(sql=sql + ";", params=params)
#
# === END FILE: pullmanager/batches.py ===
# === BEGIN FILE: pullmanager/cli.py SHA256: bc2b9888907a0057e8c0cbda85ca2586e6e5240b684d03c13d1559906c1c8258 SIZE: 8869 ===
# """Command line entry point.
#
# Phase 5 scope: inspect a manifest and render the SQL it implies. Execution
# arrives in Phase 6.
# """
#
# from __future__ import annotations
#
# import argparse
# import sys
# from pathlib import Path
#
# from . import __version__
# from .executor import (
#     RESUME_FULL,
#     RESUME_PARTIAL,
#     PlanError,
#     excluded_units,
#     plan,
#     write_sql,
# )
# from .manifest import Manifest, ManifestError
# from .naming import NamingError
# from .normalize import NormalizationError
#
#
# def summarize(manifest: Manifest) -> None:
#     project = manifest.project
#     print(f"Project:  {project.get('name')}  ({project.get('project_db')})")
#     print(f"Manifest: {manifest.path}")
#     print(f"Sessions: {len(manifest.sessions)}")
#     print()
#     for session in manifest.sessions:
#         pk_source = next((p.pk_source for p in session.phases if p.pk_source), None)
#         kind = pk_source.get("kind") if pk_source else "none"
#         epoch = session.epoch or "-"
#         print(f"  {session.session_id}  [{session.status}]  pk={session.pk_table} ({kind})")
#         print(f"    epoch {epoch}")
#         for phase in session.phases:
#             stale = "  STALE" if phase.is_stale(session.epoch) else ""
#             print(f"    phase {phase.name:<15} {phase.status:<8} {phase.yaml}{stale}")
#         for run in session.runs:
#             batch = run.batch.get("name") if run.batch else "-"
#             stale = "  STALE" if run.is_stale(session.epoch) else ""
#             print(f"    run   {batch:<15} {run.status:<8} {run.yaml}{stale}")
#         print()
#
#
# def dry_run(manifest: Manifest, args: argparse.Namespace) -> int:
#     mode = RESUME_PARTIAL if args.resume_partial else RESUME_FULL
#     units = plan(
#         manifest,
#         linked_server=args.linked_server,
#         retry_failed=args.retry_failed,
#         include_settled=args.all,
#         mode=mode,
#     )
#     left_out = excluded_units(manifest, mode=mode, retry_failed=args.retry_failed)
#     failures = [row for row in left_out if row[1] == "failed"]
#
#     if not units:
#         print("Nothing to do.")
#         print("Every phase and run is either complete for this session or deliberately")
#         print("skipped. Use --retry-failed to reopen failures, or --all to render")
#         print("everything regardless of status.")
#         _report_exclusions(left_out, failures)
#         return 0
#
#     total_blocks = 0
#     for unit in units:
#         server, local = len(unit.server_blocks), len(unit.local_blocks)
#         total_blocks += server + local
#         print(f"{unit.unit_id}  [{unit.node.status}]  server={server} local={local}")
#         print(f"    why:  {unit.reason}")
#         for note in unit.notes:
#             print(f"    note: {note}")
#         if args.verbose:
#             for block in unit.blocks:
#                 print(f"    {block.side:<6} {block.block_id}")
#
#     print(f"\n{len(units)} unit(s), {total_blocks} SQL block(s).")
#     print(f"Resume mode: {mode}")
#     print(f"Linked server placeholder: {args.linked_server}")
#     print("Nothing was executed and the manifest was not modified.")
#     _report_exclusions(left_out, failures)
#
#     if args.out_dir:
#         written = write_sql(units, Path(args.out_dir))
#         print(f"\nWrote {len(written)} file(s) to {Path(args.out_dir).resolve()}")
#     return 0
#
#
# def _report_exclusions(left_out, failures) -> None:
#     if not left_out:
#         return
#     print(f"\nExcluded {len(left_out)} unit(s):")
#     for label, status, reason in left_out:
#         print(f"  {label:<40} [{status}]  {reason}")
#     if failures:
#         print(
#             f"\n{len(failures)} unit(s) failed previously and are NOT included. Rebuilding "
#             "the\nserver side without them would finish with nothing transferred. Fix the "
#             "cause,\nthen add --retry-failed."
#         )
#
#
# def execute(manifest: Manifest, args: argparse.Namespace) -> int:
#     from .db import DatabaseError, Settings, find_env_file, load_env_file
#     from .session import SessionRunner
#
#     try:
#         loaded = load_env_file(args.env)
#     except DatabaseError as exc:
#         print(f"ERROR {exc}", file=sys.stderr)
#         return 1
#     if loaded:
#         print(f"Loaded {len(loaded)} setting(s) from {find_env_file(args.env)}")
#     elif not args.env:
#         print("No .env found; using environment variables and defaults.")
#     settings = Settings.from_env()
#     mode = RESUME_PARTIAL if args.resume_partial else RESUME_FULL
#     reports = []
#     for session in manifest.sessions:
#         print(f"=== {session.session_id} ===")
#         runner = SessionRunner(
#             manifest, session, settings, mode=mode, retry_failed=args.retry_failed
#         )
#         try:
#             with runner:
#                 report = runner.execute()
#         except DatabaseError as exc:
#             print(f"  could not open the session: {exc}", file=sys.stderr)
#             # Sessions have independent connections and PKs, so the next one
#             # still gets its chance.
#             reports.append(None)
#             continue
#         reports.append(report)
#         print(f"  epoch {report.epoch} on {report.linked_server}")
#         for label in report.completed:
#             print(f"  done     {label}")
#         for label in report.skipped:
#             print(f"  skipped  {label}")
#         for label, message in report.failed:
#             print(f"  FAILED   {label}: {message}")
#         for warning in report.warnings:
#             print(f"  warning  {warning}")
#         print()
#
#     failures = [r for r in reports if r is None or not r.ok]
#     print(f"{len(reports) - len(failures)}/{len(reports)} session(s) completed.")
#     if any(r is not None for r in reports):
#         print(f"Manifest updated: {manifest.path}")
#     else:
#         print("No session opened, so the manifest was not modified.")
#     return 1 if failures else 0
#
#
# def build_parser() -> argparse.ArgumentParser:
#     parser = argparse.ArgumentParser(
#         prog="pullmanager",
#         description="Manifest-driven executor for YAML Manager split pull folders.",
#     )
#     parser.add_argument("manifest", nargs="?", help="Path to pullmanifest.yaml")
#     parser.add_argument("--version", action="version", version=f"pullmanager {__version__}")
#     parser.add_argument(
#         "--dry-run",
#         action="store_true",
#         help="Render the SQL each phase implies without touching a database.",
#     )
#     parser.add_argument(
#         "--execute",
#         action="store_true",
#         help="Run the manifest against live connections, updating it as it goes.",
#     )
#     parser.add_argument("--out-dir", default=None, help="Write rendered SQL here (dry run).")
#     parser.add_argument(
#         "--linked-server",
#         default=None,
#         help="Cosmos instance to render OPENQUERY against. Captured per connection at "
#              "run time; supply one only for a dry run.",
#     )
#     parser.add_argument(
#         "--env",
#         default=None,
#         help="Path to a .env holding host and database names. Searched in the working "
#              "directory and beside the runtime when not given.",
#     )
#     parser.add_argument("--retry-failed", action="store_true", help="Reopen failed work.")
#     parser.add_argument(
#         "--resume-partial",
#         action="store_true",
#         help="Keep completed local transfers and replay only the server side. Server "
#              "state is gone either way; this trades a guard for not re-pulling.",
#     )
#     parser.add_argument("--all", action="store_true", help="Include already-settled work.")
#     parser.add_argument("-v", "--verbose", action="store_true", help="List every SQL block.")
#     parser.add_argument(
#         "--tdd",
#         nargs="?",
#         const="__all__",
#         metavar="MODULE",
#         help="Run the embedded test suite, optionally limited to one module.",
#     )
#     return parser
#
#
# def main(argv: list[str] | None = None) -> int:
#     parser = build_parser()
#     args = parser.parse_args(argv)
#
#     if args.tdd is not None:
#         from .tests import run as run_tests
#
#         return run_tests(None if args.tdd == "__all__" else args.tdd)
#
#     if not args.manifest:
#         parser.print_help()
#         return 1
#
#     if args.linked_server is None:
#         from .executor import DRY_RUN_LINKED_SERVER
#
#         args.linked_server = DRY_RUN_LINKED_SERVER
#
#     try:
#         manifest = Manifest.load(args.manifest)
#         if args.dry_run and args.execute:
#             print("--dry-run and --execute are mutually exclusive.", file=sys.stderr)
#             return 1
#         if args.dry_run:
#             return dry_run(manifest, args)
#         if args.execute:
#             return execute(manifest, args)
#         summarize(manifest)
#     except (ManifestError, PlanError, NamingError, NormalizationError) as exc:
#         print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
#         return 1
#     return 0
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: pullmanager/cli.py ===
# === BEGIN FILE: pullmanager/db.py SHA256: 95bf2e090578a6160ac5259be25e6a7d3604df2a33eb9cbfc570774ecb547085 SIZE: 11608 ===
# """Database adapter.
#
# pyodbc is imported lazily so the rest of the package -- planning, rendering,
# the dry run -- works on a machine without a driver.
#
# The behaviours here were harvested from the old generator rather than invented;
# see "Connection And Execution Facts" in pullmanager_contracts.md.
# """
#
# from __future__ import annotations
#
# import os
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Iterable, Iterator, Sequence
#
# DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"
# # Both hosts are DNS aliases, not machine names. The real Cosmos instance is
# # discovered per connection with @@SERVERNAME, because it changes every time.
# DEFAULT_COSMOS_SERVER = "COSMOS"
# DEFAULT_PROJECTS_SERVER = "PROJECTS"
# DEFAULT_UPLOAD_CHUNK = 20_000
#
# # `GO` is a client batch separator, not T-SQL. The driver rejects it.
# BATCH_SEPARATOR = "GO"
#
#
# class DatabaseError(RuntimeError):
#     """A SQL failure, carrying the server messages that explain it."""
#
#     def __init__(self, message: str, server_messages: Sequence[str] = ()):
#         super().__init__(message)
#         self.server_messages = list(server_messages)
#
#     def __str__(self) -> str:
#         base = super().__str__()
#         if not self.server_messages:
#             return base
#         detail = "\n".join(f"  [SQL MESSAGE] {m}" for m in self.server_messages)
#         return f"{base}\n{detail}"
#
#
# ENV_FILENAME = ".env"
#
#
# def parse_env_file(text: str) -> dict[str, str]:
#     """Parse KEY=VALUE lines. Deliberately small: the bundle stays stdlib-only."""
#     values: dict[str, str] = {}
#     for raw in text.splitlines():
#         line = raw.strip()
#         if not line or line.startswith("#"):
#             continue
#         if line.lower().startswith("export "):
#             line = line[len("export "):].lstrip()
#         if "=" not in line:
#             continue
#         key, _, value = line.partition("=")
#         key = key.strip()
#         if not key:
#             continue
#         value = value.strip()
#         if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
#             value = value[1:-1]
#         values[key] = value
#     return values
#
#
# def find_env_file(explicit: str | Path | None = None, *extra: Path) -> Path | None:
#     """Locate a .env: an explicit path, then the usual places."""
#     if explicit:
#         path = Path(explicit)
#         if not path.is_file():
#             raise DatabaseError(f"No .env file at {path}")
#         return path
#     candidates = [
#         Path.cwd() / ENV_FILENAME,
#         Path(__file__).resolve().parent.parent / ENV_FILENAME,
#         *[Path(p) / ENV_FILENAME for p in extra],
#     ]
#     for candidate in candidates:
#         if candidate.is_file():
#             return candidate
#     return None
#
#
# def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
#     """Read a .env into the process environment.
#
#     A real environment variable wins over the file unless `override`, which is
#     what lets a one-off run be redirected without editing the file.
#     """
#     found = find_env_file(path)
#     if found is None:
#         return {}
#     values = parse_env_file(found.read_text(encoding="utf-8"))
#     for key, value in values.items():
#         if override or key not in os.environ:
#             os.environ[key] = value
#     return values
#
#
# @dataclass
# class Settings:
#     """Connection settings. Windows auth, so never credentials."""
#
#     cosmos_server: str = DEFAULT_COSMOS_SERVER
#     cosmos_database: str = "COSMOS"
#     projects_server: str = DEFAULT_PROJECTS_SERVER
#     projects_database: str = ""
#     driver: str = DEFAULT_DRIVER
#     login_timeout: int = 10
#     query_timeout: int = 0
#     upload_chunk: int = DEFAULT_UPLOAD_CHUNK
#
#     @classmethod
#     def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
#         source = os.environ if env is None else env
#
#         def get(name: str, fallback: str) -> str:
#             return str(source.get(f"PULLMANAGER_{name}", fallback) or fallback)
#
#         def get_int(name: str, fallback: int) -> int:
#             try:
#                 return int(source.get(f"PULLMANAGER_{name}", fallback))
#             except (TypeError, ValueError):
#                 return fallback
#
#         return cls(
#             cosmos_server=get("COSMOS_SERVER", DEFAULT_COSMOS_SERVER),
#             cosmos_database=get("COSMOS_DATABASE", "COSMOS"),
#             projects_server=get("PROJECTS_SERVER", DEFAULT_PROJECTS_SERVER),
#             projects_database=str(source.get("PULLMANAGER_PROJECTS_DATABASE", "") or ""),
#             driver=get("ODBC_DRIVER", DEFAULT_DRIVER),
#             login_timeout=get_int("LOGIN_TIMEOUT", 10),
#             query_timeout=get_int("QUERY_TIMEOUT", 0),
#             upload_chunk=get_int("UPLOAD_CHUNK", DEFAULT_UPLOAD_CHUNK),
#         )
#
#     def connection_string(self, server: str, database: str) -> str:
#         if not server:
#             raise DatabaseError("No server configured. Set it in .env.")
#         if not database:
#             raise DatabaseError("No database configured. Set it in .env.")
#         return (
#             f"Driver={{{self.driver}}};"
#             f"Server=tcp:{server};"
#             f"Database={database};"
#             "Trusted_Connection=yes;"
#         )
#
#     def cosmos_connection_string(self, database: str | None = None) -> str:
#         return self.connection_string(self.cosmos_server, database or self.cosmos_database)
#
#     def projects_connection_string(self, database: str | None = None) -> str:
#         return self.connection_string(
#             self.projects_server, database or self.projects_database
#         )
#
#
# @dataclass
# class ResultSet:
#     columns: list[str]
#     rows: list[tuple]
#
#     def as_dicts(self) -> list[dict[str, Any]]:
#         return [dict(zip(self.columns, row)) for row in self.rows]
#
#
# @dataclass
# class ExecutionResult:
#     result_sets: list[ResultSet] = field(default_factory=list)
#     messages: list[str] = field(default_factory=list)
#
#     def rows_of(self, *columns: str) -> list[dict[str, Any]]:
#         """Result sets carrying a declared telemetry shape."""
#         wanted = set(columns)
#         out: list[dict[str, Any]] = []
#         for result in self.result_sets:
#             if wanted.issubset(set(result.columns)):
#                 out.extend(result.as_dicts())
#         return out
#
#
# def split_batches(script: str) -> list[str]:
#     """Split on lines consisting solely of GO, which the driver cannot execute."""
#     batches: list[str] = []
#     current: list[str] = []
#     for line in script.splitlines():
#         if line.strip().upper() == BATCH_SEPARATOR:
#             if current:
#                 batches.append("\n".join(current))
#                 current = []
#             continue
#         current.append(line)
#     if current:
#         batches.append("\n".join(current))
#     return [b for b in batches if b.strip()]
#
#
# def collect_messages(cursor: Any) -> list[str]:
#     """Server PRINT output and nested engine errors.
#
#     This is what surfaces the inner error of a failed OPENQUERY; without it the
#     caller sees only a generic outer failure.
#     """
#     try:
#         raw = list(cursor.messages or [])
#     except Exception:
#         return []
#     return [" | ".join(str(part) for part in message) for message in raw]
#
#
# def drain(cursor: Any) -> list[ResultSet]:
#     """Consume every result set a batch produced.
#
#     A generated script interleaves DDL, inserts and telemetry SELECTs, so a
#     batch yields a mixture of row-producing and silent statements.
#     """
#     results: list[ResultSet] = []
#     while True:
#         if cursor.description is not None:
#             columns = [column[0] for column in cursor.description]
#             try:
#                 rows = list(cursor.fetchall())
#             except Exception:
#                 rows = []
#             results.append(ResultSet(columns=columns, rows=rows))
#         else:
#             try:
#                 cursor.fetchall()
#             except Exception:
#                 pass  # statement produced no rows
#         try:
#             if not cursor.nextset():
#                 break
#         except Exception:
#             break
#     return results
#
#
# def execute_script(connection: Any, script: str, *, label: str = "script") -> ExecutionResult:
#     """Run every batch of a script, failing fast with server messages."""
#     outcome = ExecutionResult()
#     batches = split_batches(script)
#     cursor = connection.cursor()
#     for index, batch in enumerate(batches, start=1):
#         try:
#             cursor.execute(batch)
#             outcome.messages.extend(collect_messages(cursor))
#             outcome.result_sets.extend(drain(cursor))
#         except Exception as exc:
#             messages = collect_messages(cursor)
#             raise DatabaseError(
#                 f"{label}: batch {index}/{len(batches)} failed: {exc}", messages
#             ) from exc
#     return outcome
#
#
# def capture_server_name(connection: Any) -> str:
#     """The Cosmos instance name, which changes on every connection."""
#     cursor = connection.cursor()
#     cursor.execute("SELECT @@SERVERNAME;")
#     row = cursor.fetchone()
#     if not row or not row[0]:
#         raise DatabaseError(
#             "Could not determine the Cosmos instance via @@SERVERNAME. Local SQL "
#             "cannot build OPENQUERY without it."
#         )
#     return str(row[0])
#
#
# def chunked(rows: Iterable[Sequence[Any]], size: int) -> Iterator[list[Sequence[Any]]]:
#     batch: list[Sequence[Any]] = []
#     for row in rows:
#         batch.append(row)
#         if len(batch) >= size:
#             yield batch
#             batch = []
#     if batch:
#         yield batch
#
#
# def bulk_insert(
#     connection: Any,
#     table: str,
#     columns: Sequence[str],
#     rows: Iterable[Sequence[Any]],
#     *,
#     chunk_size: int = DEFAULT_UPLOAD_CHUNK,
# ) -> int:
#     """Insert rows with parameter arrays rather than a literal VALUES list.
#
#     A table value constructor is capped at 1000 rows; parameter arrays are not,
#     because the statement stays single-row and only its bindings repeat. Values
#     are bound rather than interpolated, so quotes and NULLs need no escaping.
#
#     Chunked because fast_executemany allocates buffers from declared column
#     width times batch size, so memory grows with both.
#     """
#     if not columns:
#         raise DatabaseError(f"Cannot insert into {table}: no columns given.")
#     placeholders = ", ".join("?" for _ in columns)
#     column_list = ", ".join(f"[{c}]" for c in columns)
#     statement = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
#
#     cursor = connection.cursor()
#     try:
#         cursor.fast_executemany = True
#     except Exception:
#         pass  # older drivers fall back to per-row inserts
#
#     inserted = 0
#     for batch in chunked(rows, max(1, chunk_size)):
#         try:
#             cursor.executemany(statement, batch)
#         except Exception as exc:
#             raise DatabaseError(
#                 f"Bulk insert into {table} failed after {inserted} row(s): {exc}",
#                 collect_messages(cursor),
#             ) from exc
#         inserted += len(batch)
#     return inserted
#
#
# def connect(connection_string: str, *, login_timeout: int = 10, query_timeout: int = 0) -> Any:
#     """Open a connection. pyodbc is imported here so the package loads without it."""
#     try:
#         import pyodbc
#     except ImportError as exc:
#         raise DatabaseError(
#             "pyodbc is not installed. It is needed only to execute; planning, "
#             "rendering and --dry-run work without it."
#         ) from exc
#
#     connection = pyodbc.connect(connection_string, timeout=login_timeout)
#     if query_timeout:
#         connection.timeout = query_timeout
#     return connection
#
# === END FILE: pullmanager/db.py ===
# === BEGIN FILE: pullmanager/executor.py SHA256: fd658d58f45a49875790f6a119cc8b160cd736fbce5f861331f5f2ae94dd79ea SIZE: 8444 ===
# """Traversal and planning.
#
# Walks a manifest in order and produces the work a session implies. Nothing
# here touches a database; Phase 6 supplies an adapter that executes what this
# plans, so the ordering is testable on its own.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Iterator
#
# from . import local_sql, server_sql
# from .manifest import Manifest, Node, Phase, Run, Session
# from .models import BLOCKED, DONE, FAILED, RUNNING, SKIPPED
# from .normalize import normalize_bool
# from .sql import SqlBlock
# from .yaml_io import load_yaml
#
# DRY_RUN_LINKED_SERVER = "DRY-RUN-INSTANCE"
#
#
# class PlanError(ValueError):
#     """Raised when a manifest cannot be turned into work."""
#
#
# @dataclass
# class Unit:
#     """One phase or run, with the SQL it implies."""
#
#     session_id: str
#     node: Node
#     kind: str  # "setup" | "upload_cohorts" | "pk" | "run"
#     yaml_path: Path
#     server_blocks: list[SqlBlock] = field(default_factory=list)
#     local_blocks: list[SqlBlock] = field(default_factory=list)
#     notes: list[str] = field(default_factory=list)
#     reason: str = "pending"
#
#     @property
#     def unit_id(self) -> str:
#         return f"{self.session_id}/{self.kind}" if isinstance(self.node, Phase) else self.node.label
#
#     @property
#     def blocks(self) -> list[SqlBlock]:
#         return [*self.server_blocks, *self.local_blocks]
#
#
# def iter_units(manifest: Manifest, session: Session) -> Iterator[tuple[str, Node, Path]]:
#     """Phases in routine order, then runs in manifest order."""
#     for phase in session.phases:
#         yield phase.name, phase, manifest.resolve(phase)
#     for run in session.runs:
#         yield "run", run, manifest.resolve(run)
#
#
# RESUME_FULL = "full"
# RESUME_PARTIAL = "partial"
#
#
# def should_execute(
#     node: Node,
#     kind: str,
#     *,
#     current_epoch: str | None = None,
#     mode: str = RESUME_FULL,
#     retry_failed: bool = False,
# ) -> tuple[bool, str]:
#     """Decide whether one unit runs, and say why.
#
#     `done` does not mean "its output still exists". Global temps die with the
#     connection, so work completed under a previous epoch has left nothing on
#     the server even though the status still reads done. A run is different: its
#     durable result is rows in a Projects table, which survive, so it is the one
#     kind of unit a partial resume can skip.
#     """
#     status = node.status
#     if status == FAILED:
#         if retry_failed:
#             return True, "retrying a failure"
#         return False, "failed; --retry-failed reopens it"
#     if status == RUNNING:
#         return True, "interrupted while running"
#     if status == BLOCKED:
#         return True, "was blocked; upstream may succeed this time"
#     if status == SKIPPED:
#         return False, "skipped deliberately"
#     if status == DONE:
#         if not node.is_stale(current_epoch):
#             return False, "already done in this session"
#         if kind == "run" and mode == RESUME_PARTIAL:
#             return False, "done; its rows are in a Projects table and survive"
#         return True, "done under a previous connection; server state is gone"
#     return True, "pending"
#
#
# def session_cohorts(manifest: Manifest, session: Session) -> list[dict[str, Any]]:
#     """Every cohort the session will write, gathered for the setup shells."""
#     seen: dict[str, dict[str, Any]] = {}
#     for _, node, path in iter_units(manifest, session):
#         if not path.is_file():
#             continue
#         doc = load_yaml(path) or {}
#         for cohort in doc.get("cohorts") or []:
#             if not isinstance(cohort, dict) or not cohort.get("dest_table"):
#                 continue
#             if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#                 continue
#             seen.setdefault(str(cohort["dest_table"]), cohort)
#     return list(seen.values())
#
#
# def plan_unit(
#     manifest: Manifest,
#     session: Session,
#     kind: str,
#     node: Node,
#     path: Path,
#     linked_server: str,
# ) -> Unit:
#     """Render the SQL one phase or run implies."""
#     if not path.is_file():
#         raise PlanError(f"{node.label}: phase YAML not found at {path}")
#     doc = load_yaml(path) or {}
#     unit = Unit(session_id=session.session_id, node=node, kind=kind, yaml_path=path)
#
#     if kind == "setup":
#         unit.server_blocks = server_sql.render_setup(doc, unit.unit_id)
#         cohorts = session_cohorts(manifest, session)
#         unit.local_blocks = local_sql.render_setup(doc, cohorts, unit.unit_id)
#         unit.notes.append(
#             f"setup creates {len(cohorts)} destination table(s); runs append to them"
#         )
#         return unit
#
#     if kind == "upload_cohorts":
#         uploads = doc.get("upload_cohorts") or []
#         enabled = [
#             u for u in uploads
#             if isinstance(u, dict) and normalize_bool(u.get("push_this_cycle"), default=True)
#         ]
#         unit.notes.append(
#             f"{len(enabled)} upload cohort(s); uploaded through the client, since there is "
#             "no linked server from Cosmos back to Projects"
#             if enabled else "no upload cohorts"
#         )
#         return unit
#
#     server_blocks, notes = server_sql.render_phase(doc, unit.unit_id)
#     unit.server_blocks = server_blocks
#     unit.notes.extend(notes)
#     unit.local_blocks = local_sql.render_phase(doc, unit.unit_id, linked_server)
#     return unit
#
#
# def plan_session(
#     manifest: Manifest,
#     session: Session,
#     *,
#     linked_server: str = DRY_RUN_LINKED_SERVER,
#     retry_failed: bool = False,
#     include_settled: bool = False,
#     mode: str = RESUME_FULL,
#     current_epoch: str | None = None,
# ) -> list[Unit]:
#     """Every unit this session would execute, in order.
#
#     `current_epoch` defaults to None, which models opening a fresh connection:
#     anything completed under a previous epoch is stale.
#     """
#     units: list[Unit] = []
#     for kind, node, path in iter_units(manifest, session):
#         execute, reason = should_execute(
#             node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
#         )
#         if not execute and not include_settled:
#             continue
#         unit = plan_unit(manifest, session, kind, node, path, linked_server)
#         unit.reason = reason if execute else f"included anyway ({reason})"
#         units.append(unit)
#     return units
#
#
# def plan(
#     manifest: Manifest,
#     *,
#     linked_server: str = DRY_RUN_LINKED_SERVER,
#     retry_failed: bool = False,
#     include_settled: bool = False,
#     mode: str = RESUME_FULL,
# ) -> list[Unit]:
#     units: list[Unit] = []
#     for session in manifest.sessions:
#         units.extend(
#             plan_session(
#                 manifest,
#                 session,
#                 linked_server=linked_server,
#                 retry_failed=retry_failed,
#                 include_settled=include_settled,
#                 mode=mode,
#             )
#         )
#     return units
#
#
# def excluded_units(
#     manifest: Manifest,
#     *,
#     mode: str = RESUME_FULL,
#     retry_failed: bool = False,
#     current_epoch: str | None = None,
# ) -> list[tuple[str, str, str]]:
#     """Units the plan leaves out, as (id, status, reason).
#
#     Reported rather than silently dropped: a resume that rebuilds the server
#     side but omits the run that failed would finish with nothing transferred,
#     which should not look like success.
#     """
#     left_out: list[tuple[str, str, str]] = []
#     for session in manifest.sessions:
#         for kind, node, _ in iter_units(manifest, session):
#             execute, reason = should_execute(
#                 node, kind, current_epoch=current_epoch, mode=mode, retry_failed=retry_failed
#             )
#             if not execute:
#                 label = f"{session.session_id}/{kind}" if isinstance(node, Phase) else node.label
#                 left_out.append((label, node.status, reason))
#     return left_out
#
#
# def write_sql(units: list[Unit], out_dir: Path) -> list[Path]:
#     """Write every block to an inspectable file named after its manifest id."""
#     out_dir = Path(out_dir)
#     written: list[Path] = []
#     for unit in units:
#         for block in unit.blocks:
#             safe = block.block_id.replace("/", "__")
#             path = out_dir / unit.session_id / f"{safe}.{block.side}.sql"
#             path.parent.mkdir(parents=True, exist_ok=True)
#             path.write_text(block.sql.rstrip("\n") + "\n", encoding="utf-8")
#             written.append(path)
#     return written
#
# === END FILE: pullmanager/executor.py ===
# === BEGIN FILE: pullmanager/local_sql.py SHA256: 2effa5f5fc36cbafa0a72c872f3f3e362f6a5d8d9608c356fa6663f1697f3130 SIZE: 7093 ===
# """Projects-side SQL: destination tables and the transfer from Cosmos.
#
# Write mode is decided: the destination is dropped and created once per session
# in the setup phase, and every run appends. The old generator dropped inside
# each transfer block, which with batching leaves only the last batch.
# """
#
# from __future__ import annotations
#
# from typing import Any
#
# from .naming import destination, global_temp, local_staging
# from .normalize import normalize_bool
# from .sql import (
#     SqlBlock,
#     column_list,
#     column_names,
#     ddl_body,
#     quote_literal,
#     quote_name,
# )
#
# # Types whose stored length is worth measuring so templates can be tuned.
# _MEASURABLE = ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR")
#
#
# class LocalRenderError(ValueError):
#     """Raised when local SQL cannot be rendered."""
#
#
# def _columns(cohort: dict[str, Any]) -> list[dict[str, Any]]:
#     return [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
#
#
# def _remote_query(dest: str, columns: list[dict[str, Any]]) -> str:
#     """The inner query sent to the linked server, quoted for embedding."""
#     cols = ", ".join(quote_name(n) for n in column_names(columns))
#     inner = f"SELECT {cols} FROM {global_temp(dest)}"
#     return inner.replace("'", "''")
#
#
# def render_table_shell(cohort: dict[str, Any], project_db: str) -> str:
#     """Drop and create one destination table. Runs once per session."""
#     dest = cohort.get("dest_table")
#     if not dest:
#         raise LocalRenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
#     columns = _columns(cohort)
#     if not columns:
#         raise LocalRenderError(f"Cohort {dest!r} declares no columns.")
#     table = destination(project_db, dest)
#     return (
#         f"-- session table shell for {dest}\n"
#         f"DROP TABLE IF EXISTS {table};\n\n"
#         f"CREATE TABLE {table}\n(\n{ddl_body(columns)}\n);"
#     )
#
#
# def render_transfer(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
#     """Pull one cohort from its global temp into the destination table.
#
#     The slow OPENQUERY lands in a staging table outside the transaction, so no
#     lock is held while data crosses the linked server. Only the final insert is
#     transactional, which is what makes a retry after a partial failure safe.
#     """
#     if not linked_server:
#         raise LocalRenderError(
#             "No linked server supplied. The Cosmos instance name changes on every "
#             "connection and must come from the current session."
#         )
#     dest = str(cohort["dest_table"])
#     columns = _columns(cohort)
#     table = destination(project_db, dest)
#     staging = local_staging(dest)
#     cols = column_list(columns)
#
#     return (
#         f"-- transfer {global_temp(dest)} -> {table}\n"
#         f"DROP TABLE IF EXISTS {staging};\n\n"
#         f"SELECT {cols}\n"
#         f"INTO {staging}\n"
#         f"FROM OPENQUERY(\n"
#         f"    [{linked_server}],\n"
#         f"    '{_remote_query(dest, columns)}'\n"
#         f");\n\n"
#         f"BEGIN TRANSACTION;\n"
#         f"INSERT INTO {table} ({cols})\n"
#         f"SELECT {cols} FROM {staging};\n"
#         f"COMMIT TRANSACTION;"
#     )
#
#
# def render_row_counts(cohort: dict[str, Any], project_db: str, linked_server: str) -> str:
#     """Both sides of the transfer, so a mismatch is visible."""
#     dest = str(cohort["dest_table"])
#     table = destination(project_db, dest)
#     remote = f"SELECT 1 AS dummy FROM {global_temp(dest)}".replace("'", "''")
#     return (
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         "    'cosmos' AS [Side],\n"
#         "    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM OPENQUERY([{linked_server}], '{remote}');\n\n"
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         "    'projects' AS [Side],\n"
#         "    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM {table};"
#     )
#
#
# def render_length_probe(cohort: dict[str, Any]) -> str | None:
#     """Measure the widest value actually stored in each string column.
#
#     Reported rather than applied: the destination table is created in setup,
#     before any data exists, and sizing it from one batch would truncate a later
#     batch carrying a longer value.
#     """
#     dest = str(cohort["dest_table"])
#     staging = local_staging(dest)
#     measurable = [
#         c for c in _columns(cohort)
#         if str(c.get("type", "")).split("(")[0].strip().upper() in _MEASURABLE
#     ]
#     if not measurable:
#         return None
#     selects = [
#         "SELECT\n"
#         f"    {quote_literal(dest)} AS [DestTable],\n"
#         f"    {quote_literal(str(c['name']))} AS [Column],\n"
#         f"    {quote_literal(str(c.get('type')))} AS [DeclaredType],\n"
#         f"    MAX(LEN({quote_name(str(c['name']))})) AS [MaxLength]\n"
#         f"FROM {staging}"
#         for c in measurable
#     ]
#     return "\n UNION ALL\n".join(selects) + ";"
#
#
# def render_setup(doc: dict[str, Any], cohorts: list[dict[str, Any]], block_prefix: str) -> list[SqlBlock]:
#     """One shell block per destination table for the whole session."""
#     project_db = doc.get("project_db")
#     if not project_db:
#         raise LocalRenderError("Phase document has no `project_db`.")
#     blocks = []
#     for cohort in cohorts:
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             continue
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/shell/{cohort['dest_table']}",
#                 side="local",
#                 sql=render_table_shell(cohort, str(project_db)),
#                 dest_table=str(cohort["dest_table"]),
#                 meta={"destination": destination(str(project_db), cohort["dest_table"])},
#             )
#         )
#     return blocks
#
#
# def render_phase(doc: dict[str, Any], block_prefix: str, linked_server: str) -> list[SqlBlock]:
#     """Transfer, counts and measurement for every cohort in a phase."""
#     project_db = doc.get("project_db")
#     if not project_db:
#         raise LocalRenderError("Phase document has no `project_db`.")
#     blocks: list[SqlBlock] = []
#     for cohort in doc.get("cohorts") or []:
#         if not isinstance(cohort, dict) or not cohort.get("dest_table"):
#             continue
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             continue
#         dest = str(cohort["dest_table"])
#         parts = [
#             render_transfer(cohort, str(project_db), linked_server),
#             render_row_counts(cohort, str(project_db), linked_server),
#         ]
#         probe = render_length_probe(cohort)
#         if probe:
#             parts.append(probe)
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/{dest}",
#                 side="local",
#                 sql="\n\n".join(parts),
#                 dest_table=dest,
#                 meta={
#                     "destination": destination(str(project_db), dest),
#                     "staging": local_staging(dest),
#                     "global_temp": global_temp(dest),
#                     "linked_server": linked_server,
#                 },
#             )
#         )
#     return blocks
#
# === END FILE: pullmanager/local_sql.py ===
# === BEGIN FILE: pullmanager/manifest.py SHA256: 1ccaab95c26ba0f76151c64e58e2b5988e60558ae06a9ec1f4e082df23ca4508 SIZE: 11210 ===
# """Load, mutate, and write back `pullmanifest.yaml`.
#
# The manifest is YAML Manager's plan on the way in and Pullmanager's status
# document on the way out. Nodes wrap the loaded mappings in place so keys this
# version does not understand survive a round trip untouched.
# """
#
# from __future__ import annotations
#
# import os
# from pathlib import Path
# from typing import Any, Iterator
#
# from . import MANIFEST_VERSION
# from .models import (
#     BLOCKED,
#     DONE,
#     FAILED,
#     PENDING,
#     RUNNING,
#     SETTLED_STATUSES,
#     SKIPPED,
#     duration_block,
#     new_epoch,
#     now_iso,
#     validate_status,
# )
# from .yaml_io import dump_yaml, load_yaml
#
# PHASE_ORDER = ("setup", "upload_cohorts", "pk")
#
#
# class ManifestError(ValueError):
#     """Raised when a manifest does not match the expected contract."""
#
#
# class Node:
#     """A manifest entry carrying status and timing fields."""
#
#     def __init__(self, data: dict[str, Any], label: str, session: "Session | None" = None):
#         self._data = data
#         self.label = label
#         self._session = session
#
#     @property
#     def data(self) -> dict[str, Any]:
#         return self._data
#
#     @property
#     def status(self) -> str:
#         return self._data.get("status", PENDING)
#
#     @status.setter
#     def status(self, value: str) -> None:
#         self._data["status"] = validate_status(value)
#
#     @property
#     def yaml(self) -> str | None:
#         return self._data.get("yaml")
#
#     @property
#     def rows(self) -> int | None:
#         return self._data.get("rows")
#
#     @property
#     def outputs(self) -> dict[str, Any]:
#         return self._data.setdefault("outputs", {})
#
#     @property
#     def error(self) -> dict[str, Any] | None:
#         return self._data.get("error")
#
#     @property
#     def note(self) -> str | None:
#         return self._data.get("note")
#
#     @property
#     def epoch(self) -> str | None:
#         """The server connection this node last completed under."""
#         return self._data.get("epoch")
#
#     def is_stale(self, current_epoch: str | None) -> bool:
#         """True when this finished under a connection that no longer exists.
#
#         Server-side output (global temps, uploaded tables) from a stale node is
#         gone even though the status still reads `done`. Local Projects tables
#         are permanent and survive regardless.
#         """
#         if self.status != DONE:
#             return False
#         return self.epoch is not None and self.epoch != current_epoch
#
#     def start(self) -> None:
#         self.status = RUNNING
#         self._data["started_at"] = now_iso()
#         self._data["finished_at"] = None
#         self._data["error"] = None
#         self._data.pop("duration", None)
#
#     def finish(self, rows: int | None = None, outputs: dict[str, Any] | None = None) -> None:
#         self.status = DONE
#         self._stamp_finish()
#         if rows is not None:
#             self._data["rows"] = rows
#         if outputs:
#             self.outputs.update(outputs)
#         self._data["error"] = None
#
#     def fail(self, message: str, detail: str | None = None) -> None:
#         self.status = FAILED
#         self._stamp_finish()
#         self._data["error"] = {"message": message, "detail": detail}
#
#     def skip(self, reason: str | None = None) -> None:
#         self._settle_without_running(SKIPPED, reason)
#
#     def block(self, reason: str | None = None) -> None:
#         self._settle_without_running(BLOCKED, reason)
#
#     def _settle_without_running(self, status: str, reason: str | None) -> None:
#         # Reasons go in `note`, not `error`: a skipped phase is not a failure,
#         # and an `error` block on it would read like one.
#         self.status = status
#         if reason:
#             self._data["note"] = reason
#
#     def _stamp_finish(self) -> None:
#         self._data["finished_at"] = now_iso()
#         if self._session is not None and self._session.epoch is not None:
#             self._data["epoch"] = self._session.epoch
#         duration = duration_block(self._data.get("started_at"), self._data["finished_at"])
#         if duration is not None:
#             self._data["duration"] = duration
#
#     def __repr__(self) -> str:
#         return f"<{type(self).__name__} {self.label} {self.status}>"
#
#
# class Phase(Node):
#     def __init__(self, name: str, data: dict[str, Any], session: "Session"):
#         super().__init__(data, f"{session.label}/{name}", session)
#         self.name = name
#
#     @property
#     def pk_source(self) -> dict[str, Any] | None:
#         return self._data.get("pk_source")
#
#
# class Run(Node):
#     def __init__(self, data: dict[str, Any], session: "Session"):
#         super().__init__(data, str(data.get("run_id")), session)
#
#     @property
#     def run_id(self) -> str:
#         return self._data["run_id"]
#
#     @property
#     def batch(self) -> dict[str, Any] | None:
#         return self._data.get("batch")
#
#
# class Session(Node):
#     def __init__(self, data: dict[str, Any]):
#         super().__init__(data, str(data.get("session_id")))
#         raw_phases = data.get("phases") or {}
#         unknown = set(raw_phases) - set(PHASE_ORDER)
#         if unknown:
#             raise ManifestError(
#                 f"Session {self.label!r} has unknown phase(s): {', '.join(sorted(unknown))}. "
#                 f"Expected only: {', '.join(PHASE_ORDER)}"
#             )
#         self.phases = [
#             Phase(name, raw_phases[name], self)
#             for name in PHASE_ORDER
#             if name in raw_phases
#         ]
#         self.runs = [Run(run, self) for run in data.get("runs") or []]
#
#     @property
#     def session_id(self) -> str:
#         return self._data["session_id"]
#
#     @property
#     def pk_table(self) -> str | None:
#         return self._data.get("pk_table")
#
#     @property
#     def runtime(self) -> dict[str, Any]:
#         """Facts discovered when the session's connection opened."""
#         return self._data.setdefault("runtime", {})
#
#     @property
#     def epoch(self) -> str | None:
#         return self.runtime.get("epoch")
#
#     def begin_epoch(self, linked_server: str | None = None) -> str:
#         """Open a new server connection scope for this session.
#
#         `linked_server` is always overwritten, never left in place: the Cosmos
#         instance name changes on every connection, so carrying the previous
#         one forward would point later SQL at a server that is no longer ours.
#         """
#         epoch = new_epoch()
#         self.runtime["epoch"] = epoch
#         self.runtime["opened_at"] = now_iso()
#         self.runtime["linked_server"] = linked_server
#         return epoch
#
#     def stale_children(self) -> list[Node]:
#         """Nodes marked done whose server-side output died with a past epoch."""
#         return [child for child in self.children if child.is_stale(self.epoch)]
#
#     @property
#     def children(self) -> list[Node]:
#         return [*self.phases, *self.runs]
#
#     def recompute_status(self) -> str:
#         statuses = [child.status for child in self.children]
#         if not statuses:
#             return self.status
#         if FAILED in statuses:
#             rolled = FAILED
#         elif RUNNING in statuses:
#             rolled = RUNNING
#         elif BLOCKED in statuses:
#             rolled = BLOCKED
#         elif all(status == SKIPPED for status in statuses):
#             rolled = SKIPPED
#         elif all(status in SETTLED_STATUSES for status in statuses):
#             rolled = DONE
#         elif any(status in SETTLED_STATUSES for status in statuses):
#             rolled = RUNNING
#         else:
#             rolled = PENDING
#         self.status = rolled
#         return rolled
#
#
# class Manifest:
#     def __init__(self, data: dict[str, Any], path: Path | None = None):
#         if not isinstance(data, dict):
#             raise ManifestError("Manifest root must be a mapping.")
#         version = data.get("manifest_version")
#         if version != MANIFEST_VERSION:
#             raise ManifestError(
#                 f"Unsupported manifest_version {version!r}. This Pullmanager reads version {MANIFEST_VERSION}."
#             )
#         raw_sessions = data.get("sessions")
#         if not isinstance(raw_sessions, list):
#             raise ManifestError("Manifest `sessions` must be a list.")
#         self._data = data
#         self.path = path
#         self.sessions = [Session(session) for session in raw_sessions]
#         self._validate()
#
#     def _validate(self) -> None:
#         seen_sessions: set[str] = set()
#         seen_runs: set[str] = set()
#         for session in self.sessions:
#             if not session.data.get("session_id"):
#                 raise ManifestError("Every session requires a `session_id`.")
#             if session.session_id in seen_sessions:
#                 raise ManifestError(f"Duplicate session_id {session.session_id!r}.")
#             seen_sessions.add(session.session_id)
#             validate_status(session.status)
#             for phase in session.phases:
#                 if not phase.yaml:
#                     raise ManifestError(f"Phase {phase.label!r} is missing its `yaml` path.")
#                 validate_status(phase.status)
#             for run in session.runs:
#                 if not run.data.get("run_id"):
#                     raise ManifestError(f"A run in session {session.session_id!r} is missing `run_id`.")
#                 if run.run_id in seen_runs:
#                     raise ManifestError(f"Duplicate run_id {run.run_id!r}.")
#                 seen_runs.add(run.run_id)
#                 if not run.yaml:
#                     raise ManifestError(f"Run {run.run_id!r} is missing its `yaml` path.")
#                 validate_status(run.status)
#
#     @property
#     def data(self) -> dict[str, Any]:
#         return self._data
#
#     @property
#     def project(self) -> dict[str, Any]:
#         return self._data.get("project") or {}
#
#     @property
#     def source(self) -> dict[str, Any]:
#         return self._data.get("source") or {}
#
#     @property
#     def root(self) -> Path:
#         """Directory the manifest's relative `yaml` paths resolve against."""
#         if self.path is None:
#             raise ManifestError("Manifest has no path; cannot resolve relative YAML paths.")
#         return self.path.parent
#
#     def resolve(self, node: Node) -> Path:
#         if not node.yaml:
#             raise ManifestError(f"Node {node.label!r} has no `yaml` path to resolve.")
#         return self.root / node.yaml
#
#     def iter_nodes(self) -> Iterator[tuple[Session, Node]]:
#         for session in self.sessions:
#             for child in session.children:
#                 yield session, child
#
#     @classmethod
#     def load(cls, path: str | Path) -> "Manifest":
#         path = Path(path)
#         if not path.is_file():
#             raise ManifestError(f"Manifest not found: {path}")
#         return cls(load_yaml(path), path=path)
#
#     def save(self, path: str | Path | None = None) -> Path:
#         """Write the manifest atomically so a crash cannot truncate it."""
#         target = Path(path) if path else self.path
#         if target is None:
#             raise ManifestError("No path to save manifest to.")
#         for session in self.sessions:
#             session.recompute_status()
#         tmp = target.with_name(target.name + ".tmp")
#         dump_yaml(self._data, tmp)
#         os.replace(tmp, target)
#         self.path = target
#         return target
#
# === END FILE: pullmanager/manifest.py ===
# === BEGIN FILE: pullmanager/models.py SHA256: 2eb665cf240632931fdb1f07b9b4d6e04757c51fb188ea3d17c184c60cdf87be SIZE: 2154 ===
# """Status vocabulary and timing helpers shared by manifest nodes."""
#
# from __future__ import annotations
#
# import uuid
# from datetime import datetime
#
# PENDING = "pending"
# RUNNING = "running"
# DONE = "done"
# FAILED = "failed"
# SKIPPED = "skipped"
# BLOCKED = "blocked"
#
# ALL_STATUSES = (PENDING, RUNNING, DONE, FAILED, SKIPPED, BLOCKED)
#
# # Statuses that mean "do not execute again on a plain resume".
# SETTLED_STATUSES = (DONE, SKIPPED)
#
#
# class StatusError(ValueError):
#     """Raised when an unknown status string is supplied."""
#
#
# def validate_status(status: str) -> str:
#     if status not in ALL_STATUSES:
#         raise StatusError(
#             f"Unknown status {status!r}. Expected one of: {', '.join(ALL_STATUSES)}"
#         )
#     return status
#
#
# def new_epoch() -> str:
#     """Identify one live server connection.
#
#     Global temp tables die with the connection, so work recorded under a
#     previous epoch is known to be gone from the server even though the manifest
#     still says `done`.
#     """
#     return f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
#
#
# def now_iso() -> str:
#     """Local timestamp with UTC offset, e.g. 2026-09-22T14:03:00-05:00."""
#     return datetime.now().astimezone().isoformat(timespec="seconds")
#
#
# def parse_iso(value: str) -> datetime:
#     return datetime.fromisoformat(value)
#
#
# def format_duration(seconds: float) -> str:
#     """Render a duration as "1h 2m 3s" / "5m 32s" / "45s"."""
#     total = int(round(seconds))
#     sign = "-" if total < 0 else ""
#     total = abs(total)
#     hours, remainder = divmod(total, 3600)
#     minutes, secs = divmod(remainder, 60)
#     if hours:
#         return f"{sign}{hours}h {minutes}m {secs}s"
#     if minutes:
#         return f"{sign}{minutes}m {secs}s"
#     return f"{sign}{secs}s"
#
#
# def duration_block(started_at: str | None, finished_at: str | None) -> dict | None:
#     """Build the manifest `duration` mapping from two ISO timestamps."""
#     if not started_at or not finished_at:
#         return None
#     seconds = (parse_iso(finished_at) - parse_iso(started_at)).total_seconds()
#     return {"seconds": int(round(seconds)), "display": format_duration(seconds)}
#
# === END FILE: pullmanager/models.py ===
# === BEGIN FILE: pullmanager/naming.py SHA256: 873c90540e4fd526d86e65e9c918958c39be18384cadbce239de4fc2b23c6d45 SIZE: 4175 ===
# """Table naming rules.
#
# These come from the old generator and are invariants, not preferences: the
# server, the staging table and the destination must agree or a transfer fails.
# Every function here is idempotent, so applying a rule to an already-correct
# name is a no-op rather than a double prefix.
# """
#
# from __future__ import annotations
#
# import re
#
# GLOBAL_TEMP_PREFIX = "##JVM_"
# LOCAL_STAGING_PREFIX = "#Local_"
# DEFAULT_SCHEMA = "dbo"
#
# # A bare identifier, optionally bracketed: PatientDim, [PatientDim], ##JVM_X
# _IDENTIFIER = r"(?:\[[^\]]+\]|[A-Za-z_#@][\w@$#]*)"
# # A possibly-qualified reference, matched whole so `dbo.PatientDim` is never
# # mistaken for the bare identifier `dbo` and qualified a second time.
# _REFERENCE = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
# _FROM_OR_JOIN = re.compile(
#     rf"(?P<lead>\b(?:FROM|JOIN)\s+)(?P<table>{_REFERENCE})",
#     re.IGNORECASE,
# )
# _LEADING_TABLE = re.compile(rf"^(?P<ws>\s*)(?P<table>{_REFERENCE})")
#
#
# class NamingError(ValueError):
#     """Raised when a name cannot be derived."""
#
#
# def _require(dest_table: str | None) -> str:
#     if dest_table is None or not str(dest_table).strip():
#         raise NamingError("dest_table is required to derive a table name.")
#     return str(dest_table).strip()
#
#
# def base_name(dest_table: str | None) -> str:
#     """Strip any temp marker and generator prefix down to the bare name."""
#     name = _require(dest_table).lstrip("#")
#     for prefix in ("JVM_", "Local_"):
#         if name.upper().startswith(prefix.upper()):
#             name = name[len(prefix):]
#             break
#     return name
#
#
# def global_temp(dest_table: str | None) -> str:
#     """Cosmos session-scoped output: PKTable -> ##JVM_PKTable.
#
#     A dest_table that already starts with JVM_ is not prefixed twice, which is
#     the `##JVM_JVM_Foo` bug the old generator guarded against.
#     """
#     return GLOBAL_TEMP_PREFIX + base_name(dest_table)
#
#
# def local_staging(dest_table: str | None) -> str:
#     """Projects session-scoped staging: PKTable -> #Local_PKTable."""
#     return LOCAL_STAGING_PREFIX + base_name(dest_table)
#
#
# def destination(project_db: str | None, dest_table: str | None) -> str:
#     """Durable Projects table, always fully qualified.
#
#     Generated SQL gets run from tools with ambiguous database context, so the
#     destination never relies on USE.
#     """
#     if not project_db or not str(project_db).strip():
#         raise NamingError("project_db is required to qualify a destination table.")
#     return f"{str(project_db).strip()}.{DEFAULT_SCHEMA}.{base_name(dest_table)}"
#
#
# def is_temp_table(token: str) -> bool:
#     return token.lstrip("[").startswith("#")
#
#
# def is_schema_qualified(token: str) -> bool:
#     """True when a reference already names a schema.
#
#     Dots inside brackets do not count, so `[My.Table]` is still unqualified.
#     """
#     return "." in re.sub(r"\[[^\]]*\]", "", token)
#
#
# def qualify(token: str, database: str | None = None) -> str:
#     """Add the schema, and optionally the database, to a bare table reference.
#
#     A database is supplied when a cohort reads somewhere other than the
#     connected one -- a SneakPeek cohort under `cosmos_db: Dual`, where a
#     two-part name would silently resolve against COSMOS instead.
#
#     Left alone: temp tables, which live in tempdb and must stay unqualified,
#     and anything already carrying a schema, which is what prevents `dbo.dbo.`.
#     """
#     if is_temp_table(token) or is_schema_qualified(token):
#         return token
#     if database:
#         return f"{database}.{DEFAULT_SCHEMA}.{token}"
#     return f"{DEFAULT_SCHEMA}.{token}"
#
#
# def qualify_table_ref(ref: str, database: str | None = None) -> str:
#     """Qualify the leading table of a `from` entry: `PatientDim AS p`."""
#     match = _LEADING_TABLE.match(ref)
#     if not match:
#         return ref
#     table = match.group("table")
#     return ref[: match.start("table")] + qualify(table, database) + ref[match.end("table"):]
#
#
# def qualify_join_clause(clause: str, database: str | None = None) -> str:
#     """Qualify every table named after FROM or JOIN in a clause."""
#     return _FROM_OR_JOIN.sub(
#         lambda m: m.group("lead") + qualify(m.group("table"), database), clause
#     )
#
# === END FILE: pullmanager/naming.py ===
# === BEGIN FILE: pullmanager/normalize.py SHA256: dea303653e1cdc1504e1238ff1fc4ba21cb0e74cc2899f6c160512252a6471a7 SIZE: 8239 ===
# """Compatibility rules for hand-authored cohort YAML.
#
# Each function returns its result alongside any notes worth surfacing, because
# the failure these rules guard against is silence: the old generator accepted
# only `dedup_keys` and, given `dedup_key`, emitted no deduplication at all and
# said nothing, changing row counts invisibly.
# """
#
# from __future__ import annotations
#
# from typing import Any
#
# from .naming import global_temp
#
# TRUTHY = {"true", "yes", "y", "1", "on", "t"}
# FALSY = {"false", "no", "n", "0", "off", "f", ""}
#
# # `cosmos_db` accepts several spellings and two of them are directives rather
# # than database names: `Dual`/`both` means render both variants, and the
# # per-cohort `cosmos_db` says which database each one reads.
# COSMOS_DATABASES = {
#     "cosmos": "COSMOS",
#     "dual": "COSMOS",
#     "both": "COSMOS",
#     "cosmos_sneakpeek": "COSMOS_SneakPeek",
#     "sneakpeek": "COSMOS_SneakPeek",
#     "sp": "COSMOS_SneakPeek",
# }
#
# DEAD_TEST_OPTIONS = {
#     "stop_at_for_non_pk_tables": (
#         "no longer used; row limits now apply only to the root PK cohort"
#     ),
#     "print_md": "reporting is the manifest's job; no markdown run report is written",
#     "printout_md": "reporting is the manifest's job; no markdown run report is written",
# }
#
#
# class NormalizationError(ValueError):
#     """Raised when a value cannot be interpreted."""
#
#
# def normalize_bool(value: Any, *, default: bool = False) -> bool:
#     """Accept the YAML and human spellings of a boolean."""
#     if value is None:
#         return default
#     if isinstance(value, bool):
#         return value
#     if isinstance(value, (int, float)):
#         return bool(value)
#     text = str(value).strip().lower()
#     if text in TRUTHY:
#         return True
#     if text in FALSY:
#         return False
#     raise NormalizationError(f"Cannot interpret {value!r} as a boolean.")
#
#
# def normalize_dedup_keys(cohort: dict[str, Any]) -> tuple[list[list[str]], list[str]]:
#     """Return dedup keys as a list of key sets, plus any notes.
#
#     Canonical form is `dedup_keys`, a list of lists. The legacy singular
#     `dedup_key` is accepted and normalized rather than rejected: refusing the
#     file only relocates the friction, while accepting it loudly removes the
#     silent-no-dedup outcome entirely.
#     """
#     notes: list[str] = []
#     raw = cohort.get("dedup_keys")
#     legacy = cohort.get("dedup_key")
#
#     if raw is not None and legacy is not None:
#         raise NormalizationError(
#             f"Cohort {cohort.get('dest_table') or cohort.get('name')!r} sets both "
#             "`dedup_keys` and legacy `dedup_key`. Keep only `dedup_keys`."
#         )
#
#     if raw is None and legacy is None:
#         return [], notes
#
#     if raw is None:
#         raw = legacy
#         notes.append(
#             f"`dedup_key` is legacy; normalized to `dedup_keys` for "
#             f"{cohort.get('dest_table') or cohort.get('name')!r}. Update the template."
#         )
#
#     if isinstance(raw, str):
#         key_sets = [[raw]]
#     elif isinstance(raw, list):
#         if not raw:
#             raise NormalizationError("`dedup_keys` was supplied but is empty.")
#         # A flat list is one key set; a list of lists is several.
#         if all(isinstance(item, list) for item in raw):
#             key_sets = [[str(col) for col in item] for item in raw]
#         elif any(isinstance(item, list) for item in raw):
#             raise NormalizationError(
#                 "`dedup_keys` mixes bare columns and key sets. Use either "
#                 "[[a, b]] for one key set or [[a], [b]] for two."
#             )
#         else:
#             key_sets = [[str(col) for col in raw]]
#     else:
#         raise NormalizationError(f"Cannot interpret dedup keys from {raw!r}.")
#
#     for key_set in key_sets:
#         if not key_set:
#             raise NormalizationError("`dedup_keys` contains an empty key set.")
#     return key_sets, notes
#
#
# def validate_dedup_columns(
#     key_sets: list[list[str]], cohort: dict[str, Any]
# ) -> list[str]:
#     """Check dedup keys name columns the cohort actually produces."""
#     produced = {
#         str(col.get("name"))
#         for col in cohort.get("columns") or []
#         if isinstance(col, dict) and col.get("name")
#     }
#     problems = []
#     for key_set in key_sets:
#         unknown = [col for col in key_set if col not in produced]
#         if unknown:
#             problems.append(
#                 f"dedup key(s) {', '.join(unknown)} are not columns of "
#                 f"{cohort.get('dest_table') or cohort.get('name')!r}"
#             )
#     return problems
#
#
# def dead_options(test_options: dict[str, Any] | None) -> list[str]:
#     """Notes for retired `test_options` keys that are still present."""
#     if not test_options:
#         return []
#     return [
#         f"`test_options.{key}` is ignored: {why}"
#         for key, why in DEAD_TEST_OPTIONS.items()
#         if key in test_options
#     ]
#
#
# def cosmos_database(value: Any, default: str = "COSMOS") -> str:
#     """The database to connect to, from a `cosmos_db` setting.
#
#     `Dual` and `both` are expansion directives, not database names; connecting
#     with `Database=Dual` would simply fail. Under those the connection goes to
#     COSMOS and the SneakPeek cohorts qualify their own tables instead.
#     """
#     if value is None or not str(value).strip():
#         return default
#     key = str(value).strip().lower()
#     if key in COSMOS_DATABASES:
#         return COSMOS_DATABASES[key]
#     raise NormalizationError(
#         f"Unsupported cosmos_db {value!r}. Expected one of: "
#         + ", ".join(sorted(COSMOS_DATABASES))
#     )
#
#
# def is_dual(value: Any) -> bool:
#     return str(value or "").strip().lower() in ("dual", "both")
#
#
# def is_pk(cohort: dict[str, Any]) -> bool:
#     return str(cohort.get("type", "")).strip().lower() == "pk"
#
#
# def pk_cohorts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
#     return [c for c in cohorts if isinstance(c, dict) and is_pk(c)]
#
#
# def joined_generated_tables(cohort: dict[str, Any]) -> set[str]:
#     """Global temp names this cohort joins, found in its filter text."""
#     filter_block = cohort.get("filter") or {}
#     text_parts: list[str] = []
#     for key in ("from", "join", "where"):
#         value = filter_block.get(key)
#         if isinstance(value, str):
#             text_parts.append(value)
#         elif isinstance(value, list):
#             text_parts.extend(str(item) for item in value)
#     haystack = " ".join(text_parts).upper()
#     return {token for token in _global_temp_tokens(haystack)}
#
#
# def _global_temp_tokens(haystack: str) -> set[str]:
#     tokens: set[str] = set()
#     marker = "##JVM_"
#     start = haystack.find(marker)
#     while start != -1:
#         end = start + len(marker)
#         while end < len(haystack) and (haystack[end].isalnum() or haystack[end] == "_"):
#             end += 1
#         tokens.add(haystack[start:end])
#         start = haystack.find(marker, end)
#     return tokens
#
#
# def root_pk_cohorts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
#     """PK cohorts that do not depend on another PK cohort's global temp.
#
#     A chained PK (patients -> diagnosis events for those patients) has exactly
#     one root. Row limits apply there, because limiting a downstream PK as well
#     compounds the restriction into an unrepresentative sample.
#     """
#     pks = pk_cohorts(cohorts)
#     sibling_temps = {global_temp(c.get("dest_table")).upper() for c in pks if c.get("dest_table")}
#     roots = []
#     for cohort in pks:
#         own = global_temp(cohort.get("dest_table")).upper() if cohort.get("dest_table") else None
#         depends_on = joined_generated_tables(cohort) & sibling_temps
#         depends_on.discard(own)
#         if not depends_on:
#             roots.append(cohort)
#     return roots
#
#
# def root_pk_cohort(cohorts: list[dict[str, Any]]) -> dict[str, Any] | None:
#     """The single root PK cohort, or None when there is no PK at all."""
#     roots = root_pk_cohorts(cohorts)
#     if not roots:
#         return None
#     if len(roots) > 1:
#         names = ", ".join(str(c.get("dest_table") or c.get("name")) for c in roots)
#         raise NormalizationError(
#             f"Several PK cohorts depend on nothing ({names}), so the root PK is "
#             "ambiguous. Chain them, or mark one as canonical."
#         )
#     return roots[0]
#
# === END FILE: pullmanager/normalize.py ===
# === BEGIN FILE: pullmanager/server_sql.py SHA256: 2bc6ce2982bcf2c8e76bf0e6a7a2458a340df252ad006aae19dbd6cba2a04a97 SIZE: 8743 ===
# """Cosmos-side SQL.
#
# Renders one block per cohort, addressed by manifest id. Nothing downstream
# searches SQL text to decide what to run.
# """
#
# from __future__ import annotations
#
# import re
#
# from typing import Any
#
# from .naming import global_temp
# from .normalize import (
#     cosmos_database,
#     normalize_bool,
#     normalize_dedup_keys,
#     root_pk_cohorts,
#     validate_dedup_columns,
# )
# from .sql import (
#     SqlBlock,
#     column_list,
#     column_names,
#     ddl_body,
#     non_null_predicates,
#     quote_literal,
#     quote_name,
#     render_source_clause,
#     render_where,
#     where_entries,
# )
#
# SERVER_NAME_QUERY = "SELECT @@SERVERNAME AS CosmosServerName;"
#
# _PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
#
#
# class RenderError(ValueError):
#     """Raised when a cohort cannot be rendered."""
#
#
# def top_clause(
#     cohort: dict[str, Any], doc: dict[str, Any], roots: list[dict[str, Any]]
# ) -> str:
#     """`TOP (n)`, applied to root PK cohorts only.
#
#     Limiting a downstream PK as well compounds the restriction: 500 patients
#     and then 500 of their events is not 500 patients' worth of events.
#
#     Plural because `cosmos_db: Dual` renders each cohort twice, once per
#     database. Those are parallel chains, not competing ones, so each has its
#     own root and each is limited.
#     """
#     options = doc.get("test_options") or {}
#     if not normalize_bool(options.get("smallset") or options.get("smallest")):
#         return ""
#     if not any(cohort is root for root in roots):
#         return ""
#     limit = options.get("stop_at_for_pk_table")
#     try:
#         limit = int(limit)
#     except (TypeError, ValueError):
#         return ""
#     return f"TOP ({limit}) " if limit > 0 else ""
#
#
# def cohort_predicates(cohort: dict[str, Any]) -> list[str]:
#     columns = cohort.get("columns") or []
#     return where_entries(cohort.get("filter") or {}) + non_null_predicates(columns)
#
#
# def cohort_database(cohort: dict[str, Any], doc: dict[str, Any]) -> str | None:
#     """The database this cohort reads, when it differs from the connection.
#
#     Under `cosmos_db: Dual` the SneakPeek variants carry their own `cosmos_db`,
#     and a two-part name would resolve against the connected COSMOS instead.
#     """
#     declared = cohort.get("cosmos_db")
#     if not declared:
#         return None
#     return cosmos_database(declared)
#
#
# def render_select(
#     cohort: dict[str, Any], top: str, inner_indent: str = "    ", database: str | None = None
# ) -> str:
#     columns = cohort.get("columns") or []
#     projections = [
#         f"{inner_indent}{column['source']} AS {quote_name(str(column['name']))}"
#         for column in columns
#         if isinstance(column, dict) and column.get("name") and column.get("source")
#     ]
#     parts = [f"SELECT {top}".rstrip(), ",\n".join(projections)]
#     source = render_source_clause(cohort.get("filter") or {}, database=database)
#     if source:
#         parts.append(source)
#     predicates = cohort_predicates(cohort)
#     if predicates:
#         parts.append("WHERE")
#         parts.append(render_where(predicates, inner_indent))
#     return "\n".join(parts)
#
#
# def render_dedup_select(
#     cohort: dict[str, Any],
#     key_sets: list[list[str]],
#     top: str,
#     database: str | None = None,
# ) -> tuple[str, list[str]]:
#     """Wrap the projection in ROW_NUMBER and keep one row per key set.
#
#     Returns the SQL and any notes. Deduplication is always visible in the
#     output: the old generator could silently emit none at all.
#     """
#     notes: list[str] = []
#     keys = [quote_name(k) for k in key_sets[0]]
#     if len(key_sets) > 1:
#         notes.append(
#             f"Only the first dedup key set {key_sets[0]} is applied; "
#             f"{len(key_sets) - 1} further set(s) were declared."
#         )
#     order = cohort.get("dedup_order") or cohort.get("order_by")
#     if order:
#         order_sql = order if isinstance(order, str) else ", ".join(str(o) for o in order)
#     else:
#         order_sql = ", ".join(keys)
#         notes.append(
#             f"No dedup ordering supplied for {cohort.get('dest_table')!r}; ordering by the "
#             "key columns, so the surviving row among duplicates is arbitrary but stable."
#         )
#     inner = render_select(cohort, top="", inner_indent="        ", database=database)
#     inner = inner.replace(
#         "SELECT\n",
#         "SELECT\n"
#         f"        ROW_NUMBER() OVER (PARTITION BY {', '.join(keys)} ORDER BY {order_sql}) AS [_dedup_rn],\n",
#         1,
#     )
#     cols = column_list(cohort.get("columns") or [])
#     sql = (
#         f"SELECT {top}{cols}\n"
#         f"FROM (\n"
#         f"{_indent(inner, '    ')}\n"
#         f") AS [_deduped]\n"
#         f"WHERE [_deduped].[_dedup_rn] = 1"
#     )
#     return sql, notes
#
#
# def _indent(text: str, prefix: str) -> str:
#     return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())
#
#
# def render_cohort(
#     cohort: dict[str, Any],
#     doc: dict[str, Any],
#     roots: list[dict[str, Any]] | None = None,
# ) -> tuple[str, list[str]]:
#     """DDL plus population for one cohort's global temp table."""
#     dest = cohort.get("dest_table")
#     if not dest:
#         raise RenderError(f"Cohort {cohort.get('name')!r} has no dest_table.")
#     columns = [c for c in cohort.get("columns") or [] if isinstance(c, dict) and c.get("name")]
#     if not columns:
#         raise RenderError(f"Cohort {dest!r} declares no columns.")
#     names = column_names(columns)
#     if len(names) != len(set(names)):
#         duplicates = sorted({n for n in names if names.count(n) > 1})
#         raise RenderError(f"Cohort {dest!r} declares duplicate column(s): {', '.join(duplicates)}")
#
#     notes: list[str] = []
#     temp = global_temp(dest)
#     top = top_clause(cohort, doc, roots or [])
#     database = cohort_database(cohort, doc)
#
#     key_sets, dedup_notes = normalize_dedup_keys(cohort)
#     notes.extend(dedup_notes)
#     if key_sets:
#         problems = validate_dedup_columns(key_sets, cohort)
#         if problems:
#             raise RenderError("; ".join(problems))
#         body, more = render_dedup_select(cohort, key_sets, top, database)
#         notes.extend(more)
#     else:
#         body = render_select(cohort, top, database=database)
#
#     sql = (
#         f"-- cohort {cohort.get('name')!r} -> {temp}\n"
#         f"DROP TABLE IF EXISTS {temp};\n\n"
#         f"CREATE TABLE {temp}\n(\n{ddl_body(columns)}\n);\n\n"
#         f"INSERT INTO {temp} ({column_list(columns)})\n"
#         f"{body};\n\n"
#         f"{render_cohort_telemetry(cohort, temp)}"
#     )
#     # Variable substitution is YAML Manager's job and has already happened by
#     # the time a split YAML reaches us. A placeholder surviving to here would
#     # render as invalid T-SQL, so fail with the name rather than emit it.
#     leftover = sorted({m.group(1) for m in _PLACEHOLDER.finditer(sql)})
#     if leftover:
#         raise RenderError(
#             f"Cohort {dest!r} still contains unsubstituted placeholder(s): "
#             f"{', '.join(leftover)}. Render from split YAML, not a raw template."
#         )
#     return sql, notes
#
#
# def render_cohort_telemetry(cohort: dict[str, Any], temp: str) -> str:
#     """A declared telemetry shape, not column names to be scraped."""
#     return (
#         "SELECT\n"
#         f"    {quote_literal(cohort.get('name'))} AS [CohortName],\n"
#         f"    {quote_literal(cohort.get('dest_table'))} AS [DestTable],\n"
#         f"    COUNT_BIG(1) AS [RowCount]\n"
#         f"FROM {temp};"
#     )
#
#
# def render_phase(doc: dict[str, Any], block_prefix: str) -> tuple[list[SqlBlock], list[str]]:
#     """Render every cohort in one phase document."""
#     cohorts = [c for c in doc.get("cohorts") or [] if isinstance(c, dict)]
#     roots = root_pk_cohorts(cohorts)
#     blocks: list[SqlBlock] = []
#     notes: list[str] = []
#     for cohort in cohorts:
#         if not normalize_bool(cohort.get("pull_this_cycle"), default=True):
#             notes.append(f"Skipping {cohort.get('dest_table')!r}: pull_this_cycle is false.")
#             continue
#         sql, cohort_notes = render_cohort(cohort, doc, roots)
#         notes.extend(cohort_notes)
#         blocks.append(
#             SqlBlock(
#                 block_id=f"{block_prefix}/{cohort['dest_table']}",
#                 side="server",
#                 sql=sql,
#                 dest_table=str(cohort["dest_table"]),
#                 meta={"global_temp": global_temp(cohort["dest_table"])},
#             )
#         )
#     return blocks, notes
#
#
# def render_setup(doc: dict[str, Any], block_prefix: str) -> list[SqlBlock]:
#     """Capture the runtime instance name; it changes on every connection."""
#     return [
#         SqlBlock(
#             block_id=f"{block_prefix}/server-identity",
#             side="server",
#             sql=SERVER_NAME_QUERY,
#             meta={"captures": "linked_server"},
#         )
#     ]
#
# === END FILE: pullmanager/server_sql.py ===
# === BEGIN FILE: pullmanager/session.py SHA256: 38807f146902c57d45d0227986e71f33df69bf3ee2bc1d29cdbfec3e0cea6675 SIZE: 14925 ===
# """Executing one session.
#
# The Cosmos connection is held open for the whole session, because every
# `##JVM_*` table dies with it. That single fact shapes everything here: the
# epoch, what a resume must replay, and why uploads travel through the client.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Callable
#
# from . import local_sql, server_sql, uploads
# from .batches import BatchError, select_batch_rows
# from .db import DatabaseError, Settings, bulk_insert, capture_server_name, connect, execute_script
# from .executor import RESUME_FULL, Unit, iter_units, plan_unit, session_cohorts, should_execute
# from .manifest import Manifest, Phase, Session
# from .naming import destination, global_temp
# from .normalize import cosmos_database
# from .uploads import UploadError
# from .yaml_io import load_yaml
#
# # The old generator warned past this; a pull this size is usually a mistake in
# # the filter rather than an intention.
# LARGE_ROW_WARNING = 80_000_000
#
#
# class SessionError(RuntimeError):
#     """Raised when a session cannot proceed."""
#
#
# @dataclass
# class SessionReport:
#     session_id: str
#     epoch: str = ""
#     linked_server: str = ""
#     completed: list[str] = field(default_factory=list)
#     failed: list[tuple[str, str]] = field(default_factory=list)
#     skipped: list[str] = field(default_factory=list)
#     warnings: list[str] = field(default_factory=list)
#
#     @property
#     def ok(self) -> bool:
#         return not self.failed
#
#
# class SessionRunner:
#     """Runs one session's phases and runs against live connections."""
#
#     def __init__(
#         self,
#         manifest: Manifest,
#         session: Session,
#         settings: Settings,
#         *,
#         connect_fn: Callable[..., Any] = connect,
#         mode: str = RESUME_FULL,
#         retry_failed: bool = False,
#         upload_root: Path | None = None,
#     ):
#         self.manifest = manifest
#         self.session = session
#         self.settings = settings
#         self._connect = connect_fn
#         self.mode = mode
#         self.retry_failed = retry_failed
#         self.upload_root = upload_root or manifest.root
#         self.cosmos: Any = None
#         self.projects: Any = None
#         self.report = SessionReport(session_id=session.session_id)
#
#     # ----------------------------------------------------------- lifecycle
#
#     def open(self) -> None:
#         """Open the connection whose lifetime defines the session."""
#         doc = self._phase_doc("setup")
#         self.cosmos = self._connect(
#             self.settings.cosmos_connection_string(cosmos_database(doc.get("cosmos_db"))),
#             login_timeout=self.settings.login_timeout,
#             query_timeout=self.settings.query_timeout,
#         )
#         # Captured per connection: the instance name changes every time, so a
#         # cached one would aim OPENQUERY at a server that is no longer ours.
#         linked_server = capture_server_name(self.cosmos)
#         epoch = self.session.begin_epoch(linked_server=linked_server)
#         self.report.epoch = epoch
#         self.report.linked_server = linked_server
#
#         project_db = doc.get("project_db")
#         if not project_db:
#             raise SessionError(f"{self.session.session_id}: setup.yaml has no project_db.")
#         self.project_db = str(project_db)
#         self.projects = self._connect(
#             self.settings.projects_connection_string(self.project_db),
#             login_timeout=self.settings.login_timeout,
#             query_timeout=self.settings.query_timeout,
#         )
#         self.manifest.save()
#
#     def close(self) -> None:
#         for connection in (self.projects, self.cosmos):
#             if connection is None:
#                 continue
#             try:
#                 connection.close()
#             except Exception:
#                 pass
#         self.projects = self.cosmos = None
#
#     def __enter__(self) -> "SessionRunner":
#         self.open()
#         return self
#
#     def __exit__(self, *exc_info) -> None:
#         self.close()
#
#     # --------------------------------------------------------------- units
#
#     def _phase_doc(self, kind: str) -> dict[str, Any]:
#         for name, node, path in iter_units(self.manifest, self.session):
#             if name == kind:
#                 return load_yaml(path) or {}
#         raise SessionError(f"{self.session.session_id}: no {kind} phase in the manifest.")
#
#     def execute(self) -> SessionReport:
#         """Run every unit that needs running, in order.
#
#         A failed run does not stop its siblings: batches are disjoint appends
#         and independent once the PK exists, so one night produces one list of
#         every failure. A failed phase does block what follows it, since setup,
#         uploads and PK are prerequisites.
#         """
#         blocked = False
#         for kind, node, path in iter_units(self.manifest, self.session):
#             label = node.label
#             if blocked:
#                 node.block("an earlier phase in this session failed")
#                 self.report.skipped.append(label)
#                 self.manifest.save()
#                 continue
#
#             execute, reason = should_execute(
#                 node,
#                 kind,
#                 current_epoch=self.session.epoch,
#                 mode=self.mode,
#                 retry_failed=self.retry_failed,
#             )
#             if not execute:
#                 self.report.skipped.append(f"{label} ({reason})")
#                 continue
#
#             node.start()
#             self.manifest.save()
#             try:
#                 rows = self._run_unit(kind, node, path)
#             except Exception as exc:
#                 node.fail(str(exc), detail=type(exc).__name__)
#                 self.report.failed.append((label, str(exc)))
#                 self.manifest.save()
#                 if isinstance(node, Phase):
#                     blocked = True
#                 continue
#             node.finish(rows=rows)
#             self.report.completed.append(label)
#             self.manifest.save()
#         return self.report
#
#     def _run_unit(self, kind: str, node: Any, path: Path) -> int | None:
#         if kind == "setup":
#             self._run_setup(path)
#             return None
#         if kind == "upload_cohorts":
#             return self._run_uploads(path)
#         if kind == "pk":
#             return self._run_pk(node, path)
#         return self._run_run(node, path)
#
#     # --------------------------------------------------------------- setup
#
#     def _run_setup(self, path: Path) -> None:
#         """Create the destination tables once; runs then append to them."""
#         doc = load_yaml(path) or {}
#         cohorts = session_cohorts(self.manifest, self.session)
#         for block in local_sql.render_setup(doc, cohorts, f"{self.session.session_id}/setup"):
#             execute_script(self.projects, block.sql, label=block.block_id)
#         self.projects.commit()
#         node_outputs = {"linked_server": self.report.linked_server, "tables": len(cohorts)}
#         self.session.phases[0].outputs.update(node_outputs)
#
#     # ------------------------------------------------------------- uploads
#
#     def _run_uploads(self, path: Path) -> int | None:
#         doc = load_yaml(path) or {}
#         enabled = uploads.enabled_uploads(doc)
#         if not enabled:
#             return None
#         uploaded = 0
#         for cohort in enabled:
#             kind = uploads.upload_kind(cohort)
#             if kind == "csv":
#                 plan = uploads.plan_csv_upload(cohort, self.upload_root)
#             else:
#                 plan = uploads.plan_dbtable_upload(self.projects, cohort, self.project_db)
#             self.report.warnings.extend(plan.notes)
#             uploaded += uploads.materialize(
#                 self.cosmos, plan, chunk_size=self.settings.upload_chunk
#             )
#             self.cosmos.commit()
#         return uploaded
#
#     # ------------------------------------------------------------------ pk
#
#     def _run_pk(self, node: Any, path: Path) -> int | None:
#         """Build the PK, land it in Projects, and prove its key is unique."""
#         doc = load_yaml(path) or {}
#         unit = plan_unit(
#             self.manifest, self.session, "pk", node, path, self.report.linked_server
#         )
#         rows = self._run_pair(unit)
#         node.outputs["global_temp"] = global_temp(self.session.pk_table or "")
#         node.outputs["local_table"] = destination(self.project_db, self.session.pk_table or "")
#         self._verify_pk_uniqueness(doc)
#         return rows
#
#     def _verify_pk_uniqueness(self, doc: dict[str, Any]) -> None:
#         """A non-unique key makes ORDER BY arbitrary, so chunks stop being stable."""
#         keys = self._pk_key_columns(doc)
#         if not keys:
#             self.report.warnings.append(
#                 "PK declares no key_column, so chunk ordering cannot be verified as stable."
#             )
#             return
#         table = destination(self.project_db, self.session.pk_table or "")
#         columns = ", ".join(f"[{k}]" for k in keys)
#         cursor = self.projects.cursor()
#         cursor.execute(f"SELECT COUNT_BIG(1), COUNT_BIG(DISTINCT {columns}) FROM {table};")
#         row = cursor.fetchone()
#         if not row:
#             return
#         total, distinct = int(row[0]), int(row[1])
#         if total != distinct:
#             raise SessionError(
#                 f"PK {table} has {total} rows but only {distinct} distinct "
#                 f"{', '.join(keys)}. Row chunking orders by that key, so duplicates make "
#                 "a chunk mean different rows each run. Add dedup_keys to the PK cohort."
#             )
#         if total >= LARGE_ROW_WARNING:
#             self.report.warnings.append(
#                 f"PK {table} has {total:,} rows, past the {LARGE_ROW_WARNING:,} warning "
#                 "threshold. Check the filter before running the fact pulls."
#             )
#
#     def _pk_key_columns(self, doc: dict[str, Any]) -> list[str]:
#         for cohort in doc.get("cohorts") or []:
#             if not isinstance(cohort, dict):
#                 continue
#             key = cohort.get("key_column") or cohort.get("key_columns")
#             if isinstance(key, str):
#                 return [key]
#             if isinstance(key, list) and key:
#                 return [str(k) for k in key]
#         pk_source = next((p.pk_source for p in self.session.phases if p.pk_source), None)
#         if pk_source and pk_source.get("key_columns"):
#             return [str(k) for k in pk_source["key_columns"]]
#         return []
#
#     # ----------------------------------------------------------------- run
#
#     def _run_run(self, node: Any, path: Path) -> int | None:
#         self._materialize_batch(node)
#         unit = plan_unit(
#             self.manifest, self.session, "run", node, path, self.report.linked_server
#         )
#         return self._run_pair(unit)
#
#     def _materialize_batch(self, node: Any) -> None:
#         """Narrow the PK temp to just this batch, leaving cohort SQL untouched.
#
#         The run YAML joins the PK temp by name, so replacing its contents is
#         enough; nothing in the rendered SQL needs to know about batching.
#         """
#         batch = node.batch
#         if not batch:
#             return
#         pk_table = self.session.pk_table
#         if not pk_table:
#             raise SessionError(f"{node.label}: the session has no pk_table to narrow.")
#
#         doc = self._phase_doc("pk")
#         keys = self._pk_key_columns(doc)
#         try:
#             selection = select_batch_rows(self.project_db, pk_table, batch, keys)
#         except BatchError as exc:
#             raise SessionError(f"{node.label}: {exc}") from exc
#
#         cursor = self.projects.cursor()
#         cursor.execute(selection.sql, selection.params)
#         columns = [column[0] for column in cursor.description or []]
#         rows = [tuple(row) for row in cursor.fetchall()]
#         if not rows:
#             self.report.warnings.append(
#                 f"{node.label}: batch ({selection.description}) matched no PK rows."
#             )
#
#         temp = global_temp(pk_table)
#         pk_doc_cohort = next(
#             (c for c in doc.get("cohorts") or [] if isinstance(c, dict)
#              and c.get("dest_table") == pk_table),
#             None,
#         )
#         if pk_doc_cohort is None:
#             raise SessionError(f"{node.label}: no PK cohort named {pk_table!r} in pk.yaml.")
#         shell, _ = server_sql.render_cohort(pk_doc_cohort, doc)
#         create_only = shell.split("INSERT INTO")[0]
#         execute_script(self.cosmos, create_only, label=f"{node.label} batch shell")
#         if rows:
#             bulk_insert(
#                 self.cosmos, temp, columns, rows, chunk_size=self.settings.upload_chunk
#             )
#         self.cosmos.commit()
#         node.outputs["batch_pk_rows"] = len(rows)
#         node.outputs["batch"] = selection.description
#
#     # ------------------------------------------------------------- helpers
#
#     def _run_pair(self, unit: Unit) -> int | None:
#         """Server blocks, then the local transfer, then compare both counts."""
#         server_rows: dict[str, int] = {}
#         for block in unit.server_blocks:
#             outcome = execute_script(self.cosmos, block.sql, label=block.block_id)
#             for row in outcome.rows_of("DestTable", "RowCount"):
#                 server_rows[str(row["DestTable"])] = int(row["RowCount"])
#         self.cosmos.commit()
#
#         local_rows: dict[str, int] = {}
#         for block in unit.local_blocks:
#             outcome = execute_script(self.projects, block.sql, label=block.block_id)
#             for row in outcome.rows_of("DestTable", "Side", "RowCount"):
#                 if row["Side"] == "projects":
#                     local_rows[str(row["DestTable"])] = int(row["RowCount"])
#             for row in outcome.rows_of("DestTable", "Column", "MaxLength"):
#                 if row["MaxLength"] is None:
#                     continue
#                 self.report.warnings.append(
#                     f"{row['DestTable']}.{row['Column']} declared {row['DeclaredType']}, "
#                     f"widest value {row['MaxLength']}"
#                     if row.get("DeclaredType") else
#                     f"{row['DestTable']}.{row['Column']} widest value {row['MaxLength']}"
#                 )
#         self.projects.commit()
#
#         for dest, count in server_rows.items():
#             if count >= LARGE_ROW_WARNING:
#                 self.report.warnings.append(
#                     f"{dest} produced {count:,} rows, past the "
#                     f"{LARGE_ROW_WARNING:,} warning threshold."
#                 )
#             landed = local_rows.get(dest)
#             if landed is not None and landed != count:
#                 self.report.warnings.append(
#                     f"{dest}: Cosmos reported {count:,} rows but {landed:,} landed in "
#                     "Projects. The transfer did not carry everything."
#                 )
#         return next(iter(server_rows.values()), None)
#
# === END FILE: pullmanager/session.py ===
# === BEGIN FILE: pullmanager/sql.py SHA256: e894b41f2d51392c0690d2f9e7c04d1d617fb3288505a65fccfe22dbf03dbc2c SIZE: 5320 ===
# """Shared SQL construction helpers.
#
# The delicate part is the WHERE builder. Authors write predicates as a list of
# lines, and a line may continue a parenthesised boolean group started by the
# previous one, so `AND` cannot simply be inserted between entries.
# """
#
# from __future__ import annotations
#
# from dataclasses import dataclass, field
# from typing import Any
#
# from .naming import qualify_join_clause, qualify_table_ref
# from .normalize import normalize_bool
#
# # A line that already begins with one of these continues the previous
# # predicate, so prefixing `AND` would produce invalid SQL.
# CONTINUATION_PREFIXES = ("AND", "OR", ")", "--")
#
#
# @dataclass
# class SqlBlock:
#     """One executable unit, addressed by manifest id rather than by text."""
#
#     block_id: str
#     side: str  # "server" or "local"
#     sql: str
#     dest_table: str | None = None
#     meta: dict[str, Any] = field(default_factory=dict)
#
#     def __repr__(self) -> str:
#         return f"<SqlBlock {self.side} {self.block_id}>"
#
#
# def quote_literal(value: Any) -> str:
#     """Render a Python value as a T-SQL literal."""
#     if value is None:
#         return "NULL"
#     if isinstance(value, bool):
#         return "1" if value else "0"
#     if isinstance(value, (int, float)):
#         return str(value)
#     return "'" + str(value).replace("'", "''") + "'"
#
#
# def quote_name(name: str) -> str:
#     return f"[{name}]"
#
#
# def column_names(columns: list[dict[str, Any]]) -> list[str]:
#     return [str(c["name"]) for c in columns if isinstance(c, dict) and c.get("name")]
#
#
# def column_list(columns: list[dict[str, Any]], indent: str = "") -> str:
#     return ", ".join(quote_name(n) for n in column_names(columns))
#
#
# def is_nullable(column: dict[str, Any]) -> bool:
#     return normalize_bool(column.get("nullable"), default=True)
#
#
# def ddl_body(columns: list[dict[str, Any]]) -> str:
#     """The column definitions inside a CREATE TABLE."""
#     lines = []
#     for column in columns:
#         if not isinstance(column, dict) or not column.get("name"):
#             continue
#         null = "NULL" if is_nullable(column) else "NOT NULL"
#         lines.append(f"    {quote_name(str(column['name']))} {column.get('type', 'VARCHAR(900)')} {null}")
#     return ",\n".join(lines)
#
#
# def from_entries(filter_block: dict[str, Any]) -> list[str]:
#     """`from` may be a string or a list; the old renderer tolerated both."""
#     value = (filter_block or {}).get("from")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def join_entries(filter_block: dict[str, Any]) -> list[str]:
#     value = (filter_block or {}).get("join")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def where_entries(filter_block: dict[str, Any]) -> list[str]:
#     value = (filter_block or {}).get("where")
#     if not value:
#         return []
#     if isinstance(value, str):
#         return [value]
#     return [str(item) for item in value if str(item).strip()]
#
#
# def non_null_predicates(columns: list[dict[str, Any]]) -> list[str]:
#     """`IS NOT NULL` for every column the cohort declares as non-nullable.
#
#     Without these a NOT NULL destination column rejects the insert partway
#     through, after the expensive part of the pull has already run.
#     """
#     predicates = []
#     for column in columns:
#         if not isinstance(column, dict) or is_nullable(column):
#             continue
#         source = str(column.get("source") or "").strip()
#         if source:
#             predicates.append(f"{source} IS NOT NULL")
#     return predicates
#
#
# def render_where(predicates: list[str], indent: str = "    ") -> str:
#     """Join predicates with AND, leaving continuation lines alone.
#
#     A grouped code list authored as separate list entries must survive intact:
#
#         ( dt.Value LIKE 'K50.%'      ->  AND ( dt.Value LIKE 'K50.%'
#           OR dt.Value = 'K50'        ->      OR dt.Value = 'K50'
#         )                            ->      )
#     """
#     rendered: list[str] = []
#     first = True
#     for raw in predicates:
#         for line in str(raw).splitlines() or [""]:
#             stripped = line.strip()
#             if not stripped:
#                 continue
#             continues = stripped.upper().startswith(CONTINUATION_PREFIXES)
#             if first and not continues:
#                 rendered.append(f"{indent}{stripped}")
#                 first = False
#             elif continues:
#                 rendered.append(f"{indent}{stripped}")
#             else:
#                 rendered.append(f"{indent}AND {stripped}")
#                 first = False
#     return "\n".join(rendered)
#
#
# def render_source_clause(
#     filter_block: dict[str, Any], indent: str = "", database: str | None = None
# ) -> str:
#     """FROM and JOIN lines, schema-qualified consistently."""
#     lines: list[str] = []
#     froms = from_entries(filter_block)
#     if froms:
#         lines.append(f"{indent}FROM {qualify_table_ref(froms[0], database).strip()}")
#         for extra in froms[1:]:
#             lines.append(f"{indent}    , {qualify_table_ref(extra, database).strip()}")
#     for join in join_entries(filter_block):
#         lines.append(f"{indent}{qualify_join_clause(join.strip(), database)}")
#     return "\n".join(lines)
#
# === END FILE: pullmanager/sql.py ===
# === BEGIN FILE: pullmanager/tests/__init__.py SHA256: 4f70b04f739db1fd4fdae89aa3b2dd3ac8afea47a8dc6b8d5eb7fcda8ed717a2 SIZE: 1508 ===
# """Test suite for the Pullmanager runtime.
#
# Uses stdlib unittest so it stays dependency-free and runs unchanged from an
# extracted bundle on the VM:
#
#     python pullmanager.py --tdd            # everything
#     python pullmanager.py --tdd manifest   # one module
# """
#
# from __future__ import annotations
#
# import importlib
# import unittest
# from pathlib import Path
#
#
# def module_names() -> list[str]:
#     """Discover sibling test modules by filename, so nothing needs registering."""
#     return sorted(path.stem for path in Path(__file__).parent.glob("test_*.py"))
#
#
# def build_suite(group: str | None = None) -> unittest.TestSuite:
#     loader = unittest.TestLoader()
#     suite = unittest.TestSuite()
#     available = module_names()
#     if group:
#         name = group if group.startswith("test_") else f"test_{group}"
#         if name not in available:
#             raise LookupError(
#                 f"No test module {name!r}. Available: "
#                 + ", ".join(n.removeprefix("test_") for n in available)
#             )
#         available = [name]
#     for name in available:
#         module = importlib.import_module(f".{name}", package=__name__)
#         suite.addTests(loader.loadTestsFromModule(module))
#     return suite
#
#
# def run(group: str | None = None, verbosity: int = 2) -> int:
#     try:
#         suite = build_suite(group)
#     except LookupError as exc:
#         print(exc)
#         return 1
#     result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
#     return 0 if result.wasSuccessful() else 1
#
# === END FILE: pullmanager/tests/__init__.py ===
# === BEGIN FILE: pullmanager/tests/support.py SHA256: 5c88b19c05ea4b105763db67d73e42d88cbe1c3897b2ece74c9f287b60403398 SIZE: 4541 ===
# """Shared fixture for the runtime tests."""
#
# from __future__ import annotations
#
# import copy
# from pathlib import Path
# from typing import Any
#
# from ..manifest import Manifest
#
#
# def _node(yaml_path: str, **extra: Any) -> dict[str, Any]:
#     node: dict[str, Any] = {
#         "yaml": yaml_path,
#         "status": "pending",
#         "started_at": None,
#         "finished_at": None,
#         "rows": None,
#         "outputs": {},
#         "error": None,
#     }
#     node.update(extra)
#     return node
#
#
# # Shaped exactly like real `makeYaml.py --export-split` output: one generated-PK
# # session with two batch-product runs, one uploaded-PK session with a single run.
# SAMPLE_MANIFEST: dict[str, Any] = {
#     "manifest_version": 1,
#     "project": {
#         "name": "Manager Valid Multipliers",
#         "project_folder": "Manager Valid Multipliers",
#         "project_db": "PROJECTD33A929",
#         "created_by": "yamlmanager",
#     },
#     "source": {
#         "template": "YAMLs/manager_test_cases/02_valid_multipliers_batching.yaml",
#         "recipes": "YAMLs/recipes.yaml",
#     },
#     "sessions": [
#         {
#             "session_id": "UCblackPatients",
#             "cohort": "UCblackPatients",
#             "pk_table": "UCblackPatients",
#             "status": "pending",
#             "phases": {
#                 "setup": _node("sessions/UCblackPatients/setup.yaml"),
#                 "upload_cohorts": _node("sessions/UCblackPatients/upload_cohorts.yaml"),
#                 "pk": _node(
#                     "sessions/UCblackPatients/pk.yaml",
#                     pk_source={"kind": "generated", "table": "UCblackPatients"},
#                 ),
#             },
#             "runs": [
#                 _node(
#                     "sessions/UCblackPatients/runs/LA-Female.yaml",
#                     run_id="UCblackPatients__LA-Female",
#                     batch={
#                         "name": "LA-Female",
#                         "dimensions": [
#                             {
#                                 "name": "state",
#                                 "kind": "column_values",
#                                 "column": "StateOrProvinceAbbreviation",
#                                 "value": "LA",
#                             },
#                             {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Female"},
#                         ],
#                         "runtime": [
#                             {
#                                 "name": "chunk",
#                                 "kind": "row_chunk",
#                                 "rows_per_batch": 2000,
#                                 "applies_to": "PKTable",
#                             }
#                         ],
#                     },
#                 ),
#                 _node(
#                     "sessions/UCblackPatients/runs/LA-Male.yaml",
#                     run_id="UCblackPatients__LA-Male",
#                     batch={
#                         "name": "LA-Male",
#                         "dimensions": [
#                             {
#                                 "name": "state",
#                                 "kind": "column_values",
#                                 "column": "StateOrProvinceAbbreviation",
#                                 "value": "LA",
#                             },
#                             {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Male"},
#                         ],
#                         "runtime": [],
#                     },
#                 ),
#             ],
#             "multiplier": {"session_label": "UCblackPatients"},
#         },
#         {
#             "session_id": "ClientList",
#             "cohort": "ClientList",
#             "pk_table": "ClientPatientList",
#             "status": "pending",
#             "phases": {
#                 "setup": _node("sessions/ClientList/setup.yaml"),
#                 "upload_cohorts": _node("sessions/ClientList/upload_cohorts.yaml"),
#                 "pk": _node(
#                     "sessions/ClientList/pk.yaml",
#                     pk_source={
#                         "kind": "uploaded_cohort",
#                         "upload_name": "ClientPatientList",
#                         "table": "ClientPatientList",
#                         "key_columns": ["PatientDurableKey"],
#                     },
#                 ),
#             },
#             "runs": [_node("sessions/ClientList/runs/run.yaml", run_id="ClientList__run")],
#         },
#     ],
# }
#
#
# def sample_manifest() -> Manifest:
#     return Manifest(copy.deepcopy(SAMPLE_MANIFEST), path=Path("split/pullmanifest.yaml"))
#
# === END FILE: pullmanager/tests/support.py ===
# === BEGIN FILE: pullmanager/tests/test_batches.py SHA256: e9162918f0020a69ea8b94bb61a1d761863d05e752306e28aa0bdbfe914b3719 SIZE: 5122 ===
# """Turning a logical batch into a selection over the local PK table."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..batches import BatchError, count_batch_rows, dimension_predicate, select_batch_rows
#
# PROJECT_DB = "PROJECTD93A5E7"
# KEYS = ["PatientDurableKey"]
#
#
# def batch(dimensions=None, runtime=None, name="B"):
#     return {"name": name, "dimensions": dimensions or [], "runtime": runtime or []}
#
#
# def value_dim(column, value, name="d"):
#     return {"name": name, "kind": "column_values", "column": column, "value": value}
#
#
# class PredicateTests(unittest.TestCase):
#     def test_value_dimension_binds_its_value(self):
#         clause, params = dimension_predicate(value_dim("Sex", "Female"))
#         self.assertEqual(clause, "[Sex] = ?")
#         self.assertEqual(params, ["Female"])
#
#     def test_catch_all_excludes_the_named_values_and_keeps_nulls(self):
#         # NULL is not 'not in' anything in SQL, so without the explicit test the
#         # catch-all would silently drop rows with no value.
#         clause, params = dimension_predicate(
#             {"name": "sex", "column": "Sex", "is_other": True, "excludes": ["Female", "Male"]}
#         )
#         self.assertEqual(clause, "([Sex] NOT IN (?, ?) OR [Sex] IS NULL)")
#         self.assertEqual(params, ["Female", "Male"])
#
#     def test_catch_all_without_exclusions_is_refused(self):
#         # It would otherwise select every row.
#         with self.assertRaises(BatchError):
#             dimension_predicate({"name": "sex", "column": "Sex", "is_other": True})
#
#     def test_dimension_without_a_column_is_refused(self):
#         with self.assertRaises(BatchError):
#             dimension_predicate({"name": "sex", "value": "Female"})
#
#
# class SelectionTests(unittest.TestCase):
#     def test_no_batch_selects_the_whole_pk(self):
#         selection = select_batch_rows(PROJECT_DB, "Patients", None, KEYS)
#         self.assertEqual(selection.sql, "SELECT * FROM PROJECTD93A5E7.dbo.Patients;")
#         self.assertEqual(selection.params, [])
#
#     def test_selects_whole_rows_not_just_keys(self):
#         # Batching selects on PK attributes, and cohort joins may use them.
#         selection = select_batch_rows(
#             PROJECT_DB, "Patients", batch([value_dim("Sex", "Female")]), KEYS
#         )
#         self.assertTrue(selection.sql.startswith("SELECT * FROM"))
#
#     def test_combines_dimensions_with_and(self):
#         selection = select_batch_rows(
#             PROJECT_DB,
#             "Patients",
#             batch([value_dim("StateOrProvinceAbbreviation", "LA"), value_dim("Sex", "Female")]),
#             KEYS,
#         )
#         self.assertIn("[StateOrProvinceAbbreviation] = ?", selection.sql)
#         self.assertIn("AND [Sex] = ?", selection.sql)
#         self.assertEqual(selection.params, ["LA", "Female"])
#
#     def test_chunking_orders_by_the_key(self):
#         selection = select_batch_rows(
#             PROJECT_DB,
#             "Patients",
#             batch(runtime=[{"name": "chunk", "kind": "row_chunk", "rows_per_batch": 2000}]),
#             KEYS,
#             chunk_index=2,
#         )
#         self.assertIn("ORDER BY [PatientDurableKey]", selection.sql)
#         self.assertIn("OFFSET 4000 ROWS FETCH NEXT 2000 ROWS ONLY", selection.sql)
#
#     def test_chunking_needs_key_columns(self):
#         # Without a total order a chunk means different rows each run.
#         with self.assertRaises(BatchError):
#             select_batch_rows(
#                 PROJECT_DB,
#                 "Patients",
#                 batch(runtime=[{"name": "chunk", "kind": "row_chunk", "rows_per_batch": 10}]),
#                 [],
#             )
#
#     def test_values_all_is_refused_with_an_explanation(self):
#         with self.assertRaises(BatchError) as caught:
#             select_batch_rows(
#                 PROJECT_DB,
#                 "Patients",
#                 batch(runtime=[
#                     {"name": "state", "kind": "column_values", "values": "all"},
#                     {"name": "chunk", "kind": "row_chunk", "rows_per_batch": 10},
#                 ]),
#                 KEYS,
#             )
#         self.assertIn("values: all", str(caught.exception))
#
#     def test_bad_chunk_size_is_refused(self):
#         for size in (0, -1, "lots"):
#             with self.subTest(size=size):
#                 with self.assertRaises(BatchError):
#                     select_batch_rows(
#                         PROJECT_DB,
#                         "Patients",
#                         batch(runtime=[{"name": "c", "kind": "row_chunk", "rows_per_batch": size}]),
#                         KEYS,
#                     )
#
#     def test_describes_itself_for_the_manifest(self):
#         selection = select_batch_rows(
#             PROJECT_DB, "Patients", batch([value_dim("Sex", "Female")]), KEYS
#         )
#         self.assertIn("Sex=Female", selection.description)
#
#
# class CountTests(unittest.TestCase):
#     def test_counts_before_chunking(self):
#         selection = count_batch_rows(PROJECT_DB, "Patients", batch([value_dim("Sex", "Male")]))
#         self.assertIn("COUNT_BIG(1)", selection.sql)
#         self.assertNotIn("OFFSET", selection.sql)
#         self.assertEqual(selection.params, ["Male"])
#
# === END FILE: pullmanager/tests/test_batches.py ===
# === BEGIN FILE: pullmanager/tests/test_db.py SHA256: e542b71d46f8493d9d45c1ec2d68d2adc73ebb180551a71e810f9a6ce784905c SIZE: 13107 ===
# """Adapter behaviour, exercised against a fake cursor.
#
# pyodbc is not installed on the development machine and there is no database to
# reach, so the DB-API interactions are faked. What is tested is the logic the
# old generator got right and that is easy to get wrong: GO splitting, draining
# every result set, and keeping server messages on the failure path.
# """
#
# from __future__ import annotations
#
# import unittest
# from pathlib import Path
#
# from ..db import (
#     DEFAULT_DRIVER,
#     find_env_file,
#     load_env_file,
#     parse_env_file,
#     DatabaseError,
#     ResultSet,
#     Settings,
#     bulk_insert,
#     capture_server_name,
#     chunked,
#     collect_messages,
#     drain,
#     execute_script,
#     split_batches,
# )
#
#
# class FakeCursor:
#     """Enough of the DB-API to exercise the drain loop.
#
#     `statements` is one entry per statement in the current batch: None for a
#     statement that returns no rows, or (columns, rows) for one that does.
#     """
#
#     def __init__(self, statements=None, messages=(), fail_on=None):
#         self._template = statements if statements is not None else [None]
#         self._messages = list(messages)
#         self._fail_on = fail_on
#         self.executed: list[str] = []
#         self.executemany_calls: list[tuple[str, list]] = []
#         self.fast_executemany = False
#         self._pending: list = []
#         self._current = None
#
#     @property
#     def messages(self):
#         return self._messages
#
#     def execute(self, sql, params=None):
#         self.executed.append(sql)
#         if self._fail_on is not None and self._fail_on in sql:
#             raise RuntimeError("simulated SQL failure")
#         self._pending = list(self._template)
#         self._advance()
#
#     def executemany(self, sql, seq):
#         self.executemany_calls.append((sql, list(seq)))
#
#     def _advance(self):
#         self._current = self._pending.pop(0) if self._pending else None
#
#     @property
#     def description(self):
#         if self._current is None:
#             return None
#         return [(name,) for name in self._current[0]]
#
#     def fetchall(self):
#         if self._current is None:
#             raise RuntimeError("no result set")
#         return list(self._current[1])
#
#     def fetchone(self):
#         if self._current is None:
#             return None
#         rows = self._current[1]
#         return rows[0] if rows else None
#
#     def nextset(self):
#         if not self._pending:
#             return False
#         self._advance()
#         return True
#
#
# class FakeConnection:
#     def __init__(self, cursor):
#         self._cursor = cursor
#         self.committed = False
#         self.closed = False
#
#     def cursor(self):
#         return self._cursor
#
#     def commit(self):
#         self.committed = True
#
#     def close(self):
#         self.closed = True
#
#
# class SettingsTests(unittest.TestCase):
#     def test_connection_string_matches_the_shape_in_use(self):
#         settings = Settings(cosmos_server="COSMOS", cosmos_database="COSMOS")
#         self.assertEqual(
#             settings.cosmos_connection_string(),
#             "Driver={ODBC Driver 17 for SQL Server};Server=tcp:COSMOS;"
#             "Database=COSMOS;Trusted_Connection=yes;",
#         )
#
#     def test_windows_auth_means_no_credentials_appear(self):
#         rendered = Settings(projects_server="S", projects_database="D").projects_connection_string()
#         self.assertIn("Trusted_Connection=yes", rendered)
#         for secret in ("UID=", "PWD=", "Password"):
#             self.assertNotIn(secret, rendered)
#
#     def test_reads_environment_with_defaults(self):
#         # Both hosts are DNS aliases, so an empty .env still connects.
#         settings = Settings.from_env({})
#         self.assertEqual(settings.driver, DEFAULT_DRIVER)
#         self.assertEqual(settings.cosmos_server, "COSMOS")
#         self.assertEqual(settings.projects_server, "PROJECTS")
#
#     def test_no_configuration_at_all_still_builds_both_strings(self):
#         settings = Settings.from_env({})
#         self.assertIn("Server=tcp:COSMOS;", settings.cosmos_connection_string())
#         self.assertIn(
#             "Server=tcp:PROJECTS;", settings.projects_connection_string("PROJECTD33A929")
#         )
#
#         settings = Settings.from_env({
#             "PULLMANAGER_COSMOS_SERVER": "OTHER",
#             "PULLMANAGER_UPLOAD_CHUNK": "500",
#         })
#         self.assertEqual(settings.cosmos_server, "OTHER")
#         self.assertEqual(settings.upload_chunk, 500)
#
#     def test_nonsense_numbers_fall_back(self):
#         self.assertEqual(Settings.from_env({"PULLMANAGER_UPLOAD_CHUNK": "lots"}).upload_chunk, 20_000)
#
#     def test_an_explicitly_blank_server_or_database_is_refused(self):
#         with self.assertRaises(DatabaseError):
#             Settings(projects_server="", projects_database="D").projects_connection_string()
#         with self.assertRaises(DatabaseError):
#             Settings(projects_server="S", projects_database="").projects_connection_string()
#
#
# class EnvFileTests(unittest.TestCase):
#     def setUp(self):
#         import os
#         import tempfile
#
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.dir = Path(self._tmp.name)
#         self._saved = dict(os.environ)
#         self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))
#
#     def write(self, text):
#         path = self.dir / ".env"
#         path.write_text(text, encoding="utf-8")
#         return path
#
#     def test_parses_the_usual_shapes(self):
#         parsed = parse_env_file(
#             "# a comment\n"
#             "PULLMANAGER_COSMOS_SERVER=COSMOS\n"
#             "export PULLMANAGER_PROJECTS_SERVER=\"PROJ SRV\"\n"
#             "PULLMANAGER_UPLOAD_CHUNK = 5000\n"
#             "\n"
#             "EMPTY=\n"
#             "not-an-assignment\n"
#         )
#         self.assertEqual(parsed["PULLMANAGER_COSMOS_SERVER"], "COSMOS")
#         self.assertEqual(parsed["PULLMANAGER_PROJECTS_SERVER"], "PROJ SRV")
#         self.assertEqual(parsed["PULLMANAGER_UPLOAD_CHUNK"], "5000")
#         self.assertEqual(parsed["EMPTY"], "")
#         self.assertNotIn("not-an-assignment", parsed)
#
#     def test_loads_into_the_environment(self):
#         import os
#
#         os.environ.pop("PULLMANAGER_COSMOS_SERVER", None)
#         path = self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n")
#         load_env_file(path)
#         self.assertEqual(Settings.from_env().cosmos_server, "FROMFILE")
#
#     def test_a_real_environment_variable_wins(self):
#         import os
#
#         os.environ["PULLMANAGER_COSMOS_SERVER"] = "FROMENV"
#         load_env_file(self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n"))
#         self.assertEqual(os.environ["PULLMANAGER_COSMOS_SERVER"], "FROMENV")
#
#     def test_override_lets_the_file_win(self):
#         import os
#
#         os.environ["PULLMANAGER_COSMOS_SERVER"] = "FROMENV"
#         load_env_file(self.write("PULLMANAGER_COSMOS_SERVER=FROMFILE\n"), override=True)
#         self.assertEqual(os.environ["PULLMANAGER_COSMOS_SERVER"], "FROMFILE")
#
#     def test_a_named_file_that_is_missing_is_an_error(self):
#         # Silently ignoring it would surface later as "No server configured".
#         with self.assertRaises(DatabaseError):
#             find_env_file(self.dir / "nope.env")
#
#     def test_no_env_file_anywhere_is_not_an_error(self):
#         self.assertEqual(load_env_file(None), {}) if find_env_file() is None else None
#
#
# class BatchSplitTests(unittest.TestCase):
#     def test_splits_on_go(self):
#         self.assertEqual(split_batches("SELECT 1\nGO\nSELECT 2"), ["SELECT 1", "SELECT 2"])
#
#     def test_go_is_case_insensitive_and_may_be_indented(self):
#         self.assertEqual(len(split_batches("SELECT 1\n  go  \nSELECT 2")), 2)
#
#     def test_go_inside_a_statement_is_not_a_separator(self):
#         self.assertEqual(len(split_batches("SELECT 'GO' AS x")), 1)
#
#     def test_empty_batches_are_dropped(self):
#         self.assertEqual(split_batches("GO\n\nGO\nSELECT 1\nGO\n"), ["SELECT 1"])
#
#     def test_script_without_go_is_one_batch(self):
#         self.assertEqual(len(split_batches("SELECT 1\nSELECT 2")), 1)
#
#
# class DrainTests(unittest.TestCase):
#     def test_collects_every_result_set(self):
#         cursor = FakeCursor([(["A"], [(1,)]), (["B"], [(2,), (3,)])])
#         cursor.execute("x")
#         results = drain(cursor)
#         self.assertEqual([r.columns for r in results], [["A"], ["B"]])
#         self.assertEqual(results[1].rows, [(2,), (3,)])
#
#     def test_skips_statements_that_return_nothing(self):
#         # A generated script interleaves DDL and inserts with telemetry SELECTs.
#         cursor = FakeCursor([None, (["A"], [(1,)]), None])
#         cursor.execute("x")
#         results = drain(cursor)
#         self.assertEqual(len(results), 1)
#         self.assertEqual(results[0].columns, ["A"])
#
#     def test_result_rows_convert_to_dicts(self):
#         self.assertEqual(
#             ResultSet(["A", "B"], [(1, 2)]).as_dicts(), [{"A": 1, "B": 2}]
#         )
#
#
# class ExecuteScriptTests(unittest.TestCase):
#     def test_runs_every_batch(self):
#         cursor = FakeCursor([(["A"], [(1,)])])
#         outcome = execute_script(FakeConnection(cursor), "SELECT 1\nGO\nSELECT 2")
#         self.assertEqual(len(cursor.executed), 2)
#         self.assertEqual(len(outcome.result_sets), 2)
#
#     def test_failure_names_the_batch_and_keeps_server_messages(self):
#         # Without the messages, a failed OPENQUERY reports only a generic outer
#         # error and the actual cause is lost.
#         cursor = FakeCursor(
#             messages=[("42000", "OLE DB provider returned message: Login timeout expired")],
#             fail_on="BOOM",
#         )
#         with self.assertRaises(DatabaseError) as caught:
#             execute_script(FakeConnection(cursor), "SELECT 1\nGO\nBOOM\nGO\nSELECT 3", label="pk")
#         text = str(caught.exception)
#         self.assertIn("batch 2/3", text)
#         self.assertIn("pk", text)
#         self.assertIn("Login timeout expired", text)
#
#     def test_telemetry_is_selected_by_declared_shape(self):
#         cursor = FakeCursor([
#             (["Unrelated"], [("x",)]),
#             (["DestTable", "RowCount"], [("PKTable", 12345)]),
#         ])
#         outcome = execute_script(FakeConnection(cursor), "SELECT 1")
#         self.assertEqual(
#             outcome.rows_of("DestTable", "RowCount"),
#             [{"DestTable": "PKTable", "RowCount": 12345}],
#         )
#
#     def test_messages_are_formatted_as_text(self):
#         cursor = FakeCursor(messages=[("01000", "row count"), ("01000", "done")])
#         self.assertEqual(collect_messages(cursor), ["01000 | row count", "01000 | done"])
#
#
# class ServerNameTests(unittest.TestCase):
#     def test_captures_the_instance_name(self):
#         cursor = FakeCursor([(["CosmosServerName"], [("et4003vpdsql032",)])])
#         self.assertEqual(capture_server_name(FakeConnection(cursor)), "et4003vpdsql032")
#
#     def test_absent_name_is_a_hard_error(self):
#         # Local SQL cannot build OPENQUERY without it, so a silent fallback
#         # would aim the transfer at nothing.
#         with self.assertRaises(DatabaseError):
#             capture_server_name(FakeConnection(FakeCursor([(["x"], [])])))
#
#
# class BulkInsertTests(unittest.TestCase):
#     def rows(self, count):
#         return [(i,) for i in range(count)]
#
#     def test_uses_parameter_binding_not_a_values_list(self):
#         # A table value constructor is capped at 1000 rows; parameter arrays
#         # are not, because the statement stays single-row.
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(5))
#         statement, batch = cursor.executemany_calls[0]
#         self.assertEqual(statement, "INSERT INTO ##JVM_X ([Key]) VALUES (?)")
#         self.assertEqual(len(batch), 5)
#
#     def test_enables_fast_executemany(self):
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(1))
#         self.assertTrue(cursor.fast_executemany)
#
#     def test_chunks_beyond_the_thousand_row_limit(self):
#         cursor = FakeCursor()
#         inserted = bulk_insert(
#             FakeConnection(cursor), "##JVM_X", ["Key"], self.rows(2500), chunk_size=1000
#         )
#         self.assertEqual(inserted, 2500)
#         self.assertEqual([len(b) for _, b in cursor.executemany_calls], [1000, 1000, 500])
#
#     def test_multi_column_placeholders(self):
#         cursor = FakeCursor()
#         bulk_insert(FakeConnection(cursor), "T", ["A", "B", "C"], [(1, 2, 3)])
#         self.assertEqual(cursor.executemany_calls[0][0], "INSERT INTO T ([A], [B], [C]) VALUES (?, ?, ?)")
#
#     def test_no_columns_is_refused(self):
#         with self.assertRaises(DatabaseError):
#             bulk_insert(FakeConnection(FakeCursor()), "T", [], [])
#
#     def test_empty_input_inserts_nothing(self):
#         cursor = FakeCursor()
#         self.assertEqual(bulk_insert(FakeConnection(cursor), "T", ["A"], []), 0)
#         self.assertEqual(cursor.executemany_calls, [])
#
#
# class ChunkTests(unittest.TestCase):
#     def test_splits_into_even_chunks_with_a_remainder(self):
#         self.assertEqual([len(c) for c in chunked(range(7), 3)], [3, 3, 1])
#
#     def test_empty_input_yields_nothing(self):
#         self.assertEqual(list(chunked([], 3)), [])
#
# === END FILE: pullmanager/tests/test_db.py ===
# === BEGIN FILE: pullmanager/tests/test_executor.py SHA256: f1907b950db229ff68fbcb7c455df5dd7e9b735a2b130f71f20ca8b5164ce488 SIZE: 6609 ===
# """Traversal order and resume policy."""
#
# from __future__ import annotations
#
# import shutil
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..executor import (
#     RESUME_FULL,
#     RESUME_PARTIAL,
#     PlanError,
#     excluded_units,
#     iter_units,
#     plan,
#     plan_session,
#     session_cohorts,
#     should_execute,
#     write_sql,
# )
# from ..manifest import Manifest
# from .support import sample_manifest
#
# FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
#
#
# class TraversalTests(unittest.TestCase):
#     def test_phases_come_in_routine_order_then_runs(self):
#         manifest = sample_manifest()
#         kinds = [kind for kind, _, _ in iter_units(manifest, manifest.sessions[0])]
#         self.assertEqual(kinds, ["setup", "upload_cohorts", "pk", "run", "run"])
#
#     def test_runs_keep_manifest_order(self):
#         manifest = sample_manifest()
#         labels = [
#             node.label for kind, node, _ in iter_units(manifest, manifest.sessions[0])
#             if kind == "run"
#         ]
#         self.assertEqual(labels, ["UCblackPatients__LA-Female", "UCblackPatients__LA-Male"])
#
#
# class ShouldExecuteTests(unittest.TestCase):
#     def node(self, status, epoch=None):
#         manifest = sample_manifest()
#         node = manifest.sessions[0].phases[0]
#         node.data["status"] = status
#         if epoch:
#             node.data["epoch"] = epoch
#         return node
#
#     def test_pending_runs(self):
#         run, _ = should_execute(self.node("pending"), "setup")
#         self.assertTrue(run)
#
#     def test_skipped_does_not_run(self):
#         run, why = should_execute(self.node("skipped"), "setup")
#         self.assertFalse(run)
#         self.assertIn("deliberately", why)
#
#     def test_interrupted_running_is_resumed(self):
#         run, why = should_execute(self.node("running"), "setup")
#         self.assertTrue(run)
#         self.assertIn("interrupted", why)
#
#     def test_blocked_is_retried(self):
#         run, _ = should_execute(self.node("blocked"), "setup")
#         self.assertTrue(run)
#
#     def test_failed_needs_an_explicit_retry(self):
#         node = self.node("failed")
#         self.assertFalse(should_execute(node, "run")[0])
#         self.assertTrue(should_execute(node, "run", retry_failed=True)[0])
#
#     def test_done_in_this_session_is_skipped(self):
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "setup", current_epoch="e1")
#         self.assertFalse(run)
#         self.assertIn("already done", why)
#
#     def test_done_under_a_previous_connection_replays(self):
#         # Global temps died with that connection, so the status is true but the
#         # output is gone.
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "setup", current_epoch="e2")
#         self.assertTrue(run)
#         self.assertIn("server state is gone", why)
#
#     def test_partial_resume_keeps_completed_runs(self):
#         # A run's durable result is rows in a Projects table, which survive.
#         node = self.node("done", epoch="e1")
#         run, why = should_execute(node, "run", current_epoch="e2", mode=RESUME_PARTIAL)
#         self.assertFalse(run)
#         self.assertIn("survive", why)
#
#     def test_partial_resume_still_replays_phases(self):
#         node = self.node("done", epoch="e1")
#         run, _ = should_execute(node, "pk", current_epoch="e2", mode=RESUME_PARTIAL)
#         self.assertTrue(run)
#
#     def test_manifest_without_epochs_is_not_treated_as_stale(self):
#         node = self.node("done")
#         self.assertFalse(should_execute(node, "setup", current_epoch="e9")[0])
#
#
# class PlanningTests(unittest.TestCase):
#     def setUp(self):
#         if not FIXTURES.is_dir():
#             self.skipTest(f"fixtures not found at {FIXTURES}")
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.root = Path(self._tmp.name) / "split"
#         shutil.copytree(FIXTURES, self.root)
#         self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
#
#     def test_plans_every_unit_of_a_fresh_manifest(self):
#         units = plan(self.manifest)
#         self.assertEqual(
#             [u.kind for u in units], ["setup", "upload_cohorts", "pk", "run"]
#         )
#
#     def test_setup_creates_a_shell_for_every_destination(self):
#         setup = plan(self.manifest)[0]
#         shells = [b for b in setup.local_blocks if "shell" in b.block_id]
#         self.assertEqual(len(shells), len(session_cohorts(self.manifest, self.manifest.sessions[0])))
#         self.assertTrue(all("CREATE TABLE" in b.sql for b in shells))
#
#     def test_session_cohorts_are_deduplicated(self):
#         names = [c["dest_table"] for c in session_cohorts(self.manifest, self.manifest.sessions[0])]
#         self.assertEqual(len(names), len(set(names)))
#
#     def test_run_units_render_both_sides(self):
#         run = [u for u in plan(self.manifest) if u.kind == "run"][0]
#         self.assertTrue(run.server_blocks)
#         self.assertTrue(run.local_blocks)
#
#     def test_completed_work_is_excluded_and_reported(self):
#         session = self.manifest.sessions[0]
#         session.begin_epoch(linked_server="ls")
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].start()
#         session.runs[0].fail("boom")
#         self.manifest.save()
#
#         reloaded = Manifest.load(self.root / "pullmanifest.yaml")
#         left_out = excluded_units(reloaded)
#         statuses = {label.split("/")[-1]: status for label, status, _ in left_out}
#         self.assertIn("failed", statuses.values())
#
#         units = plan(reloaded)
#         # A new connection means the phases replay even though they are done.
#         self.assertTrue(any(u.kind == "pk" for u in units))
#
#     def test_missing_phase_yaml_is_refused(self):
#         (self.root / "sessions" / "Patients" / "pk.yaml").unlink()
#         with self.assertRaises(PlanError):
#             plan(self.manifest)
#
#     def test_writes_one_file_per_block(self):
#         units = plan(self.manifest)
#         with tempfile.TemporaryDirectory() as out:
#             written = write_sql(units, Path(out))
#             self.assertEqual(len(written), sum(len(u.blocks) for u in units))
#             self.assertTrue(all(p.read_text(encoding="utf-8").strip() for p in written))
#             self.assertTrue(all(p.suffix == ".sql" for p in written))
#
#     def test_plan_is_empty_when_nothing_needs_doing(self):
#         session = self.manifest.sessions[0]
#         for child in session.children:
#             child.skip("not wanted")
#         self.assertEqual(plan_session(self.manifest, session), [])
#
# === END FILE: pullmanager/tests/test_executor.py ===
# === BEGIN FILE: pullmanager/tests/test_manifest.py SHA256: 78f8843188969abfa24793cbd298a3e24ede337d3ebb80a5a3a7c1c365f42ff7 SIZE: 14639 ===
# """Manifest loading, validation, status transitions, and round-tripping."""
#
# from __future__ import annotations
#
# import copy
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..manifest import Manifest, ManifestError
# from ..models import BLOCKED, DONE, FAILED, PENDING, RUNNING, SKIPPED
# from ..yaml_io import dump_yaml
# from .support import SAMPLE_MANIFEST, sample_manifest
#
#
# class TempDirTestCase(unittest.TestCase):
#     def setUp(self):
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.tmp = Path(self._tmp.name)
#         self.manifest_path = self.tmp / "pullmanifest.yaml"
#
#
# class LoadTests(unittest.TestCase):
#     def test_reads_sessions_phases_and_runs(self):
#         manifest = sample_manifest()
#         self.assertEqual(len(manifest.sessions), 2)
#         self.assertEqual(manifest.sessions[0].session_id, "UCblackPatients")
#         self.assertEqual(
#             [phase.name for phase in manifest.sessions[0].phases],
#             ["setup", "upload_cohorts", "pk"],
#         )
#         self.assertEqual(len(manifest.sessions[0].runs), 2)
#
#     def test_source_is_template_and_recipes(self):
#         # The design prose said `source.preyaml`; makeYaml emits template +
#         # recipes. Pin the real contract so drift shows up here.
#         self.assertEqual(sorted(sample_manifest().source), ["recipes", "template"])
#
#     def test_exposes_both_pk_source_kinds(self):
#         manifest = sample_manifest()
#         generated = manifest.sessions[0].phases[2]
#         uploaded = manifest.sessions[1].phases[2]
#         self.assertEqual(generated.pk_source["kind"], "generated")
#         self.assertEqual(uploaded.pk_source["kind"], "uploaded_cohort")
#         self.assertEqual(uploaded.pk_source["key_columns"], ["PatientDurableKey"])
#
#     def test_resolves_yaml_paths_against_manifest_directory(self):
#         manifest = sample_manifest()
#         setup = manifest.sessions[0].phases[0]
#         self.assertEqual(manifest.resolve(setup), Path("split/sessions/UCblackPatients/setup.yaml"))
#
#
# class ValidationTests(unittest.TestCase):
#     def assertRejects(self, data, fragment):
#         with self.assertRaises(ManifestError) as caught:
#             Manifest(data)
#         self.assertIn(fragment.lower(), str(caught.exception).lower())
#
#     def test_rejects_unsupported_version(self):
#         self.assertRejects({"manifest_version": 99, "sessions": []}, "manifest_version")
#
#     def test_rejects_missing_sessions_list(self):
#         self.assertRejects({"manifest_version": 1}, "sessions")
#
#     def test_rejects_non_mapping_root(self):
#         with self.assertRaises(ManifestError):
#             Manifest(["not", "a", "mapping"])
#
#     def test_rejects_unknown_phase_name(self):
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {"teardown": {"yaml": "x.yaml", "status": "pending"}},
#                         "runs": [],
#                     }
#                 ],
#             },
#             "unknown phase",
#         )
#
#     def test_rejects_duplicate_run_id(self):
#         run = {"run_id": "R", "yaml": "r.yaml", "status": "pending"}
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {},
#                         "runs": [dict(run), dict(run)],
#                     }
#                 ],
#             },
#             "duplicate run_id",
#         )
#
#     def test_rejects_duplicate_session_id(self):
#         session = {"session_id": "S", "status": "pending", "phases": {}, "runs": []}
#         self.assertRejects(
#             {"manifest_version": 1, "sessions": [dict(session), dict(session)]},
#             "duplicate session_id",
#         )
#
#     def test_rejects_phase_without_yaml_path(self):
#         self.assertRejects(
#             {
#                 "manifest_version": 1,
#                 "sessions": [
#                     {
#                         "session_id": "S",
#                         "status": "pending",
#                         "phases": {"setup": {"status": "pending"}},
#                         "runs": [],
#                     }
#                 ],
#             },
#             "missing its `yaml`",
#         )
#
#     def test_rejects_unknown_status_value(self):
#         data = copy.deepcopy(SAMPLE_MANIFEST)
#         data["sessions"][0]["phases"]["setup"]["status"] = "finished"
#         with self.assertRaises(Exception):
#             Manifest(data)
#
#
# class TransitionTests(unittest.TestCase):
#     def test_start_marks_running_and_stamps_start(self):
#         phase = sample_manifest().sessions[0].phases[0]
#         phase.start()
#         self.assertEqual(phase.status, RUNNING)
#         self.assertIsNotNone(phase.data["started_at"])
#         self.assertIsNone(phase.data["finished_at"])
#
#     def test_finish_records_rows_outputs_and_duration(self):
#         phase = sample_manifest().sessions[0].phases[2]
#         phase.start()
#         phase.finish(rows=12345, outputs={"global_temp": "##JVM_UCblackPatients"})
#         self.assertEqual(phase.status, DONE)
#         self.assertEqual(phase.rows, 12345)
#         self.assertEqual(phase.outputs["global_temp"], "##JVM_UCblackPatients")
#         self.assertIsNotNone(phase.data["finished_at"])
#         self.assertGreaterEqual(phase.data["duration"]["seconds"], 0)
#
#     def test_fail_records_message_and_detail(self):
#         run = sample_manifest().sessions[0].runs[0]
#         run.start()
#         run.fail("OPENQUERY failed", detail="Login timeout expired")
#         self.assertEqual(run.status, FAILED)
#         self.assertEqual(run.error["message"], "OPENQUERY failed")
#         self.assertEqual(run.error["detail"], "Login timeout expired")
#
#     def test_retry_clears_previous_error_and_duration(self):
#         run = sample_manifest().sessions[0].runs[0]
#         run.start()
#         run.fail("boom")
#         run.start()
#         self.assertEqual(run.status, RUNNING)
#         self.assertIsNone(run.error)
#         self.assertNotIn("duration", run.data)
#
#     def test_skip_and_block_use_note_not_error(self):
#         # A skipped phase is not a failure; an `error` block would read like one.
#         for action, expected in (("skip", SKIPPED), ("block", BLOCKED)):
#             with self.subTest(action=action):
#                 run = sample_manifest().sessions[0].runs[1]
#                 getattr(run, action)("upstream PK failed")
#                 self.assertEqual(run.status, expected)
#                 self.assertEqual(run.note, "upstream PK failed")
#                 self.assertIsNone(run.error)
#
#
# class SessionRollupTests(unittest.TestCase):
#     def test_pending_while_untouched(self):
#         self.assertEqual(sample_manifest().sessions[0].recompute_status(), PENDING)
#
#     def test_running_when_partially_complete(self):
#         session = sample_manifest().sessions[0]
#         session.phases[0].start()
#         session.phases[0].finish()
#         self.assertEqual(session.recompute_status(), RUNNING)
#
#     def test_failure_outranks_every_other_state(self):
#         session = sample_manifest().sessions[0]
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].start()
#         session.runs[0].fail("nope")
#         session.runs[1].block("upstream failed")
#         self.assertEqual(session.recompute_status(), FAILED)
#
#     def test_done_when_every_child_is_done(self):
#         session = sample_manifest().sessions[1]
#         for child in session.children:
#             child.start()
#             child.finish()
#         self.assertEqual(session.recompute_status(), DONE)
#
#     def test_done_when_children_mix_done_and_skipped(self):
#         session = sample_manifest().sessions[1]
#         session.phases[0].start()
#         session.phases[0].finish()
#         session.phases[1].skip("no upload cohorts")
#         session.phases[2].start()
#         session.phases[2].finish()
#         session.runs[0].start()
#         session.runs[0].finish()
#         self.assertEqual(session.recompute_status(), DONE)
#
#     def test_skipped_only_when_everything_skipped(self):
#         session = sample_manifest().sessions[1]
#         for child in session.children:
#             child.skip()
#         self.assertEqual(session.recompute_status(), SKIPPED)
#
#     def test_blocked_when_blocking_is_the_worst_state(self):
#         session = sample_manifest().sessions[1]
#         for phase in session.phases:
#             phase.start()
#             phase.finish()
#         session.runs[0].block("upstream failed")
#         self.assertEqual(session.recompute_status(), BLOCKED)
#
#
# class EpochTests(unittest.TestCase):
#     """Global temps die with the connection; the epoch is how we know."""
#
#     def test_begin_epoch_records_connection_facts(self):
#         session = sample_manifest().sessions[0]
#         epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
#         self.assertEqual(session.epoch, epoch)
#         self.assertEqual(session.runtime["linked_server"], "et4003vpdsq1032")
#         self.assertIsNotNone(session.runtime["opened_at"])
#
#     def test_new_epoch_clears_a_stale_linked_server(self):
#         # The Cosmos instance name changes every connection, so a value from a
#         # previous epoch must never survive into the next one.
#         session = sample_manifest().sessions[0]
#         session.begin_epoch(linked_server="et4003vpdsql032")
#         session.begin_epoch()
#         self.assertIsNone(session.runtime["linked_server"])
#
#     def test_each_epoch_is_distinct(self):
#         session = sample_manifest().sessions[0]
#         self.assertNotEqual(session.begin_epoch(), session.begin_epoch())
#
#     def test_finishing_stamps_the_current_epoch(self):
#         session = sample_manifest().sessions[0]
#         epoch = session.begin_epoch()
#         phase = session.phases[2]
#         phase.start()
#         phase.finish(rows=12345)
#         self.assertEqual(phase.epoch, epoch)
#
#     def test_work_from_the_current_epoch_is_not_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         phase = session.phases[2]
#         phase.start()
#         phase.finish()
#         self.assertFalse(phase.is_stale(session.epoch))
#
#     def test_work_from_a_previous_epoch_is_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         for child in session.children:
#             child.start()
#             child.finish()
#
#         # Restarting the process opens a new connection; the old temps are gone.
#         session.begin_epoch()
#         self.assertEqual(len(session.stale_children()), len(session.children))
#         self.assertTrue(session.phases[2].is_stale(session.epoch))
#
#     def test_unfinished_work_is_never_stale(self):
#         session = sample_manifest().sessions[0]
#         session.begin_epoch()
#         self.assertFalse(session.phases[0].is_stale(session.epoch))
#         session.runs[0].block("upstream failed")
#         self.assertFalse(session.runs[0].is_stale(session.epoch))
#
#     def test_manifest_without_epochs_is_never_stale(self):
#         # Manifests written before epochs existed must not be read as stale.
#         session = sample_manifest().sessions[0]
#         phase = session.phases[0]
#         phase.start()
#         phase.finish()
#         self.assertIsNone(phase.epoch)
#         self.assertFalse(phase.is_stale("some-new-epoch"))
#
#
# class RoundTripTests(TempDirTestCase):
#     def saved_manifest(self) -> Manifest:
#         manifest = sample_manifest()
#         manifest.path = self.manifest_path
#         return manifest
#
#     def test_status_and_rows_survive_save_and_reload(self):
#         manifest = self.saved_manifest()
#         manifest.sessions[0].phases[0].start()
#         manifest.sessions[0].phases[0].finish(rows=42)
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path)
#         setup = reloaded.sessions[0].phases[0]
#         self.assertEqual(setup.status, DONE)
#         self.assertEqual(setup.rows, 42)
#         self.assertEqual(reloaded.sessions[0].status, RUNNING)
#
#     def test_unknown_keys_survive(self):
#         manifest = self.saved_manifest()
#         manifest.data["future_field"] = {"added_by": "a later yamlmanager"}
#         manifest.sessions[0].runs[0].data["parquet_hint"] = "keep me"
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path)
#         self.assertEqual(reloaded.data["future_field"], {"added_by": "a later yamlmanager"})
#         self.assertEqual(reloaded.sessions[0].runs[0].data["parquet_hint"], "keep me")
#
#     def test_batch_product_and_multiplier_context_survive(self):
#         manifest = self.saved_manifest()
#         manifest.save()
#
#         run = Manifest.load(self.manifest_path).sessions[0].runs[0]
#         self.assertEqual(run.batch["name"], "LA-Female")
#         self.assertEqual(
#             [dim["value"] for dim in run.batch["dimensions"]],
#             ["LA", "Female"],
#         )
#         self.assertEqual([dim["name"] for dim in run.batch["runtime"]], ["chunk"])
#
#     def test_save_leaves_no_temp_file_behind(self):
#         self.saved_manifest().save()
#         self.assertEqual(
#             sorted(path.name for path in self.tmp.iterdir()),
#             ["pullmanifest.yaml"],
#         )
#
#     def test_epoch_survives_save_and_reload(self):
#         manifest = self.saved_manifest()
#         session = manifest.sessions[0]
#         epoch = session.begin_epoch(linked_server="et4003vpdsq1032")
#         session.phases[0].start()
#         session.phases[0].finish()
#         manifest.save()
#
#         reloaded = Manifest.load(self.manifest_path).sessions[0]
#         self.assertEqual(reloaded.epoch, epoch)
#         self.assertEqual(reloaded.runtime["linked_server"], "et4003vpdsq1032")
#         self.assertEqual(reloaded.phases[0].epoch, epoch)
#         self.assertFalse(reloaded.phases[0].is_stale(epoch))
#         self.assertTrue(reloaded.phases[0].is_stale("a-later-epoch"))
#
#     def test_load_rejects_missing_file(self):
#         with self.assertRaises(ManifestError):
#             Manifest.load(self.tmp / "nope.yaml")
#
#     def test_bare_yaml_nulls_load_as_none(self):
#         # makeYaml writes empty values as bare `key:`; they must come back as
#         # None rather than the string "None".
#         dump_yaml(SAMPLE_MANIFEST, self.manifest_path)
#         self.assertIn("started_at:", self.manifest_path.read_text(encoding="utf-8"))
#         reloaded = Manifest.load(self.manifest_path)
#         self.assertIsNone(reloaded.sessions[0].phases[0].data["started_at"])
#
# === END FILE: pullmanager/tests/test_manifest.py ===
# === BEGIN FILE: pullmanager/tests/test_models.py SHA256: 9293f87dcebe251868ceb465460b0067c9642790941c6e26250a6cb8d47044a8 SIZE: 2221 ===
# """Duration formatting and the status vocabulary."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..models import (
#     ALL_STATUSES,
#     SETTLED_STATUSES,
#     DONE,
#     SKIPPED,
#     StatusError,
#     duration_block,
#     format_duration,
#     validate_status,
# )
#
#
# class FormatDurationTests(unittest.TestCase):
#     def test_renders_expected_shapes(self):
#         cases = [
#             (0, "0s"),
#             (45, "45s"),
#             (60, "1m 0s"),
#             (132, "2m 12s"),
#             (332, "5m 32s"),
#             (3600, "1h 0m 0s"),
#             (3723, "1h 2m 3s"),
#         ]
#         for seconds, expected in cases:
#             with self.subTest(seconds=seconds):
#                 self.assertEqual(format_duration(seconds), expected)
#
#     def test_rounds_fractional_seconds(self):
#         self.assertEqual(format_duration(45.4), "45s")
#         self.assertEqual(format_duration(45.6), "46s")
#
#
# class DurationBlockTests(unittest.TestCase):
#     def test_builds_block_from_timestamps(self):
#         self.assertEqual(
#             duration_block("2026-09-22T14:03:00-05:00", "2026-09-22T14:08:32-05:00"),
#             {"seconds": 332, "display": "5m 32s"},
#         )
#
#     def test_handles_offset_differences(self):
#         # Same instant expressed in two zones is a zero-length duration.
#         self.assertEqual(
#             duration_block("2026-09-22T14:00:00-05:00", "2026-09-22T15:00:00-04:00"),
#             {"seconds": 0, "display": "0s"},
#         )
#
#     def test_returns_none_without_both_timestamps(self):
#         self.assertIsNone(duration_block(None, "2026-09-22T14:08:32-05:00"))
#         self.assertIsNone(duration_block("2026-09-22T14:03:00-05:00", None))
#         self.assertIsNone(duration_block(None, None))
#
#
# class StatusTests(unittest.TestCase):
#     def test_accepts_every_known_status(self):
#         for status in ALL_STATUSES:
#             with self.subTest(status=status):
#                 self.assertEqual(validate_status(status), status)
#
#     def test_rejects_unknown_status(self):
#         with self.assertRaises(StatusError):
#             validate_status("finished")
#
#     def test_settled_statuses_are_the_ones_resume_skips(self):
#         self.assertEqual(set(SETTLED_STATUSES), {DONE, SKIPPED})
#
# === END FILE: pullmanager/tests/test_models.py ===
# === BEGIN FILE: pullmanager/tests/test_naming.py SHA256: ec0c27d0b9eb3b614cb89acdf84d93b6bf8756d1b33d3fada9f65468251ea03e SIZE: 6276 ===
# """Naming invariants: server, staging and destination must agree."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..naming import (
#     NamingError,
#     base_name,
#     destination,
#     global_temp,
#     is_schema_qualified,
#     is_temp_table,
#     local_staging,
#     qualify,
#     qualify_join_clause,
#     qualify_table_ref,
# )
#
#
# class GlobalTempTests(unittest.TestCase):
#     def test_prefixes_a_plain_name(self):
#         self.assertEqual(global_temp("PKTable"), "##JVM_PKTable")
#
#     def test_never_doubles_the_jvm_prefix(self):
#         # The old generator's ##JVM_JVM_Foo bug.
#         for given in ("JVM_PKTable", "jvm_PKTable", "##JVM_PKTable"):
#             with self.subTest(given=given):
#                 self.assertEqual(global_temp(given), "##JVM_PKTable")
#
#     def test_is_idempotent(self):
#         once = global_temp("PKTable")
#         self.assertEqual(global_temp(once), once)
#
#
# class LocalStagingTests(unittest.TestCase):
#     def test_prefixes_a_plain_name(self):
#         self.assertEqual(local_staging("PKTable"), "#Local_PKTable")
#
#     def test_is_idempotent(self):
#         self.assertEqual(local_staging("#Local_PKTable"), "#Local_PKTable")
#
#     def test_derives_from_a_global_temp_name(self):
#         self.assertEqual(local_staging("##JVM_PKTable"), "#Local_PKTable")
#
#
# class DestinationTests(unittest.TestCase):
#     def test_is_always_fully_qualified(self):
#         self.assertEqual(
#             destination("PROJECTD93A5E7", "PKTable"),
#             "PROJECTD93A5E7.dbo.PKTable",
#         )
#
#     def test_strips_generator_prefixes(self):
#         self.assertEqual(
#             destination("PROJECTD93A5E7", "##JVM_PKTable"),
#             "PROJECTD93A5E7.dbo.PKTable",
#         )
#
#     def test_requires_a_project_db(self):
#         with self.assertRaises(NamingError):
#             destination("", "PKTable")
#
#
# class MissingNameTests(unittest.TestCase):
#     def test_blank_dest_table_is_rejected(self):
#         for given in (None, "", "   "):
#             with self.subTest(given=given):
#                 for fn in (base_name, global_temp, local_staging):
#                     with self.assertRaises(NamingError):
#                         fn(given)
#
#
# class PredicateTests(unittest.TestCase):
#     def test_identifies_temp_tables(self):
#         self.assertTrue(is_temp_table("#Local_X"))
#         self.assertTrue(is_temp_table("##JVM_X"))
#         self.assertFalse(is_temp_table("PatientDim"))
#
#     def test_dots_inside_brackets_do_not_qualify(self):
#         self.assertTrue(is_schema_qualified("dbo.PatientDim"))
#         self.assertFalse(is_schema_qualified("[My.Table]"))
#         self.assertTrue(is_schema_qualified("[dbo].[PatientDim]"))
#
#
# class QualifyTests(unittest.TestCase):
#     def test_adds_the_default_schema(self):
#         self.assertEqual(qualify("PatientDim"), "dbo.PatientDim")
#         self.assertEqual(qualify("[PatientDim]"), "dbo.[PatientDim]")
#
#     def test_never_produces_dbo_dbo(self):
#         for given in ("dbo.PatientDim", "[dbo].[PatientDim]", "COSMOS.dbo.PatientDim"):
#             with self.subTest(given=given):
#                 self.assertEqual(qualify(given), given)
#
#     def test_leaves_temp_tables_unqualified(self):
#         # Global temps live in tempdb; qualifying them would break the reference.
#         self.assertEqual(qualify("##JVM_PKTable2"), "##JVM_PKTable2")
#         self.assertEqual(qualify("#Local_PKTable"), "#Local_PKTable")
#
#
# class QualifyWithDatabaseTests(unittest.TestCase):
#     """Three-part names, for a cohort reading a database it is not connected to."""
#
#     def test_adds_the_database_when_given(self):
#         self.assertEqual(
#             qualify("PatientDim", "COSMOS_SneakPeek"),
#             "COSMOS_SneakPeek.dbo.PatientDim",
#         )
#
#     def test_already_qualified_names_are_left_alone(self):
#         self.assertEqual(
#             qualify("dbo.PatientDim", "COSMOS_SneakPeek"), "dbo.PatientDim"
#         )
#
#     def test_temp_tables_are_never_database_qualified(self):
#         # Global temps live in tempdb regardless of the connected database.
#         self.assertEqual(qualify("##JVM_PKTable", "COSMOS_SneakPeek"), "##JVM_PKTable")
#
#     def test_joins_take_the_database_too(self):
#         self.assertEqual(
#             qualify_join_clause("INNER JOIN EncounterFact AS e ON 1 = 1", "COSMOS_SneakPeek"),
#             "INNER JOIN COSMOS_SneakPeek.dbo.EncounterFact AS e ON 1 = 1",
#         )
#
#
# class QualifyTableRefTests(unittest.TestCase):
#     def test_qualifies_a_from_entry_keeping_the_alias(self):
#         self.assertEqual(qualify_table_ref("PatientDim AS p"), "dbo.PatientDim AS p")
#
#     def test_is_idempotent(self):
#         once = qualify_table_ref("PatientDim AS p")
#         self.assertEqual(qualify_table_ref(once), once)
#
#     def test_leaves_generated_temps_alone(self):
#         self.assertEqual(
#             qualify_table_ref("##JVM_PKTable2 AS p"), "##JVM_PKTable2 AS p"
#         )
#
#
# class QualifyJoinClauseTests(unittest.TestCase):
#     def test_qualifies_the_joined_table(self):
#         self.assertEqual(
#             qualify_join_clause(
#                 "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey"
#             ),
#             "INNER JOIN dbo.DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#         )
#
#     def test_handles_every_join_flavour(self):
#         for kind in ("INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "CROSS JOIN", "join"):
#             with self.subTest(kind=kind):
#                 self.assertIn(
#                     "dbo.EncounterFact",
#                     qualify_join_clause(f"{kind} EncounterFact AS e ON 1 = 1"),
#                 )
#
#     def test_leaves_generated_temps_alone(self):
#         clause = "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey"
#         self.assertEqual(qualify_join_clause(clause), clause)
#
#     def test_does_not_touch_the_on_predicate(self):
#         # Column references are aliases, not tables, and must be left alone.
#         clause = "INNER JOIN Foo AS f ON f.Bar = baz.Qux"
#         self.assertEqual(
#             qualify_join_clause(clause),
#             "INNER JOIN dbo.Foo AS f ON f.Bar = baz.Qux",
#         )
#
#     def test_is_idempotent(self):
#         once = qualify_join_clause("INNER JOIN Foo AS f ON 1 = 1")
#         self.assertEqual(qualify_join_clause(once), once)
#
# === END FILE: pullmanager/tests/test_naming.py ===
# === BEGIN FILE: pullmanager/tests/test_normalize.py SHA256: acf17645cde65424d49e02ecda95c2d65df2fc55943283c0a50f729fe7683814 SIZE: 7257 ===
# """Compatibility rules for hand-authored cohort YAML."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..normalize import (
#     NormalizationError,
#     cosmos_database,
#     is_dual,
#     dead_options,
#     joined_generated_tables,
#     normalize_bool,
#     normalize_dedup_keys,
#     root_pk_cohort,
#     root_pk_cohorts,
#     validate_dedup_columns,
# )
#
# # The chained PK from inputSimple.yaml: a patient list, then diagnosis events
# # for those patients.
# PATIENTS = {
#     "name": "PKTable",
#     "type": "PK",
#     "dest_table": "PKTable2",
#     "columns": [{"name": "PatientDurableKey"}],
#     "filter": {"from": "PatientDim AS p", "join": []},
# }
# EVENTS = {
#     "name": "PKTable",
#     "type": "PK",
#     "dest_table": "PKTable",
#     "dedup_key": ["DiagnosisEventKey"],
#     "columns": [{"name": "DiagnosisEventKey"}, {"name": "PatientDurableKey"}],
#     "filter": {
#         "from": "DiagnosisEventFact AS def",
#         "join": [
#             "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#             "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
#         ],
#     },
# }
# FACT = {"name": "OtherDx", "type": "fact", "dest_table": "OtherDx", "filter": {}}
#
#
# class CosmosDatabaseTests(unittest.TestCase):
#     def test_maps_every_accepted_spelling(self):
#         cases = [
#             ("COSMOS", "COSMOS"),
#             ("cosmos", "COSMOS"),
#             ("COSMOS_SneakPeek", "COSMOS_SneakPeek"),
#             ("sneakpeek", "COSMOS_SneakPeek"),
#             ("sp", "COSMOS_SneakPeek"),
#         ]
#         for given, expected in cases:
#             with self.subTest(given=given):
#                 self.assertEqual(cosmos_database(given), expected)
#
#     def test_dual_is_a_directive_not_a_database(self):
#         # Connecting with Database=Dual would simply fail; under Dual the
#         # SneakPeek cohorts qualify their own tables instead.
#         for given in ("Dual", "both", "BOTH"):
#             with self.subTest(given=given):
#                 self.assertEqual(cosmos_database(given), "COSMOS")
#                 self.assertTrue(is_dual(given))
#
#     def test_single_database_modes_are_not_dual(self):
#         for given in ("COSMOS", "sp", None):
#             with self.subTest(given=given):
#                 self.assertFalse(is_dual(given))
#
#     def test_absent_falls_back(self):
#         self.assertEqual(cosmos_database(None), "COSMOS")
#         self.assertEqual(cosmos_database("  "), "COSMOS")
#
#     def test_unknown_value_is_refused(self):
#         with self.assertRaises(NormalizationError):
#             cosmos_database("Mars")
#
#
# class BooleanTests(unittest.TestCase):
#     def test_accepts_truthy_spellings(self):
#         for given in (True, 1, "true", "True", "YES", "y", "1", "on", "t"):
#             with self.subTest(given=given):
#                 self.assertTrue(normalize_bool(given))
#
#     def test_accepts_falsy_spellings(self):
#         for given in (False, 0, "false", "No", "n", "0", "off", ""):
#             with self.subTest(given=given):
#                 self.assertFalse(normalize_bool(given))
#
#     def test_none_takes_the_default(self):
#         self.assertFalse(normalize_bool(None))
#         self.assertTrue(normalize_bool(None, default=True))
#
#     def test_rejects_nonsense(self):
#         with self.assertRaises(NormalizationError):
#             normalize_bool("maybe")
#
#
# class DedupKeyTests(unittest.TestCase):
#     def test_legacy_singular_is_accepted_with_a_note(self):
#         # The old generator accepted only `dedup_keys` and silently skipped
#         # deduplication entirely, changing row counts with no warning.
#         keys, notes = normalize_dedup_keys(EVENTS)
#         self.assertEqual(keys, [["DiagnosisEventKey"]])
#         self.assertEqual(len(notes), 1)
#         self.assertIn("dedup_key", notes[0])
#
#     def test_canonical_form_produces_no_note(self):
#         keys, notes = normalize_dedup_keys({"dedup_keys": [["A", "B"], ["C"]]})
#         self.assertEqual(keys, [["A", "B"], ["C"]])
#         self.assertEqual(notes, [])
#
#     def test_flat_list_is_one_key_set(self):
#         keys, _ = normalize_dedup_keys({"dedup_keys": ["A", "B"]})
#         self.assertEqual(keys, [["A", "B"]])
#
#     def test_bare_string_is_one_key_set(self):
#         keys, _ = normalize_dedup_keys({"dedup_keys": "A"})
#         self.assertEqual(keys, [["A"]])
#
#     def test_absent_means_no_dedup(self):
#         keys, notes = normalize_dedup_keys({"dest_table": "X"})
#         self.assertEqual(keys, [])
#         self.assertEqual(notes, [])
#
#     def test_both_spellings_at_once_is_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_key": ["A"], "dedup_keys": [["A"]]})
#
#     def test_empty_is_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": []})
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": [[]]})
#
#     def test_mixed_shapes_are_an_error(self):
#         with self.assertRaises(NormalizationError):
#             normalize_dedup_keys({"dedup_keys": ["A", ["B"]]})
#
#
# class DedupColumnTests(unittest.TestCase):
#     def test_keys_must_name_produced_columns(self):
#         self.assertEqual(validate_dedup_columns([["DiagnosisEventKey"]], EVENTS), [])
#
#     def test_unknown_column_is_reported(self):
#         problems = validate_dedup_columns([["Nope"]], EVENTS)
#         self.assertEqual(len(problems), 1)
#         self.assertIn("Nope", problems[0])
#
#
# class DeadOptionTests(unittest.TestCase):
#     def test_retired_options_are_named(self):
#         notes = dead_options({"printout_md": True, "stop_at_for_pk_table": 500})
#         self.assertEqual(len(notes), 1)
#         self.assertIn("printout_md", notes[0])
#
#     def test_live_options_are_silent(self):
#         self.assertEqual(dead_options({"stop_at_for_pk_table": 500}), [])
#         self.assertEqual(dead_options(None), [])
#
#
# class DependencyTests(unittest.TestCase):
#     def test_finds_joined_global_temps(self):
#         self.assertEqual(joined_generated_tables(EVENTS), {"##JVM_PKTABLE2"})
#
#     def test_reports_none_for_an_independent_cohort(self):
#         self.assertEqual(joined_generated_tables(PATIENTS), set())
#
#
# class RootPkTests(unittest.TestCase):
#     def test_chained_pks_have_one_root(self):
#         # Row limits apply here only; limiting the downstream PK too would
#         # compound 500 patients x 500 events into an unrepresentative sample.
#         root = root_pk_cohort([PATIENTS, EVENTS, FACT])
#         self.assertEqual(root["dest_table"], "PKTable2")
#
#     def test_order_does_not_matter(self):
#         root = root_pk_cohort([EVENTS, FACT, PATIENTS])
#         self.assertEqual(root["dest_table"], "PKTable2")
#
#     def test_single_pk_is_its_own_root(self):
#         self.assertEqual(root_pk_cohort([PATIENTS, FACT])["dest_table"], "PKTable2")
#
#     def test_no_pk_cohorts_gives_none(self):
#         self.assertIsNone(root_pk_cohort([FACT]))
#
#     def test_fact_cohorts_are_never_roots(self):
#         self.assertEqual(len(root_pk_cohorts([PATIENTS, EVENTS, FACT])), 1)
#
#     def test_ambiguous_roots_are_an_error(self):
#         other = dict(PATIENTS, dest_table="OtherPK", name="OtherPK")
#         with self.assertRaises(NormalizationError):
#             root_pk_cohort([PATIENTS, other])
#
# === END FILE: pullmanager/tests/test_normalize.py ===
# === BEGIN FILE: pullmanager/tests/test_render.py SHA256: 51b6f73d1d00c14d116353ae1a05df7e2be1307dfc2e7f51ba12706eed2f8c3f SIZE: 10654 ===
# """Server and local SQL rendering, checked against the real fixtures."""
#
# from __future__ import annotations
#
# import unittest
# from pathlib import Path
#
# from .. import local_sql, server_sql
# from ..local_sql import LocalRenderError
# from ..server_sql import RenderError
#
# FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
#
#
# def pk_cohort(**overrides):
#     cohort = {
#         "name": "Patients",
#         "type": "PK",
#         "dest_table": "PKTable2",
#         "columns": [{"source": "p.DurableKey", "name": "PatientDurableKey",
#                      "type": "BIGINT", "nullable": False}],
#         "filter": {"from": "PatientDim AS p", "join": [], "where": ["p._IsDeleted = 0"]},
#     }
#     cohort.update(overrides)
#     return cohort
#
#
# def doc_with(*cohorts, **extra):
#     doc = {"project_db": "PROJECTD93A5E7", "cosmos_db": "COSMOS", "cohorts": list(cohorts)}
#     doc.update(extra)
#     return doc
#
#
# class ServerRenderTests(unittest.TestCase):
#     def render(self, doc, prefix="S/pk"):
#         return server_sql.render_phase(doc, prefix)
#
#     def test_creates_and_populates_the_global_temp(self):
#         blocks, _ = self.render(doc_with(pk_cohort()))
#         sql = blocks[0].sql
#         self.assertIn("DROP TABLE IF EXISTS ##JVM_PKTable2;", sql)
#         self.assertIn("CREATE TABLE ##JVM_PKTable2", sql)
#         self.assertIn("INSERT INTO ##JVM_PKTable2", sql)
#         self.assertEqual(blocks[0].block_id, "S/pk/PKTable2")
#         self.assertEqual(blocks[0].meta["global_temp"], "##JVM_PKTable2")
#
#     def test_adds_non_null_filters(self):
#         blocks, _ = self.render(doc_with(pk_cohort()))
#         self.assertIn("AND p.DurableKey IS NOT NULL", blocks[0].sql)
#
#     def test_top_applies_only_to_the_root_pk(self):
#         # A downstream PK joins the root's temp; limiting it too would compound
#         # the restriction into an unrepresentative sample.
#         downstream = pk_cohort(
#             dest_table="PKTable",
#             columns=[{"source": "d.Key", "name": "Key", "type": "BIGINT", "nullable": False}],
#             filter={"from": "DiagnosisEventFact AS d",
#                     "join": ["INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = d.PatientDurableKey"]},
#         )
#         doc = doc_with(pk_cohort(), downstream,
#                        test_options={"smallset": True, "stop_at_for_pk_table": 500})
#         blocks, _ = self.render(doc)
#         by_dest = {b.dest_table: b.sql for b in blocks}
#         self.assertIn("TOP (500)", by_dest["PKTable2"])
#         self.assertNotIn("TOP (", by_dest["PKTable"])
#
#     def test_no_top_without_smallset(self):
#         doc = doc_with(pk_cohort(), test_options={"stop_at_for_pk_table": 500})
#         blocks, _ = self.render(doc)
#         self.assertNotIn("TOP (", blocks[0].sql)
#
#     def test_dedup_renders_and_is_visible(self):
#         # The old generator accepted only `dedup_keys` and silently emitted no
#         # deduplication at all.
#         cohort = pk_cohort(dedup_key=["PatientDurableKey"])
#         blocks, notes = self.render(doc_with(cohort))
#         self.assertIn("ROW_NUMBER() OVER (PARTITION BY [PatientDurableKey]", blocks[0].sql)
#         self.assertIn("[_dedup_rn] = 1", blocks[0].sql)
#         self.assertTrue(any("legacy" in n for n in notes))
#         self.assertTrue(any("arbitrary but stable" in n for n in notes))
#
#     def test_dedup_key_naming_a_missing_column_is_refused(self):
#         with self.assertRaises(RenderError):
#             self.render(doc_with(pk_cohort(dedup_keys=[["NoSuchColumn"]])))
#
#     def test_unsubstituted_placeholder_is_refused(self):
#         cohort = pk_cohort(filter={"from": "PatientDim AS p",
#                                    "where": ["p.StartDateKey > {{min_date_key}}"]})
#         with self.assertRaises(RenderError) as caught:
#             self.render(doc_with(cohort))
#         self.assertIn("min_date_key", str(caught.exception))
#
#     def test_duplicate_column_names_are_refused(self):
#         cohort = pk_cohort(columns=[
#             {"source": "p.A", "name": "Dup", "type": "BIGINT"},
#             {"source": "p.B", "name": "Dup", "type": "BIGINT"},
#         ])
#         with self.assertRaises(RenderError):
#             self.render(doc_with(cohort))
#
#     def test_missing_dest_table_is_refused(self):
#         with self.assertRaises(RenderError):
#             self.render(doc_with(pk_cohort(dest_table=None)))
#
#     def test_disabled_cohorts_are_skipped_with_a_note(self):
#         blocks, notes = self.render(doc_with(pk_cohort(pull_this_cycle=False)))
#         self.assertEqual(blocks, [])
#         self.assertTrue(any("pull_this_cycle" in n for n in notes))
#
#     def test_setup_captures_the_runtime_instance_name(self):
#         blocks = server_sql.render_setup(doc_with(), "S/setup")
#         self.assertIn("@@SERVERNAME", blocks[0].sql)
#         self.assertEqual(blocks[0].meta["captures"], "linked_server")
#
#
# class DualCosmosTests(unittest.TestCase):
#     """`cosmos_db: Dual` renders each cohort twice, against two databases."""
#
#     def cohorts(self):
#         base = pk_cohort()
#         sneak = pk_cohort(name="P_sp", dest_table="P_sp", cosmos_db="COSMOS_SneakPeek")
#         return base, sneak
#
#     def test_the_sneakpeek_variant_qualifies_its_own_database(self):
#         # Without this a two-part name resolves against the connected COSMOS,
#         # so the SneakPeek cohort would silently read the wrong data.
#         base, sneak = self.cohorts()
#         blocks, _ = server_sql.render_phase(doc_with(base, sneak, cosmos_db="Dual"), "S/pk")
#         by_dest = {b.dest_table: b.sql for b in blocks}
#         self.assertIn("FROM dbo.PatientDim AS p", by_dest["PKTable2"])
#         self.assertIn("FROM COSMOS_SneakPeek.dbo.PatientDim AS p", by_dest["P_sp"])
#
#     def test_both_variants_are_roots_and_both_are_limited(self):
#         # They are parallel chains, one per database, not competing ones.
#         base, sneak = self.cohorts()
#         doc = doc_with(base, sneak, cosmos_db="Dual",
#                        test_options={"smallset": True, "stop_at_for_pk_table": 500})
#         blocks, _ = server_sql.render_phase(doc, "S/pk")
#         self.assertTrue(all("TOP (500)" in b.sql for b in blocks))
#
#     def test_global_temps_stay_unqualified_in_both(self):
#         base, sneak = self.cohorts()
#         sneak["filter"]["join"] = ["INNER JOIN ##JVM_Other AS o ON 1 = 1"]
#         blocks, _ = server_sql.render_phase(doc_with(base, sneak, cosmos_db="Dual"), "S/pk")
#         sql = {b.dest_table: b.sql for b in blocks}["P_sp"]
#         self.assertIn("INNER JOIN ##JVM_Other AS o", sql)
#         self.assertNotIn("COSMOS_SneakPeek.dbo.##JVM_Other", sql)
#
#
# class LocalRenderTests(unittest.TestCase):
#     LINKED = "et4003vpdsql032"
#
#     def test_shell_drops_and_creates_the_destination(self):
#         blocks = local_sql.render_setup(doc_with(pk_cohort()), [pk_cohort()], "S/setup")
#         sql = blocks[0].sql
#         self.assertIn("DROP TABLE IF EXISTS PROJECTD93A5E7.dbo.PKTable2;", sql)
#         self.assertIn("CREATE TABLE PROJECTD93A5E7.dbo.PKTable2", sql)
#
#     def test_transfer_stages_then_inserts_in_a_transaction(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertIn("DROP TABLE IF EXISTS #Local_PKTable2;", sql)
#         self.assertIn("INTO #Local_PKTable2", sql)
#         self.assertIn(f"OPENQUERY(\n    [{self.LINKED}],", sql)
#         self.assertIn("BEGIN TRANSACTION;", sql)
#         self.assertIn("COMMIT TRANSACTION;", sql)
#         # The destination is created in setup, so a run only appends.
#         self.assertNotIn("CREATE TABLE PROJECTD93A5E7", sql)
#         self.assertNotIn("DROP TABLE IF EXISTS PROJECTD93A5E7", sql)
#
#     def test_slow_pull_happens_outside_the_transaction(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertLess(sql.index("OPENQUERY"), sql.index("BEGIN TRANSACTION"))
#
#     def test_captures_both_row_counts(self):
#         sql = local_sql.render_phase(doc_with(pk_cohort()), "S/pk", self.LINKED)[0].sql
#         self.assertIn("'cosmos' AS [Side]", sql)
#         self.assertIn("'projects' AS [Side]", sql)
#
#     def test_measures_string_column_lengths(self):
#         cohort = pk_cohort(columns=[
#             {"source": "p.Name", "name": "Name", "type": "VARCHAR(400)"},
#             {"source": "p.Key", "name": "Key", "type": "BIGINT"},
#         ])
#         sql = local_sql.render_phase(doc_with(cohort), "S/pk", self.LINKED)[0].sql
#         self.assertIn("MAX(LEN([Name]))", sql)
#         self.assertNotIn("MAX(LEN([Key]))", sql)
#
#     def test_no_length_probe_without_string_columns(self):
#         self.assertIsNone(local_sql.render_length_probe(pk_cohort()))
#
#     def test_missing_linked_server_is_refused(self):
#         # The instance name changes every connection, so a blank one means the
#         # session identity was never captured.
#         with self.assertRaises(LocalRenderError):
#             local_sql.render_phase(doc_with(pk_cohort()), "S/pk", "")
#
#     def test_missing_project_db_is_refused(self):
#         doc = doc_with(pk_cohort())
#         doc.pop("project_db")
#         with self.assertRaises(LocalRenderError):
#             local_sql.render_phase(doc, "S/pk", self.LINKED)
#
#
# class FixtureRenderTests(unittest.TestCase):
#     """Render the real split output rather than hand-built dictionaries."""
#
#     @classmethod
#     def setUpClass(cls):
#         if not FIXTURES.is_dir():
#             raise unittest.SkipTest(f"fixtures not found at {FIXTURES}")
#         from ..yaml_io import load_yaml
#         cls.load = staticmethod(load_yaml)
#
#     def phase(self, name):
#         return self.load(FIXTURES / "sessions" / "Patients" / name)
#
#     def test_pk_phase_renders(self):
#         blocks, _ = server_sql.render_phase(self.phase("pk.yaml"), "Patients/pk")
#         self.assertEqual([b.dest_table for b in blocks], ["Patients"])
#         self.assertIn("##JVM_Patients", blocks[0].sql)
#
#     def test_run_phase_renders_both_sides(self):
#         doc = self.phase("runs/run.yaml")
#         server, _ = server_sql.render_phase(doc, "Patients/run")
#         local = local_sql.render_phase(doc, "Patients/run", "et4003vpdsql032")
#         self.assertEqual([b.dest_table for b in server], [b.dest_table for b in local])
#         self.assertTrue(all(b.side == "server" for b in server))
#         self.assertTrue(all(b.side == "local" for b in local))
#
#     def test_block_ids_are_unique_and_addressable(self):
#         doc = self.phase("runs/run.yaml")
#         server, _ = server_sql.render_phase(doc, "Patients/run")
#         ids = [b.block_id for b in server]
#         self.assertEqual(len(ids), len(set(ids)))
#         self.assertTrue(all(b.dest_table in b.block_id for b in server))
#
# === END FILE: pullmanager/tests/test_render.py ===
# === BEGIN FILE: pullmanager/tests/test_session.py SHA256: 98a71c5383ac3eee9711dce20de58e349f78246f3bf021f7b018f2dc6b976a90 SIZE: 10227 ===
# """Session execution, against scripted fake connections.
#
# There is no database reachable from the development machine, so the
# connections are faked. What this proves is the orchestration: ordering, what a
# failure blocks, what the manifest records, and that the epoch and instance name
# are captured per connection.
# """
#
# from __future__ import annotations
#
# import re
# import shutil
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..db import Settings
# from ..manifest import Manifest
# from ..session import LARGE_ROW_WARNING, SessionError, SessionRunner
#
# FIXTURES = Path(__file__).resolve().parents[4] / "QMDs" / "pullmanager" / "fixtures" / "split"
# INSTANCE = "et4003vpdsql032"
#
#
# class ScriptedCursor:
#     """Answers by matching the SQL, so one fake serves the whole flow."""
#
#     def __init__(self, owner):
#         self.owner = owner
#         self._sets: list = []
#         self._current = None
#
#     def execute(self, sql, params=None):
#         self.owner.executed.append(sql)
#         for pattern, action in self.owner.failures.items():
#             if re.search(pattern, sql, re.I):
#                 raise RuntimeError(action)
#         self._sets = list(self.owner.results_for(sql))
#         self._advance()
#
#     def executemany(self, sql, seq):
#         self.owner.inserted.append((sql, list(seq)))
#
#     def _advance(self):
#         self._current = self._sets.pop(0) if self._sets else None
#
#     @property
#     def messages(self):
#         return []
#
#     @property
#     def description(self):
#         return None if self._current is None else [(c,) for c in self._current[0]]
#
#     def fetchall(self):
#         if self._current is None:
#             raise RuntimeError("no rows")
#         return list(self._current[1])
#
#     def fetchone(self):
#         if self._current is None:
#             return None
#         rows = self._current[1]
#         return rows[0] if rows else None
#
#     def nextset(self):
#         if not self._sets:
#             return False
#         self._advance()
#         return True
#
#     fast_executemany = False
#
#
# class FakeConnection:
#     def __init__(self, side, *, rows=10, distinct=None, landed=None, failures=None):
#         self.side = side
#         self.rows = rows
#         self.distinct = rows if distinct is None else distinct
#         self.landed = rows if landed is None else landed
#         self.failures = failures or {}
#         self.executed: list[str] = []
#         self.inserted: list = []
#         self.commits = 0
#         self.closed = False
#
#     def cursor(self):
#         return ScriptedCursor(self)
#
#     def commit(self):
#         self.commits += 1
#
#     def close(self):
#         self.closed = True
#
#     def results_for(self, sql):
#         if "@@SERVERNAME" in sql:
#             return [(["CosmosServerName"], [(INSTANCE,)])]
#         if "COUNT_BIG(DISTINCT" in sql:
#             return [(["total", "distinct"], [(self.rows, self.distinct)])]
#         if "SELECT * FROM" in sql:
#             return [(["PatientDurableKey", "Sex"], [(i, "Female") for i in range(3)])]
#         sets = []
#         for match in re.finditer(r"'([^']+)' AS \[DestTable\]", sql):
#             dest = match.group(1)
#             if "'cosmos' AS [Side]" in sql:
#                 sets.append((["DestTable", "Side", "RowCount"],
#                              [(dest, "cosmos", self.rows)]))
#                 sets.append((["DestTable", "Side", "RowCount"],
#                              [(dest, "projects", self.landed)]))
#             else:
#                 sets.append((["CohortName", "DestTable", "RowCount"],
#                              [(dest, dest, self.rows)]))
#         return sets
#
#
# class SessionTestCase(unittest.TestCase):
#     def setUp(self):
#         if not FIXTURES.is_dir():
#             self.skipTest(f"fixtures not found at {FIXTURES}")
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.root = Path(self._tmp.name) / "split"
#         shutil.copytree(FIXTURES, self.root)
#         (self.root / "fixtures").mkdir(exist_ok=True)
#         (self.root / "fixtures" / "hospital_icd_codes.csv").write_text(
#             "DiagnosisCode,Label\nK50.0,Crohn's\nK51.0,UC\n", encoding="utf-8"
#         )
#         self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
#
#     def declare_pk_key(self, column="PatientDurableKey"):
#         """The fixture's PK declares no key_column; some checks need one."""
#         from ..yaml_io import dump_yaml, load_yaml
#
#         path = self.root / "sessions" / "Patients" / "pk.yaml"
#         doc = load_yaml(path)
#         doc["cohorts"][0]["key_column"] = column
#         dump_yaml(doc, path)
#
#     def runner(self, **kwargs):
#         cosmos = FakeConnection("cosmos", **kwargs.pop("cosmos", {}))
#         projects = FakeConnection("projects", **kwargs.pop("projects", {}))
#         self.cosmos, self.projects = cosmos, projects
#         order = [cosmos, projects]
#
#         def connect_fn(conn_str, **_):
#             return order.pop(0)
#
#         return SessionRunner(
#             self.manifest,
#             self.manifest.sessions[0],
#             Settings(projects_server="PROJ", projects_database="PROJECTD33A929"),
#             connect_fn=connect_fn,
#             upload_root=self.root,
#             **kwargs,
#         )
#
#
# class HappyPathTests(SessionTestCase):
#     def test_runs_every_unit_and_records_it(self):
#         with self.runner() as runner:
#             report = runner.execute()
#         self.assertTrue(report.ok, report.failed)
#         self.assertEqual(len(report.completed), 4)
#
#     def test_captures_the_instance_name_per_connection(self):
#         # It changes every connection, so it is never cached.
#         with self.runner() as runner:
#             runner.execute()
#             self.assertEqual(runner.session.runtime["linked_server"], INSTANCE)
#             self.assertTrue(runner.session.epoch)
#
#     def test_manifest_is_saved_as_it_goes(self):
#         with self.runner() as runner:
#             runner.execute()
#         reloaded = Manifest.load(self.root / "pullmanifest.yaml")
#         session = reloaded.sessions[0]
#         self.assertEqual(session.status, "done")
#         self.assertTrue(all(p.status == "done" for p in session.phases))
#         self.assertTrue(all(p.epoch for p in session.phases))
#
#     def test_rows_are_recorded(self):
#         with self.runner(cosmos={"rows": 4242}) as runner:
#             runner.execute()
#         pk = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[2]
#         self.assertEqual(pk.rows, 4242)
#
#     def test_csv_upload_is_bound_not_interpolated(self):
#         with self.runner() as runner:
#             runner.execute()
#         statements = [sql for sql, _ in self.cosmos.inserted]
#         self.assertTrue(any("VALUES (?, ?)" in s for s in statements))
#
#     def test_connections_are_closed(self):
#         runner = self.runner()
#         with runner:
#             runner.execute()
#         self.assertTrue(self.cosmos.closed)
#         self.assertTrue(self.projects.closed)
#
#
# class FailureTests(SessionTestCase):
#     def test_a_failed_phase_blocks_what_follows(self):
#         # setup, uploads and PK are prerequisites.
#         with self.runner(projects={"failures": {r"CREATE TABLE PROJECTD": "disk full"}}) as runner:
#             report = runner.execute()
#         self.assertFalse(report.ok)
#         session = Manifest.load(self.root / "pullmanifest.yaml").sessions[0]
#         self.assertEqual(session.phases[0].status, "failed")
#         self.assertEqual(session.phases[1].status, "blocked")
#         self.assertEqual(session.runs[0].status, "blocked")
#
#     def test_failure_detail_is_kept(self):
#         with self.runner(projects={"failures": {r"CREATE TABLE PROJECTD": "disk full"}}) as runner:
#             runner.execute()
#         phase = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].phases[0]
#         self.assertIn("disk full", phase.error["message"])
#
#     def test_a_pk_without_a_key_column_warns_instead_of_checking(self):
#         # Nothing to order by means chunk stability cannot be verified.
#         with self.runner() as runner:
#             report = runner.execute()
#         self.assertTrue(any("key_column" in w for w in report.warnings))
#
#     def test_a_non_unique_pk_is_refused(self):
#         # Chunking orders by the key; duplicates make a chunk mean different
#         # rows each run.
#         self.declare_pk_key()
#         with self.runner(projects={"rows": 100, "distinct": 90}) as runner:
#             report = runner.execute()
#         self.assertFalse(report.ok)
#         self.assertTrue(any("distinct" in message for _, message in report.failed))
#
#     def test_row_count_mismatch_warns(self):
#         with self.runner(cosmos={"rows": 1000}, projects={"rows": 1000, "landed": 998}) as runner:
#             report = runner.execute()
#         self.assertTrue(any("did not carry everything" in w for w in report.warnings))
#
#     def test_very_large_pull_warns(self):
#         big = LARGE_ROW_WARNING + 1
#         with self.runner(cosmos={"rows": big}, projects={"rows": big, "landed": big}) as runner:
#             report = runner.execute()
#         self.assertTrue(any("warning threshold" in w for w in report.warnings))
#
#
# class ResumeTests(SessionTestCase):
#     def test_a_second_run_replays_stale_server_work(self):
#         with self.runner() as runner:
#             runner.execute()
#         first_epoch = Manifest.load(self.root / "pullmanifest.yaml").sessions[0].epoch
#
#         self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
#         with self.runner() as runner:
#             report = runner.execute()
#         # A new connection means the global temps are gone, so the phases run
#         # again even though their status said done.
#         self.assertIn("Patients/setup", report.completed)
#         self.assertIn("Patients/pk", report.completed)
#         self.assertNotEqual(self.manifest.sessions[0].epoch, first_epoch)
#
#     def test_partial_resume_keeps_completed_runs(self):
#         with self.runner() as runner:
#             runner.execute()
#         self.manifest = Manifest.load(self.root / "pullmanifest.yaml")
#         with self.runner(mode="partial") as runner:
#             report = runner.execute()
#         # The run's rows are in a Projects table and survive the lost connection.
#         self.assertTrue(any("survive" in s for s in report.skipped))
#
# === END FILE: pullmanager/tests/test_session.py ===
# === BEGIN FILE: pullmanager/tests/test_sql.py SHA256: 70f3bfde2d04c0ab5dc3df2d063182f2684f908b04446d708049f1ee40c1cc35 SIZE: 5285 ===
# """SQL construction, with the WHERE builder as the main risk."""
#
# from __future__ import annotations
#
# import unittest
#
# from ..sql import (
#     ddl_body,
#     from_entries,
#     is_nullable,
#     non_null_predicates,
#     quote_literal,
#     render_source_clause,
#     render_where,
#     where_entries,
# )
#
#
# class WhereBuilderTests(unittest.TestCase):
#     def render(self, predicates):
#         return [line.strip() for line in render_where(predicates).splitlines()]
#
#     def test_first_predicate_takes_no_and(self):
#         self.assertEqual(self.render(["a = 1"]), ["a = 1"])
#
#     def test_subsequent_predicates_take_and(self):
#         self.assertEqual(self.render(["a = 1", "b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_preserves_a_grouped_code_list(self):
#         # The K50/K51 filter, authored as separate list entries. Inserting AND
#         # before the OR lines or the closing paren would be invalid SQL.
#         self.assertEqual(
#             self.render([
#                 "dt.Type IN ('ICD-10-CM')",
#                 "( dt.Value LIKE 'K50.%'",
#                 "  OR dt.Value = 'K50'",
#                 "  OR dt.Value LIKE 'K51.%'",
#                 "  OR dt.Value = 'K51'",
#                 ")",
#             ]),
#             [
#                 "dt.Type IN ('ICD-10-CM')",
#                 "AND ( dt.Value LIKE 'K50.%'",
#                 "OR dt.Value = 'K50'",
#                 "OR dt.Value LIKE 'K51.%'",
#                 "OR dt.Value = 'K51'",
#                 ")",
#             ],
#         )
#
#     def test_does_not_double_an_explicit_and(self):
#         self.assertEqual(self.render(["a = 1", "AND b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_leading_or_is_left_alone(self):
#         self.assertEqual(self.render(["a = 1", "OR b = 2"]), ["a = 1", "OR b = 2"])
#
#     def test_comments_are_not_prefixed(self):
#         self.assertEqual(
#             self.render(["a = 1", "-- restrict to ICD-10", "b = 2"]),
#             ["a = 1", "-- restrict to ICD-10", "AND b = 2"],
#         )
#
#     def test_a_group_opening_the_clause_takes_no_and(self):
#         self.assertEqual(self.render(["( a = 1", "OR b = 2", ")"]), ["( a = 1", "OR b = 2", ")"])
#
#     def test_blank_entries_are_dropped(self):
#         self.assertEqual(self.render(["a = 1", "", "   ", "b = 2"]), ["a = 1", "AND b = 2"])
#
#     def test_multiline_entries_are_split(self):
#         self.assertEqual(
#             self.render(["a = 1\nAND b = 2", "c = 3"]),
#             ["a = 1", "AND b = 2", "AND c = 3"],
#         )
#
#     def test_case_insensitive_continuations(self):
#         self.assertEqual(self.render(["a = 1", "and b = 2", "or c = 3"]),
#                          ["a = 1", "and b = 2", "or c = 3"])
#
#
# class NonNullTests(unittest.TestCase):
#     def test_adds_a_predicate_for_each_non_nullable_column(self):
#         columns = [
#             {"source": "p.A", "name": "A", "nullable": False},
#             {"source": "p.B", "name": "B", "nullable": True},
#             {"source": "p.C", "name": "C"},  # absent means nullable
#         ]
#         self.assertEqual(non_null_predicates(columns), ["p.A IS NOT NULL"])
#
#     def test_accepts_boolean_spellings(self):
#         self.assertEqual(
#             non_null_predicates([{"source": "p.A", "name": "A", "nullable": "no"}]),
#             ["p.A IS NOT NULL"],
#         )
#
#     def test_absent_nullable_defaults_to_nullable(self):
#         self.assertTrue(is_nullable({"name": "A"}))
#
#
# class FilterShapeTests(unittest.TestCase):
#     def test_from_accepts_a_string_or_a_list(self):
#         self.assertEqual(from_entries({"from": "PatientDim AS p"}), ["PatientDim AS p"])
#         self.assertEqual(from_entries({"from": ["PatientDim AS p"]}), ["PatientDim AS p"])
#         self.assertEqual(from_entries({}), [])
#
#     def test_where_accepts_a_string_or_a_list(self):
#         self.assertEqual(where_entries({"where": "a = 1"}), ["a = 1"])
#         self.assertEqual(where_entries({"where": ["a = 1", "b = 2"]}), ["a = 1", "b = 2"])
#
#     def test_source_clause_qualifies_from_and_joins(self):
#         sql = render_source_clause({
#             "from": ["DiagnosisEventFact AS def"],
#             "join": [
#                 "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = def.DiagnosisKey",
#                 "INNER JOIN ##JVM_PKTable2 AS p ON p.PatientDurableKey = def.PatientDurableKey",
#             ],
#         })
#         self.assertIn("FROM dbo.DiagnosisEventFact AS def", sql)
#         self.assertIn("INNER JOIN dbo.DiagnosisTerminologyDim", sql)
#         # A generated temp lives in tempdb and must stay unqualified.
#         self.assertIn("INNER JOIN ##JVM_PKTable2 AS p", sql)
#
#
# class LiteralTests(unittest.TestCase):
#     def test_escapes_embedded_quotes(self):
#         self.assertEqual(quote_literal("HUMIRA(CF) CROHN'S STARTER"), "'HUMIRA(CF) CROHN''S STARTER'")
#
#     def test_renders_scalars(self):
#         self.assertEqual(quote_literal(None), "NULL")
#         self.assertEqual(quote_literal(42), "42")
#         self.assertEqual(quote_literal(True), "1")
#
#
# class DdlTests(unittest.TestCase):
#     def test_renders_nullability(self):
#         body = ddl_body([
#             {"name": "A", "type": "BIGINT", "nullable": False},
#             {"name": "B", "type": "VARCHAR(400)", "nullable": True},
#         ])
#         self.assertIn("[A] BIGINT NOT NULL", body)
#         self.assertIn("[B] VARCHAR(400) NULL", body)
#
# === END FILE: pullmanager/tests/test_sql.py ===
# === BEGIN FILE: pullmanager/tests/test_uploads.py SHA256: 433529c848a599c7348f5fa202e1b551fa9cb3dc2d66351e7cd37103a7d2f669 SIZE: 5626 ===
# """CSV and dbtable uploads."""
#
# from __future__ import annotations
#
# import tempfile
# import unittest
# from pathlib import Path
#
# from ..uploads import (
#     LENGTH_HEADROOM,
#     MAX_COLUMN_WIDTH,
#     MIN_COLUMN_WIDTH,
#     UploadError,
#     enabled_uploads,
#     measure_widths,
#     plan_csv_upload,
#     read_csv,
#     render_create,
#     safe_identifier,
#     upload_kind,
# )
#
#
# class IdentifierTests(unittest.TestCase):
#     def test_normalizes_awkward_headers(self):
#         cases = [
#             ("Medication Key", "Medication_Key"),
#             ("Therapeutic-Class", "Therapeutic_Class"),
#             ("2ndCode", "_2ndCode"),
#             ("  spaced  ", "spaced"),
#             ("a.b.c", "a_b_c"),
#         ]
#         for raw, expected in cases:
#             with self.subTest(raw=raw):
#                 self.assertEqual(safe_identifier(raw, 0), expected)
#
#     def test_blank_header_gets_a_position_name(self):
#         self.assertEqual(safe_identifier("", 3), "Column4")
#
#
# class CsvTests(unittest.TestCase):
#     def setUp(self):
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.root = Path(self._tmp.name)
#
#     def write(self, text, name="codes.csv", encoding="utf-8"):
#         path = self.root / name
#         path.write_text(text, encoding=encoding)
#         return path
#
#     def test_reads_headers_and_rows(self):
#         path = self.write("Key,Name\n46,RISANKIZUMAB\n403,HUMIRA\n")
#         columns, rows = read_csv(path)
#         self.assertEqual(columns, ["Key", "Name"])
#         self.assertEqual(rows, [("46", "RISANKIZUMAB"), ("403", "HUMIRA")])
#
#     def test_strips_a_byte_order_mark(self):
#         # A BOM otherwise becomes part of the first column name and silently
#         # breaks every reference to it.
#         path = self.write("Key,Name\n1,x\n", encoding="utf-8-sig")
#         columns, _ = read_csv(path)
#         self.assertEqual(columns[0], "Key")
#
#     def test_empty_cells_become_null(self):
#         path = self.write("Key,Name\n1,\n")
#         _, rows = read_csv(path)
#         self.assertEqual(rows, [("1", None)])
#
#     def test_short_rows_are_padded(self):
#         path = self.write("A,B,C\n1,2\n")
#         _, rows = read_csv(path)
#         self.assertEqual(rows, [("1", "2", None)])
#
#     def test_blank_lines_are_dropped(self):
#         path = self.write("A\n1\n\n2\n")
#         _, rows = read_csv(path)
#         self.assertEqual(rows, [("1",), ("2",)])
#
#     def test_duplicate_headers_are_made_unique(self):
#         path = self.write("Name,Name\n1,2\n")
#         columns, _ = read_csv(path)
#         self.assertEqual(columns, ["Name", "Name_1"])
#
#     def test_quotes_survive_binding(self):
#         # Values are bound, not interpolated, so an apostrophe needs no escaping.
#         path = self.write("Name\n\"HUMIRA(CF) CROHN'S STARTER\"\n")
#         _, rows = read_csv(path)
#         self.assertEqual(rows[0][0], "HUMIRA(CF) CROHN'S STARTER")
#
#     def test_missing_file_is_refused(self):
#         with self.assertRaises(UploadError):
#             read_csv(self.root / "nope.csv")
#
#     def test_empty_file_is_refused(self):
#         with self.assertRaises(UploadError):
#             read_csv(self.write(""))
#
#     def test_header_only_uploads_an_empty_table_with_a_note(self):
#         self.write("Key,Name\n")
#         plan = plan_csv_upload(
#             {"name": "U", "dest_table": "U", "file_type": "csv", "file_loc": "codes.csv"},
#             self.root,
#         )
#         self.assertEqual(plan.rows, [])
#         self.assertTrue(plan.notes)
#
#
# class WidthTests(unittest.TestCase):
#     def test_sizes_from_the_data_with_headroom(self):
#         # The whole file is in hand before the table exists, so measuring works
#         # here even though it cannot for a batched pull.
#         widths = measure_widths(["A"], [("x" * 100,)])
#         self.assertEqual(widths["A"], 150)
#
#     def test_width_is_the_longest_value_plus_headroom(self):
#         self.assertEqual(measure_widths(["A"], [("x",)])["A"], 1 + LENGTH_HEADROOM)
#
#     def test_an_all_null_column_falls_back_to_the_floor(self):
#         # The floor only binds when there is nothing to measure.
#         self.assertEqual(measure_widths(["A"], [(None,)])["A"], MIN_COLUMN_WIDTH)
#
#     def test_width_is_capped(self):
#         self.assertEqual(measure_widths(["A"], [("x" * 9000,)])["A"], MAX_COLUMN_WIDTH)
#
#     def test_create_uses_the_measured_widths(self):
#         from ..uploads import UploadPlan
#
#         plan = UploadPlan(
#             name="U", dest_table="U", global_temp="##JVM_U",
#             columns=["A"], rows=[("x" * 100,)], widths={"A": 150},
#         )
#         sql = render_create(plan)
#         self.assertIn("DROP TABLE IF EXISTS ##JVM_U;", sql)
#         self.assertIn("[A] NVARCHAR(150) NULL", sql)
#
#
# class KindTests(unittest.TestCase):
#     def test_accepts_csv_and_dbtable(self):
#         self.assertEqual(upload_kind({"file_type": "csv"}), "csv")
#         self.assertEqual(upload_kind({"file_type": "DBTable"}), "dbtable")
#
#     def test_parquet_is_refused_with_guidance(self):
#         with self.assertRaises(UploadError) as caught:
#             upload_kind({"name": "U", "file_type": "parquet"})
#         self.assertIn("Cosmos cannot read", str(caught.exception))
#
#     def test_unknown_kind_is_refused(self):
#         with self.assertRaises(UploadError):
#             upload_kind({"name": "U", "file_type": "xlsx"})
#
#     def test_push_this_cycle_gates_uploads(self):
#         doc = {"upload_cohorts": [
#             {"name": "A", "push_this_cycle": True},
#             {"name": "B", "push_this_cycle": False},
#             {"name": "C"},
#         ]}
#         self.assertEqual([u["name"] for u in enabled_uploads(doc)], ["A", "C"])
#
# === END FILE: pullmanager/tests/test_uploads.py ===
# === BEGIN FILE: pullmanager/uploads.py SHA256: 9698807e77ee5ecf179a2478f9fb4ae53e6b5d702caf8126a27cbc0b6fadb4e9 SIZE: 7077 ===
# """Upload cohorts: getting local data up into a Cosmos global temp.
#
# There is no linked server from Cosmos back to Projects, so everything here
# travels through the client and lands via parameter binding.
# """
#
# from __future__ import annotations
#
# import csv
# import re
# from dataclasses import dataclass, field
# from pathlib import Path
# from typing import Any, Sequence
#
# from .db import DatabaseError, bulk_insert, execute_script
# from .naming import global_temp
# from .normalize import normalize_bool
#
# # Room above the widest value seen, so a later file with slightly longer
# # values does not immediately fail.
# LENGTH_HEADROOM = 50
# MIN_COLUMN_WIDTH = 50
# MAX_COLUMN_WIDTH = 4000
#
# _LEADING_DIGIT = re.compile(r"^\d")
# _UNSAFE = re.compile(r"[^\w]+")
#
#
# class UploadError(ValueError):
#     """Raised when an upload cohort cannot be materialized."""
#
#
# @dataclass
# class UploadPlan:
#     name: str
#     dest_table: str
#     global_temp: str
#     columns: list[str]
#     rows: list[tuple]
#     widths: dict[str, int] = field(default_factory=dict)
#     notes: list[str] = field(default_factory=list)
#
#     @property
#     def row_count(self) -> int:
#         return len(self.rows)
#
#
# def safe_identifier(header: str, position: int) -> str:
#     """Turn a CSV header into something SQL can name."""
#     cleaned = _UNSAFE.sub("_", str(header or "").strip()).strip("_")
#     if not cleaned:
#         cleaned = f"Column{position + 1}"
#     if _LEADING_DIGIT.match(cleaned):
#         cleaned = f"_{cleaned}"
#     return cleaned
#
#
# def read_csv(path: Path) -> tuple[list[str], list[tuple]]:
#     """Read a CSV, BOM-safe, with headers normalized to SQL identifiers."""
#     if not path.is_file():
#         raise UploadError(f"Upload file not found: {path}")
#     # utf-8-sig strips a byte order mark, which otherwise becomes part of the
#     # first column name and silently breaks every reference to it.
#     with path.open("r", encoding="utf-8-sig", newline="") as handle:
#         reader = csv.reader(handle)
#         try:
#             header = next(reader)
#         except StopIteration:
#             raise UploadError(f"Upload file is empty: {path}") from None
#         columns = [safe_identifier(name, i) for i, name in enumerate(header)]
#         if not columns:
#             raise UploadError(f"Upload file has no header columns: {path}")
#         seen: dict[str, int] = {}
#         for index, name in enumerate(columns):
#             if name in seen:
#                 seen[name] += 1
#                 columns[index] = f"{name}_{seen[name]}"
#             else:
#                 seen[name] = 0
#         width = len(columns)
#         rows: list[tuple] = []
#         for record in reader:
#             if not any(str(cell).strip() for cell in record):
#                 continue
#             padded = list(record[:width]) + [None] * max(0, width - len(record))
#             rows.append(tuple(cell if str(cell) != "" else None for cell in padded))
#     return columns, rows
#
#
# def measure_widths(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, int]:
#     """Size each column from the data actually present.
#
#     This works for an upload because the whole file is in hand before the table
#     is created. It does not work for a batched pull, where the table exists
#     before any batch runs.
#     """
#     widths = {name: MIN_COLUMN_WIDTH for name in columns}
#     for row in rows:
#         for name, value in zip(columns, row):
#             if value is None:
#                 continue
#             widths[name] = max(widths[name], len(str(value)) + LENGTH_HEADROOM)
#     return {name: min(width, MAX_COLUMN_WIDTH) for name, width in widths.items()}
#
#
# def resolve_path(file_loc: str, root: Path) -> Path:
#     candidate = Path(file_loc)
#     return candidate if candidate.is_absolute() else (root / candidate)
#
#
# def plan_csv_upload(cohort: dict[str, Any], root: Path) -> UploadPlan:
#     dest = str(cohort.get("dest_table") or cohort.get("name") or "")
#     if not dest:
#         raise UploadError("Upload cohort has neither dest_table nor name.")
#     file_loc = cohort.get("file_loc")
#     if not file_loc:
#         raise UploadError(f"Upload cohort {dest!r} is file_type csv but has no file_loc.")
#     path = resolve_path(str(file_loc), root)
#     columns, rows = read_csv(path)
#     plan = UploadPlan(
#         name=str(cohort.get("name") or dest),
#         dest_table=dest,
#         global_temp=global_temp(dest),
#         columns=columns,
#         rows=rows,
#         widths=measure_widths(columns, rows),
#     )
#     if not rows:
#         plan.notes.append(f"{path.name} has a header but no data rows; uploading an empty table.")
#     return plan
#
#
# def render_create(plan: UploadPlan) -> str:
#     body = ",\n".join(
#         f"    [{name}] NVARCHAR({plan.widths.get(name, MIN_COLUMN_WIDTH)}) NULL"
#         for name in plan.columns
#     )
#     return (
#         f"DROP TABLE IF EXISTS {plan.global_temp};\n\n"
#         f"CREATE TABLE {plan.global_temp}\n(\n{body}\n);"
#     )
#
#
# def materialize(connection: Any, plan: UploadPlan, *, chunk_size: int) -> int:
#     """Create the temp table and bind the rows into it."""
#     execute_script(connection, render_create(plan), label=f"upload {plan.dest_table}")
#     if not plan.rows:
#         return 0
#     try:
#         return bulk_insert(
#             connection,
#             plan.global_temp,
#             plan.columns,
#             plan.rows,
#             chunk_size=chunk_size,
#         )
#     except DatabaseError as exc:
#         raise UploadError(f"Upload of {plan.dest_table!r} failed: {exc}") from exc
#
#
# def plan_dbtable_upload(
#     projects_connection: Any, cohort: dict[str, Any], project_db: str
# ) -> UploadPlan:
#     """Read an existing Projects table and carry it up through the client."""
#     dest = str(cohort.get("dest_table") or cohort.get("name") or "")
#     source = str(cohort.get("source_table") or dest)
#     cursor = projects_connection.cursor()
#     cursor.execute(f"SELECT * FROM {project_db}.dbo.{source};")
#     columns = [column[0] for column in cursor.description or []]
#     rows = [tuple(row) for row in cursor.fetchall()]
#     return UploadPlan(
#         name=str(cohort.get("name") or dest),
#         dest_table=dest,
#         global_temp=global_temp(dest),
#         columns=columns,
#         rows=rows,
#         widths=measure_widths(columns, rows),
#     )
#
#
# def enabled_uploads(doc: dict[str, Any]) -> list[dict[str, Any]]:
#     return [
#         upload for upload in doc.get("upload_cohorts") or []
#         if isinstance(upload, dict)
#         and normalize_bool(upload.get("push_this_cycle"), default=True)
#     ]
#
#
# def upload_kind(cohort: dict[str, Any]) -> str:
#     kind = str(cohort.get("file_type") or "").strip().lower()
#     if kind == "parquet":
#         raise UploadError(
#             f"Upload cohort {cohort.get('name')!r} is file_type parquet, which Cosmos "
#             "cannot read. Convert it to CSV, or load it into Projects and use dbtable."
#         )
#     if kind not in ("csv", "dbtable"):
#         raise UploadError(
#             f"Upload cohort {cohort.get('name')!r} has unsupported file_type "
#             f"{cohort.get('file_type')!r}. Expected csv or dbtable."
#         )
#     return kind
#
# === END FILE: pullmanager/uploads.py ===
# === BEGIN FILE: pullmanager/yaml_io.py SHA256: dca04d852f7873c8abcac4d0e9f0f0e1883c96117a9bcacdf7766fbae4255c18 SIZE: 1844 ===
# """YAML load/dump for Pullmanager.
#
# Mirrors the backend selection in scripts/makeYaml.py so manifests round-trip
# through the same representation YAML Manager wrote them with.
# """
#
# from __future__ import annotations
#
# from pathlib import Path
# from typing import Any
#
#
# def _backend():
#     try:
#         from ruamel.yaml import YAML  # type: ignore
#
#         yaml = YAML()
#         yaml.preserve_quotes = True
#         yaml.default_flow_style = False
#         return "ruamel", yaml
#     except Exception:
#         pass
#
#     try:
#         import yaml  # type: ignore
#
#         return "pyyaml", yaml
#     except Exception:
#         return "none", None
#
#
# def _plain(value: Any) -> Any:
#     if isinstance(value, dict):
#         return {str(k): _plain(v) for k, v in value.items()}
#     if isinstance(value, list):
#         return [_plain(v) for v in value]
#     return value
#
#
# def load_yaml(path: str | Path) -> Any:
#     path = Path(path)
#     backend, mod = _backend()
#     if backend == "ruamel":
#         with path.open("r", encoding="utf-8") as handle:
#             return _plain(mod.load(handle))
#     if backend == "pyyaml":
#         with path.open("r", encoding="utf-8") as handle:
#             return mod.safe_load(handle)
#     raise RuntimeError("No YAML backend available. Install ruamel.yaml or pyyaml.")
#
#
# def dump_yaml(data: Any, path: str | Path) -> None:
#     path = Path(path)
#     path.parent.mkdir(parents=True, exist_ok=True)
#     backend, mod = _backend()
#     if backend == "ruamel":
#         with path.open("w", encoding="utf-8") as handle:
#             mod.dump(data, handle)
#         return
#     if backend == "pyyaml":
#         with path.open("w", encoding="utf-8") as handle:
#             mod.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
#         return
#     raise RuntimeError("No YAML backend available. Install ruamel.yaml or pyyaml.")
#
# === END FILE: pullmanager/yaml_io.py ===
# === BEGIN FILE: scripts/makeYaml.py SHA256: fe1e7dc6b8814dfe844b8d5222eb3dd2300be1b3abf4cbdde8de3b4b98d47470 SIZE: 98604 ===
# #!/usr/bin/env python3
# """
# Compile human-authored YAML Manager templates into VM-facing YAML artifacts.
#
# The file is intentionally self-contained for the VM copy-update workflow.
# """
#
# from __future__ import annotations
#
# import argparse
# import copy
# import csv
# import json
# import os
# import re
# import shutil
# import subprocess
# import sys
# import tempfile
# import unittest
# from dataclasses import dataclass, field
# from itertools import product
# from pathlib import Path
# from typing import Any
#
#
# OUTPUT_SUFFIX = "_Full"
# PREYAML_SUFFIX = "_preyaml"
# EXPANDED_PREYAML_SUFFIX = "_preyaml_expanded"
# WILDCARD_CHARS = ("%", "_", "[", "]")
# DEFAULT_MANIFEST_PATH = Path("split") / "pullmanifest.yaml"
# DEFAULT_SPLIT_DIR = Path("split")
#
#
# # =============================================================================
# # Result structures
# # =============================================================================
#
#
# @dataclass
# class Message:
#     level: str
#     code: str
#     message: str
#     context: str = ""
#
#     def to_dict(self) -> dict[str, str]:
#         return {
#             "level": self.level,
#             "code": self.code,
#             "message": self.message,
#             "context": self.context,
#         }
#
#
# @dataclass
# class CompileResult:
#     ok: bool = True
#     errors: list[Message] = field(default_factory=list)
#     warnings: list[Message] = field(default_factory=list)
#     finished_yaml: dict[str, Any] = field(default_factory=dict)
#     analysis: dict[str, Any] = field(default_factory=dict)
#     graph: dict[str, Any] = field(default_factory=lambda: {"nodes": [], "edges": []})
#     output_path: str | None = None
#
#     def error(self, code: str, message: str, context: str = "") -> None:
#         self.ok = False
#         self.errors.append(Message("ERROR", code, message, context))
#
#     def warn(self, code: str, message: str, context: str = "") -> None:
#         self.warnings.append(Message("WARN", code, message, context))
#
#
# @dataclass
# class SplitPhase:
#     name: str
#     yaml: str
#     status: str = "pending"
#     pk_source: dict[str, Any] | None = None
#     rows: int | None = None
#     outputs: dict[str, Any] = field(default_factory=dict)
#     error: dict[str, Any] | None = None
#     started_at: str | None = None
#     finished_at: str | None = None
#
#     def to_dict(self) -> dict[str, Any]:
#         out: dict[str, Any] = {
#             "yaml": self.yaml,
#             "status": self.status,
#             "started_at": self.started_at,
#             "finished_at": self.finished_at,
#             "rows": self.rows,
#             "outputs": self.outputs,
#             "error": self.error,
#         }
#         if self.pk_source is not None:
#             out["pk_source"] = self.pk_source
#         return out
#
#
# @dataclass
# class SplitRun:
#     run_id: str
#     yaml: str
#     status: str = "pending"
#     batch: dict[str, Any] | None = None
#     rows: int | None = None
#     outputs: dict[str, Any] = field(default_factory=dict)
#     error: dict[str, Any] | None = None
#     started_at: str | None = None
#     finished_at: str | None = None
#
#     def to_dict(self) -> dict[str, Any]:
#         out: dict[str, Any] = {
#             "run_id": self.run_id,
#             "yaml": self.yaml,
#             "status": self.status,
#             "started_at": self.started_at,
#             "finished_at": self.finished_at,
#             "rows": self.rows,
#             "outputs": self.outputs,
#             "error": self.error,
#         }
#         if self.batch is not None:
#             out["batch"] = self.batch
#         return out
#
#
# @dataclass
# class SplitSession:
#     session_id: str
#     cohort: str
#     pk_table: str | None
#     phases: dict[str, SplitPhase]
#     runs: list[SplitRun]
#     status: str = "pending"
#     multiplier: dict[str, Any] | None = None
#
#     def to_dict(self) -> dict[str, Any]:
#         out: dict[str, Any] = {
#             "session_id": self.session_id,
#             "cohort": self.cohort,
#             "pk_table": self.pk_table,
#             "status": self.status,
#             "phases": {name: phase.to_dict() for name, phase in self.phases.items()},
#             "runs": [run.to_dict() for run in self.runs],
#         }
#         if self.multiplier is not None:
#             out["multiplier"] = self.multiplier
#         return out
#
#
# @dataclass
# class SplitPlan:
#     project: dict[str, Any]
#     source: dict[str, Any]
#     sessions: list[SplitSession]
#     manifest_version: int = 1
#
#     def to_dict(self) -> dict[str, Any]:
#         return {
#             "manifest_version": self.manifest_version,
#             "project": self.project,
#             "source": self.source,
#             "sessions": [session.to_dict() for session in self.sessions],
#         }
#
#
# # =============================================================================
# # YAML loading and writing
# # =============================================================================
#
#
# def _yaml_backend():
#     try:
#         from ruamel.yaml import YAML  # type: ignore
#
#         yaml = YAML()
#         yaml.preserve_quotes = True
#         yaml.default_flow_style = False
#         return "ruamel", yaml
#     except Exception:
#         pass
#
#     try:
#         import yaml  # type: ignore
#
#         return "pyyaml", yaml
#     except Exception:
#         return "ruby", None
#
#
# def load_yaml(path: str | Path) -> Any:
#     path = Path(path)
#     backend, yaml_mod = _yaml_backend()
#     if backend == "ruamel":
#         with path.open("r", encoding="utf-8") as handle:
#             data = yaml_mod.load(handle)
#         return _plain_data(data)
#     if backend == "pyyaml":
#         with path.open("r", encoding="utf-8") as handle:
#             return yaml_mod.safe_load(handle)
#
#     # Local-development fallback for machines without Python YAML packages.
#     cmd = [
#         "ruby",
#         "-ryaml",
#         "-rjson",
#         "-e",
#         "print JSON.generate(YAML.load_file(ARGV[0]))",
#         str(path),
#     ]
#     try:
#         proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
#     except Exception as exc:
#         raise RuntimeError(
#             "No Python YAML backend available. Install ruamel.yaml or pyyaml."
#         ) from exc
#     return json.loads(proc.stdout)
#
#
# def dump_yaml(data: Any, path: str | Path) -> None:
#     path = Path(path)
#     path.parent.mkdir(parents=True, exist_ok=True)
#     backend, yaml_mod = _yaml_backend()
#     if backend == "ruamel":
#         with path.open("w", encoding="utf-8") as handle:
#             yaml_mod.dump(data, handle)
#         return
#     if backend == "pyyaml":
#         with path.open("w", encoding="utf-8") as handle:
#             yaml_mod.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
#         return
#
#     # Minimal writer fallback. This keeps local TDD runnable; production should
#     # use ruamel.yaml or pyyaml.
#     path.write_text(_simple_yaml_dump(data), encoding="utf-8")
#
#
# def _plain_data(value: Any) -> Any:
#     if isinstance(value, dict):
#         return {str(k): _plain_data(v) for k, v in value.items()}
#     if isinstance(value, list):
#         return [_plain_data(v) for v in value]
#     return value
#
#
# def _simple_yaml_dump(data: Any, indent: int = 0) -> str:
#     pad = " " * indent
#     if isinstance(data, dict):
#         lines: list[str] = []
#         for key, value in data.items():
#             if isinstance(value, (dict, list)):
#                 lines.append(f"{pad}{key}:")
#                 lines.append(_simple_yaml_dump(value, indent + 2).rstrip())
#             else:
#                 lines.append(f"{pad}{key}: {_format_scalar(value)}")
#         return "\n".join(lines) + "\n"
#     if isinstance(data, list):
#         lines = []
#         for item in data:
#             if isinstance(item, dict):
#                 if not item:
#                     lines.append(f"{pad}- {{}}")
#                     continue
#                 first = True
#                 for key, value in item.items():
#                     bullet = "- " if first else "  "
#                     if isinstance(value, (dict, list)):
#                         lines.append(f"{pad}{bullet}{key}:")
#                         lines.append(_simple_yaml_dump(value, indent + 4).rstrip())
#                     else:
#                         lines.append(f"{pad}{bullet}{key}: {_format_scalar(value)}")
#                     first = False
#             elif isinstance(item, list):
#                 lines.append(f"{pad}-")
#                 lines.append(_simple_yaml_dump(item, indent + 2).rstrip())
#             else:
#                 lines.append(f"{pad}- {_format_scalar(item)}")
#         return "\n".join(lines) + "\n"
#     return f"{pad}{_format_scalar(data)}\n"
#
#
# def dump_yaml_text(data: Any) -> str:
#     return _simple_yaml_dump(data)
#
#
# def _format_scalar(value: Any) -> str:
#     if value is None:
#         return ""
#     if isinstance(value, bool):
#         return "true" if value else "false"
#     if isinstance(value, (int, float)):
#         return str(value)
#     text = str(value)
#     if text == "" or any(ch in text for ch in [":", "#", "{", "}", "[", "]", "%"]):
#         return json.dumps(text)
#     return text
#
#
# # =============================================================================
# # Normalization and recipe import
# # =============================================================================
#
#
# def script_root() -> Path:
#     return Path(__file__).resolve().parent
#
#
# def project_root() -> Path:
#     return script_root().parent
#
#
# def default_template_path() -> Path:
#     return project_root() / "YAMLs" / "template.yaml"
#
#
# def default_recipes_path() -> Path:
#     return project_root() / "YAMLs" / "recipes.yaml"
#
#
# def normalize_template(template: dict[str, Any], result: CompileResult) -> dict[str, Any]:
#     template = copy.deepcopy(template or {})
#     for section_name in ("cosmos_vars", "project_vars", "test_options"):
#         section = template.get(section_name)
#         if isinstance(section, dict):
#             for key, value in section.items():
#                 template.setdefault(key, value)
#     vars_block = dict(template.get("vars") or {})
#     run_vars = template.get("run_vars")
#     if isinstance(run_vars, dict):
#         vars_block = merge_vars(run_vars, vars_block)
#     for legacy_key in ("min_date_key", "max_date_key"):
#         if legacy_key in template and legacy_key not in vars_block:
#             vars_block[legacy_key] = template[legacy_key]
#             result.warn(
#                 "legacy_global_var",
#                 f"`{legacy_key}` should live under top-level `vars`.",
#                 legacy_key,
#             )
#     template["vars"] = vars_block
#     if "project_folder" not in template and "projaect_folder" in template:
#         template["project_folder"] = template["projaect_folder"]
#         result.warn(
#             "legacy_project_folder",
#             "`projaect_folder` is misspelled; use `project_folder`.",
#             "project_folder",
#         )
#     return template
#
#
# def recipe_index(recipes_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
#     return {recipe["name"]: recipe for recipe in recipes_doc.get("recipes", []) or []}
#
#
# def import_recipes(template: dict[str, Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
#     recipes = recipe_index(recipes_doc)
#     imported: list[dict[str, Any]] = []
#     for idx, cohort in enumerate(template.get("cohorts", []) or []):
#         if not isinstance(cohort, dict):
#             result.error("invalid_cohort", "Each cohort must be a mapping.", f"cohorts[{idx}]")
#             continue
#         if "recipe" in cohort:
#             recipe_name = cohort["recipe"]
#             if recipe_name not in recipes:
#                 result.error("missing_recipe", f"Recipe `{recipe_name}` was not found.", f"cohorts[{idx}]")
#                 continue
#             merged = deep_merge(copy.deepcopy(recipes[recipe_name]), cohort)
#             merged["_recipe"] = recipe_name
#             merged.pop("recipe", None)
#         else:
#             merged = copy.deepcopy(cohort)
#         if not merged.get("name"):
#             merged["name"] = merged.get("dest_table") or merged.get("_recipe") or f"cohort_{idx + 1}"
#             result.warn("default_name", "Cohort had no name; a generated name was assigned.", f"cohorts[{idx}]")
#         if not merged.get("dest_table"):
#             merged["dest_table"] = merged["name"]
#             result.warn(
#                 "default_dest_table",
#                 f"`dest_table` defaulted to cohort name `{merged['name']}`.",
#                 merged["name"],
#             )
#         imported.append(merged)
#     return imported
#
#
# def deep_merge(base: Any, overlay: Any) -> Any:
#     if isinstance(base, dict) and isinstance(overlay, dict):
#         out = copy.deepcopy(base)
#         for key, value in overlay.items():
#             if key in out and isinstance(out[key], dict) and isinstance(value, dict):
#                 out[key] = deep_merge(out[key], value)
#             else:
#                 out[key] = copy.deepcopy(value)
#         return out
#     return copy.deepcopy(overlay)
#
#
# def output_path_for(template: dict[str, Any], suffix: str = OUTPUT_SUFFIX, out: str | None = None) -> Path:
#     if out:
#         return Path(out)
#     name = str(template.get("project_folder") or "finished").strip() or "finished"
#     clean = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
#     return project_root() / "YAMLs" / f"{clean}{suffix}.yaml"
#
#
# # =============================================================================
# # Inference
# # =============================================================================
#
#
# JINJA_EXPR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
# TABLE_ALIAS_RE = re.compile(r"##JVM_\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
# IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
#
#
# def iter_strings(value: Any, path: str = ""):
#     if isinstance(value, str):
#         yield path, value
#     elif isinstance(value, list):
#         for idx, item in enumerate(value):
#             yield from iter_strings(item, f"{path}[{idx}]")
#     elif isinstance(value, dict):
#         for key, item in value.items():
#             yield from iter_strings(item, f"{path}.{key}" if path else str(key))
#
#
# def infer_required_vars(cohort: dict[str, Any]) -> dict[str, list[str]]:
#     found: dict[str, list[str]] = {}
#     for path, text in iter_strings(cohort):
#         for expr in JINJA_EXPR_RE.findall(text):
#             for var in vars_from_expr(expr):
#                 found.setdefault(var, []).append(path)
#     return found
#
#
# def vars_from_expr(expr: str) -> set[str]:
#     expr = expr.strip()
#     if expr.startswith("sql_condition"):
#         inside = expr[len("sql_condition") :].strip()
#         match = re.match(r"^\((.*)\)$", inside)
#         if not match:
#             return set()
#         args = split_args(match.group(1))
#         if len(args) >= 2:
#             return {args[1].strip()}
#         return set()
#     if "|" in expr:
#         left = expr.split("|", 1)[0].strip()
#         return {left} if IDENT_RE.fullmatch(left) else set()
#     ignored = {"sql_condition", "true", "false", "none", "null"}
#     return {tok for tok in IDENT_RE.findall(expr) if tok not in ignored}
#
#
# def split_args(arg_text: str) -> list[str]:
#     args: list[str] = []
#     current: list[str] = []
#     quote: str | None = None
#     for ch in arg_text:
#         if quote:
#             current.append(ch)
#             if ch == quote:
#                 quote = None
#             continue
#         if ch in ("'", '"'):
#             quote = ch
#             current.append(ch)
#         elif ch == ",":
#             args.append("".join(current).strip())
#             current = []
#         else:
#             current.append(ch)
#     if current or arg_text.endswith(","):
#         args.append("".join(current).strip())
#     return args
#
#
# def infer_table_inputs(cohort: dict[str, Any]) -> dict[str, dict[str, Any]]:
#     inputs: dict[str, dict[str, Any]] = {}
#     strings = list(iter_strings(cohort))
#     for path, text in strings:
#         for table_var, alias in TABLE_ALIAS_RE.findall(text):
#             inputs.setdefault(table_var, {"alias": alias, "paths": [], "required_columns": []})
#             inputs[table_var]["paths"].append(path)
#     full_text = "\n".join(text for _, text in strings)
#     for table_var, meta in inputs.items():
#         alias = re.escape(meta["alias"])
#         cols = sorted(set(re.findall(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)\b", full_text)))
#         meta["required_columns"] = cols
#     return inputs
#
#
# def output_columns(cohort: dict[str, Any]) -> list[str]:
#     cols: list[str] = []
#     for column in cohort.get("columns", []) or []:
#         if not isinstance(column, dict):
#             continue
#         name = column.get("name")
#         if not name and column.get("source"):
#             name = str(column["source"]).split(".")[-1]
#         if name:
#             cols.append(str(name))
#     return cols
#
#
# def analyze_cohorts(cohorts: list[dict[str, Any]]) -> dict[str, Any]:
#     required_vars = {}
#     table_inputs = {}
#     required_table_columns = {}
#     outputs = {}
#     for cohort in cohorts:
#         name = cohort.get("name")
#         required_vars[name] = infer_required_vars(cohort)
#         table_inputs[name] = infer_table_inputs(cohort)
#         required_table_columns[name] = {
#             var: meta["required_columns"] for var, meta in table_inputs[name].items()
#         }
#         outputs[cohort.get("dest_table", name)] = output_columns(cohort)
#     return {
#         "required_vars": required_vars,
#         "table_inputs": table_inputs,
#         "required_table_columns": required_table_columns,
#         "output_columns": outputs,
#     }
#
#
# # =============================================================================
# # Validation and variable resolution
# # =============================================================================
#
#
# def merge_vars(*scopes: dict[str, Any] | None) -> dict[str, Any]:
#     merged: dict[str, Any] = {}
#     for scope in scopes:
#         if scope:
#             merged.update(scope)
#     return merged
#
#
# def find_pk_table(cohorts: list[dict[str, Any]], result: CompileResult, group_key: str | None = None) -> str | None:
#     scope = [c for c in cohorts if group_key is None or c.get("_group_key", "") == group_key]
#     pk = [c for c in scope if str(c.get("type", "")).lower() == "pk"]
#     if len(pk) == 1:
#         return pk[0].get("dest_table") or pk[0].get("name")
#     if len(pk) > 1:
#         result.error(
#             "multiple_pk_cohorts",
#             "Cannot infer PKTable because multiple type: PK cohorts exist.",
#             ", ".join(str(c.get("name")) for c in pk),
#         )
#     return None
#
#
# def find_uploaded_pk_table(template: dict[str, Any], result: CompileResult) -> str | None:
#     pk_uploads = [
#         upload for upload in template.get("upload_cohorts", []) or []
#         if isinstance(upload, dict) and str(upload.get("type", "")).lower() == "pk"
#     ]
#     if len(pk_uploads) > 1:
#         result.error(
#             "multiple_uploaded_pk",
#             "Only one upload cohort may be marked `type: pk`.",
#             ", ".join(str(upload.get("name")) for upload in pk_uploads),
#         )
#         return None
#     if not pk_uploads:
#         return None
#     upload = pk_uploads[0]
#     if not upload.get("key_columns"):
#         result.error(
#             "uploaded_pk_missing_keys",
#             "Uploaded PK cohort must declare `key_columns`.",
#             str(upload.get("name")),
#         )
#     return str(upload.get("dest_table") or upload.get("name"))
#
#
# def validate_and_resolve(
#     template: dict[str, Any],
#     recipes_doc: dict[str, Any],
#     cohorts: list[dict[str, Any]],
#     analysis: dict[str, Any],
#     result: CompileResult,
#     base_dir: Path,
# ) -> list[dict[str, Any]]:
#     uploads = upload_index(template)
#     table_schemas: dict[str, list[str] | None] = {table: cols for table, cols in analysis["output_columns"].items()}
#     table_schemas.update(upload_schemas(template, uploads, result, base_dir))
#     uploaded_pk_table = find_uploaded_pk_table(template, result)
#     generated_pk = [c for c in cohorts if str(c.get("type", "")).lower() == "pk"]
#     if uploaded_pk_table and generated_pk:
#         result.error(
#             "uploaded_pk_with_generated_pk",
#             "A template may not define both an uploaded PK cohort and generated type: PK cohorts.",
#             uploaded_pk_table,
#         )
#     resolved_cohorts: list[dict[str, Any]] = []
#     for cohort in cohorts:
#         name = cohort.get("name")
#         pk_table = find_pk_table(cohorts, result, cohort.get("_group_key", "")) or uploaded_pk_table
#         auto_vars = {}
#         required = analysis["required_vars"].get(name, {})
#         if "PKTable" in required and "PKTable" not in (cohort.get("vars") or {}) and pk_table:
#             auto_vars["PKTable"] = pk_table
#         vars_for_cohort = merge_vars(template.get("vars"), upload_vars(template), auto_vars, cohort.get("vars"))
#         for var, paths in required.items():
#             if var not in vars_for_cohort:
#                 result.error(
#                     "missing_variable",
#                     f"Cohort `{name}` requires variable `{var}`, but no value was provided.",
#                     ", ".join(paths),
#                 )
#         for table_var, cols in analysis["required_table_columns"].get(name, {}).items():
#             table_name = vars_for_cohort.get(table_var)
#             if not table_name:
#                 continue
#             table_name = str(table_name)
#             if table_name not in table_schemas:
#                 result.error(
#                     "missing_input_table",
#                     f"Cohort `{name}` uses `{table_var}={table_name}`, but no cohort/upload table provides it.",
#                     table_var,
#                 )
#                 continue
#             if table_schemas[table_name] is None:
#                 continue
#             missing = [col for col in cols if col not in table_schemas[table_name]]
#             if missing:
#                 result.error(
#                     "missing_input_column",
#                     f"Cohort `{name}` uses `{table_var}={table_name}`, but `{table_name}` is missing columns: {', '.join(missing)}.",
#                     table_var,
#                 )
#         resolved = copy.deepcopy(cohort)
#         resolved["_resolved_vars"] = vars_for_cohort
#         resolved_cohorts.append(resolved)
#     validate_upload_references(template, uploads, analysis, result, base_dir)
#     validate_multipliers(template, cohorts, table_schemas, result)
#     validate_batching(template, recipes_doc, cohorts, table_schemas, result)
#     return resolved_cohorts
#
#
# def upload_index(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
#     uploads = {}
#     for upload in template.get("upload_cohorts", []) or []:
#         if isinstance(upload, dict) and upload.get("name"):
#             item = copy.deepcopy(upload)
#             item.setdefault("dest_table", item["name"])
#             item.setdefault("scope", "global")
#             item.setdefault("push_this_cycle", True)
#             uploads[item["dest_table"]] = item
#             uploads[item["name"]] = item
#     return uploads
#
#
# def upload_vars(template: dict[str, Any]) -> dict[str, Any]:
#     # Upload variables are primarily user-defined under vars. This hook exists
#     # for future derived aliases.
#     return {}
#
#
# def upload_schemas(
#     template: dict[str, Any],
#     uploads: dict[str, dict[str, Any]],
#     result: CompileResult,
#     base_dir: Path,
# ) -> dict[str, list[str] | None]:
#     schemas: dict[str, list[str] | None] = {}
#     seen: set[int] = set()
#     for upload in uploads.values():
#         ident = id(upload)
#         if ident in seen:
#             continue
#         seen.add(ident)
#         dest = str(upload.get("dest_table") or upload.get("name"))
#         file_type = str(upload.get("file_type", "")).lower()
#         if file_type == "csv" and upload.get("file_loc"):
#             file_path = resolve_file(base_dir, upload["file_loc"])
#             if not file_path.exists():
#                 result.error("missing_upload_file", f"Upload file not found: {file_path}", dest)
#                 schemas[dest] = None
#                 continue
#             try:
#                 with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
#                     reader = csv.reader(handle)
#                     schemas[dest] = next(reader, [])
#             except Exception as exc:
#                 result.error("upload_read_error", f"Could not read upload CSV `{file_path}`: {exc}", dest)
#                 schemas[dest] = []
#         elif file_type in ("dbtable", "parquet"):
#             schema = upload.get("columns") or upload.get("schema") or []
#             if schema and isinstance(schema[0], dict):
#                 schemas[dest] = [str(c.get("name")) for c in schema if c.get("name")]
#             else:
#                 schemas[dest] = [str(c) for c in schema] if schema else []
#                 if not schema:
#                     result.warn("upload_schema_unknown", f"Upload `{dest}` has no locally discoverable schema.", dest)
#         else:
#             schemas[dest] = []
#     return schemas
#
#
# def resolve_file(base_dir: Path, file_loc: str) -> Path:
#     path = Path(file_loc)
#     if path.is_absolute():
#         return path
#     candidates = [base_dir / path, project_root() / path, project_root() / "YAMLs" / path]
#     for candidate in candidates:
#         if candidate.exists():
#             return candidate
#     return candidates[0]
#
#
# def validate_upload_references(
#     template: dict[str, Any],
#     uploads: dict[str, dict[str, Any]],
#     analysis: dict[str, Any],
#     result: CompileResult,
#     base_dir: Path,
# ) -> None:
#     referenced = set()
#     for cohort_inputs in analysis.get("table_inputs", {}).values():
#         for table_var in cohort_inputs:
#             value = template.get("vars", {}).get(table_var)
#             if value:
#                 referenced.add(str(value))
#     for name in referenced:
#         upload = uploads.get(name)
#         if upload and upload.get("push_this_cycle") is False and not upload.get("assume_exists"):
#             result.error(
#                 "upload_not_pushed",
#                 f"Upload `{name}` is referenced but has push_this_cycle: false.",
#                 name,
#             )
#
#
# # =============================================================================
# # Rendering
# # =============================================================================
#
#
# def render_sql_condition(column: str, value: Any, result: CompileResult, context: str = "") -> str:
#     values = value if isinstance(value, list) else [value]
#     values = [str(v) for v in values]
#     has_wildcard = any(any(ch in v for ch in WILDCARD_CHARS) for v in values)
#     for v in values:
#         if "_" in v and has_wildcard:
#             result.warn(
#                 "like_underscore",
#                 f"`_` in `{v}` will be treated as a SQL LIKE single-character wildcard. Use [_] for a literal underscore.",
#                 context,
#             )
#     if has_wildcard:
#         parts = [f"{column} LIKE {sql_quote(v)}" for v in values]
#         return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
#     if len(values) == 1:
#         return f"{column} = {sql_quote(values[0])}"
#     return f"{column} IN ({', '.join(sql_quote(v) for v in values)})"
#
#
# def sql_quote(value: str) -> str:
#     return "'" + value.replace("'", "''") + "'"
#
#
# def render_value(value: Any, vars_for_cohort: dict[str, Any], result: CompileResult, context: str = "") -> Any:
#     if isinstance(value, str):
#         return render_string(value, vars_for_cohort, result, context)
#     if isinstance(value, list):
#         return [render_value(item, vars_for_cohort, result, f"{context}[{idx}]") for idx, item in enumerate(value)]
#     if isinstance(value, dict):
#         return {k: render_value(v, vars_for_cohort, result, f"{context}.{k}" if context else str(k)) for k, v in value.items()}
#     return value
#
#
# def render_string(text: str, vars_for_cohort: dict[str, Any], result: CompileResult, context: str = "") -> str:
#     def repl(match: re.Match[str]) -> str:
#         expr = match.group(1).strip()
#         if expr.startswith("sql_condition"):
#             args = split_args(re.match(r"^sql_condition\((.*)\)$", expr).group(1)) if re.match(r"^sql_condition\((.*)\)$", expr) else []
#             if len(args) < 2:
#                 result.error("bad_sql_condition", f"Could not parse sql_condition expression `{expr}`.", context)
#                 return match.group(0)
#             column = strip_quotes(args[0])
#             var_name = args[1].strip()
#             if var_name not in vars_for_cohort:
#                 result.error("missing_variable", f"`sql_condition` references missing variable `{var_name}`.", context)
#                 return match.group(0)
#             return render_sql_condition(column, vars_for_cohort[var_name], result, context)
#         if "|" in expr and "sql_condition" in expr:
#             var_name, rest = [part.strip() for part in expr.split("|", 1)]
#             match_args = re.search(r"sql_condition\((.*)\)", rest)
#             if match_args and var_name in vars_for_cohort:
#                 column = strip_quotes(split_args(match_args.group(1))[0])
#                 return render_sql_condition(column, vars_for_cohort[var_name], result, context)
#         if expr in vars_for_cohort:
#             return str(vars_for_cohort[expr])
#         result.error("missing_variable", f"Missing variable `{expr}`.", context)
#         return match.group(0)
#
#     return JINJA_EXPR_RE.sub(repl, text)
#
#
# def strip_quotes(text: str) -> str:
#     text = text.strip()
#     if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
#         return text[1:-1]
#     return text
#
#
# def render_cohorts(cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
#     rendered = []
#     for cohort in cohorts:
#         vars_for_cohort = cohort.get("_resolved_vars", {})
#         clean = {k: v for k, v in cohort.items() if not k.startswith("_")}
#         rendered.append(render_value(clean, vars_for_cohort, result, str(cohort.get("name"))))
#     return rendered
#
#
# # =============================================================================
# # Expansion: multipliers, batching, cosmos
# # =============================================================================
#
#
# def expand_multipliers(template: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
#     multipliers = template.get("multipliers", []) or []
#     if not multipliers:
#         return cohorts
#     level_sets = []
#     for mult in multipliers:
#         levels = mult.get("levels", []) if isinstance(mult, dict) else []
#         level_sets.append([(mult, level) for level in levels])
#     expanded: list[dict[str, Any]] = []
#     for combo in product(*level_sets):
#         prefix = "".join(str(level.get("strat", "")) for _, level in combo)
#         group_vars: dict[str, Any] = {}
#         group_meta: list[dict[str, Any]] = []
#         for mult, level in combo:
#             if mult.get("stage") == "during_build":
#                 group_vars.update(level.get("vars") or {})
#             group_meta.append({"name": mult.get("name"), "stage": mult.get("stage"), "level": level})
#         for cohort in cohorts:
#             new = copy.deepcopy(cohort)
#             base_name = str(new.get("name"))
#             base_dest = str(new.get("dest_table", base_name))
#             new["name"] = f"{prefix}{base_name}" if prefix else base_name
#             new["dest_table"] = f"{prefix}{base_dest}" if prefix else base_dest
#             new["vars"] = merge_vars(group_vars, new.get("vars"))
#             new["_group_key"] = prefix
#             new["_multiplier_group"] = group_meta
#             split_filters = [
#                 split_after_build_filter(mult, level, result)
#                 for mult, level in combo
#                 if mult.get("stage") == "split_after_build"
#                 and mult.get("applies_to") == "PKTable"
#                 and str(new.get("type", "")).lower() == "pk"
#             ]
#             split_filters = [item for item in split_filters if item]
#             if split_filters:
#                 new["split_after_build"] = split_filters
#             expanded.append(new)
#     return expanded
#
#
# def split_after_build_filter(mult: dict[str, Any], level: dict[str, Any], result: CompileResult) -> dict[str, Any] | None:
#     if not isinstance(level, dict):
#         return None
#     item = {
#         "multiplier": mult.get("name"),
#         "strat": level.get("strat"),
#         "applies_to": mult.get("applies_to"),
#     }
#     for key in ("role", "row_mult"):
#         if key in level:
#             item[key] = level[key]
#     if level.get("column") and "values" in level:
#         values = level.get("values")
#         item["column"] = level["column"]
#         item["values"] = values
#         item["condition"] = render_sql_condition(f"pk.{level['column']}", values, result, str(level.get("strat")))
#     elif level.get("where"):
#         item["where"] = level["where"]
#     return item
#
#
# def validate_multipliers(template: dict[str, Any], cohorts: list[dict[str, Any]], table_schemas: dict[str, list[str] | None], result: CompileResult) -> None:
#     pk_candidates = [c.get("dest_table") for c in cohorts if str(c.get("type", "")).lower() == "pk"]
#     for mult in template.get("multipliers", []) or []:
#         if not isinstance(mult, dict):
#             continue
#         stage = mult.get("stage")
#         if stage not in ("during_build", "split_after_build"):
#             result.error("bad_multiplier_stage", f"Unsupported multiplier stage `{stage}`.", str(mult.get("name")))
#         if stage == "split_after_build":
#             targets = pk_candidates if mult.get("applies_to") == "PKTable" else [mult.get("applies_to")]
#             target_cols = sorted({col for target in targets for col in (table_schemas.get(str(target)) or [])})
#             for level in mult.get("levels", []) or []:
#                 col = level.get("column") if isinstance(level, dict) else None
#                 if col and col not in target_cols:
#                     result.error(
#                         "missing_split_column",
#                         f"Multiplier `{mult.get('name')}` references missing column `{col}` on `{mult.get('applies_to')}`.",
#                         str(level.get("strat")),
#                     )
#
#
# def expand_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
#     normalized = normalize_batching(template.get("batching", []) or [], recipes_doc, result)
#     for cohort in cohorts:
#         cohort["batching"] = normalized
#     return cohorts
#
#
# def normalize_batching(batch_items: list[Any], recipes_doc: dict[str, Any], result: CompileResult) -> list[dict[str, Any]]:
#     presets = {item["name"]: item for item in recipes_doc.get("batching_recipes", []) or [] if isinstance(item, dict) and item.get("name")}
#     normalized = []
#     for item in batch_items:
#         if isinstance(item, int):
#             normalized.append({"name": "chunk", "kind": "row_chunk", "rows_per_batch": item, "applies_to": "PKTable"})
#         elif isinstance(item, dict) and "chunk" in item:
#             normalized.append({"name": "chunk", "kind": "row_chunk", "rows_per_batch": item["chunk"], "applies_to": "PKTable"})
#         elif isinstance(item, str) and item in presets:
#             normalized.append(copy.deepcopy(presets[item]))
#         elif isinstance(item, dict) and len(item) == 1 and next(iter(item)) in presets:
#             name = next(iter(item))
#             merged = deep_merge(presets[name], item[name] or {})
#             normalized.append(merged)
#         elif isinstance(item, dict) and item.get("name"):
#             normalized.append(item)
#         else:
#             result.error("bad_batching", f"Could not understand batching item `{item}`.", "batching")
#     return normalized
#
#
# # =============================================================================
# # Data dictionary validation
# # =============================================================================
#
#
# def default_datadictionary_path() -> Path:
#     return project_root() / "YAMLs" / "datadictionary.yaml"
#
#
# # Dictionary types are abstract and annotated ("bigint (foreign key to ...)");
# # cohorts declare T-SQL. Compare families, not literals. Widening is accepted
# # because it cannot lose data; narrowing is not.
# TYPE_FAMILIES: dict[str, set[str]] = {
#     "bigint": {"BIGINT"},
#     "integer": {"INT", "SMALLINT", "TINYINT", "BIGINT"},
#     "string": {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT"},
#     "boolean": {"BIT"},
#     "numeric": {"DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY", "SMALLMONEY"},
#     "datetime": {"DATETIME", "DATETIME2", "SMALLDATETIME", "DATE"},
#     "date/datetime": {"DATE", "DATETIME", "DATETIME2", "SMALLDATETIME"},
#     "date": {"DATE", "DATETIME", "DATETIME2"},
#     "time": {"TIME"},
# }
#
# # `PatientDim AS p`, `INNER JOIN X AS y ON ...`, `BirthFact as bf`
# _ALIAS_PATTERN = re.compile(
#     # Braces are allowed so an unsubstituted `##JVM_{{PKTable}}` still binds
#     # its alias, rather than looking like an undeclared one.
#     r"(?:\bFROM\s+|\bJOIN\s+|^)\s*(?P<table>\[[^\]]+\]|[A-Za-z_#@][\w@$#.{}]*)\s+AS\s+(?P<alias>\w+)",
#     re.IGNORECASE,
# )
# # Only a bare `alias.Column` source can be resolved to a dictionary entry.
# _SIMPLE_SOURCE = re.compile(r"^(?P<alias>\w+)\.(?P<column>\w+)$")
#
#
# def dictionary_family(raw_type: Any) -> str:
#     """`bigint (foreign key to PatientDim.DurableKey)` -> `bigint`."""
#     return str(raw_type or "").split("(")[0].strip().lower()
#
#
# def tsql_base_type(declared: Any) -> str:
#     """`VARCHAR(400)` -> `VARCHAR`."""
#     return str(declared or "").split("(")[0].strip().upper()
#
#
# def is_generated_reference(table: str) -> bool:
#     """Temp tables and unresolved placeholders are not dictionary entries."""
#     return table.startswith("#") or "{{" in table
#
#
# def filter_text_parts(cohort: dict[str, Any]) -> list[str]:
#     block = cohort.get("filter") or {}
#     parts: list[str] = []
#     for key in ("from", "join"):
#         value = block.get(key)
#         if isinstance(value, str):
#             parts.append(value)
#         elif isinstance(value, list):
#             parts.extend(str(item) for item in value)
#     return parts
#
#
# def cohort_aliases(cohort: dict[str, Any]) -> dict[str, str]:
#     """Map each alias declared in `from`/`join` to its table."""
#     aliases: dict[str, str] = {}
#     for part in filter_text_parts(cohort):
#         for match in _ALIAS_PATTERN.finditer(part):
#             table = match.group("table").strip("[]")
#             aliases[match.group("alias")] = table
#     return aliases
#
#
# def validate_data_dictionary(
#     cohorts: list[dict[str, Any]],
#     dictionary: dict[str, Any] | None,
#     result: CompileResult,
# ) -> None:
#     """Check every cohort column against the data dictionary.
#
#     An unknown table is a hard error rather than a warning: it usually means a
#     table name was invented or left as pseudocode, and the dictionary is meant
#     to stay complete, so the fix is to add the table rather than route around
#     the check.
#     """
#     if not dictionary:
#         return
#
#     for cohort in cohorts:
#         if not isinstance(cohort, dict):
#             continue
#         label = str(cohort.get("dest_table") or cohort.get("name") or "cohort")
#         aliases = cohort_aliases(cohort)
#
#         for table in sorted(set(aliases.values())):
#             if is_generated_reference(table):
#                 continue
#             if table not in dictionary:
#                 result.error(
#                     "unknown_table",
#                     f"Table `{table}` is not in the data dictionary. Add it to "
#                     f"YAMLs/datadictionary.yaml, or correct the name.",
#                     label,
#                 )
#
#         for column in cohort.get("columns") or []:
#             if not isinstance(column, dict):
#                 continue
#             source = str(column.get("source") or "").strip()
#             if not source:
#                 continue
#             match = _SIMPLE_SOURCE.match(source)
#             if not match:
#                 result.warn(
#                     "dd_source_not_checked",
#                     f"Source `{source}` is not a plain `alias.Column`, so its type "
#                     f"cannot be checked against the data dictionary.",
#                     label,
#                 )
#                 continue
#
#             alias, column_name = match.group("alias"), match.group("column")
#             table = aliases.get(alias)
#             if table is None:
#                 result.error(
#                     "unknown_alias",
#                     f"Source `{source}` uses alias `{alias}`, which is not declared "
#                     f"in this cohort's `from` or `join`.",
#                     label,
#                 )
#                 continue
#             if is_generated_reference(table) or table not in dictionary:
#                 continue
#
#             dd_columns = (dictionary[table] or {}).get("columns") or {}
#             if column_name not in dd_columns:
#                 result.error(
#                     "unknown_column",
#                     f"Column `{column_name}` is not listed under `{table}` in the "
#                     f"data dictionary. Check the alias and the spelling.",
#                     label,
#                 )
#                 continue
#
#             declared = column.get("type")
#             if not declared:
#                 continue
#             family = dictionary_family((dd_columns[column_name] or {}).get("type"))
#             accepted = TYPE_FAMILIES.get(family)
#             if accepted is None:
#                 result.warn(
#                     "dd_unknown_family",
#                     f"Data dictionary type `{family}` for `{table}.{column_name}` is "
#                     f"not a family this checker knows, so `{declared}` was not verified.",
#                     label,
#                 )
#                 continue
#             if tsql_base_type(declared) not in accepted:
#                 result.error(
#                     "dd_type_mismatch",
#                     f"`{source}` is declared `{declared}`, but the data dictionary "
#                     f"says `{table}.{column_name}` is "
#                     f"`{(dd_columns[column_name] or {}).get('type')}`.",
#                     label,
#                 )
#
#
# _DATADICT_CACHE: dict[tuple[str, float], dict[str, Any]] = {}
#
#
# def load_datadictionary(path: str | Path | None, result: CompileResult) -> dict[str, Any] | None:
#     """Load the dictionary, warning rather than failing when it is absent.
#
#     Cached by path and mtime: it is a few thousand lines and is otherwise
#     reparsed on every compile, including once per test.
#     """
#     dict_path = Path(path) if path else default_datadictionary_path()
#     if not dict_path.is_file():
#         result.warn(
#             "datadictionary_missing",
#             f"No data dictionary at {dict_path}; column types were not verified.",
#             str(dict_path),
#         )
#         return None
#     cache_key = (str(dict_path.resolve()), dict_path.stat().st_mtime)
#     cached = _DATADICT_CACHE.get(cache_key)
#     if cached is not None:
#         return cached
#     try:
#         doc = load_yaml(dict_path) or {}
#     except Exception as exc:
#         result.warn("datadictionary_unreadable", str(exc), str(dict_path))
#         return None
#     entries = doc.get("DataDictionary") if isinstance(doc, dict) else None
#     if not isinstance(entries, dict):
#         result.warn(
#             "datadictionary_malformed",
#             f"{dict_path} has no `DataDictionary` mapping; column types were not verified.",
#             str(dict_path),
#         )
#         return None
#     _DATADICT_CACHE[cache_key] = entries
#     return entries
#
#
#
# def validate_batching(template: dict[str, Any], recipes_doc: dict[str, Any], cohorts: list[dict[str, Any]], table_schemas: dict[str, list[str] | None], result: CompileResult) -> None:
#     normalized = normalize_batching(template.get("batching", []) or [], recipes_doc, result)
#     pk_candidates = [c.get("dest_table") for c in cohorts if str(c.get("type", "")).lower() == "pk"]
#     pk_cols = sorted({col for pk_table in pk_candidates for col in (table_schemas.get(str(pk_table)) or [])})
#     for item in normalized:
#         if item.get("kind") == "row_chunk":
#             continue
#         col = item.get("column")
#         if col and col not in pk_cols:
#             result.error("missing_batch_column", f"Batching `{item.get('name')}` requires missing PK column `{col}`.", "PKTable")
#
#
# def expand_cosmos(template: dict[str, Any], cohorts: list[dict[str, Any]], result: CompileResult) -> list[dict[str, Any]]:
#     cosmos = str(template.get("cosmos_db", "COSMOS"))
#     value = cosmos.lower()
#     if value in ("cosmos",):
#         return cohorts
#     if value in ("cosmos_sneakpeek", "sneakpeek", "sp"):
#         return [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek") for c in cohorts]
#     if value in ("dual", "both"):
#         return cohorts + [with_cosmos_suffix(c, "_sp", "COSMOS_SneakPeek") for c in cohorts]
#     result.error("bad_cosmos_db", f"Unsupported cosmos_db value `{cosmos}`.", "cosmos_db")
#     return cohorts
#
#
# def validate_cosmos(template: dict[str, Any], result: CompileResult) -> None:
#     value = str(template.get("cosmos_db", "COSMOS")).lower()
#     if value not in ("cosmos", "cosmos_sneakpeek", "sneakpeek", "sp", "dual", "both"):
#         result.error("bad_cosmos_db", f"Unsupported cosmos_db value `{template.get('cosmos_db')}`.", "cosmos_db")
#
#
# def with_cosmos_suffix(cohort: dict[str, Any], suffix: str, cosmos_db: str) -> dict[str, Any]:
#     new = copy.deepcopy(cohort)
#     new["name"] = f"{new.get('name')}{suffix}"
#     new["dest_table"] = f"{new.get('dest_table', new.get('name'))}{suffix}"
#     new["cosmos_db"] = cosmos_db
#     return new
#
#
# # =============================================================================
# # Reports
# # =============================================================================
#
#
# def build_report(result: CompileResult) -> str:
#     lines = ["# Manager Report", ""]
#     lines.append(f"OK: {result.ok}")
#     lines.append("")
#     lines.append("## Errors")
#     if result.errors:
#         for msg in result.errors:
#             lines.append(f"- `{msg.code}`: {msg.message} {msg.context}".rstrip())
#     else:
#         lines.append("- None")
#     lines.append("")
#     lines.append("## Warnings")
#     if result.warnings:
#         for msg in result.warnings:
#             lines.append(f"- `{msg.code}`: {msg.message} {msg.context}".rstrip())
#     else:
#         lines.append("- None")
#     lines.append("")
#     lines.append("## Expanded Cohorts")
#     for cohort in result.finished_yaml.get("cohorts", []) or []:
#         lines.append(f"- {cohort.get('name')} -> {cohort.get('dest_table')}")
#     lines.append("")
#     lines.append("## Required Columns")
#     for cohort, cols in result.analysis.get("required_table_columns", {}).items():
#         lines.append(f"- {cohort}: {cols}")
#     return "\n".join(lines) + "\n"
#
#
# # =============================================================================
# # Public API
# # =============================================================================
#
#
# def compile_yaml(
#     template_path: str | Path | None = None,
#     recipes_path: str | Path | None = None,
#     output_path: str | Path | None = None,
#     suffix: str = OUTPUT_SUFFIX,
#     write: bool = False,
#     report_path: str | Path | None = None,
#     datadictionary_path: str | Path | None = None,
# ) -> CompileResult:
#     result = CompileResult()
#     template_path = Path(template_path) if template_path else default_template_path()
#     recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
#     try:
#         template = normalize_template(load_yaml(template_path), result)
#         recipes_doc = load_yaml(recipes_path) or {}
#     except Exception as exc:
#         result.error("yaml_load_error", str(exc), str(template_path))
#         return result
#
#     cohorts = import_recipes(template, recipes_doc, result)
#     cohorts = expand_multipliers(template, cohorts, result)
#     analysis = analyze_cohorts(cohorts)
#     cohorts = validate_and_resolve(template, recipes_doc, cohorts, analysis, result, template_path.parent)
#     validate_cosmos(template, result)
#     if result.errors:
#         rendered_cohorts = [{k: v for k, v in cohort.items() if not k.startswith("_")} for cohort in cohorts]
#     else:
#         rendered_cohorts = render_cohorts(cohorts, result)
#         # Checked after rendering so template variables are already substituted,
#         # and before expansion so each real cohort reports once rather than once
#         # per multiplier and Cosmos variant.
#         validate_data_dictionary(
#             rendered_cohorts, load_datadictionary(datadictionary_path, result), result
#         )
#         rendered_cohorts = expand_batching(template, recipes_doc, rendered_cohorts, result)
#         rendered_cohorts = expand_cosmos(template, rendered_cohorts, result)
#
#     finished = copy.deepcopy(template)
#     finished["cohorts"] = rendered_cohorts
#     finished.pop("example_cohorts", None)
#     result.finished_yaml = finished
#     result.analysis = analysis
#     out_path = Path(output_path) if output_path else output_path_for(template, suffix)
#     result.output_path = str(out_path)
#     if write and result.ok:
#         dump_yaml(finished, out_path)
#     if report_path:
#         report_out = Path(report_path)
#         report_out.parent.mkdir(parents=True, exist_ok=True)
#         report_out.write_text(build_report(result), encoding="utf-8")
#     return result
#
#
# def validate_yaml(template_path: str | Path | None = None, recipes_path: str | Path | None = None) -> CompileResult:
#     return compile_yaml(template_path=template_path, recipes_path=recipes_path, write=False)
#
#
# def inspect_recipes(recipes_path: str | Path | None = None) -> CompileResult:
#     result = CompileResult()
#     recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
#     try:
#         recipes_doc = load_yaml(recipes_path) or {}
#     except Exception as exc:
#         result.error("yaml_load_error", str(exc), str(recipes_path))
#         return result
#     result.analysis = {
#         "recipes": [r.get("name") for r in recipes_doc.get("recipes", []) or []],
#         "batching_recipes": [r.get("name") for r in recipes_doc.get("batching_recipes", []) or []],
#     }
#     return result
#
#
# def safe_id(value: Any, fallback: str = "item") -> str:
#     text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
#     return text or fallback
#
#
# def project_metadata(template: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "name": template.get("project_folder") or template.get("project_db") or "YAML Manager Project",
#         "project_folder": template.get("project_folder"),
#         "project_db": template.get("project_db"),
#         "created_by": "yamlmanager",
#     }
#
#
# def uploaded_pk_source(template: dict[str, Any], result: CompileResult) -> dict[str, Any] | None:
#     pk_uploads = [
#         upload for upload in template.get("upload_cohorts", []) or []
#         if isinstance(upload, dict) and str(upload.get("type", "")).lower() == "pk"
#     ]
#     if len(pk_uploads) > 1:
#         result.error(
#             "multiple_uploaded_pk",
#             "Only one upload cohort may be marked `type: pk`.",
#             ", ".join(str(upload.get("name")) for upload in pk_uploads),
#         )
#         return None
#     if not pk_uploads:
#         return None
#     upload = pk_uploads[0]
#     key_columns = upload.get("key_columns") or upload.get("columns") or []
#     if not key_columns:
#         result.error(
#             "uploaded_pk_missing_keys",
#             "Uploaded PK cohort must declare `key_columns`.",
#             str(upload.get("name")),
#         )
#     return {
#         "kind": "uploaded_cohort",
#         "upload_name": upload.get("name"),
#         "table": upload.get("dest_table") or upload.get("name"),
#         "key_columns": key_columns,
#     }
#
#
# def session_paths(session_id: str) -> dict[str, str]:
#     base = f"sessions/{session_id}"
#     return {
#         "setup": f"{base}/setup.yaml",
#         "upload_cohorts": f"{base}/upload_cohorts.yaml",
#         "pk": f"{base}/pk.yaml",
#         "run": f"{base}/runs/run.yaml",
#     }
#
#
# def batch_buckets(dim: dict[str, Any]) -> list[dict[str, Any]] | None:
#     """Plan-time buckets for one batching dimension.
#
#     Returns None when the buckets cannot be known until the PK table exists:
#     row chunks depend on the row count, and `values: all` needs a DISTINCT over
#     real data. Those dimensions stay logical for Pullmanager to materialize.
#     """
#     if str(dim.get("kind", "")).lower() == "row_chunk":
#         return None
#     values = dim.get("values")
#     if not isinstance(values, list) or not values:
#         return None
#     buckets = [{"value": value, "is_other": False} for value in values]
#     if dim.get("include_other"):
#         buckets.append({"value": None, "is_other": True})
#     return buckets
#
#
# def bucket_label(dim: dict[str, Any], bucket: dict[str, Any]) -> str:
#     if bucket.get("is_other"):
#         return safe_id(f"{dim.get('name') or 'batch'}-other", "other")
#     return safe_id(bucket.get("value"), "value")
#
#
# def resolved_dimension(dim: dict[str, Any], bucket: dict[str, Any]) -> dict[str, Any]:
#     resolved: dict[str, Any] = {
#         "name": dim.get("name"),
#         "kind": dim.get("kind") or "column_values",
#         "column": dim.get("column"),
#     }
#     if bucket.get("is_other"):
#         # The catch-all is defined by what it is not, so it has to carry the
#         # named values; a predicate for it cannot be built from `is_other` alone.
#         resolved["is_other"] = True
#         resolved["excludes"] = [v for v in (dim.get("values") or [])]
#     else:
#         resolved["value"] = bucket.get("value")
#     return resolved
#
#
# def session_runs(
#     session_id: str,
#     pk_cohort: dict[str, Any],
#     result: CompileResult | None = None,
# ) -> list[SplitRun]:
#     """One run per batch combination.
#
#     Batching dimensions multiply: state[LA, MS] x sex[Female, Male] is four
#     runs, each a disjoint slice of the cohort, not three runs describing three
#     different axes of the whole cohort.
#     """
#     base = f"sessions/{session_id}/runs"
#     dims = [
#         dim if isinstance(dim, dict) else {"name": str(dim)}
#         for dim in pk_cohort.get("batching") or []
#     ]
#     if not dims:
#         return [SplitRun(run_id=f"{session_id}__run", yaml=f"{base}/run.yaml")]
#
#     static: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
#     runtime: list[dict[str, Any]] = []
#     for dim in dims:
#         buckets = batch_buckets(dim)
#         if buckets is None:
#             runtime.append(copy.deepcopy(dim))
#         else:
#             static.append((dim, buckets))
#
#     if not static:
#         return [
#             SplitRun(
#                 run_id=f"{session_id}__run",
#                 yaml=f"{base}/run.yaml",
#                 batch={"name": "run", "dimensions": [], "runtime": runtime},
#             )
#         ]
#
#     runs: list[SplitRun] = []
#     seen: set[str] = set()
#     for combo in product(*[buckets for _, buckets in static]):
#         pairs = list(zip(static, combo))
#         name = safe_id("-".join(bucket_label(dim, bucket) for (dim, _), bucket in pairs), "batch")
#         if name in seen:
#             if result is not None:
#                 result.error(
#                     "duplicate_batch_name",
#                     f"Batch combination `{name}` is not unique in session `{session_id}`. "
#                     "Two batching dimensions produce the same label; rename a value.",
#                     session_id,
#                 )
#             continue
#         seen.add(name)
#         runs.append(
#             SplitRun(
#                 run_id=f"{session_id}__{name}",
#                 yaml=f"{base}/{name}.yaml",
#                 batch={
#                     "name": name,
#                     "dimensions": [resolved_dimension(dim, bucket) for (dim, _), bucket in pairs],
#                     "runtime": copy.deepcopy(runtime),
#                 },
#             )
#         )
#     return runs
#
#
# def session_multiplier_context(pk_cohort: dict[str, Any], session_id: str) -> dict[str, Any] | None:
#     context: dict[str, Any] = {"session_label": session_id}
#     if pk_cohort.get("split_after_build"):
#         context["split_after_build"] = copy.deepcopy(pk_cohort.get("split_after_build"))
#     return context if len(context) > 1 else None
#
#
# def build_split_plan_from_finished(
#     finished_yaml: dict[str, Any],
#     template_path: Path,
#     recipes_path: Path,
#     result: CompileResult,
# ) -> SplitPlan:
#     cohorts = finished_yaml.get("cohorts", []) or []
#     pk_source = uploaded_pk_source(finished_yaml, result)
#     pk_cohorts = [cohort for cohort in cohorts if isinstance(cohort, dict) and str(cohort.get("type", "")).lower() == "pk"]
#     if not pk_cohorts:
#         if pk_source:
#             pk_cohorts = [{
#                 "name": pk_source.get("upload_name") or pk_source.get("table"),
#                 "dest_table": pk_source.get("table"),
#                 "type": "PK",
#             }]
#         else:
#             session_id = safe_id(finished_yaml.get("project_folder") or finished_yaml.get("project_db"), "default")
#             pk_cohorts = [{"name": session_id, "dest_table": None}]
#
#     sessions: list[SplitSession] = []
#     for pk_cohort in pk_cohorts:
#         pk_name = str(pk_cohort.get("name") or pk_cohort.get("dest_table") or "PKTable")
#         pk_table = pk_cohort.get("dest_table") or pk_cohort.get("name")
#         session_id = safe_id(pk_table or pk_name, "session")
#         paths = session_paths(session_id)
#         source = pk_source or {"kind": "generated", "table": pk_table}
#         phases = {
#             "setup": SplitPhase("setup", paths["setup"]),
#             "upload_cohorts": SplitPhase("upload_cohorts", paths["upload_cohorts"]),
#             "pk": SplitPhase("pk", paths["pk"], pk_source=source),
#         }
#         runs = session_runs(session_id, pk_cohort, result)
#         sessions.append(
#             SplitSession(
#                 session_id=session_id,
#                 cohort=pk_name,
#                 pk_table=str(pk_table) if pk_table else None,
#                 phases=phases,
#                 runs=runs,
#                 multiplier=session_multiplier_context(pk_cohort, session_id),
#             )
#         )
#
#     return SplitPlan(
#         project=project_metadata(finished_yaml),
#         source={
#             "template": str(template_path),
#             "recipes": str(recipes_path),
#         },
#         sessions=sessions,
#     )
#
#
# def plan_split_runs(
#     template_path: str | Path | None = None,
#     recipes_path: str | Path | None = None,
# ) -> CompileResult:
#     template_path = Path(template_path) if template_path else default_template_path()
#     recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
#     result = compile_yaml(template_path=template_path, recipes_path=recipes_path, write=False)
#     if result.errors:
#         return result
#     plan = build_split_plan_from_finished(result.finished_yaml, template_path, recipes_path, result)
#     result.analysis["split_plan"] = plan.to_dict()
#     return result
#
#
# def build_pullmanifest(
#     template_path: str | Path | None = None,
#     recipes_path: str | Path | None = None,
#     output_path: str | Path | None = None,
#     write: bool = False,
# ) -> CompileResult:
#     result = plan_split_runs(template_path=template_path, recipes_path=recipes_path)
#     if result.errors:
#         return result
#     manifest = result.analysis.get("split_plan", {})
#     result.finished_yaml = manifest
#     out_path = Path(output_path) if output_path else project_root() / DEFAULT_MANIFEST_PATH
#     result.output_path = str(out_path)
#     if write and result.ok:
#         dump_yaml(manifest, out_path)
#     return result
#
#
# def split_base_document(finished_yaml: dict[str, Any]) -> dict[str, Any]:
#     doc = copy.deepcopy(finished_yaml)
#     doc.pop("cohorts", None)
#     doc.pop("multipliers", None)
#     doc.pop("batching", None)
#     doc.pop("example_cohorts", None)
#     return doc
#
#
# def split_pull_context(session: dict[str, Any], phase: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
#     context: dict[str, Any] = {
#         "session_id": session.get("session_id"),
#         "phase": phase,
#         "cohort": session.get("cohort"),
#         "pk_table": session.get("pk_table"),
#     }
#     if run is not None:
#         context["run_id"] = run.get("run_id")
#         if run.get("batch") is not None:
#             context["batch"] = run.get("batch")
#     if session.get("multiplier") is not None:
#         context["multiplier"] = session.get("multiplier")
#     pk_phase = (session.get("phases") or {}).get("pk") or {}
#     if pk_phase.get("pk_source") is not None:
#         context["pk_source"] = pk_phase.get("pk_source")
#     return context
#
#
# def split_phase_document(
#     finished_yaml: dict[str, Any],
#     session: dict[str, Any],
#     phase: str,
#     run: dict[str, Any] | None = None,
# ) -> dict[str, Any]:
#     doc = split_base_document(finished_yaml)
#     cohorts = finished_yaml.get("cohorts", []) or []
#     pk_table = session.get("pk_table")
#     pk_cohorts = [
#         cohort for cohort in cohorts
#         if isinstance(cohort, dict)
#         and str(cohort.get("type", "")).lower() == "pk"
#         and (pk_table is None or cohort.get("dest_table") == pk_table or cohort.get("name") == pk_table)
#     ]
#     fact_cohorts = [
#         cohort for cohort in cohorts
#         if isinstance(cohort, dict) and str(cohort.get("type", "")).lower() != "pk"
#     ]
#     doc["pull_context"] = split_pull_context(session, phase, run)
#     if phase == "upload_cohorts":
#         doc["upload_cohorts"] = copy.deepcopy(finished_yaml.get("upload_cohorts", []) or [])
#         doc["cohorts"] = []
#     elif phase == "pk":
#         doc["cohorts"] = copy.deepcopy(pk_cohorts)
#     elif phase == "run":
#         doc["cohorts"] = copy.deepcopy(fact_cohorts)
#     else:
#         doc["cohorts"] = []
#     return doc
#
#
# UPLOAD_STAGING_DIR = "uploads"
#
#
# def stage_upload_files(
#     finished_yaml: dict[str, Any],
#     template_path: Path,
#     out_dir: Path,
#     result: CompileResult,
# ) -> None:
#     """Copy upload files into the split folder and repoint `file_loc` at them.
#
#     `file_loc` is written relative to the template, but the split folder is
#     what travels to the VM, and Pullmanager resolves relative to the manifest.
#     Without this the two anchors disagree and every upload fails to open on
#     the far side. Copying makes the split folder self-contained.
#     """
#     uploads = finished_yaml.get("upload_cohorts") or []
#     if not uploads:
#         return
#     staging = out_dir / UPLOAD_STAGING_DIR
#     for upload in uploads:
#         if not isinstance(upload, dict):
#             continue
#         file_loc = upload.get("file_loc")
#         if not file_loc:
#             continue
#         source = Path(str(file_loc))
#         if not source.is_absolute():
#             source = template_path.parent / source
#         if not source.is_file():
#             result.warn(
#                 "upload_file_not_staged",
#                 f"Upload file {source} could not be copied into the split folder; "
#                 "Pullmanager will not find it.",
#                 str(upload.get("name")),
#             )
#             continue
#         staging.mkdir(parents=True, exist_ok=True)
#         target = staging / source.name
#         shutil.copyfile(source, target)
#         upload["file_loc"] = f"{UPLOAD_STAGING_DIR}/{source.name}"
#
#
# def write_split_artifacts(
#     template_path: str | Path | None = None,
#     recipes_path: str | Path | None = None,
#     output_dir: str | Path | None = None,
# ) -> CompileResult:
#     result = plan_split_runs(template_path=template_path, recipes_path=recipes_path)
#     if result.errors:
#         return result
#     out_dir = Path(output_dir) if output_dir else project_root() / DEFAULT_SPLIT_DIR
#     finished_yaml = copy.deepcopy(result.finished_yaml)
#     out_dir.mkdir(parents=True, exist_ok=True)
#     stage_upload_files(
#         finished_yaml,
#         Path(template_path) if template_path else default_template_path(),
#         out_dir,
#         result,
#     )
#     manifest = result.analysis.get("split_plan", {})
#     manifest_path = out_dir / "pullmanifest.yaml"
#     manifest_path.parent.mkdir(parents=True, exist_ok=True)
#     dump_yaml(manifest, manifest_path)
#
#     for session in manifest.get("sessions", []) or []:
#         phases = session.get("phases", {}) or {}
#         for phase_name in ("setup", "upload_cohorts", "pk"):
#             phase = phases.get(phase_name)
#             if not phase:
#                 continue
#             path = out_dir / phase["yaml"]
#             path.parent.mkdir(parents=True, exist_ok=True)
#             dump_yaml(split_phase_document(finished_yaml, session, phase_name), path)
#         for run in session.get("runs", []) or []:
#             path = out_dir / run["yaml"]
#             path.parent.mkdir(parents=True, exist_ok=True)
#             dump_yaml(split_phase_document(finished_yaml, session, "run", run), path)
#
#     result.finished_yaml = manifest
#     result.output_path = str(manifest_path)
#     result.analysis["split_output_dir"] = str(out_dir)
#     return result
#
#
# def public_cohort(cohort: dict[str, Any]) -> dict[str, Any]:
#     return {k: v for k, v in cohort.items() if not k.startswith("_")}
#
#
# def build_preyaml(
#     template_path: str | Path | None = None,
#     recipes_path: str | Path | None = None,
#     output_path: str | Path | None = None,
#     mode: str = "symbolic",
#     write: bool = False,
#     report_path: str | Path | None = None,
# ) -> CompileResult:
#     result = CompileResult()
#     template_path = Path(template_path) if template_path else default_template_path()
#     recipes_path = Path(recipes_path) if recipes_path else default_recipes_path()
#     try:
#         template = load_yaml(template_path) or {}
#     except Exception as exc:
#         result.error("yaml_load_error", str(exc), str(template_path))
#         return result
#     if not isinstance(template, dict):
#         result.error("invalid_template", "Template YAML must be a mapping.", str(template_path))
#         return result
#     if mode not in ("symbolic", "expanded-recipes"):
#         result.error("bad_preyaml_mode", f"Unsupported pre-YAML mode `{mode}`.", mode)
#         return result
#
#     if mode == "symbolic":
#         preyaml = copy.deepcopy(template)
#         suffix = PREYAML_SUFFIX
#     else:
#         try:
#             recipes_doc = load_yaml(recipes_path) or {}
#         except Exception as exc:
#             result.error("yaml_load_error", str(exc), str(recipes_path))
#             return result
#         normalized = normalize_template(template, result)
#         cohorts = import_recipes(normalized, recipes_doc, result)
#         preyaml = copy.deepcopy(normalized)
#         preyaml["cohorts"] = [public_cohort(cohort) for cohort in cohorts]
#         preyaml.pop("example_cohorts", None)
#         result.analysis = analyze_cohorts(cohorts)
#         suffix = EXPANDED_PREYAML_SUFFIX
#
#     result.finished_yaml = preyaml
#     out_path = Path(output_path) if output_path else output_path_for(template, suffix)
#     result.output_path = str(out_path)
#     if write and result.ok:
#         dump_yaml(preyaml, out_path)
#     if report_path:
#         report_out = Path(report_path)
#         report_out.parent.mkdir(parents=True, exist_ok=True)
#         report_out.write_text(build_report(result), encoding="utf-8")
#     return result
#
#
# # =============================================================================
# # Embedded TDD
# # =============================================================================
#
#
# def write_temp_yaml(tmp: Path, name: str, data: str) -> Path:
#     path = tmp / name
#     path.write_text(data.strip() + "\n", encoding="utf-8")
#     return path
#
#
# def tiny_recipes() -> str:
#     return """
# batching_recipes:
#   - name: state
#     kind: column_values
#     applies_to: PKTable
#     column: StateOrProvinceAbbreviation
#     values: all
#   - name: sex
#     kind: column_values
#     applies_to: PKTable
#     column: Sex
#     values: [Female, Male]
#   - name: chunk
#     kind: row_chunk
#     applies_to: PKTable
#     rows_per_batch: required
# recipes:
#   - name: PatientWithDx
#     type: PK
#     columns:
#       - source: dxf.PatientDurableKey
#         name: PatientDurableKey
#       - source: dxf.DiagnosisEventKey
#         name: DiagnosisEventKey
#       - source: p.FirstRace
#         name: FirstRace
#       - source: p.Sex
#         name: Sex
#       - source: p.StateOrProvinceAbbreviation
#         name: StateOrProvinceAbbreviation
#     filter:
#       from:
#         - DiagnosisEventFact AS dxf
#       join:
#         - "INNER JOIN DiagnosisTerminologyDim AS dt ON dt.DiagnosisKey = dxf.DiagnosisKey"
#         - "INNER JOIN PatientDim AS p ON p.DurableKey = dxf.PatientDurableKey"
#       where:
#         - "dxf.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
#         - "{{sql_condition('dt.Value', ICD_Value)}}"
#   - name: OtherDx
#     type: fact
#     columns:
#       - source: def.PatientDurableKey
#         name: PatientDurableKey
#     filter:
#       from:
#         - DiagnosisEventFact AS def
#       join:
#         - "INNER JOIN ##JVM_{{PKTable}} AS pk ON pk.PatientDurableKey = def.PatientDurableKey AND pk.DiagnosisEventKey <> def.DiagnosisEventKey"
#       where:
#         - "def.StartDateKey BETWEEN {{min_date_key}} AND {{max_date_key}}"
# """
#
#
# def uploaded_pk_template(extra_upload: str = "", key_columns: bool = True) -> str:
#     keys = "    key_columns: [PatientDurableKey, DiagnosisEventKey]\n" if key_columns else ""
#     return f"""
# project_folder: Uploaded PK
# cosmos_db: COSMOS
# vars:
#   min_date_key: 20200101
#   max_date_key: 20240101
# upload_cohorts:
#   - name: ClientPK
#     type: pk
#     dest_table: ClientPK
#     file_type: csv
#     file_loc: pks.csv
# {keys}{extra_upload}
# cohorts:
#   - recipe: OtherDx
#     name: OtherDx
# """
#
#
# def tiny_template(extra: str = "") -> str:
#     return f"""
# project_folder: Test Run
# cosmos_db: COSMOS
# vars:
#   min_date_key: 20200101
#   max_date_key: 20240101
#   ICD_Value:
#     - K50
#     - K51
# cohorts:
#   - recipe: PatientWithDx
#     name: Patients
#   - recipe: OtherDx
#     name: OtherDx
# {extra}
# """
#
#
# def load_yaml_from_text(text: str) -> Any:
#     with tempfile.TemporaryDirectory() as d:
#         path = write_temp_yaml(Path(d), "inline.yaml", text)
#         return load_yaml(path)
#
#
# def has_error(result: CompileResult, code: str) -> bool:
#     return any(msg.code == code for msg in result.errors)
#
#
# def has_warning(result: CompileResult, code: str) -> bool:
#     return any(msg.code == code for msg in result.warnings)
#
#
# def summarize_result(result: CompileResult) -> str:
#     bits = []
#     if result.errors:
#         bits.append("errors=" + json.dumps([m.to_dict() for m in result.errors]))
#     if result.warnings:
#         bits.append("warnings=" + json.dumps([m.to_dict() for m in result.warnings]))
#     return "; ".join(bits) or "ok"
#
#
# class MakeYamlTest(unittest.TestCase):
#     """Base case: a scratch dir plus the template/recipes boilerplate folded in."""
#
#     def setUp(self):
#         self._tmp = tempfile.TemporaryDirectory()
#         self.addCleanup(self._tmp.cleanup)
#         self.tmp = Path(self._tmp.name)
#
#     def write_pair(self, template: str | None = None, extra: str = "") -> tuple[Path, Path]:
#         text = tiny_template(extra) if template is None else template
#         return (
#             write_temp_yaml(self.tmp, "template.yaml", text),
#             write_temp_yaml(self.tmp, "recipes.yaml", tiny_recipes()),
#         )
#
#     def compile_template(self, template: str | None = None, extra: str = "") -> CompileResult:
#         return compile_yaml(*self.write_pair(template, extra))
#
#     def plan_split(self, template: str | None = None, extra: str = "") -> CompileResult:
#         return plan_split_runs(*self.write_pair(template, extra))
#
#     def runs_for(self, extra: str = "") -> tuple[CompileResult, list[dict[str, Any]]]:
#         res = self.plan_split(extra=extra)
#         sessions = res.analysis.get("split_plan", {}).get("sessions", [])
#         return res, (sessions[0].get("runs", []) if sessions else [])
#
#     def cohorts_by_name(self, res: CompileResult) -> dict[str, Any]:
#         return {c.get("name"): c for c in res.finished_yaml.get("cohorts", [])}
#
#     def assertCompiles(self, res: CompileResult) -> None:
#         self.assertTrue(res.ok, summarize_result(res))
#
#     def assertHasError(self, res: CompileResult, code: str) -> None:
#         self.assertTrue(has_error(res, code), summarize_result(res))
#
#     def assertHasWarning(self, res: CompileResult, code: str) -> None:
#         self.assertTrue(has_warning(res, code), summarize_result(res))
#
#
# class LoadingTests(MakeYamlTest):
#     def test_valid_template_compiles(self):
#         self.assertCompiles(self.compile_template())
#
#     def test_malformed_yaml_is_reported(self):
#         res = self.compile_template("vars:\n  - bad: [")
#         self.assertFalse(res.ok)
#         self.assertHasError(res, "yaml_load_error")
#
#
# class RecipeTests(MakeYamlTest):
#     def test_recipe_cohorts_are_imported(self):
#         names = [c.get("name") for c in self.compile_template().finished_yaml.get("cohorts", [])]
#         self.assertIn("Patients", names)
#         self.assertIn("OtherDx", names)
#
#     def test_dest_table_can_be_overridden(self):
#         res = self.compile_template("""
# project_folder: Test
# vars: {min_date_key: 1, max_date_key: 2, ICD_Value: K50}
# cohorts:
#   - recipe: PatientWithDx
#     name: Patients
#     dest_table: MyPatients
# """)
#         self.assertEqual(res.finished_yaml["cohorts"][0]["dest_table"], "MyPatients")
#
#     def test_dest_table_defaults_to_cohort_name(self):
#         res = self.compile_template()
#         self.assertEqual(res.finished_yaml["cohorts"][0]["dest_table"], "Patients")
#
#
# class InferenceTests(MakeYamlTest):
#     def test_required_vars_are_inferred_from_recipe_body(self):
#         required = self.compile_template().analysis["required_vars"]["Patients"]
#         for name in ("min_date_key", "max_date_key", "ICD_Value"):
#             with self.subTest(var=name):
#                 self.assertIn(name, required)
#
#
# class NormalizationTests(MakeYamlTest):
#     def test_grouped_metadata_vars_are_flattened(self):
#         template = tiny_template().replace(
#             "project_folder: Test Run\ncosmos_db: COSMOS\nvars:\n  min_date_key: 20200101\n  max_date_key: 20240101\n",
#             "cosmos_vars:\n  project_db: PROJECTD33A929\n  cosmos_db: COSMOS\n"
#             "run_vars:\n  min_date_key: 20200101\n  max_date_key: 20240101\n"
#             "project_vars:\n  project_folder: Test Run\nvars:\n",
#         )
#         res = self.compile_template(template)
#         self.assertCompiles(res)
#         self.assertEqual(res.finished_yaml.get("project_db"), "PROJECTD33A929")
#         self.assertEqual(res.finished_yaml.get("project_folder"), "Test Run")
#         self.assertEqual(res.finished_yaml.get("vars", {}).get("min_date_key"), 20200101)
#         self.assertIn(
#             "dxf.StartDateKey BETWEEN 20200101 AND 20240101",
#             json.dumps(res.finished_yaml),
#         )
#
#
# class ValidationTests(MakeYamlTest):
#     def test_missing_variable_is_an_error(self):
#         template = tiny_template().replace("  ICD_Value:\n    - K50\n    - K51\n", "")
#         self.assertHasError(self.compile_template(template), "missing_variable")
#
#
# class RenderingTests(MakeYamlTest):
#     def test_multiple_exact_values_render_as_in(self):
#         self.assertIn(
#             "dt.Value IN ('K50', 'K51')",
#             json.dumps(self.compile_template().finished_yaml),
#         )
#
#     def test_wildcard_values_render_as_or_ed_likes(self):
#         template = tiny_template().replace("- K50\n    - K51", "- K50.%\n    - K51.%")
#         text = json.dumps(self.compile_template(template).finished_yaml)
#         self.assertIn("dt.Value LIKE 'K50.%'", text)
#         self.assertIn(" OR ", text)
#
#     def test_underscore_in_like_value_warns(self):
#         template = tiny_template().replace("- K50\n    - K51", "- K50_%")
#         self.assertHasWarning(self.compile_template(template), "like_underscore")
#
#
# class MultiplierTests(MakeYamlTest):
#     def test_split_on_missing_column_is_an_error(self):
#         res = self.compile_template(extra="""
# multipliers:
#   - name: BadSplit
#     stage: split_after_build
#     applies_to: PKTable
#     levels:
#       - strat: bad
#         column: MissingRace
#         values: [x]
# """)
#         self.assertHasError(res, "missing_split_column")
#
#     def test_during_build_multiplier_gives_each_group_its_own_pk(self):
#         template = tiny_template("""
# multipliers:
#   - name: Type
#     stage: during_build
#     levels:
#       - strat: A
#         vars:
#           ICD_Value: A%
#       - strat: B
#         vars:
#           ICD_Value: B%
# """).replace("  ICD_Value:\n    - K50\n    - K51\n", "")
#         res = self.compile_template(template)
#         self.assertCompiles(res)
#         cohorts = self.cohorts_by_name(res)
#         self.assertIn("##JVM_APatients AS pk", json.dumps(cohorts.get("AOtherDx", {})))
#         self.assertIn("##JVM_BPatients AS pk", json.dumps(cohorts.get("BOtherDx", {})))
#
#     def test_split_after_build_metadata_survives_on_the_pk(self):
#         res = self.compile_template(extra="""
# multipliers:
#   - name: Race
#     stage: split_after_build
#     applies_to: PKTable
#     levels:
#       - strat: black
#         column: FirstRace
#         values:
#           - Black %
# """)
#         pk = self.cohorts_by_name(res)["blackPatients"]
#         self.assertIn("split_after_build", pk)
#         self.assertIn("pk.FirstRace LIKE 'Black %'", json.dumps(pk))
#
#
# class BatchingTests(MakeYamlTest):
#     def test_chunk_shorthand_normalizes(self):
#         norm = normalize_batching(
#             [{"chunk": 2000}], load_yaml_from_text(tiny_recipes()), CompileResult()
#         )
#         self.assertEqual(norm[0].get("rows_per_batch"), 2000)
#         self.assertEqual(norm[0].get("kind"), "row_chunk")
#
#     def test_batching_metadata_reaches_the_pk_cohort(self):
#         res = self.compile_template(extra="""
# batching:
#   - sex
#   - chunk: 2000
# """)
#         first = res.finished_yaml["cohorts"][0]
#         self.assertIn("batching", first)
#         self.assertEqual(len(first["batching"]), 2)
#
#     def test_include_other_is_preserved_by_normalization(self):
#         norm = normalize_batching(
#             [{"sex": {"values": ["Female"], "include_other": True}}],
#             load_yaml_from_text(tiny_recipes()),
#             CompileResult(),
#         )
#         self.assertEqual(
#             {k: norm[0].get(k) for k in ("name", "values", "include_other", "column")},
#             {"name": "sex", "values": ["Female"], "include_other": True, "column": "Sex"},
#         )
#
#     def test_dimensions_cross_multiply(self):
#         # state[LA, MS] x sex[Female, Male] is four disjoint slices, not two axes.
#         res, runs = self.runs_for("""
# batching:
#   - state:
#       values: [LA, MS]
#   - sex
# """)
#         self.assertCompiles(res)
#         self.assertEqual(
#             [run["batch"]["name"] for run in runs],
#             ["LA-Female", "LA-Male", "MS-Female", "MS-Male"],
#         )
#
#     def test_each_run_records_its_resolved_dimensions(self):
#         _, runs = self.runs_for("""
# batching:
#   - state:
#       values: [LA, MS]
#   - sex
# """)
#         self.assertEqual(
#             runs[0]["batch"]["dimensions"],
#             [
#                 {
#                     "name": "state",
#                     "kind": "column_values",
#                     "column": "StateOrProvinceAbbreviation",
#                     "value": "LA",
#                 },
#                 {"name": "sex", "kind": "column_values", "column": "Sex", "value": "Female"},
#             ],
#         )
#
#     def test_include_other_contributes_a_bucket_to_the_product(self):
#         _, runs = self.runs_for("""
# batching:
#   - sex:
#       values: [Female]
#       include_other: true
# """)
#         self.assertEqual([run["batch"]["name"] for run in runs], ["Female", "sex-other"])
#         other = runs[1]["batch"]["dimensions"][0]
#         self.assertTrue(other["is_other"])
#         self.assertNotIn("value", other)
#         # The catch-all is defined by exclusion, so it carries the named values.
#         self.assertEqual(other["excludes"], ["Female"])
#
#     def test_unresolvable_dimensions_stay_logical(self):
#         # `values: all` needs a DISTINCT and chunking needs a row count, so
#         # neither can expand until the PK table exists.
#         res, runs = self.runs_for("""
# batching:
#   - state
#   - chunk: 2000
# """)
#         self.assertCompiles(res)
#         self.assertEqual(len(runs), 1)
#         self.assertEqual(runs[0]["batch"]["dimensions"], [])
#         self.assertEqual([r["name"] for r in runs[0]["batch"]["runtime"]], ["state", "chunk"])
#
#     def test_static_and_runtime_dimensions_coexist(self):
#         _, runs = self.runs_for("""
# batching:
#   - sex
#   - state
#   - chunk: 2000
# """)
#         self.assertEqual([run["batch"]["name"] for run in runs], ["Female", "Male"])
#         self.assertEqual([r["name"] for r in runs[0]["batch"]["runtime"]], ["state", "chunk"])
#
#     def test_no_batching_gives_one_unbatched_run(self):
#         res, runs = self.runs_for("")
#         self.assertCompiles(res)
#         self.assertEqual(len(runs), 1)
#         self.assertIsNone(runs[0].get("batch"))
#
#
# class CosmosTests(MakeYamlTest):
#     def test_dual_expands_to_both_instances(self):
#         out = expand_cosmos(
#             {"cosmos_db": "Dual"},
#             [{"name": "Patients", "dest_table": "Patients"}],
#             CompileResult(),
#         )
#         self.assertEqual([c["dest_table"] for c in out], ["Patients", "Patients_sp"])
#
#     def test_unknown_cosmos_db_is_an_error(self):
#         res = CompileResult()
#         validate_cosmos({"cosmos_db": "Mars"}, res)
#         self.assertHasError(res, "bad_cosmos_db")
#
#
# class ReportTests(MakeYamlTest):
#     def test_report_includes_every_section(self):
#         res = CompileResult()
#         res.error("x", "bad")
#         res.warn("y", "careful")
#         res.finished_yaml = {"cohorts": [{"name": "Patients", "dest_table": "Patients"}]}
#         res.analysis = {"required_table_columns": {"OtherDx": {"PKTable": ["PatientDurableKey"]}}}
#         report = build_report(res)
#         for section in ("Errors", "Warnings", "Patients", "Required Columns"):
#             with self.subTest(section=section):
#                 self.assertIn(section, report)
#
#
# class PreyamlTests(MakeYamlTest):
#     MIXED = """
# batching:
#   - sex
# multipliers:
#   - name: Type
#     stage: during_build
#     levels:
#       - strat: A
#         vars:
#           ICD_Value: A%
# """
#
#     def test_symbolic_mode_keeps_recipe_references(self):
#         res = build_preyaml(*self.write_pair(extra=self.MIXED), mode="symbolic")
#         self.assertCompiles(res)
#         self.assertEqual(res.finished_yaml["cohorts"][0].get("recipe"), "PatientWithDx")
#         self.assertIn("multipliers", res.finished_yaml)
#         self.assertIn("batching", res.finished_yaml)
#
#     def test_expanded_mode_inlines_recipes_without_applying_multipliers(self):
#         res = build_preyaml(*self.write_pair(extra=self.MIXED), mode="expanded-recipes")
#         self.assertCompiles(res)
#         first = res.finished_yaml["cohorts"][0]
#         text = json.dumps(res.finished_yaml)
#         self.assertEqual(first.get("name"), "Patients")
#         self.assertNotIn("recipe", first)
#         self.assertIn("DiagnosisEventFact AS dxf", text)
#         self.assertNotIn("APatients", text)
#         self.assertIn("batching", res.finished_yaml)
#
#
# class SplitPlanTests(MakeYamlTest):
#     def test_plain_template_gives_one_session_with_one_run(self):
#         res = self.plan_split()
#         plan = res.analysis.get("split_plan", {})
#         sessions = plan.get("sessions", [])
#         self.assertCompiles(res)
#         self.assertEqual(plan.get("manifest_version"), 1)
#         self.assertEqual(len(sessions), 1)
#         phases = sessions[0]["phases"]
#         self.assertEqual(set(phases), {"setup", "upload_cohorts", "pk"})
#         self.assertEqual(phases["pk"]["pk_source"]["kind"], "generated")
#         self.assertEqual([r["run_id"] for r in sessions[0]["runs"]], ["Patients__run"])
#
#     def test_multiplier_gives_one_session_per_group_each_batched(self):
#         res = self.plan_split(extra="""
# multipliers:
#   - name: Type
#     stage: during_build
#     levels:
#       - strat: A
#         vars:
#           ICD_Value: A%
#       - strat: B
#         vars:
#           ICD_Value: B%
# batching:
#   - sex
#   - chunk: 2000
# """)
#         sessions = res.analysis.get("split_plan", {}).get("sessions", [])
#         self.assertCompiles(res)
#         self.assertEqual(
#             sorted(s["session_id"] for s in sessions), ["APatients", "BPatients"]
#         )
#         for session in sessions:
#             with self.subTest(session=session["session_id"]):
#                 sid = session["session_id"]
#                 self.assertEqual(
#                     [r["run_id"] for r in session["runs"]],
#                     [f"{sid}__Female", f"{sid}__Male"],
#                 )
#                 first = session["runs"][0]["batch"]
#                 self.assertEqual([d["value"] for d in first["dimensions"]], ["Female"])
#                 self.assertEqual([r["name"] for r in first["runtime"]], ["chunk"])
#
#
# class ManifestTests(MakeYamlTest):
#     def test_manifest_is_written_with_pending_status_fields(self):
#         out = self.tmp / "pullmanifest.yaml"
#         res = build_pullmanifest(*self.write_pair(), output_path=out, write=True)
#         self.assertCompiles(res)
#         self.assertTrue(out.exists())
#
#         manifest = res.finished_yaml
#         session = manifest["sessions"][0]
#         pk_phase = session["phases"]["pk"]
#         self.assertEqual(manifest.get("manifest_version"), 1)
#         self.assertEqual(session.get("status"), "pending")
#         self.assertEqual(pk_phase.get("status"), "pending")
#         self.assertIsNone(pk_phase.get("rows"))
#         self.assertIsNone(pk_phase.get("error"))
#         self.assertEqual(session["runs"][0].get("status"), "pending")
#         self.assertEqual(session["runs"][0].get("outputs"), {})
#
#
# class SplitArtifactTests(MakeYamlTest):
#     def test_every_phase_yaml_is_written_and_standalone(self):
#         out_dir = self.tmp / "split"
#         res = write_split_artifacts(*self.write_pair(), output_dir=out_dir)
#         self.assertCompiles(res)
#
#         manifest_path = out_dir / "pullmanifest.yaml"
#         self.assertTrue(manifest_path.exists())
#         session = load_yaml(manifest_path)["sessions"][0]
#         expected = [
#             session["phases"]["setup"]["yaml"],
#             session["phases"]["upload_cohorts"]["yaml"],
#             session["phases"]["pk"]["yaml"],
#             session["runs"][0]["yaml"],
#         ]
#         for rel in expected:
#             with self.subTest(path=rel):
#                 self.assertTrue((out_dir / rel).exists())
#
#         pk_doc = load_yaml(out_dir / expected[2])
#         self.assertEqual(pk_doc["pull_context"]["phase"], "pk")
#         self.assertEqual(len(pk_doc.get("cohorts", [])), 1)
#         self.assertEqual(str(pk_doc["cohorts"][0].get("type", "")).lower(), "pk")
#
#         run_doc = load_yaml(out_dir / expected[3])
#         self.assertEqual(run_doc["pull_context"]["phase"], "run")
#         self.assertTrue(run_doc.get("cohorts"))
#         # Expansion instructions must not survive, or they would be applied twice.
#         self.assertNotIn("multipliers", run_doc)
#         self.assertNotIn("batching", run_doc)
#
#
# class UploadedPkTests(MakeYamlTest):
#     def setUp(self):
#         super().setUp()
#         (self.tmp / "pks.csv").write_text(
#             "PatientDurableKey,DiagnosisEventKey\n1,2\n", encoding="utf-8"
#         )
#
#     def test_uploaded_cohort_becomes_the_session_pk(self):
#         res = self.plan_split(uploaded_pk_template())
#         session = res.analysis["split_plan"]["sessions"][0]
#         pk_source = session["phases"]["pk"]["pk_source"]
#         self.assertCompiles(res)
#         self.assertEqual(session["session_id"], "ClientPK")
#         self.assertEqual(pk_source["kind"], "uploaded_cohort")
#         self.assertEqual(pk_source["table"], "ClientPK")
#         self.assertIn("##JVM_ClientPK AS pk", json.dumps(res.finished_yaml))
#
#     def test_two_uploaded_pk_cohorts_is_an_error(self):
#         extra = """  - name: ClientPK2
#     type: pk
#     dest_table: ClientPK2
#     file_type: csv
#     file_loc: pks.csv
#     key_columns: [PatientDurableKey, DiagnosisEventKey]
# """
#         res = self.compile_template(uploaded_pk_template(extra))
#         self.assertHasError(res, "multiple_uploaded_pk")
#
#     def test_uploaded_pk_without_key_columns_is_an_error(self):
#         res = self.compile_template(uploaded_pk_template(key_columns=False))
#         self.assertHasError(res, "uploaded_pk_missing_keys")
#
#
# class DataDictionaryTests(MakeYamlTest):
#     DICT = {
#         "PatientDim": {
#             "columns": {
#                 "DurableKey": {"type": "bigint", "nullable": False},
#                 "Sex": {"type": "string", "nullable": True},
#                 "BirthDate": {"type": "date/datetime", "nullable": True},
#                 "IsCurrent": {"type": "boolean (flag)", "nullable": False},
#                 "StartDateKey": {"type": "integer (DateKey)", "nullable": True},
#                 "Weight": {"type": "numeric", "nullable": True},
#             }
#         }
#     }
#
#     def cohort(self, source, declared="BIGINT", join=None):
#         return {
#             "dest_table": "T",
#             "columns": [{"source": source, "name": "C", "type": declared}],
#             "filter": {"from": "PatientDim AS p", "join": join or []},
#         }
#
#     def check(self, cohort, dictionary=None):
#         res = CompileResult()
#         validate_data_dictionary([cohort], self.DICT if dictionary is None else dictionary, res)
#         return res
#
#     def codes(self, res):
#         return [m.code for m in res.errors] + [m.code for m in res.warnings]
#
#     def test_valid_column_passes(self):
#         res = self.check(self.cohort("p.DurableKey", "BIGINT"))
#         self.assertEqual(self.codes(res), [])
#
#     def test_unknown_table_is_an_error(self):
#         cohort = self.cohort("h.Whatever")
#         cohort["filter"]["from"] = "HallucinatedTable AS h"
#         self.assertIn("unknown_table", self.codes(self.check(cohort)))
#
#     def test_unknown_column_is_an_error(self):
#         self.assertIn("unknown_column", self.codes(self.check(self.cohort("p.NoSuchColumn"))))
#
#     def test_undeclared_alias_is_an_error(self):
#         # Referencing an alias that no from/join declares produces SQL that
#         # fails to bind at runtime.
#         self.assertIn("unknown_alias", self.codes(self.check(self.cohort("q.DurableKey"))))
#
#     def test_type_family_mismatch_is_an_error(self):
#         self.assertIn(
#             "dd_type_mismatch", self.codes(self.check(self.cohort("p.Sex", "BIGINT")))
#         )
#
#     def test_families_accept_their_members(self):
#         cases = [
#             ("p.DurableKey", "BIGINT"),
#             ("p.Sex", "VARCHAR(400)"),
#             ("p.Sex", "NVARCHAR(50)"),
#             ("p.IsCurrent", "BIT"),
#             ("p.BirthDate", "DATETIME2(7)"),
#             ("p.StartDateKey", "INT"),
#             ("p.Weight", "FLOAT"),
#         ]
#         for source, declared in cases:
#             with self.subTest(source=source, declared=declared):
#                 self.assertEqual(self.codes(self.check(self.cohort(source, declared))), [])
#
#     def test_widening_is_accepted_but_narrowing_is_not(self):
#         # An integer fits in a BIGINT; a bigint does not fit in an INT.
#         self.assertEqual(self.codes(self.check(self.cohort("p.StartDateKey", "BIGINT"))), [])
#         self.assertIn(
#             "dd_type_mismatch", self.codes(self.check(self.cohort("p.DurableKey", "INT")))
#         )
#
#     def test_length_is_not_checked(self):
#         # The dictionary carries no lengths, so VARCHAR(50) and VARCHAR(400)
#         # are indistinguishable to it.
#         for declared in ("VARCHAR(50)", "VARCHAR(4000)"):
#             with self.subTest(declared=declared):
#                 self.assertEqual(self.codes(self.check(self.cohort("p.Sex", declared))), [])
#
#     def test_generated_temps_are_skipped(self):
#         cohort = self.cohort("pk.PatientDurableKey")
#         cohort["filter"]["join"] = ["INNER JOIN ##JVM_PKTable AS pk ON 1 = 1"]
#         self.assertEqual(self.codes(self.check(cohort)), [])
#
#     def test_unresolved_placeholder_tables_are_skipped(self):
#         cohort = self.cohort("pk.Anything")
#         cohort["filter"]["join"] = ["INNER JOIN ##JVM_{{PKTable}} AS pk ON 1 = 1"]
#         self.assertEqual(self.codes(self.check(cohort)), [])
#
#     def test_computed_source_warns_rather_than_failing(self):
#         res = self.check(self.cohort("CASE WHEN p.Sex = 'F' THEN 1 ELSE 0 END", "BIT"))
#         self.assertEqual([m.code for m in res.errors], [])
#         self.assertIn("dd_source_not_checked", [m.code for m in res.warnings])
#
#     def test_lowercase_as_is_recognized(self):
#         cohort = self.cohort("p.DurableKey", "BIGINT")
#         cohort["filter"]["from"] = "PatientDim as p"
#         self.assertEqual(self.codes(self.check(cohort)), [])
#
#     def test_absent_dictionary_checks_nothing(self):
#         self.assertEqual(self.codes(self.check(self.cohort("p.NoSuchColumn"), {})), [])
#
#     def test_missing_dictionary_file_warns_but_does_not_fail(self):
#         res = CompileResult()
#         self.assertIsNone(load_datadictionary(self.tmp / "nope.yaml", res))
#         self.assertEqual(res.errors, [])
#         self.assertEqual([m.code for m in res.warnings], ["datadictionary_missing"])
#
#     def test_real_dictionary_accepts_the_bundled_recipes(self):
#         # The shipped recipes and dictionary must agree, or every template
#         # built from them fails.
#         res = compile_yaml(
#             project_root() / "YAMLs" / "manager_test_cases" / "01_valid_basic.yaml",
#             project_root() / "YAMLs" / "recipes.yaml",
#         )
#         offenders = [
#             m for m in res.errors
#             if m.code in ("unknown_table", "unknown_column", "unknown_alias", "dd_type_mismatch")
#         ]
#         self.assertEqual(offenders, [], summarize_result(res))
#
#
# TEST_GROUPS: dict[str, type[unittest.TestCase]] = {
#     "loading": LoadingTests,
#     "recipes": RecipeTests,
#     "inference": InferenceTests,
#     "normalization": NormalizationTests,
#     "validation": ValidationTests,
#     "rendering": RenderingTests,
#     "multipliers": MultiplierTests,
#     "batching": BatchingTests,
#     "cosmos": CosmosTests,
#     "reports": ReportTests,
#     "preyaml": PreyamlTests,
#     "split_plan": SplitPlanTests,
#     "manifest": ManifestTests,
#     "split_artifacts": SplitArtifactTests,
#     "uploaded_pk": UploadedPkTests,
#     "datadictionary": DataDictionaryTests,
# }
#
#
# def run_tdd(group: str | None = None, verbosity: int = 2) -> int:
#     loader = unittest.TestLoader()
#     suite = unittest.TestSuite()
#     if group:
#         case = TEST_GROUPS.get(group)
#         if case is None:
#             print(f"No test group {group!r}. Available: " + ", ".join(TEST_GROUPS))
#             return 1
#         suite.addTests(loader.loadTestsFromTestCase(case))
#     else:
#         for case in TEST_GROUPS.values():
#             suite.addTests(loader.loadTestsFromTestCase(case))
#     result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
#     return 0 if result.wasSuccessful() else 1
#
#
# # =============================================================================
# # CLI
# # =============================================================================
#
#
# def print_messages(result: CompileResult) -> None:
#     for msg in result.errors:
#         print(f"ERROR [{msg.code}] {msg.message} {msg.context}".rstrip())
#     for msg in result.warnings:
#         print(f"WARN  [{msg.code}] {msg.message} {msg.context}".rstrip())
#
#
# def main(argv: list[str] | None = None) -> int:
#     parser = argparse.ArgumentParser(description="Compile YAML Manager templates.")
#     parser.add_argument("--template", default=str(default_template_path()))
#     parser.add_argument("--recipes", default=str(default_recipes_path()))
#     parser.add_argument("--out", default=None)
#     parser.add_argument(
#         "--datadictionary",
#         default=None,
#         help="Data dictionary to validate column types against.",
#     )
#     parser.add_argument("--suffix", default=OUTPUT_SUFFIX)
#     parser.add_argument("--write", action="store_true", help="Write finished YAML if validation passes.")
#     parser.add_argument("--validate", action="store_true", help="Validate without writing output.")
#     parser.add_argument("--inspect-recipes", action="store_true")
#     parser.add_argument("--export-preyaml", choices=("symbolic", "expanded-recipes"), default=None)
#     parser.add_argument("--export-split", action="store_true", help="Write split YAML artifacts and pullmanifest.yaml.")
#     parser.add_argument("--out-dir", default=None, help="Directory for split export artifacts.")
#     parser.add_argument("--report", action="store_true")
#     parser.add_argument("--report-out", default=None)
#     parser.add_argument("--tdd", nargs="?", const="all", default=None)
#     args = parser.parse_args(argv)
#
#     if args.tdd is not None:
#         return run_tdd(None if args.tdd == "all" else args.tdd)
#
#     if args.inspect_recipes:
#         result = inspect_recipes(args.recipes)
#         print_messages(result)
#         print(json.dumps(result.analysis, indent=2))
#         return 0 if result.ok else 1
#
#     if args.export_preyaml:
#         report_path = args.report_out if args.report else None
#         result = build_preyaml(
#             template_path=args.template,
#             recipes_path=args.recipes,
#             output_path=args.out,
#             mode=args.export_preyaml,
#             write=not args.validate,
#             report_path=report_path,
#         )
#         print_messages(result)
#         if result.ok:
#             print(f"OK: pre-YAML ready at {result.output_path}")
#             if not args.validate:
#                 print(f"Wrote {result.output_path}")
#         else:
#             print("FAILED: errors block pre-YAML export")
#         return 0 if result.ok else 1
#
#     if args.export_split:
#         result = write_split_artifacts(
#             template_path=args.template,
#             recipes_path=args.recipes,
#             output_dir=args.out_dir,
#         )
#         print_messages(result)
#         if result.ok:
#             print(f"Wrote split artifacts to {result.analysis.get('split_output_dir')}")
#             print(f"Manifest: {result.output_path}")
#         else:
#             print("FAILED: errors block split export")
#         return 0 if result.ok else 1
#
#     report_path = args.report_out if args.report else None
#     result = compile_yaml(
#         template_path=args.template,
#         recipes_path=args.recipes,
#         output_path=args.out,
#         suffix=args.suffix,
#         write=args.write and not args.validate,
#         report_path=report_path,
#         datadictionary_path=args.datadictionary,
#     )
#     print_messages(result)
#     if result.ok:
#         print(f"OK: finished YAML ready at {result.output_path}")
#         if args.write and not args.validate:
#             print(f"Wrote {result.output_path}")
#     else:
#         print("FAILED: errors block YAML generation")
#     return 0 if result.ok else 1
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: scripts/makeYaml.py ===
# === BEGIN FILE: scripts/yamlmanager.py SHA256: f898ed308eec285127782aa66f305507691e722f9166e8363b321f44c9879f22 SIZE: 105884 ===
# #!/usr/bin/env python3
# """
# Generate a self-contained HTML prototype UI for YAML Manager.
#
# This is intentionally a dependency-light script. It reads YAML Manager
# template/recipes files through a configurable backend and writes one static
# HTML dashboard.
# """
#
# from __future__ import annotations
#
# import argparse
# import html
# import http.server
# import importlib
# import ipaddress
# import json
# import os
# import re
# import socket
# import sys
# import urllib.parse
# import webbrowser
# from pathlib import Path
# from typing import Any
#
# SCRIPT_DIR = Path(__file__).resolve().parent
# PROJECT_ROOT = SCRIPT_DIR.parent
# sys.pycache_prefix = str(PROJECT_ROOT / "cleanup" / "python_cache")
# if str(SCRIPT_DIR) not in sys.path:
#     sys.path.insert(0, str(SCRIPT_DIR))
#
#
# DEFAULT_OUT = PROJECT_ROOT / "UI" / "manager_dashboard.html"
# DEFAULT_DATA_DICTIONARY = Path(
#     os.environ.get("YAMLMANAGER_DATA_DICTIONARY", PROJECT_ROOT / "YAMLs" / "datadictionary.yaml")
# )
# BACKEND_MODULE = os.environ.get(
#     "YAMLMANAGER_BACKEND_MODULE",
#     "yamlmanager_backend",
# )
# try:
#     backend = importlib.import_module(BACKEND_MODULE)
# except ImportError as exc:
#     raise SystemExit(
#         f"[yamlmanager] Could not import backend module {BACKEND_MODULE!r}. "
#         "Set YAMLMANAGER_BACKEND_MODULE to a Python module on PYTHONPATH."
#     ) from exc
#
#
# def env_int(names: tuple[str, ...], fallback: int) -> int:
#     for name in names:
#         value = os.environ.get(name)
#         if not value:
#             continue
#         try:
#             return int(value)
#         except ValueError:
#             print(f"[yamlmanager] Ignoring invalid {name}={value!r}; using {fallback}.", file=sys.stderr)
#             return fallback
#     return fallback
#
#
# DEFAULT_HOST = os.environ.get(
#     "YAMLMANAGER_HOST",
#     os.environ.get("MANAGER_UI_HOST", "127.0.0.1"),
# )
# DEFAULT_PORT = env_int(("YAMLMANAGER_PORT", "MANAGER_UI_PORT"), 8765)
#
#
# def e(value: Any) -> str:
#     return html.escape("" if value is None else str(value), quote=True)
#
#
# def slug(value: str) -> str:
#     return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower() or "item"
#
#
# def yaml_text(value: Any) -> str:
#     return backend.dump_yaml_text(value).rstrip()
#
#
# def load_data_dictionary(path: Path = DEFAULT_DATA_DICTIONARY) -> dict[str, Any]:
#     try:
#         doc = backend.load_document(path) or {}
#     except FileNotFoundError:
#         return {}
#     data = doc.get("DataDictionary", doc) if isinstance(doc, dict) else {}
#     return data if isinstance(data, dict) else {}
#
#
# def json_payload(value: Any) -> str:
#     return (
#         json.dumps(value)
#         .replace("&", "\\u0026")
#         .replace("<", "\\u003c")
#         .replace(">", "\\u003e")
#     )
#
#
# def print_messages(result: Any) -> None:
#     for msg in getattr(result, "errors", []):
#         print(f"ERROR [{msg.code}] {msg.message} {msg.context}".rstrip())
#     for msg in getattr(result, "warnings", []):
#         print(f"WARN  [{msg.code}] {msg.message} {msg.context}".rstrip())
#
#
# def message_rows(messages: list[Any]) -> str:
#     if not messages:
#         return '<div class="empty">None</div>'
#     rows = []
#     for msg in messages:
#         rows.append(
#             f"""
#             <div class="message {e(msg.level.lower())}">
#               <div class="message-code">{e(msg.code)}</div>
#               <div class="message-body">{e(msg.message)}</div>
#               <div class="message-context">{e(msg.context)}</div>
#             </div>
#             """
#         )
#     return "\n".join(rows)
#
#
# def badge(text: str, kind: str = "neutral") -> str:
#     return f'<span class="badge {e(kind)}">{e(text)}</span>'
#
#
# def value_list(values: list[str], limit: int = 9) -> str:
#     if not values:
#         return '<span class="muted">None detected</span>'
#     shown = values[:limit]
#     body = "".join(f"<li>{e(v)}</li>" for v in shown)
#     extras = values[limit:]
#     if extras:
#         body += "".join(f'<li class="extra-item hidden">{e(v)}</li>' for v in extras)
#         body += f'<li><button class="linklike" data-show-more-list>+ {len(extras)} more</button></li>'
#     return f"<ul>{body}</ul>"
#
#
# def session_label(name: str, pk_table: str) -> str:
#     for suffix in ("Patients", "PKTable"):
#         if name.endswith(suffix):
#             return name[: -len(suffix)] or name
#     for suffix in ("OtherHospitalizations", "Hospitalizations", "OtherDx"):
#         if name.endswith(suffix):
#             return name[: -len(suffix)] or name
#     return pk_table or name
#
#
# def build_connection_maps(result: backend.CompileResult) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, str]]:
#     cohorts = result.finished_yaml.get("cohorts", []) or []
#     required_cols = (result.analysis or {}).get("required_table_columns", {})
#     outgoing: dict[str, list[dict[str, Any]]] = {}
#     incoming: dict[str, list[dict[str, Any]]] = {}
#     sessions: dict[str, str] = {}
#     current_pk_dest = ""
#     color_by_source: dict[str, int] = {}
#     for cohort in cohorts:
#         dest = str(cohort.get("dest_table", cohort.get("name", "")))
#         name = str(cohort.get("name", dest))
#         if str(cohort.get("type", "")).lower() == "pk":
#             current_pk_dest = dest
#         sessions[name] = session_label(name, current_pk_dest)
#         resolved_vars = cohort.get("_resolved_vars", {})
#         for table_var, cols in (required_cols.get(name) or {}).items():
#             target = str(resolved_vars.get(table_var) or table_var)
#             if table_var == "PKTable" and current_pk_dest:
#                 target = current_pk_dest
#             if target not in color_by_source:
#                 color_by_source[target] = len(color_by_source) % 8
#             item = {
#                 "source": target,
#                 "target": name,
#                 "table_var": table_var,
#                 "columns": cols,
#                 "color": color_by_source[target],
#             }
#             outgoing.setdefault(target, []).append(item)
#             incoming.setdefault(name, []).append(item)
#     return outgoing, incoming, sessions
#
#
# def upload_dest(upload: dict[str, Any]) -> str:
#     return str(upload.get("dest_table") or upload.get("name") or "")
#
#
# def upload_columns(upload: dict[str, Any]) -> list[str]:
#     schema = upload.get("columns") or upload.get("schema") or []
#     if not schema:
#         return []
#     if isinstance(schema, list) and schema and isinstance(schema[0], dict):
#         return [str(column.get("name")) for column in schema if column.get("name")]
#     if isinstance(schema, list):
#         return [str(column) for column in schema]
#     return []
#
#
# def upload_has_error(upload: dict[str, Any], result: backend.CompileResult) -> bool:
#     name = str(upload.get("name") or "")
#     dest = upload_dest(upload)
#     for msg in result.errors:
#         haystack = f"{msg.context} {msg.message}"
#         if (name and name in haystack) or (dest and dest in haystack):
#             return True
#     return False
#
#
# def connection_chips(items: list[dict[str, Any]], compact: bool = False, side: str = "incoming") -> str:
#     if not items:
#         return '<span class="muted">None detected</span>'
#     chips = []
#     max_items = 4 if compact else len(items)
#     for item in items[:max_items]:
#         cols = item.get("columns") or []
#         col_text = ", ".join(cols[:3])
#         if len(cols) > 3:
#             col_text += f", +{len(cols) - 3} more"
#         label = item.get("source") if side == "incoming" else item.get("target")
#         chips.append(
#             f'<span class="connection-chip c{item.get("color", 0)}">'
#             f'{e(label)}: {e(col_text or item.get("table_var"))}</span>'
#         )
#     if compact and len(items) > max_items:
#         chips.append('<span class="muted">(click to expand)</span>')
#     return f'<span class="connection-chips">{"".join(chips)}</span>'
#
#
# def source_connection_columns(items: list[dict[str, Any]]) -> str:
#     cols: dict[str, int] = {}
#     for item in items:
#         for col in item.get("columns") or []:
#             text = str(col)
#             cols.setdefault(text, item.get("color", 0))
#     if not cols:
#         return '<span class="muted">None detected</span>'
#     chips = "".join(f'<span class="connection-chip c{color}">{e(col)}</span>' for col, color in cols.items())
#     return f'<span class="connection-chips">{chips}</span>'
#
#
# def grouped_outgoing_chips(items: list[dict[str, Any]], compact: bool = False) -> str:
#     if not items:
#         return '<span class="muted">None detected</span>'
#     grouped: dict[str, dict[str, Any]] = {}
#     fallback_counter = 0
#     for item in items:
#         columns = item.get("columns") or [item.get("table_var")]
#         for column in columns:
#             col = str(column)
#             if col not in grouped:
#                 grouped[col] = {"targets": [], "color": item.get("color", fallback_counter % 8)}
#                 fallback_counter += 1
#             target = str(item.get("target") or "")
#             if target and target not in grouped[col]["targets"]:
#                 grouped[col]["targets"].append(target)
#     pairs = list(grouped.items())
#     max_items = 4 if compact else len(pairs)
#     chips = []
#     for column, meta in pairs[:max_items]:
#         targets = meta["targets"]
#         target_text = ", ".join(targets[:4])
#         if len(targets) > 4:
#             target_text += f", +{len(targets) - 4} more"
#         chips.append(
#             f'<span class="connection-chip c{meta.get("color", 0)}">'
#             f'{e(column)}: {e(target_text)}</span>'
#         )
#     if compact and len(pairs) > max_items:
#         chips.append('<span class="muted">(click to expand)</span>')
#     return f'<span class="connection-chips">{"".join(chips)}</span>'
#
#
# def connection_details(items: list[dict[str, Any]], side: str = "outgoing") -> str:
#     if not items:
#         return '<div class="empty">None</div>'
#     blocks = []
#     for item in items:
#         title = item.get("source") if side == "incoming" else item.get("target")
#         blocks.append(
#             f"""
#             <div class="connection-detail c{item.get("color", 0)}">
#               <strong>{e(title)}</strong>
#               <span class="muted">via {e(item.get("table_var"))}</span>
#               {value_list(item.get("columns") or [], 12)}
#             </div>
#             """
#         )
#     return "".join(blocks)
#
#
# def column_list(values: list[str], connections: list[dict[str, Any]], limit: int = 12) -> str:
#     if not values:
#         return '<span class="muted">None detected</span>'
#     refs_by_col: dict[str, list[dict[str, Any]]] = {}
#     for item in connections:
#         for col in item.get("columns") or []:
#             refs_by_col.setdefault(str(col), []).append(item)
#     items = []
#     for idx, value in enumerate(values):
#         refs = refs_by_col.get(str(value), [])
#         ref_marks = "".join(
#             f'<span class="column-ref c{ref.get("color", 0)}" title="Referenced by {e(ref.get("target"))} via {e(ref.get("table_var"))}"></span>'
#             for ref in refs
#         )
#         hidden = ' class="extra-item hidden"' if idx >= limit else ""
#         items.append(f"<li{hidden}>{ref_marks}{e(value)}</li>")
#     if len(values) > limit:
#         items.append(f'<li><button class="linklike" data-show-more-list>+ {len(values) - limit} more</button></li>')
#     return f"<ul>{''.join(items)}</ul>"
#
#
# def upload_cards(template: dict[str, Any], result: backend.CompileResult) -> str:
#     uploads = template.get("upload_cohorts", []) or []
#     if not uploads:
#         return '<div class="empty">No upload cohorts defined.</div>'
#     upload_errors = " ".join(f"{m.context} {m.message}" for m in result.errors)
#     cards = []
#     for upload in uploads:
#         name = upload.get("name")
#         dest = upload.get("dest_table", name)
#         has_error = str(name) in upload_errors or str(dest) in upload_errors
#         kind = "error" if has_error else "ok"
#         cards.append(
#             f"""
#             <details class="card" open>
#               <summary>
#                 <span>{e(name)}</span>
#                 {badge("error" if has_error else "registered", kind)}
#               </summary>
#               <dl>
#                 <dt>Destination</dt><dd>{e(dest)}</dd>
#                 <dt>Type</dt><dd>{e(upload.get("file_type", ""))}</dd>
#                 <dt>Scope</dt><dd>{e(upload.get("scope", "global"))}</dd>
#                 <dt>Push This Cycle</dt><dd>{e(upload.get("push_this_cycle", True))}</dd>
#                 <dt>File</dt><dd>{e(upload.get("file_loc", ""))}</dd>
#               </dl>
#             </details>
#             """
#         )
#     return "\n".join(cards)
#
#
# def cohort_cards(result: backend.CompileResult) -> str:
#     cohorts = result.finished_yaml.get("cohorts", []) or []
#     uploads = result.finished_yaml.get("upload_cohorts", []) or []
#     analysis = result.analysis or {}
#     required_vars = analysis.get("required_vars", {})
#     required_cols = analysis.get("required_table_columns", {})
#     outputs = analysis.get("output_columns", {})
#     outgoing_connections, incoming_connections, sessions = build_connection_maps(result)
#     if not cohorts and not uploads:
#         return '<div class="empty">No cohorts or uploads available.</div>'
#     cards = []
#     known_tables: set[str] = set()
#     for cohort in cohorts:
#         name = cohort.get("name", "")
#         dest = cohort.get("dest_table", name)
#         known_tables.update({str(name), str(dest)})
#         ctype = str(cohort.get("type", "fact"))
#         kind = "pk" if ctype.lower() == "pk" else "neutral"
#         req_var_names = sorted((required_vars.get(name) or {}).keys())
#         table_inputs = required_cols.get(name) or {}
#         table_bits = []
#         for table_var, cols in table_inputs.items():
#             table_bits.append(f"<h4>{e(table_var)}</h4>{value_list(cols)}")
#         outgoing = outgoing_connections.get(str(dest), [])
#         incoming = incoming_connections.get(str(name), [])
#         group = sessions.get(str(name), str(dest))
#         summary = source_connection_columns(outgoing) if outgoing else connection_chips(incoming, compact=True, side="incoming")
#         split = cohort.get("split_after_build")
#         batching = cohort.get("batching")
#         cards.append(
#             f"""
#             <details class="card cohort-card" id="cohort-{slug(str(name))}">
#               <summary>
#                 <span>
#                   <strong>{e(name)}</strong>
#                   <span class="summary-connections">Cohort: {e(group)}</span>
#                   <span class="summary-connections">Connections: {summary}</span>
#                 </span>
#                 <span>{badge(ctype, kind)}</span>
#               </summary>
#               <div class="grid two">
#                 <section>
#                   <h4>Required Variables</h4>
#                   {value_list(req_var_names)}
#                 </section>
#                 <section>
#                   <h4>Output Columns</h4>
#                   {column_list(outputs.get(dest, []), outgoing, 12)}
#                 </section>
#               </div>
#               <section>
#                 <h4>Required Input Tables</h4>
#                 {''.join(table_bits) if table_bits else '<div class="empty">None</div>'}
#               </section>
#               <section>
#                 <h4>Connected To</h4>
#                 {connection_details(incoming, side="incoming") if incoming else '<div class="empty">None</div>'}
#               </section>
#               <section>
#                 <h4>Used By Tables</h4>
#                 {connection_details(outgoing)}
#               </section>
#               <section>
#                 <h4>Split After Build</h4>
#                 <pre>{e(yaml_text(split) if split else "None")}</pre>
#               </section>
#               <section>
#                 <h4>Batching</h4>
#                 <pre>{e(yaml_text(batching) if batching else "None")}</pre>
#               </section>
#             </details>
#             """
#         )
#     seen_uploads: set[int] = set()
#     for upload in uploads:
#         if not isinstance(upload, dict):
#             continue
#         ident = id(upload)
#         if ident in seen_uploads:
#             continue
#         seen_uploads.add(ident)
#         name = str(upload.get("name") or upload_dest(upload))
#         dest = upload_dest(upload) or name
#         known_tables.update({name, dest})
#         outgoing = outgoing_connections.get(dest, []) + ([] if name == dest else outgoing_connections.get(name, []))
#         cols = upload_columns(upload)
#         kind = "error" if upload_has_error(upload, result) else "upload"
#         type_badges = badge("upload", kind)
#         if str(upload.get("type", "")).lower() == "pk":
#             type_badges += " " + badge("PK", "pk")
#         detail_bits = [
#             ("Destination", dest),
#             ("File Type", upload.get("file_type", "")),
#             ("Scope", upload.get("scope", "global")),
#             ("Push This Cycle", upload.get("push_this_cycle", True)),
#             ("File/Table", upload.get("file_loc", "")),
#         ]
#         cards.append(
#             f"""
#             <details class="card cohort-card upload-card" id="cohort-{slug(name)}">
#               <summary>
#                 <span>
#                   <strong>{e(name)}</strong>
#                   <span class="summary-connections">Upload: {e(dest)}</span>
#                   <span class="summary-connections">Connections: {grouped_outgoing_chips(outgoing, compact=True)}</span>
#                 </span>
#                 <span>{type_badges}</span>
#               </summary>
#               <div class="grid two">
#                 <section>
#                   <h4>Upload Details</h4>
#                   <dl>{''.join(f'<dt>{e(label)}</dt><dd>{e(value)}</dd>' for label, value in detail_bits if value not in ("", None))}</dl>
#                 </section>
#                 <section>
#                   <h4>Output Columns</h4>
#                   {column_list(cols, outgoing, 12)}
#                 </section>
#               </div>
#               <section>
#                 <h4>Used By Tables</h4>
#                 {connection_details(outgoing)}
#               </section>
#             </details>
#             """
#         )
#     unresolved_sources = [
#         source for source in outgoing_connections
#         if source and source not in known_tables
#     ]
#     for source in unresolved_sources:
#         outgoing = outgoing_connections.get(source, [])
#         cards.append(
#             f"""
#             <details class="card cohort-card unresolved-card" id="cohort-{slug(source)}">
#               <summary>
#                 <span>
#                   <strong>{e(source)}</strong>
#                   <span class="summary-connections">Required input table</span>
#                   <span class="summary-connections">Connections: {grouped_outgoing_chips(outgoing, compact=True)}</span>
#                 </span>
#                 <span>{badge("unresolved", "warn")}</span>
#               </summary>
#               <section>
#                 <h4>Used By Tables</h4>
#                 {connection_details(outgoing)}
#               </section>
#             </details>
#             """
#         )
#     return "\n".join(cards)
#
#
# def recipe_cards(recipes_doc: dict[str, Any]) -> str:
#     recipes = recipes_doc.get("recipes", []) or []
#     if not recipes:
#         return '<div class="empty">No recipes found.</div>'
#     cards = []
#     for recipe in recipes:
#         name = recipe.get("name", "")
#         outputs = backend.recipe_output_columns(recipe)
#         req_vars = sorted(backend.recipe_required_vars(recipe).keys())
#         inputs = backend.recipe_table_inputs(recipe)
#         input_bits = []
#         for table_var, meta in inputs.items():
#             input_bits.append(f"<h4>{e(table_var)} as {e(meta.get('alias'))}</h4>{value_list(meta.get('required_columns', []))}")
#         cards.append(
#             f"""
#             <details class="card">
#               <summary>
#                 <span>{e(name)}</span>
#                 {badge(e(recipe.get("type", "fact")), "pk" if str(recipe.get("type", "")).lower() == "pk" else "neutral")}
#               </summary>
#               <p>{e(recipe.get("description", ""))}</p>
#               <div class="grid three">
#                 <section><h4>Required Variables</h4>{value_list(req_vars)}</section>
#                 <section><h4>Output Columns</h4>{value_list(outputs, 12)}</section>
#                 <section><h4>Table Inputs</h4>{''.join(input_bits) if input_bits else '<div class="empty">None</div>'}</section>
#               </div>
#             </details>
#             """
#         )
#     return "\n".join(cards)
#
#
# def graph_panel(result: backend.CompileResult) -> str:
#     cohorts = result.finished_yaml.get("cohorts", []) or []
#     uploads = result.finished_yaml.get("upload_cohorts", []) or []
#     _, incoming_connections, _ = build_connection_maps(result)
#     nodes = []
#     edges = []
#     for upload in uploads:
#         if isinstance(upload, dict):
#             nodes.append(upload_dest(upload) or str(upload.get("name", "")))
#     for cohort in cohorts:
#         name = str(cohort.get("name", ""))
#         nodes.append(name)
#         for item in incoming_connections.get(name, []):
#             edges.append((str(item.get("source")), name, str(item.get("table_var"))))
#     if not nodes:
#         return '<div class="empty">No dependency graph available.</div>'
#     unique_nodes = []
#     for item in nodes + [edge[0] for edge in edges]:
#         if item and item not in unique_nodes:
#             unique_nodes.append(item)
#     node_html = "".join(f'<div class="node" data-node="{e(n)}">{e(n)}</div>' for n in unique_nodes)
#     edge_html = "".join(f'<li><button class="linklike" data-target="{e(dst)}">{e(src)} -> {e(dst)}</button> <span class="muted">({e(label)})</span></li>' for src, dst, label in edges)
#     return f"""
#       <div class="graph-layout">
#         <div class="node-list">{node_html}</div>
#         <div>
#           <h3>Dependencies</h3>
#           <ul class="edge-list">{edge_html or '<li class="muted">No table-input edges detected.</li>'}</ul>
#         </div>
#       </div>
#     """
#
#
# def pipeline_panel(result: backend.CompileResult) -> str:
#     steps = [
#         ("Load YAML", True),
#         ("Import Recipes", not any(m.code == "missing_recipe" for m in result.errors)),
#         ("Infer Variables", bool(result.analysis)),
#         ("Validate Uploads", not any(m.code.startswith("missing_upload") or m.code == "upload_read_error" for m in result.errors)),
#         ("Validate Columns", not any("column" in m.code for m in result.errors)),
#         ("Render Output", result.ok),
#         ("Ready For VM", result.ok),
#     ]
#     return "".join(
#         f'<div class="pipeline-step {"pass" if ok else "fail"}"><span>{idx}</span><strong>{e(label)}</strong><em>{"pass" if ok else "blocked"}</em></div>'
#         for idx, (label, ok) in enumerate(steps, 1)
#     )
#
#
# def export_preview_block(title: str, artifact_id: str, filename: str, result: Any) -> str:
#     if getattr(result, "ok", False):
#         text = yaml_text(result.finished_yaml)
#     else:
#         messages = [m.to_dict() for m in getattr(result, "errors", [])]
#         text = json.dumps({"errors": messages}, indent=2)
#     return f"""
#       <section class="block export-block">
#         <div class="export-head">
#           <h2>{e(title)}</h2>
#           <div class="toolbar compact">
#             <button data-copy-artifact="{e(artifact_id)}">Copy</button>
#             <button data-download-artifact="{e(artifact_id)}" data-filename="{e(filename)}">Download</button>
#           </div>
#         </div>
#         <pre id="{e(artifact_id)}">{e(text)}</pre>
#       </section>
#     """
#
#
# def exports_panel(template_path: Path, recipes_path: Path) -> str:
#     symbolic = backend.build_preyaml(template_path, recipes_path, mode="symbolic")
#     expanded = backend.build_preyaml(template_path, recipes_path, mode="expanded-recipes")
#     manifest = backend.build_pullmanifest(template_path, recipes_path)
#     return f"""
#       <div class="grid three">
#         {export_preview_block("pre-YAML", "exportPreyamlSymbolic", "preyaml.yaml", symbolic)}
#         {export_preview_block("Expanded Recipes pre-YAML", "exportPreyamlExpanded", "preyaml.expanded.yaml", expanded)}
#         {export_preview_block("pullmanifest.yaml", "exportPullmanifest", "pullmanifest.yaml", manifest)}
#       </div>
#       <section class="block">
#         <h2>Handoff</h2>
#         <p>Pullmanager handoff remains file-based. Once Pullmanager's CLI contract is available, YAML Manager can call it with the generated manifest path.</p>
#         <pre>pullmanager split/pullmanifest.yaml</pre>
#       </section>
#     """
#
#
# def summary_cards(template: dict[str, Any], result: backend.CompileResult) -> str:
#     cohorts = result.finished_yaml.get("cohorts", []) or []
#     uploads = template.get("upload_cohorts", []) or []
#     status = "Ready" if result.ok else "Blocked"
#     return f"""
#       <div class="summary">
#         <div class="metric {('ok' if result.ok else 'error')}"><span>Status</span><strong>{status}</strong></div>
#         <div class="metric"><span>Errors</span><strong>{len(result.errors)}</strong></div>
#         <div class="metric"><span>Warnings</span><strong>{len(result.warnings)}</strong></div>
#         <div class="metric"><span>Cohorts</span><strong>{len(cohorts)}</strong></div>
#         <div class="metric"><span>Uploads</span><strong>{len(uploads)}</strong></div>
#       </div>
#     """
#
#
# def build_html(template_path: Path, recipes_path: Path, result: backend.CompileResult, auto_refresh: int = 0) -> str:
#     template = backend.load_document(template_path) or {}
#     recipes_doc = backend.load_document(recipes_path) or {}
#     data_dictionary = load_data_dictionary()
#     source_text = template_path.read_text(encoding="utf-8")
#     finished_text = yaml_text(result.finished_yaml)
#     refresh_meta = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh > 0 else ""
#     data_json = html.escape(json.dumps({
#         "errors": [m.to_dict() for m in result.errors],
#         "warnings": [m.to_dict() for m in result.warnings],
#         "analysis": result.analysis,
#     }, indent=2), quote=False)
#     recipe_defs = [recipe for recipe in recipes_doc.get("recipes", []) or [] if recipe.get("name")]
#     recipe_names = [recipe.get("name") for recipe in recipe_defs]
#     batching_recipes = [recipe for recipe in recipes_doc.get("batching_recipes", []) or [] if recipe.get("name")]
#     batching_names = [recipe.get("name") for recipe in batching_recipes]
#     return f"""<!doctype html>
# <html lang="en">
# <head>
#   <meta charset="utf-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1">
#   {refresh_meta}
#   <title>YAML Manager</title>
#   <style>{CSS}</style>
# </head>
# <body class="dark">
#   <header>
#     <div class="header-main">
#       <h1>YAML Manager</h1>
#       <form id="templatePathForm" class="header-path-form" method="get" action="/">
#         <label for="templatePathInput">Template YAML</label>
#         <input id="templatePathInput" name="template" type="text" value="{e(template_path)}">
#         <input name="recipes" type="hidden" value="{e(recipes_path)}">
#         <button id="refreshPage" type="submit" title="Reload dashboard">Refresh</button>
#       </form>
#     </div>
#     <div class="header-actions">
#       <button id="themeToggle" title="Toggle dark mode">Theme</button>
#     </div>
#   </header>
#
#   <main>
#     {summary_cards(template, result)}
#
#     <nav class="tabs" aria-label="Dashboard sections">
#       <button class="tab active" data-tab="validation">Validation</button>
#       <button class="tab" data-tab="builder">Builder</button>
#       <button class="tab" data-tab="pipeline">Pipeline</button>
#       <button class="tab" data-tab="cohorts">Cohorts</button>
#       <button class="tab" data-tab="recipes">Recipes</button>
#       <button class="tab" data-tab="graph">Graph</button>
#       <button class="tab" data-tab="exports">Exports</button>
#       <button class="tab" data-tab="yaml">YAML</button>
#     </nav>
#
#     <section id="validation" class="panel active">
#       <div class="grid two">
#         <section class="block">
#           <h2>Errors</h2>
#           {message_rows(result.errors)}
#         </section>
#         <section class="block">
#           <h2>Warnings</h2>
#           {message_rows(result.warnings)}
#         </section>
#       </div>
#     </section>
#
#     <section id="builder" class="panel">
#       <div class="builder-layout">
#         <aside class="builder-nav">
#           <button class="builder-link active" data-builder-section="builderProject">Project</button>
#           <button class="builder-link" data-builder-section="builderUploads">Uploads</button>
#           <button class="builder-link" data-builder-section="builderBatching">Batching</button>
#           <button class="builder-link" data-builder-section="builderCohorts">Cohorts</button>
#           <button class="builder-link" data-builder-section="builderDraft">Draft YAML</button>
#         </aside>
#         <div class="builder-main">
#           <section id="builderProject" class="builder-section active block">
#             <h2>Project</h2>
#             <div class="form-grid">
#               <label>Project Folder<input id="builderProjectFolder" type="text"></label>
#               <label>Project DB<input id="builderProjectDb" type="text"></label>
#               <label>Cosmos DB
#                 <select id="builderCosmosDb">
#                   <option value="COSMOS">COSMOS</option>
#                   <option value="COSMOS_SneakPeek">COSMOS_SneakPeek</option>
#                   <option value="Dual">Dual</option>
#                 </select>
#               </label>
#               <label>Min Date Key<input id="builderMinDate" type="text"></label>
#               <label>Max Date Key<input id="builderMaxDate" type="text"></label>
#             </div>
#             <h3>Test Options</h3>
#             <div class="form-grid">
#               <label class="checkbox-label"><input id="builderSmallset" type="checkbox"> Small set</label>
#               <label>PK Row Limit<input id="builderStopAtPk" type="number" min="0"></label>
#               <label>Fact Row Limit<input id="builderStopAtNonPk" type="number" min="0"></label>
#               <label class="checkbox-label"><input id="builderRandomPkSample" type="checkbox"> Random PK sample</label>
#               <label class="checkbox-label"><input id="builderPrintoutMd" type="checkbox"> Print markdown</label>
#             </div>
#             <div class="toolbar compact">
#               <button id="builderNewTemplate">New Blank Template</button>
#               <button id="builderUseCurrent">Reload Current Template</button>
#             </div>
#           </section>
#
#           <section id="builderUploads" class="builder-section block">
#             <h2>Uploads</h2>
#             <div class="inline-form">
#               <select id="newUploadType">
#                 <option value="dbtable">dbtable</option>
#                 <option value="csv">csv</option>
#                 <option value="parquet">parquet</option>
#               </select>
#               <input id="newUploadName" type="text" placeholder="name">
#               <input id="newUploadPath" type="text" placeholder="file_loc or table hint">
#               <button id="addUpload">Add Upload</button>
#             </div>
#             <div id="uploadEditorRows" class="editor-rows"></div>
#           </section>
#
#           <section id="builderBatching" class="builder-section block">
#             <h2>Batching</h2>
#             <div class="inline-form">
#               <select id="newBatchingRecipe"></select>
#               <input id="newBatchingValue" type="text" placeholder="value or chunk size" list="batchingValueSuggestions">
#               <button id="addBatching">Add Batching</button>
#             </div>
#             <datalist id="batchingValueSuggestions"></datalist>
#             <p id="batchingHelp" class="helper-text"></p>
#             <div id="batchingEditorRows" class="editor-rows"></div>
#           </section>
#
#           <section id="builderCohorts" class="builder-section block">
#             <h2>Cohorts</h2>
#             <h3>Recipes</h3>
#             <div class="inline-form">
#               <select id="newCohortRecipe"></select>
#               <input id="newCohortName" type="text" placeholder="cohort name">
#               <button id="addCohort">Add Recipe</button>
#             </div>
#             <div id="cohortEditorRows" class="editor-rows"></div>
#             <h3>Custom</h3>
#             <div class="custom-builder">
#               <div id="pkWarning" class="message warn hidden">
#                 <div class="message-code">PK warning</div>
#                 <div class="message-body">This draft already has a PK cohort. Only one PK cohort should be used.</div>
#               </div>
#               <div class="form-grid">
#                 <label>Name<input id="customName" type="text" placeholder="Mothers"></label>
#                 <label>Destination<input id="customDestTable" type="text" placeholder="Mothers"></label>
#                 <label>Type
#                   <select id="customType">
#                     <option value="fact">fact</option>
#                     <option value="PK">PK</option>
#                   </select>
#                 </label>
#                 <label class="checkbox-label"><input id="customPullThisCycle" type="checkbox" checked> Pull this cycle</label>
#                 <label>From Table<select id="customFromTable"></select></label>
#                 <label>AS<input id="customFromAlias" type="text" placeholder="bpf"></label>
#               </div>
#               <div class="subsection-head">
#                 <h4>Columns</h4>
#               </div>
#               <div class="column-picker">
#                 <section>
#                   <h4>Pulling</h4>
#                   <div id="customSelectedColumns" class="column-list"></div>
#                 </section>
#                 <section>
#                   <h4>Removed</h4>
#                   <div id="customRemovedColumns" class="column-list removed"></div>
#                 </section>
#               </div>
#               <div class="subsection-head">
#                 <h4>Joins</h4>
#                 <button id="addCustomJoin">Add Join</button>
#               </div>
#               <div class="join-builder">
#                 <label>Join Type<select id="joinType">
#                     <option value="INNER">INNER</option>
#                     <option value="LEFT">LEFT</option>
#                     <option value="RIGHT">RIGHT</option>
#                     <option value="FULL">FULL</option>
#                   </select>
#                 </label>
#                 <label>This Column<select id="joinBaseColumn"></select></label>
#                 <label>Join<select id="joinOperator">
#                     <option value="=">=</option>
#                     <option value="&lt;&gt;">&lt;&gt;</option>
#                   </select>
#                 </label>
#                 <label>Available Table<select id="joinTable"></select></label>
#                 <label>Join Column<select id="joinColumn"></select></label>
#                 <span id="joinCheck" class="join-check muted"></span>
#               </div>
#               <div id="customJoinRows" class="editor-rows"></div>
#               <div class="subsection-head">
#                 <h4>Where</h4>
#                 <button id="addCustomWhere">Add Where</button>
#               </div>
#               <div id="customWhereRows" class="editor-rows"></div>
#               <div class="toolbar compact">
#                 <button id="addCustomCohort">Add Custom Cohort</button>
#                 <button id="resetCustomCohort">Reset Custom Form</button>
#                 <button id="copyCustomRecipe">Copy Custom As Recipe</button>
#                 <button id="downloadCustomRecipe">Download Custom Recipe</button>
#               </div>
#             </div>
#           </section>
#
#           <section id="builderDraft" class="builder-section block">
#             <h2>Draft YAML</h2>
#             <div class="form-grid single">
#               <label>Download Name<input id="builderDraftFilename" type="text" placeholder="Test_Run_Full.yaml"></label>
#             </div>
#             <div class="toolbar compact">
#               <button id="copyDraftYaml">Copy Draft</button>
#               <button id="downloadDraftYaml">Download Draft</button>
#             </div>
#             <pre id="draftYaml"></pre>
#           </section>
#         </div>
#       </div>
#     </section>
#
#     <section id="pipeline" class="panel">
#       <section class="block">
#         <h2>Compiler Pipeline</h2>
#         <div class="pipeline">{pipeline_panel(result)}</div>
#       </section>
#     </section>
#
#     <section id="cohorts" class="panel">
#       <div class="toolbar">
#         <input id="cohortSearch" type="search" placeholder="Filter cohorts">
#         <button data-expand="cohorts">Expand All</button>
#         <button data-collapse="cohorts">Collapse All</button>
#       </div>
#       <div id="cohortCards">{cohort_cards(result)}</div>
#     </section>
#
#     <section id="recipes" class="panel">
#       <section class="block">
#         <h2>Recipes</h2>
#         {recipe_cards(recipes_doc)}
#       </section>
#     </section>
#
#     <section id="graph" class="panel">
#       <section class="block">
#         <h2>Dependency Graph</h2>
#         {graph_panel(result)}
#       </section>
#     </section>
#
#     <section id="exports" class="panel">
#       {exports_panel(template_path, recipes_path)}
#     </section>
#
#     <section id="yaml" class="panel">
#       <div class="grid two">
#         <section class="block">
#           <h2>Source YAML</h2>
#           <pre>{e(source_text)}</pre>
#         </section>
#         <section class="block">
#           <h2>Finished YAML Preview</h2>
#           <pre>{e(finished_text)}</pre>
#         </section>
#       </div>
#       <section class="block">
#         <h2>Analysis JSON</h2>
#         <pre>{data_json}</pre>
#       </section>
#     </section>
#   </main>
#
#   <script id="initialTemplateData" type="application/json">{json_payload(template)}</script>
#   <script id="recipeDefsData" type="application/json">{json_payload(recipe_defs)}</script>
#   <script id="recipeNamesData" type="application/json">{json_payload(recipe_names)}</script>
#   <script id="batchingNamesData" type="application/json">{json_payload(batching_names)}</script>
#   <script id="batchingRecipesData" type="application/json">{json_payload(batching_recipes)}</script>
#   <script id="dataDictionaryData" type="application/json">{json_payload(data_dictionary)}</script>
#   <script>{JS}</script>
# </body>
# </html>
# """
#
#
# CSS = r"""
# :root {
#   color-scheme: dark;
#   --bg: #16181b;
#   --panel: #20242a;
#   --ink: #edf0f3;
#   --muted: #a7b0bb;
#   --line: #343a42;
#   --accent: #6fb1cf;
#   --ok: #74c69d;
#   --warn: #e4b363;
#   --err: #ff8a8a;
#   --chip: #2b3138;
# }
# body.light {
#   color-scheme: light;
#   --bg: #f6f7f9;
#   --panel: #ffffff;
#   --ink: #20242a;
#   --muted: #69717d;
#   --line: #d9dee5;
#   --accent: #256f8f;
#   --ok: #26734d;
#   --warn: #9a6200;
#   --err: #a13737;
#   --chip: #eef2f5;
# }
# * { box-sizing: border-box; }
# body { margin: 0; background: var(--bg); color: var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
# header { display: flex; justify-content: space-between; align-items: center; gap: 18px; padding: 16px 24px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 4; }
# h1 { margin: 0; font-size: 22px; }
# h2 { margin: 0 0 14px; font-size: 18px; }
# h3 { margin: 0 0 12px; font-size: 15px; }
# h4 { margin: 12px 0 8px; font-size: 13px; }
# p { color: var(--muted); margin: 4px 0 0; }
# button, input, select { font: inherit; }
# button { border: 1px solid var(--line); background: var(--panel); color: var(--ink); padding: 8px 11px; border-radius: 7px; cursor: pointer; }
# button:hover { border-color: var(--accent); }
# input, select { border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px; background: var(--panel); color: var(--ink); min-width: 0; }
# .header-actions { display: flex; gap: 8px; align-items: center; }
# .header-main { display: grid; gap: 8px; min-width: 0; flex: 1; }
# .header-path-form { display: grid; grid-template-columns: max-content minmax(260px, 1fr) auto; gap: 10px; align-items: center; max-width: 980px; }
# .header-path-form label { color: var(--muted); font-size: 12px; font-weight: 700; }
# .header-path-form input { width: 100%; padding: 7px 9px; background: var(--bg); }
# main { padding: 22px; max-width: 1500px; margin: 0 auto; }
# .path-form { display: grid; grid-template-columns: minmax(240px, 1fr) auto; gap: 10px; align-items: end; margin-top: 12px; }
# .path-form label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
# .path-form input { width: 100%; }
# .summary { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; margin-bottom: 18px; }
# .metric { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
# .metric span { display: block; color: var(--muted); font-size: 12px; }
# .metric strong { display: block; font-size: 24px; margin-top: 6px; }
# .metric.ok strong { color: var(--ok); }
# .metric.error strong { color: var(--err); }
# .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
# .tab.active { background: var(--accent); color: white; border-color: var(--accent); }
# .panel { display: none; }
# .panel.active { display: block; }
# .grid { display: grid; gap: 14px; }
# .grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
# .grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
# .block, .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; margin-bottom: 12px; }
# .card summary { display: flex; justify-content: space-between; align-items: center; gap: 16px; cursor: pointer; font-weight: 700; }
# .badge { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 3px 8px; background: var(--chip); color: var(--ink); font-size: 12px; font-weight: 600; }
# .badge.ok, .badge.pk, .badge.upload { color: var(--ok); }
# .badge.warn { color: var(--warn); }
# .badge.error { color: var(--err); }
# .message { border-left: 4px solid var(--line); padding: 10px 12px; background: var(--chip); border-radius: 6px; margin-bottom: 9px; }
# .message.error { border-left-color: var(--err); }
# .message.warn { border-left-color: var(--warn); }
# .message-code { font-weight: 800; font-size: 13px; }
# .message-context, .muted { color: var(--muted); font-size: 12px; }
# dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 8px 14px; }
# dt { color: var(--muted); }
# dd { margin: 0; overflow-wrap: anywhere; }
# ul { padding-left: 18px; margin: 8px 0; }
# pre { white-space: pre-wrap; overflow: auto; background: var(--chip); border: 1px solid var(--line); border-radius: 7px; padding: 12px; max-height: 520px; }
# .toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
# .toolbar input { flex: 1; border: 1px solid var(--line); border-radius: 7px; padding: 9px 11px; background: var(--panel); color: var(--ink); }
# .toolbar.compact { margin-top: 14px; margin-bottom: 0; flex-wrap: wrap; }
# .export-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
# .export-head .toolbar { margin-top: 0; }
# .export-block pre { max-height: 420px; }
# .builder-layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 14px; align-items: start; }
# .builder-nav { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; position: sticky; top: 88px; display: grid; gap: 8px; }
# .builder-link { text-align: left; }
# .builder-link.active { background: var(--accent); border-color: var(--accent); color: white; }
# .builder-section { display: none; }
# .builder-section.active { display: block; }
# .form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
# .form-grid.single { grid-template-columns: minmax(220px, 520px); margin-bottom: 12px; }
# .form-grid label, .editor-row label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
# .checkbox-label { align-content: end; grid-template-columns: max-content 1fr; align-items: center; min-height: 62px; }
# .form-grid input, .form-grid select, .editor-row input, .editor-row select { width: 100%; color: var(--ink); font-weight: 400; }
# .inline-form { display: grid; grid-template-columns: minmax(140px, 190px) minmax(140px, 1fr) minmax(180px, 1.4fr) auto; gap: 8px; align-items: center; margin-bottom: 12px; }
# .editor-rows { display: grid; gap: 10px; }
# .editor-row { border: 1px solid var(--line); border-radius: 8px; padding: 10px; display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)) auto; gap: 8px; align-items: end; }
# .editor-row.cohort { grid-template-columns: minmax(140px, 1fr) minmax(140px, 1fr) auto auto; }
# .editor-row.batch { grid-template-columns: minmax(140px, 220px) minmax(220px, 1fr) auto; }
# .editor-row.custom-column { grid-template-columns: repeat(4, minmax(110px, 1fr)) auto; }
# .editor-row.line-editor { grid-template-columns: minmax(220px, 1fr) auto; }
# .custom-builder { display: grid; gap: 12px; margin-top: 10px; }
# .column-picker { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
# .column-list { display: grid; gap: 6px; align-content: start; min-height: 42px; }
# .column-item { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(90px, .35fr) auto; gap: 8px; align-items: center; border: 1px solid var(--line); border-radius: 7px; padding: 8px; background: var(--chip); }
# .column-item.removed { opacity: .78; }
# .column-title { display: grid; gap: 2px; min-width: 0; }
# .column-title strong { overflow-wrap: anywhere; }
# .column-actions { display: inline-flex; gap: 5px; justify-content: end; }
# .column-actions button { padding: 4px 7px; min-width: 28px; }
# .join-builder { display: grid; grid-template-columns: minmax(110px, .7fr) minmax(150px, 1fr) 70px minmax(170px, 1fr) minmax(150px, 1fr) minmax(130px, .7fr); gap: 8px; align-items: center; }
# .join-builder label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 650; }
# .join-builder label select, .join-builder label input { width: 100%; color: var(--ink); font-weight: 400; }
# .join-check.ok { color: var(--ok); }
# .join-check.error { color: var(--err); }
# .subsection-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 6px; }
# .subsection-head h4 { margin: 0; }
# .helper-text { margin: 0 0 12px; }
# .tag-list { display: flex; flex-wrap: wrap; gap: 7px; min-height: 38px; align-items: center; }
# .tag { display: inline-flex; gap: 6px; align-items: center; border: 1px solid var(--line); border-radius: 999px; background: var(--chip); padding: 5px 8px; color: var(--ink); }
# .tag button { border: 0; background: transparent; padding: 0 2px; color: var(--muted); }
# .tag-note { color: var(--muted); font-size: 12px; }
# .summary-connections { display: block; margin-top: 6px; font-size: 12px; color: var(--muted); }
# .connection-chips { display: inline-flex; flex-wrap: wrap; gap: 6px; vertical-align: middle; }
# .connection-chip { display: inline-flex; align-items: center; border: 1px solid currentColor; border-radius: 999px; padding: 3px 7px; background: color-mix(in srgb, currentColor 16%, transparent); color: var(--accent); }
# .connection-detail { border-left: 4px solid currentColor; background: var(--chip); border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
# .column-ref { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: currentColor; margin-right: 6px; vertical-align: -1px; }
# .c0 { color: #6fb1cf; }
# .c1 { color: #74c69d; }
# .c2 { color: #e4b363; }
# .c3 { color: #ff8a8a; }
# .c4 { color: #b99cff; }
# .c5 { color: #7bdff2; }
# .c6 { color: #f7a072; }
# .c7 { color: #b8d8ba; }
# .danger { color: var(--err); }
# .pipeline { display: grid; gap: 10px; }
# .pipeline-step { display: grid; grid-template-columns: 34px 1fr auto; align-items: center; gap: 10px; border: 1px solid var(--line); border-radius: 8px; padding: 10px; }
# .pipeline-step span { display: grid; place-items: center; height: 26px; width: 26px; border-radius: 50%; background: var(--chip); }
# .pipeline-step.pass em { color: var(--ok); }
# .pipeline-step.fail em { color: var(--err); }
# .graph-layout { display: grid; grid-template-columns: minmax(220px, 360px) 1fr; gap: 18px; }
# .node-list { display: grid; gap: 8px; }
# .node { border: 1px solid var(--line); background: var(--chip); border-radius: 8px; padding: 10px; font-weight: 650; }
# .edge-list { margin-top: 0; }
# .linklike { border: 0; background: transparent; padding: 0; color: var(--accent); text-decoration: underline; }
# .empty { color: var(--muted); padding: 8px 0; }
# .hidden { display: none; }
# @media (max-width: 900px) {
#   .summary, .grid.two, .grid.three, .graph-layout, .builder-layout, .form-grid, .inline-form, .editor-row, .editor-row.cohort, .editor-row.batch, .column-picker, .join-builder { grid-template-columns: 1fr; }
#   header { position: static; align-items: stretch; flex-direction: column; }
#   .header-actions { align-self: flex-end; }
#   .header-path-form { grid-template-columns: 1fr; }
#   .builder-nav { position: static; }
# }
# """
#
#
# JS = r"""
# const initialTemplate = JSON.parse(document.getElementById('initialTemplateData').textContent);
# const recipeDefs = JSON.parse(document.getElementById('recipeDefsData').textContent);
# const recipeNames = JSON.parse(document.getElementById('recipeNamesData').textContent);
# const batchingNames = JSON.parse(document.getElementById('batchingNamesData').textContent);
# const batchingRecipes = JSON.parse(document.getElementById('batchingRecipesData').textContent);
# const dataDictionaryRaw = JSON.parse(document.getElementById('dataDictionaryData').textContent);
# const dataDictionary = normalizeDataDictionary(dataDictionaryRaw);
# const dictionaryTableNames = Object.keys(dataDictionary).sort((a, b) => a.localeCompare(b));
# let draftTemplate = clone(initialTemplate);
# let customDraft = blankCustomCohort();
# let editingCustomIndex = null;
#
# function clone(value) {
#   return JSON.parse(JSON.stringify(value || {}));
# }
#
# document.querySelectorAll('.tab').forEach(button => {
#   button.addEventListener('click', () => {
#     document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
#     document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
#     button.classList.add('active');
#     document.getElementById(button.dataset.tab).classList.add('active');
#   });
# });
#
# document.getElementById('themeToggle').addEventListener('click', () => {
#   document.body.classList.toggle('light');
# });
#
# document.getElementById('templatePathForm')?.addEventListener('submit', event => {
#   if (window.location.protocol === 'file:') {
#     event.preventDefault();
#     window.location.reload();
#   }
# });
#
# document.querySelectorAll('[data-expand]').forEach(button => {
#   button.addEventListener('click', () => {
#     document.querySelectorAll(`#${button.dataset.expand} details`).forEach(d => d.open = true);
#   });
# });
#
# document.querySelectorAll('[data-collapse]').forEach(button => {
#   button.addEventListener('click', () => {
#     document.querySelectorAll(`#${button.dataset.collapse} details`).forEach(d => d.open = false);
#   });
# });
#
# const cohortSearch = document.getElementById('cohortSearch');
# if (cohortSearch) {
#   cohortSearch.addEventListener('input', () => {
#     const q = cohortSearch.value.toLowerCase();
#     document.querySelectorAll('.cohort-card').forEach(card => {
#       card.classList.toggle('hidden', !card.innerText.toLowerCase().includes(q));
#     });
#   });
# }
#
# document.querySelectorAll('[data-target]').forEach(button => {
#   button.addEventListener('click', () => {
#     const id = 'cohort-' + button.dataset.target.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '');
#     const target = document.getElementById(id);
#     if (!target) return;
#     document.querySelector('[data-tab="cohorts"]').click();
#     target.open = true;
#     target.scrollIntoView({ behavior: 'smooth', block: 'start' });
#   });
# });
#
# document.addEventListener('click', event => {
#   const target = event.target;
#   if (target.dataset.showMoreList !== undefined) {
#     const list = target.closest('ul');
#     if (!list) return;
#     list.querySelectorAll('.extra-item').forEach(item => item.classList.remove('hidden'));
#     target.closest('li')?.remove();
#   }
# });
#
# document.querySelectorAll('.builder-link').forEach(button => {
#   button.addEventListener('click', () => {
#     document.querySelectorAll('.builder-link').forEach(b => b.classList.remove('active'));
#     document.querySelectorAll('.builder-section').forEach(s => s.classList.remove('active'));
#     button.classList.add('active');
#     document.getElementById(button.dataset.builderSection).classList.add('active');
#   });
# });
#
# function ensureDraftShape() {
#   draftTemplate.cosmos_vars = draftTemplate.cosmos_vars || {};
#   draftTemplate.run_vars = draftTemplate.run_vars || {};
#   draftTemplate.project_vars = draftTemplate.project_vars || {};
#   draftTemplate.test_options = draftTemplate.test_options || {};
#   draftTemplate.vars = draftTemplate.vars || {};
#   draftTemplate.upload_cohorts = Array.isArray(draftTemplate.upload_cohorts) ? draftTemplate.upload_cohorts : [];
#   draftTemplate.batching = Array.isArray(draftTemplate.batching) ? draftTemplate.batching : [];
#   draftTemplate.cohorts = Array.isArray(draftTemplate.cohorts) ? draftTemplate.cohorts : [];
# }
#
# function blankTemplate() {
#   return {
#     cosmos_vars: {
#       project_db: 'PROJECTD93A57',
#       cosmos_db: 'COSMOS'
#     },
#     run_vars: {
#       min_date_key: '',
#       max_date_key: ''
#     },
#     test_options: {
#       smallset: false,
#       stop_at_for_pk_table: 10,
#       stop_at_for_non_pk_tables: 0,
#       random_pk_sample: false,
#       printout_md: true
#     },
#     project_vars: {
#       project_folder: 'New Project'
#     },
#     vars: {},
#     upload_cohorts: [],
#     multipliers: [],
#     batching: [],
#     cohorts: []
#   };
# }
#
# function hydrateBuilder() {
#   ensureDraftShape();
#   setValue('builderProjectFolder', draftTemplate.project_vars.project_folder || draftTemplate.project_folder || '');
#   setValue('builderProjectDb', draftTemplate.cosmos_vars.project_db || draftTemplate.project_db || '');
#   setValue('builderCosmosDb', draftTemplate.cosmos_vars.cosmos_db || draftTemplate.cosmos_db || 'COSMOS');
#   setValue('builderMinDate', draftTemplate.run_vars.min_date_key || draftTemplate.vars.min_date_key || '');
#   setValue('builderMaxDate', draftTemplate.run_vars.max_date_key || draftTemplate.vars.max_date_key || '');
#   setChecked('builderSmallset', Boolean(draftTemplate.test_options.smallset));
#   setValue('builderStopAtPk', draftTemplate.test_options.stop_at_for_pk_table ?? '');
#   setValue('builderStopAtNonPk', draftTemplate.test_options.stop_at_for_non_pk_tables ?? '');
#   setChecked('builderRandomPkSample', Boolean(draftTemplate.test_options.random_pk_sample));
#   setChecked('builderPrintoutMd', draftTemplate.test_options.printout_md !== false);
#   renderRecipeOptions();
#   renderBatchingOptions();
#   renderDictionaryTableOptions();
#   renderCustomBuilder();
#   renderUploadRows();
#   renderBatchingRows();
#   renderCohortRows();
#   updateDraftYaml();
# }
#
# function setValue(id, value) {
#   const el = document.getElementById(id);
#   if (el) el.value = value;
# }
#
# function setChecked(id, value) {
#   const el = document.getElementById(id);
#   if (el) el.checked = Boolean(value);
# }
#
# function getValue(id) {
#   const el = document.getElementById(id);
#   return el ? el.value.trim() : '';
# }
#
# function getChecked(id) {
#   const el = document.getElementById(id);
#   return Boolean(el?.checked);
# }
#
# function numericOrZero(value) {
#   const text = String(value ?? '').trim();
#   if (text === '') return 0;
#   const number = Number(text);
#   return Number.isFinite(number) ? number : 0;
# }
#
# function syncProjectFields() {
#   ensureDraftShape();
#   draftTemplate.project_vars.project_folder = getValue('builderProjectFolder');
#   draftTemplate.cosmos_vars.project_db = getValue('builderProjectDb');
#   draftTemplate.cosmos_vars.cosmos_db = getValue('builderCosmosDb') || 'COSMOS';
#   draftTemplate.run_vars.min_date_key = getValue('builderMinDate');
#   draftTemplate.run_vars.max_date_key = getValue('builderMaxDate');
#   draftTemplate.test_options.smallset = getChecked('builderSmallset');
#   draftTemplate.test_options.stop_at_for_pk_table = numericOrZero(getValue('builderStopAtPk'));
#   draftTemplate.test_options.stop_at_for_non_pk_tables = numericOrZero(getValue('builderStopAtNonPk'));
#   draftTemplate.test_options.random_pk_sample = getChecked('builderRandomPkSample');
#   draftTemplate.test_options.printout_md = getChecked('builderPrintoutMd');
#   updateDraftYaml();
# }
#
# ['builderProjectFolder', 'builderProjectDb', 'builderCosmosDb', 'builderMinDate', 'builderMaxDate', 'builderSmallset', 'builderStopAtPk', 'builderStopAtNonPk', 'builderRandomPkSample', 'builderPrintoutMd'].forEach(id => {
#   const el = document.getElementById(id);
#   if (el) el.addEventListener('input', syncProjectFields);
#   if (el) el.addEventListener('change', syncProjectFields);
# });
#
# ['customName', 'customDestTable', 'customType', 'customPullThisCycle', 'customFromTable', 'customFromAlias'].forEach(id => {
#   const el = document.getElementById(id);
#   if (el) el.addEventListener('input', syncCustomFields);
#   if (el) el.addEventListener('change', syncCustomFields);
# });
#
# ['joinType', 'joinBaseColumn', 'joinOperator', 'joinTable', 'joinColumn'].forEach(id => {
#   const el = document.getElementById(id);
#   if (!el) return;
#   el.addEventListener('input', () => {
#     if (id === 'joinTable') {
#       renderJoinBuilder();
#     } else {
#       updateJoinCheck();
#     }
#   });
#   el.addEventListener('change', () => {
#     if (id === 'joinTable') {
#       renderJoinBuilder();
#     } else {
#       updateJoinCheck();
#     }
#   });
# });
#
# document.getElementById('builderDraftFilename')?.addEventListener('input', updateDraftYaml);
# document.getElementById('newBatchingRecipe')?.addEventListener('change', updateBatchingHelp);
#
# function renderRecipeOptions() {
#   const select = document.getElementById('newCohortRecipe');
#   if (!select) return;
#   select.innerHTML = [
#     ...recipeNames.map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`),
#     '<option value="__custom__">Custom</option>'
#   ].join('');
# }
#
# function renderBatchingOptions() {
#   const select = document.getElementById('newBatchingRecipe');
#   if (!select) return;
#   select.innerHTML = batchingNames.map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`).join('');
#   updateBatchingHelp();
# }
#
# function renderDictionaryTableOptions() {
#   const tableOptions = ['<option value="">Select table</option>']
#     .concat(dictionaryTableNames.map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`))
#     .join('');
#   ['customFromTable', 'joinTable'].forEach(id => {
#     const select = document.getElementById(id);
#     if (!select) return;
#     const current = select.value;
#     select.innerHTML = tableOptions;
#     if (current) select.value = current;
#   });
# }
#
# function renderUploadRows() {
#   const root = document.getElementById('uploadEditorRows');
#   if (!root) return;
#   root.innerHTML = draftTemplate.upload_cohorts.map((upload, index) => `
#     <div class="editor-row">
#       <label>Name<input data-upload-field="name" data-index="${index}" value="${escapeAttr(upload.name || '')}"></label>
#       <label>Destination<input data-upload-field="dest_table" data-index="${index}" value="${escapeAttr(upload.dest_table || upload.name || '')}"></label>
#       <label>Type
#         <select data-upload-field="file_type" data-index="${index}">
#           ${['dbtable', 'csv', 'parquet'].map(type => `<option value="${type}" ${type === upload.file_type ? 'selected' : ''}>${type}</option>`).join('')}
#         </select>
#       </label>
#       <label>File/Table<input data-upload-field="file_loc" data-index="${index}" value="${escapeAttr(upload.file_loc || '')}"></label>
#       <button class="danger" data-remove-upload="${index}">Remove</button>
#     </div>
#   `).join('') || '<div class="empty">No uploads in draft.</div>';
# }
#
# function renderBatchingRows() {
#   const root = document.getElementById('batchingEditorRows');
#   if (!root) return;
#   root.innerHTML = draftTemplate.batching.map((item, index) => {
#     const parsed = describeBatching(item);
#     const isChunk = parsed.name === 'chunk';
#     const tags = parsed.values.map((value, valueIndex) => `
#       <span class="tag">${escapeHtml(value)} <button title="Remove value" data-remove-batching-value="${index}" data-value-index="${valueIndex}">x</button></span>
#     `).join('');
#     return `
#       <div class="editor-row batch">
#         <label>Recipe
#           <select data-batching-field="name" data-index="${index}">
#             ${batchingNames.map(name => `<option value="${escapeAttr(name)}" ${name === parsed.name ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')}
#           </select>
#         </label>
#         <label>${isChunk ? 'Rows Per Batch' : 'Values'}
#           ${isChunk
#             ? `<input data-batching-field="chunk" data-index="${index}" value="${escapeAttr(parsed.chunk || '')}" placeholder="2000">`
#             : `<div class="tag-list">${tags || '<span class="tag-note">All values. Explicit picks will also create an all-other batch.</span>'}</div>
#                <input data-batching-add-value="${index}" value="" placeholder="type value and press Enter">`}
#         </label>
#         <button class="danger" data-remove-batching="${index}">Remove</button>
#       </div>
#     `;
#   }).join('') || '<div class="empty">No batching rules in draft.</div>';
# }
#
# function renderCohortRows() {
#   const root = document.getElementById('cohortEditorRows');
#   if (!root) return;
#   root.innerHTML = draftTemplate.cohorts.map((cohort, index) => `
#     <div class="editor-row cohort">
#       <label>${cohort.recipe ? 'Recipe' : 'Custom'}<input data-cohort-field="${cohort.recipe ? 'recipe' : 'type'}" data-index="${index}" value="${escapeAttr(cohort.recipe || cohort.type || '')}"></label>
#       <label>Name<input data-cohort-field="name" data-index="${index}" value="${escapeAttr(cohort.name || '')}"></label>
#       ${cohort.recipe ? '' : `<button data-edit-custom-cohort="${index}">Load</button>`}
#       <button class="danger" data-remove-cohort="${index}">Remove</button>
#     </div>
#   `).join('') || '<div class="empty">No cohorts in draft.</div>';
#   updatePkWarning();
#   renderJoinBuilder();
# }
#
# function blankCustomCohort() {
#   return {
#     name: '',
#     dest_table: '',
#     type: 'fact',
#     pull_this_cycle: true,
#     columns: [],
#     removed_columns: [],
#     filter: {
#       from_table: '',
#       from_alias: '',
#       join: [],
#       where: []
#     }
#   };
# }
#
# function normalizeDataDictionary(raw) {
#   const source = raw?.DataDictionary || raw || {};
#   const normalized = {};
#   Object.entries(source).forEach(([tableName, table]) => {
#     const columns = table?.columns || {};
#     normalized[tableName] = {
#       description: table?.description || '',
#       granularity: table?.granularity || '',
#       columns: Object.entries(columns).map(([name, meta]) => ({
#         name,
#         type: String(meta?.type || ''),
#         normalizedType: normalizeColumnType(meta?.type || ''),
#         nullable: meta?.nullable,
#         description: meta?.description || ''
#       }))
#     };
#   });
#   return normalized;
# }
#
# function normalizeColumnType(type) {
#   const text = String(type || '').toLowerCase();
#   if (/(char|text|string|varchar|nvarchar)/.test(text)) return 'string';
#   if (/(date|time)/.test(text)) return 'datetime';
#   if (/(bool|bit|flag)/.test(text)) return 'boolean';
#   if (/(bigint|integer|int|numeric|decimal|float|double|real)/.test(text)) return 'number';
#   return text.replace(/\s*\(.*/, '').trim() || 'unknown';
# }
#
# function columnTypeForYaml(type) {
#   const text = String(type || '').toLowerCase();
#   if (/bigint/.test(text)) return 'BIGINT';
#   if (/integer|int/.test(text)) return 'INT';
#   if (/date\/datetime|datetime|date|time/.test(text)) return 'DATETIME2(7)';
#   if (/bool|bit|flag/.test(text)) return 'BIT';
#   if (/numeric|decimal|float|double|real/.test(text)) return 'FLOAT';
#   if (/char|text|string|varchar|nvarchar/.test(text)) return 'VARCHAR(400)';
#   return String(type || '');
# }
#
# function typesCompatible(left, right) {
#   const a = normalizeColumnType(left);
#   const b = normalizeColumnType(right);
#   return a !== 'unknown' && b !== 'unknown' && a === b;
# }
#
# function aliasForTable(tableName) {
#   const words = String(tableName || '').match(/[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+/g) || [];
#   const letters = words.map(word => word[0]).join('').toLowerCase();
#   return letters || String(tableName || '').slice(0, 3).toLowerCase() || 't';
# }
#
# function dictionaryColumns(tableName) {
#   return dataDictionary[tableName]?.columns || [];
# }
#
# function dictionaryColumn(tableName, columnName) {
#   return dictionaryColumns(tableName).find(column => column.name === columnName) || null;
# }
#
# function columnFromDictionary(tableName, columnName, alias) {
#   const meta = dictionaryColumn(tableName, columnName) || {};
#   return {
#     source: `${alias || aliasForTable(tableName)}.${columnName}`,
#     name: columnName,
#     type: columnTypeForYaml(meta.type),
#     nullable: meta.nullable === undefined ? '' : Boolean(meta.nullable),
#     dict_table: tableName,
#     dict_column: columnName
#   };
# }
#
# function parseFromClause(fromValue) {
#   const first = Array.isArray(fromValue) ? fromValue[0] : fromValue;
#   const text = String(first || '').trim();
#   const match = text.match(/^(.+?)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)$/i);
#   if (match && dataDictionary[match[1].trim()]) return { table: match[1].trim(), alias: match[2].trim() };
#   return { table: dataDictionary[text] ? text : '', alias: '' };
# }
#
# function customDraftFromCohort(cohort) {
#   const from = parseFromClause(cohort?.filter?.from);
#   const alias = from.alias || aliasForTable(from.table);
#   const draft = blankCustomCohort();
#   draft.name = cohort?.name || '';
#   draft.dest_table = cohort?.dest_table || cohort?.name || '';
#   draft.type = cohort?.type || 'fact';
#   draft.pull_this_cycle = cohort?.pull_this_cycle !== false;
#   draft.filter.from_table = from.table;
#   draft.filter.from_alias = alias;
#   draft.filter.join = Array.isArray(cohort?.filter?.join) ? clone(cohort.filter.join) : [];
#   draft.filter.where = Array.isArray(cohort?.filter?.where) ? clone(cohort.filter.where) : [];
#   const existing = Array.isArray(cohort?.columns) ? cohort.columns : [];
#   if (from.table) {
#     const selected = [];
#     existing.forEach(column => {
#       const source = String(column.source || '');
#       const sourceCol = source.includes('.') ? source.split('.').pop() : column.name;
#       selected.push({
#         ...column,
#         name: column.name || sourceCol,
#         dict_table: from.table,
#         dict_column: sourceCol
#       });
#     });
#     const selectedNames = new Set(selected.map(column => column.dict_column || column.name));
#     draft.columns = selected;
#     draft.removed_columns = dictionaryColumns(from.table)
#       .filter(column => !selectedNames.has(column.name))
#       .map(column => column.name);
#   } else {
#     draft.columns = existing;
#   }
#   return draft;
# }
#
# function resetColumnsForTable(tableName, preserveExisting = false) {
#   const alias = customDraft.filter.from_alias || aliasForTable(tableName);
#   const dictCols = dictionaryColumns(tableName);
#   if (!tableName || !dictCols.length) return;
#   if (preserveExisting && customDraft.columns.length) {
#     const existingNames = new Set(customDraft.columns.map(column => column.dict_column || String(column.source || '').split('.').pop() || column.name));
#     customDraft.columns = customDraft.columns.map(column => {
#       const name = column.dict_column || String(column.source || '').split('.').pop() || column.name;
#       return { ...columnFromDictionary(tableName, name, alias), ...column, dict_table: tableName, dict_column: name };
#     });
#     customDraft.removed_columns = dictCols.filter(column => !existingNames.has(column.name)).map(column => column.name);
#     return;
#   }
#   customDraft.columns = dictCols.map(column => columnFromDictionary(tableName, column.name, alias));
#   customDraft.removed_columns = [];
# }
#
# function renderCustomBuilder() {
#   renderDictionaryTableOptions();
#   setValue('customName', customDraft.name || '');
#   setValue('customDestTable', customDraft.dest_table || '');
#   setValue('customType', customDraft.type || 'fact');
#   const pull = document.getElementById('customPullThisCycle');
#   if (pull) pull.checked = customDraft.pull_this_cycle !== false;
#   setValue('customFromTable', customDraft.filter.from_table || '');
#   setValue('customFromAlias', customDraft.filter.from_alias || '');
#   if (customDraft.filter.from_table && !customDraft.columns.length && !(customDraft.removed_columns || []).length) {
#     resetColumnsForTable(customDraft.filter.from_table);
#   }
#   renderCustomColumnRows();
#   renderJoinBuilder();
#   renderCustomJoinRows();
#   renderCustomLineRows('customWhereRows', 'where', 'where condition');
#   updatePkWarning();
# }
#
# function renderCustomColumnRows() {
#   const selected = document.getElementById('customSelectedColumns');
#   const removed = document.getElementById('customRemovedColumns');
#   if (!selected || !removed) return;
#   selected.innerHTML = customDraft.columns.map((column, index) => {
#     const meta = dictionaryColumn(column.dict_table || customDraft.filter.from_table, column.dict_column || column.name) || {};
#     return `
#       <div class="column-item">
#         <span class="column-title">
#           <strong>${escapeHtml(column.name || column.dict_column || '')}</strong>
#           <span class="muted">${escapeHtml(meta.type || column.type || '')}${column.nullable === false ? ' · required' : ''}</span>
#         </span>
#         <input data-custom-column-field="name" data-index="${index}" value="${escapeAttr(column.name || '')}" title="Output name">
#         <span class="column-actions">
#           <button data-move-custom-column="${index}" data-direction="-1" title="Move up">Up</button>
#           <button data-move-custom-column="${index}" data-direction="1" title="Move down">Down</button>
#           <button class="danger" data-remove-custom-column="${index}" title="Remove">-</button>
#         </span>
#       </div>
#     `;
#   }).join('') || '<div class="empty">No columns selected.</div>';
#   removed.innerHTML = (customDraft.removed_columns || []).map(columnName => {
#     const meta = dictionaryColumn(customDraft.filter.from_table, columnName) || {};
#     return `
#       <div class="column-item removed">
#         <span class="column-title">
#           <strong>${escapeHtml(columnName)}</strong>
#           <span class="muted">${escapeHtml(meta.type || '')}</span>
#         </span>
#         <span></span>
#         <span class="column-actions"><button data-add-removed-column="${escapeAttr(columnName)}" title="Add">+</button></span>
#       </div>
#     `;
#   }).join('') || '<div class="empty">No removed columns.</div>';
# }
#
# function cohortOutputColumns(cohort) {
#   if (!cohort) return [];
#   const source = cohort.recipe ? recipeDefs.find(recipe => recipe.name === cohort.recipe) : cohort;
#   return (source?.columns || []).map(column => {
#     const name = column.name || String(column.source || '').split('.').pop();
#     return {
#       name,
#       type: column.type || '',
#       normalizedType: normalizeColumnType(column.type || ''),
#       nullable: column.nullable
#     };
#   }).filter(column => column.name);
# }
#
# function uploadOutputColumns(upload) {
#   const schema = upload?.columns || upload?.schema || [];
#   if (!Array.isArray(schema)) return [];
#   return schema.map(column => {
#     if (column && typeof column === 'object') {
#       return {
#         name: column.name,
#         type: column.type || '',
#         normalizedType: normalizeColumnType(column.type || ''),
#         nullable: column.nullable
#       };
#     }
#     return { name: String(column), type: '', normalizedType: 'unknown', nullable: '' };
#   }).filter(column => column.name);
# }
#
# function availableJoinTables() {
#   const tables = [];
#   draftTemplate.cohorts.forEach((cohort, index) => {
#     if (editingCustomIndex !== null && index === editingCustomIndex) return;
#     const name = cohort.dest_table || cohort.name || cohort.recipe;
#     if (!name) return;
#     tables.push({
#       name,
#       kind: cohort.recipe ? 'recipe' : 'custom',
#       columns: cohortOutputColumns(cohort)
#     });
#   });
#   draftTemplate.upload_cohorts.forEach(upload => {
#     const name = upload.dest_table || upload.name;
#     if (!name) return;
#     tables.push({
#       name,
#       kind: 'upload',
#       columns: uploadOutputColumns(upload)
#     });
#   });
#   return tables;
# }
#
# function currentColumnOptions(selected = '') {
#   return customDraft.columns.map((column, index) => {
#     const sourceColumn = column.dict_column || String(column.source || '').split('.').pop() || column.name;
#     const label = column.name && column.name !== sourceColumn ? `${column.name} (${sourceColumn})` : sourceColumn;
#     return `<option value="${index}" ${String(index) === String(selected) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
#   }).join('');
# }
#
# function availableColumnOptions(tableName, selected = '') {
#   const table = availableJoinTables().find(item => item.name === tableName);
#   return (table?.columns || []).map(column => `<option value="${escapeAttr(column.name)}" ${column.name === selected ? 'selected' : ''}>${escapeHtml(column.name)}</option>`).join('');
# }
#
# function renderJoinBuilder() {
#   const baseColumn = document.getElementById('joinBaseColumn');
#   const joinTable = document.getElementById('joinTable');
#   const joinColumn = document.getElementById('joinColumn');
#   if (!baseColumn || !joinTable || !joinColumn) return;
#   const priorBaseColumn = baseColumn.value;
#   const priorTable = joinTable.value;
#   baseColumn.innerHTML = currentColumnOptions(priorBaseColumn);
#   const tables = availableJoinTables();
#   joinTable.innerHTML = tables.map(table => `<option value="${escapeAttr(table.name)}" ${table.name === priorTable ? 'selected' : ''}>${escapeHtml(table.name)} (${escapeHtml(table.kind)})</option>`).join('');
#   if (!joinTable.value && tables.length) joinTable.value = tables[0].name;
#   joinColumn.innerHTML = availableColumnOptions(joinTable.value, joinColumn.value);
#   updateJoinCheck();
# }
#
# function updateJoinCheck() {
#   const check = document.getElementById('joinCheck');
#   if (!check) return;
#   const left = customDraft.columns[Number(getValue('joinBaseColumn'))];
#   const table = availableJoinTables().find(item => item.name === getValue('joinTable'));
#   const right = table?.columns.find(column => column.name === getValue('joinColumn'));
#   check.classList.remove('ok', 'error');
#   if (!left || !right) {
#     check.textContent = table ? '' : 'No available tables';
#     return;
#   }
#   if (typesCompatible(left.type, right.type)) {
#     check.textContent = `${normalizeColumnType(left.type)} match`;
#     check.classList.add('ok');
#   } else {
#     check.textContent = `${left.type} vs ${right.type}`;
#     check.classList.add('error');
#   }
# }
#
# function joinDraftFromFields() {
#   const left = customDraft.columns[Number(getValue('joinBaseColumn'))];
#   const table = getValue('joinTable');
#   const available = availableJoinTables().find(item => item.name === table);
#   const right = available?.columns.find(column => column.name === getValue('joinColumn'));
#   if (!left || !available || !right || !typesCompatible(left.type, right.type)) return null;
#   const sourceColumn = left.dict_column || String(left.source || '').split('.').pop() || left.name;
#   return {
#     join_type: getValue('joinType') || 'INNER',
#     base_alias: customDraft.filter.from_alias || aliasForTable(customDraft.filter.from_table),
#     base_column: sourceColumn,
#     operator: getValue('joinOperator') || '=',
#     table,
#     alias: aliasForTable(table),
#     column: right.name
#   };
# }
#
# function renderJoinLine(join) {
#   if (typeof join === 'string') return join;
#   const operator = join.operator || '=';
#   const joinType = join.join_type || 'INNER';
#   return `${joinType} JOIN ##JVM_${join.table} AS ${join.alias} ON ${join.base_alias}.${join.base_column} ${operator} ${join.alias}.${join.column}`;
# }
#
# function renderCustomJoinRows() {
#   const root = document.getElementById('customJoinRows');
#   if (!root) return;
#   root.innerHTML = customDraft.filter.join.map((join, index) => `
#     <div class="editor-row line-editor">
#       <label>join<input data-custom-line-field="join" data-index="${index}" value="${escapeAttr(renderJoinLine(join))}" ${typeof join === 'string' ? '' : 'readonly'}></label>
#       <button class="danger" data-remove-custom-line="join" data-index="${index}">Remove</button>
#     </div>
#   `).join('') || '<div class="empty">No join lines yet.</div>';
# }
#
# function renderCustomLineRows(rootId, field, placeholder) {
#   const root = document.getElementById(rootId);
#   if (!root) return;
#   root.innerHTML = customDraft.filter[field].map((line, index) => `
#     <div class="editor-row line-editor">
#       <label>${field}<input data-custom-line-field="${field}" data-index="${index}" value="${escapeAttr(line || '')}" placeholder="${escapeAttr(placeholder)}"></label>
#       <button class="danger" data-remove-custom-line="${field}" data-index="${index}">Remove</button>
#     </div>
#   `).join('') || `<div class="empty">No ${escapeHtml(field)} lines yet.</div>`;
# }
#
# function syncCustomFields() {
#   const oldTable = customDraft.filter.from_table;
#   const oldAlias = customDraft.filter.from_alias;
#   customDraft.name = getValue('customName');
#   customDraft.dest_table = getValue('customDestTable');
#   customDraft.type = getValue('customType') || 'fact';
#   customDraft.pull_this_cycle = !!document.getElementById('customPullThisCycle')?.checked;
#   customDraft.filter.from_table = getValue('customFromTable');
#   customDraft.filter.from_alias = getValue('customFromAlias');
#   if (customDraft.filter.from_alias && customDraft.filter.from_alias !== oldAlias) {
#     customDraft.columns = customDraft.columns.map(column => {
#       const dictCol = column.dict_column || String(column.source || '').split('.').pop() || column.name;
#       return { ...column, source: `${customDraft.filter.from_alias}.${dictCol}`, dict_column: dictCol };
#     });
#   }
#   if (customDraft.filter.from_table && customDraft.filter.from_table !== oldTable) {
#     if (!customDraft.filter.from_alias || customDraft.filter.from_alias === aliasForTable(oldTable)) {
#       customDraft.filter.from_alias = aliasForTable(customDraft.filter.from_table);
#       setValue('customFromAlias', customDraft.filter.from_alias);
#     }
#     resetColumnsForTable(customDraft.filter.from_table);
#     customDraft.filter.join = [];
#     renderCustomColumnRows();
#     renderJoinBuilder();
#     renderCustomJoinRows();
#   }
#   updatePkWarning();
# }
#
# function makeCustomCohort() {
#   syncCustomFields();
#   const cohort = {
#     name: customDraft.name || 'CustomCohort',
#     dest_table: customDraft.dest_table || customDraft.name || 'CustomCohort',
#     type: customDraft.type || 'fact',
#     pull_this_cycle: customDraft.pull_this_cycle !== false,
#     columns: customDraft.columns
#       .filter(column => column.source || column.name || column.type || column.nullable !== undefined)
#       .map(cleanColumn),
#     filter: {}
#   };
#   const fromTable = customDraft.filter.from_table;
#   const fromAlias = customDraft.filter.from_alias;
#   if (fromTable) cohort.filter.from = [fromAlias ? `${fromTable} as ${fromAlias}` : fromTable];
#   const joins = customDraft.filter.join.map(join => renderJoinLine(join).trim()).filter(Boolean);
#   const wheres = customDraft.filter.where.map(line => line.trim()).filter(Boolean);
#   if (joins.length) cohort.filter.join = joins;
#   if (wheres.length) cohort.filter.where = wheres;
#   return cohort;
# }
#
# function cleanColumn(column) {
#   const clean = {};
#   if (column.source) clean.source = column.source;
#   if (column.name) clean.name = column.name;
#   if (column.type) clean.type = column.type;
#   if (column.nullable !== undefined && column.nullable !== '') clean.nullable = column.nullable === true || column.nullable === 'true';
#   return clean;
# }
#
# function updatePkWarning() {
#   const warning = document.getElementById('pkWarning');
#   if (!warning) return;
#   const existingPk = draftTemplate.cohorts.some(cohort => {
#     if (String(cohort.type || '').toLowerCase() === 'pk') return true;
#     const recipe = recipeDefs.find(item => item.name === cohort.recipe);
#     return String(recipe?.type || '').toLowerCase() === 'pk';
#   });
#   const customPk = String(getValue('customType') || customDraft.type || '').toLowerCase() === 'pk';
#   warning.classList.toggle('hidden', !(existingPk && customPk));
# }
#
# function customRecipeText() {
#   const cohort = makeCustomCohort();
#   const recipeName = cohort.name || 'CustomRecipe';
#   const recipe = clone(cohort);
#   recipe.name = recipeName;
#   delete recipe.recipe;
#   return toYaml({ recipes: [recipe] });
# }
#
# function describeBatching(item) {
#   if (typeof item === 'number') return { name: 'chunk', chunk: String(item), values: [] };
#   if (typeof item === 'string') return { name: item, chunk: '', values: [] };
#   if (item && typeof item === 'object') {
#     if ('chunk' in item) return { name: 'chunk', chunk: String(item.chunk), values: [] };
#     if (item.name) return { name: item.name, chunk: String(item.rows_per_batch || ''), values: Array.isArray(item.values) ? item.values : [] };
#     const keys = Object.keys(item);
#     if (keys.length === 1) {
#       const name = keys[0];
#       const value = item[name];
#       if (value && typeof value === 'object') return { name, chunk: String(value.rows_per_batch || ''), values: Array.isArray(value.values) ? value.values : [] };
#       return { name, chunk: '', values: value == null ? [] : [String(value)] };
#     }
#   }
#   return { name: '', chunk: '', values: [] };
# }
#
# function batchingFromFields(name, value) {
#   if (name === 'chunk') {
#     const parsed = parseInt(value || '0', 10);
#     return { chunk: Number.isFinite(parsed) && parsed > 0 ? parsed : 2000 };
#   }
#   const values = Array.isArray(value) ? value : String(value || '').split(',').map(v => v.trim()).filter(Boolean);
#   if (values.length) {
#     return { [name]: { values, include_other: true } };
#   }
#   return name;
# }
#
# function updateBatchingHelp() {
#   const name = getValue('newBatchingRecipe');
#   const help = document.getElementById('batchingHelp');
#   const list = document.getElementById('batchingValueSuggestions');
#   const recipe = batchingRecipes.find(item => item.name === name) || {};
#   if (help) {
#     help.textContent = name === 'chunk'
#       ? 'Enter the number of PK rows per batch.'
#       : `Leave blank to batch by all ${name || 'selected'} values. If selecting specific values, all other values will be batched together.`;
#   }
#   if (list) {
#     const values = Array.isArray(recipe.values) ? recipe.values : [];
#     list.innerHTML = values.map(value => `<option value="${escapeAttr(value)}"></option>`).join('');
#   }
# }
#
# document.getElementById('addUpload')?.addEventListener('click', () => {
#   ensureDraftShape();
#   const name = getValue('newUploadName') || `Upload${draftTemplate.upload_cohorts.length + 1}`;
#   const fileType = getValue('newUploadType') || 'csv';
#   const upload = {
#     name,
#     dest_table: name,
#     file_type: fileType,
#     push_this_cycle: true
#   };
#   const fileLoc = getValue('newUploadPath');
#   if (fileLoc) upload.file_loc = fileLoc;
#   draftTemplate.upload_cohorts.push(upload);
#   setValue('newUploadName', '');
#   setValue('newUploadPath', '');
#   renderUploadRows();
#   updateDraftYaml();
# });
#
# document.getElementById('addBatching')?.addEventListener('click', () => {
#   ensureDraftShape();
#   const name = getValue('newBatchingRecipe');
#   if (!name) return;
#   draftTemplate.batching.push(batchingFromFields(name, getValue('newBatchingValue')));
#   setValue('newBatchingValue', '');
#   renderBatchingRows();
#   updateDraftYaml();
# });
#
# document.getElementById('addCohort')?.addEventListener('click', () => {
#   ensureDraftShape();
#   const recipe = getValue('newCohortRecipe');
#   if (!recipe) return;
#   if (recipe === '__custom__') {
#     document.getElementById('customName')?.focus();
#     return;
#   }
#   draftTemplate.cohorts.push({ recipe, name: getValue('newCohortName') || recipe });
#   setValue('newCohortName', '');
#   renderCohortRows();
#   updateDraftYaml();
# });
#
# document.getElementById('addCustomColumn')?.addEventListener('click', () => {
#   customDraft.columns.push({ source: '', name: '', type: '', nullable: '' });
#   renderCustomColumnRows();
# });
#
# document.getElementById('addCustomJoin')?.addEventListener('click', () => {
#   const join = joinDraftFromFields();
#   if (!join) {
#     updateJoinCheck();
#     return;
#   }
#   customDraft.filter.join.push(join);
#   renderJoinBuilder();
#   renderCustomJoinRows();
# });
#
# document.getElementById('addCustomWhere')?.addEventListener('click', () => {
#   customDraft.filter.where.push('');
#   renderCustomLineRows('customWhereRows', 'where', 'where condition');
# });
#
# document.getElementById('addCustomCohort')?.addEventListener('click', () => {
#   ensureDraftShape();
#   const cohort = makeCustomCohort();
#   if (editingCustomIndex === null) {
#     draftTemplate.cohorts.push(cohort);
#   } else {
#     draftTemplate.cohorts[editingCustomIndex] = cohort;
#   }
#   customDraft = blankCustomCohort();
#   editingCustomIndex = null;
#   renderCustomBuilder();
#   renderCohortRows();
#   updateDraftYaml();
# });
#
# document.getElementById('resetCustomCohort')?.addEventListener('click', () => {
#   customDraft = blankCustomCohort();
#   editingCustomIndex = null;
#   renderCustomBuilder();
# });
#
# document.getElementById('copyCustomRecipe')?.addEventListener('click', async () => {
#   const text = customRecipeText();
#   try {
#     await navigator.clipboard.writeText(text);
#   } catch (err) {
#     window.prompt('Copy custom recipe YAML:', text);
#   }
# });
#
# document.getElementById('downloadCustomRecipe')?.addEventListener('click', () => {
#   const text = customRecipeText();
#   const blob = new Blob([text], { type: 'text/yaml' });
#   const a = document.createElement('a');
#   const name = cleanDownloadName(`recipe_${makeCustomCohort().name || 'custom'}.yaml`);
#   a.href = URL.createObjectURL(blob);
#   a.download = name;
#   a.click();
#   URL.revokeObjectURL(a.href);
# });
#
# document.addEventListener('input', event => {
#   const target = event.target;
#   const index = Number(target.dataset.index);
#   if (target.dataset.uploadField) {
#     draftTemplate.upload_cohorts[index][target.dataset.uploadField] = target.value;
#     updateDraftYaml();
#   }
#   if (target.dataset.batchingField) {
#     const parsed = describeBatching(draftTemplate.batching[index]);
#     if (target.dataset.batchingField === 'name') {
#       draftTemplate.batching[index] = batchingFromFields(target.value, parsed.name === 'chunk' ? parsed.chunk : parsed.values);
#       renderBatchingRows();
#     } else {
#       draftTemplate.batching[index] = batchingFromFields(parsed.name, target.value);
#     }
#     updateDraftYaml();
#   }
#   if (target.dataset.cohortField) {
#     draftTemplate.cohorts[index][target.dataset.cohortField] = target.value;
#     updateDraftYaml();
#   }
#   if (target.dataset.customColumnField) {
#     const column = customDraft.columns[index];
#     const field = target.dataset.customColumnField;
#     column[field] = target.value;
#   }
#   if (target.dataset.customLineField) {
#     customDraft.filter[target.dataset.customLineField][index] = target.value;
#   }
# });
#
# document.addEventListener('change', event => {
#   const target = event.target;
#   const index = Number(target.dataset.index);
#   if (target.dataset.uploadField) {
#     draftTemplate.upload_cohorts[index][target.dataset.uploadField] = target.value;
#     updateDraftYaml();
#   }
#   if (target.dataset.batchingField) {
#     const parsed = describeBatching(draftTemplate.batching[index]);
#     if (target.dataset.batchingField === 'name') {
#       draftTemplate.batching[index] = batchingFromFields(target.value, parsed.name === 'chunk' ? parsed.chunk : parsed.values);
#       renderBatchingRows();
#     } else {
#       draftTemplate.batching[index] = batchingFromFields(parsed.name, target.value);
#     }
#     updateDraftYaml();
#   }
#   if (target.dataset.customColumnField) {
#     const column = customDraft.columns[index];
#     const field = target.dataset.customColumnField;
#     column[field] = target.value;
#   }
# });
#
# document.addEventListener('click', event => {
#   const target = event.target;
#   if (target.dataset.copyArtifact) {
#     const text = document.getElementById(target.dataset.copyArtifact)?.innerText || '';
#     navigator.clipboard.writeText(text).catch(() => window.prompt('Copy artifact:', text));
#   }
#   if (target.dataset.downloadArtifact) {
#     const text = document.getElementById(target.dataset.downloadArtifact)?.innerText || '';
#     const blob = new Blob([text], { type: 'text/yaml' });
#     const a = document.createElement('a');
#     a.href = URL.createObjectURL(blob);
#     a.download = cleanDownloadName(target.dataset.filename || 'artifact.yaml');
#     a.click();
#     URL.revokeObjectURL(a.href);
#   }
#   if (target.dataset.removeUpload) {
#     draftTemplate.upload_cohorts.splice(Number(target.dataset.removeUpload), 1);
#     renderUploadRows();
#     updateDraftYaml();
#   }
#   if (target.dataset.removeBatching) {
#     draftTemplate.batching.splice(Number(target.dataset.removeBatching), 1);
#     renderBatchingRows();
#     updateDraftYaml();
#   }
#   if (target.dataset.removeBatchingValue) {
#     const index = Number(target.dataset.removeBatchingValue);
#     const valueIndex = Number(target.dataset.valueIndex);
#     const parsed = describeBatching(draftTemplate.batching[index]);
#     parsed.values.splice(valueIndex, 1);
#     draftTemplate.batching[index] = batchingFromFields(parsed.name, parsed.values);
#     renderBatchingRows();
#     updateDraftYaml();
#   }
#   if (target.dataset.removeCohort) {
#     draftTemplate.cohorts.splice(Number(target.dataset.removeCohort), 1);
#     if (editingCustomIndex === Number(target.dataset.removeCohort)) {
#       customDraft = blankCustomCohort();
#       editingCustomIndex = null;
#       renderCustomBuilder();
#     }
#     renderCohortRows();
#     updateDraftYaml();
#   }
#   if (target.dataset.editCustomCohort) {
#     editingCustomIndex = Number(target.dataset.editCustomCohort);
#     customDraft = customDraftFromCohort(draftTemplate.cohorts[editingCustomIndex]);
#     renderCustomBuilder();
#     document.getElementById('customName')?.focus();
#   }
#   if (target.dataset.moveCustomColumn) {
#     const index = Number(target.dataset.moveCustomColumn);
#     const direction = Number(target.dataset.direction);
#     const next = index + direction;
#     if (next >= 0 && next < customDraft.columns.length) {
#       const [column] = customDraft.columns.splice(index, 1);
#       customDraft.columns.splice(next, 0, column);
#       renderCustomColumnRows();
#       renderJoinBuilder();
#     }
#   }
#   if (target.dataset.removeCustomColumn) {
#     const index = Number(target.dataset.removeCustomColumn);
#     const [column] = customDraft.columns.splice(index, 1);
#     const name = column?.dict_column || column?.name;
#     if (name && !(customDraft.removed_columns || []).includes(name)) {
#       customDraft.removed_columns = customDraft.removed_columns || [];
#       customDraft.removed_columns.push(name);
#     }
#     renderCustomColumnRows();
#     renderJoinBuilder();
#   }
#   if (target.dataset.addRemovedColumn) {
#     const name = target.dataset.addRemovedColumn;
#     customDraft.removed_columns = (customDraft.removed_columns || []).filter(column => column !== name);
#     customDraft.columns.push(columnFromDictionary(customDraft.filter.from_table, name, customDraft.filter.from_alias));
#     renderCustomColumnRows();
#     renderJoinBuilder();
#   }
#   if (target.dataset.removeCustomLine) {
#     customDraft.filter[target.dataset.removeCustomLine].splice(Number(target.dataset.index), 1);
#     if (target.dataset.removeCustomLine === 'join') {
#       renderJoinBuilder();
#       renderCustomJoinRows();
#     } else {
#       renderCustomLineRows('customWhereRows', 'where', 'where condition');
#     }
#   }
# });
#
# document.addEventListener('keydown', event => {
#   const target = event.target;
#   if (target.dataset.batchingAddValue && event.key === 'Enter') {
#     event.preventDefault();
#     const value = target.value.trim();
#     if (!value) return;
#     const index = Number(target.dataset.batchingAddValue);
#     const parsed = describeBatching(draftTemplate.batching[index]);
#     if (!parsed.values.includes(value)) parsed.values.push(value);
#     draftTemplate.batching[index] = batchingFromFields(parsed.name, parsed.values);
#     renderBatchingRows();
#     updateDraftYaml();
#   }
# });
#
# document.getElementById('builderNewTemplate')?.addEventListener('click', () => {
#   draftTemplate = blankTemplate();
#   hydrateBuilder();
# });
#
# document.getElementById('builderUseCurrent')?.addEventListener('click', () => {
#   draftTemplate = clone(initialTemplate);
#   hydrateBuilder();
# });
#
# document.getElementById('copyDraftYaml')?.addEventListener('click', async () => {
#   const text = document.getElementById('draftYaml')?.innerText || '';
#   try {
#     await navigator.clipboard.writeText(text);
#   } catch (err) {
#     window.prompt('Copy draft YAML:', text);
#   }
# });
#
# document.getElementById('downloadDraftYaml')?.addEventListener('click', () => {
#   const text = document.getElementById('draftYaml')?.innerText || '';
#   const blob = new Blob([text], { type: 'text/yaml' });
#   const a = document.createElement('a');
#   const projectName = draftTemplate.project_vars?.project_folder || draftTemplate.project_folder || 'draft';
#   const project = projectName.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'draft';
#   const requested = getValue('builderDraftFilename');
#   const filename = cleanDownloadName(requested || `${project}_Full.yaml`);
#   a.href = URL.createObjectURL(blob);
#   a.download = filename;
#   a.click();
#   URL.revokeObjectURL(a.href);
# });
#
# function cleanDownloadName(value) {
#   const cleaned = String(value || 'draft.yaml')
#     .replace(/[\\/:*?"<>|]+/g, '_')
#     .replace(/^_+|_+$/g, '');
#   return /\.ya?ml$/i.test(cleaned) ? cleaned : `${cleaned || 'draft'}.yaml`;
# }
#
# function updateDraftYaml() {
#   ensureDraftShape();
#   const clean = {
#     cosmos_vars: draftTemplate.cosmos_vars || {},
#     run_vars: draftTemplate.run_vars || {},
#     test_options: draftTemplate.test_options || {},
#     project_vars: draftTemplate.project_vars || {},
#     vars: draftTemplate.vars || {},
#     upload_cohorts: draftTemplate.upload_cohorts || [],
#     multipliers: draftTemplate.multipliers || [],
#     batching: draftTemplate.batching || [],
#     cohorts: draftTemplate.cohorts || []
#   };
#   document.getElementById('draftYaml').textContent = toYaml(clean);
# }
#
# function toYaml(value, indent = 0) {
#   const pad = ' '.repeat(indent);
#   if (Array.isArray(value)) {
#     if (value.length === 0) return '[]';
#     return value.map(item => {
#       if (item && typeof item === 'object' && !Array.isArray(item)) {
#         const entries = Object.entries(item);
#         if (entries.length === 0) return `${pad}- {}`;
#         return entries.map(([key, val], idx) => {
#           const prefix = idx === 0 ? `${pad}- ${key}:` : `${pad}  ${key}:`;
#           if (val && typeof val === 'object') return `${prefix}\n${toYaml(val, indent + 4)}`;
#           return `${prefix} ${formatScalar(val)}`;
#         }).join('\n');
#       }
#       if (Array.isArray(item)) return `${pad}-\n${toYaml(item, indent + 2)}`;
#       return `${pad}- ${formatScalar(item)}`;
#     }).join('\n');
#   }
#   if (value && typeof value === 'object') {
#     const entries = Object.entries(value).filter(([, val]) => val !== undefined);
#     if (entries.length === 0) return '{}';
#     return entries.map(([key, val]) => {
#       if (val && typeof val === 'object') return `${pad}${key}:\n${toYaml(val, indent + 2)}`;
#       return `${pad}${key}: ${formatScalar(val)}`;
#     }).join('\n');
#   }
#   return `${pad}${formatScalar(value)}`;
# }
#
# function formatScalar(value) {
#   if (value === null || value === undefined) return '';
#   if (typeof value === 'boolean') return value ? 'true' : 'false';
#   if (typeof value === 'number') return String(value);
#   const text = String(value);
#   if (text === '') return '""';
#   if (/[:#{}\[\]%,]|^\s|\s$/.test(text)) return JSON.stringify(text);
#   return text;
# }
#
# function escapeHtml(value) {
#   return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
# }
#
# function escapeAttr(value) {
#   return escapeHtml(value);
# }
#
# hydrateBuilder();
# """
#
#
# def render_dashboard(template_path: Path, recipes_path: Path, auto_refresh: int = 0) -> tuple[str, backend.CompileResult]:
#     result = backend.compile_dashboard(template_path=template_path, recipes_path=recipes_path, write=False)
#     return build_html(template_path, recipes_path, result, auto_refresh), result
#
#
# def resolve_workspace_path(value: str | Path) -> Path:
#     path = Path(value).expanduser()
#     if path.is_absolute():
#         return path
#     return PROJECT_ROOT / path
#
#
# def url_host(host: str) -> str:
#     if host in ("", "0.0.0.0"):
#         return "127.0.0.1"
#     if host == "::":
#         return "[::1]"
#     if ":" in host and not host.startswith("["):
#         return f"[{host}]"
#     return host
#
#
# def network_hosts() -> list[str]:
#     ipv4_hosts: list[str] = []
#     ipv6_hosts: list[str] = []
#     try:
#         infos = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
#     except OSError:
#         return []
#     for info in infos:
#         address = info[4][0]
#         try:
#             parsed = ipaddress.ip_address(address)
#         except ValueError:
#             continue
#         if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
#             continue
#         hosts = ipv4_hosts if parsed.version == 4 else ipv6_hosts
#         if address not in hosts:
#             hosts.append(address)
#     return (ipv4_hosts + ipv6_hosts)[:8]
#
#
# def dashboard_url(host: str, port: int, template: str, recipes: str) -> str:
#     query = urllib.parse.urlencode({"template": template, "recipes": recipes})
#     return f"http://{url_host(host)}:{port}/?{query}"
#
#
# def error_html(title: str, message: str) -> str:
#     return f"""<!doctype html>
# <html lang="en">
# <head>
#   <meta charset="utf-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1">
#   <title>{e(title)}</title>
#   <style>{CSS}</style>
# </head>
# <body class="dark">
#   <main>
#     <section class="block">
#       <h1>{e(title)}</h1>
#       <p>{e(message)}</p>
#       <form class="path-form" method="get" action="/">
#         <label>Template YAML<input name="template" type="text" value="YAMLs/template.yaml"></label>
#         <button type="submit">Refresh</button>
#       </form>
#     </section>
#   </main>
# </body>
# </html>
# """
#
#
# def serve_dashboard(
#     host: str,
#     port: int,
#     template: str,
#     recipes: str,
#     auto_refresh: int,
#     open_browser: bool,
#     browser_host: str | None = None,
# ) -> int:
#     default_template = template
#     default_recipes = recipes
#
#     class DashboardHandler(http.server.BaseHTTPRequestHandler):
#         def do_GET(self) -> None:
#             parsed = urllib.parse.urlparse(self.path)
#             if parsed.path not in ("/", "/dashboard", "/dashboard.html"):
#                 self.send_error(404)
#                 return
#             params = urllib.parse.parse_qs(parsed.query)
#             template_arg = params.get("template", [default_template])[0] or default_template
#             recipes_arg = params.get("recipes", [default_recipes])[0] or default_recipes
#             template_path = resolve_workspace_path(template_arg)
#             recipes_path = resolve_workspace_path(recipes_arg)
#             try:
#                 html_text, _ = render_dashboard(template_path, recipes_path, auto_refresh)
#                 status = 200
#             except Exception as exc:  # noqa: BLE001 - surfaced as a dashboard page for local UI use.
#                 html_text = error_html("Dashboard Refresh Failed", str(exc))
#                 status = 500
#             data = html_text.encode("utf-8")
#             self.send_response(status)
#             self.send_header("Content-Type", "text/html; charset=utf-8")
#             self.send_header("Content-Length", str(len(data)))
#             self.end_headers()
#             self.wfile.write(data)
#
#         def log_message(self, format: str, *args: Any) -> None:
#             print(f"[yamlmanager] {self.address_string()} - {format % args}")
#
#     class DashboardServer(http.server.ThreadingHTTPServer):
#         address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
#
#     try:
#         server = DashboardServer((host, port), DashboardHandler)
#     except OSError as exc:
#         print(f"[yamlmanager] Could not bind to {host}:{port}: {exc}", file=sys.stderr)
#         print("[yamlmanager] Try --host 127.0.0.1 for SSH tunnel use, --public for VM/LAN access, or --port 0 for a free port.", file=sys.stderr)
#         return 1
#
#     actual_host, actual_port = server.server_address[:2]
#     display_host = browser_host or actual_host
#     url = dashboard_url(display_host, int(actual_port), default_template, default_recipes)
#     print(f"Serving YAML Manager at {url}")
#     if actual_host in ("", "0.0.0.0", "::"):
#         extra_urls = [dashboard_url(host, int(actual_port), default_template, default_recipes) for host in network_hosts()]
#         if extra_urls:
#             print("Other reachable URLs may include:")
#             for extra_url in extra_urls:
#                 print(f"  {extra_url}")
#     print("Stop the manager with Ctrl+C, or the stop button in your editor.")
#     if open_browser:
#         if not webbrowser.open(url):
#             print("[yamlmanager] No browser was opened automatically; copy the URL above into a browser.")
#     try:
#         server.serve_forever()
#     except KeyboardInterrupt:
#         print("\nStopped YAML Manager.")
#     finally:
#         server.server_close()
#     return 0
#
#
# def main(argv: list[str] | None = None) -> int:
#     parser = argparse.ArgumentParser(description="Generate a static YAML Manager UI.")
#     parser.add_argument("--template", default="YAMLs/template.yaml")
#     parser.add_argument("--recipes", default="YAMLs/recipes.yaml")
#     parser.add_argument("--out", default=str(DEFAULT_OUT))
#     parser.add_argument("--open", action="store_true", help="Open the generated dashboard in a browser.")
#     parser.add_argument("--serve", action="store_true", help="Run a local dashboard server so template paths can be changed in the UI.")
#     parser.add_argument("--static", action="store_true", help="Write a static dashboard file instead of starting the local server when no args are provided.")
#     parser.add_argument("--export-preyaml", choices=("symbolic", "expanded-recipes"), help="Write a pre-YAML artifact and exit.")
#     parser.add_argument("--export-split", action="store_true", help="Write split YAML artifacts and pullmanifest.yaml, then exit.")
#     parser.add_argument("--out-dir", help="Directory for split export artifacts.")
#     parser.add_argument("--host", default=DEFAULT_HOST, help=f"Address to bind. Defaults to {DEFAULT_HOST!r}, or YAMLMANAGER_HOST/MANAGER_UI_HOST.")
#     parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind. Use 0 for a free port. Defaults to {DEFAULT_PORT}, or YAMLMANAGER_PORT/MANAGER_UI_PORT.")
#     parser.add_argument("--public", action="store_true", help="Bind to all interfaces unless --host is also supplied.")
#     parser.add_argument("--browser-host", help="Host name to use in printed/opened URLs when it differs from the bind address.")
#     parser.add_argument("--no-open", action="store_true", help="Do not try to open a browser when serving with no arguments.")
#     parser.add_argument("--auto-refresh", type=int, default=0, help="Add browser auto-refresh, in seconds. Use 5 for every five seconds.")
#     raw_argv = sys.argv[1:] if argv is None else argv
#     open_by_default = not raw_argv
#     args = parser.parse_args(raw_argv)
#     explicit_host = any(arg == "--host" or arg.startswith("--host=") for arg in raw_argv)
#     explicit_out = any(arg == "--out" or arg.startswith("--out=") for arg in raw_argv)
#     if args.public and not explicit_host:
#         args.host = "0.0.0.0"
#
#     if args.export_preyaml:
#         result = backend.build_preyaml(
#             template_path=resolve_workspace_path(args.template),
#             recipes_path=resolve_workspace_path(args.recipes),
#             output_path=args.out if explicit_out else None,
#             mode=args.export_preyaml,
#             write=True,
#         )
#         print_messages(result)
#         if result.ok:
#             print(f"Wrote {result.output_path}")
#         else:
#             print("FAILED: errors block pre-YAML export")
#         return 0 if result.ok else 1
#
#     if args.export_split:
#         result = backend.write_split_artifacts(
#             template_path=resolve_workspace_path(args.template),
#             recipes_path=resolve_workspace_path(args.recipes),
#             output_dir=args.out_dir,
#         )
#         print_messages(result)
#         if result.ok:
#             print(f"Wrote split artifacts to {result.analysis.get('split_output_dir')}")
#             print(f"Manifest: {result.output_path}")
#         else:
#             print("FAILED: errors block split export")
#         return 0 if result.ok else 1
#
#     if args.serve or (open_by_default and not args.static):
#         return serve_dashboard(
#             args.host,
#             args.port,
#             args.template,
#             args.recipes,
#             args.auto_refresh,
#             open_browser=not args.static and not args.no_open,
#             browser_host=args.browser_host,
#         )
#
#     template_path = resolve_workspace_path(args.template)
#     recipes_path = resolve_workspace_path(args.recipes)
#
#     out_path = Path(args.out)
#     html_text, result = render_dashboard(template_path, recipes_path, args.auto_refresh)
#     out_path.parent.mkdir(parents=True, exist_ok=True)
#     out_path.write_text(html_text, encoding="utf-8")
#
#     print(f"Wrote {out_path}")
#     print(f"Status: {'Ready' if result.ok else 'Blocked'}")
#     print(f"Errors: {len(result.errors)}")
#     print(f"Warnings: {len(result.warnings)}")
#     if args.open or open_by_default:
#         webbrowser.open(out_path.resolve().as_uri())
#     return 0
#
#
# if __name__ == "__main__":
#     raise SystemExit(main())
#
# === END FILE: scripts/yamlmanager.py ===
# === BEGIN FILE: scripts/yamlmanager_backend.py SHA256: 940c80eea5f71b870ff08d72fce4c405e4ac2950c1c624a7399104681a66f20f SIZE: 2307 ===
# """
# Stable Python API for YAML Manager frontends.
#
# Frontends should import this module instead of reaching into makeYaml directly.
# That keeps CLI/UI/Desktop naming independent from the compiler implementation.
# """
#
# from __future__ import annotations
#
# from pathlib import Path
# from typing import Any
#
# import makeYaml
#
#
# CompileResult = makeYaml.CompileResult
#
#
# def load_document(path: str | Path) -> Any:
#     return makeYaml.load_yaml(path)
#
#
# def compile_dashboard(template_path: str | Path, recipes_path: str | Path, write: bool = False) -> CompileResult:
#     return makeYaml.compile_yaml(template_path=template_path, recipes_path=recipes_path, write=write)
#
#
# def build_preyaml(
#     template_path: str | Path,
#     recipes_path: str | Path,
#     output_path: str | Path | None = None,
#     mode: str = "symbolic",
#     write: bool = False,
# ) -> CompileResult:
#     return makeYaml.build_preyaml(
#         template_path=template_path,
#         recipes_path=recipes_path,
#         output_path=output_path,
#         mode=mode,
#         write=write,
#     )
#
#
# def plan_split_runs(template_path: str | Path, recipes_path: str | Path) -> CompileResult:
#     return makeYaml.plan_split_runs(template_path=template_path, recipes_path=recipes_path)
#
#
# def build_pullmanifest(
#     template_path: str | Path,
#     recipes_path: str | Path,
#     output_path: str | Path | None = None,
#     write: bool = False,
# ) -> CompileResult:
#     return makeYaml.build_pullmanifest(
#         template_path=template_path,
#         recipes_path=recipes_path,
#         output_path=output_path,
#         write=write,
#     )
#
#
# def write_split_artifacts(
#     template_path: str | Path,
#     recipes_path: str | Path,
#     output_dir: str | Path | None = None,
# ) -> CompileResult:
#     return makeYaml.write_split_artifacts(
#         template_path=template_path,
#         recipes_path=recipes_path,
#         output_dir=output_dir,
#     )
#
#
# def dump_yaml_text(data: Any) -> str:
#     return makeYaml.dump_yaml_text(data)
#
#
# def recipe_output_columns(recipe: dict[str, Any]) -> list[str]:
#     return makeYaml.output_columns(recipe)
#
#
# def recipe_required_vars(recipe: dict[str, Any]) -> dict[str, Any]:
#     return makeYaml.infer_required_vars(recipe)
#
#
# def recipe_table_inputs(recipe: dict[str, Any]) -> dict[str, list[str]]:
#     return makeYaml.infer_table_inputs(recipe)
#
# === END FILE: scripts/yamlmanager_backend.py ===
