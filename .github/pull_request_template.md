## What this changes

<!-- One paragraph. Link the issue if there is one. -->

## Why

<!-- The reasoning. If it changes behaviour, say what the old behaviour was. -->

## Verification

<!-- Commands you actually ran and their result. "Should work" is not verification. -->

- [ ] `cd backend && pytest` — result:
- [ ] `cd frontend && npm test` — result:
- [ ] `npx tsc -b` and `npm run lint` — result:
- [ ] Manually exercised the affected flow (`python run.py`) — what I saw:

## Checklist

- [ ] No new secret, credential, token or `.env` content is added
      (test fixtures included).
- [ ] New behaviour has a test; changed behaviour updates the existing one.
- [ ] Local-first defaults still work with no `DATABASE_URL`, no `REDIS_URL`
      and no provider key.
- [ ] No invented benchmark numbers, and demo/MOCK traffic stays excluded from
      any measurement.
- [ ] Docs updated if a default, flag or endpoint changed.

## Screenshots (UI changes only)

<!-- Dark-theme dashboard: include one for a visual change. -->
