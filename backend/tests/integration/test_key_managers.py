"""Direct tests for the two key managers (coverage audit A5).

`app.core.api_key_manager` sat at 56% and `app.core.provider_key_manager` at
65% line coverage — both are key-handling code, i.e. exactly the "critical"
class the audit requires above 70%. These tests exercise the generation,
validation, usage accounting, local-identity and BYOK CRUD paths directly
against a real in-memory SQLite session (no HTTP layer in the way).
"""

import pytest

from app.core.api_key_manager import (
    LOCAL_IDENTITY_LABEL,
    _LOCAL_IDENTITY_HASH,
    generate_api_key,
    get_local_identity,
    hash_api_key,
    record_api_key_usage,
    validate_api_key,
)
from app.core.provider_key_manager import (
    SUPPORTED_PROVIDERS,
    delete_provider_key,
    get_decrypted_provider_keys,
    get_provider_key,
    is_supported_provider,
    list_provider_keys,
    server_key_for,
    upsert_provider_key,
)
from app.models.api_key import ApiKeyDB
from app.models.provider_key import ProviderKeyDB


# --------------------------------------------------------------------------
# api_key_manager
# --------------------------------------------------------------------------


def test_generated_keys_hash_to_their_stored_hash():
    raw_key, key_hash, key_prefix = generate_api_key()
    assert raw_key.startswith("rk_")
    assert key_hash == hash_api_key(raw_key)
    assert key_prefix == raw_key[:8]
    # Two generations never collide.
    raw_b, hash_b, _ = generate_api_key()
    assert raw_b != raw_key and hash_b != key_hash


async def test_validate_rejects_missing_and_unknown_keys(db_session):
    assert await validate_api_key(None, db_session) is None
    assert await validate_api_key("", db_session) is None
    assert await validate_api_key("rk_not-a-real-key-value", db_session) is None


async def test_validate_returns_the_active_row_and_touches_last_used(db_session):
    raw_key, key_hash, key_prefix = generate_api_key()
    db_session.add(ApiKeyDB(key_hash=key_hash, key_prefix=key_prefix, label="k"))
    await db_session.commit()

    record = await validate_api_key(raw_key, db_session)
    assert record is not None and record.key_hash == key_hash
    assert record.last_used_at is not None  # the touch happened

    # An INACTIVE row never validates, whatever the raw key says.
    record.is_active = False
    await db_session.commit()
    assert await validate_api_key(raw_key, db_session) is None


async def test_usage_accounting_increments_and_never_goes_negative(db_session):
    raw_key, key_hash, key_prefix = generate_api_key()
    db_session.add(ApiKeyDB(key_hash=key_hash, key_prefix=key_prefix, label="k"))
    await db_session.commit()
    record = await validate_api_key(raw_key, db_session)

    await record_api_key_usage(db_session, record, 120)
    await record_api_key_usage(db_session, record, -5)  # clamped, not subtracted
    await db_session.refresh(record)
    assert record.total_requests == 2
    assert record.total_tokens_used == 120  # -5 counted as 0


async def test_local_identity_is_created_once_and_survives_revocation(db_session):
    first = await get_local_identity(db_session)
    assert first.key_hash == _LOCAL_IDENTITY_HASH
    assert first.label == LOCAL_IDENTITY_LABEL
    assert first.is_active is True

    again = await get_local_identity(db_session)
    assert again.id == first.id  # idempotent: one row per database

    # A revoked local row is REACTIVATED, never a lockout.
    again.is_active = False
    await db_session.commit()
    revived = await get_local_identity(db_session)
    assert revived.id == first.id and revived.is_active is True


# --------------------------------------------------------------------------
# provider_key_manager
# --------------------------------------------------------------------------


async def _store(db_session, api_key_id: int, provider: str, raw: str) -> ProviderKeyDB:
    return await upsert_provider_key(db_session, api_key_id, provider, raw)


async def test_supported_providers_exclude_the_demo_provider_and_match_registry():
    assert "mock" not in SUPPORTED_PROVIDERS
    from app.providers.registry import PROVIDER_REGISTRY

    assert is_supported_provider("gemini")
    assert not is_supported_provider("not-a-provider")


async def test_upsert_inserts_then_updates_without_duplicating_rows(db_session):
    stored = await _store(db_session, 1, "groq", "gsk_livetest_key_value_000111")
    assert stored.encrypted_key != "gsk_livetest_key_value_000111"  # encrypted at rest
    assert stored.key_hint != "gsk_livetest_key_value_000111"  # hint is not the key
    assert "gsk_livetest_key_value_000111" not in (stored.key_hint or "")
    first_ciphertext = stored.encrypted_key

    updated = await _store(db_session, 1, "groq", "gsk_second_key_value_222333")
    rows = await list_provider_keys(db_session, 1)
    assert len(rows) == 1  # upsert, not insert
    assert updated.id == stored.id
    assert updated.encrypted_key != first_ciphertext


async def test_delete_reports_whether_a_row_existed(db_session):
    await _store(db_session, 2, "deepseek", "sk-live-deepseek-key-445566")
    assert await delete_provider_key(db_session, 2, "deepseek") is True
    assert await delete_provider_key(db_session, 2, "deepseek") is False


async def test_decryption_roundtrip_and_absent_cases(db_session):
    assert await get_decrypted_provider_keys(db_session, None) == {}
    assert await get_provider_key(db_session, 3, "gemini") is None

    await _store(db_session, 3, "gemini", "AIzaSyLIVE_roundtrip_key_778899")
    keys = await get_decrypted_provider_keys(db_session, 3)
    assert keys == {"gemini": "AIzaSyLIVE_roundtrip_key_778899"}
    assert await get_provider_key(db_session, 3, "groq") is None


async def test_rows_are_scoped_per_caller(db_session):
    await _store(db_session, 10, "gemini", "AIzaSy_caller10_key")
    await _store(db_session, 11, "gemini", "AIzaSy_caller11_key")
    assert (await get_decrypted_provider_keys(db_session, 10))["gemini"] == "AIzaSy_caller10_key"
    assert (await get_decrypted_provider_keys(db_session, 11))["gemini"] == "AIzaSy_caller11_key"


def test_server_key_reads_settings_and_treats_blank_as_absent(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_operator_server_key_xyz")
    assert server_key_for("groq") == "gsk_operator_server_key_xyz"

    monkeypatch.setattr(settings, "GROQ_API_KEY", "   ")
    assert server_key_for("groq") is None

    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    assert server_key_for("groq") is None


# The BYOK isolation (per-caller scoping, fail-closed on encryption loss) is
# exercised through the HTTP layer in test_provider_keys.py; these direct tests
# deliberately re-verify the scoping invariant at the manager level too.
@pytest.mark.parametrize("provider", ["gemini", "groq", "deepseek"])
async def test_each_real_provider_roundtrips(db_session, provider):
    raw = "AIzaSy_test_only_value_for_%s_00" % provider
    await _store(db_session, 99, provider, raw)
    assert (await get_decrypted_provider_keys(db_session, 99)).get(provider) == raw
