import {
  AlertTriangle,
  BadgeCheck,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleSlash,
  ClipboardCheck,
  Clock,
  Copy,
  FlaskConical,
  Loader2,
  Pin,
  RefreshCw,
  Search,
  ShieldQuestion,
  Stethoscope,
  XCircle,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  getProviderModels,
  runProviderDiagnostics,
  setPinnedModel,
  testProvider,
  testProviderModel,
} from '../api/client'
import { toApiError } from '../api/errors'
import type { ApiErrorKind } from '../api/errors'
import type {
  ModelSort,
  ModelStatus,
  ModelTier,
  ProviderDiagnosticsResponse,
  ProviderModelInfo,
  ProviderModelsResponse,
  ProviderTestResult,
  ProviderVerdict,
} from '../types/api'
import { ErrorState } from './ErrorState'
import { Skeleton } from './Skeleton'

/** Batch size for "Test all": 8 per window on the free tier, sequentially. */
const TEST_BATCH_SIZE = 5

/**
 * Client-side pacing for "Test all" (T8c).
 *
 * The server allows TEST_RATE_LIMIT = 10 test calls per key per rolling 60 s
 * and 5 in flight (app/core/test_endpoint_guard.py). The old loop fired every
 * model back to back in batches of 5 with no cap and no pause, so 25 models
 * meant 25 calls in a few hundred milliseconds — the button reliably produced
 * its own 429.
 *
 * The rule here is "stay under the limit we know about": a token bucket that
 * holds at most TEST_MAX_CALLS_PER_WINDOW calls per window, refilled by
 * TEST_CALL_INTERVAL_MS per call. 8 (not 10) leaves headroom for a manual
 * Test click in the same window.
 */
const TEST_MAX_CALLS_PER_WINDOW = 8
const TEST_WINDOW_MS = 60_000
const TEST_CALL_INTERVAL_MS = TEST_WINDOW_MS / TEST_MAX_CALLS_PER_WINDOW
/** Never spend more than this many calls on one "Test all" click. */
const TEST_ALL_CAP = 8

/** Free-tier models are tested first so the cap is spent where it costs least. */
const TIER_TEST_ORDER: Record<string, number> = { free: 0, unknown: 1, paid: 2 }

/** Verdict styling for the one Test-connection button (PR #15 taxonomy). */
const VERDICT_STYLE: Record<ProviderVerdict, { label: string; className: string }> = {
  valid: { label: 'Works', className: 'border-success/25 bg-success/10 text-success' },
  auth_failed: { label: 'Key rejected', className: 'border-danger/25 bg-danger/10 text-danger' },
  model_not_found: {
    label: 'Model unavailable',
    className: 'border-warning/25 bg-warning/10 text-warning',
  },
  rate_limited: { label: 'Rate limited', className: 'border-warning/25 bg-warning/10 text-warning' },
  bad_request: { label: 'Request rejected', className: 'border-warning/25 bg-warning/10 text-warning' },
  provider_error: { label: 'Provider problem', className: 'border-warning/25 bg-warning/10 text-warning' },
  network_error: { label: 'Cannot reach provider', className: 'border-warning/25 bg-warning/10 text-warning' },
  not_configured: { label: 'No key', className: 'border-white/[0.08] bg-white/[0.04] text-text-secondary' },
}

const TIER_BADGE: Record<ModelTier, string> = {
  free: 'border-success/25 bg-success/10 text-success',
  paid: 'border-warning/25 bg-warning/10 text-warning',
  unknown: 'border-white/[0.08] bg-white/[0.04] text-text-secondary',
}

const STATUS_CHIP: Record<ModelStatus, { label: string; className: string }> = {
  untested: { label: 'Untested', className: 'border-white/[0.08] bg-white/[0.04] text-text-secondary' },
  works: { label: 'Works', className: 'border-success/25 bg-success/10 text-success' },
  denied: { label: 'Denied', className: 'border-danger/25 bg-danger/10 text-danger' },
  rate_limited: { label: 'Rate limited', className: 'border-warning/25 bg-warning/10 text-warning' },
}

function formatContext(value: number | null): string {
  if (!value) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 === 0 ? 0 : 1)}M ctx`
  if (value >= 1000) return `${Math.round(value / 1000)}K ctx`
  return `${value} ctx`
}

interface ModelExplorerPanelProps {
  providerName: string
  providerDisplay: string
  routellmKey: string
  /** Raised to the page so the card badge can say "tested". */
  onTestedChange?: (hasWorkingModel: boolean) => void
  /**
   * Pause between "Test all" batches, in ms. Defaults to the real pacing
   * derived from the server guard (60 s / 8 calls = 7.5 s). Only tests set
   * it, so the production interval stays the honest one and the suite does
   * not have to wait 7.5 s per batch.
   */
  testPacingMs?: number
}

/**
 * Dark amber glassmorphism model explorer for one BYOK provider.
 * Sort / tier filter / search / open-state persist in the URL query string.
 */
export function ModelExplorerPanel({
  providerName,
  providerDisplay,
  routellmKey,
  onTestedChange,
  testPacingMs = TEST_CALL_INTERVAL_MS,
}: ModelExplorerPanelProps) {
  const [searchParams, setSearchParams] = useSearchParams()
  const sort = (searchParams.get('sort') as ModelSort) ?? 'tier'
  const tier = (searchParams.get('tier') as 'all' | 'free' | 'paid') ?? 'all'
  const search = searchParams.get('q') ?? ''

  const [data, setData] = useState<ProviderModelsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [errorKind, setErrorKind] = useState<ApiErrorKind | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  const [busyModel, setBusyModel] = useState<string | null>(null)
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set())
  const [batchProgress, setBatchProgressSection] = useState<{
    tested: number
    capped: number
    total: number
  } | null>(null)
  /** Seconds left on a 429 cold-down, shown as a live countdown (T8c). */
  const [cooldownLeft, setCooldownLeft] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  // One Test-connection flow (the single route: POST /provider-test/{provider}).
  const [connResult, setConnResult] = useState<ProviderTestResult | null>(null)
  const [connTesting, setConnTesting] = useState(false)
  // T1: in-app diagnostics state.
  const [diag, setDiag] = useState<ProviderDiagnosticsResponse | null>(null)
  const [diagBusy, setDiagBusy] = useState(false)
  const [diagCopied, setDiagCopied] = useState(false)
  const mounted = useRef(true)

  // Live 1s countdown for a 429 Retry-After, so the button re-enables itself
  // when the window has actually passed instead of staying stuck.
  useEffect(() => {
    if (cooldownLeft <= 0) return
    const id = window.setTimeout(() => setCooldownLeft((n) => Math.max(0, n - 1)), 1000)
    return () => window.clearTimeout(id)
  }, [cooldownLeft])

  // The page passes a fresh inline callback per render; keeping it in a ref
  // keeps `load` (and therefore the fetch effect) identity-stable so mounting
  // the panel never causes a fetch loop.
  const onTestedRef = useRef(onTestedChange)
  useEffect(() => {
    onTestedRef.current = onTestedChange
  }, [onTestedChange])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // A different key invalidates any verdict shown here: it belonged to the
  // previous key, and leaving it up would be exactly the lie this replaces.
  useEffect(() => {
    setConnResult(null)
    setDiag(null)
    setDiagCopied(false)
  }, [routellmKey, providerName])

  const updateParams = useCallback(
    (patch: Record<string, string | null>) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [key, value] of Object.entries(patch)) {
            if (value === null || value === '') next.delete(key)
            else next.set(key, value)
          }
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true)
      setErrorKind(null)
      setErrorDetail(null)
      try {
        const result = await getProviderModels(routellmKey, providerName, {
          sort,
          tier,
          search,
          refresh,
        })
        if (!mounted.current) return
        setData(result)
        onTestedRef.current?.(result.models.some((m) => m.status === 'works'))
      } catch (err) {
        if (!mounted.current) return
        const apiError = toApiError(err)
        setErrorKind(apiError.kind)
        setErrorDetail(apiError.serverDetail ?? null)
        setData(null)
      } finally {
        if (mounted.current) setLoading(false)
      }
    },
    [routellmKey, providerName, sort, tier, search],
  )

  useEffect(() => {
    void load(false)
  }, [load])

  const models = data?.models ?? []
  const pinned = data?.pinned_model ?? null
  const autoMode = pinned === null

  const recommended = useMemo(() => {
    const working = models.filter((m) => m.status === 'works')
    const freeWorking = working.filter((m) => m.tier === 'free')
    return freeWorking[0] ?? working[0] ?? null
  }, [models])

  const onTest = useCallback(
    async (modelId: string) => {
      setBusyModel(modelId)
      setNotice(null)
      try {
        const result = await testProviderModel(routellmKey, providerName, modelId)
        if (!mounted.current) return
        setData((prev) =>
          prev
            ? {
                ...prev,
                models: prev.models.map((m) =>
                  m.id === modelId
                    ? {
                        ...m,
                        status: result.status as ModelStatus,
                        last_error: result.error,
                        last_checked_at: new Date().toISOString(),
                      }
                    : m,
                ),
              }
            : prev,
        )
        if (result.status === 'works') onTestedRef.current?.(true)
        if (result.status === 'denied') {
          setNotice(`${modelId} is not available to this key (model_not_found) — routing will skip it automatically.`)
        }
      } catch (err) {
        if (!mounted.current) return
        const apiError = toApiError(err)
        setErrorKind(apiError.kind)
        setErrorDetail(apiError.serverDetail ?? null)
      } finally {
        if (mounted.current) setBusyModel(null)
      }
    },
    [routellmKey, providerName],
  )

  const onTestAll = useCallback(async () => {
    const allPending = models.filter((m) => m.status !== 'works')
    if (allPending.length === 0) return

    // Free tier first, then unknown, then paid — the cap below is small, so
    // spend it where a test is cheapest and most likely to inform.
    const ordered = [...allPending].sort(
      (a, b) => (TIER_TEST_ORDER[a.tier] ?? 1) - (TIER_TEST_ORDER[b.tier] ?? 1),
    )
    // Capped: 8 calls per click, never "every model in the account".
    const pending = ordered.slice(0, TEST_ALL_CAP)
    const notTested = allPending.length - pending.length

    setNotice(null)
    setCooldownLeft(0)
    setBatchProgressSection({ tested: 0, capped: pending.length, total: allPending.length })
    if (notTested > 0) {
      setNotice(
        `Testing ${pending.length} of ${allPending.length} models (${notTested} not tested). ` +
          `Free models first; click Test all again in a minute for the next batch.`,
      )
    }

    const results = new Map<string, ProviderModelInfo>()
    let anyWorks = false
    let spent = 0
    let halted = false

    for (let i = 0; i < pending.length; i += TEST_BATCH_SIZE) {
      const batch = pending.slice(i, i + TEST_BATCH_SIZE)
      setTestingIds(new Set(batch.map((m) => m.id)))
      const outcomes = await Promise.allSettled(
        batch.map((m) => testProviderModel(routellmKey, providerName, m.id)),
      )
      outcomes.forEach((outcome, index) => {
        const model = batch[index]
        if (outcome.status === 'fulfilled') {
          results.set(model.id, {
            ...model,
            status: outcome.value.status as ModelStatus,
            last_error: outcome.value.error,
            last_checked_at: new Date().toISOString(),
          })
          if (outcome.value.status === 'works') anyWorks = true
          return
        }
        // A rejected call is usually the SERVER's guard saying "slow down".
        // Respect Retry-After instead of hammering the next batch into it.
        const apiError = toApiError(outcome.reason)
        if (apiError.status === 429) {
          halted = true
          setCooldownLeft(apiError.retryAfterSeconds ?? 60)
        }
      })
      spent += batch.length
      if (halted) break
      if (!mounted.current) return
      setBatchProgressSection({
        tested: spent,
        capped: pending.length,
        total: allPending.length,
      })
      // Pace: wait one slot before the next batch so the rolling window the
      // server enforces is never exceeded by our own clicks.
      if (spent < pending.length) {
        await new Promise((resolve) => setTimeout(resolve, testPacingMs))
        if (!mounted.current) return
      }
    }

    setTestingIds(new Set())
    setData((prev) =>
      prev
        ? {
            ...prev,
            models: prev.models.map((m) => results.get(m.id) ?? m),
          }
        : prev,
    )
    onTestedRef.current?.(anyWorks || models.some((m) => m.status === 'works'))
  }, [models, routellmKey, providerName, testPacingMs])

  /**
   * THE Test-connection button (Auto mode): one route, one code path with
   * routing. The server walks ranked candidates, skipping models this key
   * was already denied; `attempted`/`skipped` report that walk. After a
   * verdict the model list is refreshed so status chips agree with it.
   */
  const onTestConnection = useCallback(async () => {
    setConnTesting(true)
    setNotice(null)
    try {
      const result = await testProvider(routellmKey, providerName)
      if (!mounted.current) return
      setConnResult(result)
      if (result.verdict === 'valid') onTestedRef.current?.(true)
      // The server-side test writes per-model statuses (works/denied);
      // refresh quietly so the chips match the verdict.
      void load(true)
    } catch (err) {
      if (!mounted.current) return
      setConnResult(null)
      const apiError = toApiError(err)
      setNotice(
        `Could not run the test: ${apiError.serverDetail ?? apiError.message}`,
      )
    } finally {
      if (mounted.current) setConnTesting(false)
    }
  }, [routellmKey, providerName, load])

  /**
   * T1: run the in-app diagnostics (replaces manual terminal work). The
   * server returns the full wire report — list counts, transport, probe
   * table — with every string already scrubbed of keys.
   */
  const onDiagnostics = useCallback(async () => {
    setDiagBusy(true)
    setNotice(null)
    setDiagCopied(false)
    try {
      const result = await runProviderDiagnostics(routellmKey, providerName)
      if (!mounted.current) return
      setDiag(result)
    } catch (err) {
      if (!mounted.current) return
      const apiError = toApiError(err)
      setNotice(
        `Diagnostics failed: ${apiError.serverDetail ?? apiError.message}`,
      )
    } finally {
      if (mounted.current) setDiagBusy(false)
    }
  }, [routellmKey, providerName])

  /** Copy the server-built Markdown report (contains no keys). */
  const onCopyReport = useCallback(async () => {
    if (!diag) return
    try {
      await navigator.clipboard.writeText(diag.report_markdown)
      setDiagCopied(true)
      window.setTimeout(() => {
        if (mounted.current) setDiagCopied(false)
      }, 2500)
    } catch {
      setNotice('Copy failed — your browser blocked clipboard access.')
    }
  }, [diag])

  const onUseModel = useCallback(
    async (modelId: string | null) => {
      setBusyModel(modelId ?? '__auto__')
      setNotice(null)
      try {
        const result = await setPinnedModel(routellmKey, providerName, modelId)
        if (!mounted.current) return
        setData(result)
        setNotice(
          modelId === null
            ? 'Auto mode: the router picks the newest ranked model and skips ones that fail.'
            : `${modelId} pinned for ${providerDisplay} routing.`,
        )
      } catch (err) {
        if (!mounted.current) return
        const apiError = toApiError(err)
        setErrorKind(apiError.kind)
        setErrorDetail(apiError.serverDetail ?? null)
      } finally {
        if (mounted.current) setBusyModel(null)
      }
    },
    [routellmKey, providerName, providerDisplay],
  )

  const hiddenCount = data?.hidden_non_chat_count ?? 0

  return (
    <section
      aria-label={`${providerDisplay} model explorer`}
      className="rounded-xl border border-amber-400/20 bg-amber-400/[0.04] p-4 backdrop-blur-sm"
    >
      {/* Pinned / Auto state — always prominent */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="rl-eyebrow">Routing model</span>
        {autoMode ? (
          <span
            className="rl-badge border-amber-400/30 bg-amber-400/10 text-amber-300"
            data-testid={`auto-badge-${providerName}`}
          >
            <Zap size={12} aria-hidden="true" /> Auto — router chooses, failing models are skipped
          </span>
        ) : (
          <span className="rl-badge border-accent/25 bg-accent/10 text-accent" data-testid={`pin-badge-${providerName}`}>
            <Pin size={12} aria-hidden="true" /> {pinned}
          </span>
        )}
        <button
          type="button"
          role="switch"
          aria-checked={autoMode}
          aria-label={`Auto mode for ${providerDisplay}`}
          onClick={() => void onUseModel(autoMode ? recommended?.id ?? models[0]?.id ?? null : null)}
          disabled={busyModel !== null}
          className="rl-btn-ghost px-2 py-1 text-xs disabled:opacity-40"
        >
          {busyModel === '__auto__' ? (
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          ) : null}
          {autoMode ? 'Turn Auto off' : 'Turn Auto on'}
        </button>
        <button
          type="button"
          onClick={() => void onTestConnection()}
          disabled={connTesting}
          className="rl-btn-ghost border border-white/[0.1] px-2.5 py-1 text-xs disabled:opacity-40"
          aria-label={`Test ${providerDisplay} connection`}
          data-testid={`test-connection-${providerName}`}
        >
          {connTesting ? (
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          ) : (
            <ShieldQuestion size={12} aria-hidden="true" />
          )}
          {connTesting ? 'Testing…' : 'Test connection (Auto)'}
        </button>
        {connResult && (
          <span
            className={`rl-badge ${VERDICT_STYLE[connResult.verdict].className}`}
            role="status"
            aria-label={`${providerDisplay} connection status`}
            data-testid={`connection-verdict-${providerName}`}
          >
            {connResult.verdict === 'valid' ? (
              <CheckCircle2 size={12} aria-hidden="true" />
            ) : (
              <AlertTriangle size={12} aria-hidden="true" />
            )}
            {VERDICT_STYLE[connResult.verdict].label}
            {connResult.model && (
              <>
                {' · '}
                <code className="font-mono">{connResult.model}</code>
              </>
            )}
          </span>
        )}
      </div>

      {/* The walk report: which candidates were tried or skipped, in order. */}
      {connResult && (connResult.attempted?.length || connResult.skipped?.length) ? (
        <ul
          className="mt-2 space-y-1 text-xs text-text-secondary"
          data-testid={`connection-walk-${providerName}`}
          aria-label={`${providerDisplay} candidate walk`}
        >
          {(connResult.skipped ?? []).map((step) => (
            <li key={`skip-${step.model}`} className="flex items-center gap-1.5">
              <CircleSlash size={11} aria-hidden="true" />
              <code className="font-mono">{step.model}</code> skipped — {step.reason}
            </li>
          ))}
          {(connResult.attempted ?? []).map((step) => (
            <li key={`try-${step.model}`} className="flex items-center gap-1.5">
              {step.success ? (
                <CheckCircle2 size={11} className="text-success" aria-hidden="true" />
              ) : (
                <XCircle size={11} className="text-danger" aria-hidden="true" />
              )}
              <code className="font-mono">{step.model}</code>
              {step.success ? ' worked' : ` failed — ${step.reason}`}
            </li>
          ))}
        </ul>
      ) : null}
      {connResult && connResult.verdict !== 'valid' && connResult.error && (
        <p className="mt-2 rounded-lg border border-white/[0.06] bg-white/[0.02] px-2.5 py-2 text-xs leading-relaxed text-text-secondary">
          {connResult.error}
        </p>
      )}

      {/* T1: in-app diagnostics — no terminal work needed. */}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void onDiagnostics()}
          disabled={diagBusy}
          className="rl-btn-ghost border border-white/[0.1] px-2.5 py-1 text-xs disabled:opacity-40"
          aria-label={`Run ${providerDisplay} diagnostics`}
          data-testid={`run-diagnostics-${providerName}`}
        >
          {diagBusy ? (
            <Loader2 size={12} className="animate-spin" aria-hidden="true" />
          ) : (
            <Stethoscope size={12} aria-hidden="true" />
          )}
          {diagBusy ? 'Running…' : 'Run diagnostics'}
        </button>
        {diag && (
          <>
            <button
              type="button"
              onClick={() => void onCopyReport()}
              className="rl-btn-ghost border border-white/[0.1] px-2.5 py-1 text-xs"
              aria-label={`Copy the ${providerDisplay} diagnostics report`}
              data-testid={`copy-report-${providerName}`}
            >
              {diagCopied ? (
                <ClipboardCheck size={12} aria-hidden="true" />
              ) : (
                <Copy size={12} aria-hidden="true" />
              )}
              {diagCopied ? 'Copied' : 'Copy report'}
            </button>
            <span
              className="rl-badge border-white/[0.08] bg-white/[0.04] text-text-secondary"
              data-testid={`diag-no-keys-${providerName}`}
            >
              Contains no keys
            </span>
          </>
        )}
      </div>

      {diag && (
        <div
          className="mt-2 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 text-xs text-text-secondary"
          data-testid={`diagnostics-${providerName}`}
          aria-label={`${providerDisplay} diagnostics report`}
        >
          <p className="leading-relaxed">
            {diag.notice} List call: HTTP {diag.list_http_status ?? '—'}
            {diag.list_count_before_filter !== null &&
              ` — ${diag.list_count_before_filter} entries before filtering, ${diag.list_count_after_filter} chat-capable after`}
            . Key transport: {diag.key_transport}. API {diag.api_version} at{' '}
            {diag.api_host}. App {diag.app_version}, Python {diag.python_version}
            .{diag.discovery_reason ? ` Note: ${diag.discovery_reason}` : ''}
          </p>
          {diag.rows.length > 0 && (
            <table className="mt-2 w-full text-left text-xs">
              <thead>
                <tr className="text-text-secondary">
                  <th scope="col" className="py-1 pr-2 font-normal">model</th>
                  <th scope="col" className="py-1 pr-2 font-normal">HTTP</th>
                  <th scope="col" className="py-1 pr-2 font-normal">provider status</th>
                  <th scope="col" className="py-1 pr-2 font-normal">outcome</th>
                  <th scope="col" className="py-1 text-right font-normal">ms</th>
                </tr>
              </thead>
              <tbody>
                {diag.rows.map((row) => (
                  <tr
                    key={row.model_id}
                    data-testid={`diag-row-${providerName}-${row.model_id}`}
                  >
                    <td className="py-1 pr-2 font-mono">{row.model_id}</td>
                    <td className="py-1 pr-2">{row.http_status ?? '—'}</td>
                    <td className="py-1 pr-2">
                      {row.provider_status ?? '—'}
                      {row.provider_reason ? ` · ${row.provider_reason}` : ''}
                    </td>
                    <td className="py-1 pr-2">
                      {row.outcome ?? (row.success ? 'works' : '—')}
                    </td>
                    <td className="py-1 text-right">{Math.round(row.latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Controls */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-xs text-text-secondary">
          <span className="sr-only">Sort models</span>
          <select
            aria-label={`Sort ${providerDisplay} models`}
            className="rl-input py-1.5 text-xs"
            value={sort}
            onChange={(e) => updateParams({ sort: e.target.value })}
          >
            <option value="tier">Free first, newest</option>
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="name">Name</option>
          </select>
        </label>
        <div role="group" aria-label={`Tier filter for ${providerDisplay}`} className="flex gap-1">
          {(['all', 'free', 'paid'] as const).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={tier === value}
              onClick={() => updateParams({ tier: value })}
              className={`rounded-lg border px-2.5 py-1 text-xs font-medium transition ${
                tier === value
                  ? 'border-amber-400/40 bg-amber-400/15 text-amber-200'
                  : 'border-white/[0.08] bg-white/[0.03] text-text-secondary hover:text-text-primary'
              }`}
            >
              {value === 'all' ? 'All' : value === 'free' ? 'Free' : 'Paid'}
            </button>
          ))}
        </div>
        <label className="relative min-w-[160px] flex-1">
          <span className="sr-only">Search models</span>
          <Search size={12} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-text-secondary" />
          <input
            type="search"
            aria-label={`Search ${providerDisplay} models`}
            className="rl-input py-1.5 pl-7 text-xs"
            placeholder="Search models…"
            value={search}
            onChange={(e) => updateParams({ q: e.target.value })}
          />
        </label>
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={loading}
          className="rl-btn-ghost px-2.5 py-1.5 text-xs disabled:opacity-40"
          aria-label={`Refresh ${providerDisplay} models`}
        >
          <RefreshCw size={12} aria-hidden="true" className={loading ? 'animate-spin' : undefined} />
          Refresh
        </button>
        <button
          type="button"
          onClick={() => void onTestAll()}
          disabled={loading || testingIds.size > 0 || models.length === 0 || cooldownLeft > 0}
          className="rl-btn-ghost border border-amber-400/25 px-2.5 py-1.5 text-xs text-amber-200 disabled:opacity-40"
          aria-label={`Test all ${providerDisplay} models`}
        >
          <FlaskConical size={12} aria-hidden="true" />
          {batchProgress
            ? `Testing ${batchProgress.tested}/${batchProgress.capped} of ${batchProgress.total}…`
            : 'Test all (paced)'}
        </button>
      </div>

      {hiddenCount > 0 && (
        <p className="mt-2 text-xs text-text-secondary" role="note">
          {hiddenCount} non-chat model{hiddenCount === 1 ? '' : 's'} hidden (embeddings, TTS, image…).
        </p>
      )}

      {cooldownLeft > 0 && (
        <p
          role="status"
          data-testid="test-all-cooldown"
          className="mt-2 rounded-lg border border-warning/25 bg-warning/10 px-3 py-2 text-xs text-warning"
        >
          Rate limited by the provider guard — retry in {cooldownLeft}s.
          {batchProgress
            ? ` Tested ${batchProgress.tested} of ${batchProgress.capped} before stopping.`
            : ''}
        </p>
      )}

      {notice && (
        <p role="status" className="mt-2 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-xs text-text-secondary">
          {notice}
        </p>
      )}

      {errorKind && (
        <div className="mt-3">
          <ErrorState kind={errorKind} detail={errorDetail ?? undefined} onRetry={() => void load(false)} />
        </div>
      )}

      {loading && !errorKind && (
        <div className="mt-3 space-y-2" aria-busy="true" aria-label={`Loading ${providerDisplay} models`} role="status">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} className="flex items-center gap-3 rounded-lg border border-white/[0.06] p-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="h-4 w-14 rounded-full" />
              <Skeleton className="h-4 w-16" />
              <Skeleton className="ml-auto h-7 w-24 rounded-lg" />
            </div>
          ))}
        </div>
      )}

      {!loading && !errorKind && models.length === 0 && (
        <div className="mt-3 rounded-lg border border-white/[0.08] bg-white/[0.03] px-4 py-5 text-center text-sm text-text-secondary" role="status">
          {data?.reason
            ? `No models found — ${data.reason}`
            : 'No models found, check the key or refresh.'}
        </div>
      )}

      {!loading && !errorKind && models.length > 0 && (
        <ul className="mt-3 space-y-2" aria-label={`${providerDisplay} models`}>
          {models.map((model) => {
            const chip = STATUS_CHIP[model.status]
            const isPinned = pinned === model.id
            const isRecommended = recommended?.id === model.id
            const isTesting = testingIds.has(model.id) || busyModel === model.id
            return (
              <li
                key={model.id}
                data-testid={`model-row-${providerName}-${model.id}`}
                className={`rounded-lg border p-3 transition ${
                  isPinned
                    ? 'border-accent/40 bg-accent/[0.07]'
                    : 'border-white/[0.06] bg-white/[0.02] hover:border-white/[0.12]'
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-sm text-text-primary">{model.id}</code>
                  <span
                    className={`rl-badge ${TIER_BADGE[model.tier]}`}
                    title={`Tier is an estimate — the ${providerDisplay} API does not expose pricing (tier_source: ${model.tier_source})`}
                  >
                    {model.tier === 'free' ? 'FREE' : model.tier === 'paid' ? 'PAID' : 'UNKNOWN'}
                  </span>
                  {model.released_or_version && (
                    <span className="text-xs text-text-secondary">v{model.released_or_version}</span>
                  )}
                  <span className="text-xs text-text-secondary">{formatContext(model.context_window)}</span>
                  <span className={`rl-badge ${chip.className}`} data-testid={`status-${providerName}-${model.id}`}>
                    {model.status === 'works' && <BadgeCheck size={12} aria-hidden="true" />}
                    {model.status === 'denied' && <XCircle size={12} aria-hidden="true" />}
                    {model.status === 'rate_limited' && <Clock size={12} aria-hidden="true" />}
                    {model.status === 'untested' && <CircleSlash size={12} aria-hidden="true" />}
                    {chip.label}
                  </span>
                  {isRecommended && model.status === 'works' && (
                    <span className="rl-badge border-success/25 bg-success/10 text-success">
                      <CheckCircle2 size={12} aria-hidden="true" /> Recommended
                    </span>
                  )}
                  {isPinned && (
                    <span className="rl-badge border-accent/25 bg-accent/10 text-accent">
                      <Pin size={12} aria-hidden="true" /> Pinned
                    </span>
                  )}
                  <span className="ml-auto flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => void onTest(model.id)}
                      disabled={isTesting}
                      className="rl-btn-ghost px-2.5 py-1 text-xs disabled:opacity-40"
                      aria-label={`Test ${model.id}`}
                    >
                      {isTesting ? (
                        <Loader2 size={12} className="animate-spin" aria-hidden="true" />
                      ) : (
                        <FlaskConical size={12} aria-hidden="true" />
                      )}
                      Test
                    </button>
                    <button
                      type="button"
                      onClick={() => void onUseModel(model.id)}
                      disabled={busyModel !== null || isPinned}
                      className="rl-btn-ghost border border-white/[0.1] px-2.5 py-1 text-xs disabled:opacity-40"
                      aria-label={`Use ${model.id} for routing`}
                    >
                      Use this model
                    </button>
                  </span>
                </div>
                {model.last_error && model.status !== 'works' && (
                  <p className="mt-1.5 text-xs text-text-secondary" role="note">
                    Last failure: {model.last_error}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <details className="mt-3 text-xs text-text-secondary">
        <summary className="cursor-pointer select-none" aria-label={`About ${providerDisplay} tiers and statuses`}>
          <ChevronDown size={10} aria-hidden="true" className="mr-1 inline" />
          What do the badges mean?
          <ChevronUp size={10} aria-hidden="true" className="ml-1 inline" />
        </summary>
        <p className="mt-1.5 leading-relaxed">
          FREE/PAID are estimates from a static table (the provider's list API does not expose
          pricing); unmatched models show UNKNOWN. "Works" = a real 1-token call succeeded.
          "Denied" = the provider answered model_not_found for this key (e.g. a model that is
          listed but not available to new accounts) — Auto routing skips it for about an hour.
          "Rate limited" is transient and is never treated as denied.
        </p>
      </details>
    </section>
  )
}
