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
  /**
   * Route to one named provider, skipping ranking. Also the only way to reach
   * the demo (MOCK) provider — automatic routing never selects it.
   */
  force_provider?: string | null
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
  /**
   * "unknown" means live telemetry (Postgres/Redis) was unavailable — the
   * card renders a neutral state instead of a misleading "healthy".
   */
  status: ProviderStatusValue | 'unknown'
  /** null when live telemetry is unavailable — not the same as zero. */
  success_rate_overall: number | null
  requests_today: number | null
  daily_quota_limit: number | null
  quota_percentage_used: number | null
  /** DEV/DEMO ONLY — true while a chaos injection is active on this provider. */
  chaos_active?: boolean
}

/**
 * GET /api/v1/models envelope — the top-level flag lets the frontend skip all
 * chaos UI on deployments where CHAOS_MODE_ENABLED is off.
 */
export interface ModelsResponse {
  chaos_mode_enabled: boolean
  /** False when the backend could not reach its database/Redis. */
  live_stats_available?: boolean
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
  /** Live telemetry — null when the backend's DB/Redis is unreachable
   *  (fail-soft catalog: static specs still render as "No live data"). */
  live_success_rate: number | null
  avg_latency_ms: number | null
  current_status: ProviderStatusValue | null
}

export interface CatalogResponse {
  /** False → all live_* fields above are null (backend couldn't observe). */
  live_stats_available: boolean
  models: CatalogEntry[]
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

/**
 * Real result of a live provider probe.
 *
 * `verdict` is the honest classification: only `auth_failed` may be described
 * as a key problem. `model_not_found` means the key works but the model does
 * not — the opposite of what the old "Configured" badge implied.
 */
export type ProviderVerdict =
  | 'valid'
  | 'auth_failed'
  | 'model_not_found'
  | 'rate_limited'
  | 'bad_request'
  | 'provider_error'
  | 'network_error'
  | 'not_configured'

export interface ProviderTestResult {
  provider: string
  display: string
  verdict: ProviderVerdict
  key_valid: boolean
  model: string | null
  latency_ms: number | null
  error: string | null
  available_models: string[]
  model_source: string | null
  /** Candidate-walk report (Auto mode). Optional so the PR #15 shape holds. */
  attempted?: ProviderConnectionTestStep[]
  skipped?: ProviderConnectionTestStep[]
}

export interface DeleteProviderKeyResponse {
  status: string
  provider_name: string
}

/** One provider row in the onboarding checklist (presence only, never a key). */
export interface SetupProvider {
  name: string
  display: string
  console_url: string
  free_note: string
  configured: boolean
  key_source: 'byok' | 'env' | null
  usable: boolean
}

/** One step of the first-run checklist. */
export interface SetupStep {
  id: 'provider_key' | 'test_prompt' | 'try_failover' | string
  title: string
  done: boolean
  detail: string
}

/** `GET /api/v1/setup/status` — what this installation still needs. */
export interface SetupStatus {
  local_mode: boolean
  require_api_key: boolean
  providers: SetupProvider[]
  any_provider_configured: boolean
  any_provider_usable: boolean
  demo_mode_enabled: boolean
  demo_available: boolean
  demo_provider_name: string
  chaos_mode_enabled: boolean
  data_dir: string
  database_driver: string | null
  preferred_provider: string | null
  preferred_console_url: string | null
  steps: SetupStep[]
}

// --------------------------------------------------------------------------
// Model explorer (per-caller, BYOK)
// --------------------------------------------------------------------------

export type ModelTier = 'free' | 'paid' | 'unknown'
export type ModelStatus = 'untested' | 'works' | 'denied' | 'rate_limited'
export type ModelSort = 'tier' | 'newest' | 'oldest' | 'name'

export interface ProviderModelInfo {
  id: string
  display_name: string
  released_or_version: string | null
  sort_key: string
  tier: ModelTier
  tier_source: string
  context_window: number | null
  max_output: number | null
  capabilities: string[]
  status: ModelStatus
  last_checked_at: string | null
  last_error: string | null
}

export interface ProviderModelsResponse {
  provider_name: string
  models: ProviderModelInfo[]
  hidden_non_chat_count: number
  pinned_model: string | null
  reason: string | null
}

export interface ProviderModelTestResponse {
  provider_name: string
  model_id: string
  status: ModelStatus | 'auth_failed' | 'error'
  latency_ms: number
  error: string | null
}

export interface ProviderConnectionTestStep {
  model: string
  skipped: boolean
  reason: string
  success: boolean
}

export interface ProviderConnectionTestResponse {
  provider_name: string
  success: boolean
  working_model: string | null
  attempted: ProviderConnectionTestStep[]
  skipped: ProviderConnectionTestStep[]
}
