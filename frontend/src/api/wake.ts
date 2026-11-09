/**
 * Cold-start (waking) retry window shared by all data loaders.
 *
 * Hugging Face Spaces on the free tier sleep; the Cloudflare Worker proxy
 * reports that condition as 503 {"upstream":"down"} (or a request times out
 * while the Space is booting). Instead of a generic error, data loaders wrap
 * themselves in runWithWakeRetry: it retries with exponential backoff for up
 * to ~60s total while publishing Waking-state to useWakeStore, which the
 * header pill (ConnectionStatus) and any error UI can render.
 */

import { create } from 'zustand'
import type { ApiError } from './errors'

export const WAKE_BUDGET_MS = 60_000
/** 1s, 2s, 4s, 8s, 16s, 20s → cumulative ~51s of waits inside a 60s budget. */
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 20_000]

interface WakeState {
  /** True while a loader is inside the wake-retry window. */
  waking: boolean
  /** Zero-based retry attempt currently pending (0 while first in flight). */
  attempt: number
  set: (update: { waking: boolean; attempt: number }) => void
}

/**
 * Bumped whenever a real request fails with a transport-shaped error.
 *
 * The health pill polls every 15s, so without this the UI can contradict
 * itself: a page says "no response from the backend" while the header still
 * reads "Online · 3ms" from a probe issued before the outage. Data loaders call
 * requestHealthReprobe(); the pill re-probes immediately when the counter
 * changes.
 */
export const useReprobeStore = create<{ nonce: number; bump: () => void }>((set) => ({
  nonce: 0,
  bump: () => set((state) => ({ nonce: state.nonce + 1 })),
}))

/** Ask the connectivity pill to check now instead of at the next poll. */
export function requestHealthReprobe(): void {
  useReprobeStore.getState().bump()
}

export const useWakeStore = create<WakeState>((set) => ({
  waking: false,
  attempt: 0,
  set: (update) => set(update),
}))

export function isWakingCondition(err: unknown): err is ApiError {
  const apiError = err as ApiError | undefined
  if (!apiError || typeof apiError !== 'object' || typeof apiError.kind !== 'string') {
    return false
  }
  // The proxy explicitly told us the Space is down/cold…
  if (apiError.kind === 'backend_waking') return true
  // …or the request died before ANY response arrived while a wake window is
  // plausible: axios timeout / connection failure shapes.
  if (apiError.kind === 'network') return true
  return false
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Run `loader`, retrying wake-shaped failures with backoff up to ~60s total.
 * Unknown/other failures propagate immediately (no retry). Last error still
 * propagates after the budget is exhausted so callers can render the
 * "Waking up" error state with a manual Retry button.
 */
export async function runWithWakeRetry<T>(loader: () => Promise<T>): Promise<T> {
  const startedAt = Date.now()
  const wake = useWakeStore.getState()
  wake.set({ waking: true, attempt: 0 })

  try {
    // Attempt 0 — the loader itself decides whether its error is wake-shaped.
    // We retry on EVERY waking-shaped failure with the documented backoff.
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await loader()
      } catch (err) {
        const exhausted = Date.now() - startedAt >= WAKE_BUDGET_MS
        if (!isWakingCondition(err) || exhausted) {
          throw err
        }
      }
      const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]
      useWakeStore.getState().set({ waking: true, attempt: attempt + 1 })
      await sleep(wait)
    }
  } finally {
    useWakeStore.getState().set({ waking: false, attempt: 0 })
  }
}
