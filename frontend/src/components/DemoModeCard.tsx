import { motion } from 'framer-motion'
import { FlaskConical, Loader2, ShieldQuestion, Zap } from 'lucide-react'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { postRoute } from '../api/client'
import { toApiError } from '../api/errors'
import type { RouteResponse } from '../types/api'
import { GlassCard } from './GlassCard'

/**
 * Demo mode: a real `/api/v1/route` call that names the MOCK provider.
 *
 * Why a button instead of a silent fallback: an answer produced by a mock is
 * only honest if the person asked for it. The card says MOCK everywhere — in
 * the title, the response label, the endpoint hint — so a screenshot can never
 * be mistaken for a real completion. The backend keeps the same promise: the
 * demo provider is excluded from automatic routing, from the catalog, from
 * model health and from benchmark/analytics counts.
 */

interface DemoModeCardProps {
  /** Backend-reported availability; false once a real key is configured. */
  available: boolean
  /** Registry name of the demo provider (server-provided, default "mock"). */
  providerName?: string
}

export function DemoModeCard({ available, providerName = 'mock' }: DemoModeCardProps) {
  const [response, setResponse] = useState<RouteResponse | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const runDemo = useCallback(async () => {
    setPending(true)
    setError(null)
    try {
      const result = await postRoute({
        prompt: 'Explain how automatic failover works, in one sentence.',
        // Explicit, and the only way to reach the demo provider.
        force_provider: providerName,
      })
      setResponse(result)
    } catch (err) {
      setError(toApiError(err).message)
    } finally {
      setPending(false)
    }
  }, [providerName])

  return (
    <GlassCard hoverable={false} className="border-info/25 bg-info/[0.05]">
      <div className="flex items-start justify-between gap-4">
        <div className="flex gap-3">
          <FlaskConical size={18} className="mt-0.5 shrink-0 text-info" aria-hidden="true" />
          <div>
            <h2 className="flex items-center gap-2 font-display text-base font-semibold text-text-primary">
              Demo mode
              <span className="rl-badge border-info/30 bg-info/10 text-info">MOCK</span>
            </h2>
            <p className="mt-1 text-sm text-text-secondary">
              {available ? (
                <>
                  Routes one prompt to the built-in MOCK provider — no key, no network call, no spend.
                  The answer is labelled synthetic, so it is never mistaken for a real completion.
                </>
              ) : (
                <>
                  A real provider key is configured, so automatic routing uses it and the MOCK
                  provider steps aside. Remove your provider keys on the{' '}
                  <Link to="/providers" className="text-accent hover:underline">
                    Providers page
                  </Link>{' '}
                  to see demo mode again.
                </>
              )}
            </p>
          </div>
        </div>
      </div>

      {available && (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => void runDemo()}
            disabled={pending}
            className="rl-btn-primary"
          >
            {pending ? (
              <Loader2 size={14} className="animate-spin" aria-hidden="true" />
            ) : (
              <Zap size={14} aria-hidden="true" />
            )}
            {pending ? 'Routing…' : 'Send a demo prompt'}
          </button>
          <span className="text-xs text-text-secondary">
            <code className="font-mono">POST /api/v1/route</code>{' '}
            <code className="font-mono">{`{"force_provider": "${providerName}"}`}</code>
          </span>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 text-xs text-danger">
          {error}
        </p>
      )}

      {response && (
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className="mt-4 rounded-xl border border-white/[0.08] bg-black/25 p-3"
        >
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-secondary">
            <span className="rl-badge border-info/25 bg-info/10 text-info">MOCK</span>
            <span>
              provider <code className="font-mono text-text-primary">{response.model_used ?? providerName}</code>
            </span>
            {typeof response.latency_ms === 'number' && <span>{Math.round(response.latency_ms)} ms</span>}
            <span className="inline-flex items-center gap-1">
              <ShieldQuestion size={11} aria-hidden="true" /> synthetic · not benchmarked
            </span>
          </div>
          <p className="mt-2 whitespace-pre-wrap text-sm text-text-primary">{response.result}</p>
        </motion.div>
      )}
    </GlassCard>
  )
}
