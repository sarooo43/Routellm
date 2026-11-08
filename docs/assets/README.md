# Screenshot / demo assets

The root README references `demo.gif`. It is intentionally **absent** until a
real recording exists — a broken image in a portfolio README is worse than an
obvious placeholder.

## What to record (owner task)

1. Start clean: `rm -rf data backend/.env && python run.py`
2. Record ~15 seconds at 1280×720, dark UI:
   - the dashboard with the getting-started checklist,
   - **Providers** → paste a key → "Configured",
   - **Explorer** → send a prompt → routing decision + attempt chain,
   - **Simulate outage** → the chain shows a skipped provider and a fallback,
   - the health pill staying green.
3. Export as GIF (≤ 8 MB, 12–15 fps) to `docs/assets/demo.gif`, or as MP4 and
   link the video instead.

## Rules

- **No keys on screen.** Mask or pre-fill with a fake value; blur any real key
  before saving the file.
- **No invented numbers.** Recorded KPIs must be from the run you filmed.
- If it shows demo mode, keep the `MOCK` badge visible — that is the honest
  version of the feature, not something to crop out.
