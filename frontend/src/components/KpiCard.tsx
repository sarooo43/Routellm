import type { LucideIcon } from 'lucide-react'
import { GlassCard } from './GlassCard'
import { useCountUp } from '../hooks/useCountUp'

interface KpiCardProps {
  label: string
  value: number
  icon: LucideIcon
  /** Hex accent used for the icon tile + value tint. */
  accentColor?: string
  trend?: {
    direction: 'up' | 'down'
    percent: number
  }
  format?: (value: number) => string
}

export function KpiCard({
  label,
  value,
  icon: Icon,
  accentColor = '#E87722',
  trend,
  format = (v) => String(Math.round(v)),
}: KpiCardProps) {
  const displayValue = useCountUp(value)

  return (
    <GlassCard className="!p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-[0.18em] text-text-secondary">
            {label}
          </p>
          <p
            className="mt-2 text-3xl font-semibold tabular-nums truncate"
            style={{ color: accentColor }}
          >
            {format(displayValue)}
          </p>
          {trend && (
            <p
              className={`mt-1.5 text-xs font-medium ${
                trend.direction === 'up' ? 'text-success' : 'text-danger'
              }`}
            >
              {trend.direction === 'up' ? '▲' : '▼'} {trend.percent}% vs yesterday
            </p>
          )}
        </div>
        <div
          className="w-9 h-9 shrink-0 rounded-lg border border-border flex items-center justify-center"
          style={{ color: accentColor, backgroundColor: `${accentColor}1A` }}
        >
          <Icon size={18} />
        </div>
      </div>
    </GlassCard>
  )
}
