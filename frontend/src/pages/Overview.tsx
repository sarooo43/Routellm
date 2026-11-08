import { motion } from 'framer-motion'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, Coins, LibraryBig, Target, Timer, Zap } from 'lucide-react'
import { DemoModeCard } from '../components/DemoModeCard'
import { KpiCard } from '../components/KpiCard'
import { LiveFeedTicker } from '../components/LiveFeedTicker'
import { NoProviderKeyBanner } from '../components/NoProviderKeyBanner'
import { OnboardingChecklist } from '../components/OnboardingChecklist'
import { PageHeader } from '../components/PageHeader'
import { TryItLive } from '../components/TryItLive'
import { formatCount, formatDollars, formatMs, formatPercent } from '../lib/format'
import { useAppStore } from '../store/useAppStore'
import { getSetupStatus } from '../api/client'
import type { SetupStatus } from '../types/api'

const POLL_INTERVAL_MS = 5000

const containerVariants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06 } },
}

const itemVariants = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { duration: 0.4, ease: 'easeOut' as const } },
}

export function Overview() {
  const analytics = useAppStore((state) => state.analytics)
  const pollAll = useAppStore((state) => state.pollAll)
  const [setup, setSetup] = useState<SetupStatus | null>(null)
  const [demoOpen, setDemoOpen] = useState(false)

  const refreshSetup = useCallback(async () => {
    try {
      setSetup(await getSetupStatus())
    } catch {
      // The page's own error state covers an unreachable backend; onboarding
      // must not turn that into a second, competing message.
      setSetup(null)
    }
  }, [])

  useEffect(() => {
    pollAll()
    // Fetch-on-mount by design: onboarding readiness is external state, and a
    // fetch is inherently "set state after an async boundary".
    // eslint-disable-next-line react/set-state-in-effect
    void refreshSetup()
    const interval = setInterval(pollAll, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [pollAll, refreshSetup])

  const costSaved = analytics
    ? analytics.estimated_cost_if_single_model - analytics.total_cost_estimate
    : 0

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Gateway pulse"
        title="Overview"
        description="Live routing health, cost efficiency, and every fallback decision — in one place."
        actions={
          <Link to="/catalog" className="rl-btn-secondary">
            <LibraryBig size={15} aria-hidden="true" />
            Browse catalog
            <ArrowUpRight size={14} className="text-text-secondary" aria-hidden="true" />
          </Link>
        }
      />

      <OnboardingChecklist onTryDemo={() => setDemoOpen(true)} />
      <NoProviderKeyBanner />

      {(demoOpen || setup?.demo_available) && (
        <DemoModeCard
          available={setup ? setup.demo_available : true}
          providerName={setup?.demo_provider_name ?? 'mock'}
        />
      )}

      <motion.div
        variants={containerVariants}
        initial="hidden"
        animate="show"
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4"
      >
        <motion.div variants={itemVariants}>
          <KpiCard
            label="Total Requests"
            value={analytics?.total_requests ?? 0}
            icon={Zap}
            accentColor="#E87722"
            format={formatCount}
          />
        </motion.div>
        <motion.div variants={itemVariants}>
          <KpiCard
            label="Success Rate"
            value={analytics?.overall_success_rate ?? 0}
            icon={Target}
            accentColor="#3DDC97"
            format={(v) => formatPercent(v, 1)}
          />
        </motion.div>
        <motion.div variants={itemVariants}>
          <KpiCard
            label="Avg Latency"
            value={analytics?.avg_latency_ms ?? 0}
            icon={Timer}
            accentColor="#F5A623"
            format={(v) => formatMs(v, 0)}
          />
        </motion.div>
        <motion.div variants={itemVariants}>
          <KpiCard
            label="Cost Saved"
            value={costSaved}
            icon={Coins}
            accentColor="#D4AF37"
            format={(v) => formatDollars(Math.max(v, 0), 6)}
          />
        </motion.div>
      </motion.div>

      <TryItLive />
      <LiveFeedTicker />
    </div>
  )
}
