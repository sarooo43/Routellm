"""Packaging contracts: Dockerfile, compose file and the release workflow.

These files are not exercised by pytest directly, so their load-bearing
assumptions are asserted here instead — the alternative is discovering them
on a user's first ``docker compose up``.

Nothing in this module builds an image or runs Docker.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Dockerfile
# --------------------------------------------------------------------------


def test_dockerfile_builds_the_dashboard_and_ships_it():
    """The image serves UI and API from one process, so dist must be inside."""
    text = _read(DOCKERFILE)
    assert "npm run build" in text
    assert "COPY --from=frontend /app/dist /app/frontend/dist" in text


def test_dockerfile_uses_the_repo_root_as_build_context():
    """Dockerfile lives in backend/ but needs frontend/; compose must say so."""
    compose = _read(COMPOSE)
    assert "context: ." in compose
    assert "dockerfile: backend/Dockerfile" in compose


def test_dockerfile_keeps_state_in_a_volume_and_drops_privileges():
    text = _read(DOCKERFILE)
    assert "ROUTELLM_DATA_DIR=/data" in text
    assert "VOLUME [\"/data\"]" in text
    assert "USER appuser" in text
    assert "chown -R appuser:appuser /data" in text


def test_default_runtime_needs_no_postgres_or_redis():
    """A container with no extra services must still be healthy."""
    text = _read(DOCKERFILE)
    assert "DATABASE_URL" not in text.replace("DATABASE_URL:", "")
    assert "REDIS_URL" not in text
    assert "/health" in text  # the healthcheck hits the always-200 endpoint


def test_dockerfile_does_not_bake_secrets():
    text = _read(DOCKERFILE)
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                 "PROVIDER_KEY_ENCRYPTION_KEY"):
        assert f"{name}=" not in text


# --------------------------------------------------------------------------
# docker-compose.yml
# --------------------------------------------------------------------------


def test_compose_is_a_single_service_on_loopback():
    text = _read(COMPOSE)
    services_block = text.split("\nvolumes:", 1)[0]
    assert re.search(r"^services:\s*$", services_block, re.MULTILINE)
    service_names = re.findall(
        r"^  ([a-z][a-z0-9_-]*):\s*$", services_block, re.MULTILINE
    )
    assert service_names == ["routellm"], service_names
    assert '"127.0.0.1:8000:8000"' in text
    # Opting into Postgres/Redis is documented in comments (backend URLs, not
    # services); no such service may be defined.
    for absent in ("image: postgres", "image: redis", "image: caddy"):
        assert absent not in text, absent
    assert not re.findall(r"^  (postgres|redis|caddy):", text, re.MULTILINE)


def test_compose_persists_the_data_volume():
    text = _read(COMPOSE)
    assert "routellm-data:/data" in text
    assert re.search(r"^volumes:\s*\n  routellm-data:", text, re.MULTILINE)


def test_compose_healthcheck_targets_health_not_api_health():
    """`/health` always answers 200; a 503-on-degraded path would flap."""
    text = _read(COMPOSE)
    assert "127.0.0.1:8000/health" in text
    assert "/api/v1/health" not in text


# --------------------------------------------------------------------------
# .dockerignore
# --------------------------------------------------------------------------


def test_dockerignore_excludes_state_and_secrets_but_keeps_the_example():
    text = _read(DOCKERIGNORE)
    for excluded in ("data", "backend/data", ".env", "frontend/node_modules"):
        assert excluded in text, excluded
    assert "!backend/.env.example" in text


# --------------------------------------------------------------------------
# Release workflow
# --------------------------------------------------------------------------


def test_release_workflow_triggers_on_version_tags():
    text = _read(RELEASE_WORKFLOW)
    assert '"v*.*.*"' in text
    assert "contents: write" in text


def test_release_archive_contains_a_prebuilt_dashboard():
    """No Node.js on the user's machine: dist must be in the zip."""
    text = _read(RELEASE_WORKFLOW)
    assert 'cp -r frontend/dist "dist-release/${STAGE}/frontend/dist"' in text


def test_release_archive_excludes_tests_and_local_state():
    text = _read(RELEASE_WORKFLOW)
    assert "--exclude 'tests'" in text
    assert "--exclude 'data'" in text
    assert "--exclude '.env'" in text


def test_release_smoke_tests_the_archive_before_publishing():
    text = _read(RELEASE_WORKFLOW)
    assert "Smoke-test the archive" in text
    assert "/health" in text
    assert text.index("Smoke-test the archive") < text.index("Attach the archive")


def test_release_keeps_the_ci_node_pin():
    """The owner pinned Node for CI reproducibility; the release must match."""
    text = _read(RELEASE_WORKFLOW)
    assert 'node-version: "24.22.2"' in text


@pytest.mark.parametrize(
    "path",
    ["run.py", "start.sh", "start.ps1", "README.md", "LICENSE"],
)
def test_release_copies_the_documented_entrypoints(path):
    text = _read(RELEASE_WORKFLOW)
    assert path in text
    assert (REPO_ROOT / path).exists(), f"{path} is listed but missing"
