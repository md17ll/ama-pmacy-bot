from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from app.utils import ParsedShift, parse_date_value, parse_time_value


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_GEMINI_ROWS = 500  # Kept for compatibility with existing imports.
OPENROUTER_TIMEOUT_SECONDS = 90
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


class GeminiShiftRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pharmacy_name: str = Field(
        min_length=1,
        max_length=255,
        description="اسم الصيدلية كما يظهر في الصورة",
    )
    duty_date: str = Field(max_length=64, description="التاريخ بصيغة YYYY-MM-DD عند الإمكان")
    start_time: str = Field(max_length=64, description="وقت بداية المناوبة مع AM أو PM")
    end_time: str = Field(max_length=64, description="وقت نهاية المناوبة مع AM أو PM")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str | None = Field(default=None, max_length=500)


class GeminiSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[GeminiShiftRow] = Field(max_length=MAX_GEMINI_ROWS)
    document_language: str | None = Field(default=None, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class OpenRouterScheduleReader:
    """Read pharmacy-duty images through OpenRouter using a vision model."""

    def __init__(self, api_key: str | None, model: str):
        self.api_key = api_key
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def read_image(self, image_bytes: bytes, mime_type: str) -> tuple[list[ParsedShift], list[str]]:
        if not self.api_key:
            raise RuntimeError("مفتاح OpenRouter غير مضبوط في Railway")
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("حجم الصورة أكبر من الحد المسموح")
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("صيغة الصورة غير مدعومة")

        schedule = await self._request_schedule(image_bytes, mime_type)
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
                            "model": self.model,
                            "provider": "openrouter",
                        },
                    )
                )
            except ValueError as exc:
                warnings.append(f"السطر {index}: {exc}")

        if not parsed:
            raise ValueError("لم يستطع GPT استخراج أي مناوبة صالحة من الصورة")
        return parsed, warnings

    def _request_payload(self, image_bytes: bytes, mime_type: str) -> dict[str, Any]:
        encoded_image = base64.b64encode(image_bytes).decode("ascii")
        prompt = """
أنت تقرأ صورة جدول مناوبات صيدليات في مدينة عامودا في سوريا.
استخرج كل صف مناوبة ظاهر في الصورة بدقة، ولا تهمل أي صيدلية.
استخرج فقط: اسم الصيدلية، تاريخ المناوبة، وقت البداية، وقت النهاية، ودرجة الثقة.
لا تستخرج أرقام الهواتف أو روابط الخرائط أو العناوين.
حافظ على اسم الصيدلية كما يظهر، مع تصحيح أخطاء OCR الواضحة فقط.
حوّل التاريخ إلى YYYY-MM-DD عند الإمكان.
حوّل الوقت إلى نظام 12 ساعة واكتب AM أو PM بوضوح.
إذا كانت نهاية المناوبة صباح اليوم التالي، اكتب وقت النهاية الصحيح ولا تغيّر تاريخ البداية؛ النظام سيضيف اليوم التالي.
لا تخمن قيمة غير واضحة. ضع ملاحظة وتحذيراً ودرجة ثقة منخفضة بدلاً من التخمين.
أعد النتيجة حصراً وفق JSON Schema المطلوب.
""".strip()
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{encoded_image}",
                            },
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "amuda_pharmacy_schedule",
                    "strict": True,
                    "schema": GeminiSchedule.model_json_schema(),
                },
            },
            "max_tokens": 16000,
        }

    async def _request_schedule(self, image_bytes: bytes, mime_type: str) -> GeminiSchedule:
        payload = self._request_payload(image_bytes, mime_type)
        timeout = aiohttp.ClientTimeout(total=OPENROUTER_TIMEOUT_SECONDS)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Amuda Pharmacy Bot",
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    OPENROUTER_API_URL,
                    headers=headers,
                    json=payload,
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        self._raise_api_error(response.status, body)
        except TimeoutError as exc:
            raise RuntimeError("انتهت مهلة تحليل الصورة عبر OpenRouter") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError("تعذر الاتصال بخدمة OpenRouter") from exc

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("OpenRouter أعاد استجابة غير مفهومة") from exc

        text = self._extract_text(content)
        if not text:
            raise ValueError("OpenRouter لم يُرجع بيانات")
        return GeminiSchedule.model_validate_json(self._strip_code_fence(text))

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
            return "".join(parts).strip()
        return ""

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _raise_api_error(status: int, body: str) -> None:
        if status == 401:
            raise RuntimeError("مفتاح OpenRouter غير صالح")
        if status == 402:
            raise RuntimeError("رصيد OpenRouter غير كافٍ")
        if status == 429:
            raise RuntimeError("ضغط مؤقت على OpenRouter؛ أعد المحاولة بعد قليل")

        message = ""
        try:
            error = json.loads(body).get("error", {})
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"].strip()[:300]
        except (json.JSONDecodeError, AttributeError):
            pass
        suffix = f": {message}" if message else ""
        raise RuntimeError(f"فشل OpenRouter برمز {status}{suffix}")


# Compatibility alias: existing handlers keep the old dependency name.
GeminiScheduleReader = OpenRouterScheduleReader
