/**
 * Single source of truth for backend API connectivity.
 *
 * VITE_API_BASE_URL is optional BY DESIGN for the primary deployment:
 * the Cloudflare Worker (see cloudflare/worker.js + wrangler.jsonc) serves
 * the SPA and proxies /api/*, /v1/* and /health server-to-server to
 * ROUTELLM_API_ORIGIN — so a build with no env var and same-origin ('')
 * requests is the SUPPORTED production topology, never a mistake.
 *
 * Rules:
 *  - Dev (vite dev server): '' + vite.config.ts proxy to 127.0.0.1:8000.
 *  - Worker deployment: '' (worker proxies). No configuration needed.
 *  - Direct-call deployments: set VITE_API_BASE_URL to the backend origin
 *    (https REQUIRED in production — an http:// value here would be blocked
 *    as mixed content on the https dashboard; we demote it to '' and warn
 *    instead of shipping a request that can never fire).
 */

const raw = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/+$/, '')

const IS_PROD = import.meta.env.PROD
const looksLikeInsecureHttp = raw.startsWith('http://')

// Block mixed content: an http:// base would be silently blocked by browsers
// (which is precisely how the "backend is not running" bug manifests). Same
// WSecure default applies to http://localhost — set the env var with https
// or use the worker-proxy mode.
export const API_BASE_URL: string = IS_PROD && looksLikeInsecureHttp ? '' : raw

export const MIXED_CONTENT_MASKED: boolean = IS_PROD && looksLikeInsecureHttp && raw !== ''

export const IS_SAME_ORIGIN: boolean = API_BASE_URL === ''
