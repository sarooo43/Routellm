"""Repository hygiene: the files a visitor checks before trusting a project.

Licence, contributor docs, templates, changelog — and, more importantly, the
*claims* the README makes. A portfolio repository loses credibility faster
from a fabricated benchmark table than from a missing badge, so the honesty
rules are asserted here too.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"missing {relative}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Required files
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "LICENSE",
        "README.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "CHANGELOG.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        "docs/deploy/README.md",
        "docs/assets/README.md",
        ".pre-commit-config.yaml",
        ".gitleaks.toml",
    ],
)
def test_required_file_exists(relative):
    _read(relative)


def test_licence_is_mit_for_the_declared_holder():
    text = _read("LICENSE")
    assert text.startswith("MIT License")
    assert "sarooo43" in text
    # The previous licence must be gone, not merely supplemented.
    assert "Boost Software License" not in text


def test_readme_badge_matches_the_licence():
    readme = _read("README.md")
    assert "licence-MIT" in readme
    assert "boost" not in readme.lower()


def test_changelog_starts_at_the_first_release():
    text = _read("CHANGELOG.md")
    assert text.index("[Unreleased]") < text.index("## [0.1.0]")
    assert re.search(r"^## \[0\.1\.0\]", text, re.MULTILINE)


# --------------------------------------------------------------------------
# Name collision handled once, and consistently
# --------------------------------------------------------------------------


def test_upstream_disclaimer_is_stated_in_both_languages():
    for relative in ("backend/app/identity.py", "frontend/src/appInfo.ts"):
        text = _read(relative)
        assert "LMSYS" in text, relative


def test_app_name_constant_is_the_declared_one():
    from app.identity import APP_NAME, APP_SHORT_NAME

    assert APP_NAME == "RouteLLM Gateway"
    assert APP_SHORT_NAME == "RouteLLM"


def test_readme_disclaims_affiliation():
    # Wrapped prose is normal; compare with whitespace collapsed.
    flat = " ".join(_read("README.md").lower().split())
    assert "not affiliated with lmsys routellm" in flat


# --------------------------------------------------------------------------
# Honesty rules for the README
# --------------------------------------------------------------------------


def test_readme_does_not_claim_unmeasured_benchmark_numbers():
    """The benchmark table is a placeholder; no percentage may be presented."""
    readme = _read("README.md")
    benchmark_section = readme.split("## Benchmarks", 1)[1].split("## ", 1)[0]
    assert "Placeholder" in benchmark_section
    # A number is allowed only when it is being disowned, never presented as a
    # result: every mention of the 0% artefact must sit next to the caveat.
    for match in re.finditer(r"\d{1,3}(?:\.\d+)?\s?%", benchmark_section):
        window = benchmark_section[max(0, match.start() - 400): match.end() + 400]
        assert "must not be quoted" in window or "without" in window.lower(), (
            f"benchmark number without its caveat: {match.group(0)!r}"
        )


def test_readme_marks_the_committed_benchmark_report_as_unusable():
    """The committed REPORT.md is a zero-credential run; never quote it."""
    readme = _read("README.md")
    assert "must not be quoted" in readme


def test_demo_assets_are_not_a_broken_image():
    readme = _read("README.md")
    gif_refs = re.findall(r"!\[[^\]]*\]\((docs/assets/demo\.gif)\)", readme)
    if gif_refs:
        assert (REPO_ROOT / "docs/assets/demo.gif").is_file(), (
            "README embeds a demo GIF that does not exist"
        )


def test_quick_start_is_the_first_command_block():
    """A new reader must not scroll to find how to start it."""
    readme = _read("README.md")
    first_block = re.search(r"```bash\n(.*?)```", readme, re.DOTALL)
    assert first_block is not None
    assert "git clone" in first_block.group(1)
    assert "python run.py" in first_block.group(1)


def test_readme_documents_the_openai_escape_hatch():
    readme = _read("README.md")
    assert "base_url=\"http://localhost:8000/v1\"" in readme
    assert "routellm-auto" in readme


# --------------------------------------------------------------------------
# Docs relocation
# --------------------------------------------------------------------------


def test_cloud_docs_live_under_docs_deploy():
    assert (REPO_ROOT / "docs/deploy").is_dir()
    for moved in (
        "CLOUDFLARE.md",
        "DEPLOY_WALKTHROUGH.md",
        "HF_BUILD_TROUBLESHOOTING.md",
        "HTTPS_AND_WEBHOOKS.md",
        "VERIFICATION.md",
        "HUGGINGFACE_AND_MANAGED.md",
    ):
        assert (REPO_ROOT / "docs/deploy" / moved).is_file(), moved


def test_no_dangling_references_to_the_old_docs_paths():
    # Joins keep this file from matching its own search.
    stale = (f"docs/{'CLOUDFLARE'}.md", f"docs/{'DEPLOY_WALKTHROUGH'}.md",
             f"docs/{'HF_BUILD_TROUBLESHOOTING'}.md", f"docs/{'HTTPS_AND_WEBHOOKS'}.md",
             f"docs/{'VERIFICATION'}.md")
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".py", ".yml", ".txt", ".json", ".ts", ".tsx"}:
            continue
        if any(part in {".git", "node_modules", ".venv", "dist"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for reference in stale:
            if reference in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {reference}")
    assert not offenders, "old doc paths still referenced:\n" + "\n".join(offenders)


def test_main_readme_does_not_send_users_to_the_cloud_path_first():
    readme = _read("README.md")
    cloud_index = readme.find("docs/deploy/")
    assert cloud_index == -1 or "experimental" in readme.lower()
