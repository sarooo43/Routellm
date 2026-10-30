import { create } from 'zustand'
import * as apiClient from '../api/client'
import type { Analytics, LogsQuery, ProviderStatus, RouteResponse, TaskLog } from '../types/api'

const LOGS_PAGE_LIMIT = 50

interface AppState {
  models: ProviderStatus[]
  /** DEV/DEMO ONLY — mirrored backend CHAOS_MODE_ENABLED flag. */
  chaosModeEnabled: boolean
  logs: TaskLog[]
  logsTotal: number
  analytics: Analytics | null
  loading: boolean
  error: string | null

  // "Try it Live" demo state (Overview page) — sends a real /route request.
  tryItPrompt: string
  tryItResponse: RouteResponse | null
  tryItSending: boolean

  fetchModels: () => Promise<void>
  fetchLogs: (query?: LogsQuery) => Promise<void>
  fetchAnalytics: () => Promise<void>
  /** Poll everything the dashboard needs; safe to call on an interval. */
  pollAll: () => Promise<void>
  setTryItPrompt: (prompt: string) => void
  sendTryItRequest: () => Promise<void>
}

export const useAppStore = create<AppState>((set) => ({
  models: [],
  chaosModeEnabled: false,
  logs: [],
  logsTotal: 0,
  analytics: null,
  loading: false,
  error: null,

  fetchModels: async () => {
    try {
      const { chaos_mode_enabled: chaosModeEnabled, providers: models } =
        await apiClient.getModels()
      set({ models, chaosModeEnabled, error: null })
    } catch (err) {
      set({ error: describeError(err) })
    }
  },

  fetchLogs: async (query: LogsQuery = { limit: LOGS_PAGE_LIMIT, offset: 0 }) => {
    try {
      const { total, logs } = await apiClient.getLogs(query)
      set({ logs, logsTotal: total, error: null })
    } catch (err) {
      set({ error: describeError(err) })
    }
  },

  fetchAnalytics: async () => {
    try {
      const analytics = await apiClient.getAnalytics()
      set({ analytics, error: null })
    } catch (err) {
      set({ error: describeError(err) })
    }
  },

  pollAll: async () => {
    set({ loading: true })
    try {
      const [modelsResponse, analytics, logsPage] = await Promise.all([
        apiClient.getModels(),
        apiClient.getAnalytics(),
        apiClient.getLogs({ limit: LOGS_PAGE_LIMIT, offset: 0 }),
      ])
      set({
        models: modelsResponse.providers,
        chaosModeEnabled: modelsResponse.chaos_mode_enabled,
        analytics,
        logs: logsPage.logs,
        logsTotal: logsPage.total,
        error: null,
      })
    } catch (err) {
      set({ error: describeError(err) })
    } finally {
      set({ loading: false })
    }
  },

  // --------------------------------------------------------------------------
  // "Try it Live" — fires a REAL routing request so viewers can watch the
  // FallbackChainGraph animate the actual attempt chain after a chaos inject.
  // --------------------------------------------------------------------------
  tryItPrompt: '',
  tryItResponse: null,
  tryItSending: false,

  setTryItPrompt: (prompt: string) => {
    set({ tryItPrompt: prompt })
  },

  sendTryItRequest: async () => {
    const { tryItPrompt } = useAppStore.getState()
    if (tryItPrompt.trim().length === 0) return
    set({ tryItSending: true, tryItResponse: null })
    try {
      const response = await apiClient.postRoute({ prompt: tryItPrompt.trim() })
      set({ tryItResponse: response, error: null })
    } catch (err) {
      set({ error: describeError(err) })
    } finally {
      set({ tryItSending: false })
    }
  },
}))

function describeError(err: unknown): string {
  if (axiosLikeError(err)) {
    return `Backend error (${err.response.status})`
  }
  return 'Cannot reach the backend'
}

function axiosLikeError(err: unknown): err is { response: { status: number } } {
  return typeof err === 'object' && err !== null && 'response' in err
}
