import { motion, useAnimationControls } from 'framer-motion'
import { useEffect, useRef } from 'react'
import type { ProviderStatusValue } from '../types/api'
import { formatCount } from '../lib/format'
import { ChaosControl } from './ChaosControl'

const STATUS_META: Record<
  ProviderStatusValue,
  { label: string; color: string; bg: string }
> = {
  healthy: { label: 'Healthy', color: '#3DDC97', bg: 'rgba(61, 220, 151, 0.12)' },
  degraded: { label: 'Degraded', color: '#F5A623', bg: 'rgba(245, 166, 35, 0.12)' },
  circuit_open: { label: 'Circuit Open', color: '#FF4D4D', bg: 'rgba(255, 77, 77, 0.12)' },
}

function ringColorFor(rate: number): string {
  if (rate >= 0.85) return '#3DDC97' // success green
  if (rate >= 0.6) return '#F5A623' // warning amber
  return '#FF4D4D' // danger red
}

export function StatusBadge({ status }: { status: ProviderStatusValue }) {
  const meta = STATUS_META[status]
  return (
    <span
      data-testid="status-badge"
      className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-widest px-2.5 py-1 rounded-full"
      style={{ color: meta.color, backgroundColor: meta.bg }}
    >
      <span
        aria-hidden="true"
        className="w-1.5 h-1.5 rounded-full"
        style={{ backgroundColor: meta.color }}
      />
      {meta.label}
    </span>
  )
}

interface ModelHealthCardProps {
  name: string
  status: ProviderStatusValue
  successRate: number // 0..1
  requestsToday: number
  quotaPercentageUsed: number
  /** DEV/DEMO ONLY — render the chaos controls at all? */
  chaosEnabled?: boolean
  chaosActive?: boolean
  onChaosInjected?: (providerName: string, expiresInSeconds: number) => void
  onChaosCleared?: (providerName: string) => void
}

const RING_RADIUS = 26
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS

/**
 * One provider card on the Model Health page. Border color morphs whenever
 * the status changes; transitioning INTO circuit_open additionally plays a
 * brief shake — motion that communicates "this provider just went down".
 */
export function ModelHealthCard({
  name,
  status,
  successRate,
  requestsToday,
  quotaPercentageUsed,
  chaosEnabled = false,
  chaosActive = false,
  onChaosInjected,
  onChaosCleared,
}: ModelHealthCardProps) {
  const meta = STATUS_META[status]
  const shakeControls = useAnimationControls()
  const prevStatusRef = useRef<ProviderStatusValue>(status)

  useEffect(() => {
    if (prevStatusRef.current !== 'circuit_open' && status === 'circuit_open') {
      shakeControls.start({
        x: [0, -5, 5, -5, 5, 0],
        transition: { duration: 0.4 },
      })
    }
    prevStatusRef.current = status
  }, [status, shakeControls])

  const clampedRate = Math.min(Math.max(successRate, 0), 1)
  const ringColor = ringColorFor(clampedRate)
  const ringPct = Math.round(clampedRate * 100)

  return (
    <motion.div animate={shakeControls}>
      <motion.div
        className="rl-card h-full p-6"
        animate={{ borderColor: meta.color === '#3DDC97' ? 'rgba(255, 255, 255, 0.07)' : `${meta.color}66` }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl text-sm font-bold uppercase"
              style={{ color: meta.color, backgroundColor: meta.bg }}
              aria-hidden="true"
            >
              {name.slice(0, 2)}
            </span>
            <div>
              <h3 className="text-lg font-semibold capitalize tracking-tight">{name}</h3>
              <p className="text-[11px] text-text-secondary/70">Provider node</p>
            </div>
          </div>
          <StatusBadge status={status} />
        </div>

        <div className="mt-6 flex items-center gap-5 rounded-xl border border-white/[0.05] bg-black/20 p-4">
          <div className="relative shrink-0" role="img" aria-label={`Success rate ${ringPct}%`}>
            <svg width="64" height="64" viewBox="0 0 64 64">
              <circle
                cx="32"
                cy="32"
                r={RING_RADIUS}
                fill="none"
                stroke="rgba(255, 255, 255, 0.08)"
                strokeWidth="5"
              />
              <g transform="rotate(-90 32 32)">
                <motion.circle
                  cx="32"
                  cy="32"
                  r={RING_RADIUS}
                  fill="none"
                  stroke={ringColor}
                  strokeWidth="5"
                  strokeLinecap="round"
                  strokeDasharray={RING_CIRCUMFERENCE}
                  initial={{ strokeDashoffset: RING_CIRCUMFERENCE }}
                  animate={{ strokeDashoffset: RING_CIRCUMFERENCE * (1 - clampedRate) }}
                  transition={{ duration: 0.8, ease: 'easeOut' }}
                />
              </g>
            </svg>
            <span
              className="absolute inset-0 flex items-center justify-center text-xs font-semibold tabular-nums"
              style={{ color: ringColor }}
            >
              {ringPct}%
            </span>
          </div>

          <div className="min-w-0 text-sm space-y-1.5">
            <p className="text-text-secondary">
              Requests today{' '}
              <span className="text-text-primary font-semibold tabular-nums">
                {formatCount(requestsToday)}
              </span>
            </p>
            <p className="text-text-secondary">
              Quota used{' '}
              <span
                className="font-semibold tabular-nums"
                style={{
                  color: quotaPercentageUsed >= 90 ? '#FF4D4D' : quotaPercentageUsed >= 70 ? '#F5A623' : '#F5F5F5',
                }}
              >
                {quotaPercentageUsed.toFixed(2)}%
              </span>
            </p>
            <div className="h-1.5 w-full max-w-[140px] rounded-full bg-white/5 overflow-hidden">
              <motion.div
                className="h-full rounded-full bg-accent"
                initial={{ width: 0 }}
                animate={{ width: `${Math.min(quotaPercentageUsed, 100)}%` }}
                transition={{ duration: 0.7, ease: 'easeOut' }}
              />
            </div>
          </div>
        </div>

        {chaosEnabled && (
          <ChaosControl
            providerName={name}
            chaosActive={chaosActive}
            onInjected={onChaosInjected}
            onCleared={onChaosCleared}
          />
        )}
      </motion.div>
    </motion.div>
  )
}
