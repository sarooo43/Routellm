import { motion } from 'framer-motion'
import { useEffect } from 'react'
import { Zap } from 'lucide-react'
import * as apiClient from '../api/client'
import { useAppStore } from '../store/useAppStore'
import { CHAOS_COLOR } from './ChaosControl'

const POLL_INTERVAL_MS = 5000

/**
 * DEV/DEMO ONLY — persistent warning banner shown app-wide whenever any
 * provider has an active chaos injection ("⚡ Chaos Mode: N provider(s)
 * artificially down"). Carries Clear All so nobody forgets to reset state
 * mid-demo. Renders nothing on production deployments (flag off / none active).
 */
export function ChaosBanner() {
  const chaosModeEnabled = useAppStore((s) => s.chaosModeEnabled)
  const models = useAppStore((s) => s.models)
  const fetchModels = useAppStore((s) => s.fetchModels)

  useEffect(() => {
    fetchModels()
    const interval = setInterval(fetchModels, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const active = models.filter((provider) => provider.chaos_active)
  if (!chaosModeEnabled || active.length === 0) return null

  const clearAll = async () => {
    await Promise.all(active.map((provider) => apiClient.clearChaos({ provider_name: provider.name })))
    await fetchModels()
  }

  return (
    <motion.div
      data-testid="chaos-banner"
      className="flex items-center justify-between gap-3 px-8 py-2 text-sm"
      style={{ backgroundColor: 'rgba(179, 136, 255, 0.08)', borderBottom: `1px solid ${CHAOS_COLOR}44` }}
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <span className="inline-flex items-center gap-2" style={{ color: CHAOS_COLOR }}>
        <motion.span
          animate={{ opacity: [1, 0.5, 1] }}
          transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
          className="inline-flex items-center gap-2"
        >
          <Zap size={13} aria-hidden="true" />
          Chaos Mode: {active.length} provider{active.length > 1 ? 's' : ''} artificially down
          <span className="text-text-secondary normal-case tracking-normal">
            ({active.map((p) => p.name).join(', ')})
          </span>
        </motion.span>
      </span>

      <button
        type="button"
        data-testid="chaos-clear-all"
        onClick={clearAll}
        className="text-xs font-semibold px-3 py-1 rounded-lg border transition-colors hover:opacity-80"
        style={{ borderColor: CHAOS_COLOR, color: CHAOS_COLOR }}
      >
        Clear All
      </button>
    </motion.div>
  )
}
