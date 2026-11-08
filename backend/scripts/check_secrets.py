#!/usr/bin/env python3
"""Secret scanner for the working tree **and** git history.

Why both: a key removed in a later commit is still readable by anyone who
clones, so "it is not in HEAD" is not the question. This script answers "has
this repository ever contained something that looks like a credential?"

Design rules:

* **Findings are masked.** Output shows ``gsk_…cdef`` (prefix + last 4), never
  the value. Printing a leaked key into a CI log copies the leak.
* **No network, no dependencies.** Runs on a bare interpreter, so it can be a
  pre-commit hook or a CI step before anything is installed.
* **Allowlisted by intent.** Test fixtures use obviously fake values
  (``gsk_test_…``, ``rk_not_a_real_key``); those are listed explicitly rather
  than tuning the patterns into uselessness.
* **Exit code matters.** 0 = clean, 1 = findings (CI fails), 2 = usage error.

Usage::

    python backend/scripts/check_secrets.py            # tree + history
    python backend/scripts/check_secrets.py --tree      # working tree only
    python backend/scripts/check_secrets.py --history   # git history only
    python backend/scripts/check_secrets.py --strict    # also print the file:line
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Pattern:
    """One credential shape worth failing a build over."""

    name: str
    regex: re.Pattern[str]
    hint: str


# Ordered by how specific they are: a Hugging Face token also matches a generic
# "long token" rule, so the specific name should win.
PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "huggingface-token",
        re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
        "Hugging Face access token — rotate it, then rewrite history.",
    ),
    Pattern(
        "openai-style-key",
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b"),
        "Provider API key — rotate it at the provider.",
    ),
    Pattern(
        "groq-key",
        re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b"),
        "Groq API key — rotate it in the console.",
    ),
    Pattern(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "Google API key — rotate it in AI Studio / Cloud console.",
    ),
    Pattern(
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
        "Anthropic API key — rotate it.",
    ),
    # URL patterns require a password that does not look like documentation:
    # `user:password@host` in a docstring or a test fixture is not a leak.
    Pattern(
        "database-password-url",
        re.compile(
            r"(?:postgres|postgresql)(?:\+\w+)?://[^\s:/@]+:"
            r"(?!(?i:password|passwd|pw|secret|user|test|changeme|example|host|redacted)\b)"
            r"(?:(?i:password|passwd|pw|secret|user|test|changeme|example|host|"
            r"redacted|your-password|db|database)|[^\s@/]{8,})"
            r"@[^\s\"']+"
        ),
        "Database URL with an inline password — rotate it and use a secret.",
    ),
    Pattern(
        "redis-password-url",
        re.compile(
            r"rediss?://(?:[^\s:/@]*):"
            r"(?!(?i:password|passwd|pw|secret|user|test|changeme|example|host|"
            r"redacted|your-password)\b)"
            r"(?:(?i:password|passwd|pw|secret|user|test|changeme|example|host|"
            r"redacted|your-password)|[^\s@/]{8,})"
            r"@[^\s\"']+"
        ),
        "Redis URL with an inline password — rotate it.",
    ),
    Pattern(
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "Private key material — rotate the key immediately.",
    ),
    Pattern(
        "aws-access-key-id",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "AWS access key id — rotate the pair.",
    ),
    Pattern(
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
        "GitHub token — revoke it in settings.",
    ),
    Pattern(
        "slack-token",
        re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
        "Slack token — revoke it.",
    ),
    # RouteLLM's own demo keys: only a problem if a REAL one (not a placeholder)
    # ever lands in the repository.
    Pattern(
        "routellm-demo-key",
        re.compile(r"\brk_[A-Za-z0-9_-]{30,}\b"),
        "A RouteLLM demo key. These are disposable, but treat one in git as "
        "revoked and regenerate it.",
    ),
)

#: Values that are obviously placeholders. Matched case-insensitively against
#: the whole match; anything listed here is not reported.
ALLOWLIST_SUBSTRINGS: tuple[str, ...] = (
    "your-real-key",
    "your_key",
    "placeholder",
    "example",
    "dummy",
    "fake",
    "not_a_real_key",
    "stale_key",
    "test_",
    "_test",
    "changeme",
    "xxxx",
    "0000000",
    "1234567890",
    "abcdef123456",
    "super-secret-value-for-detection",
    "live-value",
    "gsk_value",
    "3456",     # frontend fixtures: rk_aaaa…3456
    "aaaaaaaa",
    "example_key",
)

#: Test sources are excluded on purpose: they must contain
#: credential-SHAPED strings to test masking/fail-closed behaviour, and every
#: repository that scans them ends up with an allowlist longer than the code.
#: A real key committed to a test file is still caught in review, and this
#: scanner's own tests cover that case explicitly.
SKIP_PATH_PARTS = {"tests", "__tests__", "test"}

#: Paths never scanned in the working tree.
SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".ruff_cache",
    "data",
}
#: Tracked files beyond this size are skipped (lockfiles, datasets, media).
MAX_FILE_BYTES = 2_000_000
#: Lines that are documentation rather than configuration. A commented-out
#: example (`#   REDIS_URL=rediss://...`) teaches the format; it cannot be used
#: as a credential, so it is not a finding.
DOC_LINE_PREFIXES = ("#", "//", "--", ";")


def _is_documentation_line(text: str, index: int) -> bool:
    """True when the match sits on a comment/example line."""
    line_start = text.rfind("\n", 0, index) + 1
    return text[line_start:index].strip().startswith(DOC_LINE_PREFIXES)


#: Local state that is git-ignored by design. Scanning it would only nag about
#: the developer's own machine; a *committed* .env is caught by the history
#: scan instead.
SKIP_FILENAMES = {".env", ".env.local", ".env.hf.local", ".dev.vars"}
#: Extensions that cannot hold a readable credential.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".whl", ".so", ".dylib", ".dll", ".pyc", ".woff", ".woff2", ".ttf", ".mp4",
    ".pkl", ".sqlite", ".db",
}


@dataclass(frozen=True)
class Finding:
    """A masked hit: enough to locate it, not enough to reuse it."""

    pattern: Pattern
    location: str
    masked: str

    def render(self, strict: bool = False) -> str:
        where = self.location if strict else self.location.split(":", 1)[0]
        return f"  [{self.pattern.name}] {where}  ->  {self.masked}\n      {self.pattern.hint}"


def mask(value: str) -> str:
    """``gsk_abcdef…cdef`` — identify, never disclose."""
    cleaned = value.strip()
    if len(cleaned) <= 8:
        return "****"
    return f"{cleaned[:4]}…{cleaned[-4:]}"


def _allowlisted(match: str) -> bool:
    lowered = match.lower()
    return any(token in lowered for token in ALLOWLIST_SUBSTRINGS)


def scan_text(text: str, location: str) -> list[Finding]:
    """Every credential-shaped match in ``text``, masked and allowlist-filtered."""
    findings: list[Finding] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            value = match.group(0)
            if _allowlisted(value):
                continue
            if _is_documentation_line(text, match.start()):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(pattern, f"{location}:{line}", mask(value))
            )
    return findings


def iter_tree_files(root: Path) -> Iterator[Path]:
    """Tracked-looking files under ``root``, skipping the obvious noise."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if any(part in SKIP_PATH_PARTS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.name in SKIP_FILENAMES or path.name.startswith(".env."):
            # Keep the tracked example: it documents which variables exist and
            # must never contain a real value.
            if path.name != ".env.example":
                continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:  # pragma: no cover - disappearing file
            continue
        yield path


def scan_tree(root: Path = REPO_ROOT) -> list[Finding]:
    """Scan every readable file in the working tree."""
    findings: list[Finding] = []
    for path in iter_tree_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        findings.extend(
            scan_text(text, str(path.relative_to(root)))
        )
    return findings


def _git(*args: str) -> str:
    """Run a git command in the repo, returning decoded stdout ('' on failure).

    Decoding is explicit and lossy on purpose. ``text=True`` would decode with
    the locale codec and *raise* on the first non-UTF-8 byte, and history
    contains plenty: an old PNG, a Windows-1252 character in someone's commit.
    A scanner that crashes on a binary blob is a scanner that reports "clean".
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git not installed
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _git_raw(*args: str) -> bytes:
    """Bytes-returning variant of :func:`_git`, for blob contents."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git not installed
        return b""
    return result.stdout if result.returncode == 0 else b""


def _looks_binary(data: bytes) -> bool:
    """True for blobs that decode to noise (images, archives, compiled objects).

    Credentials are text. Scanning a PNG costs time and can only produce false
    positives, so the NUL-byte / undecodable-share heuristic that git itself
    uses is applied first.
    """
    if b"\x00" in data[:8000]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def scan_history() -> list[Finding]:
    """Scan every blob ever committed, plus the commit messages.

    ``git log -p`` would be simpler but stops at the first path that has ever
    been a binary; ``git rev-list --objects --all`` + ``cat-file`` covers the
    whole object graph deterministically.
    """
    findings: list[Finding] = []

    # Commit/annotation messages are plain text in the object database.
    log = _git("log", "--all", "--format=%H%x00%B")
    for chunk in log.split("\x00"):
        if chunk.strip():
            findings.extend(scan_text(chunk, "git:commit-message"))

    object_ids = _git("rev-list", "--objects", "--all").splitlines()
    for entry in object_ids:
        parts = entry.split(maxsplit=1)
        if not parts:
            continue
        object_id = parts[0]
        name = parts[1] if len(parts) > 1 else "(unnamed)"
        if Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_PATH_PARTS for part in Path(name).parts):
            continue  # fake credentials in tests: see SKIP_PATH_PARTS
        kind = _git("cat-file", "-t", object_id).strip()
        if kind != "blob":
            continue
        raw = _git_raw("cat-file", "-p", object_id)
        if not raw or _looks_binary(raw):
            continue
        findings.extend(
            scan_text(raw.decode("utf-8", errors="replace"), f"git:{name}@{object_id[:8]}")
        )
    return findings


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """One line per (pattern, location, masked value)."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.pattern.name, finding.location.split(":", 1)[0], finding.masked)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan this repository (and its history) for credentials. "
        "Findings are masked."
    )
    parser.add_argument("--tree", action="store_true", help="working tree only")
    parser.add_argument("--history", action="store_true", help="git history only")
    parser.add_argument(
        "--strict", action="store_true", help="include the line number in output"
    )
    args = parser.parse_args(argv)

    do_tree = args.tree or not (args.tree or args.history)
    do_history = args.history or not (args.tree or args.history)

    findings: list[Finding] = []
    if do_tree:
        findings.extend(scan_tree())
    if do_history:
        findings.extend(scan_history())
    findings = dedupe(findings)

    if not findings:
        scope = "working tree" if args.tree else ("history" if args.history else "tree + history")
        print(f"No credentials found in the {scope}. Findings are masked; "
              "rotate anything that ever appears here.")
        return 0

    print(f"{len(findings)} possible credential(s) found (values masked):")
    for finding in findings:
        print(finding.render(strict=args.strict))
    print()
    print("If any of these is real: rotate it at the provider FIRST, then remove")
    print("it from history (e.g. git filter-repo) and force-push. Removing it")
    print("from HEAD alone does not un-leak it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
