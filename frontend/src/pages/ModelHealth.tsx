import { motion, AnimatePresence } from 'framer-motion'
import { useEffect, useState } from 'react'
import { Activity, Zap } from 'lucide-react'
import { ModelHealthCard } from '../components/ModelHealthCard'
import { PageHeader } from '../components/PageHeader'
import { CHAOS_COLOR } from '../components/ChaosControl'
import { useAppStore } from '../store/useAppStore'

const POLL_INTERVAL_MS = 5000

const containerVariants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06 } },
}

const itemVariants = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { duration: 0.4, ease: 'easeOut' as const } },
}

export function ModelHealth() {
  const models = useAppStore((state) => state.models)
  const chaosModeEnabled = useAppStore((state) => state.chaosModeEnabled)
  const fetchModels = useAppStore((state) => state.fetchModels)
  const [toast, setToast] = useState<string | null>(null)
  const [toastVisible, setToastVisible] = useState(false)

  useEffect(() => {
    fetchModels()
    const interval = setInterval(fetchModels, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [fetchModels])

  const onChaosInjected = async (providerName: string, expiresInSeconds: number) => {
    await fetchModels()
    setToast(`⚡ Chaos injected on ${providerName} — auto-expires in ${expiresInSeconds}s`)
    setToastVisible(true)
    window.setTimeout(() => setToastVisible(false), 4000)
  }

  const onChaosCleared = async (providerName: string) => {
    await fetchModels()
    setToast(`${providerName} restored — chaos cleared`)
    setToastVisible(true)
    window.setTimeout(() => setToastVisible(false), 4000)
  }

  const healthy = models.filter((m) => m.status === 'healthy').length
  const degraded = models.filter((m) => m.status === 'degraded').length
  const open = models.filter((m) => m.status === 'circuit_open').length

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Operations"
        title="Model Health"
        description="Live provider status, quota pressure, and circuit-breaker state. Polls every 5 seconds."
        actions={
          <span className="rl-badge border-white/[0.08] bg-white/[0.03] text-text-secondary">
            <Activity size={12} aria-hidden="true" />
            {models.length} providers
          </span>
        }
      />

      {models.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          <HealthSummary label="Healthy" value={healthy} color="#3DDC97" />
          <HealthSummary label="Degraded" value={degraded} color="#F5A623" />
          <HealthSummary label="Circuit open" value={open} color="#FF4D4D" />
        </div>
      )}

      {models.length === 0 ? (
        <div className="rl-card px-6 py-14 text-center">
          <Activity size={24} className="mx-auto text-text-secondary/50" aria-hidden="true" />
          <p className="mt-3 text-sm text-text-secondary">
            Waiting for the first health snapshot from the backend…
          </p>
        </div>
      ) : (
        <motion.div
          variants={containerVariants}
          initial="hidden"
          animate="show"
          className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
        >
          {models.map((provider) => (
            <motion.div key={provider.name} variants={itemVariants}>
              <ModelHealthCard
                name={provider.name}
                status={provider.status}
                successRate={provider.success_rate_overall}
                requestsToday={provider.requests_today}
                quotaPercentageUsed={provider.quota_percentage_used}
                chaosEnabled={chaosModeEnabled}
                chaosActive={provider.chaos_active ?? false}
                onChaosInjected={onChaosInjected}
                onChaosCleared={onChaosCleared}
              />
            </motion.div>
          ))}
        </motion.div>
      )}

      <AnimatePresence>
        {toast && toastVisible && (
          <motion.div
            data-testid="chaos-toast"
            role="status"
            className="fixed bottom-6 right-6 z-30 flex max-w-[calc(100vw-3rem)] items-center gap-2 rounded-xl border px-4 py-3 text-sm shadow-2xl"
            style={{ borderColor: CHAOS_COLOR, backgroundColor: 'rgba(20, 16, 30, 0.95)', color: CHAOS_COLOR }}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 20 }}
          >
            <Zap size={14} aria-hidden="true" />
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function HealthSummary({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="rl-card flex items-center gap-3 px-4 py-3">
      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} />
      <div className="min-w-0">
        <p className="truncate text-[11px] uppercase tracking-widest text-text-secondary">{label}</p>
        <p className="text-lg font-semibold tabular-nums" style={{ color }}>
          {value}
        </p>
      </div>
    </div>
  )
}
