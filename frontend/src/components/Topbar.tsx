import { motion } from 'framer-motion'
import { Menu } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { findNavItem } from './navigation'

interface TopbarProps {
  onMobileMenuOpen: () => void
  mobileMenuOpen: boolean
}

export function Topbar({ onMobileMenuOpen, mobileMenuOpen }: TopbarProps) {
  const { pathname } = useLocation()
  const current = findNavItem(pathname)

  return (
    <header className="sticky top-0 z-20 flex h-[72px] shrink-0 items-center justify-between gap-4 border-b border-white/[0.06] bg-background/75 px-4 backdrop-blur-2xl sm:px-6 lg:px-8">
      <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          onClick={onMobileMenuOpen}
          aria-label="Open navigation"
          aria-controls="primary-sidebar"
          aria-expanded={mobileMenuOpen}
          className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-white/[0.08] bg-white/[0.03] text-text-primary transition-colors hover:border-accent/40 hover:bg-accent/10 lg:hidden"
        >
          <Menu size={18} aria-hidden="true" />
        </button>

        <div className="min-w-0">
          <p className="hidden text-[10px] font-semibold uppercase tracking-[0.22em] text-text-secondary/60 sm:block">
            RouteLLM / {current?.shortLabel ?? 'Dashboard'}
          </p>
          <h1 className="truncate text-base font-semibold tracking-tight text-text-primary sm:text-lg">
            {current?.label ?? 'Dashboard'}
          </h1>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-3">
        <span className="hidden rounded-full border border-white/[0.08] bg-white/[0.03] px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-text-secondary md:inline-flex">
          API v1
        </span>
        <div
          className="inline-flex items-center gap-2 rounded-full border border-success/20 bg-success/[0.08] px-3 py-1.5"
          aria-label="System status: live"
        >
          <motion.span
            className="h-2 w-2 rounded-full bg-success"
            animate={{ scale: [1, 1.25, 1], opacity: [1, 0.65, 1] }}
            transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
          />
          <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-success">
            Live
          </span>
        </div>
      </div>
    </header>
  )
}
