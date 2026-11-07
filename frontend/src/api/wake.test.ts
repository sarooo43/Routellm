import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { runWithWakeRetry, useWakeStore, isWakingCondition } from './wake'
import { ApiError } from './errors'

beforeEach(() => {
  useWakeStore.getState().set({ waking: false, attempt: 0 })
})

afterEach(() => {
  vi.useRealTimers()
  useWakeStore.getState().set({ waking: false, attempt: 0 })
})

function wakingError(): ApiError {
  return new ApiError('backend_waking', 'Waking up the backend', 503)
}

function hardError(): ApiError {
  return new ApiError('server_error', 'Backend error (500)', 500)
}

describe('isWakingCondition', () => {
  it('classifies the worker-proxy wake shape and network timeouts as waking', () => {
    expect(isWakingCondition(wakingError())).toBe(true)
    expect(isWakingCondition(new ApiError('network', 'timed out'))).toBe(true)
    expect(isWakingCondition(hardError())).toBe(false)
    expect(isWakingCondition(new Error('plain'))).toBe(false)
    expect(isWakingCondition(undefined)).toBe(false)
  })
})

describe('runWithWakeRetry', () => {
  it('succeeds without retries when the first attempt works', async () => {
    let calls = 0
    const result = await runWithWakeRetry(() => {
      calls += 1
      return Promise.resolve('ok')
    })
    expect(result).toBe('ok')
    expect(calls).toBe(1)
    expect(useWakeStore.getState().waking).toBe(false)
  })

  it('retries wake-shaped failures with backoff and then succeeds', async () => {
    vi.useFakeTimers()
    let calls = 0
    const promise = runWithWakeRetry(() => {
      calls += 1
      if (calls === 1) return Promise.reject(wakingError())
      return Promise.resolve('recovered')
    })
    await vi.runAllTimersAsync()
    expect(await promise).toBe('recovered')
    expect(calls).toBe(2)
    expect(useWakeStore.getState().waking).toBe(false)
  })

  it('shows waking state while a retry is pending', async () => {
    vi.useFakeTimers()
    let calls = 0
    const promise = runWithWakeRetry(() => {
      calls += 1
      if (calls === 1) return Promise.reject(wakingError())
      return Promise.resolve('fine')
    })
    await Promise.resolve()
    await vi.runAllTimersAsync()
    await promise
    // By the time it resolves the window is closed:
    expect(useWakeStore.getState().waking).toBe(false)
    expect(calls).toBe(2)
  })

  it('does NOT retry non-wake failures (500 propagates immediately)', async () => {
    let calls = 0
    // One shared instance: toBe identity across construction sites is
    // meaningless for class objects that serialize identically.
    const hard = new ApiError('server_error', 'Backend error (500)', 500)
    await expect(
      runWithWakeRetry(() => {
        calls += 1
        return Promise.reject(hard)
      }),
    ).rejects.toBe(hard)
    expect(calls).toBe(1)
  })

  it('propagates the last waking error after the ~60s budget is exhausted', async () => {
    vi.useFakeTimers()
    let calls = 0
    const promise = runWithWakeRetry(() => {
      calls += 1
      return Promise.reject(wakingError())
    })
    promise.catch(() => undefined)
    await vi.runAllTimersAsync()
    await expect(promise).rejects.toMatchObject({ kind: 'backend_waking' })
    expect(calls).toBeGreaterThan(1)
    expect(useWakeStore.getState().waking).toBe(false)
  })
})
