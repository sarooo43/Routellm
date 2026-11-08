"""One place for the product's name, version and upstream credit.

Why a constant instead of literals: this project shares a name with
**LMSYS RouteLLM** (the routing-model research project and its
``routellm``/``route llm`` weights). They are unrelated codebases. Keeping the
name in one module means the disclaimer is written once and cannot drift, and
renaming later is a one-line change rather than a repository-wide search.

Renaming is deliberately NOT done here: package names, module paths, the
``rk_`` key prefix and the database filename stay as they are, so existing
installs keep working.
"""

from __future__ import annotations

#: Display name used by the API, the dashboard and the docs.
APP_NAME = "RouteLLM Gateway"
#: Short name for tight UI spots (sidebar, chips, log lines).
APP_SHORT_NAME = "RouteLLM"
#: Version reported by /health and the OpenAPI document.
APP_VERSION = "0.1.0"

#: Shown next to the name wherever confusion is plausible.
UPSTREAM_DISCLAIMER = (
    "Independent project. Not affiliated with LMSYS RouteLLM, the routing-model "
    "research project of the same name."
)

#: Canonical project URL (used in the OpenAPI docs and the landing page).
PROJECT_URL = "https://github.com/sarooo43/Routellm"


def user_agent(suffix: str = "") -> str:
    """``User-Agent`` string for outbound provider requests.

    Provider APIs rate-limit by client, so an honest, stable value matters —
    and it must not contain a machine name or username.
    """
    base = f"{APP_SHORT_NAME}/{APP_VERSION}"
    return f"{base} {suffix}".strip() if suffix else base
