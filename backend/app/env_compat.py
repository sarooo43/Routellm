"""Environment-variable prefix compatibility for the Routtener rename.

The project was renamed RouteLLM -> Routtener. Every documented environment
variable now has a primary ``ROUTTENER_*`` name, while the old ``ROUTELLM_*``
name keeps working as a documented fallback so existing installs, launch
scripts, docker files and CI configurations keep working unchanged.

Rules (covered by ``tests/integration/test_rename_compat.py``):

* ``ROUTTENER_X`` set, ``ROUTELLM_X`` not set -> the new value is used.
* ``ROUTELLM_X`` set, ``ROUTTENER_X`` not set -> the old value is used.
* both set -> the NEW value wins.
* neither set -> the default is used.

``get()`` is for modules that read ``os.environ`` directly (``app.local``,
``app.runtime``, ``hf_app.py``). Pydantic settings fields are handled by the
validator in ``app.config``.
"""

from __future__ import annotations

import os

#: Primary variable-name prefix after the rename.
NEW_PREFIX = "ROUTTENER_"
#: Legacy prefix, kept working as a documented fallback.
OLD_PREFIX = "ROUTELLM_"


def get(suffix: str, default: str = "") -> str:
    """Return an env value by its prefix-free suffix, new prefix winning.

    ``get("DATA_DIR")`` reads ``ROUTTENER_DATA_DIR`` first, then
    ``ROUTELLM_DATA_DIR``, then returns ``default``.
    """
    value = os.environ.get(NEW_PREFIX + suffix)
    if value is not None:
        return value
    return os.environ.get(OLD_PREFIX + suffix, default)


def get_int(suffix: str, default: int) -> int:
    """``get()`` for integer-valued variables, falling back on any parse error."""
    raw = get(suffix)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def truthy(suffix: str, default: bool = False) -> bool:
    """``get()`` for flag-valued variables ("1", "true", "yes", "on")."""
    flag = get(suffix).strip().lower()
    if not flag:
        return default
    return flag in {"1", "true", "yes", "on"}
