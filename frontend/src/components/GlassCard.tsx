import { motion } from 'framer-motion'
import type { ReactNode } from 'react'

interface GlassCardProps {
  children: ReactNode
  className?: string
  /** Set false for cards whose hover transform would fight inner animations (e.g. graphs). */
  hoverable?: boolean
  onClick?: () => void
}

const ACCENT_RGB = '232, 119, 34' // hermes orange #E87722

export function GlassCard({
  children,
  className = '',
  hoverable = true,
  onClick,
}: GlassCardProps) {
  return (
    <motion.div
      onClick={onClick}
      whileHover={
        hoverable
          ? {
              scale: 1.02,
              borderColor: `rgba(${ACCENT_RGB}, 0.35)`,
              boxShadow: `0 0 32px rgba(${ACCENT_RGB}, 0.15)`,
            }
          : undefined
      }
      transition={{ type: 'spring', stiffness: 260, damping: 22 }}
      className={`rounded-2xl bg-surface backdrop-blur-xl border border-border p-6 ${className}`}
    >
      {children}
    </motion.div>
  )
}
