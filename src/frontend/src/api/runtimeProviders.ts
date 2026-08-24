import type { UUID } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export interface RuntimeProviderStatus {
  ready: boolean
  code: string
  message: string
  installable: boolean
}

export interface TextRuntimeProvider {
  mode: 'local' | 'cloud'
  base_url: string
  model: string
  context_window: number
  has_api_key: boolean
  status: RuntimeProviderStatus
}

export interface ImageRuntimeProvider {
  mode: 'local' | 'cloud' | 'off'
  base_url: string
  model: string
  has_api_key: boolean
  status: RuntimeProviderStatus
}

export interface RuntimeProviderProfile {
  text: TextRuntimeProvider
  image: ImageRuntimeProvider
}

export interface InstallJob {
  id: string
  kind: 'text' | 'image'
  status: 'running' | 'completed' | 'failed'
  error?: string | null
  result?: RuntimeProviderStatus | null
  created_at: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const payload = await response.json() as { detail?: unknown }
      if (typeof payload.detail === 'string') message = payload.detail
    } catch { /* keep generic HTTP message */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export const runtimeProviderApi = {
  profile: () => request<RuntimeProviderProfile>('/api/runtime/providers'),
  check: () => request<{ text: RuntimeProviderStatus; image: RuntimeProviderStatus }>(
    '/api/runtime/providers/check',
    { method: 'POST' },
  ),
  configureText: (payload: {
    mode: 'local' | 'cloud'
    base_url?: string
    model?: string
    api_key?: string
    context_window?: number
    campaign_id?: UUID
  }) => request<TextRuntimeProvider>('/api/runtime/providers/text', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  configureImage: (payload: {
    mode: 'local' | 'cloud' | 'off'
    base_url?: string
    model?: string
    api_key?: string
  }) => request<ImageRuntimeProvider>('/api/runtime/providers/image', {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  install: (kind: 'text' | 'image') => request<InstallJob>(
    `/api/runtime/providers/${kind}/install`,
    { method: 'POST' },
  ),
  installJob: (jobId: string) => request<InstallJob>(`/api/runtime/providers/install/${jobId}`),
}
