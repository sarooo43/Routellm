/**
 * Typed mirrors of the exact Phase 7 API contracts.
 * Any drift here will surface immediately at the TypeScript boundary.
 */

export type ProviderStatusValue = 'healthy' | 'degraded' | 'circuit_open'

export interface AttemptLog {
  provider_name: string
  success: boolean
  error: string | null
  latency_ms: number
}

export interface RouteRequest {
  prompt: string
  task_type?: string | null
  max_fallbacks?: number
}

export interface RouteResponse {
  success: boolean
  result: string
  model_used: string | null
  confidence_score: number
  fallback_chain_attempted: AttemptLog[]
  latency_ms: number
  tokens_used: number
  request_id: string
  error: string | null
}

export interface ProviderStatus {
  name: string
  status: ProviderStatusValue
  success_rate_overall: number
  requests_today: number
  daily_quota_limit: number
  quota_percentage_used: number
}

export interface FallbackChainEntry {
  provider_name: string
  success: boolean
  error: string | null
  latency_ms: number
}

export interface TaskLog {
  request_id: string
  task_type: string
  selected_model: string | null
  confidence_score: number
  status: 'success' | 'failed'
  latency_ms: number
  tokens_used: number
  error_message: string | null
  fallback_chain: FallbackChainEntry[]
  created_at: string
}

export interface LogsResponse {
  total: number
  logs: TaskLog[]
}

export interface RequestsPerDayPoint {
  date: string
  count: number
}

export interface Analytics {
  total_requests: number
  overall_success_rate: number
  avg_latency_ms: number
  total_cost_estimate: number
  model_usage_breakdown: Record<string, number>
  estimated_cost_if_single_model: number
  requests_per_day: RequestsPerDayPoint[]
}

export interface LogsQuery {
  limit?: number
  offset?: number
  task_type?: string | null
  status?: string | null
}
