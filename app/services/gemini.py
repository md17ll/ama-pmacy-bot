from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.utils import ParsedShift, parse_date_value, parse_time_value


class GeminiShiftRow(BaseModel):
    pharmacy_name: str = Field(description="اسم الصيدلية كما يظهر في الصورة")
    duty_date: str = Field(description="التاريخ بصيغة YYYY-MM-DD عند الإمكان")
    start_time: str = Field(description="وقت بداية المناوبة مع AM أو PM")
    end_time: str = Field(description="وقت نهاية المناوبة مع AM أو PM")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str | None = None


class GeminiSchedule(BaseModel):
    rows: list[GeminiShiftRow]
    document_language: str | None = None
    warnings: list[str] = Field(default_factory=list)


class GeminiScheduleReader:
    def __init__(self, api_key: str | None, model: str):
        self.api_key = api_key
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def read_image(self, image_bytes: bytes, mime_type: str) -> tuple[list[ParsedShift], list[str]]:
        if not self.api_key:
            raise RuntimeError("مفتاح Gemini غير مضبوط في Railway")
        if len(image_bytes) > 15 * 1024 * 1024:
            raise ValueError("حجم الصورة أكبر من الحد المسموح")
        schedule = await asyncio.to_thread(self._read_image_sync, image_bytes, mime_type)
        parsed: list[ParsedShift] = []
        warnings = list(schedule.warnings)
        default_year = datetime.now().year
        for index, row in enumerate(schedule.rows, start=1):
            try:
                parsed.append(
                    ParsedShift(
                        pharmacy_name=row.pharmacy_name.strip(),
                        duty_date=parse_date_value(row.duty_date, default_year=default_year),
                        start_time=parse_time_value(row.start_time),
                        end_time=parse_time_value(row.end_time),
                        row_number=index,
                        raw_data={
                            "pharmacy": row.pharmacy_name,
                            "date": row.duty_date,
                            "start": row.start_time,
                            "end": row.end_time,
                            "confidence": row.confidence,
                            "note": row.note,
                        },
                    )
                )
            except ValueError as exc:
                warnings.append(f"السطر {index}: {exc}")
        if not parsed:
            raise ValueError("لم يستطع Gemini استخراج أي مناوبة صالحة من الصورة")
        return parsed, warnings

    def _read_image_sync(self, image_bytes: bytes, mime_type: str) -> GeminiSchedule:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        prompt = """
أنت تقرأ جدول مناوبات صيدليات في مدينة عامودا.
استخرج فقط: اسم الصيدلية، تاريخ المناوبة، وقت البداية، وقت النهاية.
لا تستخرج رقم هاتف ولا موقع خرائط ولا عنوان.
حوّل التاريخ إلى YYYY-MM-DD عند الإمكان.
حوّل الوقت إلى نظام 12 ساعة واكتب AM أو PM بوضوح.
إذا كانت النهاية صباح اليوم التالي، اترك التاريخ نفسه واكتب وقت النهاية الصحيح؛ النظام سيضيف اليوم التالي.
لا تخمن أي قيمة غير واضحة. أضف تحذيراً بدلاً من التخمين.
أعد النتيجة وفق المخطط المطلوب فقط.
""".strip()
        response = client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeminiSchedule,
                temperature=0.0,
            ),
        )
        if getattr(response, "parsed", None) is not None:
            return GeminiSchedule.model_validate(response.parsed)
        if not response.text:
            raise ValueError("Gemini لم يُرجع بيانات")
        return GeminiSchedule.model_validate_json(response.text)
