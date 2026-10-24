import { motion } from 'framer-motion'
import { useEffect } from 'react'
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
import { GlassCard } from '../components/GlassCard'
import { useAppStore } from '../store/useAppStore'

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

  useEffect(() => {
    fetchAnalytics()
    const interval = setInterval(fetchAnalytics, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [fetchAnalytics])

  const perDay = analytics?.requests_per_day ?? []
  const breakdown = Object.entries(analytics?.model_usage_breakdown ?? {}).map(
    ([name, count]) => ({ name, value: count }),
  )

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Analytics</h2>
        <p className="mt-1 text-sm text-text-secondary">
          Traffic and routing mix over time — polls every 5 seconds.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: 'easeOut' }}
          className="lg:col-span-3"
        >
          <GlassCard hoverable={false}>
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
          <GlassCard hoverable={false}>
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
    </div>
  )
}
