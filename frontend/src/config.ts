/**
 * Single source of truth for backend API connectivity.
 *
 * LOCAL-FIRST DEFAULT: same-origin, no configuration.
 * The backend serves the built dashboard itself (`backend/app/frontend.py`),
 * so `''` — meaning "call the origin that served this page" — is the SUPPORTED
 * local topology: `python run.py` then http://127.0.0.1:8000/.
 *
 * Rules:
 *  - Local (python run.py): '' — backend and UI share an origin.
 *  - Dev (`python run.py --dev`): '' + vite.config.ts proxy to 127.0.0.1:8000.
 *  - Worker deployment (optional/experimental): '' — the worker proxies
 *    /api/*, /v1/* and /health to ROUTELLM_API_ORIGIN.
 *  - Split deployment: set VITE_API_BASE_URL to the backend origin (https
 *    REQUIRED in a production build — an http:// value would be blocked as
 *    mixed content on an https dashboard, so it is demoted to '' with a flag
 *    instead of shipping a request that can never fire).
 *
 * There is deliberately no "missing backend URL" error state: an unset
 * variable is the normal, working case.
 */

const raw = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/+$/, '')

const IS_PROD = import.meta.env.PROD
const looksLikeInsecureHttp = raw.startsWith('http://')

// Block mixed content: an http:// base would be silently blocked by the
// browser (which is exactly how the old "backend is not running" bug
// manifested). A development build keeps it, so `--dev` against a plain-http
// backend still works.
export const API_BASE_URL: string = IS_PROD && looksLikeInsecureHttp ? '' : raw

export const MIXED_CONTENT_MASKED: boolean = IS_PROD && looksLikeInsecureHttp && raw !== ''

export const IS_SAME_ORIGIN: boolean = API_BASE_URL === ''
