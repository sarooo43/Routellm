# Deploying the Cloudflare Worker — absolute beginner walkthrough

No prior terminal experience assumed. If a command fails, stop and read the
error line before continuing.

---

## Why this page exists

The Worker is the thing your browser talks to at
`https://routellm.saboornasar66.workers.dev`. It serves the dashboard **and**
forwards `/api/*`, `/v1/*` and `/health` to the FastAPI backend on Hugging Face.

The Worker needs to know the backend's address. That address now lives in
`wrangler.jsonc` (`vars.ROUTELLM_API_ORIGIN`), so it is part of the repo — but
**it only reaches Cloudflare when you deploy.** Editing it and pushing to GitHub
is not enough.

---

## Step 0 — the terminal question you asked

You need a terminal on **your own computer**, not in the browser and not in the
Cloudflare dashboard. Any of these is fine:

| OS | How to open it |
|---|---|
| Windows | Start menu → type `PowerShell` → Enter |
| macOS | ⌘+Space → type `Terminal` → Enter |
| Linux | Ctrl+Alt+T |

You also need **Node.js** (version 20 or newer). Check:

```powershell
node --version
```

If that prints something like `v22.11.0`, you are ready. If it says "not
recognized", install the LTS build from <https://nodejs.org> and reopen the
terminal.

---

## Step 1 — Get the code and its dependencies

```powershell
git clone https://github.com/sarooo43/Routellm.git
cd Routellm
git pull
npm ci
```

`npm ci` installs `wrangler` (the deploy tool) **and** the dashboard
dependencies. This is the step whose absence causes:

```
sh: 1: wrangler: not found
```

> Run `npm ci` in the **repository root** (`Routellm/`), not in `frontend/`. The
> root is an npm workspace; running it in `frontend/` alone leaves `wrangler`
> uninstalled and `npm run deploy` fails.

---

## Step 2 — Build the dashboard

```powershell
npm run build
```

Expected tail:

```text
✓ built in 1.04s
```

This creates `frontend/dist`. The Worker serves those files, so the deploy step
below **fails** with `assets.directory … does not exist` if you skip it.

---

## Step 3 — Check what will be deployed BEFORE deploying

```powershell
npm run deploy:dry-run
```

You want to see this row in the bindings table:

```text
env.ROUTELLM_API_ORIGIN ("https://saboornotgonna-routellm.hf.sp...")   Environment Variable
```

- **Row present** → the Worker will know where your backend is. Continue.
- **Row missing** → `wrangler.jsonc` was not updated. Run `git pull` again.

This step never changes anything on Cloudflare. It only parses the config.

---

## Step 4 — Log in to Cloudflare (one time)

```powershell
npx wrangler login
```

A browser window opens. Sign in with the account that owns the `routellm`
Worker and click **Allow**. The terminal then says `Successfully logged in`.

If it says the Worker is on a different account, check
`CLOUDFLARE.md` §2 — the account id is `a7014a6c3b2a2ad8b4c889f5842d3218`.

---

## Step 5 — Deploy

```powershell
npm run deploy
```

A successful deploy ends with something like:

```text
Uploaded routellm (3.21 sec)
Deployed routellm triggers (1.02 sec)
  https://routellm.saboornasar66.workers.dev
Current Version ID: 8f4c1b1e-....
```

**A new `Current Version ID` is the proof it landed.** If you re-run the deploy
and that ID is *identical* to the previous one, nothing was published — scroll
up for the error.

---

## Step 6 — Verify the Worker now reaches the backend

```powershell
curl.exe -s https://routellm.saboornasar66.workers.dev/health
```

Read the answer:

| Response | Meaning | Next |
|---|---|---|
| `{"status":"ok","db":"ok","redis":"ok",...}` | **Done.** Worker and backend both healthy. | Open the dashboard. |
| `{"error":"routellm_backend_unreachable",...}` | Worker is correctly deployed; the **Space** is not answering. | The Space is still down — see the Space section below. |
| `{"error":"routellm_backend_not_configured",...}` | The deploy did **not** publish the new config. | Repeat steps 2–5 and watch for the new Version ID. |
| `<!doctype html> … not configured` | You are viewing the Worker's explanatory page (browser navigation). | Same as `not_configured`. |

Then open the dashboard in a browser. The pill in the top-right must read
**Online** (green). Anything else, and the pill itself now prints the reason.

---

## The Space side (the other half)

The Worker cannot fix a Space that is not running. Check it first:

```powershell
curl.exe -s https://saboornotgonna-routellm.hf.space/health
```

- `{"status":"ok",...}` → the Space is up.
- Anything else, or a timeout → go to the Space's **Logs** tab and look for
  `Uvicorn running on http://0.0.0.0:7860`.

### CPU basic is selectable on this account

The "Confirm hardware update" dialog you saw:

```text
CPU basic
2 vCPU · 16 GB RAM
Free
```

means the tier is available; the confirm dialog is simply how Hugging Face
applies a **downgrade** (it warns that files may be lost and that you are
leaving ZeroGPU). Confirming it will not delete your code — but a rebuild
follows, so nothing is served for a minute or two.

**Recommendation:** switch to CPU basic. This backend has no GPU code, and the
`address already in use` failure only appears on ZeroGPU hardware.

---

## What "done" looks like, in order

- [ ] `npm run deploy:dry-run` shows `env.ROUTELLM_API_ORIGIN`
- [ ] `npm run deploy` prints a **new** Current Version ID
- [ ] Worker `/health` returns the backend's JSON, not an error
- [ ] Space Logs show `Uvicorn running on http://0.0.0.0:7860`
- [ ] Dashboard pill reads **Online**
- [ ] `python scripts/smoke_deployed.py --api https://saboornotgonna-routellm.hf.space --origin https://routellm.saboornasar66.workers.dev` passes

---

## Common failures

| Message | Cause | Fix |
|---|---|---|
| `wrangler: not found` | `npm ci` was not run at the repo root | `cd Routellm && npm ci` |
| `assets.directory … does not exist` | `frontend/dist` missing | `npm run build` |
| `You are not logged in` | no session | `npx wrangler login` |
| `Could not resolve host` / TLS error | no internet or a proxy | check connectivity |
| Deploy "succeeds" but nothing changes | you deployed from an old checkout | `git pull`, then rebuild and redeploy |
| `Workers Builds` fails in 0s on GitHub | Cloudflare's dashboard build config, not this code | follow `CLOUDFLARE.md` §2 — build `npm run build`, deploy `npm run deploy`, root `/` |
