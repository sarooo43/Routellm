import { formatDistanceToNow } from 'date-fns'

/** Backend timestamps arrive ISO without timezone info — treat as UTC. */
export function parseBackendDate(iso: string): Date {
  const needsZulu = !/Z|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(needsZulu ? `${iso}Z` : iso.replace(/([+-]\d{2}(?::?\d{2})?)$/, ''))
}

export function relativeTime(iso: string): string {
  return `${formatDistanceToNow(parseBackendDate(iso), { addSuffix: true })}`
}

export function formatMs(value: number, decimals = 0): string {
  return `${value.toFixed(decimals)}ms`
}

export function formatPercent(value: number, decimals = 1): string {
  return `${(value * 100).toFixed(decimals)}%`
}

export function formatDollars(value: number, decimals = 6): string {
  return `$${value.toFixed(decimals)}`
}

export function formatCount(value: number): string {
  return value.toLocaleString('en-US')
}

/** Human-friendly context-window size: 64000 → "64K tokens", 1000000 → "1M tokens". */
export function formatTokenWindow(value: number): string {
  if (value >= 1_000_000) {
    const millions = value / 1_000_000
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M tokens`
  }
  if (value >= 1_000) {
    const thousands = value / 1_000
    return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}K tokens`
  }
  return `${value} tokens`
}
