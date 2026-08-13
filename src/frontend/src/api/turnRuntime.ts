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
  created_at: string
  updated_at: string
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
