import type { UUID } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export interface VisualAsset {
  kind: string
  available: boolean
  url: string
  file_path?: string
  prompt?: string
  seed?: number
  generated?: boolean
}

export interface VisualStatus {
  enabled: boolean
  connected: boolean
  provider: string
  base_url: string
  model: string
  text_encoder: string
  lora: string
}

async function requestVisual(path: string, init?: RequestInit): Promise<VisualAsset> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const body = await response.json() as { detail?: unknown }
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail) message = JSON.stringify(body.detail)
    } catch {
      // Keep the HTTP status fallback.
    }
    throw new Error(message)
  }
  const result = await response.json() as VisualAsset
  return { ...result, url: absoluteVisualUrl(result.url) }
}

async function requestVisualStatus(): Promise<VisualStatus> {
  const response = await fetch(`${API_BASE}/api/visuals/status`)
  if (!response.ok) throw new Error(`Visual backend status failed: HTTP ${response.status}`)
  return response.json() as Promise<VisualStatus>
}

export function absoluteVisualUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

export const visualUrls = {
  characterPortrait: (characterId: UUID) =>
    absoluteVisualUrl(`/generated/characters/${characterId}/portrait.png`),
  campaignCover: (campaignId: UUID) =>
    absoluteVisualUrl(`/generated/campaigns/${campaignId}/cover.png`),
  scene: (sceneId: UUID) =>
    absoluteVisualUrl(`/generated/scenes/${sceneId}/latest.png`),
}

export const visualApi = {
  status: requestVisualStatus,
  generateCharacterPortrait: (characterId: UUID) =>
    requestVisual(`/api/characters/${characterId}/visuals/portrait?force=true`, {
      method: 'POST',
    }),
  generateCampaignCover: (campaignId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/visuals/cover?force=true`, {
      method: 'POST',
    }),
  generateScene: (campaignId: UUID, sceneId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals?force=true`, {
      method: 'POST',
    }),
}
