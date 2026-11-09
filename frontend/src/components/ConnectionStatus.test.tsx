/**
 * The header pill must not contradict a page that just failed.
 *
 * Field report: a page said "backend is not running" while the header still
 * read "Online · 3ms" from a probe issued before the outage. The pill only
 * polls every 15s, so a failed request has to push it to re-check immediately.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import axios from 'axios'
import { ConnectionStatus } from './ConnectionStatus'
import { requestHealthReprobe, useReprobeStore } from '../api/wake'

vi.mock('axios', async () => {
  const get = vi.fn()
  const isAxiosError = (e: unknown) =>
    Boolean(e && typeof e === 'object' && (e as { isAxiosError?: boolean }).isAxiosError)
  return { default: { get, isAxiosError }, isAxiosError }
})

const mockedGet = vi.mocked(axios.get)

/** The pill publishes its state as a data attribute — query that, not text. */
function pillState(): string | null {
  return screen.getByTestId('connection-status').getAttribute('data-state')
}

async function expectState(state: string) {
  await waitFor(() => expect(pillState()).toBe(state))
}

/**
 * The backend's current answer, flipped by each test. State-driven rather than
 * call-order-driven: React StrictMode mounts effects twice in development, so
 * "the next call fails" is not a stable way to model an outage.
 */
let backendMode: 'up' | 'down' | 'degraded' = 'up'

function answer() {
  if (backendMode === 'down') return Promise.reject(new Error('Network Error'))
  return Promise.resolve({
    data: {
      status: 'ok',
      components: { redis: 'ok', database: backendMode === 'degraded' ? 'down' : 'ok' },
    },
  })
}

describe('ConnectionStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    backendMode = 'up'
    useReprobeStore.setState({ nonce: 0 })
    mockedGet.mockImplementation(() => answer())
  })

  it('shows the measured latency when the backend answers', async () => {
    render(<ConnectionStatus />)
    await expectState('online')
    expect(mockedGet).toHaveBeenCalledWith('/health', expect.anything())
  })

  it('re-probes immediately when a request reports a transport failure', async () => {
    render(<ConnectionStatus />)
    await expectState('online')
    const before = mockedGet.mock.calls.length

    // A page's request just failed: the pill must not wait for the 15s poll.
    backendMode = 'down'
    requestHealthReprobe()

    await waitFor(() => expect(mockedGet.mock.calls.length).toBeGreaterThan(before))
    await expectState('offline')
  })

  it('returns to Online once the backend answers again', async () => {
    render(<ConnectionStatus />)
    await expectState('online')

    backendMode = 'down'
    requestHealthReprobe()
    await expectState('offline')

    backendMode = 'up'
    requestHealthReprobe()
    await expectState('online')
  })

  it('reports Degraded when a component is down but the app answers', async () => {
    backendMode = 'degraded'
    render(<ConnectionStatus />)
    await expectState('degraded')
  })
})
