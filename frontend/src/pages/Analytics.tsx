import { motion } from 'framer-motion'
import { useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getFeedbackSummary } from '../api/client'
import { GlassCard } from '../components/GlassCard'
import { PageHeader } from '../components/PageHeader'
import { useAppStore } from '../store/useAppStore'
import type { FeedbackSummary } from '../types/api'

const POLL_INTERVAL_MS = 5000

const ACCENT = '#E87722'
const MODEL_COLORS: Record<string, string> = {
  groq: '#E87722', // hermes orange
  gemini: '#4A90D9', // blue
  deepseek: '#9B59B6', // purple
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function GlassTooltip(props: any) {
  const { active, payload, label } = props
  if (!active || !payload || payload.length === 0) return null
  return (
    <div className="rounded-xl border border-border bg-[#141414]/95 backdrop-blur-xl px-4 py-3 shadow-2xl">
      {label !== undefined && (
        <p className="text-[11px] uppercase tracking-widest text-text-secondary">{label}</p>
      )}
      {payload.map((entry: any) => (
        <p key={entry.name} className="text-sm mt-1" style={{ color: entry.color ?? entry.fill }}>
          {entry.name}: <span className="font-semibold tabular-nums">{entry.value}</span>
        </p>
      ))}
    </div>
  )
}
/* eslint-enable @typescript-eslint/no-explicit-any */

const axisTick = { fontSize: 11, fill: '#A0A0A0' }

export function Analytics() {
  const analytics = useAppStore((state) => state.analytics)
  const fetchAnalytics = useAppStore((state) => state.fetchAnalytics)
  const [feedback, setFeedback] = useState<FeedbackSummary | null>(null)

  useEffect(() => {
    fetchAnalytics()
    const interval = setInterval(fetchAnalytics, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [fetchAnalytics])

  useEffect(() => {
    let cancelled = false
    const pull = async () => {
      try {
        const summary = await getFeedbackSummary()
        if (!cancelled) setFeedback(summary)
      } catch {
        /* summary is a nice-to-have; keep the page usable without it */
      }
    }
    pull()
    const interval = setInterval(pull, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  const perDay = analytics?.requests_per_day ?? []
  const breakdown = Object.entries(analytics?.model_usage_breakdown ?? {}).map(
    ([name, count]) => ({ name, value: count }),
  )

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Insights"
        title="Analytics"
        description="Traffic, model mix, and human feedback quality — refreshed every 5 seconds."
      />

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: 'easeOut' }}
          className="lg:col-span-3"
        >
          <GlassCard hoverable={false} className="h-full">
            <h3 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary mb-4">
              Requests per day (14d)
            </h3>
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={perDay} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                <defs>
                  <linearGradient id="requestGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={ACCENT} stopOpacity={0.45} />
                    <stop offset="100%" stopColor={ACCENT} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgba(255, 255, 255, 0.06)" vertical={false} />
                <XAxis
                  dataKey="date"
                  stroke="rgba(255, 255, 255, 0.08)"
                  tick={axisTick}
                  tickLine={false}
                  tickFormatter={(value: string) => value.slice(5)} // MM-DD
                />
                <YAxis
                  allowDecimals={false}
                  stroke="rgba(255, 255, 255, 0.08)"
                  tick={axisTick}
                  tickLine={false}
                />
                <Tooltip content={<GlassTooltip />} cursor={{ stroke: 'rgba(232, 119, 34, 0.3)' }} />
                <Area
                  type="monotone"
                  dataKey="count"
                  name="Requests"
                  stroke={ACCENT}
                  strokeWidth={2}
                  fill="url(#requestGradient)"
                  isAnimationActive
                  animationDuration={900}
                />
              </AreaChart>
            </ResponsiveContainer>
          </GlassCard>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: 'easeOut', delay: 0.08 }}
          className="lg:col-span-2"
        >
          <GlassCard hoverable={false} className="h-full">
            <h3 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary mb-4">
              Model usage mix
            </h3>
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie
                  data={breakdown}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={62}
                  outerRadius={92}
                  paddingAngle={3}
                  stroke="none"
                  isAnimationActive
                  animationDuration={900}
                >
                  {breakdown.map((entry) => (
                    <Cell
                      key={entry.name}
                      fill={MODEL_COLORS[entry.name] ?? '#A0A0A0'}
                    />
                  ))}
                </Pie>
                <Tooltip content={<GlassTooltip />} />
              </PieChart>
            </ResponsiveContainer>

            <div className="flex items-center justify-center gap-5 mt-2">
              {breakdown.map((entry) => (
                <span
                  key={entry.name}
                  className="flex items-center gap-1.5 text-xs text-text-secondary capitalize"
                >
                  <span
                    aria-hidden="true"
                    className="w-2 h-2 rounded-full"
                    style={{ backgroundColor: MODEL_COLORS[entry.name] ?? '#A0A0A0' }}
                  />
                  {entry.name}
                  <span className="tabular-nums text-text-primary font-medium">{entry.value}</span>
                </span>
              ))}
            </div>
          </GlassCard>
        </motion.div>
      </div>

      <FeedbackQuality summary={feedback} />
    </div>
  )
}

/**
 * Human thumbs quality — overall positive rate plus a small per-provider
 * horizontal bar breakdown. Bars reuse the provider colors of the usage
 * pie so the sections read as one dashboard.
 */
function FeedbackQuality({ summary }: { summary: FeedbackSummary | null }) {
  const byProvider = Object.entries(summary?.by_provider ?? {})

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, ease: 'easeOut', delay: 0.16 }}
    >
      <GlassCard hoverable={false}>
        <div className="flex items-baseline justify-between mb-4">
          <h3 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary">
            Feedback quality
          </h3>
          <span className="text-xs text-text-secondary">
            <span className="tabular-nums text-text-primary font-medium">
              {summary?.total_rated ?? 0}
            </span>{' '}
            rated ·{' '}
            <span
              className="tabular-nums font-medium"
              style={{ color: (summary?.positive_rate ?? 0) >= 0.7 ? '#3DDC97' : '#F5A623' }}
            >
              {Math.round((summary?.positive_rate ?? 0) * 100)}%
            </span>{' '}
            positive
          </span>
        </div>

        {byProvider.length === 0 ? (
          <p className="text-sm text-text-secondary/70 py-6 text-center">
            No responses rated yet — rate one from the Routing Explorer (expand a
            successful request).
          </p>
        ) : (
          <div className="space-y-3">
            {byProvider.map(([provider, counts]) => (
              <div key={provider} className="flex items-center gap-3">
                <span className="w-20 text-xs text-text-secondary capitalize">{provider}</span>
                <div className="flex-1 h-2.5 rounded-full bg-white/5 overflow-hidden">
                  <motion.div
                    className="h-full rounded-full"
                    style={{ backgroundColor: MODEL_COLORS[provider] ?? '#A0A0A0' }}
                    initial={{ width: 0 }}
                    animate={{ width: `${counts.positive_rate * 100}%` }}
                    transition={{ duration: 0.7, ease: 'easeOut' }}
                  />
                </div>
                <span className="w-24 text-right text-xs tabular-nums text-text-secondary">
                  {Math.round(counts.positive_rate * 100)}% pos
                  <span className="text-text-secondary/60">
                    {' '}
                    ({counts.positive}/{counts.negative})
                  </span>
                </span>
              </div>
            ))}
          </div>
        )}
      </GlassCard>
    </motion.div>
  )
}
