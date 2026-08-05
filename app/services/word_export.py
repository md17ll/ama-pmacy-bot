from __future__ import annotations

from collections import defaultdict
from datetime import date, time
from io import BytesIO
from math import ceil
from typing import Iterable
from zoneinfo import ZoneInfo

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from app.models import Shift
from app.services.shift_schedule_tools import ShiftTimes
from app.utils import as_local


MAX_DATES_PER_PAGE = 38
DATE_BLUE = RGBColor(0x2F, 0x54, 0x96)
TITLE_RED = RGBColor(0xFF, 0x00, 0x00)
LEVANT_MONTHS = {
    1: "كانون الثاني",
    2: "شباط",
    3: "آذار",
    4: "نيسان",
    5: "أيار",
    6: "حزيران",
    7: "تموز",
    8: "آب",
    9: "أيلول",
    10: "تشرين الأول",
    11: "تشرين الثاني",
    12: "كانون الأول",
}
ARABIC_DAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الاحد",
}
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


class WordExportError(ValueError):
    pass


def _rtl_paragraph(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_pr = paragraph._p.get_or_add_pPr()
    bidi = p_pr.find(qn("w:bidi"))
    if bidi is None:
        bidi = OxmlElement("w:bidi")
        p_pr.append(bidi)


def _set_run_rtl(run) -> None:
    r_pr = run._r.get_or_add_rPr()
    rtl = r_pr.find(qn("w:rtl"))
    if rtl is None:
        rtl = OxmlElement("w:rtl")
        r_pr.append(rtl)
    fonts = r_pr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, fonts)
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn(f"w:{attr}"), "Arial")
    fonts.set(qn("w:hint"), "cs")
    if run.bold:
        bold_cs = r_pr.find(qn("w:bCs"))
        if bold_cs is None:
            r_pr.append(OxmlElement("w:bCs"))
    if run.font.size is not None:
        size_cs = r_pr.find(qn("w:szCs"))
        if size_cs is None:
            size_cs = OxmlElement("w:szCs")
            r_pr.append(size_cs)
        size_cs.set(qn("w:val"), str(int(run.font.size.pt * 2)))


def _set_cell_margins(cell, *, top: int = 20, start: int = 30, bottom: int = 20, end: int = 30) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _clear_cell(cell) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    _rtl_paragraph(paragraph)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _set_cell_margins(cell)


def _write_cell(
    cell,
    text: str,
    *,
    size: float,
    color: RGBColor | None = None,
    bold: bool = True,
) -> None:
    _clear_cell(cell)
    paragraph = cell.paragraphs[0]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        run = paragraph.add_run(line)
        if index < len(lines) - 1:
            run.add_break()
        run.bold = bold
        run.font.size = Pt(size)
        run.font.name = "Arial"
        if color is not None:
            run.font.color.rgb = color
        _set_run_rtl(run)


def _time_12h(
    value: time,
    *,
    arabic_digits: bool,
    dot: bool = False,
    always_minutes: bool = False,
) -> str:
    hour = value.hour % 12 or 12
    separator = "." if dot else ":"
    if value.minute or always_minutes:
        rendered = f"{hour}{separator}{value.minute:02d}"
    else:
        rendered = str(hour)
    return rendered.translate(ARABIC_DIGITS) if arabic_digits else rendered


def _header_range(start: time, end: time) -> str:
    return f"{_time_12h(start, arabic_digits=True, dot=True)}-{_time_12h(end, arabic_digits=True, dot=True)}"


def _date_label(value: date) -> str:
    return f"{ARABIC_DAYS[value.weekday()]}: {value.day}/{value.month}/{value.year}"


def _title_for(first_date: date) -> str:
    month_name = LEVANT_MONTHS[first_date.month]
    return (
        "جدول المنوبات لمدينة عامودة خلال "
        f"شهر//{first_date.month}// {month_name} لعام {first_date.year}"
    )


def _group_shifts(shifts: Iterable[Shift], timezone: ZoneInfo) -> dict[date, dict[str, list[str]]]:
    grouped: dict[date, dict[str, list[str]]] = defaultdict(lambda: {"day": [], "evening": []})
    for shift in sorted(shifts, key=lambda item: (item.start_at, item.pharmacy.name)):
        if not getattr(shift, "active", True):
            continue
        local_start = as_local(shift.start_at, timezone)
        period = "day" if local_start.time().replace(tzinfo=None) < time(18, 0) else "evening"
        name = shift.pharmacy.name.strip()
        if name and name not in grouped[local_start.date()][period]:
            grouped[local_start.date()][period].append(name)
    return dict(grouped)


def _joined_names(values: list[str]) -> str:
    return " / ".join(values)


def _configure_document(document: Document) -> None:
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(12)


def _add_title(document: Document, first_date: date) -> None:
    paragraph = document.add_paragraph()
    _rtl_paragraph(paragraph)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(5)
    run = paragraph.add_run(_title_for(first_date))
    run.bold = True
    run.font.size = Pt(18)
    run.font.color.rgb = TITLE_RED
    run.font.name = "Arial"
    _set_run_rtl(run)


def _add_schedule_table(
    document: Document,
    page_dates: list[date],
    grouped: dict[date, dict[str, list[str]]],
    times: ShiftTimes,
) -> None:
    rows_per_side = max(1, ceil(len(page_dates) / 2))
    table = document.add_table(rows=rows_per_side + 1, cols=6)
    table.style = "Table Grid"
    table.autofit = True
    tbl_pr = table._tbl.tblPr
    if tbl_pr.find(qn("w:bidiVisual")) is None:
        tbl_pr.append(OxmlElement("w:bidiVisual"))
    widths = (1.574, 1.083, 1.286, 1.371, 1.051, 1.208)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = Inches(widths[index])

    header = table.rows[0]
    header.height = Inches(0.47)
    header.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    day_header = f"النهارية:\n{_header_range(times.day_start, times.day_end)}"
    evening_header = f"المساء:\n{_header_range(times.evening_start, times.evening_end)}"
    for base in (0, 3):
        _write_cell(header.cells[base], "اليوم\nوالتاريخ", size=16)
        _write_cell(header.cells[base + 1], day_header, size=16)
        _write_cell(header.cells[base + 2], evening_header, size=16)

    right_dates = page_dates[:rows_per_side]
    left_dates = page_dates[rows_per_side:]
    for row_index in range(rows_per_side):
        row = table.rows[row_index + 1]
        row.height = Inches(0.37)
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        pairs = (
            (0, right_dates[row_index] if row_index < len(right_dates) else None),
            (3, left_dates[row_index] if row_index < len(left_dates) else None),
        )
        for base, duty_date in pairs:
            if duty_date is None:
                for offset in range(3):
                    _write_cell(row.cells[base + offset], "", size=12)
                continue
            values = grouped[duty_date]
            _write_cell(row.cells[base], _date_label(duty_date), size=12, color=DATE_BLUE)
            _write_cell(row.cells[base + 1], _joined_names(values["day"]), size=14)
            _write_cell(row.cells[base + 2], _joined_names(values["evening"]), size=14)


def _add_footer(document: Document, times: ShiftTimes) -> None:
    paragraph = document.add_paragraph()
    _rtl_paragraph(paragraph)
    paragraph.alignment = None
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(0)
    text = (
        "الدوام المسائي:"
        f"{_time_12h(times.day_end, arabic_digits=False, always_minutes=True)} – "
        f"{_time_12h(times.evening_start, arabic_digits=False, always_minutes=True)}"
    )
    run = paragraph.add_run(text)
    run.bold = True
    run.font.size = Pt(24)
    run.font.color.rgb = TITLE_RED
    run.font.name = "Arial"
    _set_run_rtl(run)


def build_official_word_schedule(
    shifts: Iterable[Shift],
    timezone: ZoneInfo,
    times: ShiftTimes,
) -> bytes:
    grouped = _group_shifts(shifts, timezone)
    dates = sorted(grouped)
    if not dates:
        raise WordExportError("لا توجد مناوبات قابلة للتصدير إلى Word.")

    document = Document()
    _configure_document(document)
    for page_index, start in enumerate(range(0, len(dates), MAX_DATES_PER_PAGE)):
        page_dates = dates[start : start + MAX_DATES_PER_PAGE]
        if page_index:
            document.add_page_break()
        _add_title(document, page_dates[0])
        _add_schedule_table(document, page_dates, grouped, times)
        _add_footer(document, times)

    output = BytesIO()
    document.save(output)
    return output.getvalue()
