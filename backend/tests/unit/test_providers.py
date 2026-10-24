import json

import httpx
import pytest
import respx

from app.providers.base import ModelProvider
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.groq_provider import GroqProvider
from app.providers.registry import PROVIDER_REGISTRY

GROQ_URL = GroqProvider.API_URL
DEEPSEEK_URL = DeepSeekProvider.API_URL
GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PATH = "/v1beta/models/gemini-2.0-flash:generateContent"


def openai_body(text="Hello from the mock!", total_tokens=21):
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": total_tokens},
    }


def gemini_body(text="Hello from the mock!", total_tokens=13):
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}],
        "usageMetadata": {"totalTokenCount": total_tokens},
    }


# ----------------------------- Groq -----------------------------


@respx.mock
async def test_groq_success():
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_body())
    )
    resp = await GroqProvider().generate("Say hi")
    assert route.called
    assert resp.success is True
    assert resp.text == "Hello from the mock!"
    assert resp.tokens_used == 21
    assert resp.provider_name == "groq"
    assert resp.model_name == "llama-3.3-70b-versatile"
    assert resp.latency_ms >= 0
    assert resp.error is None


@respx.mock
async def test_groq_rate_limit():
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(429, json={"error": "rate limit"})
    )
    resp = await GroqProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "rate_limit_exceeded"
    assert resp.latency_ms >= 0


@respx.mock
async def test_groq_timeout():
    respx.post(GROQ_URL).mock(
        side_effect=httpx.TimeoutException("simulated timeout")
    )
    resp = await GroqProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "timeout"
    assert resp.latency_ms >= 0


@respx.mock
async def test_groq_empty_response():
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_body(text="   ", total_tokens=5))
    )
    resp = await GroqProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "empty_response"
    assert resp.text == ""


@respx.mock
async def test_groq_server_error():
    respx.post(GROQ_URL).mock(
        return_value=httpx.Response(500, text="internal error")
    )
    resp = await GroqProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "provider_server_error"


# ----------------------------- Gemini -----------------------------


@respx.mock
async def test_gemini_success():
    route = respx.post(host=GEMINI_HOST, path=GEMINI_PATH).mock(
        return_value=httpx.Response(200, json=gemini_body())
    )
    resp = await GeminiProvider().generate("Say hi")
    assert route.called
    assert resp.success is True
    assert resp.text == "Hello from the mock!"
    assert resp.tokens_used == 13
    assert resp.provider_name == "gemini"
    assert resp.model_name == "gemini-2.0-flash"
    assert resp.latency_ms >= 0
    assert resp.error is None


@respx.mock
async def test_gemini_rate_limit():
    respx.post(host=GEMINI_HOST, path=GEMINI_PATH).mock(
        return_value=httpx.Response(429, json={"error": "rate limit"})
    )
    resp = await GeminiProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "rate_limit_exceeded"


@respx.mock
async def test_gemini_timeout():
    respx.post(host=GEMINI_HOST, path=GEMINI_PATH).mock(
        side_effect=httpx.TimeoutException("simulated timeout")
    )
    resp = await GeminiProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "timeout"


@respx.mock
async def test_gemini_empty_response():
    respx.post(host=GEMINI_HOST, path=GEMINI_PATH).mock(
        return_value=httpx.Response(200, json=gemini_body(text=""))
    )
    resp = await GeminiProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "empty_response"
    assert resp.text == ""


@respx.mock
async def test_gemini_server_error():
    respx.post(host=GEMINI_HOST, path=GEMINI_PATH).mock(
        return_value=httpx.Response(500, text="internal error")
    )
    resp = await GeminiProvider().generate("Say hi")
    assert resp.success is False
    assert resp.error == "provider_server_error"


# ----------------------------- DeepSeek -----------------------------


def _deepseek():
    return DeepSeekProvider()


@respx.mock
async def test_deepseek_success():
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=openai_body())
    )
    resp = await _deepseek().generate("Say hi")
    assert route.called
    assert resp.success is True
    assert resp.text == "Hello from the mock!"
    assert resp.tokens_used == 21
    assert resp.provider_name == "deepseek"
    assert resp.model_name == "deepseek-chat"
    assert resp.latency_ms >= 0
    assert resp.error is None


@respx.mock
async def test_deepseek_rate_limit():
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(429, json={"error": "rate limit"})
    )
    resp = await _deepseek().generate("Say hi")
    assert resp.success is False
    assert resp.error == "rate_limit_exceeded"


@respx.mock
async def test_deepseek_timeout():
    respx.post(DEEPSEEK_URL).mock(
        side_effect=httpx.TimeoutException("simulated timeout")
    )
    resp = await _deepseek().generate("Say hi")
    assert resp.success is False
    assert resp.error == "timeout"


@respx.mock
async def test_deepseek_empty_response():
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(
            200, json=openai_body(text="", total_tokens=3)
        )
    )
    resp = await _deepseek().generate("Say hi")
    assert resp.success is False
    assert resp.error == "empty_response"
    assert resp.text == ""


@respx.mock
async def test_deepseek_server_error():
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(503, text="service unavailable")
    )
    resp = await _deepseek().generate("Say hi")
    assert resp.success is False
    assert resp.error == "provider_server_error"


# ----------------------------- Extras -----------------------------


@respx.mock
async def test_deepseek_model_variant_kwarg():
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=openai_body())
    )
    resp = await _deepseek().generate("write code", model_variant="deepseek-coder")
    assert resp.success is True
    assert resp.model_name == "deepseek-coder"
    sent_payload = json.loads(route.calls.last.request.content)
    assert sent_payload["model"] == "deepseek-coder"


async def test_registry_contains_expected_providers():
    assert set(PROVIDER_REGISTRY.keys()) == {"groq", "gemini", "deepseek"}
    for name, provider in PROVIDER_REGISTRY.items():
        assert isinstance(provider, ModelProvider)
        assert provider.provider_name == name
