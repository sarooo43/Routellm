# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

- Use GitHub's private reporting: **Security → Report a vulnerability** on
  [github.com/sarooo43/Routellm](https://github.com/sarooo43/Routellm), or
- email the maintainer listed on that profile.

Include what you need to make the problem reproducible, and mask every
credential (never paste a real provider key, `ENCRYPTION_KEY`, `rk_` key or a
database URL with a password). Expect an acknowledgement within a few days;
this is a side project, not a funded product.

If a secret has been committed and pushed, treat it as compromised even after
it is removed: rotate it at the provider first, then tell me so history can be
cleaned up.

## Threat model (local-first)

This app runs on **your** machine, holding **your** provider keys. The design
choices follow from that:

**Bind loopback by default.** `python run.py` listens on `127.0.0.1`. Anyone
who can reach the port can spend your provider credits, so `--host 0.0.0.0`
prints a warning and is an explicit decision, not a default.

**No accounts.** `REQUIRE_API_KEY=false` is the local default: requests are
attributed to a single stable "local identity" row so usage counters and
per-caller provider keys still work. Set `REQUIRE_API_KEY=true` to require an
`X-API-Key` on every protected route. Generated keys (`rk_…`) are stored as
SHA-256 hashes with an 8-character display prefix; the raw key is shown once.
This is lightweight demo auth, not multi-tenancy — do not expose it to the
internet on the assumption that it is one.

**Provider keys are encrypted at rest.** Keys saved through the Providers page
are Fernet-encrypted before they touch the database, and the API returns only a
4-character hint. The encryption key is read from `PROVIDER_KEY_ENCRYPTION_KEY`,
or generated once into `<data dir>/encryption.key` with mode `0600` (POSIX) and
reused afterwards. Deleting that file makes stored provider keys unreadable —
re-enter them. The key is never logged, returned by an API, or shown in the UI.
If no key can be obtained at all, writes and BYOK routing fail closed with 503
rather than silently falling back.

**Environment keys are a fallback, not the default.** `GROQ_API_KEY`,
`GEMINI_API_KEY` and `DEEPSEEK_API_KEY` in `backend/.env` work, but the
Providers page wins because it scopes keys to one identity and keeps them
encrypted at rest.

**What is stored on disk.** `<data dir>/` (default `./data`, git-ignored)
contains the SQLite database, the generated encryption key and logs. Back it up
if you care about the history; delete it to start clean. It is never uploaded
anywhere.

**Outbound traffic.** The gateway only calls the provider you configured, over
HTTPS. Demo mode makes no network call at all. There is no telemetry, no
analytics beacon and no phone-home.

**What this app does not protect against.** A local attacker with your user
account can read `./data` (file permissions are best-effort, and Windows has no
POSIX mode bits), and a browser extension can read whatever the dashboard can.
Run it as yourself, on a machine you trust, behind loopback.

## Supported versions

Fixes land on `main` and in the latest release tag. Older tags are not patched.

## Optional cloud deployment

The Hugging Face Space, Cloudflare Worker and Caddy configurations under
`docs/deploy/` are **experimental** and outside the supported local-first
threat model. If you expose this publicly: set `REQUIRE_API_KEY=true`, set an
explicit `CORS_ORIGINS`, pin `PROVIDER_KEY_ENCRYPTION_KEY`, disable
`CHAOS_MODE_ENABLED`, and keep provider keys server-side (never in a public
deployment's client bundle).
