"""Provider registry.

Real providers are always registered so the catalog, docs and BYOK validation
know about them. The demo (mock) provider is registered **only** when
``DEMO_MODE_ENABLED`` is true, so a deployment cannot accidentally route real
traffic to a fake model.

``routable_providers()`` answers the question routing actually asks: which
providers may a normal request be sent to **automatically**? The demo provider
is never among them — a synthetic answer must always be something the caller
asked for explicitly (``force_provider: "mock"`` or OpenAI model ``mock``), so
it can never appear in a real user's reply by accident.

That explicitness is what keeps demo traffic out of usage numbers and
benchmarks: it only exists in requests that named it.
"""

from typing import Optional

from app.config import settings
from app.providers.base import ModelProvider
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.groq_provider import GroqProvider

#: Real, registry-name -> environment variable that carries an operator key.
SERVER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

PROVIDER_REGISTRY: dict[str, ModelProvider] = {
    "groq": GroqProvider(),
    "gemini": GeminiProvider(),
    "deepseek": DeepSeekProvider(),
}

if settings.DEMO_MODE_ENABLED:
    # Imported lazily so a demo-disabled process never loads the module.
    from app.providers.demo_provider import DEMO_PROVIDER_NAME, DemoProvider

    PROVIDER_REGISTRY[DEMO_PROVIDER_NAME] = DemoProvider()


def server_key_configured_providers() -> list[str]:
    """Providers with an operator key in the environment, in registry order.

    Returns names only — never the values.
    """
    import os

    configured = []
    for name, env_name in SERVER_KEY_ENV.items():
        if name in PROVIDER_REGISTRY and (os.environ.get(env_name) or "").strip():
            configured.append(name)
    return configured


def demo_provider_available() -> bool:
    """True when the mock provider is registered for this process."""
    return "mock" in PROVIDER_REGISTRY


def routable_providers() -> list[str]:
    """Real providers automatic routing may consider (never the demo one)."""
    return [name for name in PROVIDER_REGISTRY if not provider_is_demo(name)]


def demo_provider_usable() -> bool:
    """True when a caller could reach the demo provider right now.

    Only while it is registered *and* no real provider has a key: once real
    credentials exist, silently answering from a mock would be worse than
    saying "add a key".
    """
    return demo_provider_available() and not server_key_configured_providers()


def provider_is_demo(provider_name: Optional[str]) -> bool:
    """True for the mock provider name (no import of the demo module)."""
    return provider_name == "mock"


def real_provider_names() -> list[str]:
    """Provider names a caller can store a BYOK key for (never the demo one)."""
    return [name for name in PROVIDER_REGISTRY if not provider_is_demo(name)]
