import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, ShieldQuestion } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { getProviderModels, testProvider } from '../api/client'
import { toApiError } from '../api/errors'
import type { ProviderTestResult, ProviderVerdict } from '../types/api'

/**
 * The result of a REAL test call, per provider.
 *
 * Why this exists: the page used to show "Configured" the moment a row was
 * saved, which only meant the ciphertext decrypted. A key that 404'd on every
 * prompt still showed green, and the failure then reached the user as
 * "auth_failed" in the middle of a request. A badge that cannot be wrong is
 * worth more than one that is always cheerful, so this panel reports exactly
 * what the last live call returned — including the model it used.
 */

interface Props {
  apiKey: string
  provider: string
  display: string
  /** Shown until the user asks for a test. */
  enabled: boolean
}

const VERDICT_STYLE: Record<ProviderVerdict, { label: string; className: string }> = {
  valid: { label: 'Works', className: 'border-success/25 bg-success/10 text-success' },
  auth_failed: { label: 'Key rejected', className: 'border-danger/25 bg-danger/10 text-danger' },
  model_not_found: {
    label: 'Model unavailable',
    className: 'border-warning/25 bg-warning/10 text-warning',
  },
  rate_limited: {
    label: 'Rate limited',
    className: 'border-warning/25 bg-warning/10 text-warning',
  },
  bad_request: { label: 'Request rejected', className: 'border-warning/25 bg-warning/10 text-warning' },
  provider_error: {
    label: 'Provider problem',
    className: 'border-warning/25 bg-warning/10 text-warning',
  },
  network_error: {
    label: 'Cannot reach provider',
    className: 'border-warning/25 bg-warning/10 text-warning',
  },
  not_configured: {
    label: 'No key',
    className: 'border-white/[0.08] bg-white/[0.04] text-text-secondary',
  },
}

export function ProviderConnectionPanel({ apiKey, provider, display, enabled }: Props) {
  const [result, setResult] = useState<ProviderTestResult | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [testing, setTesting] = useState(false)
  const [loadingModels, setLoadingModels] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A different key invalidates everything shown here: the verdict belonged to
  // the previous key, and leaving it up would be exactly the lie this replaces.
  useEffect(() => {
    setResult(null)
    setModels([])
    setError(null)
  }, [apiKey, provider])

  const loadModels = useCallback(
    async (refresh: boolean) => {
      if (!enabled) return
      setLoadingModels(true)
      try {
        setModels(await getProviderModels(apiKey, provider, refresh))
      } catch (err) {
        // Model listing is an affordance, not the verdict — never shout here.
        setModels([])
        if (toApiError(err).status === 409) setError(null)
      } finally {
        setLoadingModels(false)
      }
    },
    [apiKey, enabled, provider],
  )

  const runTest = useCallback(async () => {
    if (!enabled) return
    setTesting(true)
    setError(null)
    try {
      const next = await testProvider(apiKey, provider)
      setResult(next)
      if (next.available_models.length > 0) setModels(next.available_models)
      else if (next.verdict === 'model_not_found') void loadModels(true)
    } catch (err) {
      // A failure of the dashboard itself is NOT a provider verdict: say so
      // instead of painting the provider red.
      setResult(null)
      setError(toApiError(err).serverDetail ?? toApiError(err).message)
    } finally {
      setTesting(false)
    }
  }, [apiKey, enabled, loadModels, provider])

  const style = result ? VERDICT_STYLE[result.verdict] : null
  const showPicker =
    !!result &&
    (result.verdict === 'model_not_found' || (result.available_models?.length ?? 0) > 1)

  return (
    <div className="mt-3 space-y-2" data-testid={`provider-test-${provider}`}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void runTest()}
          disabled={!enabled || testing}
          className="inline-flex items-center gap-1.5 rounded-lg border border-white/[0.08] px-3 py-1.5 text-xs font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent disabled:opacity-40"
          aria-label={`Test ${display} connection`}
        >
          {testing ? (
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          ) : (
            <ShieldQuestion size={12} aria-hidden="true" />
          )}
          {testing ? 'Testing…' : 'Test connection'}
        </button>

        {result && style && (
          <span
            className={`rl-badge ${style.className}`}
            role="status"
            aria-label={`${display} connection status`}
          >
            {result.verdict === 'valid' ? (
              <CheckCircle2 size={12} aria-hidden="true" />
            ) : (
              <AlertTriangle size={12} aria-hidden="true" />
            )}
            {style.label}
          </span>
        )}
      </div>

      {result?.verdict === 'valid' && (
        <p className="text-xs text-text-secondary">
          Key works.
          {result.model && (
            <>
              {' '}
              Using model: <code className="font-mono text-text-primary">{result.model}</code>
            </>
          )}
          {typeof result.latency_ms === 'number' && (
            <span className="text-text-secondary/70"> · {Math.round(result.latency_ms)} ms</span>
          )}
        </p>
      )}

      {result && result.verdict !== 'valid' && result.error && (
        <p className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-2.5 py-2 text-xs leading-relaxed text-text-secondary">
          {result.error}
        </p>
      )}

      {error && (
        <p role="alert" className="text-xs text-danger">
          Could not run the test: {error}
        </p>
      )}

      {showPicker && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <label htmlFor={`model-${provider}`} className="text-xs text-text-secondary">
              Available models
            </label>
            <button
              type="button"
              onClick={() => void loadModels(true)}
              disabled={loadingModels}
              className="inline-flex items-center gap-1 text-[11px] text-text-secondary transition hover:text-accent disabled:opacity-40"
              aria-label={`Refresh ${display} models`}
            >
              <RefreshCw size={11} aria-hidden="true" />
              {loadingModels ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
          <select
            id={`model-${provider}`}
            className="rl-input text-xs"
            value={result?.model ?? ''}
            onChange={() => {
              // Picking a model only *records* the choice for the operator to
              // set as an override; it does not silently rewrite the .env.
              // The value shown is what the next test will use once the
              // override is set, so re-testing gives an honest answer.
            }}
            aria-label={`${display} model`}
          >
            {(models.length > 0 ? models : result?.model ? [result.model] : []).map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
          <p className="text-[11px] leading-relaxed text-text-secondary/70">
            Set <code className="font-mono">{provider.toUpperCase()}_MODEL</code> in your{' '}
            <code className="font-mono">.env</code> to pin one of these, then test again.
          </p>
        </div>
      )}
    </div>
  )
}
