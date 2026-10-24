"""Rule-based task classifier.

This is the v1 classifier: pure string/regex logic, no external calls, runs in
microseconds before every routing decision. The public entrypoint
``classify_task()`` delegates to the rule-based implementation; later phases
can add ``_classify_embedding_based()`` and swap the internal implementation
without changing the public signature anywhere in the codebase.
"""

import re

# ---------------------------------------------------------------------------
# Module-level keyword constants (easy to extend later)
# ---------------------------------------------------------------------------

CODE_KEYWORDS: tuple[str, ...] = (
    "function",
    "class ",
    "def ",
    "debug",
    "fix this bug",
    "write code",
    "python",
    "javascript",
    "sql query",
    "algorithm",
    "compile error",
    "stack trace",
    "refactor",
    "```",
)

CODE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\(\s*\)\s*\{"),  # function signature, e.g. "() {"
    re.compile(r"=>"),  # arrow function, e.g. "x => x * 2"
)

REASONING_KEYWORDS: tuple[str, ...] = (
    "explain why",
    "step by step",
    "prove that",
    "logic",
    "solve this problem",
    "what is the reasoning",
    "think through",
    "analyze",
)

SUMMARIZATION_KEYWORDS: tuple[str, ...] = (
    "summarize",
    "tl;dr",
    "key points",
    "in short",
    "condense",
    "give me a summary",
)

# Long pasted text with no other signal is usually meant to be summarized.
LONG_PROMPT_THRESHOLD = 1500

# A "qa" prompt is a short, direct question.
QA_MAX_LENGTH = 200
QUESTION_START_RE = re.compile(r"^(what|who|when|where|why|how|is|does|can)\b")


def classify_task(prompt: str) -> str:
    """Classify a prompt into: code, reasoning, summarization, qa, general.

    Returns "general" when no strong signal is detected. Accepts None
    (treated as an empty string) and never raises.
    """
    if not prompt or not isinstance(prompt, str):
        return "general"
    return _classify_rule_based(prompt)


def _classify_rule_based(prompt: str) -> str:
    text = prompt.strip().lower()

    if not text:
        return "general"

    # Priority order matters: a prompt can match several categories, and the
    # first match wins (code > reasoning > summarization > qa > general).
    if any(keyword in text for keyword in CODE_KEYWORDS) or any(
        pattern.search(text) for pattern in CODE_PATTERNS
    ):
        return "code"

    if any(keyword in text for keyword in REASONING_KEYWORDS):
        return "reasoning"

    if any(keyword in text for keyword in SUMMARIZATION_KEYWORDS) or (
        len(text) > LONG_PROMPT_THRESHOLD
    ):
        return "summarization"

    if (
        len(text) < QA_MAX_LENGTH
        and text.endswith("?")
        and QUESTION_START_RE.match(text)
    ):
        return "qa"

    return "general"
