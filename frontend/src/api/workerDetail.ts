/**
 * Pull the actionable `detail` out of a failed Worker/API response.
 *
 * The Worker's fail-closed shape is
 * `{ error: "routellm_backend_not_configured", detail: "Set ROUTELLM_API_ORIGIN …" }`.
 * That detail is the deployment fix, so it must reach the user.
 */

export function extractWorkerDetail(err: unknown): string | undefined {
  const data = (err as { response?: { data?: unknown } })?.response?.data
  if (data && typeof data === 'object') {
    const record = data as { detail?: unknown; error?: unknown }
    if (typeof record.detail === 'string' && record.detail.trim() !== '') {
      return record.detail
    }
    if (typeof record.error === 'string' && record.error.trim() !== '') {
      return record.error
    }
  }
  return undefined
}

