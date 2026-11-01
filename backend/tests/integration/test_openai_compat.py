"""OpenAI adapter integration tests: real ASGI app and routing pipeline.

Only upstream providers are stubbed. Classification, scoring, quota checks,
circuit breaking, fallback and SQLite persistence all run for real. Redis is
fakeredis; no test makes a provider API call or needs credentials.
"""

import time

import httpx
import pytest
from openai.types import Model
from openai.types.chat import ChatCompletion
from sqlalchemy import select

from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.core.circuit_breaker import CircuitBreaker
from app.db.session import get_session
from app.main import app
from app.models import TaskLog
from tests.integration.conftest import StubProvider, expected_ranking, fail, ok

CHAT_URL = "/v1/chat/completions"
UPSTREAM_ERROR = {
    "error": {
        "message": "All providers failed to generate a response",
        "type": "upstream_error",
        "code": "all_providers_failed",
    }
}


@pytest.fixture
def stubs(install_stubs):
    """Healthy, recording upstream providers, restored after each test."""
    return install_stubs(
        **{
            name: StubProvider(name, [ok(name, text=f"Hello from {name}", tokens=21)])
            for name in ("groq", "gemini", "deepseek")
        }
    )


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client, stubs, auth_headers):
    """Use the actual mounted app, with isolated DB and shared Redis clients."""

    async def override_session():
        yield db_session

    monkeypatch.setattr(
        app,
        "dependency_overrides",
        {**app.dependency_overrides, get_session: override_session},
    )
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=auth_headers
    ) as ac:
        yield ac


def chat_body(model="routellm-auto", content="Say hi", **overrides):
    """Build a minimal request, allowing individual fields to be overridden."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        **overrides,
    }


async def test_auto_completion_runs_pipeline_and_matches_openai_schema(
    client, stubs, breaker, db_session
):
    before = int(time.time())
    response = await client.post(CHAT_URL, json=chat_body())
    assert response.status_code == 200
    data = response.json()

    assert set(data) == {
        "id", "object", "created", "model", "choices", "usage", "x_routellm_metadata"
    }
    assert data["id"].startswith("chatcmpl-")
    assert data["object"] == "chat.completion"
    assert before <= data["created"] <= int(time.time())
    assert data["model"] == "groq"
    assert data["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from groq"},
            "finish_reason": "stop",
        }
    ]
    assert data["usage"] == {
        "prompt_tokens": 0, "completion_tokens": 21, "total_tokens": 21
    }
    metadata = data["x_routellm_metadata"]
    assert set(metadata) == {
        "confidence_score", "fallback_chain_attempted", "routing_method"
    }
    assert metadata["confidence_score"] > 0
    assert metadata["routing_method"] == "rule_based"
    assert metadata["fallback_chain_attempted"] == [
        {
            "provider_name": "groq",
            "success": True,
            "error": None,
            "latency_ms": 50.0,
            "chaos_injected": False,
        }
    ]
    # Parsing with the REAL SDK's response type catches missing required fields.
    parsed = ChatCompletion.model_validate(data)
    assert parsed.choices[0].message.content == "Hello from groq"
    assert parsed.model_extra["x_routellm_metadata"] == metadata

    assert stubs["groq"].calls == 1
    assert stubs["groq"].kwargs_seen == [{"temperature": 0.7}]
    assert stubs["gemini"].calls == stubs["deepseek"].calls == 0
    assert breaker.success_calls == ["groq"]
    assert breaker.failure_calls == []
    log = (await db_session.execute(select(TaskLog))).scalars().one()
    assert log.status == "success"
    assert log.selected_model == "groq"
    assert data["id"] == f"chatcmpl-{log.request_id}"


async def test_auto_completion_uses_real_scorer_and_fallback_chain(
    client, stubs, breaker
):
    stubs["groq"].responses = [fail("groq")]
    response = await client.post(CHAT_URL, json=chat_body(content="What is the capital of France?"))
    assert response.status_code == 200
    data = response.json()
    order = expected_ranking("qa")
    assert order[:2] == ["groq", "gemini"]
    assert data["model"] == "gemini"
    chain = data["x_routellm_metadata"]["fallback_chain_attempted"]
    assert [attempt["provider_name"] for attempt in chain] == order[:2]
    assert [attempt["success"] for attempt in chain] == [False, True]
    assert chain[0]["error"] == "rate_limit_exceeded"
    assert breaker.failure_calls == ["groq"]
    assert breaker.success_calls == ["gemini"]
    assert stubs["deepseek"].calls == 0


async def test_named_provider_is_forced_without_ranking_or_fallback(
    client, stubs, monkeypatch, breaker
):
    def must_not_rank(*args, **kwargs):
        pytest.fail("A named provider must bypass ranking")

    monkeypatch.setattr(executor_module, "rank_models", must_not_rank)
    response = await client.post(CHAT_URL, json=chat_body(model="deepseek"))
    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "deepseek"
    assert data["choices"][0]["message"]["content"] == "Hello from deepseek"
    assert data["x_routellm_metadata"]["confidence_score"] == 0.0
    chain = data["x_routellm_metadata"]["fallback_chain_attempted"]
    assert len(chain) == 1
    assert chain[0]["provider_name"] == "deepseek"
    assert stubs["deepseek"].calls == 1
    assert stubs["groq"].calls == stubs["gemini"].calls == 0
    # Match Phase 11's forced baseline, including its no-breaker-side-effects rule.
    assert breaker.success_calls == breaker.failure_calls == []


async def test_named_provider_failure_never_falls_back(client, stubs):
    stubs["deepseek"].responses = [fail("deepseek", error="timeout")]
    response = await client.post(CHAT_URL, json=chat_body(model="deepseek"))
    assert response.status_code == 502
    assert response.json() == UPSTREAM_ERROR
    assert stubs["deepseek"].calls == 1
    assert stubs["groq"].calls == stubs["gemini"].calls == 0


@pytest.mark.parametrize("model", ["groq", "gemini", "deepseek"])
async def test_each_registered_provider_can_be_selected(client, stubs, model):
    response = await client.post(CHAT_URL, json=chat_body(model=model))
    assert response.status_code == 200
    assert response.json()["model"] == model
    assert stubs[model].calls == 1
    assert sum(stub.calls for stub in stubs.values()) == 1


async def test_unknown_model_returns_openai_error_before_execution(client, stubs):
    response = await client.post(CHAT_URL, json=chat_body(model="not_a_real_model"))
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Unknown model 'not_a_real_model'. Valid options: "
            "routellm-auto, groq, gemini, deepseek",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    assert all(stub.calls == 0 for stub in stubs.values())


@pytest.mark.parametrize("model", ["routellm-auto", "deepseek", "not_a_real_model"])
async def test_streaming_is_explicitly_rejected(client, stubs, model):
    response = await client.post(
        CHAT_URL, json=chat_body(model=model, stream=True)
    )
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Streaming is not yet supported",
            "type": "invalid_request_error",
            "code": "streaming_not_supported",
        }
    }
    assert all(stub.calls == 0 for stub in stubs.values())


@pytest.mark.parametrize("stream", [False, None])
async def test_non_streaming_flag_values_are_accepted(client, stream):
    response = await client.post(CHAT_URL, json=chat_body(stream=stream))
    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"


async def test_model_list_matches_openai_sdk_and_tracks_registry(client, stubs):
    response = await client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"object", "data"}
    assert data["object"] == "list"
    assert data["data"] == [
        {"id": "routellm-auto", "object": "model", "created": 0, "owned_by": "routellm"},
        {"id": "groq", "object": "model", "created": 0, "owned_by": "groq"},
        {"id": "gemini", "object": "model", "created": 0, "owned_by": "google"},
        {"id": "deepseek", "object": "model", "created": 0, "owned_by": "deepseek"},
    ]
    assert [Model.model_validate(item).id for item in data["data"]] == [
        item["id"] for item in data["data"]
    ]
    assert all(stub.calls == 0 for stub in stubs.values())


async def test_conversation_preserves_order_roles_and_latest_user_message(client, stubs):
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "My name is Sam."},
        {"role": "assistant", "content": "Hello, Sam."},
        {"role": "user", "content": "What is my name?"},
    ]
    response = await client.post(
        CHAT_URL,
        json=chat_body(content="unused", messages=messages, task_type="qa"),
    )
    assert response.status_code == 200
    expected_prompt = (
        "System: Be concise.\n\n"
        "User: My name is Sam.\n\n"
        "Assistant: Hello, Sam.\n\n"
        "User: What is my name?"
    )
    assert stubs["groq"].prompts_seen == [expected_prompt]
    assert response.json()["choices"][0]["message"]["content"] == "Hello from groq"


async def test_task_type_override_reaches_classifier_and_persistence(
    client, stubs, db_session
):
    response = await client.post(
        CHAT_URL,
        json=chat_body(content="Say hi", task_type="code", model="deepseek"),
    )
    assert response.status_code == 200
    assert stubs["deepseek"].kwargs_seen == [
        {"model_variant": "deepseek-coder", "temperature": 0.7}
    ]
    log = (await db_session.execute(select(TaskLog))).scalars().one()
    assert log.task_type == "code"


@pytest.mark.parametrize("model", ["routellm-auto", "deepseek"])
async def test_generation_options_reach_the_executor_on_both_paths(
    client, stubs, model
):
    response = await client.post(
        CHAT_URL,
        json=chat_body(model=model, temperature=0.2, max_tokens=64),
    )
    assert response.status_code == 200
    expected = {"temperature": 0.2, "max_tokens": 64}
    if model == "deepseek":
        expected["model_variant"] = "deepseek-chat"
    assert stubs["groq" if model == "routellm-auto" else model].kwargs_seen == [expected]


async def test_generation_options_survive_fallback(client, stubs):
    stubs["groq"].responses = [fail("groq")]
    response = await client.post(
        CHAT_URL,
        json=chat_body(
            content="What is the capital of France?",
            temperature=0.1,
            max_tokens=32,
        ),
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gemini"
    expected = {"temperature": 0.1, "max_tokens": 32}
    assert stubs["groq"].kwargs_seen == [expected]
    assert stubs["gemini"].kwargs_seen == [expected]


async def test_unavailable_token_count_is_zero_not_fabricated(client, stubs):
    stubs["groq"].responses = [ok("groq", tokens=0)]
    response = await client.post(CHAT_URL, json=chat_body())
    assert response.status_code == 200
    assert response.json()["usage"] == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0
    }


async def test_total_provider_failure_returns_502_and_records_every_attempt(
    client, stubs, breaker, db_session
):
    for name, stub in stubs.items():
        stub.responses = [fail(name, error="provider_server_error")]
    response = await client.post(CHAT_URL, json=chat_body(task_type="qa"))
    assert response.status_code == 502
    assert response.json() == UPSTREAM_ERROR
    assert all(stub.calls == 1 for stub in stubs.values())
    assert breaker.failure_calls == expected_ranking("qa")
    assert breaker.success_calls == []
    log = (await db_session.execute(select(TaskLog))).scalars().one()
    assert log.status == "failed"
    assert log.error_message == "all_providers_failed"
    assert len(log.fallback_chain) == 3


async def test_open_circuit_is_skipped_by_the_compatibility_endpoint(
    client, stubs, breaker
):
    for _ in range(5):
        await breaker.record_failure("groq")
    breaker.failure_calls.clear()
    response = await client.post(CHAT_URL, json=chat_body(task_type="qa"))
    assert response.status_code == 200
    assert response.json()["model"] == "gemini"
    chain = response.json()["x_routellm_metadata"]["fallback_chain_attempted"]
    # The real scorer ranks OPEN providers last; a healthy provider succeeds
    # before the chain reaches it. Either way, the broken provider is not called.
    assert [attempt["provider_name"] for attempt in chain] == ["gemini"]
    assert stubs["groq"].calls == 0
    assert stubs["gemini"].calls == 1
    assert stubs["deepseek"].calls == 0
    assert breaker.failure_calls == []


@pytest.mark.parametrize(
    "body,param",
    [
        ({"model": "routellm-auto"}, "messages"),
        (chat_body(content=[]), "messages.0.content"),
        ({"model": "routellm-auto", "messages": []}, "messages"),
        (chat_body(messages=[{"role": "tool", "content": "hi"}]), "messages.0.role"),
        (chat_body(max_tokens=0), "max_tokens"),
        (chat_body(temperature=3), "temperature"),
    ],
)
async def test_invalid_request_uses_openai_error_envelope(client, stubs, body, param):
    response = await client.post(CHAT_URL, json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_request"
    assert error["param"] == param
    assert isinstance(error["message"], str)
    assert all(stub.calls == 0 for stub in stubs.values())


async def test_unexpected_adapter_failure_does_not_leak_details(client, monkeypatch):
    async def broken_executor(*args, **kwargs):
        raise RuntimeError("sensitive upstream details")

    monkeypatch.setattr(
        "app.api.openai_compat_routes.execute_with_fallback", broken_executor
    )
    response = await client.post(CHAT_URL, json=chat_body())
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "message": "Internal server error",
            "type": "server_error",
            "code": "internal_error",
        }
    }


async def test_legacy_route_keeps_its_rich_response_and_validation(client, stubs):
    response = await client.post("/api/v1/route", json={"prompt": "Say hi"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {
        "success", "result", "model_used", "confidence_score",
        "fallback_chain_attempted", "latency_ms", "tokens_used", "request_id",
        "error", "routing_method",
    }
    assert data["result"] == "Hello from groq"
    # Legacy callers do not opt into generation settings: provider kwargs and
    # the legacy 422 validation envelope must remain unchanged.
    assert stubs["groq"].kwargs_seen == [{}]
    invalid = await client.post("/api/v1/route", json={})
    assert invalid.status_code == 422
    assert "detail" in invalid.json()
    assert "error" not in invalid.json()
