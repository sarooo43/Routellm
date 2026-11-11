import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useSearchParams } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  ProviderModelInfo,
  ProviderModelsResponse,
} from '../types/api'
import { ModelExplorerPanel } from './ModelExplorerPanel'

vi.mock('../api/client', () => ({
  getProviderModels: vi.fn(),
  testProviderModel: vi.fn(),
  testProvider: vi.fn(),
  setPinnedModel: vi.fn(),
}))

import {
  getProviderModels,
  setPinnedModel,
  testProvider,
  testProviderModel,
} from '../api/client'

// A secret-shaped RouteLLM key: the panel receives it to authenticate, but it
// must never appear anywhere in the rendered DOM.
const SECRET = 'rk_secretvalue_nevershowinpanel'

function makeModel(overrides: Partial<ProviderModelInfo> = {}): ProviderModelInfo {
  return {
    id: 'llama-3.3-70b-versatile',
    display_name: 'Llama 3.3 70B Versatile',
    released_or_version: '2024-12-17',
    sort_key: '0-2024-12-17',
    tier: 'free',
    tier_source: 'estimated',
    context_window: 131072,
    max_output: 32768,
    capabilities: ['chat'],
    status: 'untested',
    last_checked_at: null,
    last_error: null,
    ...overrides,
  }
}

function listResponse(
  models: ProviderModelInfo[],
  extras: Partial<ProviderModelsResponse> = {},
): ProviderModelsResponse {
  return {
    provider_name: 'groq',
    models,
    hidden_non_chat_count: 0,
    pinned_model: null,
    reason: null,
    ...extras,
  }
}

const fixtures = [
  makeModel({ id: 'llama-3.3-70b-versatile' }),
  makeModel({
    id: 'gemma2-9b-it',
    tier: 'paid',
    released_or_version: '2024-06-27',
    context_window: 8192,
  }),
]

/** Exposes the current query string so URL-persistence can be asserted. */
function PanelWithProbe(props: Parameters<typeof ModelExplorerPanel>[0]) {
  const [searchParams] = useSearchParams()
  return (
    <>
      <div data-testid="qs">{searchParams.toString()}</div>
      <ModelExplorerPanel {...props} />
    </>
  )
}

function renderPanel(
  props: Partial<Parameters<typeof ModelExplorerPanel>[0]> = {},
) {
  const onTestedChange = vi.fn()
  const merged = {
    providerName: 'groq',
    providerDisplay: 'Groq',
    routellmKey: SECRET,
    onTestedChange,
    ...props,
  }
  render(
    <MemoryRouter>
      <PanelWithProbe {...merged} />
    </MemoryRouter>,
  )
  return { onTestedChange }
}

beforeEach(() => {
  vi.mocked(getProviderModels).mockResolvedValue(listResponse(fixtures))
  vi.mocked(testProviderModel).mockResolvedValue({
    provider_name: 'groq',
    model_id: 'llama-3.3-70b-versatile',
    status: 'works',
    latency_ms: 210,
    error: null,
  })
  vi.mocked(setPinnedModel).mockImplementation(
    async (_key: string, _provider: string, modelId: string | null) =>
      listResponse(fixtures, { pinned_model: modelId }),
  )
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ModelExplorerPanel', () => {
  it('renders model rows with tier badge (estimated tooltip), version, context window, and status chip', async () => {
    renderPanel()

    expect(
      await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile'),
    ).toHaveTextContent('llama-3.3-70b-versatile')
    expect(screen.getByTestId('model-row-groq-gemma2-9b-it')).toBeInTheDocument()
    // Tier is an estimate: surfaced via tooltip, never stated as fact.
    expect(screen.getByText('FREE')).toHaveAttribute('title', expect.stringContaining('estimate'))
    expect(screen.getByText('131K ctx')).toBeInTheDocument()
    expect(screen.getByTestId('status-groq-llama-3.3-70b-versatile')).toHaveTextContent('Untested')
  })

  it('refetches with the chosen sort and persists it in the URL', async () => {
    renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

    fireEvent.change(screen.getByLabelText('Sort Groq models'), {
      target: { value: 'newest' },
    })

    await waitFor(() =>
      expect(getProviderModels).toHaveBeenLastCalledWith(
        SECRET,
        'groq',
        expect.objectContaining({ sort: 'newest' }),
      ),
    )
    expect(screen.getByTestId('qs')).toHaveTextContent('sort=newest')
  })

  it('filters by tier pills and persists the filter in the URL', async () => {
    renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

    fireEvent.click(screen.getByRole('button', { name: 'Free' }))

    await waitFor(() =>
      expect(getProviderModels).toHaveBeenLastCalledWith(
        SECRET,
        'groq',
        expect.objectContaining({ tier: 'free' }),
      ),
    )
    expect(screen.getByTestId('qs')).toHaveTextContent('tier=free')
    expect(screen.getByRole('button', { name: 'Free' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('searches models and persists the query in the URL', async () => {
    renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

    fireEvent.change(screen.getByLabelText('Search Groq models'), {
      target: { value: 'gemma' },
    })

    await waitFor(() =>
      expect(getProviderModels).toHaveBeenLastCalledWith(
        SECRET,
        'groq',
        expect.objectContaining({ search: 'gemma' }),
      ),
    )
    expect(screen.getByTestId('qs')).toHaveTextContent('q=gemma')
  })

  it('runs a per-model test through the client, updates the chip, and reports the working model upward', async () => {
    const { onTestedChange } = renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

    fireEvent.click(screen.getByRole('button', { name: 'Test llama-3.3-70b-versatile' }))

    await waitFor(() =>
      expect(testProviderModel).toHaveBeenCalledWith(SECRET, 'groq', 'llama-3.3-70b-versatile'),
    )
    await waitFor(() =>
      expect(screen.getByTestId('status-groq-llama-3.3-70b-versatile')).toHaveTextContent('Works'),
    )
    await waitFor(() => expect(onTestedChange).toHaveBeenCalledWith(true))
  })

  it('explains a denied result instead of blaming the key', async () => {
    vi.mocked(getProviderModels).mockResolvedValue(
      listResponse([
        makeModel({ id: 'gemini-2.5-flash' }),
        makeModel({ id: 'gemini-3.8-flash', released_or_version: '2025-10-01' }),
      ]),
    )
    vi.mocked(testProviderModel).mockResolvedValue({
      provider_name: 'gemini',
      model_id: 'gemini-2.5-flash',
      status: 'denied',
      latency_ms: 320,
      error: 'models/gemini-2.5-flash is no longer available to new users.',
    })
    renderPanel({ providerName: 'gemini', providerDisplay: 'Google Gemini' })
    await screen.findByText('gemini-2.5-flash')

    fireEvent.click(screen.getByRole('button', { name: 'Test gemini-2.5-flash' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'model_not_found',
    )
    expect(screen.getByTestId('status-gemini-gemini-2.5-flash')).toHaveTextContent('Denied')
  })

  it('pins a model via "Use this model" and can return to Auto with a null pin', async () => {
    renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

    fireEvent.click(
      screen.getByRole('button', { name: 'Use llama-3.3-70b-versatile for routing' }),
    )
    await waitFor(() =>
      expect(setPinnedModel).toHaveBeenCalledWith(SECRET, 'groq', 'llama-3.3-70b-versatile'),
    )
    await waitFor(() =>
      expect(screen.getByTestId('pin-badge-groq')).toHaveTextContent('llama-3.3-70b-versatile'),
    )

    fireEvent.click(screen.getByRole('switch', { name: 'Auto mode for Groq' }))
    await waitFor(() =>
      expect(setPinnedModel).toHaveBeenLastCalledWith(SECRET, 'groq', null),
    )
    await waitFor(() => expect(screen.getByTestId('auto-badge-groq')).toBeInTheDocument())
  })

  it('runs the one Test-connection flow: verdict badge plus attempted/skipped walk', async () => {
    vi.mocked(testProvider).mockResolvedValue({
      provider: 'groq',
      display: 'Groq',
      verdict: 'valid',
      key_valid: true,
      model: 'llama-3.3-70b-versatile',
      latency_ms: 250,
      error: null,
      available_models: ['llama-3.3-70b-versatile'],
      model_source: 'discovered',
      attempted: [
        { model: 'llama-3.3-70b-versatile', skipped: false, reason: '', success: true },
      ],
      skipped: [
        {
          model: 'gemma2-9b-it',
          skipped: true,
          reason: 'denied: a previous call returned model_not_found for this key',
          success: false,
        },
      ],
    })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Test Groq connection' }))

    await waitFor(() => expect(testProvider).toHaveBeenCalledWith(SECRET, 'groq'))
    const verdict = await screen.findByTestId('connection-verdict-groq')
    expect(verdict).toHaveTextContent('Works')
    expect(verdict).toHaveTextContent('llama-3.3-70b-versatile')
    const walk = screen.getByTestId('connection-walk-groq')
    expect(walk).toHaveTextContent('gemma2-9b-it skipped')
    expect(walk).toHaveTextContent('llama-3.3-70b-versatile worked')
    // The DOM never shows the key used to authenticate the calls.
    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('shows an auth-failed verdict without pretending the models are the problem', async () => {
    vi.mocked(testProvider).mockResolvedValue({
      provider: 'groq',
      display: 'Groq',
      verdict: 'auth_failed',
      key_valid: false,
      model: null,
      latency_ms: null,
      error: 'The API key was rejected (401). Replace the key on the Providers page.',
      available_models: [],
      model_source: null,
    })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Test Groq connection' }))

    expect(await screen.findByTestId('connection-verdict-groq')).toHaveTextContent('Key rejected')
    expect(screen.getByText(/Replace the key/)).toBeInTheDocument()
  })

  it('tests all pending models and reports progress', async () => {
    vi.mocked(testProviderModel).mockImplementation(async (_key, _provider, modelId) => ({
      provider_name: 'groq',
      model_id: modelId,
      status: 'works',
      latency_ms: 100,
      error: null,
    }))
    renderPanel()
    await screen.findByTestId('model-row-groq-gemma2-9b-it')

    fireEvent.click(screen.getByRole('button', { name: 'Test all Groq models' }))

    await waitFor(() => {
      expect(testProviderModel).toHaveBeenCalledWith(SECRET, 'groq', 'llama-3.3-70b-versatile')
      expect(testProviderModel).toHaveBeenCalledWith(SECRET, 'groq', 'gemma2-9b-it')
    })
    await waitFor(() =>
      expect(screen.getByTestId('status-groq-gemma2-9b-it')).toHaveTextContent('Works'),
    )
  })

  it('shows the empty state (with the server reason when given)', async () => {
    vi.mocked(getProviderModels).mockResolvedValue(
      listResponse([], { reason: 'the provider key is not configured' }),
    )
    renderPanel()

    expect(
      await screen.findByText('No models found — the provider key is not configured'),
    ).toBeInTheDocument()
  })

  it('uses the taxonomy error state (401 is a key problem, not "backend not running")', async () => {
    vi.mocked(getProviderModels).mockRejectedValue({
      response: { status: 401, data: { detail: 'Invalid or revoked key.' } },
    })
    renderPanel()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Add or generate an API key',
    )
    expect(screen.queryByText('Backend is not reachable')).not.toBeInTheDocument()
  })

  it('keeps network failures worded accurately as unreachable', async () => {
    vi.mocked(getProviderModels).mockRejectedValue({ code: 'ECONNABORTED' })
    renderPanel()

    expect(await screen.findByRole('alert')).toHaveTextContent('No response from the backend')
  })

  it('never renders the RouteLLM key anywhere in the DOM', async () => {
    renderPanel()
    await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')
    fireEvent.click(screen.getByRole('button', { name: 'Test llama-3.3-70b-versatile' }))
    await waitFor(() =>
      expect(screen.getByTestId('status-groq-llama-3.3-70b-versatile')).toHaveTextContent('Works'),
    )

    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('notes the hidden non-chat count from the server', async () => {
    vi.mocked(getProviderModels).mockResolvedValue(
      listResponse(fixtures, { hidden_non_chat_count: 4 }),
    )
    renderPanel()

    expect(await screen.findByRole('note')).toHaveTextContent(
      '4 non-chat models hidden',
    )
  })
})
