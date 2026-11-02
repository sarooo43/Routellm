import { AlertTriangle, KeyRound, RefreshCw, ShieldOff, WifiOff, Clock } from 'lucide-react'
import type { ApiErrorKind } from '../api/errors'
import { API_BASE_URL, IS_SAME_ORIGIN } from '../config'

/**
 * Reusable, taxonomy-aware error state used by every page.
 *
 * "Backend is not reachable" wording appears ONLY for genuinely
 * network-shaped failures (kind network / cors_or_unreachable) — 401/403
 * tell the user to add a key, 503 to pause, and specific kinds keep their
 * own actionable copy. `detail` (server detail, if any) is shown as muted
 * text — a better message must never hide the info the server gave us.
 */

interface ErrorStateProps {
  kind: ApiErrorKind
  /** Server-provided detail (e.g. FastAPI "detail" or Worker 503 detail). */
  detail?: string
  onRetry?: () => void
  retrying?: boolean
}

const KIND_META: Record<ApiErrorKind, { title: string; icon: typeof WifiOff; tone: 'danger' | 'warn' | 'neutral' }> = {
  network: { title: 'Backend is not reachable', icon: WifiOff, tone: 'danger' },
  cors_or_unreachable: { title: 'Backend is not reachable', icon: WifiOff, tone: 'danger' },
  unauthorized: { title: 'Add or generate an API key', icon: KeyRound, tone: 'neutral' },
  forbidden: { title: 'This key lacks access', icon: ShieldOff, tone: 'warn' },
  not_found: { title: 'Not available on this deployment', icon: AlertTriangle, tone: 'neutral' },
  rate_limited: { title: 'Rate limited', icon: Clock, tone: 'warn' },
  service_unavailable: { title: 'Database temporarily unavailable', icon: Clock, tone: 'warn' },
  server_error: { title: 'Backend error', icon: AlertTriangle, tone: 'danger' },
  validation: { title: 'Request failed', icon: AlertTriangle, tone: 'neutral' },
}

const TONE_STYLES: Record<'danger' | 'warn' | 'neutral', string> = {
  danger: 'border-danger/30 bg-danger/10 text-danger',
  warn: 'border-warning/30 bg-warning/10 text-warning',
  neutral: 'border-white/10 bg-white/[0.04] text-text-primary',
}

export function ErrorState({ kind, detail, onRetry, retrying }: ErrorStateProps) {
  const meta = KIND_META[kind]
  const Icon = meta.icon
  const body = kindBody(kind, detail)

  return (
    <div
      role="alert"
      data-testid={`error-state-${kind}`}
      className={`rounded-xl border px-4 py-4 sm:px-5 ${TONE_STYLES[meta.tone]}`}
    >
      <div className="flex flex-wrap items-start gap-3">
        <Icon size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold leading-snug">{meta.title}</p>
          <p className="mt-1 break-words text-xs leading-relaxed text-text-secondary">{body}</p>
          {kind === 'unauthorized' && (
            <a
              href="/keys"
              className="mt-2 inline-flex items-center gap-1.5 text-xs font-semibold text-accent underline-offset-4 hover:underline"
            >
              <KeyRound size={12} aria-hidden="true" /> Go to API Keys
            </a>
          )}
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              disabled={retrying}
              data-testid="error-state-retry"
              className="rl-btn-ghost ml-0 mt-2 border border-white/[0.08] text-xs disabled:opacity-50"
            >
              <RefreshCw size={12} aria-hidden="true" className={retrying ? 'animate-spin' : undefined} />
              {retrying ? 'Retrying…' : 'Retry'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function kindBody(kind: ApiErrorKind, detail?: string): string {
  switch (kind) {
    case 'network':
    case 'cors_or_unreachable':
      return IS_SAME_ORIGIN
        ? `Requests go same-origin here — if this is a Cloudflare Worker deployment, ROUTELLM_API_ORIGIN must point at your FastAPI backend. Target: ${typeof window !== 'undefined' ? window.location.host : 'unknown host'}${API_BASE_URL || ' /api'}`
        : `Configured API host: ${API_BASE_URL}. Check that it is reachable and served over HTTPS.`
    case 'unauthorized':
      return 'Generate a key on the API Keys page (free, one click) — or select a previously saved one.'
    case 'forbidden':
      return 'The active RouteLLM key is not allowed to perform this action.'
    case 'rate_limited':
      return 'Too many requests in a short window. Wait a moment and retry.'
    case 'service_unavailable':
      return 'The database behind this endpoint is down or starting up. Retrying usually fixes it.'
    default:
      return detail ?? 'Something went wrong while talking to the backend.'
  }
}
