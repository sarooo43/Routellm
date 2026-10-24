import time

from app.core.classifier import classify_task


def test_code_prompt():
    assert classify_task("Write a python function to reverse a list") == "code"


def test_code_prompt_code_fence():
    assert classify_task("```\nprint('hello')\n```") == "code"


def test_code_prompt_arrow_pattern():
    assert classify_task("Use map(x => x * 2) to double the values") == "code"


def test_code_prompt_function_signature_pattern():
    assert classify_task("insert this at the top: main() { return 0; }") == "code"


def test_reasoning_prompt():
    assert classify_task("Explain why the sky is blue, step by step") == "reasoning"


def test_summarization_keyword():
    assert classify_task("Summarize this article for me") == "summarization"


def test_summarization_long_text_no_other_signal():
    long_text = ("the quick brown fox jumps over the lazy dog. " * 50).strip()
    assert len(long_text) > 1500
    assert classify_task(long_text) == "summarization"


def test_qa_short_direct_question():
    assert classify_task("What is the capital of France?") == "qa"


def test_qa_rejects_long_question():
    long_question = "What " + "x " * 200 + "?"
    assert classify_task(long_question) == "general"


def test_ambiguous_generic_prompt():
    assert classify_task("Tell me something interesting about space.") == "general"


def test_mixed_code_and_reasoning_code_wins():
    assert classify_task("Explain why this function has a bug") == "code"


def test_empty_string_returns_general():
    assert classify_task("") == "general"


def test_whitespace_only_returns_general():
    assert classify_task("   \n\t  ") == "general"


def test_none_returns_general():
    assert classify_task(None) == "general"


def test_non_ascii_input_does_not_crash():
    assert classify_task("日本語のテキストです。これは何ですか？") in {
        "code",
        "reasoning",
        "summarization",
        "qa",
        "general",
    }


def test_performance_single_call_under_5ms():
    prompt = "Explain why this function has a bug and suggest a fix. " * 30
    start = time.perf_counter()
    classify_task(prompt)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 5
