import axios from 'axios'
import type {
  Analytics,
  ChaosClearRequest,
  ChaosClearResponse,
  ChaosInjectRequest,
  ChaosInjectResponse,
  LogsQuery,
  LogsResponse,
  ModelsResponse,
  RouteRequest,
  RouteResponse,
} from '../types/api'

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15_000,
})

export async function getModels(): Promise<ModelsResponse> {
  const { data } = await api.get<ModelsResponse>('/api/v1/models')
  return data
 }

export async function getLogs(params: LogsQuery = {}): Promise<LogsResponse> {
  const { data } = await api.get<LogsResponse>('/api/v1/logs', { params })
  return data
}

export async function getAnalytics(): Promise<Analytics> {
  const { data } = await api.get<Analytics>('/api/v1/analytics')
  return data
}

export async function postRoute(payload: RouteRequest): Promise<RouteResponse> {
  const { data } = await api.post<RouteResponse>('/api/v1/route', payload)
  return data
}

// DEV/DEMO ONLY — chaos admin calls. The backend serves these 404-other
// deployments, so callers must tolerate 404 when CHAOS_MODE_ENABLED is off.
export async function injectChaos(payload: ChaosInjectRequest): Promise<ChaosInjectResponse> {
  const { data } = await api.post<ChaosInjectResponse>('/api/v1/admin/chaos/inject', payload)
  return data
}

export async function clearChaos(payload: ChaosClearRequest): Promise<ChaosClearResponse> {
  const { data } = await api.post<ChaosClearResponse>('/api/v1/admin/chaos/clear', payload)
  return data
}
