import { useEffect, useRef } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { ChevronRight, PanelLeftClose, PanelLeftOpen, Route, X } from 'lucide-react'
import { NAV_GROUPS } from './navigation'

interface SidebarProps {
  mobileOpen: boolean
  collapsed: boolean
  onMobileClose: () => void
  onToggleCollapsed: () => void
}

export function Sidebar({
  mobileOpen,
  collapsed,
  onMobileClose,
  onToggleCollapsed,
}: SidebarProps) {
  const location = useLocation()
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  // Close the mobile drawer on navigation, including browser back/forward.
  useEffect(() => {
    onMobileClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps, react/set-state-in-effect
  }, [location.pathname])

  // Escape closes the drawer; body scroll is locked only while it is open.
  useEffect(() => {
    if (!mobileOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeButtonRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onMobileClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [mobileOpen, onMobileClose])

  return (
    <>
      <button
        type="button"
        aria-label="Dismiss navigation overlay"
        tabIndex={mobileOpen ? 0 : -1}
        onClick={onMobileClose}
        className={`fixed inset-0 z-30 bg-black/70 backdrop-blur-sm transition-opacity lg:hidden ${
          mobileOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
      />

      <aside
        id="primary-sidebar"
        data-testid="primary-sidebar"
        data-state={mobileOpen ? 'open' : 'closed'}
        aria-label="Primary navigation"
        className={`fixed inset-y-0 left-0 z-40 flex h-dvh flex-col border-r border-white/[0.06] bg-[#0c0e11]/95 shadow-2xl shadow-black/40 backdrop-blur-2xl transition-[width,transform] duration-300 ease-out ${
          collapsed ? 'lg:w-[84px]' : 'lg:w-[276px]'
        } w-[288px] max-w-[86vw] ${
          mobileOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'
        }`}
      >
        <div className="flex h-[72px] shrink-0 items-center gap-3 border-b border-white/[0.06] px-4">
          <NavLink
            to="/"
            onClick={onMobileClose}
            className="flex min-w-0 flex-1 items-center gap-3 rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
            aria-label="RouteLLM overview"
          >
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-accent to-[#b95312] shadow-lg shadow-accent/20">
              <Route size={19} className="text-black" aria-hidden="true" />
            </span>
            <span className={`min-w-0 ${collapsed ? 'lg:hidden' : ''}`}>
              <span className="block truncate text-[15px] font-semibold tracking-[0.18em] text-text-primary">
                ROUTELLM
              </span>
              <span className="block truncate text-[11px] text-text-secondary">
                Intelligent routing gateway
              </span>
            </span>
          </NavLink>

          <button
            ref={closeButtonRef}
            type="button"
            onClick={onMobileClose}
            aria-label="Close navigation"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-text-secondary hover:bg-white/5 hover:text-text-primary lg:hidden"
          >
            <X size={18} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={onToggleCollapsed}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-pressed={collapsed}
            className="hidden h-9 w-9 shrink-0 place-items-center rounded-lg text-text-secondary hover:bg-white/5 hover:text-text-primary lg:grid"
          >
            {collapsed ? (
              <PanelLeftOpen size={17} aria-hidden="true" />
            ) : (
              <PanelLeftClose size={17} aria-hidden="true" />
            )}
          </button>
        </div>

        <nav
          aria-label="Main navigation"
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-5 [scrollbar-width:thin]"
        >
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="mb-6 last:mb-0">
              <p
                className={`mb-2 px-3 text-[10px] font-semibold uppercase tracking-[0.22em] text-text-secondary/60 ${
                  collapsed ? 'lg:hidden' : ''
                }`}
              >
                {group.label}
              </p>
              <div className="space-y-1">
                {group.items.map(({ to, label, description, icon: Icon }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={to === '/'}
                    onClick={onMobileClose}
                    title={collapsed ? label : undefined}
                    aria-label={collapsed ? label : undefined}
                    className={({ isActive }) =>
                      `group relative flex items-center gap-3 rounded-xl px-3 py-2.5 outline-none transition-all duration-200 focus-visible:ring-2 focus-visible:ring-accent/70 ${
                        collapsed ? 'lg:justify-center lg:px-0' : ''
                      } ${
                        isActive
                          ? 'bg-accent/[0.12] text-text-primary shadow-[inset_0_0_0_1px_rgba(232,119,34,0.18)]'
                          : 'text-text-secondary hover:bg-white/[0.04] hover:text-text-primary'
                      }`
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {isActive && (
                          <span
                            aria-hidden="true"
                            className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-accent shadow-[0_0_12px_rgba(232,119,34,0.8)]"
                          />
                        )}
                        <span
                          className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg transition-colors ${
                            isActive
                              ? 'bg-accent/15 text-accent'
                              : 'bg-white/[0.03] text-text-secondary group-hover:text-text-primary'
                          }`}
                        >
                          <Icon size={17} aria-hidden="true" />
                        </span>
                        <span className={`min-w-0 flex-1 ${collapsed ? 'lg:hidden' : ''}`}>
                          <span className="block truncate text-sm font-medium">{label}</span>
                          <span className="block truncate text-[11px] text-text-secondary/70">
                            {description}
                          </span>
                        </span>
                        {isActive && (
                          <ChevronRight
                            size={14}
                            className={`shrink-0 text-accent ${collapsed ? 'lg:hidden' : ''}`}
                            aria-hidden="true"
                          />
                        )}

                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="shrink-0 border-t border-white/[0.06] p-3">
          <div
            className={`mx-auto grid h-10 w-10 place-items-center rounded-xl border border-success/20 bg-success/10 ${
              collapsed ? 'hidden lg:grid' : 'hidden'
            }`}
            title="Demo environment · API at /api/v1"
          >
            <span className="h-2 w-2 rounded-full bg-success shadow-[0_0_10px_rgba(61,220,151,0.8)]" />
            <span className="sr-only">Demo environment, API at /api/v1</span>
          </div>
          <div
            className={`rounded-xl border border-white/[0.06] bg-white/[0.025] p-3 ${
              collapsed ? 'lg:hidden' : ''
            }`}
          >
            <div className="flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
              </span>
              <p className="text-xs font-medium text-text-primary">Demo environment</p>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-text-secondary">
              Local keys · stubbed providers
              <br />
              API at <code className="text-text-secondary/90">/api/v1</code>
            </p>
          </div>
        </div>
      </aside>
    </>
  )
}
