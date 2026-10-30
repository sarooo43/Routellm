import { motion } from 'framer-motion'
import { useEffect } from 'react'
import { Coins, Target, Timer, Zap } from 'lucide-react'
import { KpiCard } from '../components/KpiCard'
import { LiveFeedTicker } from '../components/LiveFeedTicker'
import { TryItLive } from '../components/TryItLive'
import { formatCount, formatDollars, formatMs, formatPercent } from '../lib/format'
import { useAppStore } from '../store/useAppStore'

const POLL_INTERVAL_MS = 5000

const containerVariants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.08 } },
}

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: 'easeOut' as const } },
}

export function Overview() {
  const analytics = useAppStore((state) => state.analytics)
  const pollAll = useAppStore((state) => state.pollAll)

  useEffect(() => {
    pollAll()
    const interval = setInterval(pollAll, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [pollAll])

  const costSaved = analytics
    ? analytics.estimated_cost_if_single_model - analytics.total_cost_estimate
    : 0

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Overview</h2>
        <p className="mt-1 text-sm text-text-secondary">
          Live pulse of your intelligent routing gateway.
        </p>
      </div>

      <motion.div
        variants={containerVariants}
        initial="hidden"
        animate="show"
        className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4"
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
