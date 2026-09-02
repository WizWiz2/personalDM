import type { Entity, UUID } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export interface Location extends Entity {
  geography: string | null
  atmosphere: string | null
  access_rules: string | null
  parent_location_id: UUID | null
  climate: string | null
  notable_features: string | null
  danger_level: string | null
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json() as Promise<T>
}

export const locationApi = {
  list: (campaignId: UUID) => request<Location[]>(`/api/campaigns/${campaignId}/locations`),
  get: (locationId: UUID) => request<Location>(`/api/locations/${locationId}`),
}
