"""What the bundle says about itself: nothing (D200, D201).

The Mac's sources describe how the software reaches the VM: bundling,
extraction, the Mac. The VM is shown as if the system were built and run
there, so the bundle's copy of each file drops that prose, and a few messages
a VM user sees are reworded. The Mac's files are not touched.

- `cut`: drops every block of lines between `# mac-only {` and `# } mac-only`
  (D205): code that exists only on the Mac, such as Author's Exports tab and
  the bundle queue, leaves no trace in the bundle. A block left open, or
  closed without opening, stops the build.
- `reword`: exact replacements, file by file (VM_WORDING). One that no longer
  matches stops the build, so the table cannot drift from the code silently.
- `scrub`: drops every comment block and docstring paragraph in Python, every
  HTML comment in Markdown, and every comment block in YAML that mentions
  delivery (DELIVERY_WORDS). The Python must still compile.
- `mentions`: what is left that should not be, for the build's guard.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

DELIVERY_WORDS = re.compile(
    r"bundl|extract|\bMac\b|macOS|to the VM|off the VM|onto the VM|from the VM|carry across"
    r"|copied over|copy (it |them )?over|\bshipped\b|plan that travels|travels with it",
    re.IGNORECASE,
)
SCREENSHOT_WORDS = re.compile(r"transcri|screenshot", re.IGNORECASE)
# Names the VM must not show anywhere, code included (D205).
RETIRED_NAMES = re.compile(r"(?i:transfer yaml|telescope)|\bExports\b")
# Real names that only look like delivery: an R package in the VM's list.
ALLOWED_LINES = (re.compile(r'^\s*- name: "bundle v[\d.]+"$'),)


class ScrubError(ValueError):
    pass


# Messages a VM user can see, and what the bundle says instead. Code names are
# renamed in the source (D202); the Mac-only code is cut (D205).
VM_WORDING: dict[str, list[tuple[str, str]]] = {
    "pullmanager/databases.py": [('"pullmanager/config.py and bundle again, "', '"pullmanager/config.py, "')],
    "pullmanager/launcher.py": [('"Run it from an extracted bundle."', '"Run it from the folder that holds them."')],
    "scripts/makeYaml.py": [
        ('fix="Recipes are kept on the Mac (D49). There, export this template with "\n'
         '            "`makeYaml.py --export-blueprint`, which writes every recipe out in full, and "\n'
         '            "bring that file across. Or pass `--recipes` with the recipes file.",',
         'fix="Pass `--recipes` with the recipes file, or use a template with every recipe "\n'
         '            "written out in full (`makeYaml.py --export-blueprint` writes one).",'),
        ("(`sex`, Mac only), ", "(`sex`), "),
        ('"YAMLs/datadictionary.yaml on the Mac and rebuild the bundle."', '"the data dictionary."'),
    ],
    "scripts/yamlmanager_model.py": [
        ('f"No recipes file here ({self.recipes_path.name}): recipes stay on the "\n'
         '                    "Mac (D49), and a blueprint carries its own written out.")',
         'f"No recipes file here ({self.recipes_path.name}): a blueprint "\n'
         '                    "carries its recipes written out.")'),
        ('"Pending transfer is for the Mac: here on the VM the file must be "',
         '"Pending transfer is not offered here: the file must be "'),
        ('f"There is no {recipes_path.name} here: recipes are kept on the Mac (D49), "\n'
         '                         f"so {what} is saved there.")',
         'f"There is no {recipes_path.name} here, "\n'
         '                         f"so {what} cannot be saved.")'),
        ('f" Not here yet, to copy to the VM: {', 'f" Not here yet: {'),
        ('self.assertIn("Mac", message)', 'self.assertIn("cannot be saved", message)'),
    ],
}


CUT_OPEN = re.compile(r"^\s*# mac-only \{\s*$")
CUT_CLOSE = re.compile(r"^\s*# \} mac-only\s*$")


def cut(published: str, text: str) -> str:
    """The file without its mac-only blocks, markers included (D205)."""
    out: list[str] = []
    opened = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if CUT_OPEN.match(line):
            if opened:
                raise ScrubError(f"{published}:{number}: a mac-only block opens inside the one "
                                 f"opened at line {opened}; close that one first.")
            opened = number
        elif CUT_CLOSE.match(line):
            if not opened:
                raise ScrubError(f"{published}:{number}: `# }} mac-only` closes a block that never opened.")
            opened = 0
        elif not opened:
            out.append(line)
    if opened:
        raise ScrubError(f"{published}:{opened}: this mac-only block is never closed with `# }} mac-only`.")
    return "".join(out)


def reword(published: str, text: str) -> str:
    for old, new in VM_WORDING.get(published, []):
        if old not in text:
            raise ScrubError(
                f"{published}: the wording to replace is no longer there: {old[:80]!r}. "
                "Update VM_WORDING in scripts/bundle_scrub.py to match the code (D201).")
        text = text.replace(old, new)
    return text


def _docstrings(tree: ast.AST) -> list[tuple[ast.AST, ast.Expr]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.append((node, first))
    return found


def _comment_lines(text: str) -> list[tuple[int, int, str]]:
    """(line, column, text) of every comment."""
    return [(t.start[0], t.start[1], t.string)
            for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]


def _drop_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    full = {}  # line number -> indentation, for whole-line comments
    for number, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            full[number] = len(line) - len(stripped)
    drop: set[int] = set()
    cut: dict[int, int] = {}
    for number, column, comment in _comment_lines(text):
        if not DELIVERY_WORDS.search(comment) or number == 1 and comment.startswith("#!"):
            continue
        if number in full:
            # The whole block it belongs to: a sentence often runs on.
            indent = full[number]
            low = high = number
            while low - 1 in full and full[low - 1] == indent:
                low -= 1
            while high + 1 in full and full[high + 1] == indent:
                high += 1
            drop.update(range(low, high + 1))
        else:
            cut[number] = column
    out = []
    for number, line in enumerate(lines, 1):
        if number in drop:
            continue
        if number in cut:
            line = line[:cut[number]].rstrip() + "\n"
        out.append(line)
    return "".join(out)


_QUOTE = re.compile(r'^(?P<prefix>[rRuU]?)(?P<quote>"""|\'\'\'|"|\')')


_LIST_LINE = re.compile(r"^\s*(python3? |[-*] |\d+\.\s)|\s#\s")


def _without_delivery(paragraph: str) -> str:
    """A docstring paragraph without its delivery prose: a list or a block of
    commands loses only its matching lines; prose loses the whole paragraph,
    since its sentences lean on each other."""
    if not DELIVERY_WORDS.search(paragraph):
        return paragraph
    lines = paragraph.split("\n")
    if all(_LIST_LINE.search(line) for line in lines if line.strip()):
        return "\n".join(line for line in lines if not DELIVERY_WORDS.search(line))
    return ""


def _drop_docstring_paragraphs(text: str) -> str:
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    # Bottom up, so earlier positions stay right.
    for owner, expr in sorted(_docstrings(tree), key=lambda pair: pair[1].lineno, reverse=True):
        start = sum(len(line) for line in lines[:expr.lineno - 1]) + expr.col_offset
        end = sum(len(line) for line in lines[:expr.end_lineno - 1]) + expr.end_col_offset
        source = "".join(lines)
        literal = source[start:end]
        match = _QUOTE.match(literal)
        if not match or not DELIVERY_WORDS.search(literal):
            continue
        quote = match.group("quote")
        head = match.group(0)
        body = literal[len(head):len(literal) - len(quote)]
        tail = re.search(r"\s*$", body).group(0)
        paragraphs = re.split(r"\n[ \t]*\n", body.rstrip())
        kept = [k for k in (_without_delivery(p) for p in paragraphs) if k.strip()]
        if kept:
            if kept[0] is not paragraphs[0] and kept[0] != paragraphs[0]:
                # The opening went: start on the next line, at its indent.
                kept[0] = "\n" + kept[0].lstrip("\n")
            new_literal = head + "\n\n".join(kept) + (tail if "\n" in tail else "") + quote
            source = source[:start] + new_literal + source[end:]
        elif len(owner.body) > 1:
            # The whole statement goes, with its line.
            line_start = source.rfind("\n", 0, start) + 1
            line_end = source.find("\n", end)
            line_end = len(source) if line_end == -1 else line_end + 1
            source = source[:line_start] + source[line_end:]
        else:
            source = source[:start] + "pass" + source[end:]
        lines = source.splitlines(keepends=True)
    return "".join(lines)


def scrub_python(published: str, text: str) -> str:
    text = _drop_comments(text)
    text = _drop_docstring_paragraphs(text)
    try:
        compile(text, published, "exec")
    except SyntaxError as exc:
        raise ScrubError(f"{published}: no longer compiles once its delivery prose is dropped: {exc}") from exc
    return text


def scrub_markdown(text: str) -> str:
    return re.sub(r"<!--.*?-->\n?", lambda m: "" if DELIVERY_WORDS.search(m.group(0)) else m.group(0),
                  text, flags=re.DOTALL)


def scrub_yaml(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out, block = [], []

    def flush():
        if not any(DELIVERY_WORDS.search(line) for line in block):
            out.extend(block)
        block.clear()

    for line in lines:
        if line.lstrip().startswith("#"):
            block.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "".join(out)


def scrub(published: str, text: str) -> str:
    text = reword(published, cut(published, text))
    if published.endswith(".py"):
        return scrub_python(published, text)
    if published.endswith(".md"):
        return scrub_markdown(text)
    if published.endswith((".yaml", ".yml")):
        return scrub_yaml(text)
    return text


def mentions(published: str, text: str) -> list[str]:
    """Lines that still say what the bundle must not (D200, D201). Python's
    code and strings are left to the wording table; its comments and
    docstrings, and every other file's lines, are checked here."""
    found = [f"{published}:{n}: {line.strip()[:100]}"
             for n, line in enumerate(text.splitlines(), 1)
             if SCREENSHOT_WORDS.search(line) or RETIRED_NAMES.search(line)]
    if published.endswith(".py"):
        prose = [(n, comment) for n, _column, comment in _comment_lines(text)]
        lines = text.splitlines()
        for _owner, expr in _docstrings(ast.parse(text)):
            prose += [(n, lines[n - 1]) for n in range(expr.lineno, expr.end_lineno + 1)]
        found += [f"{published}:{n}: {line.strip()[:100]}" for n, line in sorted(set(prose))
                  if DELIVERY_WORDS.search(line) and not line.startswith("#!")]
    else:
        found += [f"{published}:{n}: {line.strip()[:100]}" for n, line in enumerate(text.splitlines(), 1)
                  if DELIVERY_WORDS.search(line) and not any(a.match(line) for a in ALLOWED_LINES)]
    return found
