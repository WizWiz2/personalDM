export type UUID = string

export interface Campaign {
  id: UUID
  name: string
  description: string | null
  system_instructions: string | null
  narrative_style: string | null
  current_scene_id: UUID | null
  player_character_id: UUID | null
  created_at: string
  updated_at: string
}

export interface CampaignCreate {
  name: string
  description?: string | null
  narrative_style?: string | null
}

export interface ProviderConfig {
  id: UUID
  campaign_id: UUID
  base_url: string
  model_name: string
  has_api_key: boolean
  context_window: number
  created_at: string
}

export interface SessionZero {
  campaign_id: UUID
  status: string
  setting_name: string | null
  genre: string | null
  premise: string | null
  tone: string | null
  themes: string[]
  boundaries: string[]
  boundaries_confirmed: boolean
  rules_system: string | null
  world_summary: string | null
  starting_situation: string | null
  starting_location_id: UUID | null
  starting_location_name: string | null
  starting_scene_title: string | null
  play_style: string | null
  content_rating: string | null
  custom_fields: Record<string, unknown>
  player_character_id: UUID | null
  player_character_name: string | null
  current_scene_id: UUID | null
  character_card_missing_fields: string[]
  missing_fields: string[]
  ready_to_complete: boolean
  legacy_imported: boolean
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface Turn {
  id: UUID
  campaign_id: UUID
  scene_id: UUID | null
  acting_character_id: UUID | null
  role: 'user' | 'assistant' | 'system' | 'meta_user' | 'meta_assistant' | string
  content: string
  parent_turn_id: UUID | null
  status: string
  model_name: string | null
  token_count: number | null
  created_at: string
  channel: 'narrative' | 'meta'
}

export interface LocationExit {
  id: UUID
  campaign_id: UUID
  from_location_id: UUID
  to_location_id: UUID
  from_location_name: string
  to_location_name: string
  label: string
  direction: string | null
  travel_time: string | null
  access_rule: string | null
  discovered: boolean
  active: boolean
  created_at: string
  updated_at: string
}

export interface SceneState {
  campaign_id: UUID
  scene_id: UUID
  scene_status: string
  scene_title: string
  location_id: UUID | null
  location_path: string[]
  world_time_label: string | null
  world_time_order: number
  scene_goal: string | null
  active_conflict: string | null
  participant_ids: UUID[]
  participant_names: string[]
  object_ids: UUID[]
  object_names: string[]
  available_exits: LocationExit[]
  invariant_errors: string[]
}

export interface Scene {
  id: UUID
  campaign_id: UUID
  title: string
  location_id: UUID | null
  location_description: string | null
  mood: string | null
  tension: string | null
  status: string
  participants: UUID[]
  created_at: string
  updated_at: string
}

export interface Entity {
  id: UUID
  campaign_id: UUID
  entity_type: string
  canonical_name: string
  aliases: string[]
  description: string | null
  status: string
  provenance: string
  version: number
  custom_fields: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface Character extends Entity {
  appearance?: string | null
  face_description?: string | null
  body_description?: string | null
  immutable_features?: string | null
  personality?: string | null
  values?: string[]
  fears?: string[]
  desires?: string[]
  voice?: string | null
  speech_patterns?: string | null
  biography?: string | null
  backstory_public?: string | null
  emotional_state?: string | null
  current_location_id?: UUID | null
  current_intentions?: string[]
  visual_profile?: Record<string, unknown> | null
}

export interface Goal {
  id: UUID
  character_id: UUID
  description: string
  priority: number
  status: string
  is_secret: boolean
  source_turn_id: UUID | null
  valid_until: string | null
  created_at: string
  updated_at: string
}

export interface Belief {
  id: UUID
  character_id: UUID
  fact_id: UUID | null
  proposition: string
  status: string
  confidence: number
  source_turn_id: UUID | null
  source_character_id: UUID | null
  visibility: string
  is_current: boolean
  superseded_by: UUID | null
  created_at: string
  updated_at: string
}

export interface Relationship {
  id: UUID
  campaign_id: UUID
  subject_id: UUID
  object_id: UUID
  relation_type: string
  description: string
  reason: string | null
  intensity: number | null
  source_turn_id: UUID | null
  provenance: string
  confidence: number
  valid_from: string
  valid_until: string | null
  is_current: boolean
  visibility: string
  superseded_by: UUID | null
  created_at: string
}

export interface Equipment {
  id: UUID
  canonical_name: string
  description: string | null
  item_type: string | null
  physical_properties: string | null
  magical_properties: string | null
  value_estimate: string | null
  current_owner_id: UUID | null
  current_location_id: UUID | null
  is_unique: boolean
  lore: string | null
}

export interface CharacterCard {
  character: Character
  current_location: Entity | null
  goals: Goal[]
  beliefs: Belief[]
  relationships: Relationship[]
  equipment: Equipment[]
  capabilities: string[]
  limitations: string[]
  identity: Record<string, unknown>
  mechanics: Record<string, unknown>
  resources: Record<string, unknown>
  social: Record<string, unknown>
  visibility: Record<string, unknown>
  missing_fields: string[]
  completion_ratio: number
  ready_for_play: boolean
}

export interface Fact {
  id: UUID
  campaign_id: UUID
  subject: string
  predicate: string
  object_value: string | null
  truth_status: string
  source_turn_id: UUID | null
  confidence: number
  visibility: string
  scope: 'campaign' | 'scene'
  scene_id: UUID | null
  memory_kind: string
  subject_entity_id: UUID | null
  is_current: boolean
  superseded_by: UUID | null
  created_at: string
  updated_at: string
}
