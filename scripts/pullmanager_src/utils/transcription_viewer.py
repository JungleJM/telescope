#!/usr/bin/env python3
"""Desktop viewer for turning text, Markdown, or YAML into screenshot pages.

Run on the VM:
    python utils/transcription_viewer.py

Optional:
    python utils/transcription_viewer.py path/to/file.md

Uses only Python batteries plus packages listed in YAMLs/DSVM Plugins.yaml:
    pillow, pyyaml
"""

from __future__ import annotations

import io
import re
import sys
import textwrap
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageTk


PAGE_PRESETS = {
    "Window": None,
    "1920 x 1080": (1920, 1080),
    "2560 x 1440": (2560, 1440),
    "3840 x 2160": (3840, 2160),
    "PDF landscape": (2200, 1700),
    "PDF portrait": (1700, 2200),
}

MONO_FONT_CANDIDATES = [
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
]


@dataclass(frozen=True)
class Section:
    label: str
    text: str


@dataclass(frozen=True)
class RenderSettings:
    width: int
    height: int
    columns: int
    font_size: int
    line_spacing: int
    margin: int
    line_numbers: bool
    dark_mode: bool


@dataclass(frozen=True)
class Page:
    number: int
    total: int
    section_label: str
    column_lines: tuple[tuple[str, ...], ...]


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def normalize_yaml_if_requested(name: str, text: str, pretty: bool) -> str:
    if not pretty or not name.lower().endswith((".yaml", ".yml")):
        return text
    try:
        import yaml

        parsed = yaml.safe_load(text)
        return yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=1000)
    except Exception:
        return text


def extract_fenced_code(text: str) -> list[Section]:
    pattern = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)(?:\n```|$)", re.DOTALL)
    sections: list[Section] = []
    for index, match in enumerate(pattern.finditer(text), start=1):
        lang = match.group("lang").strip() or "code"
        body = match.group("body").rstrip("\n")
        sections.append(Section(f"code block {index} ({lang})", body))
    return sections


def make_sections(name: str, text: str) -> list[Section]:
    sections = [Section(f"{name} - full source", text.rstrip("\n"))]
    sections.extend(extract_fenced_code(text))
    return sections


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in MONO_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    for name in ("DejaVuSansMono.ttf", "Consolas.ttf", "Courier New.ttf", "LiberationMono-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def text_metrics(font: ImageFont.ImageFont, line_spacing: int) -> tuple[int, int]:
    probe = Image.new("RGB", (100, 100))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), "M", font=font)
    char_width = max(1, bbox[2] - bbox[0])
    line_height = max(1, bbox[3] - bbox[1] + line_spacing)
    return char_width, line_height


def visual_lines(text: str, chars_per_line: int, line_numbers: bool) -> list[str]:
    rendered: list[str] = []
    raw_lines = text.splitlines() or [""]
    number_width = max(4, len(str(len(raw_lines))))
    width = max(20, chars_per_line)

    for line_no, raw_line in enumerate(raw_lines, start=1):
        expanded = raw_line.expandtabs(4)
        prefix = f"{line_no:>{number_width}} | " if line_numbers else ""
        continuation_prefix = f"{'':>{number_width}} : " if line_numbers else "  "
        available = max(12, width - len(prefix))
        wrapped = textwrap.wrap(
            expanded,
            width=available,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        for index, chunk in enumerate(wrapped):
            rendered.append((prefix if index == 0 else continuation_prefix) + chunk)
    return rendered


def paginate(section: Section, settings: RenderSettings) -> list[Page]:
    font = find_font(settings.font_size)
    char_width, line_height = text_metrics(font, settings.line_spacing)
    header_height = int(settings.font_size * 2.4)
    footer_height = int(settings.font_size * 2.1)
    gutter = settings.margin // 2
    usable_width = settings.width - (settings.margin * 2) - (gutter * (settings.columns - 1))
    column_width = max(200, usable_width // settings.columns)
    chars_per_line = max(24, column_width // char_width)
    usable_height = settings.height - (settings.margin * 2) - header_height - footer_height
    lines_per_column = max(6, usable_height // line_height)
    lines_per_page = max(1, lines_per_column * settings.columns)
    lines = visual_lines(section.text, chars_per_line, settings.line_numbers)
    total = max(1, (len(lines) + lines_per_page - 1) // lines_per_page)
    pages: list[Page] = []

    for page_index in range(total):
        start = page_index * lines_per_page
        page_lines = lines[start : start + lines_per_page]
        columns: list[tuple[str, ...]] = []
        for column_index in range(settings.columns):
            c_start = column_index * lines_per_column
            c_end = c_start + lines_per_column
            columns.append(tuple(page_lines[c_start:c_end]))
        pages.append(Page(page_index + 1, total, section.label, tuple(columns)))
    return pages


def render_page(page: Page, settings: RenderSettings, source_name: str) -> Image.Image:
    bg = (10, 14, 18) if settings.dark_mode else (255, 255, 255)
    ink = (232, 238, 245) if settings.dark_mode else (18, 24, 31)
    muted = (147, 161, 176) if settings.dark_mode else (86, 96, 108)
    guide = (65, 78, 92) if settings.dark_mode else (218, 224, 232)
    banner = (31, 44, 59) if settings.dark_mode else (239, 244, 249)

    image = Image.new("RGB", (settings.width, settings.height), bg)
    draw = ImageDraw.Draw(image)
    font = find_font(settings.font_size)
    header_font = find_font(max(16, int(settings.font_size * 0.8)))
    footer_font = find_font(max(14, int(settings.font_size * 0.72)))
    _, line_height = text_metrics(font, settings.line_spacing)

    header_h = int(settings.font_size * 2.4)
    footer_h = int(settings.font_size * 2.1)
    draw.rectangle((0, 0, settings.width, header_h), fill=banner)
    draw.rectangle((0, settings.height - footer_h, settings.width, settings.height), fill=banner)

    title = f"{source_name} | {page.section_label}"
    page_id = f"P{page.number:03d} / P{page.total:03d}"
    draw.text((settings.margin, int(settings.font_size * 0.45)), title, fill=ink, font=header_font)
    id_w = draw.textlength(page_id, font=header_font)
    draw.text((settings.width - settings.margin - id_w, int(settings.font_size * 0.45)), page_id, fill=ink, font=header_font)

    begin = f"BEGIN P{page.number:03d}"
    if page.number > 1:
        begin += f" - continues from P{page.number - 1:03d}"
    draw.text((settings.margin, settings.height - footer_h + 8), begin, fill=muted, font=footer_font)

    end = f"END P{page.number:03d}"
    if page.number < page.total:
        end += f" - continue to P{page.number + 1:03d}"
    end_w = draw.textlength(end, font=footer_font)
    draw.text((settings.width - settings.margin - end_w, settings.height - footer_h + 8), end, fill=muted, font=footer_font)

    gutter = settings.margin // 2
    usable_width = settings.width - (settings.margin * 2) - (gutter * (settings.columns - 1))
    column_width = usable_width // settings.columns
    y0 = settings.margin + header_h

    for column_index, lines in enumerate(page.column_lines):
        x = settings.margin + column_index * (column_width + gutter)
        if column_index > 0:
            guide_x = x - gutter // 2
            draw.line((guide_x, y0, guide_x, settings.height - settings.margin - footer_h), fill=guide, width=2)
        y = y0
        for line in lines:
            draw.text((x, y), line, fill=ink, font=font)
            y += line_height
    return image


def pdf_bytes(images: Iterable[Image.Image]) -> bytes:
    image_list = [image.convert("RGB") for image in images]
    buffer = io.BytesIO()
    first, rest = image_list[0], image_list[1:]
    first.save(buffer, format="PDF", save_all=True, append_images=rest, resolution=200)
    return buffer.getvalue()


def safe_stem(name: str) -> str:
    stem = Path(name).stem or "transcription_pages"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "transcription_pages"


class TranscriptionViewer(tk.Tk):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.title("Transcription Viewer")
        self.geometry("1400x850")
        self.minsize(900, 500)

        self.source_name = "pasted-text.txt"
        self.raw_text = ""
        self.sections = [Section("empty", "")]
        self.pages: list[Page] = []
        self.current_page = 0
        self.current_image: Image.Image | None = None
        self.tk_image: ImageTk.PhotoImage | None = None
        self.resize_job: str | None = None

        self.page_size_var = tk.StringVar(value="Window")
        self.section_var = tk.StringVar(value="empty")
        self.page_var = tk.StringVar(value="1")
        self.columns_var = tk.IntVar(value=2)
        self.font_size_var = tk.IntVar(value=30)
        self.spacing_var = tk.IntVar(value=6)
        self.margin_var = tk.IntVar(value=64)
        self.line_numbers_var = tk.BooleanVar(value=True)
        self.dark_var = tk.BooleanVar(value=False)
        self.pretty_yaml_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Open a file or paste text.")

        self._build_ui()
        self.bind("<Left>", lambda _event: self.prev_page())
        self.bind("<Right>", lambda _event: self.next_page())
        self.bind("<Prior>", lambda _event: self.prev_page())
        self.bind("<Next>", lambda _event: self.next_page())
        self.bind("<F11>", lambda _event: self.toggle_fullscreen())
        self.bind("<Escape>", lambda _event: self.attributes("-fullscreen", False))

        if initial_path:
            self.open_path(initial_path)
        else:
            self._set_text("pasted-text.txt", "")

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(6, 4))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="Open", command=self.open_file).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="Paste", command=self.paste_text).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(toolbar, text="PDF", command=self.save_pdf).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="PNG", command=self.save_png).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(toolbar, text="<", width=3, command=self.prev_page).pack(side=tk.LEFT)
        page_entry = ttk.Entry(toolbar, width=5, textvariable=self.page_var)
        page_entry.pack(side=tk.LEFT, padx=3)
        page_entry.bind("<Return>", lambda _event: self.goto_page())
        ttk.Button(toolbar, text=">", width=3, command=self.next_page).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(toolbar, text="Section").pack(side=tk.LEFT)
        self.section_combo = ttk.Combobox(toolbar, width=30, textvariable=self.section_var, state="readonly")
        self.section_combo.pack(side=tk.LEFT, padx=(3, 10))
        self.section_combo.bind("<<ComboboxSelected>>", lambda _event: self.rebuild(reset_page=True))

        ttk.Label(toolbar, text="Size").pack(side=tk.LEFT)
        size_combo = ttk.Combobox(toolbar, width=13, textvariable=self.page_size_var, values=list(PAGE_PRESETS), state="readonly")
        size_combo.pack(side=tk.LEFT, padx=(3, 8))
        size_combo.bind("<<ComboboxSelected>>", lambda _event: self.rebuild(reset_page=False))

        self._spin(toolbar, "Cols", self.columns_var, 1, 3)
        self._spin(toolbar, "Font", self.font_size_var, 16, 60)
        self._spin(toolbar, "Gap", self.spacing_var, 0, 20)
        self._spin(toolbar, "Margin", self.margin_var, 20, 180)
        ttk.Checkbutton(toolbar, text="#", variable=self.line_numbers_var, command=self.rebuild).pack(side=tk.LEFT)
        ttk.Checkbutton(toolbar, text="Dark", variable=self.dark_var, command=self.rebuild).pack(side=tk.LEFT)
        ttk.Checkbutton(toolbar, text="YAML", variable=self.pretty_yaml_var, command=lambda: self._set_text(self.source_name, self.raw_text)).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Full", command=self.toggle_fullscreen).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, background="#111820", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        status = ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=(6, 2))
        status.pack(fill=tk.X)

    def _spin(self, parent: tk.Misc, label: str, variable: tk.IntVar, start: int, end: int) -> None:
        ttk.Label(parent, text=label).pack(side=tk.LEFT)
        spin = ttk.Spinbox(parent, from_=start, to=end, width=4, textvariable=variable, command=self.rebuild)
        spin.pack(side=tk.LEFT, padx=(2, 8))
        spin.bind("<Return>", lambda _event: self.rebuild())

    def _on_canvas_resize(self, _event: tk.Event) -> None:
        if self.page_size_var.get() != "Window":
            self.show_page()
            return
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(160, lambda: self.rebuild(reset_page=False))

    def current_settings(self) -> RenderSettings:
        preset = PAGE_PRESETS.get(self.page_size_var.get())
        if preset is None:
            width = max(640, self.canvas.winfo_width())
            height = max(360, self.canvas.winfo_height())
        else:
            width, height = preset
        return RenderSettings(
            width=width,
            height=height,
            columns=max(1, min(3, self.columns_var.get())),
            font_size=max(8, self.font_size_var.get()),
            line_spacing=max(0, self.spacing_var.get()),
            margin=max(8, self.margin_var.get()),
            line_numbers=self.line_numbers_var.get(),
            dark_mode=self.dark_var.get(),
        )

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open text, Markdown, or YAML",
            filetypes=[
                ("Text files", "*.txt *.md *.markdown *.yaml *.yml *.py *.sql *.json *.csv"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.open_path(Path(path))

    def open_path(self, path: Path) -> None:
        try:
            self._set_text(path.name, read_text_file(path))
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def paste_text(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Clipboard empty", "Copy text first, then click Paste.")
            return
        self._set_text("pasted-text.txt", text)

    def _set_text(self, name: str, text: str) -> None:
        self.source_name = name
        self.raw_text = text
        display_text = normalize_yaml_if_requested(name, text, self.pretty_yaml_var.get())
        self.sections = make_sections(name, display_text) if display_text.strip() else [Section("empty", "")]
        labels = [section.label for section in self.sections]
        self.section_combo.configure(values=labels)
        self.section_var.set(labels[0])
        self.rebuild(reset_page=True)

    def selected_section(self) -> Section:
        label = self.section_var.get()
        for section in self.sections:
            if section.label == label:
                return section
        return self.sections[0]

    def rebuild(self, reset_page: bool = False) -> None:
        if reset_page:
            self.current_page = 0
        settings = self.current_settings()
        self.pages = paginate(self.selected_section(), settings)
        self.current_page = max(0, min(self.current_page, len(self.pages) - 1))
        self.show_page()

    def show_page(self) -> None:
        if not self.pages:
            return
        settings = self.current_settings()
        page = self.pages[self.current_page]
        self.current_image = render_page(page, settings, self.source_name)
        display_image = self.current_image
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        if display_image.size != (canvas_w, canvas_h):
            scale = min(canvas_w / display_image.width, canvas_h / display_image.height)
            scaled = (max(1, int(display_image.width * scale)), max(1, int(display_image.height * scale)))
            display_image = display_image.resize(scaled, Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(display_image)
        self.canvas.delete("all")
        self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.tk_image, anchor=tk.CENTER)
        self.page_var.set(str(page.number))
        self.status_var.set(
            f"{self.source_name} | {page.section_label} | page {page.number} of {page.total} | "
            f"{settings.width} x {settings.height}px"
        )

    def goto_page(self) -> None:
        try:
            wanted = int(self.page_var.get())
        except ValueError:
            wanted = self.current_page + 1
        self.current_page = max(0, min(wanted - 1, len(self.pages) - 1))
        self.show_page()

    def prev_page(self) -> None:
        if self.current_page > 0:
            self.current_page -= 1
            self.show_page()

    def next_page(self) -> None:
        if self.current_page + 1 < len(self.pages):
            self.current_page += 1
            self.show_page()

    def all_images(self) -> list[Image.Image]:
        settings = self.current_settings()
        return [render_page(page, settings, self.source_name) for page in self.pages]

    def save_pdf(self) -> None:
        if not self.pages:
            return
        path = filedialog.asksaveasfilename(
            title="Save PDF",
            defaultextension=".pdf",
            initialfile=f"{safe_stem(self.source_name)}_transcription_pages.pdf",
            filetypes=[("PDF", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_bytes(pdf_bytes(self.all_images()))
            self.status_var.set(f"Saved {path}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def save_png(self) -> None:
        if self.current_image is None:
            return
        page = self.pages[self.current_page]
        path = filedialog.asksaveasfilename(
            title="Save current PNG",
            defaultextension=".png",
            initialfile=f"{safe_stem(self.source_name)}_P{page.number:03d}.png",
            filetypes=[("PNG", "*.png"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.current_image.save(path, format="PNG", optimize=True)
            self.status_var.set(f"Saved {path}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def toggle_fullscreen(self) -> None:
        self.attributes("-fullscreen", not bool(self.attributes("-fullscreen")))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    initial_path = Path(args[0]) if args else None
    app = TranscriptionViewer(initial_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
