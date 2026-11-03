"""Normalize a Postgres DSN into something asyncpg/SQLAlchemy accepts.

Managed providers hand out URLs in a shape that ``asyncpg`` cannot consume
directly. The three real-world offenders:

* ``postgres://user:pass@host/db`` — the Heroku/Render legacy scheme. SQLAlchemy
  refuses it outright ("Can't load plugin: sqlalchemy.dialects:postgres").
* ``?sslmode=require`` — a libpq keyword. asyncpg spells TLS as ``ssl=require``
  and raises ``TypeError: connect() got an unexpected keyword argument
  'sslmode'`` for the libpq name.
* ``?channel_binding=require`` / ``?options=...`` / ``?target_session_attrs=...``
  — libpq-only knobs with no asyncpg equivalent; they must be dropped instead
  of forwarded.

This module is intentionally dependency-free and side-effect-free: it is pure
string/URL surgery so it can be unit-tested without a database, and so that a
malformed URL fails with a clear message instead of a driver traceback.

SECURITY: the password is never included in any raised error or log line. Use
:func:`mask_database_url` when a DSN must be displayed.
"""

from dataclasses import dataclass
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

#: Schemes that mean "Postgres" and whether the driver is already named.
_DRIVER = "postgresql+asyncpg"

#: libpq TLS keywords → accepted values, mapped onto asyncpg's ``ssl``.
_SSLMODE_TO_ASYNC = {
    "disable": "disable",
    "allow": "allow",
    "prefer": "prefer",
    "require": "require",
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}

#: libpq query parameters that asyncpg does not understand. Dropped silently
#: (``channel_binding`` is the Neon default that breaks the most installs).
_UNSUPPORTED_PARAMS = {
    "channel_binding",
    "target_session_attrs",
    "options",
    "gssencmode",
    "krbsrvname",
    "service",
    "passfile",
    "sslcert",
    "sslkey",
    "sslrootcert",
    "sslpassword",
    "sslcrl",
    "sslcrldir",
    "sslcompression",
    "ssl_min_protocol_version",
    "ssl_max_protocol_version",
    "connect_timeout",
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
    "tcp_user_timeout",
    "requiressl",
    "multipleactiveresultsets",
    "load_balance_hosts",
}

#: Provider hint appended to the dialect so SQLAlchemy knows the client.
_ASYNC_DRIVER_NAMES = {"asyncpg"}


class InvalidDatabaseUrl(ValueError):
    """Raised for a DSN that cannot be normalized (message never has secrets)."""


@dataclass(frozen=True)
class NormalizedDatabaseUrl:
    """Result of :func:`normalize_database_url`.

    ``url`` is safe to hand to ``create_async_engine``; ``scheme``/``driver``
    exist so startup logging can describe the connection without echoing it.
    """

    url: str
    scheme: str
    driver: str
    ssl_required: bool
    stripped_params: tuple[str, ...]


def _split_scheme(raw: str) -> tuple[str, str]:
    """Return ``(scheme_prefix, rest)``; raises on anything non-postgres."""
    if "://" not in raw:
        raise InvalidDatabaseUrl(
            "DATABASE_URL is not a URL (expected postgresql://user:pass@host/db)"
        )
    scheme, rest = raw.split("://", 1)
    scheme = scheme.strip().lower()
    if scheme.split("+", 1)[0] not in {"postgres", "postgresql"}:
        raise InvalidDatabaseUrl(
            "DATABASE_URL must start with postgres:// or postgresql:// "
            "(or sqlite+aiosqlite:// for local development) "
            f"(got {scheme.split('+', 1)[0]!r})"
        )
    return scheme, rest


def normalize_database_url(raw: str) -> NormalizedDatabaseUrl:
    """Convert any supported Postgres DSN into a SQLAlchemy asyncpg DSN.

    Accepts ``postgres://``, ``postgresql://`` and ``postgresql+asyncpg://``,
    rewrites ``sslmode=`` to asyncpg's ``ssl=``, and drops libpq-only query
    parameters. The password is preserved verbatim (percent-encoded when it
    contains URL-reserved characters) and never appears in an exception.
    """
    if raw is None or not str(raw).strip():
        raise InvalidDatabaseUrl("DATABASE_URL is empty")

    url = str(raw).strip()
    # A DSN pasted with surrounding quotes (common from dashboards) is a
    # frequent copy/paste failure — strip one matching pair.
    if len(url) >= 2 and url[0] == url[-1] and url[0] in "\"'":
        url = url[1:-1].strip()

    # Local development convenience: an async SQLite DSN is passed through
    # untouched (no libpq concepts apply). Requires aiosqlite, which lives in
    # requirements-dev.txt — deployments use Postgres.
    if url.lower().startswith(("sqlite+aiosqlite://", "sqlite:///")) and "aiosqlite" in url:
        return NormalizedDatabaseUrl(
            url=url,
            scheme="sqlite+aiosqlite",
            driver="aiosqlite",
            ssl_required=False,
            stripped_params=(),
        )

    base_scheme, _ = _split_scheme(url)
    if base_scheme.startswith("sqlite"):
        # Local dev only (aiosqlite). No libpq parameters apply.
        return NormalizedDatabaseUrl(
            url=url,
            scheme=base_scheme,
            driver="aiosqlite",
            ssl_required=False,
            stripped_params=(),
        )
    # Normalize the scheme but keep any explicitly named async driver.
    driver = "asyncpg"
    if "+" in base_scheme:
        _, driver = base_scheme.split("+", 1)
    if driver not in _ASYNC_DRIVER_NAMES:
        raise InvalidDatabaseUrl(
            "DATABASE_URL must not request the synchronous driver "
            f"({driver!r}); this app only speaks asyncpg"
        )

    parts = urlsplit(f"{_DRIVER}://{url.split('://', 1)[1]}")

    # urlsplit keeps the credentials; re-encode the password so characters
    # like '@' or '/' inside it cannot break the next parse.
    username = parts.username
    password = parts.password
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if username is not None:
        # unquote() first: urlsplit() leaves percent-escapes intact, so
        # quoting twice would turn `p%40ss` into `p%2540ss`.
        creds = quote(unquote(username), safe="")
        if password is not None:
            creds = f"{creds}:{quote(unquote(password), safe='')}"
        netloc = f"{creds}@{netloc}"

    # --- query rewriting -------------------------------------------------
    kept: list[tuple[str, str]] = []
    stripped: list[str] = []
    ssl_required = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered == "sslmode":
            mapped = _SSLMODE_TO_ASYNC.get(value.lower())
            if mapped is None:
                raise InvalidDatabaseUrl(
                    f"DATABASE_URL has an unrecognised sslmode={value!r}"
                )
            # asyncpg accepts `ssl=require`/`ssl=disable`/... keywords.
            kept.append(("ssl", mapped))
            ssl_required = mapped != "disable"
        elif lowered == "ssl":
            kept.append(("ssl", value))
            ssl_required = value.lower() not in {"0", "false", "disable"}
        elif lowered in _UNSUPPORTED_PARAMS:
            stripped.append(key)
        else:
            kept.append((key, value))

    query = urlencode(kept)
    normalized = urlunsplit(
        (_DRIVER, netloc, parts.path or "", query, parts.fragment)
    )
    # `ssl=require` in the query string is what makes asyncpg enforce TLS;
    # ssl_required only mirrors it for startup logging/health messaging.
    return NormalizedDatabaseUrl(
        url=normalized,
        scheme="postgresql+asyncpg",
        driver="asyncpg",
        ssl_required=ssl_required,
        stripped_params=tuple(stripped),
    )


def mask_database_url(raw: str) -> str:
    """Return a log-safe rendering: ``postgresql+asyncpg://user:****@host/db``.

    Never raises — a DSN we cannot parse is returned as ``<unparseable>``
    rather than echoed, because echoing is exactly how passwords leak.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    # Fail closed: anything that is not a recognisable postgres DSN is
    # replaced wholesale. Reconstructing a "best effort" string from arbitrary
    # input is how a password ends up in a log (e.g. "::::secret" parses as a
    # path, not credentials).
    lowered = text.lower()
    if lowered.startswith(("sqlite+aiosqlite://", "sqlite:///")):
        # No credentials in a SQLite DSN, but the path is noise in a log.
        return lowered.split("://", 1)[0] + "://<local-file>"
    if "://" not in text or not lowered.startswith("postgres"):
        return "<unparseable>"
    try:
        parts = urlsplit(text)
        if not parts.hostname:
            return "<unparseable>"
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        user = parts.username
        creds = f"{quote(user, safe='')}:****@" if user else ""
        path = parts.path or ""
        # Query strings are dropped entirely: sslmode=... is harmless, but a
        # `password=`-style custom parameter is not worth the risk.
        return f"{parts.scheme or 'postgresql'}://{creds}{host}{path}"
    except Exception:
        return "<unparseable>"


def describe_redis_url(raw: str) -> str:
    """Return a log-safe summary of a Redis DSN (host + TLS scheme only)."""
    if not raw:
        return "not configured"
    try:
        parts = urlsplit(str(raw).strip())
        scheme = parts.scheme.lower() or "redis"
        host = parts.hostname or "?"
        if parts.port:
            host = f"{host}:{parts.port}"
        tls = " (TLS)" if scheme == "rediss" else ""
        return f"{scheme}://{host}{tls}"
    except Exception:
        return "<unparseable>"
