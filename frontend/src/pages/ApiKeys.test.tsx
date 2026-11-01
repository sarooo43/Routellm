import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getStoredKeys } from '../api/keyStorage'
import { ApiKeys } from './ApiKeys'

vi.mock('../api/client', () => ({
  generateApiKey: vi.fn(),
  getKeyUsage: vi.fn(),
  revokeKey: vi.fn(),
}))

import { generateApiKey, getKeyUsage, revokeKey } from '../api/client'

const generated = {
  api_key: 'rk_abcdefghijklmnopqrstuvwxyz123456',
  key_prefix: 'rk_abcde',
  label: 'Demo key',
  warning: 'Save this key now — it will not be shown again.',
}

beforeEach(() => {
  localStorage.clear()
  vi.mocked(generateApiKey).mockResolvedValue(generated)
  vi.mocked(getKeyUsage).mockResolvedValue({
    key_prefix: generated.key_prefix,
    label: generated.label,
    created_at: new Date().toISOString(),
    total_requests: 3,
    total_tokens_used: 120,
    last_used_at: new Date().toISOString(),
  })
  vi.mocked(revokeKey).mockResolvedValue({ status: 'revoked', key_prefix: generated.key_prefix })
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  localStorage.clear()
})

describe('ApiKeys', () => {
  it('generating a key shows the raw secret once in a modal with a copy button', async () => {
    render(<ApiKeys />)
    fireEvent.change(screen.getByLabelText('Key label'), { target: { value: 'Demo key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate New Key' }))

    expect(await screen.findByTestId('generated-raw-key')).toHaveTextContent(generated.api_key)
    expect(screen.getByText('This key won’t be shown again — copy it now.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(generated.api_key))
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'I’ve saved it' }))
    await waitFor(() => expect(screen.queryByTestId('generated-raw-key')).not.toBeInTheDocument())
    expect(screen.queryByText(generated.api_key)).not.toBeInTheDocument()
  })

  it('persists generated keys in localStorage and reloads only the prefix', async () => {
    const { unmount } = render(<ApiKeys />)
    fireEvent.click(screen.getByRole('button', { name: 'Generate New Key' }))
    await screen.findByTestId('generated-raw-key')
    fireEvent.click(screen.getByRole('button', { name: 'I’ve saved it' }))

    await waitFor(() => expect(getStoredKeys()).toHaveLength(1))
    expect(getStoredKeys()[0].api_key).toBe(generated.api_key)
    unmount()

    render(<ApiKeys />)
    expect(await screen.findByText('rk_abcde…')).toBeInTheDocument()
    expect(screen.queryByText(generated.api_key)).not.toBeInTheDocument()
    await waitFor(() => expect(getKeyUsage).toHaveBeenCalledWith(generated.api_key))
    expect(await screen.findByText('3')).toBeInTheDocument()
    expect(screen.getByText('120')).toBeInTheDocument()
  })

  it('revoke calls DELETE /keys/me for that specific key and marks it revoked', async () => {
    render(<ApiKeys />)
    fireEvent.click(screen.getByRole('button', { name: 'Generate New Key' }))
    await screen.findByTestId('generated-raw-key')
    fireEvent.click(screen.getByRole('button', { name: 'I’ve saved it' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke Demo key' }))
    await waitFor(() => expect(revokeKey).toHaveBeenCalledWith(generated.api_key))
    expect(await screen.findByText('Revoked')).toBeInTheDocument()
    expect(getStoredKeys()[0].revoked).toBe(true)
    expect(screen.queryByRole('button', { name: 'Revoke Demo key' })).not.toBeInTheDocument()
  })
})
