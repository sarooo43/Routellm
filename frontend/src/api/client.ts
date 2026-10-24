import axios from 'axios'
import type {
  Analytics,
  LogsQuery,
  LogsResponse,
  ProviderStatus,
  RouteRequest,
  RouteResponse,
} from '../types/api'

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15_000,
})

export async function getModels(): Promise<ProviderStatus[]> {
  const { data } = await api.get<ProviderStatus[]>('/api/v1/models')
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
