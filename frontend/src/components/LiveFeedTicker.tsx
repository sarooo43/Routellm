import { AnimatePresence, motion } from 'framer-motion'
import { CheckCircle2, XCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { getLogs } from '../api/client'
import { relativeTime } from '../lib/format'
import type { TaskLog } from '../types/api'
import { GlassCard } from './GlassCard'

const POLL_INTERVAL_MS = 3000
const MAX_VISIBLE = 10
const ACCENT_GLOW = 'rgba(232, 119, 34, 0.20)' // accent #E87722 at 20%
const GLOW_FADE_S = 1.5

interface FeedEntry {
  log: TaskLog
  /** True only for entries that arrived after the initial page load. */
  isNew: boolean
}

/**
 * Live tail of the most recent routing decisions (GET /api/v1/logs).
 * New entries slide in from the top with a brief amber glow that fades out;
 * entries that push past the visible cap fade out at the bottom.
 */
export function LiveFeedTicker({ limit = MAX_VISIBLE }: { limit?: number }) {
  const [entries, setEntries] = useState<FeedEntry[]>([])
  const knownIdsRef = useRef<Set<string>>(new Set())
  const isFirstFetchRef = useRef(true)

  useEffect(() => {
    let cancelled = false

    const poll = async () => {
      try {
        const page = await getLogs({ limit, offset: 0 })
        if (cancelled) return

        const isFirstFetch = isFirstFetchRef.current
        isFirstFetchRef.current = false

        setEntries(
          page.logs.map((log) => ({
            log,
            isNew: !isFirstFetch && !knownIdsRef.current.has(log.request_id),
          })),
        )
        knownIdsRef.current = new Set(page.logs.map((entry) => entry.request_id))
      } catch {
        // Backend briefly unreachable — keep the last known feed visible.
      }
    }

    poll()
    const interval = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [limit])

  return (
    <GlassCard hoverable={false}>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary">
          Live Routing Feed
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-text-secondary/60">
          Polling every 3s
        </span>
      </div>

      <div className="space-y-2" role="feed" aria-label="Recent routing decisions">
        <AnimatePresence initial={false}>
          {entries.map(({ log, isNew }) => (
            <motion.div
              key={log.request_id}
              layout
              initial={
                isNew
                  ? { opacity: 0, y: -20, backgroundColor: ACCENT_GLOW }
                  : { opacity: 1, y: 0, backgroundColor: 'rgba(232, 119, 34, 0)' }
              }
              animate={{ opacity: 1, y: 0, backgroundColor: 'rgba(232, 119, 34, 0)' }}
              exit={{ opacity: 0, y: 14 }}
              transition={{
                backgroundColor: { duration: GLOW_FADE_S, ease: 'easeOut' },
                y: { type: 'spring', stiffness: 300, damping: 26 },
                opacity: { duration: 0.3 },
              }}
              className="flex items-center gap-3 rounded-xl border border-border bg-white/[0.02] px-4 py-3"
            >
              {log.status === 'success' ? (
                <CheckCircle2 size={16} className="text-success shrink-0" />
              ) : (
                <XCircle size={16} className="text-danger shrink-0" />
              )}

              <span className="text-[11px] uppercase tracking-widest rounded-md border border-border px-2 py-0.5 text-text-secondary shrink-0">
                {log.task_type}
              </span>

              <span
                className={`text-sm font-medium truncate ${
                  log.selected_model ? 'text-text-primary' : 'text-text-secondary'
                }`}
              >
                {log.selected_model ?? 'no model available'}
              </span>

              <span className="ml-auto text-xs tabular-nums text-text-secondary shrink-0">
                {Math.round(log.latency_ms)}ms
              </span>
              <span className="text-xs text-text-secondary/60 shrink-0 w-20 text-right">
                {relativeTime(log.created_at)}
              </span>
            </motion.div>
          ))}
        </AnimatePresence>

        {entries.length === 0 && (
          <p className="text-sm text-text-secondary/70 py-6 text-center">
            No routing activity yet. Fire a POST /api/v1/route request to see it appear here live.
          </p>
        )}
      </div>
    </GlassCard>
  )
}
