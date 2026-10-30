import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChaosControl } from './ChaosControl'

vi.mock('../api/client', () => ({
  injectChaos: vi.fn().mockResolvedValue({
    status: 'injected',
    provider_name: 'groq',
    expires_in_seconds: 60,
  }),
  clearChaos: vi.fn().mockResolvedValue({
    status: 'cleared',
    provider_name: 'groq',
  }),
  CHAOS_COLOR: '#B388FF',
}))

import * as apiClient from '../api/client'

const injectChaos = vi.mocked(apiClient.injectChaos)
const clearChaos = vi.mocked(apiClient.clearChaos)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ChaosControl (DEV/DEMO ONLY)', () => {
  it('shows the Simulate Outage button while chaos is inactive', () => {
    render(<ChaosControl providerName="groq" chaosActive={false} />)

    expect(screen.getByTestId('chaos-inject')).toBeInTheDocument()
    expect(screen.queryByTestId('chaos-badge')).not.toBeInTheDocument()
    expect(screen.queryByTestId('chaos-reset')).not.toBeInTheDocument()
  })

  it('clicking Simulate Outage POSTs inject with the right provider name', async () => {
    const onInjected = vi.fn()
    render(<ChaosControl providerName="groq" chaosActive={false} onInjected={onInjected} />)

    fireEvent.click(screen.getByTestId('chaos-inject'))

    await waitFor(() => expect(injectChaos).toHaveBeenCalledTimes(1))
    expect(injectChaos).toHaveBeenCalledWith({
      provider_name: 'groq',
    })
    await waitFor(() => expect(onInjected).toHaveBeenCalledWith('groq', 60))
  })

  it('active chaos shows the chaos badge and Reset Now instead of Simulate Outage', () => {
    render(<ChaosControl providerName="groq" chaosActive />)

    expect(screen.getByTestId('chaos-badge')).toHaveTextContent('Chaos Injected')
    expect(screen.getByTestId('chaos-reset')).toBeInTheDocument()
    expect(screen.queryByTestId('chaos-inject')).not.toBeInTheDocument()
  })

  it('clicking Reset Now POSTs clear with the right provider name', async () => {
    const onCleared = vi.fn()
    render(<ChaosControl providerName="gemini" chaosActive onCleared={onCleared} />)

    fireEvent.click(screen.getByTestId('chaos-reset'))

    await waitFor(() => expect(clearChaos).toHaveBeenCalledTimes(1))
    expect(clearChaos).toHaveBeenCalledWith({ provider_name: 'gemini' })
    await waitFor(() => expect(onCleared).toHaveBeenCalledWith('gemini'))
  })
})
