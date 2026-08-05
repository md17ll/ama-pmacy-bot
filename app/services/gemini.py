from __future__ import annotations

import asyncio
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

# OpenAI strict structured outputs require every property to be listed in
# "required", even nullable ones. A hand-written schema avoids provider-side
# validation errors caused by defaults in an auto-generated Pydantic schema.
OPENROUTER_SCHEDULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "maxItems": MAX_GEMINI_ROWS,
            "items": {
                "type": "object",
                "properties": {
                    "pharmacy_name": {
                        "type": "string",
                        "description": "اسم الصيدلية كما يظهر في الصورة",
                    },
                    "duty_date": {
                        "type": "string",
                        "description": "تاريخ المناوبة بصيغة YYYY-MM-DD",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "وقت بداية المناوبة مع AM أو PM",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "وقت نهاية المناوبة مع AM أو PM",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "note": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": [
                    "pharmacy_name",
                    "duty_date",
                    "start_time",
                    "end_time",
                    "confidence",
                    "note",
                ],
                "additionalProperties": False,
            },
        },
        "document_language": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ]
        },
        "warnings": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string"},
        },
    },
    "required": ["rows", "document_language", "warnings"],
    "additionalProperties": False,
}


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


class OpenRouterRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        error_type: str | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.provider_code = provider_code


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

    def _request_payload(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        strict_schema: bool = True,
    ) -> dict[str, Any]:
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
أعد كائناً بصيغة JSON فقط، يحتوي على المفاتيح rows وdocument_language وwarnings.
كل عنصر داخل rows يجب أن يحتوي على pharmacy_name وduty_date وstart_time وend_time وconfidence وnote.
""".strip()
        response_format: dict[str, Any]
        if strict_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "amuda_pharmacy_schedule",
                    "strict": True,
                    "schema": OPENROUTER_SCHEDULE_SCHEMA,
                },
            }
        else:
            response_format = {"type": "json_object"}

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
            "response_format": response_format,
            "provider": {
                "require_parameters": True,
                "allow_fallbacks": True,
            },
            "max_completion_tokens": 8000,
        }

    async def _request_schedule(self, image_bytes: bytes, mime_type: str) -> GeminiSchedule:
        try:
            data = await self._post_payload(
                self._request_payload(image_bytes, mime_type, strict_schema=True)
            )
        except OpenRouterRequestError as exc:
            if not self._should_retry_without_schema(exc):
                raise
            # Some upstream providers reject otherwise valid strict JSON schemas.
            # Retry once with JSON mode while keeping the same GPT model, then
            # validate the returned object locally with Pydantic.
            data = await self._post_payload(
                self._request_payload(image_bytes, mime_type, strict_schema=False)
            )

        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            refusal = message.get("refusal")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError("OpenRouter أعاد استجابة غير مفهومة") from exc

        if refusal:
            raise ValueError(f"النموذج رفض تحليل الصورة: {str(refusal)[:200]}")
        text = self._extract_text(content)
        if not text:
            raise ValueError("OpenRouter لم يُرجع بيانات")
        return GeminiSchedule.model_validate_json(self._strip_code_fence(text))

    async def _post_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
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
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as exc:
                        if response.status != 200:
                            raise OpenRouterRequestError(
                                f"فشل OpenRouter برمز {response.status}",
                                status=response.status,
                            ) from exc
                        raise ValueError("OpenRouter أعاد استجابة غير مفهومة") from exc
                    if response.status != 200 or data.get("error"):
                        raise self._build_api_error(response.status, data)
                    return data
        except asyncio.TimeoutError as exc:
            raise RuntimeError("انتهت مهلة تحليل الصورة عبر OpenRouter") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError("تعذر الاتصال بخدمة OpenRouter") from exc

    @staticmethod
    def _should_retry_without_schema(exc: OpenRouterRequestError) -> bool:
        if exc.status != 400:
            return False
        blocked_types = {
            "invalid_image",
            "image_too_large",
            "image_too_small",
            "unsupported_image_format",
            "content_policy_violation",
            "refusal",
        }
        return exc.error_type not in blocked_types

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
    def _build_api_error(status: int, data: dict[str, Any]) -> OpenRouterRequestError:
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        metadata = error.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        error_type = metadata.get("error_type") or data.get("error_type")
        provider_code = metadata.get("provider_code")
        message = error.get("message")
        message = message.strip()[:300] if isinstance(message, str) else ""
        effective_status = error.get("code") if isinstance(error.get("code"), int) else status

        if effective_status == 401:
            text = "مفتاح OpenRouter غير صالح"
        elif effective_status == 402:
            text = "رصيد OpenRouter غير كافٍ"
        elif effective_status == 403:
            text = "طلب OpenRouter مرفوض بسبب الصلاحيات أو الحماية"
        elif effective_status == 429:
            text = "ضغط مؤقت على OpenRouter؛ أعد المحاولة بعد قليل"
        elif error_type == "invalid_image":
            text = "الصورة غير صالحة أو غير قابلة للقراءة"
        elif error_type == "image_too_large":
            text = "أبعاد الصورة أو حجمها أكبر من حد مزوّد النموذج"
        elif error_type == "unsupported_image_format":
            text = "مزود النموذج لا يدعم صيغة الصورة"
        else:
            details = [item for item in (message, error_type, provider_code) if item]
            suffix = f": {' | '.join(map(str, details))}" if details else ""
            text = f"فشل OpenRouter برمز {effective_status}{suffix}"

        return OpenRouterRequestError(
            text,
            status=int(effective_status or status),
            error_type=str(error_type) if error_type else None,
            provider_code=str(provider_code) if provider_code else None,
        )


# Compatibility alias: existing handlers keep the old dependency name.
GeminiScheduleReader = OpenRouterScheduleReader
