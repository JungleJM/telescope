#!/usr/bin/env python3
"""Move an R package library to the offline VM as pasteable text.

On the Mac:

    python3 phewas_transfer.py pack ~/phewas_transfer_build/Rlib ~/phewas_transfer_build/parts

On the VM:

    python phewas_transfer.py unpack  C:\\phewas_offline\\parts C:\\phewas_offline\\Rlib
    python phewas_transfer.py promote C:\\phewas_offline\\Rlib

pack zips the library, base64-encodes it at 76 characters a line and splits it
into numbered parts. Every part carries its own hash and the whole zip's, so a
bad paste is caught and names the one part to copy again.

unpack reads every .txt file in the parts folder, checks each part and the
rebuilt zip against the hashes embedded at build time, and extracts into
<Rlib>.new. The live library is not touched.

promote swaps <Rlib>.new into place once check_phewas_lib() has passed on it,
keeping the previous library as <Rlib>.old.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

HEAD_RE = re.compile(r"^# PHEWAS-TRANSFER v1 part (\d+)/(\d+)$")
ZIP_RE = re.compile(r"^# zip-sha256: ([0-9a-f]{64}) zip-bytes: (\d+)$")
PART_RE = re.compile(r"^# part-sha256: ([0-9a-f]{64})$")
END_RE = re.compile(r"^# END part (\d+)/(\d+)$")
B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")
CHECK_MARKER = ".check-passed"


LINE_WIDTH = 76
LINES_PER_PART = 13000  # about 1 MB of text a part


class TransferError(Exception):
    pass


def pack(lib: Path, out: Path, lines_per_part: int) -> None:
    if not lib.is_dir():
        raise TransferError(f"No library at {lib}")
    if (lib / CHECK_MARKER).exists():
        raise TransferError(f"{lib} holds a VM check marker; rebuild it cleanly")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Entries sit at the zip root, with no Rlib/ folder; sorted, so the
        # same library always gives the same order.
        for path in sorted(lib.rglob("*")):
            # Skip Finder's .DS_Store and AppleDouble ._ files.
            if path.is_file() and path.name != ".DS_Store" and not path.name.startswith("._"):
                zf.write(path, path.relative_to(lib).as_posix())
    data = buf.getvalue()
    zip_sha = hashlib.sha256(data).hexdigest()
    text = base64.b64encode(data).decode("ascii")
    lines = [text[i:i + LINE_WIDTH] for i in range(0, len(text), LINE_WIDTH)]
    total = max(1, -(-len(lines) // lines_per_part))

    if out.exists():
        for old in out.glob("part*.txt"):
            old.unlink()
    out.mkdir(parents=True, exist_ok=True)
    for n in range(1, total + 1):
        chunk = lines[(n - 1) * lines_per_part:n * lines_per_part]
        part_sha = hashlib.sha256("".join(chunk).encode("ascii")).hexdigest()
        body = [
            f"# PHEWAS-TRANSFER v1 part {n}/{total}",
            f"# zip-sha256: {zip_sha} zip-bytes: {len(data)}",
            f"# part-sha256: {part_sha}",
            *chunk,
            f"# END part {n}/{total}",
        ]
        (out / f"part{n:02d}.txt").write_text("\n".join(body) + "\n", encoding="ascii")
    print(f"{total} part(s) in {out}: zip {len(data)} bytes, sha256 {zip_sha}")


def read_parts(folder: Path) -> bytes:
    """Return the zip bytes, or raise naming the part that needs re-copying."""
    files = sorted(folder.glob("*.txt"))
    if not files:
        raise TransferError(f"No .txt files in {folder}")
    parts = {}
    for f in files:
        # utf-8-sig: Notepad may save a byte-order mark.
        lines = [line.strip() for line in f.read_text(encoding="utf-8-sig").splitlines()]
        i = 0
        while i < len(lines):
            if not lines[i]:
                i += 1
                continue
            head = HEAD_RE.match(lines[i])
            if not head:
                raise TransferError(
                    f"{f.name} line {i + 1}: expected a part header, found {lines[i][:40]!r}"
                )
            n, total = int(head[1]), int(head[2])
            zip_m = ZIP_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
            part_m = PART_RE.match(lines[i + 2]) if i + 2 < len(lines) else None
            if not (zip_m and part_m):
                raise TransferError(f"{f.name}: the header of part {n} is incomplete")
            body = []
            j = i + 3
            while j < len(lines) and not lines[j].startswith("# END"):
                if lines[j]:
                    if not B64_RE.match(lines[j]):
                        raise TransferError(
                            f"{f.name} line {j + 1}: not base64 -- re-copy part {n}"
                        )
                    body.append(lines[j])
                j += 1
            end = END_RE.match(lines[j]) if j < len(lines) else None
            if not end or (int(end[1]), int(end[2])) != (n, total):
                raise TransferError(
                    f"{f.name}: part {n} has no matching END line -- the paste was cut short"
                )
            text = "".join(body)
            if hashlib.sha256(text.encode("ascii")).hexdigest() != part_m[1]:
                raise TransferError(f"{f.name}: part {n} does not match its hash -- re-copy part {n}")
            if n in parts:
                raise TransferError(f"Part {n} appears twice -- delete the extra copy")
            parts[n] = (total, zip_m[1], int(zip_m[2]), text)
            i = j + 1

    builds = {(p[0], p[1], p[2]) for p in parts.values()}
    if len(builds) != 1:
        raise TransferError(
            "The parts come from different builds -- empty the folder and copy one build's parts"
        )
    total, zip_sha, zip_bytes = builds.pop()
    missing = sorted(set(range(1, total + 1)) - set(parts))
    if missing:
        raise TransferError(f"Missing part(s) {missing} of {total}")

    data = base64.b64decode("".join(parts[k][3] for k in range(1, total + 1)), validate=True)
    if len(data) != zip_bytes or hashlib.sha256(data).hexdigest() != zip_sha:
        raise TransferError("Every part verified but the rebuilt zip does not -- rebuild the transfer")
    print(f"Verified {total} part(s): {zip_bytes} bytes, sha256 {zip_sha}")
    return data


def safe_member(name: str) -> PurePosixPath:
    # Some Windows zip writers use backslashes; the path rules are the same.
    posix = name.replace("\\", "/")
    rel = PurePosixPath(posix)
    if posix.startswith("/") or DRIVE_RE.match(posix) or ".." in rel.parts:
        raise TransferError(f"Unsafe path in zip: {name!r}")
    return rel


def unpack(parts_dir: Path, lib: Path) -> None:
    data = read_parts(parts_dir)
    staging = lib.with_name(lib.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                rel = safe_member(info.filename)
                target = staging.joinpath(*rel.parts)
                if info.filename.endswith(("/", "\\")):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    packages = sorted(p.name for p in staging.iterdir() if (p / "DESCRIPTION").is_file())
    print(f"Unpacked {len(packages)} package(s) into {staging}:")
    print("  " + ", ".join(packages))
    print("Next: run check_phewas_lib() on that folder in R, restart R, then promote.")


def promote(lib: Path) -> None:
    staging = lib.with_name(lib.name + ".new")
    old = lib.with_name(lib.name + ".old")
    if not staging.is_dir():
        raise TransferError(f"Nothing to promote: {staging} does not exist. Run unpack first.")
    if not (staging / CHECK_MARKER).is_file():
        raise TransferError(f"check_phewas_lib() has not passed on {staging}. Run it first.")
    moved_live = False
    try:
        if lib.exists():
            if old.exists():
                shutil.rmtree(old)
            lib.rename(old)
            moved_live = True
        staging.rename(lib)
    except PermissionError as e:
        if moved_live and not lib.exists():
            old.rename(lib)
        raise TransferError(
            f"Windows would not move {e.filename}. Close every R or RStudio session "
            "that has loaded packages from it, then run promote again."
        ) from None
    print(f"{lib} is now the checked library." + (f" The previous one is {old}." if moved_live else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_pack = sub.add_parser("pack", help="zip and encode a library into text parts")
    p_pack.add_argument("lib", type=Path)
    p_pack.add_argument("out", type=Path)
    p_pack.add_argument("--lines-per-part", type=int, default=LINES_PER_PART)
    p_unpack = sub.add_parser("unpack", help="verify the parts and extract into <lib>.new")
    p_unpack.add_argument("parts_dir", type=Path)
    p_unpack.add_argument("lib", type=Path)
    p_promote = sub.add_parser("promote", help="swap a checked <lib>.new into place")
    p_promote.add_argument("lib", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            pack(args.lib.expanduser(), args.out.expanduser(), args.lines_per_part)
        elif args.command == "unpack":
            unpack(args.parts_dir, args.lib)
        else:
            promote(args.lib)
    except TransferError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
