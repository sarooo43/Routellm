import { motion } from 'framer-motion'
import { useEffect } from 'react'
import { ModelHealthCard } from '../components/ModelHealthCard'
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
  const fetchModels = useAppStore((state) => state.fetchModels)

  useEffect(() => {
    fetchModels()
    const interval = setInterval(fetchModels, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [fetchModels])

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
              />
            </motion.div>
          ))}
        </motion.div>
      )}
    </div>
  )
}
