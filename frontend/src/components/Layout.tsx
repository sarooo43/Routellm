import { useCallback, useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { ChaosBanner } from './ChaosBanner'

const COLLAPSE_STORAGE_KEY = 'routellm.sidebar.collapsed.v1'

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_STORAGE_KEY) === 'true'
    } catch {
      return false
    }
  })
  const location = useLocation()

  const closeMobile = useCallback(() => setMobileOpen(false), [])
  const openMobile = useCallback(() => setMobileOpen(true), [])
  const toggleCollapsed = useCallback(() => {
    setCollapsed((current) => {
      const next = !current
      try {
        localStorage.setItem(COLLAPSE_STORAGE_KEY, String(next))
      } catch {
        /* private browsing / storage disabled: keep in-memory state */
      }
      return next
    })
  }, [])

  // Scroll the content pane back to the top on page changes.
  useEffect(() => {
    window.scrollTo(0, 0)
  }, [location.pathname])

  return (
    <div className="min-h-dvh bg-background text-text-primary">
      <a
        href="#main-content"
        className="sr-only z-50 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-black focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
      >
        Skip to content
      </a>

      <Sidebar
        mobileOpen={mobileOpen}
        collapsed={collapsed}
        onMobileClose={closeMobile}
        onToggleCollapsed={toggleCollapsed}
      />

      <div
        className={`flex min-h-dvh flex-col transition-[margin] duration-300 ease-out ${
          collapsed ? 'lg:ml-[84px]' : 'lg:ml-[276px]'
        }`}
      >
        <Topbar onMobileMenuOpen={openMobile} mobileMenuOpen={mobileOpen} />
        <ChaosBanner />
        <main id="main-content" className="mx-auto w-full max-w-[1440px] flex-1 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
          <Outlet />
        </main>
        <footer className="border-t border-white/[0.04] px-4 py-4 text-center text-[11px] text-text-secondary/50 sm:px-6 lg:px-8">
          RouteLLM · intelligent routing, automatic fallback, live observability
        </footer>
      </div>
    </div>
  )
}
