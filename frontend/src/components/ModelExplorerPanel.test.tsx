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
  runProviderDiagnostics: vi.fn(),
}))

import {
  getProviderModels,
  runProviderDiagnostics,
  setPinnedModel,
  testProvider,
  testProviderModel,
} from '../api/client'
import type { ProviderDiagnosticsResponse } from '../types/api'

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
    // Production paces at 7.5 s/batch (60 s window / 8 calls). Tests must not
    // sleep that long, so the interval is injected; the pacing *behaviour*
    // (cap, order, halt-on-429) is what is asserted.
    testPacingMs: 0,
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

  it('paces "Test all" for 25 models: caps at 8, free tier first, and says so', async () => {
    // 25 models, mixed tiers. T8c: the old loop fired every one of them
    // immediately in batches of 5 with no cap, so the button generated its own
    // 429 from the server guard (10/min per key).
    const many = Array.from({ length: 25 }, (_, i) =>
      makeModel({
        id: `model-${String(i).padStart(2, '0')}`,
        display_name: `Model ${i}`,
        tier: i % 3 === 0 ? 'free' : i % 3 === 1 ? 'paid' : 'unknown',
      }),
    )
    vi.mocked(getProviderModels).mockResolvedValue(listResponse(many))
    vi.mocked(testProviderModel).mockImplementation(async (_key, _provider, modelId) => ({
      provider_name: 'groq',
      model_id: modelId,
      status: 'works',
      latency_ms: 50,
      error: null,
    }))
    renderPanel()
    await screen.findByTestId('model-row-groq-model-00')

    fireEvent.click(screen.getByRole('button', { name: 'Test all Groq models' }))

    // Exactly 8 calls: never one per model, never more than the window allows.
    await waitFor(() => expect(testProviderModel).toHaveBeenCalledTimes(8))
    // And it stays at 8 — the cap is not a burst that keeps going.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(testProviderModel).toHaveBeenCalledTimes(8)

    // Free-tier models were chosen first.
    const testedIds = vi.mocked(testProviderModel).mock.calls.map((call) => call[2])
    const freeIds = many.filter((m) => m.tier === 'free').map((m) => m.id)
    expect(testedIds.some((id) => freeIds.includes(id))).toBe(true)

    // The user is told what was left out rather than silently dropped.
    expect(await screen.findByText(/Testing 8 of 25 models \(17 not tested\)/)).toBeInTheDocument()
  })

  it('stops the "Test all" walk on a 429 and shows a Retry-After countdown', async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      makeModel({ id: `m-${i}`, display_name: `M ${i}`, tier: 'free' }),
    )
    vi.mocked(getProviderModels).mockResolvedValue(listResponse(many))

    // A real axios 429 from the server guard: toApiError reads response.status
    // and the retry-after header, so the test must throw that shape rather than
    // hand-build an ApiError (which is what the first version of this test did,
    // and why it silently never entered the cooldown branch).
    let call = 0
    vi.mocked(testProviderModel).mockImplementation(async (_key, _provider, modelId) => {
      call += 1
      if (call > 5) {
        throw {
          isAxiosError: true,
          response: {
            status: 429,
            headers: { 'retry-after': '42' },
            data: { detail: 'Too many test calls; retry later.' },
          },
        }
      }
      return { provider_name: 'groq', model_id: modelId, status: 'works', latency_ms: 10, error: null }
    })
    renderPanel()
    await screen.findByTestId('model-row-groq-m-0')

    fireEvent.click(screen.getByRole('button', { name: 'Test all Groq models' }))

    // It surfaces the server's own delay and stops rather than marching on.
    const cooldown = await screen.findByTestId('test-all-cooldown')
    expect(cooldown).toHaveTextContent('retry in 42s')
    expect(cooldown).toHaveTextContent('Tested 5 of 8')

    const callsAtHalt = vi.mocked(testProviderModel).mock.calls.length
    await new Promise((resolve) => setTimeout(resolve, 60))
    // No further batches: a halted walk never generates its own 429 storm.
    expect(vi.mocked(testProviderModel).mock.calls.length).toBe(callsAtHalt)
    // 8 = the capped run's first batch (5) + the second batch (3) that hit the
    // guard. Crucially it is NOT 12: the cap stopped it at 8, and the 429
    // stopped it before any third batch.
    expect(callsAtHalt).toBe(8)
    // The button is disabled while the cooldown runs.
    expect(screen.getByRole('button', { name: 'Test all Groq models' })).toBeDisabled()
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

  describe('T1 diagnostics', () => {
    const diagResult: ProviderDiagnosticsResponse = {
      provider_name: 'groq',
      generated_at: '2026-11-13T00:00:00Z',
      notice: 'Contains no keys. Provider messages are redacted.',
      key_transport: 'header: Authorization: Bearer',
      api_host: 'api.groq.com',
      api_version: 'openai/v1',
      list_http_status: 200,
      list_count_before_filter: 30,
      list_count_after_filter: 25,
      discovery_reason: null,
      probe_count: 2,
      rows: [
        {
          model_id: 'llama-3.3-70b-versatile',
          http_status: 200,
          provider_status: null,
          provider_reason: null,
          outcome: null,
          success: true,
          latency_ms: 210,
          message: null,
        },
        {
          model_id: 'gemma2-9b-it',
          http_status: 429,
          provider_status: 'RESOURCE_EXHAUSTED',
          provider_reason: null,
          outcome: 'rate_limit_exceeded',
          success: false,
          latency_ms: 90,
          message: 'rate limit exceeded: api.groq.com is rate limiting this key (HTTP 429).',
        },
      ],
      received_host: 'testserver',
      app_version: '0.1.0',
      python_version: '3.13.2',
      report_markdown:
        '# Provider diagnostics — groq\n\nContains no keys. Provider messages are redacted; nothing below is a credential.\n',
    }

    beforeEach(() => {
      vi.mocked(runProviderDiagnostics).mockResolvedValue(diagResult)
    })

    it('runs diagnostics and renders the wire facts plus the probe table', async () => {
      renderPanel()
      await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

      fireEvent.click(screen.getByTestId('run-diagnostics-groq'))

      const report = await screen.findByTestId('diagnostics-groq')
      // The wire facts the report exists for.
      expect(report).toHaveTextContent('30 entries before filtering')
      expect(report).toHaveTextContent('25 chat-capable after')
      expect(report).toHaveTextContent('header: Authorization: Bearer')
      // The per-row table: model id, HTTP status, provider identity.
      expect(
        screen.getByTestId('diag-row-groq-llama-3.3-70b-versatile'),
      ).toHaveTextContent('200')
      expect(
        screen.getByTestId('diag-row-groq-gemma2-9b-it'),
      ).toHaveTextContent('429')
      expect(
        screen.getByTestId('diag-row-groq-gemma2-9b-it'),
      ).toHaveTextContent('RESOURCE_EXHAUSTED')
      // The "Contains no keys" notice is always visible with a report.
      expect(screen.getByTestId('diag-no-keys-groq')).toBeInTheDocument()
      expect(runProviderDiagnostics).toHaveBeenCalledWith(SECRET, 'groq')
    })

    it('copies the server-built Markdown report and never renders the key', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText },
        configurable: true,
      })
      renderPanel()
      await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

      fireEvent.click(screen.getByTestId('run-diagnostics-groq'))
      fireEvent.click(await screen.findByTestId('copy-report-groq'))

      await waitFor(() => expect(writeText).toHaveBeenCalled())
      expect(writeText.mock.calls[0][0]).toContain('# Provider diagnostics — groq')
      expect(writeText.mock.calls[0][0]).toContain('Contains no keys')
      // The panel authenticates with the RouteLLM key; it must never show it.
      expect(document.body.textContent).not.toContain(SECRET)
    })

    it('surfaces a rate-limited diagnostics run as a readable notice', async () => {
      vi.mocked(runProviderDiagnostics).mockRejectedValue({
        response: {
          status: 429,
          data: {
            detail: 'Rate limit: at most 10 tests per minute per key. Try again shortly.',
          },
        },
      })
      renderPanel()
      await screen.findByTestId('model-row-groq-llama-3.3-70b-versatile')

      fireEvent.click(screen.getByTestId('run-diagnostics-groq'))

      expect(await screen.findByRole('status')).toHaveTextContent(
        'Rate limit: at most 10 tests per minute per key',
      )
    })
  })
})
