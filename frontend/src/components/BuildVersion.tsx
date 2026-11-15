import { AlertTriangle } from 'lucide-react'
import { frontendBuildCommit } from '../buildInfo'
import { useBackendBuild } from '../hooks/useBackendBuild'

/**
 * T1 stale-build detection, rendered.
 *
 * - `BuildVersionBanner`: a fixed amber strip when the frontend bundle was
 *   built from a DIFFERENT commit than the running backend — the classic
 *   "old server still up" state the owner hit after PR #16 merged (walk and
 *   diagnostics looked broken because the old bundle was still served).
 *   Quiet whenever either side is unknown: no identity, no claim.
 * - `BuildVersionChips`: the footer one-liner showing both commits.
 */

export function buildMismatch(frontend: string, backend: string | null): boolean {
  return backend !== null && frontend !== 'unknown' && frontend !== backend
}

export function BuildVersionBanner() {
  const backend = useBackendBuild()
  const frontend = frontendBuildCommit()

  if (!buildMismatch(frontend, backend.commit)) return null

  return (
    <div
      role="status"
      data-testid="build-mismatch-banner"
      aria-label="Frontend and backend builds differ"
      className="mb-3 flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/[0.08] px-3 py-2 text-xs text-warning"
    >
      <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>
        Frontend build (<code className="font-mono">{frontend}</code>) does not match the running
        backend (<code className="font-mono">{backend.commit}</code>). The page may be stale —
        restart <code className="font-mono">python run.py</code>; it rebuilds the dashboard when
        sources changed.
      </span>
    </div>
  )
}

export function BuildVersionChips() {
  const backend = useBackendBuild()
  const frontend = frontendBuildCommit()

  return (
    <span data-testid="build-version-chips">
      frontend <code className="font-mono">{frontend}</code>
      <span className="mx-1.5 text-text-secondary/30">·</span>
      backend <code className="font-mono">{backend.commit ?? '—'}</code>
    </span>
  )
}
