import { create } from 'zustand'
import * as apiClient from '../api/client'
import type { Analytics, LogsQuery, ProviderStatus, TaskLog } from '../types/api'

const LOGS_PAGE_LIMIT = 50

interface AppState {
  models: ProviderStatus[]
  logs: TaskLog[]
  logsTotal: number
  analytics: Analytics | null
  loading: boolean
  error: string | null

  fetchModels: () => Promise<void>
  fetchLogs: (query?: LogsQuery) => Promise<void>
  fetchAnalytics: () => Promise<void>
  /** Poll everything the dashboard needs; safe to call on an interval. */
  pollAll: () => Promise<void>
}

export const useAppStore = create<AppState>((set) => ({
  models: [],
  logs: [],
  logsTotal: 0,
  analytics: null,
  loading: false,
  error: null,

  fetchModels: async () => {
    try {
      const models = await apiClient.getModels()
      set({ models, error: null })
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
      const [models, analytics, logsPage] = await Promise.all([
        apiClient.getModels(),
        apiClient.getAnalytics(),
        apiClient.getLogs({ limit: LOGS_PAGE_LIMIT, offset: 0 }),
      ])
      set({ models, analytics, logs: logsPage.logs, logsTotal: logsPage.total, error: null })
    } catch (err) {
      set({ error: describeError(err) })
    } finally {
      set({ loading: false })
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
