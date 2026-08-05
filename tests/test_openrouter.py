from __future__ import annotations

import base64
import json

import pytest

from app.services.gemini import (
    GeminiSchedule,
    GeminiScheduleReader,
    OPENROUTER_SCHEDULE_SCHEMA,
    OpenRouterRequestError,
)


def test_openrouter_payload_uses_selected_vision_model_and_json_schema() -> None:
    reader = GeminiScheduleReader("test-key", "openai/gpt-5.4-mini")
    payload = reader._request_payload(b"image-bytes", "image/jpeg")

    assert payload["model"] == "openai/gpt-5.4-mini"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["provider"]["require_parameters"] is True
    assert payload["max_completion_tokens"] == 8000

    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    expected = base64.b64encode(b"image-bytes").decode("ascii")
    assert content[1]["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"


def test_strict_schema_requires_every_property() -> None:
    assert set(OPENROUTER_SCHEDULE_SCHEMA["required"]) == {
        "rows",
        "document_language",
        "warnings",
    }
    row_schema = OPENROUTER_SCHEDULE_SCHEMA["properties"]["rows"]["items"]
    assert set(row_schema["required"]) == set(row_schema["properties"])
    assert row_schema["additionalProperties"] is False
    assert OPENROUTER_SCHEDULE_SCHEMA["additionalProperties"] is False


def test_openrouter_can_fallback_to_json_object_mode() -> None:
    reader = GeminiScheduleReader("test-key", "openai/gpt-5.4-mini")
    payload = reader._request_payload(
        b"image-bytes",
        "image/jpeg",
        strict_schema=False,
    )
    assert payload["response_format"] == {"type": "json_object"}


def test_openrouter_retries_generic_provider_400_without_schema() -> None:
    generic = OpenRouterRequestError(
        "Provider returned error",
        status=400,
        error_type="invalid_request",
    )
    image_error = OpenRouterRequestError(
        "Invalid image",
        status=400,
        error_type="invalid_image",
    )
    auth_error = OpenRouterRequestError("Invalid key", status=401)

    assert GeminiScheduleReader._should_retry_without_schema(generic) is True
    assert GeminiScheduleReader._should_retry_without_schema(image_error) is False
    assert GeminiScheduleReader._should_retry_without_schema(auth_error) is False


def test_openrouter_structured_response_validation() -> None:
    raw = json.dumps(
        {
            "rows": [
                {
                    "pharmacy_name": "صيدلية الشفاء",
                    "duty_date": "2026-08-05",
                    "start_time": "8:00 PM",
                    "end_time": "8:00 AM",
                    "confidence": 0.98,
                    "note": None,
                }
            ],
            "document_language": "ar",
            "warnings": [],
        },
        ensure_ascii=False,
    )
    schedule = GeminiSchedule.model_validate_json(raw)

    assert schedule.rows[0].pharmacy_name == "صيدلية الشفاء"
    assert schedule.rows[0].confidence == 0.98


def test_openrouter_rejects_extra_unstructured_fields() -> None:
    with pytest.raises(Exception):
        GeminiSchedule.model_validate(
            {
                "rows": [],
                "document_language": "ar",
                "warnings": [],
                "unexpected": "value",
            }
        )


def test_openrouter_strips_json_code_fence() -> None:
    reader = GeminiScheduleReader("test-key", "openai/gpt-5.4-mini")
    assert reader._strip_code_fence("```json\n{\"rows\": []}\n```") == '{"rows": []}'
