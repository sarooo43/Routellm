"""Unit tests for DATABASE_URL / REDIS_URL normalization.

Every case uses a fake host and a throwaway password; none of these tests
touch a network or a real credential.
"""

import pytest

from app.db.url import (
    InvalidDatabaseUrl,
    describe_redis_url,
    mask_database_url,
    normalize_database_url,
)

SECRET_PW = "sup3r-s3cret-pw"


# --------------------------------------------------------------------------
# Scheme handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"postgres://u:{SECRET_PW}@db.example.com/db",
        f"postgresql://u:{SECRET_PW}@db.example.com/db",
        f"postgresql+asyncpg://u:{SECRET_PW}@db.example.com/db",
    ],
)
def test_all_three_postgres_schemes_become_asyncpg(raw):
    result = normalize_database_url(raw)
    assert result.url == f"postgresql+asyncpg://u:{SECRET_PW}@db.example.com/db"
    assert result.scheme == "postgresql+asyncpg"
    assert result.driver == "asyncpg"


def test_non_postgres_scheme_is_rejected():
    with pytest.raises(InvalidDatabaseUrl, match="postgres://"):
        normalize_database_url("mysql://u:p@host/db")


def test_garbage_input_is_rejected():
    with pytest.raises(InvalidDatabaseUrl):
        normalize_database_url("not-a-url")


def test_empty_input_is_rejected():
    with pytest.raises(InvalidDatabaseUrl):
        normalize_database_url("")


def test_synchronous_driver_is_rejected():
    with pytest.raises(InvalidDatabaseUrl, match="asyncpg"):
        normalize_database_url("postgresql+psycopg2://u:p@host/db")


def test_surrounding_quotes_are_stripped():
    """Dashboard copy/paste often includes the quotes."""
    result = normalize_database_url(f'"{SECRET_PW and "postgresql://u:p@host/db"}"')
    assert result.url == "postgresql+asyncpg://u:p@host/db"


# --------------------------------------------------------------------------
# sslmode → ssl
# --------------------------------------------------------------------------


def test_sslmode_require_becomes_ssl_require():
    result = normalize_database_url("postgresql://u:p@host/db?sslmode=require")
    assert result.url == "postgresql+asyncpg://u:p@host/db?ssl=require"
    assert result.ssl_required is True


def test_sslmode_disable_means_no_tls():
    result = normalize_database_url("postgresql://u:p@host/db?sslmode=disable")
    assert result.url == "postgresql+asyncpg://u:p@host/db?ssl=disable"
    assert result.ssl_required is False


@pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
def test_sslmode_verify_variants_are_preserved(mode):
    result = normalize_database_url(f"postgresql://u:p@host/db?sslmode={mode}")
    assert result.url.endswith(f"ssl={mode}")


def test_unknown_sslmode_is_a_clear_error():
    with pytest.raises(InvalidDatabaseUrl, match="sslmode"):
        normalize_database_url("postgresql://u:p@host/db?sslmode=banana")


def test_native_ssl_parameter_is_accepted():
    result = normalize_database_url("postgresql://u:p@host/db?ssl=require")
    assert result.url == "postgresql+asyncpg://u:p@host/db?ssl=require"
    assert result.ssl_required is True


# --------------------------------------------------------------------------
# Unsupported libpq parameters
# --------------------------------------------------------------------------


def test_channel_binding_is_stripped():
    """Neon appends this by default; asyncpg rejects it."""
    result = normalize_database_url(
        "postgresql://u:p@ep-x-pooler.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    assert result.url == "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/neondb?ssl=require"
    assert result.stripped_params == ("channel_binding",)
    assert "channel_binding" not in result.url


def test_multiple_libpq_params_are_stripped_and_reported():
    result = normalize_database_url(
        "postgresql://u:p@host/db"
        "?sslmode=require&channel_binding=require&options=-csearch_path%3Dpublic"
        "&target_session_attrs=read-write"
    )
    assert "ssl=require" in result.url
    assert set(result.stripped_params) == {
        "channel_binding",
        "options",
        "target_session_attrs",
    }


def test_unknown_query_params_are_forwarded_untouched():
    result = normalize_database_url("postgresql://u:p@host/db?application_name=routellm")
    assert "application_name=routellm" in result.url


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_percent_encoded_password_is_not_double_encoded():
    result = normalize_database_url("postgresql://u:p%40ss@host/db")
    assert result.url == "postgresql+asyncpg://u:p%40ss@host/db"


def test_reserved_characters_in_password_are_encoded():
    """A raw '@' in the password must not corrupt the host portion."""
    result = normalize_database_url("postgresql://u:pa@ss@host/db")
    assert result.url == "postgresql+asyncpg://u:pa%40ss@host/db"


def test_port_is_preserved():
    result = normalize_database_url("postgresql://u:p@host:6543/db")
    assert result.url == "postgresql+asyncpg://u:p@host:6543/db"


def test_error_messages_never_contain_the_password():
    with pytest.raises(InvalidDatabaseUrl) as excinfo:
        normalize_database_url(
            f"mysql://u:{SECRET_PW}@host/db"
        )
    assert SECRET_PW not in str(excinfo.value)


# --------------------------------------------------------------------------
# Masking helpers used in logs
# --------------------------------------------------------------------------


def test_mask_database_url_hides_password():
    masked = mask_database_url(
        f"postgresql+asyncpg://neon_user:{SECRET_PW}@ep-x.neon.tech/neondb"
    )
    assert masked == "postgresql+asyncpg://neon_user:****@ep-x.neon.tech/neondb"
    assert SECRET_PW not in masked


def test_mask_database_url_keeps_port():
    masked = mask_database_url(f"postgresql://u:{SECRET_PW}@host:5432/db")
    assert masked == "postgresql://u:****@host:5432/db"


def test_mask_database_url_handles_junk_without_echoing_it():
    masked = mask_database_url(f"::::{SECRET_PW}")
    assert SECRET_PW not in masked


def test_mask_database_url_of_empty_string():
    assert mask_database_url("") == ""


def test_describe_redis_url_marks_tls_and_hides_password():
    described = describe_redis_url(
        f"rediss://default:{SECRET_PW}@example-pelican.upstash.io:6379"
    )
    assert described == "rediss://example-pelican.upstash.io:6379 (TLS)"
    assert SECRET_PW not in described


def test_describe_redis_url_plain():
    described = describe_redis_url(f"redis://default:{SECRET_PW}@localhost:6379")
    assert described == "redis://localhost:6379"
    assert "TLS" not in described


def test_describe_redis_url_unset():
    assert describe_redis_url("") == "not configured"
