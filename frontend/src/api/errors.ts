/**
 * Typed API error taxonomy — replaces the old one-size-fits-all
 * "Is the backend running?" message with real diagnostics.
 *
 * `kind` drives WHAT the user is told:
 *  - network / cors_or_unreachable → "Backend is not reachable" (with host
 *    info + Retry) — ONLY these two kinds mean the wording is accurate;
 *  - unauthorized / forbidden → "Add or generate an API key" with a link;
 *  - rate_limited → includes retry-after when the server sends it;
 *  - service_unavailable → "Database temporarily unavailable" + auto-retry;
 *  - not_found / validation / server_error → actionable specifics.
 */

export type ApiErrorKind =
  | 'network'
  | 'cors_or_unreachable'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'rate_limited'
  | 'service_unavailable'
  | 'server_error'
  | 'validation'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly status?: number
  /** Seconds to wait before retrying — present on 429 with Retry-After. */
  readonly retryAfterSeconds?: number
  /** Server-provided human detail (e.g. FastAPI "detail" field). */
  readonly serverDetail?: string

  constructor(kind: ApiErrorKind, message: string, status?: number, retryAfterSeconds?: number, serverDetail?: string) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.retryAfterSeconds = retryAfterSeconds
    this.serverDetail = serverDetail
  }
}

/** Axios-style error: has an HTTP response attached. */
interface WithResponse {
  response?: { status: number; headers?: unknown; data?: unknown }
}

/**
 * Flatten any thrown value into an ApiError. Network-shaped failures (no
 * response object at all) are split into `network` vs `cors_or_unreachable`
 * heuristically: ERR_NETWORK/timeout codes are real transport failures,
 * anything else with no response is most commonly a CORS block or a dead
 * hostname.
 */
export function toApiError(err: unknown): ApiError {
  const withResponse = err as WithResponse | undefined
  const response = withResponse?.response
  const code = (err as { code?: string } | null | undefined)?.code
  const serverDetail = extractDetail(response?.data)

  if (!response) {
    if (code === 'ECONNABORTED' || code === 'ETIMEDOUT') {
      return new ApiError('network', 'The request timed out before the backend answered.')
    }
    return new ApiError('cors_or_unreachable', 'The backend could not be reached from this page.')
  }

  switch (response.status) {
    case 400:
      return new ApiError('validation', 'The request was not valid.', 400, undefined, serverDetail)
    case 401:
      return new ApiError('unauthorized', 'Add or generate an API key to continue.', 401, undefined, serverDetail)
    case 403:
      return new ApiError('forbidden', 'This key does not have access to that action.', 403, undefined, serverDetail)
    case 404:
      return new ApiError('not_found', 'That resource does not exist (it may be disabled on this deployment).', 404, undefined, serverDetail)
    case 429: {
      const retryAfter = parseRetryAfter(response.headers)
      return new ApiError(
        'rate_limited',
        retryAfter
          ? `Rate limited — retry in ~${retryAfter}s.`
          : 'Rate limited — too many requests, slow down and retry.',
        429,
        retryAfter,
      )
    }
    case 502:
    case 503:
      return new ApiError(
        'service_unavailable',
        'The service is temporarily unavailable.',
        response.status,
        undefined,
        serverDetail,
      )
    default:
      if (response.status >= 500) {
        return new ApiError('server_error', `Backend error (${response.status}).`, response.status, undefined, serverDetail)
      }
      return new ApiError('validation', `Request failed (${response.status}).`, response.status, undefined, serverDetail)
  }
}

function parseRetryAfter(headers: unknown): number | undefined {
  const raw = (headers as Record<string, unknown> | undefined)?.['retry-after']
  const seconds = Number(raw)
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : undefined
}

function extractDetail(data: unknown): string | undefined {
  if (typeof data === 'object' && data !== null && data !== undefined) {
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    // Wrangler fail-closed shape: { error: ..., detail: ... } — covered above;
    // OpenAI/other shapes fall through to undefined.
  }
  if (typeof data === 'string') return data
  return undefined
}

/** True network/reachability kinds — the ONLY cases where "not reachable"
 *  wording is accurate. */
export function isReachabilityKind(kind: ApiErrorKind): boolean {
  return kind === 'network' || kind === 'cors_or_unreachable'
}
