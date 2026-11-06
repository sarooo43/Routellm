# Hugging Face build failures — what to collect, and the usual causes

Hugging Face's build log is the only place a build failure is explained. When it
is empty, truncated, or only shows cache telemetry, work through this page.

---

## 1. "cache miss: …" is not an error

This is normal build-cache accounting:

```text
cache miss: [base 7/7] RUN mkdir -p /home/user && ( [ -e /home/user/app ] || ln -s /app/ /home/user/app ) || true
cache miss: [base 6/7] RUN --mount=target=/tmp/requirements.txt,source=requirements.txt pip install …
{"total":26,"completed":20,"user_cached":5,…}
```

It says "this layer had to be rebuilt instead of reused". A `cache miss` on the
pip layer simply means pip did real network work this run.

**The failure is a line that starts with `ERROR`, `error:`, `Killed`,
`No space left on device`, or a Python traceback**, and it appears *before* that
summary.

## 2. The Space runs two different "builds"

| Where | What it builds | Which file decides |
|---|---|---|
| Gradio SDK (what this project uses) | a `python:3.12` image, then `pip install` + your `app_file` | `README.md` header (`sdk: gradio`, `app_file: hf_app.py`) |
| Docker SDK | your `Dockerfile` | `Dockerfile` |

`backend/Dockerfile` exists for the Caddy/self-host path and is **not** used by
the Gradio SDK. Editing it will never change a Space build. This confuses people
regularly, so it is worth stating: if the log shows `FROM docker.io/library/python:3.12…`
and `gradio[oauth,mcp]==…`, you are on the SDK path.

## 3. The install command the Space actually runs

```text
pip install --no-cache-dir -r /tmp/requirements.txt "torch<=2.13.0" \
    gradio[oauth,mcp]==<sdk_version> \
    "uvicorn>=0.14.0" "websockets>=10.4" \
    spaces==0.51.3
```

Two dependency constraints have already broken this build once each:

1. **`pydantic` must be `<=2.12.5`** — Gradio's `mcp` extra caps it. A local
   `pip install gradio` does *not* declare that cap, which is why it passed
   locally and failed on the Space. Pinned to `2.12.5` in
   `backend/requirements.txt`, guarded by `backend/tests/test_hf_requirements.py`.
2. **`gradio` must not be pinned in `requirements.txt`** — Hugging Face injects
   `gradio[oauth,mcp]==<sdk_version>` itself; a second pin fights it.

Reproduce the exact resolution locally (no 2 GB torch download):

```bash
cd backend
python3 -m venv /tmp/spacecheck
/tmp/spacecheck/bin/pip install "torch-stub==0.0.1" || true
/tmp/spacecheck/bin/pip install --dry-run -r requirements.txt \
  "gradio[oauth,mcp]==6.31.0" "spaces==0.51.3" \
  "uvicorn>=0.14.0" "websockets>=10.4"
```

`--dry-run` never installs anything; exit code 0 means the Space's resolution
will succeed. The `hf-space-boot` CI job does the real install on every push.

## 4. Ranked causes, with the log line that identifies each

| Log line you see | Cause | Fix |
|---|---|---|
| `ResolutionImpossible` / `conflicting dependencies` | a pin fights Gradio's extras | see §3; run the dry-run above |
| `Killed` or exit code **137** | out-of-memory during install or import | `Requirements`/`app_file` too heavy; keep ML packages out (they live in `requirements-ml.txt`) |
| `No space left on device` | build cache full | **Factory rebuild** (⋯ menu) — discards all cached layers |
| `Could not find a version that satisfies the requirement X` | typo, yanked version, or a pin newer than the index | check the package on PyPI; pin an existing version |
| `ERROR: Failed building wheel for …` | a package needs a compiler the image lacks | prefer a pinned wheel version |
| nothing at all / empty log / red *before* any step | platform-side failure (capacity, quota, a rejected config) | Rebuild; if it repeats, Factory rebuild; if it still repeats, it is account/platform — nothing in the repo can fix it |
| `Commit SHA:` does not match your latest push | the build ran an older revision | redeploy (`deploy_hf.yml` or `scripts/deploy_hf.ps1`) |

## 5. What to collect before asking for help

1. The **first** `===== Build Queued at … / Commit SHA: xxxxxxx =====` line.
2. Everything **after** the pip `RUN` block — 20 lines is plenty.
3. Whether the Space is on **CPU basic** or **ZeroGPU** (Settings → Hardware).

Paste those three and the cause is usually obvious in one look. With only the
cache summary, the failure genuinely cannot be diagnosed — which is the
situation `docs/VERIFICATION.md` §5 documents from the owner's side.

## 6. This project's build is deterministic, and here is the proof

The commit that last failed to build contained **no** change to
`requirements.txt`, `README.md`, or any other file the build consumes — only
runtime code in `hf_app.py` and docs. And the exact install command still
resolves against PyPI today. So a failure at that commit was not caused by a
dependency change; it was transient or platform-side.

That is why the response to a build failure here is, in order:

1. **Rebuild** (Space ⋯ menu) — cheapest, fixes transient failures.
2. **Factory rebuild** — clears a poisoned build cache.
3. Re-check the resolution locally (§3) before touching any pin. Editing pins
   blind is how the `pydantic` conflict was introduced in the first place.
