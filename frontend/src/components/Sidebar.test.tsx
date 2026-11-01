import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from './Sidebar'

afterEach(cleanup)

function renderSidebar(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const onMobileClose = vi.fn()
  const onToggleCollapsed = vi.fn()
  render(
    <MemoryRouter initialEntries={['/catalog']}>
      <Sidebar
        mobileOpen={false}
        collapsed={false}
        onMobileClose={onMobileClose}
        onToggleCollapsed={onToggleCollapsed}
        {...props}
      />
    </MemoryRouter>,
  )
  return { onMobileClose, onToggleCollapsed }
}

describe('Sidebar', () => {
  it('renders grouped navigation and marks the active route', () => {
    renderSidebar()
    expect(screen.getByText('Workspace')).toBeInTheDocument()
    expect(screen.getByText('Operate')).toBeInTheDocument()
    const active = screen.getByRole('link', { name: /Catalog/ })
    expect(active).toHaveAttribute('aria-current', 'page')
  })

  it('mobile drawer closes via the close button and Escape', () => {
    const { onMobileClose } = renderSidebar({ mobileOpen: true })
    onMobileClose.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }))
    expect(onMobileClose).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onMobileClose).toHaveBeenCalledTimes(2)
  })

  it('collapse control reports its state', () => {
    const { onToggleCollapsed } = renderSidebar()
    const button = screen.getByRole('button', { name: 'Collapse sidebar' })
    expect(button).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(button)
    expect(onToggleCollapsed).toHaveBeenCalled()
  })

  it('collapsed sidebar exposes an expand control', () => {
    renderSidebar({ collapsed: true })
    expect(screen.getByRole('button', { name: 'Expand sidebar' })).toHaveAttribute('aria-pressed', 'true')
  })
})
