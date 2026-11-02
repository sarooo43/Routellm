import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import { ConnectionStatus } from './ConnectionStatus'

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

  it('shows red Offline when /health is unreachable', async () => {
    getMock.mockRejectedValue(new Error('Network Error'))
    render(<ConnectionStatus />)
    const pill = await screen.findByTestId('connection-status')
    await waitFor(() => expect(pill.dataset.state).toBe('offline'))
    expect(pill.textContent).toContain('Offline')
  })
})
