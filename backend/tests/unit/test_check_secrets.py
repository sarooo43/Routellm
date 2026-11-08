"""The secret scanner must catch real shapes and stay quiet about noise.

Two failure modes matter equally:

* a **false negative** — the scanner misses a credential that lands in the
  repo, which is the whole point of the tool;
* a **false positive** — it fails on test fixtures and documentation
  placeholders, so people disable it, which produces false negatives.

Both are tested here, and every finding is checked to be masked: printing a
leaked key into a CI log copies the leak.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_secrets.py"


@pytest.fixture(scope="module")
def scanner():
    import sys

    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolves annotations through sys.modules, so the module must
    # be registered before it is executed.
    sys.modules["check_secrets"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Positive controls: these MUST be reported.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample,expected_pattern",
    [
        ("hf_" + "Ab3dEf9hIj2Lm5Np8Qr1St4Uv7Xy0Za3Bc6", "huggingface-token"),
        ("sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "openai-style-key"),
        ("gsk_" + "k" * 48, "groq-key"),
        ("AIza" + "b" * 35, "google-api-key"),
        ("postgresql://neon_user:" + "s3cr3tPassw0rd" + "@ep-x.neon.tech/db",
         "database-password-url"),
        ("rediss://default:" + "upstashSecret99" + "@host:6379", "redis-password-url"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private-key-block"),
        ("AKIA" + "Q" * 16, "aws-access-key-id"),
        ("ghp_" + "z" * 40, "github-token"),
    ],
)
def test_real_shapes_are_reported(scanner, sample, expected_pattern):
    findings = scanner.scan_text(f"const value = '{sample}'", "sample.py")
    assert [f.pattern.name for f in findings] == [expected_pattern]


def test_findings_are_always_masked(scanner):
    secret = "gsk_" + "Z" * 48
    findings = scanner.scan_text(secret, "leak.py")
    assert findings
    rendered = "\n".join(f.render(strict=True) for f in findings)
    assert secret not in rendered
    assert "gsk_…" in rendered[:40] or findings[0].masked.startswith("gsk_")
    assert findings[0].masked.endswith("ZZZZ")


def test_mask_of_a_short_value_reveals_nothing(scanner):
    assert scanner.mask("abc") == "****"
    assert scanner.mask("abcdefgh") == "****"


# --------------------------------------------------------------------------
# Negative controls: these must stay quiet.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        # Documentation placeholders.
        "DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DB",
        "postgresql+asyncpg://user:password@localhost:5432/routellm",
        "#   REDIS_URL=rediss://default:<your-password>@HOST:6379",
        "rediss://default:PASSWORD@host:6379",
        # Test fixtures used by this repo.
        "gsk_test_1234567890abcdef",
        "gsk_test_failclosed_123",
        "AIzaSyDUMMY_failclosed",
        "rk_not_a_real_key",
        "rk_stale_key_from_another_run",
        "sk-live-value",
        # Ordinary prose and code.
        "the token is stored as a SHA-256 hash",
        "REDIS_URL=rediss://host:6379",
        "password = os.environ['DB_PASSWORD']",
    ],
)
def test_placeholders_and_prose_are_not_reported(scanner, sample):
    assert scanner.scan_text(sample, "docs.md") == []


def test_commented_out_examples_are_never_findings(scanner):
    sample = "# DATABASE_URL=postgresql://admin:Hunter2Hunter2@db.internal:5432/prod"
    assert scanner.scan_text(sample, "README.md") == []


def test_the_same_line_uncommented_is_a_finding(scanner):
    sample = "DATABASE_URL=postgresql://admin:Hunter2Hunter2@db.internal:5432/prod"
    assert [f.pattern.name for f in scanner.scan_text(sample, ".env")] == [
        "database-password-url"
    ]


# --------------------------------------------------------------------------
# Scanning the repository itself.
# --------------------------------------------------------------------------


def test_the_repository_has_no_credentials_in_the_tree(scanner):
    findings = scanner.scan_tree(scanner.REPO_ROOT)
    rendered = "\n".join(f.render(strict=True) for f in findings)
    assert not findings, f"credential-shaped strings in the working tree:\n{rendered}"


def test_history_scan_is_clean_or_reports_nothing_actionable(scanner):
    """History is checked wholesale: a key removed later is still public."""
    findings = scanner.dedupe(scanner.scan_history())
    rendered = "\n".join(f.render(strict=True) for f in findings)
    assert not findings, f"credential-shaped strings in git history:\n{rendered}"


def test_test_paths_are_skipped_by_design(scanner):
    """Fixtures live in tests/; a committed .env is caught by the history scan."""
    assert "tests" in scanner.SKIP_PATH_PARTS
    assert ".env" in scanner.SKIP_FILENAMES


def test_gitleaks_config_exists_and_allowlists_tests():
    config = (SCRIPT.parents[2] / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "useDefault = true" in config
    assert "backend/tests" in config
    # The example env file is documentation, not a credential store.
    assert r"\.env\.example" in config
