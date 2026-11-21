# Running the live resilience eval

This run calls real providers and can spend quota. A dry run checks saved-key
ownership and Chaos Mode, but does **not** prove that the providers answer.
Use only provider credentials you are authorized to use.

## Start the gateway

1. Start the gateway with Chaos Mode enabled. In a terminal at the repository
   root, set `CHAOS_MODE_ENABLED=true` and run `python run.py`.
2. Open the dashboard URL shown by the launcher. Keep the gateway running.
3. Open **API Keys**. Select an existing active Routtener key whose full value
   you saved when it was created, or generate a new key and copy its full value
   from the one-time display into a secure location. Do not paste it into a
   shell command, chat, issue, or log.
4. Open **Provider Keys**. Confirm the active-key prefix matches the key you
   will enter for the eval. Save at least two provider keys while that Routtener
   key is active. Use the dashboard's provider **Test** control to verify that
   each provider actually answers; a saved key alone does not prove this.

## Why the active key matters

- Each saved provider key belongs to one Routtener API key.
- The gateway uses the key supplied with the request to find its owner.
- Provider keys saved under a different Routtener key are not visible.
- A newly generated throwaway eval key owns no saved provider keys.
- Switching the dashboard's active key changes which saved keys you see.

## Check first, then run

In a separate interactive terminal, from the repository root, run:

```bash
python evals/run_resilience_eval.py --live --dry-run --yes-spend-quota --api-key-prompt --base-url http://127.0.0.1:8000
```

Enter the same full Routtener key when prompted; terminal input is hidden.
Do not include the key in the command itself. Confirm the provider list and
Chaos check. A dry run sends no routing calls and writes no live results.

After testing the providers and checking their quotas, run:

```bash
python evals/run_resilience_eval.py --live --yes-spend-quota --api-key-prompt --base-url http://127.0.0.1:8000
```

The script will ask for the key again. A completed live run writes
`docs/eval-results.md`; review it before committing. The live run reports the
router arm only, not a live naive baseline. The mocked eval's naive arm has its
only provider deliberately down, so its zero success is by construction: it
shows that fallback can work, not how often real providers fail. Confidence
intervals express uncertainty around the observed success proportion, not a
guarantee of future service.

## Troubleshooting

| Message | What to do |
|---|---|
| `--api-key-prompt requires an interactive terminal` | Run the command in a terminal, not a pipe or unattended job. |
| `the supplied eval API key is malformed` | Enter the complete Routtener key, with no whitespace; do not enter a provider key. |
| `minting a throwaway key` | Stop and use `--api-key-prompt` with the dashboard key that owns the saved provider keys. |
| `Providers detected: none` | Check the active prefix in Provider Keys and save keys under the same Routtener key used for the eval. |
| `fewer than 2 providers have saved keys` | Save at least two provider keys under that active key, then retry the dry run. |
| `Chaos check: NOT enabled` | Restart the gateway with `CHAOS_MODE_ENABLED=true`. |
| `--base-url is not a valid URL` | Use an HTTP or HTTPS URL with a host and numeric port, for example `http://127.0.0.1:8000`. |
| `backend rate-limited the eval` | Adjust the gateway rate limit before retrying; check provider quotas too. |

For unattended use, `ROUTTENER_EVAL_API_KEY` supplies the active key; the
legacy `ROUTELLM_EVAL_API_KEY` is used only when the new variable is absent.
The prompt takes precedence over both. Never put a key into a script or commit.
