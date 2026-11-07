import type { CSSProperties } from 'react'

/**
 * Shimmer "skeleton" block for loading placeholders, tinted to sit inside the
 * dark glass cards without flashing to a different visual language.
 *
 * Reduced motion is handled globally (index.css forces animation durations to
 * 0.01ms under prefers-reduced-motion), and the decoration is aria-hidden;
 * the surrounding region should carry its own accessible loading signal
 * (e.g. aria-busy on the card grid container).
 */

export function Skeleton({
  className = '',
  style,
}: {
  className?: string
  style?: CSSProperties
}) {
  return (
    <div
      aria-hidden="true"
      className={`rl-skeleton ${className}`}
      style={style}
    />
  )
}

/** Full skeleton grid shaped like the ModelCatalog card grid (6 cards). */
export function CatalogSkeletonGrid() {
  return (
    <div
      className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3"
      aria-busy="true"
      aria-label="Loading model catalog"
      role="status"
    >
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="rl-card p-5">
          <div className="flex items-start justify-between gap-3">
            <Skeleton className="h-5 w-2/5" />
            <Skeleton className="h-6 w-16 rounded-full" />
          </div>
          <Skeleton className="mt-4 h-3 w-3/4" />
          <Skeleton className="mt-2 h-3 w-1/2" />
          <div className="mt-6 grid grid-cols-2 gap-3">
            <Skeleton className="h-9 rounded-xl" />
            <Skeleton className="h-9 rounded-xl" />
          </div>
        </div>
      ))}
    </div>
  )
}
