import { NavLink } from 'react-router-dom'
import { BarChart3, Gauge, Route, Waypoints } from 'lucide-react'

const navItems = [
  { to: '/', label: 'Overview', icon: Gauge },
  { to: '/models', label: 'Model Health', icon: BarChart3 },
  { to: '/explorer', label: 'Routing Explorer', icon: Route },
  { to: '/analytics', label: 'Analytics', icon: Waypoints },
]

export function Sidebar() {
  return (
    <aside
      className="fixed inset-y-0 left-0 z-30 w-64 h-full bg-surface backdrop-blur-xl border-r border-border"
      aria-label="Primary"
    >
      <div className="h-16 flex items-center gap-3 px-5 border-b border-border">
        <span className="w-8 h-8 rounded-xl bg-accent flex items-center justify-center shrink-0">
          <Route size={18} className="text-background" />
        </span>
        <span className="tracking-[0.2em] font-semibold uppercase text-sm text-text-primary">
          RouteLLM
        </span>
      </div>

      <nav className="p-4 space-y-1" aria-label="Main navigation">
        {navItems.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `relative flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm transition-colors ${
                isActive
                  ? 'text-text-primary bg-white/[0.06]'
                  : 'text-text-secondary hover:text-text-primary hover:bg-white/[0.03]'
              }`
            }
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <span
                    aria-hidden="true"
                    className="absolute left-0 top-1.5 bottom-1.5 w-[2px] bg-accent rounded-full"
                  />
                )}
                <Icon size={18} className={isActive ? 'text-accent' : ''} />
                <span>{label}</span>
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="absolute bottom-4 inset-x-0 text-center text-[10px] uppercase tracking-widest text-text-secondary/60">
        API at /api/v1
      </div>
    </aside>
  )
}
