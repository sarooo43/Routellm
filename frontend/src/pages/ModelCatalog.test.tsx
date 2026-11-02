import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CatalogResponse } from '../types/api'
import { ModelCatalog } from './ModelCatalog'

vi.mock('../api/client', () => ({
  getCatalog: vi.fn(),
}))

import { getCatalog } from '../api/client'

// The backend now serves CatalogResponse { live_stats_available, models } —
// these tests build the static entries then wrap them in the response shape.
const entries: CatalogEntry[] = [  {
    provider_name: 'groq',
    display_name: 'Llama 3.3 70B (via Groq)',
    description: 'Ultra-low-latency inference.',
    context_window: 128000,
    capabilities: ['qa', 'general', 'summarization'],
    pricing_tier: 'free',
    cost_per_1k_tokens: 0,
    live_success_rate: 0.94,
    avg_latency_ms: 312,
    current_status: 'healthy',
  },
  {
    provider_name: 'gemini',
    display_name: 'Gemini 2.0 Flash',
    description: 'Large context window.',
    context_window: 1000000,
    capabilities: ['summarization', 'general', 'qa'],
    pricing_tier: 'free',
    cost_per_1k_tokens: 0,
    live_success_rate: 0.88,
    avg_latency_ms: 520,
    current_status: 'healthy',
  },
  {
    provider_name: 'deepseek',
    display_name: 'DeepSeek V3 / Coder',
    description: 'Strong coding and reasoning.',
    context_window: 64000,
    capabilities: ['code', 'reasoning'],
    pricing_tier: 'cheap',
    cost_per_1k_tokens: 0.0002,
    live_success_rate: 0.91,
    avg_latency_ms: 780,
    current_status: 'healthy',
  },
]

const catalog: CatalogResponse = {
  live_stats_available: true,
  models: entries,
}

function visibleProviders(): string[] {
  return screen
    .getAllByTestId('catalog-card')
    .map((card) => card.getAttribute('data-provider') ?? '')
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ModelCatalog', () => {
  it('renders every provider with catalog metadata', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)

    expect(await screen.findByText('Llama 3.3 70B (via Groq)')).toBeInTheDocument()
    expect(screen.getByText('Gemini 2.0 Flash')).toBeInTheDocument()
    expect(screen.getByText('DeepSeek V3 / Coder')).toBeInTheDocument()
    expect(screen.getByText('128K tokens')).toBeInTheDocument()
    expect(screen.getByText('1M tokens')).toBeInTheDocument()
    expect(screen.getByText('64K tokens')).toBeInTheDocument()
    expect(screen.getAllByText('FREE')).toHaveLength(2)
    expect(screen.getByText('CHEAP')).toBeInTheDocument()
    expect(visibleProviders()).toEqual(['groq', 'deepseek', 'gemini'])
  })

  it('FAIL-SOFT: live-telemetry outage shows static cards + "No live data", not an error', async () => {
    vi.mocked(getCatalog).mockResolvedValue({
      live_stats_available: false,
      models: entries.map((entry) => ({
        ...entry,
        live_success_rate: null,
        avg_latency_ms: null,
        current_status: null,
      })),
    })
    render(<ModelCatalog />)

    // Static directory still fully renders…
    expect(await screen.findByText('Llama 3.3 70B (via Groq)')).toBeInTheDocument()
    // …live fields degrade to explicit "No live data" instead of blanks/NaN,
    expect(screen.getAllByLabelText('No live data')).toHaveLength(6) // 3 cards × 2 fields
    // …and the banner explains what happened — NO error state anywhere.
    expect(screen.getByText(/Serving the static catalog only/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('capability pills narrow the grid and can be combined', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)
    await screen.findByText('DeepSeek V3 / Coder')

    fireEvent.click(screen.getByRole('button', { name: 'Code' }))
    await waitFor(() => expect(visibleProviders()).toEqual(['deepseek']))

    fireEvent.click(screen.getByRole('button', { name: 'QA' }))
    await waitFor(() => expect(visibleProviders()).toEqual(['groq', 'deepseek', 'gemini']))

    fireEvent.click(screen.getByRole('button', { name: 'Code' }))
    await waitFor(() => expect(visibleProviders()).toEqual(['groq', 'gemini']))
  })

  it('pricing tier filter works and shows an empty state for premium', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)
    await screen.findByText('DeepSeek V3 / Coder')

    fireEvent.click(screen.getByRole('button', { name: 'Free Only' }))
    await waitFor(() => expect(visibleProviders()).toEqual(['groq', 'gemini']))

    fireEvent.click(screen.getByRole('button', { name: 'Cheap' }))
    await waitFor(() => expect(visibleProviders()).toEqual(['deepseek']))

    fireEvent.click(screen.getByRole('button', { name: 'Premium' }))
    expect(await screen.findByText('No models match these filters.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(visibleProviders()).toHaveLength(3))
  })

  it('sort dropdown changes order by success rate, latency, and context window', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)
    await screen.findByText('DeepSeek V3 / Coder')

    // Default: success rate descending.
    expect(visibleProviders()).toEqual(['groq', 'deepseek', 'gemini'])

    fireEvent.change(screen.getByLabelText('Sort models'), { target: { value: 'latency' } })
    await waitFor(() => expect(visibleProviders()).toEqual(['groq', 'gemini', 'deepseek']))

    fireEvent.change(screen.getByLabelText('Sort models'), { target: { value: 'context_window' } })
    await waitFor(() => expect(visibleProviders()).toEqual(['gemini', 'groq', 'deepseek']))
  })

  it('combines capability and pricing filters', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)
    await screen.findByText('DeepSeek V3 / Coder')

    fireEvent.click(screen.getByRole('button', { name: 'Summarization' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cheap' }))
    expect(await screen.findByText('No models match these filters.')).toBeInTheDocument()
    expect(screen.queryByTestId('catalog-card')).not.toBeInTheDocument()
  })

  it('card exposes live success, latency, and status', async () => {
    vi.mocked(getCatalog).mockResolvedValue(catalog)
    render(<ModelCatalog />)
    const cards = await screen.findAllByTestId('catalog-card')
    const groq = cards[0]
    expect(within(groq).getByText('94.0%')).toBeInTheDocument()
    expect(within(groq).getByText('312ms')).toBeInTheDocument()
    expect(within(groq).getByText('Healthy')).toBeInTheDocument()
  })
})
