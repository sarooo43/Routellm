import { motion } from 'framer-motion'
import { useState } from 'react'
import { Zap } from 'lucide-react'
import * as apiClient from '../api/client'

export const CHAOS_COLOR = '#B388FF' // distinct purple: artificial, not real-red

/**
 * DEV/DEMO ONLY. The inject/reset affordance on a ModelHealthCard:
 * - inactive provider → "⚡ Simulate Outage" (POST /admin/chaos/inject, 60s)
 * - active provider   → "⚡ Chaos Injected" badge + "Reset Now"
 *                        (POST /admin/chaos/clear)
 * Rendered only when the backend reports chaos_mode_enabled; fully inert
 * elsewhere. The badge stays while chaos is live and disappears when the
 * TTL expires or the user resets.
 */
export function ChaosControl({
  providerName,
  chaosActive,
  onInjected,
  onCleared,
}: {
  providerName: string
  chaosActive: boolean
  /** Called after a successful inject/clear so the page can toast. */
  onInjected?: (providerName: string, expiresInSeconds: number) => void
  onCleared?: (providerName: string) => void
}) {
  const [busy, setBusy] = useState(false)

  const inject = async () => {
    setBusy(true)
    try {
      const response = await apiClient.injectChaos({
        provider_name: providerName,
      })
      onInjected?.(response.provider_name, response.expires_in_seconds)
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    setBusy(true)
    try {
      await apiClient.clearChaos({ provider_name: providerName })
      onCleared?.(providerName)
    } finally {
      setBusy(false)
    }
  }

  if (chaosActive) {
    return (
      <div className="flex items-center justify-between gap-3 mt-4">
        <motion.span
          data-testid="chaos-badge"
          className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-widest px-2.5 py-1 rounded-full"
          style={{ color: CHAOS_COLOR, backgroundColor: 'rgba(179, 136, 255, 0.15)' }}
          animate={{ opacity: [1, 0.6, 1], boxShadow: [
            `0 0 0px 0px ${CHAOS_COLOR}00`,
            `0 0 10px 2px ${CHAOS_COLOR}55`,
            `0 0 0px 0px ${CHAOS_COLOR}00`,
          ] }}
          transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
        >
          <Zap size={11} aria-hidden="true" />
          Chaos Injected
        </motion.span>

        <button
          type="button"
          data-testid="chaos-reset"
          onClick={clear}
          disabled={busy}
          className="text-xs font-semibold px-3 py-1.5 rounded-lg border border-border hover:border-[#B388FF] hover:text-[#B388FF] transition-colors disabled:opacity-50"
        >
          {busy ? 'Resetting…' : 'Reset Now'}
        </button>
      </div>
    )
  }

  return (
    <div className="flex justify-end mt-4">
      <button
        type="button"
        data-testid="chaos-inject"
        onClick={inject}
        disabled={busy}
        className="inline-flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border border-border hover:border-[#B388FF] hover:text-[#B388FF] transition-colors disabled:opacity-50"
      >
        <Zap size={12} aria-hidden="true" />
        {busy ? 'Injecting…' : 'Simulate Outage'}
      </button>
    </div>
  )
}
