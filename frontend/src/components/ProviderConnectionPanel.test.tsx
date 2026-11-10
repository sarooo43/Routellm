import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getProviderModels, testProvider } from '../api/client'
import type { ProviderTestResult } from '../types/api'
import { ProviderConnectionPanel } from './ProviderConnectionPanel'

vi.mock('../api/client', () => ({
  testProvider: vi.fn(),
  getProviderModels: vi.fn(),
}))

const base: ProviderTestResult = {
  provider: 'groq',
  display: 'Groq',
  verdict: 'valid',
  key_valid: true,
  model: 'llama-3.1-8b-instant',
  latency_ms: 123.4,
  error: null,
  available_models: ['llama-3.1-8b-instant', 'openai/gpt-oss-120b'],
  model_source: 'discovered',
}

function renderPanel(overrides: Partial<ProviderTestResult> = {}, enabled = true) {
  vi.mocked(testProvider).mockResolvedValue({ ...base, ...overrides })
  render(
    <ProviderConnectionPanel apiKey="rk_testkey" provider="groq" display="Groq" enabled={enabled} />,
  )
}

beforeEach(() => {
  vi.mocked(getProviderModels).mockResolvedValue(['llama-3.1-8b-instant'])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ProviderConnectionPanel', () => {
  it('does not claim anything before a test is run', () => {
    renderPanel()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText(/Key works/)).not.toBeInTheDocument()
  })

  it('reports the model actually used when the key works', async () => {
    renderPanel()
    fireEvent.click(screen.getByLabelText('Test Groq connection'))

    expect(await screen.findByText(/Key works/)).toBeInTheDocument()
    // Once in the "Using model" line, once as the selected picker option.
    expect(screen.getAllByText('llama-3.1-8b-instant').length).toBeGreaterThan(0)
    expect(screen.getByText(/123 ms/)).toBeInTheDocument()
    expect(screen.getByLabelText('Groq connection status')).toHaveTextContent('Works')
    expect(testProvider).toHaveBeenCalledWith('rk_testkey', 'groq')
  })

  it('calls a 404 a model problem, never a key problem', async () => {
    renderPanel({
      verdict: 'model_not_found',
      key_valid: false,
      model: 'llama-3.3-70b-versatile',
      error:
        "model_not_found: groq (api.groq.com) has no model named 'llama-3.3-70b-versatile'. " +
        'The URL and your key are fine.',
    })
    fireEvent.click(screen.getByLabelText('Test Groq connection'))

    const badge = await screen.findByLabelText('Groq connection status')
    expect(badge).toHaveTextContent('Model unavailable')
    expect(badge).not.toHaveTextContent('Key rejected')
    expect(screen.getByText(/has no model named/)).toBeInTheDocument()
    // The key is explicitly absolved.
    expect(screen.getByText(/your key are fine/)).toBeInTheDocument()
  })

  it('only blames the key for auth_failed', async () => {
    renderPanel({
      verdict: 'auth_failed',
      key_valid: false,
      error: 'auth_failed: groq (api.groq.com) rejected the credential (HTTP 401).',
    })
    fireEvent.click(screen.getByLabelText('Test Groq connection'))
    expect(await screen.findByLabelText('Groq connection status')).toHaveTextContent('Key rejected')
  })

  it('offers the discovered models when the model is the problem', async () => {
    renderPanel({
      verdict: 'model_not_found',
      key_valid: false,
      available_models: ['llama-3.1-8b-instant', 'gemma2-9b-it'],
      error: "model_not_found: groq has no model named 'gone'.",
    })
    fireEvent.click(screen.getByLabelText('Test Groq connection'))

    const picker = await screen.findByLabelText('Groq model')
    expect(picker).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'gemma2-9b-it' })).toBeInTheDocument()
    expect(screen.getByText(/GROQ_MODEL/)).toBeInTheDocument()
  })

  it('shows a network failure as unreachable, not as a bad key', async () => {
    renderPanel({
      verdict: 'network_error',
      key_valid: false,
      error: 'network_error: could not open a connection to api.groq.com.',
    })
    fireEvent.click(screen.getByLabelText('Test Groq connection'))
    expect(await screen.findByLabelText('Groq connection status')).toHaveTextContent(
      'Cannot reach provider',
    )
    expect(screen.queryByText(/Key rejected/)).not.toBeInTheDocument()
  })

  it('does not paint the provider red when the dashboard itself fails', async () => {
    vi.mocked(testProvider).mockRejectedValue(new Error('dashboard offline'))
    render(
      <ProviderConnectionPanel apiKey="rk_testkey" provider="groq" display="Groq" enabled />,
    )
    fireEvent.click(screen.getByLabelText('Test Groq connection'))

    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not run the test/)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('cannot test without a saved key', () => {
    renderPanel({}, false)
    expect(screen.getByLabelText('Test Groq connection')).toBeDisabled()
    fireEvent.click(screen.getByLabelText('Test Groq connection'))
    expect(testProvider).not.toHaveBeenCalled()
  })

  it('forgets the previous verdict when the key changes', async () => {
    vi.mocked(testProvider).mockResolvedValue(base)
    const { rerender } = render(
      <ProviderConnectionPanel apiKey="rk_first" provider="groq" display="Groq" enabled />,
    )
    fireEvent.click(screen.getByLabelText('Test Groq connection'))
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())

    rerender(<ProviderConnectionPanel apiKey="rk_second" provider="groq" display="Groq" enabled />)
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
  })
})
