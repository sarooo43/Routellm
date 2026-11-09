/**
 * Typed API error taxonomy — replaces the old one-size-fits-all
 * "Is the backend running?" message with real diagnostics.
 *
 * `kind` drives WHAT the user is told:
 *  - network / cors_or_unreachable → "No response from the backend" (with
 *    host info + Retry) — ONLY these two kinds mean the wording is accurate,
 *    and it must not assert that the process is dead: it usually is not;
 *  - unauthorized / forbidden → "Add or generate an API key" with a link;
 *  - rate_limited → includes retry-after when the server sends it;
 *  - service_unavailable → "Database temporarily unavailable" + auto-retry;
 *  - not_found / validation / server_error → actionable specifics.
 *
 * The server's own `detail` is always preferred over a generic sentence: the
 * backend usually knows exactly what is wrong ("Provider key must be between 8
 * and 512 characters"), and paraphrasing that into "Request failed" throws away
 * the only actionable part.
 */

/** True only for the two kinds that mean "nothing answered". */
export function isReachabilityKind(kind: ApiErrorKind): boolean {
  return kind === 'network' || kind === 'cors_or_unreachable'
}

export type ApiErrorKind =
  | 'network'
  | 'cors_or_unreachable'
  | 'backend_waking'
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
 * Pull a readable sentence out of FastAPI/Pydantic error bodies.
 *
 * 422 is the awkward one: the endpoint's own `detail` is a string, but an
 * automatic request-validation failure supplies a list of objects with `loc`
 * and `msg`, which stringifies to noise. Both shapes are handled so the message
 * names the offending field instead of printing `[object Object]`.
 */
function extractDetail(data: unknown): string | undefined {
  if (typeof data === 'string') return data
  if (!data || typeof data !== 'object') return undefined
  const maybeDetail = (data as { detail?: unknown }).detail
  if (typeof maybeDetail === 'string') return maybeDetail
  if (Array.isArray(maybeDetail)) {
    const parts = maybeDetail
      .map((entry) => {
        if (typeof entry === 'string') return entry
        if (entry && typeof entry === 'object') {
          const { loc, msg } = entry as { loc?: unknown[]; msg?: unknown }
          const field = Array.isArray(loc)
            ? loc.filter((p) => p !== 'body' && p !== 'query').join('.')
            : ''
          if (typeof msg === 'string') return field ? `${field}: ${msg}` : msg
        }
        return null
      })
      .filter((p): p is string => Boolean(p))
    if (parts.length) return parts.join('; ')
  }
  return undefined
}

function parseRetryAfter(headers: unknown): number | undefined {
  const raw = (headers as { 'retry-after'?: unknown } | undefined)?.['retry-after']
  const value = Array.isArray(raw) ? raw[0] : raw
  const seconds = Number(value)
  // Round up: telling someone to retry in 0s when 0.4s remains is pointless.
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : undefined
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
    case 422:
      // The server explains what was wrong with the request; repeat it.
      return new ApiError(
        'validation',
        serverDetail ?? 'The request was not valid.',
        response.status,
        undefined,
        serverDetail,
      )
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
        serverDetail,
      )
    }
    case 502:
    case 503: {
      // Worker-proxy cold-start signature: {"detail": ..., "upstream":"down"}
      // — the Hugging Face Space is asleep/booting, NOT a server crash. This
      // must NEVER be worded as "backend not reachable"; the wake-retry layer
      // handles it with backoff.
      const data = response.data as { upstream?: unknown; detail?: unknown } | undefined
      if (data && data.upstream === 'down') {
        return new ApiError(
          'backend_waking',
          'Waking up the backend — this can take up to a minute.',
          response.status,
          undefined,
          typeof data.detail === 'string' ? data.detail : undefined,
        )
      }
      return new ApiError(
        'service_unavailable',
        serverDetail ?? 'The service is temporarily unavailable.',
        response.status,
        undefined,
        serverDetail,
      )
    }
    default: {
      if (response.status >= 500) {
        return new ApiError('server_error', serverDetail ?? `Backend error (${response.status}).`, response.status, undefined, serverDetail)
      }
      return new ApiError('validation', serverDetail ?? `Request failed (${response.status}).`, response.status, undefined, serverDetail)
    }
  }
}
