"""Whether a provider API key can legally travel in an HTTP header.

Why this module exists (OBSERVED, reproduced against a real uvicorn server):
a saved provider key containing a newline made every chat call fail *locally*,
before any byte reached the provider:

    LocalProtocolError: Illegal header value b'Bearer gsk_...\\n...'

httpx/h11 refuse to serialise a header value containing CR, LF or a NUL, and
refuse to UTF-8 encode a non-ASCII value. The provider then never sees the
request, so no status code comes back - and the router reported it as
``network_error`` ("the request failed before a response"), which points the
owner at their internet connection instead of at the key they just pasted.

RETRACTED EXPLANATION (C1, corrected): an earlier version of this note claimed
the two paths disagreed because the model-list call went through
requests/urllib3, which trims a header value before serialising it, while the
chat call went through httpx/h11, which raises. That is WRONG. Both paths use
httpx and build the header the same way, so no stored value can make one send
and the other fail. Measured: for every control character, both paths raise
identical errors.

What IS true, and is what makes a saved-key problem unavoidable to diagnose:
discovery OMITS the Authorization header entirely when the key is falsy
(``_auth_headers`` only adds it ``if api_key``), while the chat adapters build
``Bearer {key}`` unconditionally. So an empty stored value yields a request
with no credential (a provider answers 401) on one path and
``Authorization: Bearer `` on the other. Neither is a LocalProtocolError, but
both are silent misbehaviour of the same kind: the app sends a credential it
never validated.

The owner's specific split (401 on the list call, LocalProtocolError on chat,
same key) is therefore NOT explained by this repo. It stays UNEXPLAINED rather
than being papered over with a hypothesis that the code contradicts.

So the rule is stated once, here, and enforced in two places:
  * on SAVE - reject the key with a clear 4xx that never echoes it;
  * at REQUEST time - a key stored before this check existed must not be able
    to crash the request; it produces a truthful ``invalid_key_format`` error.

The message never contains the key, only its length and the offset of the
first offending character.
"""

from __future__ import annotations

#: Characters that may never appear inside a header value. CR and LF split the
#: header (request smuggling); NUL and other C0 controls are illegal on the
#: wire; DEL is illegal too. Tab is technically legal inside a value but no
#: real provider key contains one, and pasting one is always a mistake.
_FORBIDDEN_CONTROLS = frozenset(
    [chr(c) for c in range(0x00, 0x20) if c != 0x09] + [chr(0x7F)]
)


class ProviderKeyFormatError(ValueError):
    """A provider key that cannot be sent in an HTTP header.

    ``reason`` is safe to show the user: it names the character class and the
    position, never the key.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def describe_unusable(value: str) -> str | None:
    """Why ``value`` cannot be sent, or ``None`` when it is fine.

    Whitespace at the EDGES is not an error - it is trimmed on save and again
    on use, because copying a key out of a dashboard very often catches a
    trailing space. Only what survives trimming can break a request.
    """
    if any(ch in _FORBIDDEN_CONTROLS for ch in value):
        for index, ch in enumerate(value):
            if ch in _FORBIDDEN_CONTROLS:
                name = "newline (\\n)" if ch == "\n" else (
                    "carriage return (\\r)" if ch == "\r" else f"control character U+{ord(ch):04X}"
                )
                return (
                    f"contains a {name} at position {index + 1}; "
                    "a header value cannot contain control characters"
                )
    for index, ch in enumerate(value):
        if ord(ch) > 0x7E:
            return (
                f"contains a non-ASCII character (U+{ord(ch):04X}) at position "
                f"{index + 1}; header values must be ASCII, so this usually "
                "means the key was copied with smart quotes or an invisible "
                "character"
            )
    if value != value.strip():
        return (
            "has whitespace at the start or end; it is trimmed automatically, "
            "so this alone is not an error"
        )
    return None


def normalize(value: str | None) -> str:
    """The form of a key that is safe to store and send: edges trimmed.

    Raises ``ProviderKeyFormatError`` when the interior cannot be sent.
    """
    trimmed = (value or "").strip()
    if not trimmed:
        raise ProviderKeyFormatError("is empty")
    reason = describe_unusable(trimmed)
    if reason is not None and "whitespace at the start or end" not in reason:
        raise ProviderKeyFormatError(reason)
    return trimmed


def is_sendable(value: str) -> bool:
    """True when the value can be placed in an ``Authorization`` header as-is."""
    reason = describe_unusable(value)
    return reason is None or "whitespace at the start or end" in reason
