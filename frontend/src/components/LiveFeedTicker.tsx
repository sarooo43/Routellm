import { AnimatePresence, motion } from 'framer-motion'
import { Activity, CheckCircle2, XCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { getLogs } from '../api/client'
import { relativeTime } from '../lib/format'
import type { TaskLog } from '../types/api'
import { GlassCard } from './GlassCard'

const POLL_INTERVAL_MS = 3000
const MAX_VISIBLE = 10
const ACCENT_GLOW = 'rgba(232, 119, 34, 0.20)'
const GLOW_FADE_S = 1.5

interface FeedEntry {
  log: TaskLog
  isNew: boolean
}

/** Live tail of the most recent routing decisions (GET /api/v1/logs). */
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
    <GlassCard hoverable={false} className="overflow-hidden p-0">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-5 py-4 sm:px-6">
        <div className="flex items-center gap-2.5">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent/10 text-accent">
            <Activity size={15} aria-hidden="true" />
          </span>
          <div>
            <h2 className="text-sm font-semibold text-text-primary">Live Routing Feed</h2>
            <p className="text-[11px] text-text-secondary/70">Polling every 3 seconds</p>
          </div>
        </div>
        <Link
          to="/explorer"
          className="text-xs font-medium text-accent transition hover:text-accent/80"
        >
          Open explorer →
        </Link>
      </div>

      <div className="space-y-2 p-4 sm:p-5" role="feed" aria-label="Recent routing decisions">
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
              className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3"
            >
              {log.status === 'success' ? (
                <CheckCircle2 size={16} className="shrink-0 text-success" aria-hidden="true" />
              ) : (
                <XCircle size={16} className="shrink-0 text-danger" aria-hidden="true" />
              )}

              <span className="shrink-0 rounded-md border border-white/[0.07] bg-black/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-widest text-text-secondary">
                {log.task_type}
              </span>

              <span
                className={`min-w-0 truncate text-sm font-medium ${
                  log.selected_model ? 'text-text-primary' : 'text-text-secondary'
                }`}
              >
                {log.selected_model ?? 'no model available'}
              </span>

              <span className="ml-auto shrink-0 rounded-md bg-white/[0.04] px-2 py-0.5 text-xs tabular-nums text-text-secondary">
                {Math.round(log.latency_ms)}ms
              </span>
              <span className="w-20 shrink-0 text-right text-xs text-text-secondary/60">
                {relativeTime(log.created_at)}
              </span>
            </motion.div>
          ))}
        </AnimatePresence>

        {entries.length === 0 && (
          <div className="rounded-xl border border-dashed border-white/[0.08] px-4 py-10 text-center">
            <Activity size={22} className="mx-auto text-text-secondary/50" aria-hidden="true" />
            <p className="mt-3 text-sm text-text-secondary">
              No routing activity yet. Send a request from “Try it Live” to see it appear here.
            </p>
          </div>
        )}
      </div>
    </GlassCard>
  )
}
