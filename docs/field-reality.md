# Field reality: what we actually know about each provider

Every provider fact in this project is one of three things, and the label
matters more than the fact:

- **OBSERVED LIVE** — seen in the owner's real run with a real key. Trustworthy.
- **DOCUMENTED** — from public provider docs. Not verified by this project.
- **ASSUMED** — a guess. Never presented to the user as fact.

Nothing here is invented. Where we do not know something, it says so.

## Gemini

| Fact | Label | Evidence |
|---|---|---|
| `models.list` returns 49 entries for a free-tier key, filtered to `generateContent` | OBSERVED LIVE | owner's run; names recorded in `backend/tests/fixtures/provider_shapes/gemini_list_models.json` |
| The app kept 44 of them | OBSERVED LIVE | `model_discovery_ok models=44` in the app log |
| The 5 dropped are exactly the TTS ids | OBSERVED LIVE (count) / INFERRED (identity) | 49 − 44 = 5; the deny-list matched `tts`. `test_tts_exactly_explains_49_vs_44` pins this |
| The list's own order is not a ranking; the app took `gemini-2.5-flash` as the default | OBSERVED LIVE | sample `["gemini-2.5-flash", "gemini-2.5-pro", …]` |
| `gemini-2.5-flash` answers **404** "no longer available to new users … use models/gemini-3.8-flash" | OBSERVED LIVE, truncated | the app's 200-char log cap cut it; the cap is now 1000 |
| The same key answers **403** to a direct `generateContent`, 404 through the app, for the same model | OBSERVED LIVE, status only, body unseen | unexplained; `Run diagnostics` exists to capture the body |
| Whether 403 is always key-wide | ASSUMED | the walk probes one cross-family model and then stops; a code comment says why |
| `-latest` aliases point at the newest stable family | ASSUMED, unverified | they rank first in Auto only until a live test passes |
| Key transport: `x-goog-api-key` header, never `?key=` | DOCUMENTED + enforced | a key must never appear in a URL, an httpx exception or a log line; unit-tested |
| Retired models stay in the list but cannot be called | OBSERVED LIVE | the 404 above came from a model that had just been listed |
| Free-tier eligibility is per project and changes without notice | ASSUMED | the 403 body would say; we have not seen one |

The Auto allow-list was designed from the observed 49 names: stable
`gemini-<ver>-flash` / `-flash-lite` / `-pro` and `gemma-*-it`, with `-latest`
aliases first, previews behind an off-by-default setting, and codenames
(`gemini-4-argon`/`carbon`/`hydrogen`) and specialized families (tts, image,
lyria, robotics, computer-use, deep-research, antigravity, nano-banana, omni,
customtools) never eligible.

## Groq

| Fact | Label | Evidence |
|---|---|---|
| OpenAI-compatible surface at `https://api.groq.com/openai/v1` | DOCUMENTED | asserted verbatim in `test_provider_urls.py` |
| Models come from `GET /openai/v1/models` | DOCUMENTED | same |
| Free-tier limits and available models | ASSUMED | nothing has been observed live for Groq in this project; fixtures are labelled SYNTHETIC |
| Non-chat ids (whisper, tts, guard) are excluded from Auto | ASSUMED | deny-list matched against synthetic fixtures only |

## DeepSeek

| Fact | Label | Evidence |
|---|---|---|
| Base URL `https://api.deepseek.com/v1` | DOCUMENTED | asserted verbatim in `test_provider_urls.py` |
| `deepseek-coder` exists and is preferred for code tasks | ASSUMED, and previously wrong | it was hardcoded and injected by the executor, bypassing discovery; now a soft preference applied only if the account lists it |
| Model list and pricing | ASSUMED | synthetic fixtures only |

## Pricing / tiers in the UI

`tier` (free | paid | unknown) is a **static estimated table** and the API says
so: every row carries `tier_source: "estimated"`. The list-models endpoints do
not return pricing. If a provider starts returning it, that field is where it
goes. Capture mode exists to replace the estimates with observed shapes:

```bash
ROUTELLM_CAPTURE_PROVIDER_SHAPES=true python run.py
# then use the Providers page; sanitized shapes land in
# data/provider_shapes/<provider>.json (gitignored, never contains a key)
```

## Why this file exists

An earlier report described Gemini's 404 as "assumed". It was not: it had been
observed, with the message recorded. Mixing observed and assumed facts is how a
guess gets shipped as a feature. If a fact is not labelled here, treat it as
ASSUMED until someone runs `Run diagnostics` (Providers page) and pastes the
report back.
