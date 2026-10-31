import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, ChevronLeft, ChevronRight, ThumbsDown, ThumbsUp } from 'lucide-react'
import { useEffect, useState } from 'react'
import { postFeedback } from '../api/client'
import { FallbackChainGraph } from '../components/FallbackChainGraph'
import { relativeTime } from '../lib/format'
import { useAppStore } from '../store/useAppStore'
import type { FeedbackRating, TaskLog } from '../types/api'

const PAGE_SIZE = 10

const TASK_TYPES = ['code', 'reasoning', 'summarization', 'qa', 'general']
const STATUSES = ['success', 'failed']

function confidenceColor(score: number): string {
  if (score >= 0.7) return '#3DDC97'
  if (score >= 0.4) return '#F5A623'
  return '#FF4D4D'
}

type FeedbackState = 'idle' | 'sending' | 'done' | 'error'

/**
 * Thumbs quality widget for a single routed response. Only rendered for
 * successful requests — there is no response text to judge on failures.
 * `feedback_given` (from GET /logs) renders the already-rated state on load
 * so no duplicate submission ever hits the API.
 */
function FeedbackWidget({ log }: { log: TaskLog }) {
  const [state, setState] = useState<FeedbackState>(
    log.feedback_given ? 'done' : 'idle',
  )
  const [error, setError] = useState<string | null>(null)

  if (log.status !== 'success') return null

  const submit = async (rating: FeedbackRating) => {
    setState('sending')
    setError(null)
    try {
      await postFeedback({ request_id: log.request_id, rating })
      setState('done')
    } catch {
      // 409 duplicates (double-click races) also land here; show a
      // non-fatal hint rather than breaking the expanded row.
      setState('error')
      setError('Feedback could not be saved (it may already exist).')
    }
  }

  if (state === 'done') {
    return (
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.5, ease: 'easeOut' }}
        className="text-xs text-success"
        data-testid="feedback-thanks"
      >
        Thanks for your feedback
      </motion.p>
    )
  }

  return (
    <div className="space-y-1.5">
      <p className="text-[11px] uppercase tracking-widest text-text-secondary">
        Was this response useful?
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          data-testid="feedback-up"
          aria-label="Rate positive"
          disabled={state === 'sending'}
          onClick={() => submit('positive')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:border-success hover:text-success disabled:opacity-40 transition-colors"
        >
          <ThumbsUp size={14} /> 👍
        </button>
        <button
          type="button"
          data-testid="feedback-down"
          aria-label="Rate negative"
          disabled={state === 'sending'}
          onClick={() => submit('negative')}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:border-danger hover:text-danger disabled:opacity-40 transition-colors"
        >
          <ThumbsDown size={14} /> 👎
        </button>
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  )
}

const selectClasses =
  'bg-surface border border-border rounded-lg px-3 py-2 text-sm text-text-primary outline-none focus:border-accent'

export function RoutingExplorer() {
  const logs = useAppStore((state) => state.logs)
  const total = useAppStore((state) => state.logsTotal)
  const fetchLogs = useAppStore((state) => state.fetchLogs)

  const [taskType, setTaskType] = useState('')
  const [status, setStatus] = useState('')
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  useEffect(() => {
    fetchLogs({
      limit: PAGE_SIZE,
      offset,
      task_type: taskType || null,
      status: status || null,
    })
  }, [fetchLogs, offset, taskType, status])

  const resetPagination = () => {
    setOffset(0)
    setExpandedId(null)
  }

  const rangeStart = total === 0 ? 0 : offset + 1
  const rangeEnd = Math.min(offset + PAGE_SIZE, total)

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Routing Explorer</h2>
        <p className="mt-1 text-sm text-text-secondary">
          Every routing decision, with its full fallback chain. Click a row to expand.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <select
          aria-label="Filter by task type"
          className={selectClasses}
          value={taskType}
          onChange={(e) => {
            setTaskType(e.target.value)
            resetPagination()
          }}
        >
          <option value="">All task types</option>
          {TASK_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        <select
          aria-label="Filter by status"
          className={selectClasses}
          value={status}
          onChange={(e) => {
            setStatus(e.target.value)
            resetPagination()
          }}
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        <div className="ml-auto flex items-center gap-2 text-sm text-text-secondary">
          <span className="tabular-nums">
            {rangeStart}–{rangeEnd} of {total}
          </span>
          <button
            type="button"
            aria-label="Previous page"
            className="p-1.5 rounded-lg border border-border disabled:opacity-30 hover:border-accent transition-colors"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            <ChevronLeft size={16} />
          </button>
          <button
            type="button"
            aria-label="Next page"
            className="p-1.5 rounded-lg border border-border disabled:opacity-30 hover:border-accent transition-colors"
            disabled={rangeEnd >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <div className="rounded-2xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-widest text-text-secondary bg-white/[0.03]">
              <th className="px-4 py-3 font-medium">When</th>
              <th className="px-4 py-3 font-medium">Task</th>
              <th className="px-4 py-3 font-medium">Model</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium text-right">Latency</th>
              <th className="w-8" />
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => (
              <ExplorerRow
                key={log.request_id}
                log={log}
                expanded={expandedId === log.request_id}
                onToggle={() =>
                  setExpandedId(expandedId === log.request_id ? null : log.request_id)
                }
              />
            ))}
          </tbody>
        </table>

        {logs.length === 0 && (
          <p className="text-sm text-text-secondary/70 py-10 text-center">
            No requests match these filters.
          </p>
        )}
      </div>
    </div>
  )
}

function ExplorerRow({
  log,
  expanded,
  onToggle,
}: {
  log: TaskLog
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr
        className="border-t border-border cursor-pointer hover:bg-white/[0.03] transition-colors"
        onClick={onToggle}
      >
        <td className="px-4 py-3 text-text-secondary whitespace-nowrap">
          {relativeTime(log.created_at)}
        </td>
        <td className="px-4 py-3">
          <span className="text-[11px] uppercase tracking-widest rounded-md border border-border px-2 py-0.5 text-text-secondary">
            {log.task_type}
          </span>
        </td>
        <td className="px-4 py-3 font-medium">{log.selected_model ?? '—'}</td>
        <td className="px-4 py-3">
          <span className={log.status === 'success' ? 'text-success' : 'text-danger'}>
            {log.status}
          </span>
        </td>
        <td className="px-4 py-3 text-right tabular-nums text-text-secondary">
          {Math.round(log.latency_ms)}ms
        </td>
        <td className="px-2 py-3 text-text-secondary">
          <motion.span
            animate={{ rotate: expanded ? 180 : 0 }}
            transition={{ duration: 0.2 }}
            className="block"
          >
            <ChevronDown size={16} />
          </motion.span>
        </td>
      </tr>

      <tr>
        <td colSpan={6} className="p-0 border-t border-border">
          <AnimatePresence initial={false}>
            {expanded && (
              <motion.div
                key="expanded"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.3, ease: 'easeOut' }}
                className="overflow-hidden bg-white/[0.02]"
              >
                <div className="px-6 py-5 space-y-4">
                  <div>
                    <p className="text-[11px] uppercase tracking-widest text-text-secondary mb-3">
                      Fallback chain
                    </p>
                    <FallbackChainGraph attempts={log.fallback_chain} />
                  </div>

                  <div>
                    <p className="text-[11px] uppercase tracking-widest text-text-secondary mb-2">
                      Confidence score{' '}
                      <span className="tabular-nums text-text-primary">
                        {log.confidence_score.toFixed(3)}
                      </span>
                      {log.routing_method && (
                        <span
                          className={`ml-2 inline-flex items-center rounded-md border px-2 py-0.5 normal-case tracking-normal text-[11px] ${
                            log.routing_method === 'ml_hybrid'
                              ? 'border-purple-400/40 text-purple-300 bg-purple-400/10'
                              : 'border-border text-text-secondary bg-white/[0.03]'
                          }`}
                        >
                          {log.routing_method === 'ml_hybrid'
                            ? '🧠 ML-Assisted'
                            : '📋 Rule-Based'}
                        </span>
                      )}
                    </p>
                    <div className="h-2 w-full max-w-sm rounded-full bg-white/5 overflow-hidden">
                      <motion.div
                        className="h-full rounded-full"
                        style={{ backgroundColor: confidenceColor(log.confidence_score) }}
                        initial={{ width: 0 }}
                        animate={{ width: `${log.confidence_score * 100}%` }}
                        transition={{ duration: 0.7, ease: 'easeOut', delay: 0.2 }}
                      />
                    </div>
                  </div>

                  {log.error_message && (
                    <p className="text-sm text-danger">
                      {log.error_message}
                      {' · '}
                      <span className="text-text-secondary">
                        {log.tokens_used} tokens used
                      </span>
                    </p>
                  )}

                  <FeedbackWidget log={log} />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </td>
      </tr>
    </>
  )
}
