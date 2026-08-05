from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "DejaVuSansMono.ttf"


@dataclass
class PdfResult:
    path: Path
    warning: str | None = None


@dataclass
class PrintResult:
    ok: bool
    request_id: str | None = None
    error: str | None = None


class _TabPDF(FPDF):
    def __init__(self, footer_text: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.footer_text = footer_text

    def footer(self):
        self.set_y(-9)
        self.set_font("DejaVuMono", size=6)
        self.cell(0, 4, f"{self.footer_text} · стр. {self.page_no()}", align="C")


def _systems(text: str) -> list[list[str]]:
    normalized = text.replace("\r\n", "\n").strip("\n")
    if not normalized:
        return [[]]
    return [block.splitlines() for block in re.split(r"\n\s*\n", normalized)]


def ascii_to_pdf(
    source_path: str | Path,
    title: str,
    *,
    preset: str,
    version: int,
    output_path: str | Path | None = None,
) -> PdfResult:
    source_path = Path(source_path)
    output_path = Path(output_path) if output_path else source_path.with_suffix(".pdf")
    text = source_path.read_text(encoding="utf-8", errors="replace")
    footer = f"{title} · {preset} · v{version} · {datetime.now().date().isoformat()}"
    pdf = _TabPDF(footer)
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=13)
    pdf.add_font("DejaVuMono", style="", fname=str(FONT_PATH))
    pdf.add_page()

    usable_width = pdf.w - pdf.l_margin - pdf.r_margin
    longest = max(text.splitlines() or [""], key=len)
    font_size = 8.5
    while font_size > 6:
        pdf.set_font("DejaVuMono", size=font_size)
        if pdf.get_string_width(longest) <= usable_width:
            break
        font_size = max(6, font_size - 0.25)
    pdf.set_font("DejaVuMono", size=font_size)
    clipped = pdf.get_string_width(longest) > usable_width
    line_height = max(2.5, font_size * 0.3528 * 1.18)

    for system_index, lines in enumerate(_systems(text)):
        required = max(1, len(lines)) * line_height + (line_height if system_index else 0)
        if pdf.get_y() + required > pdf.h - 13 and pdf.get_y() > pdf.t_margin:
            # At the very top of a fresh page there is nothing to push down:
            # adding another page would leave a blank one and auto-page-break
            # splits the oversized system anyway.
            pdf.add_page()
        elif system_index:
            pdf.ln(line_height)
        for line in lines:
            # No multi_cell: wrapping a tab line destroys alignment. Cell output is
            # intentionally clipped by the page when 6pt still does not fit.
            pdf.cell(usable_width, line_height, text=line, new_x="LMARGIN", new_y="NEXT")

    if clipped:
        pdf.set_y(-14)
        pdf.set_font("DejaVuMono", size=6)
        pdf.cell(0, 3, "ВНИМАНИЕ: строки шире A4 обрезаны справа", align="C")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return PdfResult(output_path, "строки шире A4 обрезаны справа" if clipped else None)


def print_pdf(
    pdf_path: str | Path,
    *,
    server: str,
    printer: str,
    timeout: int = 15,
) -> PrintResult:
    pdf_path = Path(pdf_path)
    if not printer:
        return PrintResult(False, error="CUPS_PRINTER не настроен")
    command = [
        "lp", "-h", server, "-d", printer,
        # Absolute path: a relative name beginning with "-" would be parsed by lp
        # as an option rather than the file to print.
        "-o", "media=A4", "-o", "sides=one-sided", str(pdf_path.resolve()),
    ]
    try:
        completed = subprocess.run(command, timeout=timeout, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PrintResult(False, error=str(exc))
    if completed.returncode:
        return PrintResult(False, error=(completed.stderr or completed.stdout or f"lp exit {completed.returncode}").strip())
    match = re.search(r"request id is\s+([^\s]+)", completed.stdout or "", re.IGNORECASE)
    return PrintResult(True, request_id=match.group(1) if match else None)
