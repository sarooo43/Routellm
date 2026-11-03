"""The Space's build installs a different closure than `pip install gradio`.

Hugging Face (Gradio SDK) runs, in effect:

    pip install -r requirements.txt \
        "torch<=2.13.0" \
        "gradio[oauth,mcp]==<sdk_version>" \
        "uvicorn>=0.14.0" "websockets>=10.4" \
        "spaces==x.y.z"

Note the **extras**: `oauth` and `mcp`. Gradio's `mcp` extra carries its own
pydantic ceiling (`pydantic<=2.12.5`), which is tighter than what plain
`pip install gradio` asks for. Pinning pydantic==2.14.2 (fine locally) made the
real Space build die with `ResolutionImpossible` before any code ran.

These tests are offline/static so they always run; the live check against PyPI
is opt-in via ROUTELLM_CHECK_PYPI=1 and is also run in CI by a dedicated step
that actually resolves the real install command.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REQUIREMENTS = BACKEND / "requirements.txt"
README = BACKEND / "README.md"

#: Extras Hugging Face enables for the Gradio SDK. Mirrored in docs/DEPLOY.
HF_GRADIO_EXTRAS = ("oauth", "mcp")


def _requirements() -> dict[str, str]:
    """Map lowercase package name -> pinned version, ignoring comments/blanks."""
    pins: dict[str, str] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins[name.split("[")[0].strip().lower()] = version.strip()
    return pins


def _sdk_version() -> str:
    """Read sdk_version from the Space README's YAML header."""
    text = README.read_text(encoding="utf-8")
    match = re.search(r"^sdk_version:\s*(\S+)\s*$", text, re.MULTILINE)
    assert match, "backend/README.md has no sdk_version in its YAML header"
    return match.group(1).strip().strip('"').strip("'")


# --------------------------------------------------------------------------
# Static invariants
# --------------------------------------------------------------------------


def test_pydantic_is_pinned_within_gradios_mcp_ceiling():
    """The exact constraint that broke the build: gradio[mcp] needs <=2.12.5."""
    version = _requirements().get("pydantic")
    assert version, "pydantic must be explicitly pinned in requirements.txt"
    major, minor, patch = (int(p) for p in version.split(".")[:3])
    assert (major, minor, patch) <= (2, 12, 5), (
        f"pydantic=={version} exceeds gradio[mcp]'s ceiling of 2.12.5 — the "
        "Hugging Face build will fail with ResolutionImpossible"
    )


def test_pydantic_still_satisfies_fastapi_and_friends():
    """Don't 'fix' the ceiling by pinning something too old."""
    pins = _requirements()
    version = tuple(int(p) for p in pins["pydantic"].split(".")[:3])
    # fastapi>=2.9, pydantic-settings>=2.7, sqlmodel>=2.11 (per PyPI metadata)
    assert version >= (2, 11, 10), (
        f"pydantic=={pins['pydantic']} is below sqlmodel 0.0.48's floor of 2.11.0"
    )


def test_sdk_version_is_pinned():
    version = _sdk_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"sdk_version must be an exact version, got {version!r}. An unpinned "
        "sdk_version lets Hugging Face pick a new Gradio with new constraints."
    )
    # The pinned version must be one we actually verified.
    assert version == "6.31.0", (
        "sdk_version changed — re-verify the install closure and the test "
        "suite on the new Gradio before updating this assertion"
    )


def test_gradio_is_not_a_runtime_dependency():
    """Hugging Face provides gradio; listing it would fight its own extras."""
    assert "gradio" not in _requirements(), (
        "gradio must NOT be pinned in requirements.txt: the SDK injects "
        "gradio[oauth,mcp]==sdk_version and a bare pin conflicts with it"
    )


def test_ml_packages_stay_out_of_the_runtime_set():
    pins = _requirements()
    for heavy in ("torch", "sentence-transformers", "scikit-learn", "joblib"):
        assert heavy not in pins, f"{heavy} must stay in requirements-ml.txt"


def test_runtime_set_is_fully_pinned():
    """Every requirement must be ==; a range can drift into a conflict."""
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        assert "==" in line, f"unpinned requirement: {line!r}"


# --------------------------------------------------------------------------
# Live check against PyPI metadata (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("ROUTELLM_CHECK_PYPI"),
    reason="set ROUTELLM_CHECK_PYPI=1 to query PyPI",
)
def test_pinned_set_satisfies_gradio_extras_on_pypi():
    """Resolve the real metadata: gradio[<extras>] must accept every pin."""
    import json
    import urllib.request

    from packaging.requirements import Requirement
    from packaging.version import Version

    sdk = _sdk_version()
    url = f"https://pypi.org/pypi/gradio/{sdk}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        metadata = json.load(response)

    pins = {name: Version(version) for name, version in _requirements().items()}
    checked = 0
    for requirement in metadata["info"]["requires_dist"] or []:
        parsed = Requirement(requirement)
        extras = set(parsed.extras)
        # Only the extras the Space enables are installed.
        if extras and not extras & set(HF_GRADIO_EXTRAS):
            continue
        name = parsed.name.lower()
        if name not in pins:
            continue
        checked += 1
        assert parsed.specifier.contains(pins[name], prereleases=True), (
            f"gradio[{sdk}] requires {parsed}; requirements.txt pins "
            f"{name}=={pins[name]} — the Space build would fail"
        )
    assert checked > 0, "no overlapping pins found — metadata parsing is broken"


def test_constraint_mirrors_hugging_face_extras():
    """If HF's install line changes, this constant (and the docs) must too."""
    import subprocess

    workflow = (BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    expected = f"gradio[{','.join(HF_GRADIO_EXTRAS)}]"
    assert expected in workflow.replace(" ", ""), (
        "ci.yml must install gradio with the same extras Hugging Face uses "
        f"({HF_GRADIO_EXTRAS}); otherwise CI cannot catch this class of failure"
    )
