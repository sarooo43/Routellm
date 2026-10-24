import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ModelHealthCard } from './ModelHealthCard'

afterEach(cleanup)

const BASE = {
  name: 'groq',
  successRate: 0.92,
  requestsToday: 42,
  quotaPercentageUsed: 12.5,
}

describe('ModelHealthCard', () => {
  it('renders a green "Healthy" badge for healthy status', () => {
    render(<ModelHealthCard {...BASE} status="healthy" />)
    const badge = screen.getByTestId('status-badge')
    expect(badge).toHaveTextContent('Healthy')
    expect(badge).toHaveStyle({ color: '#3DDC97' })
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('12.50%')).toBeInTheDocument()
  })

  it('renders an amber "Degraded" badge for degraded status', () => {
    render(<ModelHealthCard {...BASE} status="degraded" />)
    const badge = screen.getByTestId('status-badge')
    expect(badge).toHaveTextContent('Degraded')
    expect(badge).toHaveStyle({ color: '#F5A623' })
  })

  it('renders a red "Circuit Open" badge for circuit_open status', () => {
    render(<ModelHealthCard {...BASE} status="circuit_open" />)
    const badge = screen.getByTestId('status-badge')
    expect(badge).toHaveTextContent('Circuit Open')
    expect(badge).toHaveStyle({ color: '#FF4D4D' })
  })

  it('shows the provider name and success-rate percentage', () => {
    render(<ModelHealthCard {...BASE} status="healthy" />)
    expect(screen.getByText('groq')).toBeInTheDocument()
    expect(screen.getByText('92%')).toBeInTheDocument()
  })
})
