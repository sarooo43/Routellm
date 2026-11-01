"""Minimal HTTPS webhook receiver that calls RouteLLM.

Run:
    pip install fastapi uvicorn httpx
    export ROUTELLM_BASE_URL=https://routellm.example.com
    export ROUTELLM_API_KEY=rk_...
    export WEBHOOK_SECRET=choose-a-long-random-secret
    uvicorn examples.webhook.receiver:app --host 0.0.0.0 --port 9000

This example uses a shared-secret header for simplicity. For a real platform
webhook (Stripe, GitHub, Slack, etc.), verify that platform's HMAC signature
instead.
"""

import hmac
import os

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="RouteLLM webhook example")


class WebhookPayload(BaseModel):
    """Incoming webhook body."""

    text: str


@app.post("/webhook")
async def webhook(
    payload: WebhookPayload,
    x_webhook_secret: str = Header(default=None),
) -> dict:
    """Verify the shared secret, call RouteLLM over HTTPS, return the answer."""
    expected = os.environ.get("WEBHOOK_SECRET")
    if not expected or not hmac.compare_digest(x_webhook_secret or "", expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    base_url = os.environ.get("ROUTELLM_BASE_URL", "https://routellm.example.com")
    api_key = os.environ.get("ROUTELLM_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ROUTELLM_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/api/v1/route",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"prompt": payload.text},
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="RouteLLM request failed")
    data = response.json()
    return {
        "model_used": data.get("model_used"),
        "result": data.get("result"),
        "request_id": data.get("request_id"),
    }
