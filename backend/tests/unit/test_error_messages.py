"""Provider failures must be explained, not dumped.

Field report that produced this file: a user pasted a Groq key, sent a prompt,
and got "All providers failed: unexpected_error: ConnectError". They concluded
**the backend was not running** — it was, and had answered every request with a
200. The raw code sent them debugging the wrong system.

So these tests assert two things, in order of importance:

1. the message never claims something we cannot know (a ConnectError is *this
   machine's* outbound failure, not proof the provider is down, and says nothing
   about the user's key);
2. the message says what to do next, and names the provider.
"""

import pytest

from app.core.error_messages import (
    CONSOLE_URLS,
    explain,
    summarize_attempt_chain,
)


# --------------------------------------------------------------------------
# The exact string from the field report.
# --------------------------------------------------------------------------


def test_the_field_report_error_is_translated():
    message = explain("unexpected_error: ConnectError", "groq")
    assert message is not None
    assert message.startswith("network_unreachable:")
    # The dead end users hit: they believe the gateway is down.
    assert "gateway is working" in message
    assert "groq" in message
    # Still greppable, and still shows the original code.
    assert "ConnectError" in message


def test_network_failure_does_not_blame_the_provider_or_the_key():
    """We know the request failed to leave this machine. Say only that."""
    message = explain("ConnectError", "groq")
    lowered = message.lower()
    assert "provider is down" not in lowered
    assert "invalid key" not in lowered
    assert "wrong key" not in lowered
    # And it offers the fixes that actually apply.
    assert "internet access" in lowered
    assert "proxy" in lowered or "vpn" in lowered


@pytest.mark.parametrize(
    "code",
    ["ConnectError", "ConnectTimeout", "ReadTimeout", "timeout", "network_error"],
)
def test_every_transport_failure_maps_to_network_unreachable(code):
    assert explain(code, "gemini").startswith("network_unreachable:")


# --------------------------------------------------------------------------
# The other families
# --------------------------------------------------------------------------


def test_auth_failure_points_at_the_console():
    message = explain("auth_failed", "groq")
    assert message.startswith("auth_failed:")
    assert CONSOLE_URLS["groq"] in message
    assert "Providers page" in message


@pytest.mark.parametrize("code", ["unexpected_error: HTTP 401", "unauthorized", "forbidden"])
def test_http_401_and_403_are_auth_problems(code):
    assert explain(code, "deepseek").startswith("auth_failed:")


def test_provider_side_error_clears_this_machine_and_the_key():
    message = explain("provider_server_error", "gemini")
    assert message.startswith("provider_unavailable:")
    assert "Nothing is wrong with this machine or your key" in message
    assert "fallback" in message


@pytest.mark.parametrize("code", ["unexpected_error: HTTP 503", "overloaded"])
def test_5xx_from_the_provider_is_provider_unavailable(code):
    assert explain(code, "gemini").startswith("provider_unavailable:")


def test_rate_limit_suggests_waiting_or_fallback():
    message = explain("rate_limit_exceeded", "groq")
    assert message.startswith("rate_limited:")
    assert "Wait" in message
    assert "fallback" in message


def test_chaos_failure_is_labelled_as_simulated():
    message = explain("chaos_injected_failure", "groq")
    assert message.startswith("chaos_injected:")
    assert "simulated" in message


def test_unknown_code_still_names_a_next_step():
    message = explain("something_new", "groq")
    assert message.startswith("provider_error:")
    assert "server log" in message


# --------------------------------------------------------------------------
# Contract details
# --------------------------------------------------------------------------


def test_empty_error_is_not_invented():
    assert explain(None) is None
    assert explain("") is None
    assert explain("   ") is None


def test_provider_is_optional():
    assert "the provider" in explain("ConnectError")


def test_every_message_is_one_sentence_plus_action():
    """Long enough to be useful, short enough to show in a card."""
    for code in ["ConnectError", "auth_failed", "provider_server_error", "rate_limit_exceeded"]:
        message = explain(code, "groq")
        assert 60 <= len(message) <= 400, (code, len(message))


def test_chain_summary_uses_the_first_real_failure():
    attempts = [
        {"provider_name": "groq", "success": False, "error": "ConnectError"},
        {"provider_name": "gemini", "success": False, "error": "auth_failed"},
    ]
    summary = summarize_attempt_chain(attempts)
    assert summary.startswith("network_unreachable:")
    assert "groq" in summary
    # The second failure gets one clause, not its own paragraph.
    assert "All 2 attempted providers failed" in summary


def test_chain_summary_stays_short_for_a_single_failure():
    """No "All 1 attempted providers failed" noise."""
    summary = summarize_attempt_chain(
        [{"provider_name": "groq", "success": False, "error": "ConnectError"}]
    )
    assert summary.startswith("network_unreachable:")
    assert "All" not in summary


def test_chain_summary_handles_a_succeeded_chain():
    """Nothing failed, so there is nothing to explain."""
    assert (
        summarize_attempt_chain([{"provider_name": "groq", "success": True, "error": None}])
        is None
    )


def test_chain_summary_is_silent_when_anything_succeeded():
    """A fallback that worked is not a failure, even if the first leg failed."""
    attempts = [
        {"provider_name": "groq", "success": False, "error": "ConnectError"},
        {"provider_name": "gemini", "success": True, "error": None},
    ]
    assert summarize_attempt_chain(attempts) is None
    assert summarize_attempt_chain([]) is None


# --------------------------------------------------------------------------
# Whole-route summaries: the generic sentinel must not win
# --------------------------------------------------------------------------


def test_generic_sentinel_yields_to_the_specific_failure():
    """``all_providers_failed`` is a code, not a diagnosis."""
    from app.core.error_messages import explain_route_failure

    attempts = [
        {"provider_name": "groq", "success": False, "error": "rate_limit_exceeded"},
        {"provider_name": "gemini", "success": False, "error": "timeout"},
    ]
    message = explain_route_failure("all_providers_failed", attempts)
    assert message.startswith("rate_limited:")
    assert "groq" in message
    assert "All 2 attempted providers failed" in message


def test_route_summary_accepts_attempt_objects_not_just_dicts():
    """The route handler passes AttemptLog instances; tests pass dicts."""
    from app.core.error_messages import explain_route_failure
    from app.core.executor import AttemptLog

    attempts = [AttemptLog(provider_name="groq", success=False, error="ConnectError")]
    message = explain_route_failure("all_providers_failed", attempts)
    assert message.startswith("network_unreachable:")
    assert "groq" in message


def test_route_summary_never_says_all_providers_failed_alone():
    from app.core.error_messages import explain_route_failure

    message = explain_route_failure("all_providers_failed", [])
    assert message is not None
    assert message != "all_providers_failed"
    assert message.startswith("provider_error:")
