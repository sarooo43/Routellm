import { motion } from 'framer-motion'
import { Loader2, Send, Sparkles, TerminalSquare } from 'lucide-react'
import { FallbackChainGraph } from './FallbackChainGraph'
import { useAppStore } from '../store/useAppStore'
import { formatMs } from '../lib/format'
import type { RouteResponse } from '../types/api'

const EXAMPLE_PROMPTS = [
  'Write a Python function to merge two sorted lists',
  'Summarize the benefits of circuit breakers in one paragraph',
  'Explain async/await like I’m five',
]

/**
 * "Try it Live" demo surface: type a prompt, send a REAL POST /api/v1/route
 * from the dashboard, and watch the FallbackChainGraph animate whichever
 * providers were actually attempted.
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
      className="rl-card relative overflow-hidden p-6 sm:p-7"
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -left-20 -top-24 h-64 w-64 rounded-full bg-accent/[0.07] blur-3xl"
      />
      <div className="relative">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-accent/25 bg-accent/10 text-accent">
              <TerminalSquare size={18} aria-hidden="true" />
            </span>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-lg font-semibold tracking-tight">Try it Live</h3>
                <span className="rl-badge border-success/25 bg-success/10 text-success">
                  Real request
                </span>
              </div>
              <p className="mt-1 max-w-xl text-sm leading-relaxed text-text-secondary">
                Send a prompt through the real routing engine and watch the fallback chain
                animate every provider actually attempted.
              </p>
            </div>
          </div>
          {!sending && response && <RouteResult result={response} />}
        </div>

        <div className="mt-5 flex flex-col gap-3 sm:flex-row">
          <input
            data-testid="try-it-live-input"
            type="text"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !disabled) sendRequest()
            }}
            placeholder="e.g. Write a haiku about routing traffic…"
            className="rl-input flex-1"
            aria-label="Test prompt"
          />
          <button
            type="button"
            data-testid="try-it-live-send"
            onClick={sendRequest}
            disabled={disabled}
            className="rl-btn-primary shrink-0"
          >
            {sending ? (
              <Loader2 size={15} className="animate-spin" aria-hidden="true" />
            ) : (
              <Send size={15} aria-hidden="true" />
            )}
            {sending ? 'Routing…' : 'Send Request'}
          </button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1 text-[11px] text-text-secondary/60">
            <Sparkles size={12} aria-hidden="true" />
            Try:
          </span>
          {EXAMPLE_PROMPTS.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setPrompt(example)}
              className="max-w-full truncate rounded-full border border-white/[0.07] bg-white/[0.02] px-3 py-1 text-[11px] text-text-secondary transition hover:border-accent/35 hover:text-text-primary"
            >
              {example}
            </button>
          ))}
        </div>

        {response && (
          <motion.div
            data-testid="try-it-live-result"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-6 rounded-xl border border-white/[0.06] bg-black/25 p-4 sm:p-5"
          >
            <FallbackChainGraph attempts={response.fallback_chain_attempted} />
            {response.success ? (
              <p className="mt-4 text-sm text-text-secondary">
                Routed to{' '}
                <span className="font-semibold text-text-primary">{response.model_used}</span> in{' '}
                <span className="tabular-nums text-text-primary">{formatMs(response.latency_ms, 0)}</span>{' '}
                · <span className="tabular-nums">{response.tokens_used}</span> tokens
              </p>
            ) : (
              <p className="mt-4 text-sm text-danger">
                All providers failed: {response.error}
              </p>
            )}
          </motion.div>
        )}
      </div>
    </section>
  )
}

function RouteResult({ result }: { result: RouteResponse }) {
  return (
    <motion.span
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className={`rl-badge ${
        result.success
          ? 'border-success/25 bg-success/10 text-success'
          : 'border-danger/25 bg-danger/10 text-danger'
      }`}
    >
      {result.success ? `served by ${result.model_used}` : 'all providers failed'}
    </motion.span>
  )
}
