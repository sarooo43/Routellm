import { describe, expect, it } from 'vitest'
import { toApiError } from './errors'

/** Axios-style wrapper for building response-carrying errors in tests. */
function httpError(status: number, data: unknown) {
  return { response: { status, headers: {}, data } }
}

describe('toApiError', () => {
  it('surfaces the Host-guard 400 detail verbatim so the fix is readable', () => {
    const detail =
      "Invalid Host header 'stranger.app.github.dev'. This backend only accepts "
      + 'localhost, 127.0.0.1, ::1 and its configured host. Fix: add '
      + 'ALLOWED_HOSTS=<your-host> in .env and restart (or ALLOWED_HOSTS=* behind '
      + 'a trusted proxy).'
    const err = toApiError(httpError(400, { detail }))
    expect(err.kind).toBe('validation')
    expect(err.serverDetail).toContain('Invalid Host header')
    expect(err.serverDetail).toContain('ALLOWED_HOSTS')
  })
})
