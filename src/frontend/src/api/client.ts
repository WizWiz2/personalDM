import type {
  Campaign,
  CampaignCreate,
  Character,
  CharacterCard,
  Fact,
  ProviderConfig,
  Scene,
  SceneState,
  SessionZero,
  Turn,
  UUID,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export interface SessionZeroInterviewWorldDraft {
  setting_name: string | null
  genre: string | null
  premise: string | null
  tone: string | null
  themes: string[]
  boundaries: string[]
  boundaries_confirmed: boolean
  rules_system: string | null
  world_summary: string | null
  play_style: string | null
  narrative_style: string | null
  content_rating: string | null
  starting_location_name: string | null
  starting_situation: string | null
  starting_scene_title: string | null
}

export interface SessionZeroInterviewCharacterDraft {
  name: string | null
  description: string | null
  appearance: string | null
  personality: string | null
  values: string[]
  fears: string[]
  desires: string[]
  voice: string | null
  speech_patterns: string | null
  biography: string | null
  capabilities: string[]
  limitations: string[]
  first_goal: string | null
}

export interface SessionZeroInterviewState {
  version: number
  response_language: string
  messages: Array<{ role: 'user' | 'assistant'; content: string }>
  draft: {
    world: SessionZeroInterviewWorldDraft
    character: SessionZeroInterviewCharacterDraft
  }
  pending_user_message: string | null
  last_summary: string | null
  last_question_topics: string[]
  delegated_fields: string[]
}

export interface SessionZeroInterviewSnapshot {
  opening_message: string
  status: string
  summary: string
  state: SessionZeroInterviewState
}

export interface SessionZeroInterviewTurnResult {
  decision: {
    assistant_message: string
    ready_to_finalize: boolean
    draft: SessionZeroInterviewState['draft']
    missing_topics: string[]
    question_topics: string[]
    summary: string | null
  }
  completed: boolean
  scene_title: string | null
  summary: string
  state: SessionZeroInterviewState
}

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function parseError(response: Response) {
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  health: () => request<{ status: string; version: string; model: string }>('/health'),

  listCampaigns: () => request<Campaign[]>('/api/campaigns'),
  getCampaign: (id: UUID) => request<Campaign>(`/api/campaigns/${id}`),
  createCampaign: (data: CampaignCreate) =>
    request<Campaign>('/api/campaigns', { method: 'POST', body: JSON.stringify(data) }),
  deleteCampaign: (id: UUID) =>
    request<void>(`/api/campaigns/${id}`, { method: 'DELETE' }),
  updateCampaign: (id: UUID, data: Partial<Campaign>) =>
    request<Campaign>(`/api/campaigns/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  getSessionZero: (campaignId: UUID) =>
    request<SessionZero>(`/api/campaigns/${campaignId}/session-zero`),
  getSessionZeroInterview: (campaignId: UUID) =>
    request<SessionZeroInterviewSnapshot>(
      `/api/campaigns/${campaignId}/session-zero/interview`,
    ),
  answerSessionZeroInterview: (campaignId: UUID, message: string) =>
    request<SessionZeroInterviewTurnResult>(
      `/api/campaigns/${campaignId}/session-zero/interview/answer`,
      { method: 'POST', body: JSON.stringify({ message }) },
    ),
  retrySessionZeroInterview: (campaignId: UUID) =>
    request<SessionZeroInterviewTurnResult>(
      `/api/campaigns/${campaignId}/session-zero/interview/retry`,
      { method: 'POST' },
    ),

  listTurns: (campaignId: UUID, limit = 100, channel: 'all' | 'narrative' | 'meta' = 'all') =>
    request<Turn[]>(
      `/api/campaigns/${campaignId}/turns?limit=${limit}&active_only=true&channel=${channel}`,
    ),
  undoTurn: (campaignId: UUID) =>
    request<{ success: boolean }>(`/api/campaigns/${campaignId}/turns/undo`, { method: 'POST' }),
  stopGeneration: (campaignId: UUID) =>
    request<{ success: boolean }>(`/api/campaigns/${campaignId}/turns/stop`, { method: 'POST' }),

  async streamTurn(
    campaignId: UUID,
    content: string,
    onChunk: (chunk: string) => void,
    options?: { regenerateTurnId?: UUID; signal?: AbortSignal },
  ): Promise<'narrative' | 'meta'> {
    const path = options?.regenerateTurnId
      ? `/api/campaigns/${campaignId}/turns/${options.regenerateTurnId}/regenerate`
      : `/api/campaigns/${campaignId}/turns`
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: options?.regenerateTurnId ? undefined : JSON.stringify({ role: 'user', content }),
      signal: options?.signal,
    })
    if (!response.ok) await parseError(response)
    const channel = (response.headers.get('X-PersonalDM-Channel') ?? 'narrative') as
      | 'narrative'
      | 'meta'
    if (!response.body) return channel
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      onChunk(decoder.decode(value, { stream: true }))
    }
    return channel
  },

  listScenes: (campaignId: UUID) => request<Scene[]>(`/api/campaigns/${campaignId}/scenes`),
  getSceneState: (campaignId: UUID, sceneId: UUID) =>
    request<SceneState>(`/api/campaigns/${campaignId}/scenes/${sceneId}/state`),

  getCharacter: (characterId: UUID) =>
    request<Character>(`/api/characters/${characterId}`),
  getCharacterCard: (characterId: UUID) =>
    request<CharacterCard>(`/api/characters/${characterId}/card`),
  listPlayerFacts: (campaignId: UUID) =>
    request<Fact[]>(`/api/campaigns/${campaignId}/facts?visibility=player`),

  getProvider: (campaignId: UUID) =>
    request<ProviderConfig>(`/api/campaigns/${campaignId}/provider`),
  checkProvider: (campaignId: UUID) =>
    request<{ connected: boolean }>(`/api/campaigns/${campaignId}/provider/check`, {
      method: 'POST',
    }),
  saveProvider: (
    campaignId: UUID,
    data: { base_url: string; model_name: string; api_key?: string; context_window: number },
  ) =>
    request<ProviderConfig>(`/api/campaigns/${campaignId}/provider`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  exportCampaign: (campaignId: UUID) =>
    request<{ path: string; archive: unknown; campaign: Campaign }>(
      `/api/campaigns/${campaignId}/export`,
    ),
}

export function readableError(error: unknown): string {
  if (error instanceof ApiError) {
    const payload = error.detail as { detail?: unknown } | null
    const detail = payload?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object' && 'message' in detail) {
      return String((detail as { message: unknown }).message)
    }
    return error.message
  }
  if (error instanceof Error) return error.message
  return 'Неизвестная ошибка'
}
