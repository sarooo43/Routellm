import { useEffect, useRef, useState } from 'react'

/**
 * Counts a numeric display value from its previous value up to `target`
 * using requestAnimationFrame with an ease-out curve. Keeps the component
 * dependency-light (no countup library).
 */
export function useCountUp(target: number, durationMs = 800): number {
  const [value, setValue] = useState(0)
  const previousRef = useRef(0)

  useEffect(() => {
    const from = previousRef.current
    previousRef.current = target
    if (from === target) return

    const start = performance.now()
    let raf = 0

    const tick = () => {
      // Read the clock here (NOT from the rAF callback's timestamp): some
      // environments (jsdom tests) hand the callback a timestamp captured
      // before `start`, which would produce a negative elapsed time.
      const progress = Math.min((performance.now() - start) / durationMs, 1)
      const eased = Math.max(0, 1 - Math.pow(1 - progress, 3)) // easeOutCubic, clamped
      setValue(from + (target - from) * eased)
      if (progress < 1) {
        raf = requestAnimationFrame(tick)
      } else {
        // Clamp to the exact target so the final render shows a crisp number.
        setValue(target)
      }
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, durationMs])

  return value
}
