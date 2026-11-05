import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import { ConnectionStatus } from './ConnectionStatus'
import { extractWorkerDetail } from '../api/workerDetail'

vi.mock('axios')

// Framer-motion / polling: ConnectionStatus probes /health once on mount,
// then every 15 s. These tests assert the FIRST probe only (mount-time),
// so no fake timers are needed.
const getMock = axios.get as unknown as ReturnType<typeof vi.fn>

beforeEach(() => {
  getMock.mockReset()
})

afterEach(() => cleanup())

describe('ConnectionStatus', () => {
  it('shows green Online with latency when /health is healthy', async () => {
    getMock.mockResolvedValue({ data: { components: { database: 'ok', redis: 'ok' } } })
    render(<ConnectionStatus />)
    const pill = await screen.findByTestId('connection-status')
    await waitFor(() => expect(pill.dataset.state).toBe('online'))
    expect(pill.textContent).toMatch(/Online · \d+ms/)
  })

  it('shows amber Degraded when a component is down', async () => {
    getMock.mockResolvedValue({ data: { components: { database: 'down', redis: 'ok' } } })
    render(<ConnectionStatus />)
    const pill = await screen.findByTestId('connection-status')
    await waitFor(() => expect(pill.dataset.state).toBe('degraded'))
    expect(pill.textContent).toContain('Degraded')
  })

  it("surfaces the Worker's configuration detail instead of a bare Offline", async () => {
    // Exactly what the deployed Worker returns when ROUTELLM_API_ORIGIN is
    // unset: { error: "routellm_backend_not_configured", detail: "Set …" }
    getMock.mockRejectedValue({
      response: {
        status: 503,
        data: {
          error: 'routellm_backend_not_configured',
          detail: 'Set ROUTELLM_API_ORIGIN to the HTTPS origin of the RouteLLM FastAPI backend.',
        },
      },
    })
    render(<ConnectionStatus />)
    const pill = await screen.findByTestId('connection-status')
    await waitFor(() => expect(pill.dataset.state).toBe('offline'))
    // The fix must be visible without hovering a tooltip.
    expect(screen.getByTestId('connection-status-detail').textContent).toContain(
      'ROUTELLM_API_ORIGIN',
    )
  })

  it('shows red Offline when /health is unreachable', async () => {
    getMock.mockRejectedValue(new Error('Network Error'))
    render(<ConnectionStatus />)
    const pill = await screen.findByTestId('connection-status')
    await waitFor(() => expect(pill.dataset.state).toBe('offline'))
    expect(pill.textContent).toContain('Offline')
  })
})

describe('extractWorkerDetail', () => {
  it('prefers detail over error', () => {
    expect(
      extractWorkerDetail({
        response: { data: { error: 'routellm_backend_not_configured', detail: 'Set X' } },
      }),
    ).toBe('Set X')
  })

  it('falls back to the error code', () => {
    expect(
      extractWorkerDetail({ response: { data: { error: 'routellm_backend_unreachable' } } }),
    ).toBe('routellm_backend_unreachable')
  })

  it('returns undefined for plain network errors', () => {
    expect(extractWorkerDetail(new Error('Network Error'))).toBeUndefined()
    expect(extractWorkerDetail(undefined)).toBeUndefined()
    expect(extractWorkerDetail({ response: { data: 'not json' } })).toBeUndefined()
  })
})
