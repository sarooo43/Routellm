import { motion } from 'framer-motion'
import { Loader2, Send } from 'lucide-react'
import { FallbackChainGraph } from './FallbackChainGraph'
import { useAppStore } from '../store/useAppStore'
import { formatMs } from '../lib/format'
import type { RouteResponse } from '../types/api'

/**
 * "Try it Live" demo surface: type a prompt, send a REAL POST /api/v1/route
 * from the dashboard, and watch the FallbackChainGraph animate whichever
 * providers were actually attempted — the 15-second "watch this" moment:
 * inject chaos on a provider here, send a prompt, see it reroute.
 */
export function TryItLive() {
  const prompt = useAppStore((s) => s.tryItPrompt)
  const setPrompt = useAppStore((s) => s.setTryItPrompt)
  const sendRequest = useAppStore((s) => s.sendTryItRequest)
  const response = useAppStore((s) => s.tryItResponse)
  const sending = useAppStore((s) => s.tryItSending)

  const disabled = sending || prompt.trim().length === 0

  return (
    <section
      data-testid="try-it-live"
      className="rounded-2xl bg-surface backdrop-blur-xl border border-border p-6"
    >
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <h3 className="text-lg font-semibold tracking-tight">Try it Live</h3>
          <p className="mt-0.5 text-sm text-text-secondary">
            Sends a real POST /api/v1/route — watch the fallback chain animate
            every provider actually attempted.
          </p>
        </div>
        {!sending && response && <RouteResult result={response} />}
      </div>

      <div className="mt-4 flex gap-3">
        <input
          data-testid="try-it-live-input"
          type="text"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !disabled) sendRequest()
          }}
          placeholder="e.g. Write a haiku about routing traffic…"
          className="flex-1 rounded-xl bg-black/30 border border-border px-4 py-2.5 text-sm outline-none focus:border-accent"
          aria-label="Test prompt"
        />
        <button
          type="button"
          data-testid="try-it-live-send"
          onClick={sendRequest}
          disabled={disabled}
          className="inline-flex items-center gap-2 rounded-xl bg-accent text-black font-semibold text-sm px-4 py-2.5 hover:opacity-90 transition-opacity disabled:opacity-40"
        >
          {sending ? (
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          ) : (
            <Send size={14} aria-hidden="true" />
          )}
          {sending ? 'Routing…' : 'Send Request'}
        </button>
      </div>

      {response && (
        <div data-testid="try-it-live-result" className="mt-6">
          <FallbackChainGraph attempts={response.fallback_chain_attempted} />
          {response.success ? (
            <p className="mt-3 text-sm text-text-secondary">
              Routed to{' '}
              <span className="text-text-primary font-semibold">{response.model_used}</span> in{' '}
              <span className="tabular-nums">{formatMs(response.latency_ms, 0)}</span>{' '}
              · {response.tokens_used} tokens
            </p>
          ) : (
            <p className="mt-3 text-sm" style={{ color: '#FF4D4D' }}>
              All providers failed: {response.error}
            </p>
          )}
        </div>
      )}
    </section>
  )
}

function RouteResult({ result }: { result: RouteResponse }) {
  // Compact header summary; the graph below carries the real story.
  return (
    <motion.span
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="text-xs uppercase tracking-widest text-text-secondary"
    >
      {result.success ? `served by ${result.model_used}` : 'all providers failed'}
    </motion.span>
  )
}
