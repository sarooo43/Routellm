import { AnimatePresence, motion } from 'framer-motion'
import { ArrowDownWideNarrow, LibraryBig, RefreshCw, SearchX } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { getCatalog } from '../api/client'
import { GlassCard } from '../components/GlassCard'
import { formatMs, formatPercent, formatTokenWindow } from '../lib/format'
import type { Capability, CatalogEntry, PricingTier, ProviderStatusValue } from '../types/api'

const CAPABILITIES: { value: Capability; label: string; color: string }[] = [
  { value: 'code', label: 'Code', color: '#E87722' },
  { value: 'reasoning', label: 'Reasoning', color: '#B388FF' },
  { value: 'summarization', label: 'Summarization', color: '#60A5FA' },
  { value: 'qa', label: 'QA', color: '#3DDC97' },
  { value: 'general', label: 'General', color: '#A0A0A0' },
]

const PRICING_FILTERS: { value: PricingTier | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'free', label: 'Free Only' },
  { value: 'cheap', label: 'Cheap' },
  { value: 'premium', label: 'Premium' },
]

const PRICING_META: Record<PricingTier, { label: string; color: string; bg: string }> = {
  free: { label: 'FREE', color: '#3DDC97', bg: 'rgba(61, 220, 151, 0.12)' },
  cheap: { label: 'CHEAP', color: '#D4AF37', bg: 'rgba(212, 175, 55, 0.12)' },
  premium: { label: 'PREMIUM', color: '#E87722', bg: 'rgba(232, 119, 34, 0.12)' },
}

const STATUS_META: Record<ProviderStatusValue, { label: string; color: string }> = {
  healthy: { label: 'Healthy', color: '#3DDC97' },
  degraded: { label: 'Degraded', color: '#F5A623' },
  circuit_open: { label: 'Circuit Open', color: '#FF4D4D' },
}

type SortKey = 'success_rate' | 'latency' | 'context_window'

export function ModelCatalog() {
  const [models, setModels] = useState<CatalogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedCapabilities, setSelectedCapabilities] = useState<Capability[]>([])
  const [pricing, setPricing] = useState<PricingTier | 'all'>('all')
  const [sort, setSort] = useState<SortKey>('success_rate')

  const fetchCatalog = async () => {
    try {
      setModels(await getCatalog())
      setError(null)
    } catch {
      setError('Could not load the models catalog. Is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  const load = () => {
    setLoading(true)
    setError(null)
    void fetchCatalog()
  }

  useEffect(() => {
    void fetchCatalog()
    // Initial load only; subsequent refreshes are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const visibleModels = useMemo(() => {
    const filtered = models.filter((model) => {
      const capabilityMatch =
        selectedCapabilities.length === 0 ||
        selectedCapabilities.some((capability) => model.capabilities.includes(capability))
      const pricingMatch = pricing === 'all' || model.pricing_tier === pricing
      return capabilityMatch && pricingMatch
    })

    return [...filtered].sort((a, b) => {
      if (sort === 'latency') return a.avg_latency_ms - b.avg_latency_ms
      if (sort === 'context_window') return b.context_window - a.context_window
      return b.live_success_rate - a.live_success_rate
    })
  }, [models, selectedCapabilities, pricing, sort])

  const toggleCapability = (capability: Capability) => {
    setSelectedCapabilities((current) =>
      current.includes(capability)
        ? current.filter((item) => item !== capability)
        : [...current, capability],
    )
  }

  const clearFilters = () => {
    setSelectedCapabilities([])
    setPricing('all')
  }

  return (
    <div className="space-y-6">
      <section className="rl-card relative overflow-hidden p-7 sm:p-9">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 bg-gradient-to-br from-accent/[0.12] via-transparent to-gold/[0.05]"
        />
        <div className="relative z-10 max-w-2xl">
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.25em] text-accent">
            <LibraryBig size={14} aria-hidden="true" />
            Models marketplace
          </div>
          <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Models Catalog</h2>
          <p className="mt-3 text-sm leading-relaxed text-text-secondary">
            Browse what each model can do, how much context it supports, and what it costs.
            This is a directory of capabilities and pricing — use Model Health for live
            operational status, quota pressure, and chaos controls.
          </p>
        </div>
        <div className="pointer-events-none absolute -right-16 -top-16 h-64 w-64 rounded-full bg-accent/10 blur-3xl" />
      </section>

      <GlassCard hoverable={false} className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">Filter by capability</h3>
            <p className="text-xs text-text-secondary">Matches any selected capability.</p>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            className="rl-btn-ghost border border-white/[0.07]"
          >
            <RefreshCw size={12} aria-hidden="true" />
            Refresh
          </button>
        </div>

        <div className="flex flex-wrap gap-2" role="group" aria-label="Capability filters">
          {CAPABILITIES.map(({ value, label, color }) => {
            const active = selectedCapabilities.includes(value)
            return (
              <button
                key={value}
                type="button"
                aria-pressed={active}
                onClick={() => toggleCapability(value)}
                className="rounded-full border px-3.5 py-1.5 text-xs font-medium transition-all hover:brightness-125"
                style={{
                  borderColor: active ? color : 'rgba(255,255,255,0.08)',
                  backgroundColor: active ? `${color}22` : 'transparent',
                  color: active ? color : '#A0A0A0',
                }}
              >
                {label}
              </button>
            )
          })}
        </div>

        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h3 className="mb-2 text-sm font-semibold">Pricing tier</h3>
            <div className="inline-flex flex-wrap rounded-xl border border-white/[0.07] bg-black/30 p-1" role="group" aria-label="Pricing tier filter">
              {PRICING_FILTERS.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={pricing === value}
                  onClick={() => setPricing(value)}
                  className={`rounded-lg px-3 py-1.5 text-xs transition-all ${
                    pricing === value
                      ? 'bg-accent font-semibold text-black shadow-md shadow-accent/20'
                      : 'text-text-secondary hover:bg-white/[0.05] hover:text-text-primary'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <label className="flex items-center gap-2 text-xs text-text-secondary">
            <ArrowDownWideNarrow size={14} aria-hidden="true" />
            Sort by
            <select
              aria-label="Sort models"
              value={sort}
              onChange={(event) => setSort(event.target.value as SortKey)}
              className="rl-select text-xs"
            >
              <option value="success_rate">Success rate</option>
              <option value="latency">Latency</option>
              <option value="context_window">Context window</option>
            </select>
          </label>
        </div>
      </GlassCard>

      {error && (
        <p role="alert" className="rounded-xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {!loading && !error && (
        <p className="text-xs text-text-secondary/70">
          Showing <span className="font-semibold text-text-primary">{visibleModels.length}</span> of{' '}
          {models.length} models
        </p>
      )}

      {loading ? (
        <div className="rl-card py-14 text-center text-sm text-text-secondary">Loading catalog…</div>
      ) : visibleModels.length === 0 ? (
        <GlassCard hoverable={false} className="py-12 text-center">
          <SearchX size={28} className="mx-auto text-text-secondary" aria-hidden="true" />
          <p className="mt-3 text-sm text-text-secondary">No models match these filters.</p>
          <button
            type="button"
            onClick={clearFilters}
            className="rl-btn-secondary mt-4 text-xs"
          >
            Clear filters
          </button>
        </GlassCard>
      ) : (
        <motion.div layout className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
          <AnimatePresence mode="popLayout">
            {visibleModels.map((model) => (
              <motion.div
                layout
                key={model.provider_name}
                data-testid="catalog-card"
                data-provider={model.provider_name}
                initial={{ opacity: 0, scale: 0.96 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.96 }}
                transition={{ duration: 0.2 }}
              >
                <CatalogCard model={model} />
              </motion.div>
            ))}
          </AnimatePresence>
        </motion.div>
      )}
    </div>
  )
}

function CatalogCard({ model }: { model: CatalogEntry }) {
  const pricing = PRICING_META[model.pricing_tier]
  const status = STATUS_META[model.current_status]

  return (
    <GlassCard className="flex h-full flex-col overflow-hidden p-0">
      <div className="h-1 w-full" style={{ backgroundColor: pricing.color }} />
      <div className="flex flex-1 flex-col p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[10px] uppercase tracking-widest text-text-secondary">
              {model.provider_name}
            </p>
            <h3 className="mt-1 text-lg font-semibold tracking-tight">{model.display_name}</h3>
          </div>
          <span
            className="shrink-0 rounded-full px-2.5 py-1 text-[10px] font-bold tracking-widest"
            style={{ color: pricing.color, backgroundColor: pricing.bg }}
          >
            {pricing.label}
          </span>
        </div>

        <p className="mt-3 flex-1 text-sm leading-relaxed text-text-secondary">
          {model.description}
        </p>

        <div className="mt-4 flex flex-wrap gap-1.5">
          {model.capabilities.map((capability) => {
            const meta = CAPABILITIES.find((item) => item.value === capability)
            return (
              <span
                key={capability}
                className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                style={{
                  color: meta?.color ?? '#A0A0A0',
                  backgroundColor: `${meta?.color ?? '#A0A0A0'}18`,
                }}
              >
                {meta?.label ?? capability}
              </span>
            )
          })}
        </div>

        <div className="mt-5 rounded-xl border border-white/[0.06] bg-black/25 p-4">
          <p className="text-[10px] uppercase tracking-widest text-text-secondary">Context window</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">
            {formatTokenWindow(model.context_window)}
          </p>
          <p className="mt-1 text-xs text-text-secondary">
            ${model.cost_per_1k_tokens.toFixed(4)} / 1K tokens
          </p>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div>
            <p className="text-[10px] uppercase tracking-widest text-text-secondary">Live success</p>
            <p className="mt-1 font-semibold tabular-nums text-success">
              {formatPercent(model.live_success_rate, 1)}
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-widest text-text-secondary">Avg latency</p>
            <p className="mt-1 font-semibold tabular-nums">{formatMs(model.avg_latency_ms, 0)}</p>
          </div>
        </div>

        <div className="mt-4 flex items-center gap-2 border-t border-white/[0.06] pt-4 text-xs text-text-secondary">
          <span
            aria-hidden="true"
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: status.color }}
          />
          {status.label}
        </div>
      </div>
    </GlassCard>
  )
}
