import { useEffect, useMemo, useState } from 'react'
import { api, readableError } from '../api/client'
import type { Character, CharacterCard, Fact, SceneState } from '../api/types'
import { visualUrls } from '../api/visuals'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { GeneratedPixelArt } from '../components/GeneratedPixelArt'
import { PixelPortrait, PixelScene } from '../components/PixelArt'
import { ErrorState, LoadingState } from '../components/States'

type Tab = 'characters' | 'locations' | 'knowledge'
type WorldItem = {
  id: string
  name: string
  subtitle: string
  description?: string | null
  appearance?: string | null
}

function characterRole(character: Character | undefined): string | null {
  const role = character?.custom_fields?.role
  return typeof role === 'string' && role.trim() ? role.trim() : null
}

function CharacterPortrait({ id, name }: { id: string; name: string }) {
  return <GeneratedPixelArt
    src={visualUrls.characterPortrait(id)}
    alt={`Портрет ${name}`}
    fallback={<PixelPortrait seed={name} />}
  />
}

export function WorldPage() {
  const { campaign } = useCampaignWorkspace()
  const [tab, setTab] = useState<Tab>('characters')
  const [card, setCard] = useState<CharacterCard | null>(null)
  const [scene, setScene] = useState<SceneState | null>(null)
  const [characters, setCharacters] = useState<Record<string, Character>>({})
  const [facts, setFacts] = useState<Fact[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const run = async () => {
      try {
        const [heroCard, playerFacts, sceneState] = await Promise.all([
          campaign.player_character_id ? api.getCharacterCard(campaign.player_character_id) : Promise.resolve(null),
          api.listPlayerFacts(campaign.id).catch(() => []),
          campaign.current_scene_id ? api.getSceneState(campaign.id, campaign.current_scene_id) : Promise.resolve(null),
        ])
        if (!active) return

        const npcIds = (sceneState?.participant_ids || []).filter(
          (id) => id !== campaign.player_character_id,
        )
        const npcCharacters = await Promise.all(
          npcIds.map((id) => api.getCharacter(id).catch(() => null)),
        )
        if (!active) return

        setCard(heroCard)
        setFacts(playerFacts)
        setScene(sceneState)
        setCharacters(Object.fromEntries(
          npcCharacters
            .filter((character): character is Character => character !== null)
            .map((character) => [character.id, character]),
        ))
      } catch (err) { if (active) setError(readableError(err)) }
      finally { if (active) setLoading(false) }
    }
    void run()
    return () => { active = false }
  }, [campaign.id, campaign.player_character_id, campaign.current_scene_id])

  const characterItems = useMemo<WorldItem[]>(() => {
    if (!scene) return []
    return scene.participant_ids.flatMap((id, index) => {
      if (id === campaign.player_character_id) return []
      const name = scene.participant_names[index]
      if (!name) return []
      const character = characters[id]
      const relationship = card?.relationships.find((rel) =>
        (rel.visibility === 'player' || rel.subject_id === campaign.player_character_id) &&
        (rel.subject_id === id || rel.object_id === id),
      )
      return [{
        id,
        name,
        subtitle: relationship?.relation_type || characterRole(character) || 'Участник текущей сцены',
        description:
          character?.description ||
          relationship?.description ||
          character?.appearance ||
          'Сейчас находится в одной сцене с героем.',
        appearance: character?.appearance,
      }]
    })
  }, [scene, characters, card, campaign.player_character_id])

  const locationItems = useMemo<WorldItem[]>(() => {
    const result: WorldItem[] = []
    if (scene?.location_id) result.push({
      id: scene.location_id,
      name: scene.location_path.at(-1) || 'Текущая локация',
      subtitle: scene.location_path.slice(0, -1).join(' → ') || 'Текущая локация',
    })
    scene?.available_exits.filter((x) => x.discovered && x.active).forEach((exit) => result.push({
      id: exit.to_location_id,
      name: exit.to_location_name,
      subtitle: exit.travel_time || exit.direction || 'Доступный путь',
      description: exit.label,
    }))
    return [...new Map(result.map((x) => [x.id, x] as const)).values()]
  }, [scene])

  const knowledgeItems = useMemo<WorldItem[]>(() => facts.filter((f) => f.is_current).map((f) => ({
    id: f.id,
    name: `${f.subject} — ${f.predicate}`,
    subtitle: f.object_value || f.truth_status,
    description: f.object_value,
  })), [facts])

  const source = tab === 'characters' ? characterItems : tab === 'locations' ? locationItems : knowledgeItems
  const filtered = source.filter((item) => `${item.name} ${item.subtitle}`.toLowerCase().includes(query.toLowerCase()))
  const selectedItem = filtered.find((x) => x.id === selected) || filtered[0]

  useEffect(() => { setSelected(null) }, [tab, query])

  return <div className="workspace-page">
    <header className="workspace-topbar world-topbar"><div><h1>Мир</h1><p>То, что известно герою</p></div><input className="search-input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Поиск…" /></header>
    <div className="page-content world-content">
      <div className="tabs"><button className={tab === 'characters' ? 'active' : ''} onClick={() => setTab('characters')}>Персонажи</button><button className={tab === 'locations' ? 'active' : ''} onClick={() => setTab('locations')}>Места</button><button className={tab === 'knowledge' ? 'active' : ''} onClick={() => setTab('knowledge')}>Знания</button></div>
      {loading && <LoadingState label="Собираем известный мир…" />}
      {error && <ErrorState message={error} />}
      {!loading && !error && <div className="world-layout">
        <div className="world-index">
          {filtered.length === 0 && <p className="muted-note">Пока здесь ничего нет.</p>}
          {filtered.map((item) => <button key={item.id} className={`world-index-item ${selectedItem?.id === item.id ? 'active' : ''}`} onClick={() => setSelected(item.id)}>{tab === 'characters' ? <span className="tiny-portrait"><CharacterPortrait id={item.id} name={item.name} /></span> : <span className="tiny-scene"><PixelScene seed={item.name} compact /></span>}<span><strong>{item.name}</strong><small>{item.subtitle}</small></span></button>)}
        </div>
        <article className="world-detail">
          {selectedItem ? <>
            {tab === 'characters' ? <div className="world-portrait"><CharacterPortrait id={selectedItem.id} name={selectedItem.name} /></div> : tab === 'locations' ? <div className="world-scene"><PixelScene seed={selectedItem.name} /></div> : null}
            <span className="eyebrow">{tab === 'characters' ? 'Известный персонаж' : tab === 'locations' ? 'Известное место' : 'Знание'}</span>
            <h2>{selectedItem.name}</h2>
            <p>{selectedItem.description || selectedItem.subtitle}</p>
            {tab === 'characters' && selectedItem.appearance && selectedItem.appearance !== selectedItem.description && <section className="dossier-section"><h3>Внешность</h3><p>{selectedItem.appearance}</p></section>}
            {tab === 'locations' && scene?.location_id === selectedItem.id && <section className="dossier-section"><h3>Сейчас здесь</h3><p>{scene.participant_names.join(', ') || 'Никого не указано'}</p></section>}
            {tab === 'knowledge' && <section className="dossier-section"><h3>Статус</h3><p>Это знание доступно герою.</p></section>}
          </> : <div className="muted-note">Выбери запись слева.</div>}
        </article>
      </div>}
      <p className="world-safety-note">Здесь показываются только сведения, которые уже доступны герою. Скрытая часть канона остаётся у мастера.</p>
    </div>
  </div>
}
