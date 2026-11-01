"""Smoke-test the REAL OpenAI SDK over HTTP against a running RouteLLM server.

Start the credential-free demo server first (from backend/):
    python -m uvicorn scripts.demo_server:app --host 0.0.0.0 --port 8000

Then, in another terminal:
    pip install openai
    python scripts/smoke_openai_sdk.py

This is deliberately not an ASGITransport/TestClient test: the SDK's normal
HTTP transport connects to localhost:8000. Only upstream LLM providers are
stubbed by demo_server; the HTTP server, routing pipeline and SDK are real.
The demo database is local demo.db, and no provider API keys are needed.
The script generates a lightweight RouteLLM demo API key and sends it as
X-API-Key; protected routing/chat endpoints require that header.
"""

import json

import httpx
import openai
from openai import BadRequestError, OpenAI


def main() -> None:
    """Exercise model discovery, automatic routing, errors, and the legacy API."""
    generated = httpx.post(
        "http://localhost:8000/api/v1/keys/generate",
        json={"label": "OpenAI SDK smoke"},
        timeout=30.0,
    )
    generated.raise_for_status()
    demo_key = generated.json()["api_key"]
    print(f"Demo key prefix: {generated.json()['key_prefix']}")

    client = OpenAI(
        base_url="http://localhost:8000/v1",
        api_key="anything",
        default_headers={"X-API-Key": demo_key},
        max_retries=0,
        timeout=30.0,
    )
    print(f"openai=={openai.__version__}")
    print(f"base_url={client.base_url}")

    models = client.models.list()
    model_ids = [model.id for model in models.data]
    assert "routellm-auto" in model_ids
    print(f"models.list(): {model_ids}")

    response = client.chat.completions.create(
        model="routellm-auto",
        messages=[{"role": "user", "content": "Say hello in one short sentence."}],
    )
    assert response.object == "chat.completion"
    assert response.id.startswith("chatcmpl-")
    assert response.choices[0].message.content
    assert response.choices[0].finish_reason == "stop"
    assert response.model in model_ids
    print("chat.completions.create():")
    print(json.dumps(response.model_dump(), indent=2))

    try:
        client.chat.completions.create(
            model="not_a_real_model",
            messages=[{"role": "user", "content": "Hello"}],
        )
    except BadRequestError as exc:
        assert exc.code == "model_not_found"
        print(f"unknown model: {exc.status_code} {exc.code}")
    else:
        raise AssertionError("Unknown model did not raise BadRequestError")

    try:
        client.chat.completions.create(
            model="routellm-auto",
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
        )
    except BadRequestError as exc:
        assert exc.code == "streaming_not_supported"
        print(f"stream=true: {exc.status_code} {exc.code}")
    else:
        raise AssertionError("Streaming did not raise BadRequestError")

    legacy = httpx.post(
        "http://localhost:8000/api/v1/route",
        json={"prompt": "Say hello in one short sentence."},
        headers={"X-API-Key": demo_key},
        timeout=30.0,
    )
    legacy.raise_for_status()
    legacy_data = legacy.json()
    assert legacy_data["success"] is True
    assert "confidence_score" in legacy_data
    assert "fallback_chain_attempted" in legacy_data
    print(f"legacy /api/v1/route: {legacy.status_code}, success={legacy_data['success']}")
    print("PASS: real OpenAI SDK over HTTP; upstream providers are demo stubs.")


if __name__ == "__main__":
    main()
