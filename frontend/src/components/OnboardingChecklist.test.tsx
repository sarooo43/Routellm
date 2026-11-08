import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SetupStatus } from '../types/api'
import { clearOnboardingState, isOnboardingDismissed } from '../api/onboarding'
import { OnboardingChecklist } from './OnboardingChecklist'

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
        free_note: 'Free tier, no credit card.',
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
    steps: [
      { id: 'provider_key', title: 'Add a free provider key', done: false, detail: 'Demo mode works now.' },
      { id: 'test_prompt', title: 'Send a test prompt', done: false, detail: 'Open the Explorer.' },
      { id: 'try_failover', title: 'Watch the router fail over', done: false, detail: 'Simulate an outage.' },
    ],
    ...overrides,
  }
}

function renderChecklist(onTryDemo = vi.fn()) {
  return render(
    <MemoryRouter>
      <OnboardingChecklist onTryDemo={onTryDemo} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  clearOnboardingState()
  getSetupStatus.mockReset()
})

afterEach(() => {
  cleanup()
  clearOnboardingState()
})

describe('OnboardingChecklist', () => {
  it('lists the three steps from the server', async () => {
    getSetupStatus.mockResolvedValue(status())
    renderChecklist()
    expect(await screen.findByText('Add a free provider key')).toBeInTheDocument()
    expect(screen.getByText('Send a test prompt')).toBeInTheDocument()
    expect(screen.getByText('Watch the router fail over')).toBeInTheDocument()
  })

  it('links to the first unconfigured provider with a safe rel', async () => {
    getSetupStatus.mockResolvedValue(status())
    renderChecklist()
    const link = await screen.findByRole('link', { name: /get a free groq key/i })
    expect(link).toHaveAttribute('href', 'https://console.groq.com/keys')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('dismissal is persisted, not just hidden for the session', async () => {
    getSetupStatus.mockResolvedValue(status())
    const { unmount } = renderChecklist()
    await screen.findByText('Add a free provider key')

    fireEvent.click(screen.getByRole('button', { name: /dismiss the getting-started checklist/i }))
    expect(screen.queryByText('Add a free provider key')).not.toBeInTheDocument()
    expect(isOnboardingDismissed()).toBe(true)

    unmount()
    renderChecklist()
    await waitFor(() => expect(screen.queryByText('Add a free provider key')).not.toBeInTheDocument())
  })

  it('renders nothing once every step is done', async () => {
    getSetupStatus.mockResolvedValue(
      status({
        any_provider_configured: true,
        demo_available: false,
        steps: [
          { id: 'provider_key', title: 'Add a free provider key', done: true, detail: '' },
          { id: 'test_prompt', title: 'Send a test prompt', done: true, detail: '' },
          { id: 'try_failover', title: 'Watch the router fail over', done: true, detail: '' },
        ],
      }),
    )
    renderChecklist()
    await waitFor(() => expect(screen.queryByText(/three steps/i)).not.toBeInTheDocument())
  })

  it('stays hidden when the backend is unreachable', async () => {
    getSetupStatus.mockRejectedValue(new Error('network'))
    renderChecklist()
    await waitFor(() => expect(screen.queryByText(/three steps/i)).not.toBeInTheDocument())
  })

  it('marks the failover step done and asks the page to open demo mode', async () => {
    const onTryDemo = vi.fn()
    getSetupStatus.mockResolvedValue(status())
    renderChecklist(onTryDemo)
    fireEvent.click(await screen.findByRole('button', { name: /try demo mode/i }))
    expect(onTryDemo).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(screen.getByText('Watch the router fail over')).toHaveClass('line-through'),
    )
  })

  it('says demo mode answers without spending anything', async () => {
    getSetupStatus.mockResolvedValue(status())
    renderChecklist()
    expect(await screen.findByText(/without\s+spending anything/i)).toBeInTheDocument()
  })
})
