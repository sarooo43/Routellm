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
  /** DEV/DEMO ONLY — chaos-injected fallback step (renders purple on graph). */
  chaos_injected?: boolean
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
  /** DEV/DEMO ONLY — true while a chaos injection is active on this provider. */
  chaos_active?: boolean
}

/**
 * GET /api/v1/models envelope — the top-level flag lets the frontend skip all
 * chaos UI on deployments where CHAOS_MODE_ENABLED is off.
 */
export interface ModelsResponse {
  chaos_mode_enabled: boolean
  providers: ProviderStatus[]
}

// DEV/DEMO ONLY — chaos admin endpoints (exist only when backend flag is on).
export interface ChaosInjectRequest {
  provider_name: string
  duration_seconds?: number
}

export interface ChaosInjectResponse {
  status: string
  provider_name: string
  expires_in_seconds: number
}

export interface ChaosClearRequest {
  provider_name: string
}

export interface ChaosClearResponse {
  status: string
  provider_name: string
}

export interface ChaosStatusResponse {
  active_chaos: string[]
}

export interface FallbackChainEntry {
  provider_name: string
  success: boolean
  error: string | null
  latency_ms: number
  chaos_injected?: boolean
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
  /** True when a thumbs rating already exists — shows the already-rated UI on load. */
  feedback_given?: boolean
  /** EXPERIMENTAL: "ml_hybrid" | "rule_based" — which capability source ranked providers. */
  routing_method?: string
}

// --------------------------------------------------------------------------
// Human feedback (thumbs quality signal)
// --------------------------------------------------------------------------

export type FeedbackRating = 'positive' | 'negative'

export interface FeedbackRequest {
  request_id: string
  rating: FeedbackRating
}

export interface FeedbackResponse {
  status: string
  request_id: string
}

export interface FeedbackCounts {
  positive: number
  negative: number
  positive_rate: number
}

export interface FeedbackSummary {
  total_rated: number
  positive_rate: number
  by_provider: Record<string, FeedbackCounts>
  by_task_type: Record<string, FeedbackCounts>
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

// --------------------------------------------------------------------------
// Models Catalog (Phase 15)
// --------------------------------------------------------------------------

export type Capability = 'code' | 'reasoning' | 'summarization' | 'qa' | 'general'
export type PricingTier = 'free' | 'cheap' | 'premium'

export interface CatalogEntry {
  provider_name: string
  display_name: string
  description: string
  context_window: number
  capabilities: Capability[]
  pricing_tier: PricingTier
  cost_per_1k_tokens: number
  live_success_rate: number
  avg_latency_ms: number
  current_status: ProviderStatusValue
}

// --------------------------------------------------------------------------
// Lightweight API keys (Phase 16)
// --------------------------------------------------------------------------

export interface GenerateKeyResponse {
  api_key: string
  key_prefix: string
  label: string
  warning: string
}

export interface KeyUsageResponse {
  key_prefix: string
  label: string
  created_at: string
  total_requests: number
  total_tokens_used: number
  last_used_at: string | null
}

export interface RevokeKeyResponse {
  status: string
  key_prefix: string
}

// --------------------------------------------------------------------------
// Per-caller provider keys / BYOK (Phase 18)
// --------------------------------------------------------------------------

export interface ProviderKeyStatus {
  provider_name: string
  configured: boolean
  key_hint: string | null
  created_at: string | null
  updated_at: string | null
}

export interface DeleteProviderKeyResponse {
  status: string
  provider_name: string
}
