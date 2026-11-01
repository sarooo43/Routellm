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
    <GlassCard className="relative h-full overflow-hidden !p-5">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full opacity-[0.07] blur-2xl"
        style={{ backgroundColor: accentColor }}
      />
      <div className="relative flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="rl-eyebrow">{label}</p>
          <p className="mt-2.5 truncate text-[28px] font-semibold leading-none tracking-tight tabular-nums text-text-primary">
            {format(displayValue)}
          </p>
          {trend ? (
            <p
              className={`mt-2.5 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                trend.direction === 'up'
                  ? 'bg-success/10 text-success'
                  : 'bg-danger/10 text-danger'
              }`}
            >
              {trend.direction === 'up' ? '▲' : '▼'} {trend.percent}% vs yesterday
            </p>
          ) : (
            <p className="mt-2.5 text-[11px] text-text-secondary/60">Live from routing engine</p>
          )}
        </div>
        <div
          className="grid h-11 w-11 shrink-0 place-items-center rounded-xl border"
          style={{
            color: accentColor,
            backgroundColor: `${accentColor}14`,
            borderColor: `${accentColor}30`,
          }}
        >
          <Icon size={19} aria-hidden="true" />
        </div>
      </div>
      <div
        aria-hidden="true"
        className="absolute bottom-0 left-5 right-5 h-px opacity-40"
        style={{ background: `linear-gradient(90deg, ${accentColor}, transparent)` }}
      />
    </GlassCard>
  )
}
