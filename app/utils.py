from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser


ARABIC_DAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}

ARABIC_MONTHS = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}

MONTH_ALIASES = {
    "يناير": 1,
    "كانون الثاني": 1,
    "فبراير": 2,
    "شباط": 2,
    "مارس": 3,
    "آذار": 3,
    "اذار": 3,
    "أبريل": 4,
    "ابريل": 4,
    "نيسان": 4,
    "مايو": 5,
    "أيار": 5,
    "ايار": 5,
    "يونيو": 6,
    "حزيران": 6,
    "يوليو": 7,
    "تموز": 7,
    "أغسطس": 8,
    "اغسطس": 8,
    "آب": 8,
    "اب": 8,
    "سبتمبر": 9,
    "أيلول": 9,
    "ايلول": 9,
    "أكتوبر": 10,
    "اكتوبر": 10,
    "تشرين الأول": 10,
    "تشرين الاول": 10,
    "نوفمبر": 11,
    "تشرين الثاني": 11,
    "ديسمبر": 12,
    "كانون الأول": 12,
    "كانون الاول": 12,
}

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_digits(value: str) -> str:
    return value.translate(ARABIC_DIGITS)


def normalize_text(value: str) -> str:
    value = normalize_digits(value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    replacements = {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
        "ـ": "",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = re.sub(r"\bصيدليه\b", "", value)
    value = re.sub(r"\bصيدلية\b", "", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_date_value(value: str | date | datetime, *, default_year: int | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = normalize_digits(str(value)).strip()
    if not raw:
        raise ValueError("التاريخ فارغ")

    for alias, month in sorted(MONTH_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in raw:
            numbers = [int(item) for item in re.findall(r"\d{1,4}", raw)]
            if not numbers:
                raise ValueError(f"تعذر قراءة اليوم من التاريخ: {value}")
            day = numbers[0]
            year = next((num for num in numbers[1:] if num >= 1000), default_year)
            if year is None:
                year = datetime.now().year
            return date(year, month, day)

    formats = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d-%m-%y",
        "%d/%m/%y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    try:
        parsed = date_parser.parse(raw, dayfirst=True, fuzzy=True, default=datetime(default_year or datetime.now().year, 1, 1))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"تعذر قراءة التاريخ: {value}") from exc
    return parsed.date()


def parse_time_value(value: str | time | datetime) -> time:
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)

    raw = normalize_digits(str(value)).strip().lower()
    if not raw:
        raise ValueError("الوقت فارغ")

    raw = raw.replace("صباحًا", "am").replace("صباحاً", "am").replace("صباحا", "am")
    raw = raw.replace("صباح", "am").replace("ص", "am")
    raw = raw.replace("مساءً", "pm").replace("مساءً", "pm").replace("مساءا", "pm")
    raw = raw.replace("مساء", "pm").replace("م", "pm")
    raw = raw.replace("منتصف الليل", "12:00 am").replace("الظهر", "12:00 pm").replace("ظهراً", "12:00 pm")
    raw = raw.replace("٫", ":").replace(".", ":")
    raw = re.sub(r"\s+", " ", raw).strip()

    formats = ("%I:%M %p", "%I %p", "%H:%M", "%H")
    for fmt in formats:
        try:
            return datetime.strptime(raw.upper(), fmt).time()
        except ValueError:
            continue

    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)?", raw, flags=re.I)
    if not match:
        raise ValueError(f"تعذر قراءة الوقت: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = (match.group(3) or "").lower()
    if minute > 59:
        raise ValueError(f"دقائق غير صالحة: {value}")
    if marker:
        if not 1 <= hour <= 12:
            raise ValueError(f"ساعة غير صالحة بنظام 12 ساعة: {value}")
        if marker == "am" and hour == 12:
            hour = 0
        elif marker == "pm" and hour != 12:
            hour += 12
    elif hour > 23:
        raise ValueError(f"ساعة غير صالحة: {value}")
    return time(hour, minute)


def combine_shift(
    duty_date: date,
    start_time: time,
    end_time: time,
    tz: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(duty_date, start_time, tzinfo=tz)
    end_local = datetime.combine(duty_date, end_time, tzinfo=tz)
    if end_local <= start_local:
        end_local += timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def as_local(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def format_date_ar(value: date | datetime, tz: ZoneInfo | None = None) -> str:
    if isinstance(value, datetime):
        value = as_local(value, tz) if tz else value
        day_value = value.date()
    else:
        day_value = value
    return f"{ARABIC_DAYS[day_value.weekday()]}، {day_value.day} {ARABIC_MONTHS[day_value.month]} {day_value.year}"


def format_time_ar(value: time | datetime, tz: ZoneInfo | None = None) -> str:
    if isinstance(value, datetime):
        if tz:
            value = as_local(value, tz)
        hour = value.hour
        minute = value.minute
    else:
        hour = value.hour
        minute = value.minute

    display_hour = hour % 12 or 12
    minute_part = f":{minute:02d}"
    if hour == 0 and minute == 0:
        marker = "منتصف الليل"
    elif hour == 12 and minute == 0:
        marker = "ظهراً"
    elif hour < 12:
        marker = "صباحاً"
    else:
        marker = "مساءً"
    return f"{display_hour}{minute_part} {marker}"


def format_datetime_ar(value: datetime, tz: ZoneInfo) -> str:
    local = as_local(value, tz)
    return f"{format_date_ar(local)}، {format_time_ar(local)}"


def format_duration(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} يوم")
    if hours:
        parts.append(f"{hours} ساعة")
    if minutes or not parts:
        parts.append(f"{minutes} دقيقة")
    return " و".join(parts)


def html(value: object) -> str:
    return escape(str(value), quote=True)


@dataclass(slots=True, frozen=True)
class ParsedShift:
    pharmacy_name: str
    duty_date: date
    start_time: time
    end_time: time
    row_number: int
    raw_data: dict[str, object]
