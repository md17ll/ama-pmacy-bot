from __future__ import annotations

import re
import zipfile
from datetime import time
from io import BytesIO

from docx import Document

from app.utils import ParsedShift, normalize_digits, parse_date_value


MAX_DOCX_BYTES = 10 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_DOCX_ENTRIES = 500
MAX_DOCX_ROWS = 1000
DEFAULT_DAY_START = time(13, 30)
DEFAULT_DAY_END = time(17, 0)
DEFAULT_EVENING_START = time(20, 30)
DEFAULT_EVENING_END = time(23, 30)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_digits(value or "")).strip()


def _validate_docx_archive(data: bytes) -> None:
    if len(data) > MAX_DOCX_BYTES:
        raise ValueError("حجم ملف Word أكبر من الحد المسموح")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise ValueError("ملف Word يحتوي عدداً غير طبيعي من الملفات الداخلية")
            total = sum(item.file_size for item in entries)
            if total > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("ملف Word كبير جداً بعد فك الضغط")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("الملف ليس مستند Word صالحاً")
            for item in entries:
                if item.file_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ValueError("ملف Word يحتوي عنصراً داخلياً كبيراً جداً")
                if item.compress_size and item.file_size / item.compress_size > 250:
                    raise ValueError("ملف Word مضغوط بنسبة غير آمنة")
    except zipfile.BadZipFile as exc:
        raise ValueError("الملف ليس مستند Word صالحاً") from exc


def _clock(value: str, *, evening: bool) -> time:
    raw = _clean(value).replace("٫", ":").replace(".", ":")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", raw)
    if not match:
        raise ValueError(f"تعذر قراءة الوقت: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if minute > 59 or hour > 23:
        raise ValueError(f"وقت غير صالح: {value}")
    if evening and hour < 12:
        hour += 12
    elif not evening and hour <= 7:
        hour += 12
    return time(hour, minute)


def _range_from_header(
    text: str,
    *,
    evening: bool,
    fallback: tuple[time, time],
) -> tuple[time, time]:
    raw = _clean(text).replace("٫", ".")
    match = re.search(
        r"(\d{1,2}(?:[.:]\d{1,2})?)\s*[-–—]\s*(\d{1,2}(?:[.:]\d{1,2})?)",
        raw,
    )
    if not match:
        return fallback
    try:
        return (
            _clock(match.group(1), evening=evening),
            _clock(match.group(2), evening=evening),
        )
    except ValueError:
        return fallback


def _date_from_cell(value: str):
    raw = _clean(value)
    match = re.search(r"\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}", raw)
    if not match:
        return None
    return parse_date_value(re.sub(r"\s+", "", match.group(0)))


def parse_amuda_word_schedule(data: bytes) -> tuple[list[ParsedShift], list[str]]:
    """Parse the official two-period Amuda pharmacy schedule Word layout.

    Every group of three columns is interpreted as:
    date | daytime pharmacy | evening pharmacy.
    """
    _validate_docx_archive(data)
    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise ValueError("تعذر فتح ملف Word") from exc

    parsed: list[ParsedShift] = []
    warnings: list[str] = []
    sequence = 1

    for table_index, table in enumerate(document.tables, start=1):
        if not table.rows or len(table.columns) < 3:
            continue
        if len(table.rows) > MAX_DOCX_ROWS:
            raise ValueError("عدد صفوف جدول Word أكبر من الحد المسموح")

        header = [_clean(cell.text) for cell in table.rows[0].cells]
        group_count = len(header) // 3
        if group_count < 1:
            continue

        periods: list[tuple[time, time, time, time]] = []
        for group in range(group_count):
            base = group * 3
            day_range = _range_from_header(
                header[base + 1],
                evening=False,
                fallback=(DEFAULT_DAY_START, DEFAULT_DAY_END),
            )
            evening_range = _range_from_header(
                header[base + 2],
                evening=True,
                fallback=(DEFAULT_EVENING_START, DEFAULT_EVENING_END),
            )
            periods.append((*day_range, *evening_range))

        for table_row, row in enumerate(table.rows[1:], start=2):
            cells = [_clean(cell.text) for cell in row.cells]
            for group, period in enumerate(periods):
                base = group * 3
                if base + 2 >= len(cells):
                    continue
                duty_date = _date_from_cell(cells[base])
                if duty_date is None:
                    continue

                day_name = cells[base + 1]
                evening_name = cells[base + 2]
                day_start, day_end, evening_start, evening_end = period

                if day_name:
                    parsed.append(
                        ParsedShift(
                            pharmacy_name=day_name,
                            duty_date=duty_date,
                            start_time=day_start,
                            end_time=day_end,
                            row_number=sequence,
                            raw_data={
                                "source": "word",
                                "period": "نهارية",
                                "table": table_index,
                                "table_row": table_row,
                                "table_group": group + 1,
                                "date_text": cells[base],
                            },
                        )
                    )
                    sequence += 1
                else:
                    warnings.append(f"{duty_date}: اسم الصيدلية النهارية فارغ")

                if evening_name:
                    parsed.append(
                        ParsedShift(
                            pharmacy_name=evening_name,
                            duty_date=duty_date,
                            start_time=evening_start,
                            end_time=evening_end,
                            row_number=sequence,
                            raw_data={
                                "source": "word",
                                "period": "مسائية",
                                "table": table_index,
                                "table_row": table_row,
                                "table_group": group + 1,
                                "date_text": cells[base],
                            },
                        )
                    )
                    sequence += 1
                else:
                    warnings.append(f"{duty_date}: اسم الصيدلية المسائية فارغ")

    if not parsed:
        raise ValueError(
            "لم أجد جدولاً مطابقاً للنظام: التاريخ | النهارية | المساء"
        )

    dates = {item.duty_date for item in parsed}
    if len(dates) * 2 != len(parsed):
        warnings.append("بعض التواريخ لا تحتوي مناوبتين كاملتين")
    return parsed, warnings
