import { motion } from 'framer-motion'
import type { ReactNode } from 'react'

interface GlassCardProps {
  children: ReactNode
  className?: string
  /** Set false for cards whose hover transform would fight inner animations (e.g. graphs). */
  hoverable?: boolean
  onClick?: () => void
}

const ACCENT_RGB = '232, 119, 34'

export function GlassCard({
  children,
  className = '',
  hoverable = true,
  onClick,
}: GlassCardProps) {
  const hasCustomPadding = /(?:^|\s)p[xytrbl]?-/.test(className)

  return (
    <motion.div
      onClick={onClick}
      whileHover={
        hoverable
          ? {
              y: -2,
              borderColor: `rgba(${ACCENT_RGB}, 0.28)`,
              boxShadow: `0 0 32px rgba(${ACCENT_RGB}, 0.10), 0 16px 40px -28px rgba(0,0,0,0.9)`,
            }
          : undefined
      }
      transition={{ type: 'spring', stiffness: 300, damping: 26 }}
      className={`rl-card ${hasCustomPadding ? '' : 'p-6'} ${className}`}
    >
      {children}
    </motion.div>
  )
}
