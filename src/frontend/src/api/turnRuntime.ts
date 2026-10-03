import { ApiError } from './client'
import type { Turn, UUID } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export interface GenerationRun {
  id: UUID
  campaign_id: UUID
  user_turn_id: UUID
  assistant_turn_id: UUID | null
  status: 'running' | 'completed' | 'failed' | 'cancelled' | string
  cancel_requested: boolean
  error: string | null
  phase?: string | null
  created_at: string
  updated_at: string
}

export interface UsageBucket {
  calls: number
  input_tokens: number
  cached_input_tokens: number
  cache_write_tokens: number
  output_tokens: number
  reasoning_tokens: number
  total_tokens: number
  estimated_cost_usd: number | null
  cost_complete: boolean
}

export interface TurnUsageSummary extends UsageBucket {
  campaign_id: UUID
  user_turn_id: UUID
  event_count: number
  generation_status: string | null
  post_turn_status: 'processing' | 'partial' | 'completed' | 'none'
  complete: boolean
  by_role: Array<UsageBucket & { role: string }>
  by_model: Array<UsageBucket & { model: string }>
}

export interface TraceViolation {
  code: string
  severity: 'warning' | 'error' | string
  evidence: string
  correction: string
  span: { start: number; end: number; excerpt: string } | null
}

export type GenerationTraceEntry =
  | {
    kind: 'call'
    at: string
    role: string | null
    model: string
    status: string
    duration_ms: number | null
    total_tokens: number
  }
  | {
    kind: 'decision'
    at: string
    step: 'authority' | 'validate' | 'evaluate' | 'repair' | 'publish' | 'final' | string
    role: string | null
    outcome: string
    payload: { reason?: string; violations?: TraceViolation[]; [key: string]: unknown }
  }

export interface GenerationTrace {
  generation_run_id: UUID
  user_turn_id: UUID
  status: string
  phase: string | null
  error: string | null
  timeline: GenerationTraceEntry[]
}

export interface AcceptedTurn {
  accepted: true
  channel: 'narrative' | 'meta'
  user_turn: Turn
  generation: GenerationRun
}

async function parseError(response: Response): Promise<never> {
  let detail: unknown = null
  try {
    detail = await response.json()
  } catch {
    detail = await response.text().catch(() => null)
  }
  const message =
    typeof detail === 'object' && detail && 'detail' in detail
      ? JSON.stringify((detail as { detail: unknown }).detail)
      : `HTTP ${response.status}`
  throw new ApiError(message, response.status, detail)
}

export async function submitDetachedTurn(
  campaignId: UUID,
  content: string,
): Promise<AcceptedTurn> {
  const response = await fetch(`${API_BASE}/api/campaigns/${campaignId}/turns/async`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role: 'user', content }),
  })
  if (!response.ok) await parseError(response)
  return await response.json() as AcceptedTurn
}

export async function getLatestGeneration(
  campaignId: UUID,
): Promise<GenerationRun | null> {
  const response = await fetch(
    `${API_BASE}/api/campaigns/${campaignId}/turns/generation/latest`,
    { headers: { 'Content-Type': 'application/json' } },
  )
  if (!response.ok) await parseError(response)
  return await response.json() as GenerationRun | null
}


export async function getTurnUsage(
  campaignId: UUID,
  userTurnId: UUID,
): Promise<TurnUsageSummary> {
  const response = await fetch(
    `${API_BASE}/api/campaigns/${campaignId}/turns/usage/${userTurnId}`,
    { headers: { 'Content-Type': 'application/json' } },
  )
  if (!response.ok) await parseError(response)
  return await response.json() as TurnUsageSummary
}

export async function getGenerationTrace(
  campaignId: UUID,
  runId: UUID,
): Promise<GenerationTrace> {
  const response = await fetch(
    `${API_BASE}/api/campaigns/${campaignId}/turns/generation/${runId}/trace`,
    { headers: { 'Content-Type': 'application/json' } },
  )
  if (!response.ok) await parseError(response)
  return await response.json() as GenerationTrace
}
