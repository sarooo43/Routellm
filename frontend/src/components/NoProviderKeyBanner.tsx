import { KeyRound, Sparkles, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getSetupStatus } from '../api/client'
import type { SetupStatus } from '../types/api'

const DISMISS_KEY = 'routellm.noProviderKeyBanner.dismissed.v1'

/**
 * Shown when no provider key is configured anywhere.
 *
 * This is a banner, not an error: the app is fully usable without a key (demo
 * mode answers with a labelled MOCK provider), so the wording must not imply
 * something is broken. It disappears by itself as soon as a key exists.
 */
export function NoProviderKeyBanner() {
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === 'true'
    } catch {
      return false
    }
  })

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const next = await getSetupStatus()
        if (!cancelled) setStatus(next)
      } catch {
        if (!cancelled) setStatus(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (dismissed || !status || status.any_provider_configured) return null

  const provider = status.providers[0]

  return (
    <div
      role="status"
      data-testid="no-provider-key-banner"
      className="flex flex-wrap items-center gap-3 rounded-xl border border-accent/25 bg-accent/[0.06] px-4 py-3 text-sm"
    >
      <KeyRound size={16} className="shrink-0 text-accent" aria-hidden="true" />
      <p className="min-w-0 flex-1 text-text-secondary">
        <span className="font-medium text-text-primary">No provider key yet.</span>{' '}
        {status.demo_available ? (
          <>
            Demo mode answers with a labelled MOCK provider — add a real key whenever you like (
            {provider ? <>{provider.display} has a free tier</> : 'Groq, Google AI Studio or DeepSeek'}
            ).
          </>
        ) : (
          <>Add one to start routing to a real model.</>
        )}
      </p>
      <Link to="/providers" className="rl-btn-secondary shrink-0 !px-3 !py-1.5 text-xs">
        <Sparkles size={13} aria-hidden="true" />
        Add a key
      </Link>
      <button
        type="button"
        aria-label="Dismiss the provider-key banner"
        onClick={() => {
          try {
            localStorage.setItem(DISMISS_KEY, 'true')
          } catch {
            /* ignore */
          }
          setDismissed(true)
        }}
        className="shrink-0 rounded-lg p-1 text-text-secondary transition hover:bg-white/[0.06] hover:text-text-primary"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  )
}
