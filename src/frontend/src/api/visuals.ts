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

export interface GalleryAsset {
  id: UUID
  kind: string
  url: string
  prompt?: string | null
  seed?: number | null
  scene_id?: UUID | null
  created_at: string
  metadata: Record<string, unknown>
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

async function requestGallery(campaignId: UUID): Promise<GalleryAsset[]> {
  const response = await fetch(`${API_BASE}/api/campaigns/${campaignId}/visuals/gallery`)
  if (!response.ok) throw new Error(`Gallery request failed: HTTP ${response.status}`)
  const result = await response.json() as GalleryAsset[]
  return result.map((asset) => ({ ...asset, url: absoluteVisualUrl(asset.url) }))
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
  gallery: requestGallery,
  getCampaignCover: (campaignId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/visuals/cover`),
  generateCharacterPortrait: (characterId: UUID) =>
    requestVisual(`/api/characters/${characterId}/visuals/portrait?force=true`, {
      method: 'POST',
    }),
  generateCampaignCover: (campaignId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/visuals/cover?force=true`, {
      method: 'POST',
    }),
  generateScene: async (campaignId: UUID, sceneId: UUID) => {
    const status = await requestVisualStatus()
    if (!status.enabled) {
      throw new Error(
        'Локальная генерация изображений выключена в запущенном backend. Перезапусти PersonalDM через play.bat.',
      )
    }
    if (!status.connected) {
      throw new Error(
        `ComfyUI не отвечает на ${status.base_url}. Перезапусти PersonalDM через play.bat и проверь image setup в окне запуска.`,
      )
    }
    return requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals?force=true`, {
      method: 'POST',
    })
  },
}
