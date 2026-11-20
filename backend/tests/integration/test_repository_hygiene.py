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
    # Owner's decision (C6): the holder is the project, not a personal handle.
    assert "Saboor Nasar" in text
    # A holder line must be present and dated - an empty copyright is not one.
    assert "Copyright (c) 2026 Saboor Nasar" in text
    # The repo slug in clone URLs is unchanged, so it must not have been
    # rewritten to the new holder by accident.
    assert "Saboor Nasar" in _read("README.md")
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

    assert APP_NAME == "Routtener Gateway"
    assert APP_SHORT_NAME == "Routtener"


def test_readme_disclaims_affiliation():
    # Wrapped prose is normal; compare with whitespace collapsed.
    flat = " ".join(_read("README.md").lower().split())
    assert "not affiliated with lmsys routellm" in flat


# --------------------------------------------------------------------------
# Honesty rules for the README
# --------------------------------------------------------------------------


def test_readme_does_not_claim_unmeasured_benchmark_numbers():
    """The repo ships no benchmark numbers at all: the section claims none.

    Stronger than the old placeholder rule: since T3 the results folder is
    empty and the README's Benchmarks section must not contain a single
    percentage — every number in the repo is produced by the owner's own
    run, never pre-written.
    """
    readme = _read("README.md")
    benchmark_section = readme.split("## Benchmarks", 1)[1].split("## ", 1)[0]
    assert "No numbers are claimed" in benchmark_section
    percentages = re.findall(r"\d{1,3}(?:\.\d+)?\s?%", benchmark_section)
    assert not percentages, (
        f"the Benchmarks section presents numbers it cannot have measured: "
        f"{percentages}"
    )


def test_readme_points_the_numbers_at_the_owners_own_run():
    """The README must route benchmark claims to a real, local run."""
    readme = _read("README.md")
    benchmark_section = readme.split("## Benchmarks", 1)[1].split("## ", 1)[0]
    assert "run_benchmark.py" in benchmark_section
    assert "your own" in benchmark_section.lower()
    assert "benchmark/README.md" in benchmark_section


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
    assert "routtener-auto" in readme


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


# ---------------------------------------------------------------------------
# No test may leave junk in the working tree.
#
# OBSERVED: three env-prefix tests in test_rename_compat.py used a data dir of
# "C:/tmp/new-dir" to prove the setting is passed through verbatim. Merely
# constructing Settings() CREATES that directory, and on Linux it is a relative
# path, so the repository grew a literal `backend/C:/tmp/new-dir/routellm.db`.
# A release archive built afterwards contained those files.
# ---------------------------------------------------------------------------

def test_no_windows_shaped_paths_exist_inside_the_backend_tree():
    """A `C:` directory inside the repo is always an accident, never a feature."""
    backend = REPO_ROOT / "backend"
    strays = [
        p for p in backend.rglob("*")
        if p.is_dir() and p.name.startswith(("C:", "D:"))
        and "__pycache__" not in p.parts
    ]
    assert strays == [], (
        "test pollution: these directories were created inside the repository "
        f"and would ship in a release archive: {[str(p) for p in strays]}"
    )


def test_no_stray_sqlite_files_outside_a_data_directory():
    """routellm.db belongs in the data directory, nowhere else in the tree."""
    backend = REPO_ROOT / "backend"
    strays = [
        p for p in backend.rglob("routellm.db*")
        if "__pycache__" not in p.parts and "data" not in p.parts
    ]
    assert strays == [], f"stray database files inside the repo: {strays}"
