"""Manually curated catalog metadata for the models RouteLLM can route to.

This is editorial/descriptive data for the Catalog page. Operational health,
pricing numbers, and live performance are merged in by the API at request time.
"""

MODEL_CATALOG: dict[str, dict] = {
    "groq": {
        "display_name": "Llama 3.3 70B (via Groq)",
        "description": (
            "Ultra-low-latency inference, ideal for fast Q&A and conversational tasks."
        ),
        "context_window": 128000,
        "capabilities": ["qa", "general", "summarization"],
        "pricing_tier": "free",
        "max_output_tokens": 8192,
    },
    "gemini": {
        "display_name": "Gemini 2.0 Flash",
        "description": (
            "Large context window with strong summarization and multimodal-ready architecture."
        ),
        "context_window": 1000000,
        "capabilities": ["summarization", "general", "qa"],
        "pricing_tier": "free",
        "max_output_tokens": 8192,
    },
    "deepseek": {
        "display_name": "DeepSeek V3 / Coder",
        "description": (
            "Strongest coding and reasoning performance in our current lineup at very low cost."
        ),
        "context_window": 64000,
        "capabilities": ["code", "reasoning"],
        "pricing_tier": "cheap",
        "max_output_tokens": 8192,
    },
}
