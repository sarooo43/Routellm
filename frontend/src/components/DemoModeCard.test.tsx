import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RouteResponse } from '../types/api'
import { DemoModeCard } from './DemoModeCard'

const postRoute = vi.fn()

vi.mock('../api/client', () => ({
  postRoute: (payload: unknown) => postRoute(payload),
}))

function routeResponse(overrides: Partial<RouteResponse> = {}): RouteResponse {
  return {
    success: true,
    result: '[DEMO — synthetic response, no model was called]\n\nSynthetic completion.',
    model_used: 'mock',
    confidence_score: 0.2,
    fallback_chain_attempted: [],
    latency_ms: 341,
    tokens_used: 42,
    request_id: 'req_demo',
    error: null,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  postRoute.mockReset()
})

describe('DemoModeCard', () => {
  it('labels itself MOCK before anything is sent', () => {
    render(
      <MemoryRouter>
        <DemoModeCard available />
      </MemoryRouter>,
    )
    expect(screen.getByText('MOCK')).toBeInTheDocument()
    expect(screen.getByText(/no key, no network call, no spend/i)).toBeInTheDocument()
  })

  it('forces the mock provider explicitly — never relies on automatic routing', async () => {
    postRoute.mockResolvedValue(routeResponse())
    render(
      <MemoryRouter>
        <DemoModeCard available providerName="mock" />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /send a demo prompt/i }))
    await waitFor(() => expect(postRoute).toHaveBeenCalledTimes(1))
    expect(postRoute.mock.calls[0][0]).toMatchObject({ force_provider: 'mock' })
  })

  it('shows the marked response and marks it as synthetic', async () => {
    postRoute.mockResolvedValue(routeResponse())
    render(
      <MemoryRouter>
        <DemoModeCard available />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /send a demo prompt/i }))
    expect(await screen.findByText(/synthetic response, no model was called/i)).toBeInTheDocument()
    expect(screen.getByText(/synthetic · not benchmarked/i)).toBeInTheDocument()
    expect(screen.getByText('341 ms')).toBeInTheDocument()
  })

  it('explains itself instead of failing when a real key is configured', () => {
    render(
      <MemoryRouter>
        <DemoModeCard available={false} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('button', { name: /send a demo prompt/i })).not.toBeInTheDocument()
    expect(screen.getByText(/real provider key is configured/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /providers page/i })).toHaveAttribute('href', '/providers')
  })

  it('surfaces a failure without blaming the backend generically', async () => {
    postRoute.mockRejectedValue({ response: { status: 503, data: { detail: 'Database temporarily unavailable' } } })
    render(
      <MemoryRouter>
        <DemoModeCard available />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /send a demo prompt/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/temporarily unavailable/i)
    expect(screen.queryByText(/no response from the backend/i)).not.toBeInTheDocument()
  })
})
