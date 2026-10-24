import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { LucideIcon } from 'lucide-react'
import { KpiCard } from './KpiCard'

afterEach(cleanup)

describe('KpiCard', () => {
  it('renders the label and final counted-up value', async () => {
    render(<KpiCard label="Total Requests" value={42} icon={PlaceholderIcon as unknown as LucideIcon} format={String} />)

    expect(screen.getByText('Total Requests')).toBeInTheDocument()

    // The value animates up via requestAnimationFrame until it settles on the
    // exact target — wait for the final clamped value to appear.
    await waitFor(() => expect(screen.getByText('42')).toBeInTheDocument())
  })

  it('formats the animated value with the provided formatter', async () => {
    render(
      <KpiCard
        label="Success Rate"
        value={0.75}
        icon={PlaceholderIcon as unknown as LucideIcon}
        format={(v) => `${(v * 100).toFixed(1)}%`}
      />,
    )
    await waitFor(() => expect(screen.getByText('75.0%')).toBeInTheDocument())
  })

  it('renders an uptrend note when trend is provided', async () => {
    render(
      <KpiCard label="Requests" value={5} icon={PlaceholderIcon as unknown as LucideIcon} format={String}
        trend={{ direction: 'up', percent: 12 }} />,
    )
    expect(screen.getByText(/12% vs yesterday/)).toBeInTheDocument()
  })

  it('marks down-trends in red', async () => {
    const { container } = render(
      <KpiCard label="Requests" value={5} icon={PlaceholderIcon as unknown as LucideIcon} format={String}
        trend={{ direction: 'down', percent: 8 }} />,
    )
    const trend = screen.getByText(/8% vs yesterday/)
    expect(trend).toHaveClass('text-danger')
    expect(container).toBeDefined()
  })
})

// Minimal stand-in so the test file does not depend on specific lucide icons.
function PlaceholderIcon() {
  return <svg data-testid="kpi-icon" />
}
