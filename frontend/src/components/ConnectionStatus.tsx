import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, HeartPulse } from 'lucide-react'
import axios from 'axios'

/**
 * Header connectivity pill: polls GET /health every 15s.
 *  - green  "Online · <ms>"   → all components up
 *  - amber  "Degraded"        → 200 but db/redis down (partial 503)
 *  - red    "Offline"         → network-shaped failure
 * Polling pauses when the tab is hidden; first paint is neutral until the
 * first probe answers.
 */

type Pill = { state: 'unknown' | 'online' | 'degraded' | 'offline'; latencyMs?: number; detail?: string }

const POLL_MS = 15_000

export function ConnectionStatus() {
  const [pill, setPill] = useState<Pill>({ state: 'unknown' })
  const inFlight = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const probe = async () => {
      if (document.hidden || inFlight.current) return
      inFlight.current = true
      const started = performance.now()
      try {
        const resp = await axios.get('/health', { timeout: 5000 })
        const latencyMs = Math.round(performance.now() - started)
        if (cancelled) return
        const components = (resp.data?.components ?? {}) as Record<string, string>
        const anyDown = Object.values(components).some((v) => v === 'down')
        setPill(
          anyDown
            ? {
                state: 'degraded',
                latencyMs,
                detail: Object.entries(components)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(', '),
              }
            : { state: 'online', latencyMs },
        )
      } catch {
        if (!cancelled) setPill({ state: 'offline' })
      } finally {
        inFlight.current = false
      }
    }

    void probe()
    timer = window.setInterval(probe, POLL_MS)
    const onVisible = () => {
      if (!document.hidden) void probe()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  const styles: Record<Pill['state'], string> = {
    unknown: 'border-white/[0.08] bg-white/[0.03] text-text-secondary',
    online: 'border-success/20 bg-success/[0.08] text-success',
    degraded: 'border-warning/25 bg-warning/[0.08] text-warning',
    offline: 'border-danger/30 bg-danger/[0.10] text-danger',
  }

  const label: Record<Pill['state'], string> = {
    unknown: 'Checking…',
    online: `Online · ${pill.latencyMs ?? '–'}ms`,
    degraded: 'Degraded',
    offline: 'Offline',
  }

  const tooltip =
    pill.state === 'degraded'
      ? `Some backend components are down (${pill.detail}) — routing may fall back automatically.`
      : pill.state === 'offline'
        ? 'The backend health endpoint is not reachable right now.'
        : 'Live health probe of the FastAPI gateway (db + redis), refreshed every 15 s.'

  if (pill.state === 'unknown') return null

  return (
    <div
      title={tooltip}
      aria-label={`Connection status: ${label[pill.state]}`}
      data-testid="connection-status"
      data-state={pill.state}
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 ${styles[pill.state]}`}
    >
      {pill.state === 'degraded' ? (
        <AlertTriangle size={11} aria-hidden="true" />
      ) : (
        <HeartPulse size={11} aria-hidden="true" />
      )}
      <span className="text-[10px] font-semibold uppercase tracking-[0.18em]">
        {label[pill.state]}
      </span>
    </div>
  )
}
