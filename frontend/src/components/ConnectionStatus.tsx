import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, HeartPulse, Loader2 } from 'lucide-react'
import axios from 'axios'
import { useWakeStore } from '../api/wake'
import { extractWorkerDetail } from '../api/workerDetail'

/**
 * Header connectivity pill: polls GET /health every 15s.
 *  - green   "Online · <ms>"  → all components up
 *  - amber   "Degraded"       → 200 but db/redis down
 *  - amber   "Waking up…"     → proxy reports the Space asleep/down, or the
 *                               probe times out while the wake window runs
 *  - red     "Offline"        → true network failure / proxy down shape
 * Polling pauses when the tab is hidden; first paint is neutral until the
 * first probe answers.
 */

type Pill = {
  state: 'unknown' | 'online' | 'degraded' | 'offline' | 'waking'
  latencyMs?: number
  detail?: string
}

const POLL_MS = 15_000

export function ConnectionStatus() {
  const [pill, setPill] = useState<Pill>({ state: 'unknown' })
  const inFlight = useRef(false)
  const waking = useWakeStore((state) => state.waking)

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
      } catch (err) {
        if (!cancelled) {
          // A timeout (no response at all) while a cold Space boots reads as
          // "waking", not "offline" — only real failures/offline shapes show
          // the red pill. An active loader wake window (api/wake.ts) always
          // wins: the probe failing under it is the sleeping Space, not a
          // real outage, and the catch may fire after the waking effect.
          const wakingNow = useWakeStore.getState().waking
          const isTimeout = axios.isAxiosError(err) && err.code === 'ECONNABORTED'
          // A real "offline" gets the Worker's actionable reason carried in
          // the structured 503/502 body ("…backend origin could not be
          // reached: …"), so the user sees the fix, not just a red dot.
          const detail = wakingNow || isTimeout ? undefined : extractWorkerDetail(err)
          setPill({ state: wakingNow || isTimeout ? 'waking' : 'offline', detail })
        }
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

  // Any active data-loader wake window overrides the pill immediately.
  useEffect(() => {
    if (waking) {
      setPill({ state: 'waking' })
    }
  }, [waking])

  const styles: Record<Pill['state'], string> = {
    unknown: 'border-white/[0.08] bg-white/[0.03] text-text-secondary',
    online: 'border-success/20 bg-success/[0.08] text-success',
    degraded: 'border-warning/25 bg-warning/[0.08] text-warning',
    waking: 'border-warning/25 bg-warning/[0.08] text-warning',
    offline: 'border-danger/30 bg-danger/[0.10] text-danger',
  }

  const label: Record<Pill['state'], string> = {
    unknown: 'Checking…',
    online: `Online · ${pill.latencyMs ?? '–'}ms`,
    degraded: 'Degraded',
    waking: 'Waking up…',
    offline: 'Offline',
  }

  const tooltip =
    pill.state === 'degraded'
      ? `Some backend components are down (${pill.detail}) — routing may fall back automatically.`
      : pill.state === 'waking'
        ? 'The backend Space is sleeping or booting — waking it can take up to a minute. Retrying automatically.'
        : pill.state === 'offline'
          ? pill.detail
            ? `The backend is not answering: ${pill.detail}`
            : 'The backend health endpoint is not reachable right now.'
          : 'Live health probe of the FastAPI gateway (db + redis), refreshed every 15 s.'

  // Show the actionable reason inline for a genuine outage (a deployment
  // misconfiguration is an error you can READ, not a transient blip). Never
  // during "waking": there the explanation is time, not a fix.
  const showReason =
    pill.state === 'offline' && Boolean(pill.detail) && (pill.detail?.length ?? 0) < 120

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
      {pill.state === 'waking' && (
        <Loader2 size={11} aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
      )}
      {showReason && (
        <span
          data-testid="connection-status-detail"
          className="max-w-[38ch] truncate text-[10px] font-medium normal-case tracking-normal opacity-80"
        >
          {pill.detail}
        </span>
      )}
    </div>
  )
}
