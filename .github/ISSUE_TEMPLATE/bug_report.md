---
name: Bug report
about: Something does not work as documented
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- What you did, what you saw, what you expected. -->

## Steps to reproduce

1.
2.
3.

## Environment

- OS:
- Python version (`python --version`):
- Node version (`node --version`), if the dashboard is involved:
- How you started it (`python run.py`, `--dev`, Docker, other):

## Diagnostics

`GET /health` response (it always returns 200 and names the failing component):

```json
<paste here>
```

Relevant log lines, **with secrets masked**:

```
<paste here>
```

## Checklist

- [ ] I masked every API key, token, `ENCRYPTION_KEY` and database password.
- [ ] I am on the latest `main` (or say which commit/tag).
- [ ] I checked `docs/` and the README troubleshooting section.
