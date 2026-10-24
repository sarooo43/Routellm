from app.providers.base import ModelProvider
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.groq_provider import GroqProvider

PROVIDER_REGISTRY: dict[str, ModelProvider] = {
    "groq": GroqProvider(),
    "gemini": GeminiProvider(),
    "deepseek": DeepSeekProvider(),
}
