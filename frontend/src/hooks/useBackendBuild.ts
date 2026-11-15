import { useEffect, useState } from 'react'
import axios from 'axios'

/**
 * T1 stale-build detection: the backend's build identity from /health.
 *
 * Probes on mount and every 60 s (the connection pill already covers faster
 * liveness; this only needs to notice a restarted backend). Fail-soft: any
 * probe problem leaves the fields unknown and the banner stays quiet — a
 * missing build identity must never become a false "versions mismatch".
 */
export type BackendBuild = {
  commit: string | null
  commitTime: string | null
}

const POLL_MS = 60_000

export function useBackendBuild(): BackendBuild {
  const [build, setBuild] = useState<BackendBuild>({ commit: null, commitTime: null })

  useEffect(() => {
    let cancelled = false
    const probe = async () => {
      try {
        const resp = await axios.get('/health', { timeout: 5_000 })
        if (cancelled) return
        const raw = resp.data?.build as { git_commit?: unknown; git_commit_time?: unknown } | undefined
        setBuild({
          commit: typeof raw?.git_commit === 'string' && raw.git_commit ? raw.git_commit : null,
          commitTime:
            typeof raw?.git_commit_time === 'string' && raw.git_commit_time ? raw.git_commit_time : null,
        })
      } catch {
        if (!cancelled) setBuild({ commit: null, commitTime: null })
      }
    }
    void probe()
    const timer = window.setInterval(() => void probe(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  return build
}
