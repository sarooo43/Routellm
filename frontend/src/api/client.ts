import axios from 'axios'
import { API_BASE_URL } from '../config'
import type {
  Analytics,
  CatalogResponse,
  ChaosClearRequest,
  ChaosClearResponse,
  ChaosInjectRequest,
  ChaosInjectResponse,
  DeleteProviderKeyResponse,
  FeedbackRequest,
  FeedbackResponse,
  FeedbackSummary,
  GenerateKeyResponse,
  KeyUsageResponse,
  LogsQuery,
  LogsResponse,
  ModelsResponse,
  ProviderKeyStatus,
  RevokeKeyResponse,
  RouteRequest,
  RouteResponse,
} from '../types/api'
import { getActiveKey, markKeyRevoked, saveGeneratedKey } from './keyStorage'

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

export async function getCatalog(): Promise<CatalogResponse> {
  const { data } = await api.get<CatalogResponse>('/api/v1/catalog')
  return data
}

export async function generateApiKey(label = 'Unnamed Key'): Promise<GenerateKeyResponse> {
  const { data } = await api.post<GenerateKeyResponse>('/api/v1/keys/generate', { label })
  return data
}

export async function getKeyUsage(apiKey: string): Promise<KeyUsageResponse> {
  const { data } = await api.get<KeyUsageResponse>('/api/v1/keys/me', {
    headers: { 'X-API-Key': apiKey },
  })
  return data
}

export async function revokeKey(apiKey: string): Promise<RevokeKeyResponse> {
  const { data } = await api.delete<RevokeKeyResponse>('/api/v1/keys/me', {
    headers: { 'X-API-Key': apiKey },
  })
  return data
}

/** List BYOK provider-key status for one RouteLLM caller key (hints only). */
export async function getProviderKeys(apiKey: string): Promise<ProviderKeyStatus[]> {
  const { data } = await api.get<ProviderKeyStatus[]>('/api/v1/provider-keys', {
    headers: { 'X-API-Key': apiKey },
  })
  return data
}

/** Encrypt and store one upstream provider key for the calling RouteLLM key. */
export async function setProviderKey(
  apiKey: string,
  providerName: string,
  providerApiKey: string,
): Promise<ProviderKeyStatus> {
  const { data } = await api.put<ProviderKeyStatus>(
    `/api/v1/provider-keys/${encodeURIComponent(providerName)}`,
    { api_key: providerApiKey },
    { headers: { 'X-API-Key': apiKey } },
  )
  return data
}

/** Remove one caller-scoped upstream provider key. */
export async function deleteProviderKey(
  apiKey: string,
  providerName: string,
): Promise<DeleteProviderKeyResponse> {
  const { data } = await api.delete<DeleteProviderKeyResponse>(
    `/api/v1/provider-keys/${encodeURIComponent(providerName)}`,
    { headers: { 'X-API-Key': apiKey } },
  )
  return data
}

/** Use the selected local key, or create a demo key automatically on first use. */
export async function ensureActiveApiKey(): Promise<string> {
  const active = getActiveKey()
  if (active) return active.api_key
  const generated = await generateApiKey('Dashboard demo key')
  saveGeneratedKey(generated)
  return generated.api_key
}

export async function postRoute(payload: RouteRequest): Promise<RouteResponse> {
  const apiKey = await ensureActiveApiKey()
  try {
    const { data } = await api.post<RouteResponse>('/api/v1/route', payload, {
      headers: { 'X-API-Key': apiKey },
    })
    return data
  } catch (err) {
    // A key revoked on the server should not permanently break the demo panel.
    if (isUnauthorized(err)) {
      const active = getActiveKey()
      if (active) markKeyRevoked(active.key_prefix)
      const replacement = await generateApiKey('Dashboard demo key')
      saveGeneratedKey(replacement)
      const { data } = await api.post<RouteResponse>('/api/v1/route', payload, {
        headers: { 'X-API-Key': replacement.api_key },
      })
      return data
    }
    throw err
  }
}

function isUnauthorized(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'response' in err &&
    (err as { response?: { status?: number } }).response?.status === 401
  )
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

// Human thumbs rating on a routed response. The backend rejects duplicates
// with 409 (one rating per request_id) — callers should render already-rated
// state using TaskLog.feedback_given instead of clicking twice.
export async function postFeedback(payload: FeedbackRequest): Promise<FeedbackResponse> {
  const { data } = await api.post<FeedbackResponse>('/api/v1/feedback', payload)
  return data
}

export async function getFeedbackSummary(): Promise<FeedbackSummary> {
  const { data } = await api.get<FeedbackSummary>('/api/v1/feedback/summary')
  return data
}
