import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, isReachabilityKind, toApiError } from './errors'

function axiosLike(status: number, retryAfter?: string) {
  return {
    code: undefined,
    response: {
      status,
      headers: retryAfter ? { 'retry-after': retryAfter } : {},
      data: { detail: 'server said' },
    },
  }
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('toApiError', () => {
  it('maps 401 to unauthorized with key guidance', () => {
    const err = toApiError(axiosLike(401))
    expect(err.kind).toBe('unauthorized')
    expect(err.message.toLowerCase()).toContain('api key')
  })

  it('maps 403 to forbidden', () => {
    expect(toApiError(axiosLike(403)).kind).toBe('forbidden')
  })

  it('maps 404 to not_found', () => {
    expect(toApiError(axiosLike(404)).kind).toBe('not_found')
  })

  it('maps 429 to rate_limited and parses Retry-After', () => {
    const err = toApiError(axiosLike(429, '7'))
    expect(err.kind).toBe('rate_limited')
    expect(err.retryAfterSeconds).toBe(7)
    expect(err.message).toContain('7s')
  })

  it('maps 503 to service_unavailable and carries server detail', () => {
    const err = toApiError(axiosLike(503))
    expect(err.kind).toBe('service_unavailable')
    expect(err.serverDetail).toBe('server said')
  })

  it('maps 500 to server_error', () => {
    expect(toApiError(axiosLike(500)).kind).toBe('server_error')
  })

  it('maps 400 to validation', () => {
    expect(toApiError(axiosLike(400)).kind).toBe('validation')
  })

  it('classifies timeout codes as network', () => {
    const err = toApiError({ code: 'ECONNABORTED' })
    expect(err.kind).toBe('network')
    expect(isReachabilityKind(err.kind)).toBe(true)
  })

  it('classifies no-response failures as cors_or_unreachable', () => {
    const err = toApiError(new Error('Network Error'))
    expect(err.kind).toBe('cors_or_unreachable')
    expect(isReachabilityKind(err.kind)).toBe(true)
    // The reachability wording is ONLY accurate for these two kinds:
    expect(isReachabilityKind('unauthorized')).toBe(false)
    expect(isReachabilityKind('service_unavailable')).toBe(false)
  })
})

describe('ApiError', () => {
  it('is an Error subclass with kind metadata', () => {
    const err = new ApiError('unauthorized', 'msg', 401, 3, 'detail')
    expect(err).toBeInstanceOf(Error)
    expect(err.kind).toBe('unauthorized')
    expect(err.status).toBe(401)
    expect(err.retryAfterSeconds).toBe(3)
    expect(err.serverDetail).toBe('detail')
  })
})
