import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ErrorState } from './ErrorState'

afterEach(() => cleanup())

describe('ErrorState', () => {
  it('network failures read as unreachable and offer retry', () => {
    render(<ErrorState kind="network" onRetry={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Backend is not reachable')
    expect(screen.getByTestId('error-state-network')).toBeInTheDocument()
    expect(screen.getByTestId('error-state-retry')).toBeInTheDocument()
  })

  it('401 never blames connectivity and links to the keys page', () => {
    render(<ErrorState kind="unauthorized" />)
    expect(screen.queryByText('Backend is not reachable')).not.toBeInTheDocument()
    expect(screen.getByText(/generate an api key/i)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /api keys/i })
    expect(link).toHaveAttribute('href', '/keys')
  })

  it('503 explains the database is unavailable — not the network', () => {
    render(<ErrorState kind="service_unavailable" detail="Database temporarily unavailable" />)
    expect(screen.getByText('Database temporarily unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Backend is not reachable')).not.toBeInTheDocument()
  })

  it('shows server detail as the muted body line', () => {
    render(<ErrorState kind="validation" detail="rating must be positive or negative" />)
    expect(screen.getByText('rating must be positive or negative')).toBeInTheDocument()
  })

  it('retry button dispatches onRetry', () => {
    const onRetry = vi.fn()
    render(<ErrorState kind="server_error" onRetry={onRetry} />)
    fireEvent.click(screen.getByTestId('error-state-retry'))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
