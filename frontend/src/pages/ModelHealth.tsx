import { motion, AnimatePresence } from 'framer-motion'
import { useEffect, useState } from 'react'
import { Zap } from 'lucide-react'
import { ModelHealthCard } from '../components/ModelHealthCard'
import { CHAOS_COLOR } from '../components/ChaosControl'
import { useAppStore } from '../store/useAppStore'

const POLL_INTERVAL_MS = 5000

const containerVariants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.08 } },
}

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: 'easeOut' as const } },
}

export function ModelHealth() {
  const models = useAppStore((state) => state.models)
  const chaosModeEnabled = useAppStore((state) => state.chaosModeEnabled)
  const fetchModels = useAppStore((state) => state.fetchModels)
  // DEV/DEMO ONLY — transient toast confirming a chaos injection/reset.
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

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Model Health</h2>
        <p className="mt-1 text-sm text-text-secondary">
          Live provider status — polls every 5 seconds.
        </p>
      </div>

      {models.length === 0 ? (
        <p className="text-sm text-text-secondary/70 py-10 text-center">
          Waiting for the first health snapshot from the backend…
        </p>
      ) : (
        <motion.div
          variants={containerVariants}
          initial="hidden"
          animate="show"
          className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4"
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
            className="fixed bottom-6 right-6 z-30 flex items-center gap-2 rounded-xl border px-4 py-3 text-sm"
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
