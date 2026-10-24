import { motion } from 'framer-motion'

export function Topbar() {
  return (
    <header className="sticky top-0 z-20 h-16 flex items-center justify-between px-8 border-b border-border bg-background/70 backdrop-blur-xl">
      <h1 className="text-lg font-semibold tracking-wide">
        <span className="text-text-primary">Route</span>
        <span className="text-accent">LLM</span>
      </h1>

      <div className="flex items-center gap-2" aria-label="System status: live">
        <motion.span
          className="w-2.5 h-2.5 rounded-full bg-success"
          animate={{ scale: [1, 1.2, 1], opacity: [1, 0.7, 1] }}
          transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
        />
        <span className="text-xs uppercase tracking-widest text-text-secondary">
          System Live
        </span>
      </div>
    </header>
  )
}
