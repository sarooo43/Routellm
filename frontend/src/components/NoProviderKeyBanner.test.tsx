import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SetupStatus } from '../types/api'
import { NoProviderKeyBanner } from './NoProviderKeyBanner'

const getSetupStatus = vi.fn()

vi.mock('../api/client', () => ({
  getSetupStatus: () => getSetupStatus(),
}))

function status(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    local_mode: true,
    require_api_key: false,
    providers: [
      {
        name: 'groq',
        display: 'Groq',
        console_url: 'https://console.groq.com/keys',
        free_note: 'Free tier.',
        configured: false,
        key_source: null,
        usable: false,
      },
    ],
    any_provider_configured: false,
    any_provider_usable: false,
    demo_mode_enabled: true,
    demo_available: true,
    demo_provider_name: 'mock',
    chaos_mode_enabled: true,
    data_dir: './data',
    database_driver: 'aiosqlite',
    preferred_provider: 'groq',
    preferred_console_url: 'https://console.groq.com/keys',
    steps: [],
    ...overrides,
  }
}

beforeEach(() => {
  localStorage.clear()
  getSetupStatus.mockReset()
})

afterEach(() => cleanup())

describe('NoProviderKeyBanner', () => {
  it('is a banner, not an error: the app still works', async () => {
    getSetupStatus.mockResolvedValue(status())
    render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    const banner = await screen.findByTestId('no-provider-key-banner')
    expect(banner).toHaveAttribute('role', 'status')
    expect(banner).not.toHaveAttribute('role', 'alert')
    expect(banner).toHaveTextContent(/demo mode answers with a labelled mock provider/i)
    expect(banner).toHaveTextContent(/groq has a free tier/i)
  })

  it('links to the Providers page', async () => {
    getSetupStatus.mockResolvedValue(status())
    render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    expect(await screen.findByRole('link', { name: /add a key/i })).toHaveAttribute('href', '/providers')
  })

  it('disappears once a provider is configured', async () => {
    getSetupStatus.mockResolvedValue(status({ any_provider_configured: true }))
    render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.queryByTestId('no-provider-key-banner')).not.toBeInTheDocument(),
    )
  })

  it('stays hidden when the backend cannot be reached', async () => {
    getSetupStatus.mockRejectedValue(new Error('offline'))
    render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.queryByTestId('no-provider-key-banner')).not.toBeInTheDocument(),
    )
  })

  it('dismissal is remembered', async () => {
    getSetupStatus.mockResolvedValue(status())
    const { unmount } = render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: /dismiss the provider-key banner/i }))
    expect(screen.queryByTestId('no-provider-key-banner')).not.toBeInTheDocument()
    expect(localStorage.getItem('routellm.noProviderKeyBanner.dismissed.v1')).toBe('true')

    unmount()
    render(
      <MemoryRouter>
        <NoProviderKeyBanner />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.queryByTestId('no-provider-key-banner')).not.toBeInTheDocument(),
    )
  })
})
