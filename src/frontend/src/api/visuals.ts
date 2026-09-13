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
  visual_busy?: boolean
  narrative_running?: boolean
}

export interface VisualJobAccepted extends VisualAsset {
  accepted?: boolean
  generating?: boolean
  visual_busy?: boolean
  narrative_running?: boolean
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



async function waitForVisualJob(options?: {
  timeoutMs?: number
  intervalMs?: number
  isDone?: () => Promise<boolean>
}): Promise<void> {
  const timeoutMs = options?.timeoutMs ?? 240_000
  const intervalMs = options?.intervalMs ?? 1_200
  const started = Date.now()
  let sawBusy = false
  await new Promise((resolve) => window.setTimeout(resolve, 150))
  for (;;) {
    const status = await requestVisualStatus()
    if (status.visual_busy) sawBusy = true
    const done = options?.isDone ? await options.isDone() : false
    // Idle after we observed work, or done predicate true while nothing is running.
    if (!status.visual_busy && !status.narrative_running && (sawBusy || done)) {
      if (!options?.isDone || done) return
    }
    // Fast path: job finished before first poll saw busy.
    if (!status.visual_busy && !status.narrative_running && Date.now() - started > 2_500) {
      if (!options?.isDone || await options.isDone()) return
    }
    if (Date.now() - started > timeoutMs) {
      throw new Error('Генерация изображения не завершилась вовремя. Попробуй ещё раз.')
    }
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs))
  }
}

export function friendlyVisualError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? '')
  const lower = raw.toLocaleLowerCase('en-US')
  if (/comfyui is not reachable|connection refused|econnrefused/.test(lower)) {
    return 'Локальный image runtime (ComfyUI) недоступен. Открой Кампания → Модели и запусти/почини графику.'
  }
  if (/comfyui/.test(lower) && /timeout|timed out/.test(lower)) {
    return 'ComfyUI не ответил вовремя. Проверь, что он запущен, или повтори позже.'
  }
  if (/generation.*disabled|images? disabled|visuals? disabled/.test(lower)) {
    return 'Генерация изображений выключена в настройках моделей.'
  }
  return raw || 'Не удалось выполнить операцию с изображениями.'
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
  getCharacterPortrait: (characterId: UUID) =>
    requestVisual(`/api/characters/${characterId}/visuals/portrait`),
  getCampaignCover: (campaignId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/visuals/cover`),
  getSceneVisual: (campaignId: UUID, sceneId: UUID) =>
    requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals/latest`),
  generateCharacterPortrait: async (characterId: UUID) => {
    const accepted = await requestVisual(`/api/characters/${characterId}/visuals/portrait?force=true`, {
      method: 'POST',
    }) as VisualJobAccepted
    await waitForVisualJob()
    const result = await requestVisual(`/api/characters/${characterId}/visuals/portrait`)
    window.dispatchEvent(new CustomEvent('personaldm:visual-generated', {
      detail: { url: result.url, kind: result.kind },
    }))
    return { ...result, seed: accepted.seed }
  },
  generateCampaignCover: async (campaignId: UUID) => {
    const accepted = await requestVisual(`/api/campaigns/${campaignId}/visuals/cover?force=true`, {
      method: 'POST',
    }) as VisualJobAccepted
    await waitForVisualJob()
    const result = await requestVisual(`/api/campaigns/${campaignId}/visuals/cover`)
    window.dispatchEvent(new CustomEvent('personaldm:visual-generated', {
      detail: { url: result.url, kind: result.kind },
    }))
    return { ...result, seed: accepted.seed }
  },
  generateScene: async (campaignId: UUID, sceneId: UUID) => {
    const status = await requestVisualStatus()
    if (!status.enabled) {
      throw new Error(
        'Генерация изображений отключена. Её можно включить в настройках кампании → Модели.',
      )
    }
    if (!status.connected) {
      const label = status.provider === 'cloud' ? 'Облачный image provider' : 'Локальный image provider'
      throw new Error(
        `${label} недоступен (${status.base_url}). Открой настройки кампании → Модели, проверь provider или повтори установку/ремонт.`,
      )
    }
    await requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals?force=true`, {
      method: 'POST',
    })
    await waitForVisualJob({
      isDone: async () => {
        const latest = await requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals/latest`)
        return latest.available
      },
    })
    const result = await requestVisual(`/api/campaigns/${campaignId}/scenes/${sceneId}/visuals/latest`)
    if (!result.available) {
      throw new Error(
        'Сцена не появилась после генерации (ход мог забрать видеопамять). Дождись ответа мастера и нажми ещё раз.',
      )
    }
    window.dispatchEvent(new CustomEvent('personaldm:visual-generated', {
      detail: { url: result.url, kind: result.kind },
    }))
    return result
  },
}
