import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { saveGeneratedKey } from '../api/keyStorage'
import type { ProviderKeyStatus } from '../types/api'
import { ProviderKeys } from './ProviderKeys'

vi.mock('../api/client', () => ({
  generateApiKey: vi.fn(),
  getProviderKeys: vi.fn(),
  setProviderKey: vi.fn(),
  deleteProviderKey: vi.fn(),
}))

import {
  deleteProviderKey,
  generateApiKey,
  getProviderKeys,
  setProviderKey,
} from '../api/client'

const generated = {
  api_key: 'rk_abcdefghijklmnopqrstuvwxyz123456',
  key_prefix: 'rk_abcde',
  label: 'BYOK demo key',
  warning: 'Save this key now — it will not be shown again.',
}

const unconfigured: ProviderKeyStatus[] = ['groq', 'gemini', 'deepseek'].map((name) => ({
  provider_name: name,
  configured: false,
  key_hint: null,
  created_at: null,
  updated_at: null,
}))

function renderPage() {
  return render(
    <MemoryRouter>
      <ProviderKeys />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.mocked(getProviderKeys).mockResolvedValue(unconfigured)
  vi.mocked(generateApiKey).mockResolvedValue(generated)
  vi.mocked(setProviderKey).mockResolvedValue({
    provider_name: 'groq',
    configured: true,
    key_hint: '****cdef',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })
  vi.mocked(deleteProviderKey).mockResolvedValue({ status: 'deleted', provider_name: 'groq' })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  localStorage.clear()
})

describe('ProviderKeys', () => {
  it('renders a section for each provider and loads status for the active RouteLLM key', async () => {
    saveGeneratedKey(generated)
    renderPage()

    expect(await screen.findByLabelText('Groq API key')).toBeInTheDocument()
    expect(screen.getByLabelText('Google Gemini API key')).toBeInTheDocument()
    expect(screen.getByLabelText('DeepSeek API key')).toBeInTheDocument()
    await waitFor(() => expect(getProviderKeys).toHaveBeenCalledWith(generated.api_key))
    expect(screen.getAllByText('Not configured')).toHaveLength(3)
  })

  it('saves a provider key against the selected RouteLLM key, clears the draft, and never re-renders the raw secret', async () => {
    saveGeneratedKey(generated)
    renderPage()
    const secret = 'gsk_test_provider_secret_abcdef'
    const input = await screen.findByLabelText('Groq API key')
    fireEvent.change(input, { target: { value: secret } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Groq key' }))

    await waitFor(() =>
      expect(setProviderKey).toHaveBeenCalledWith(generated.api_key, 'groq', secret),
    )
    expect(await screen.findByText('****cdef')).toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(screen.queryByText(secret)).not.toBeInTheDocument()
    expect(screen.getByText('Configured')).toBeInTheDocument()
  })

  it('removes a configured provider key for the selected RouteLLM key', async () => {
    saveGeneratedKey(generated)
    vi.mocked(getProviderKeys).mockResolvedValue([
      {
        provider_name: 'groq',
        configured: true,
        key_hint: '****cdef',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      ...unconfigured.filter((row) => row.provider_name !== 'groq'),
    ])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Remove Groq key' }))
    await waitFor(() => expect(deleteProviderKey).toHaveBeenCalledWith(generated.api_key, 'groq'))
    expect(await screen.findByText('Groq key removed from rk_abcde…')).toBeInTheDocument()
    expect(screen.getAllByText('Not configured')).toHaveLength(3)
  })

  it('can generate a RouteLLM key when none exists, then load provider status', async () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Generate demo key' }))

    await waitFor(() => expect(generateApiKey).toHaveBeenCalledWith('Provider keys demo key'))
    await waitFor(() => expect(getProviderKeys).toHaveBeenCalledWith(generated.api_key))
    expect(await screen.findByText('rk_abcde…')).toBeInTheDocument()
  })

  it('surfaces a fail-closed encryption error without storing the provider secret', async () => {
    saveGeneratedKey(generated)
    vi.mocked(setProviderKey).mockRejectedValue({
      response: { status: 503, data: { detail: 'Provider key encryption is unavailable.' } },
    })
    renderPage()
    const input = await screen.findByLabelText('Groq API key')
    fireEvent.change(input, { target: { value: 'gsk_test_provider_secret_abcdef' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Groq key' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Provider key encryption is unavailable.')
    expect(screen.queryByText('Configured')).not.toBeInTheDocument()
  })
})
