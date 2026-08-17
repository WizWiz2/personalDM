import { useEffect, useMemo, useState } from 'react'
import { api, readableError } from '../api/client'
import type { CharacterCard, SceneState } from '../api/types'
import { visualApi, visualUrls } from '../api/visuals'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { GeneratedPixelArt } from '../components/GeneratedPixelArt'
import { PixelPortrait } from '../components/PixelArt'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

export function HeroPage() {
  const { campaign } = useCampaignWorkspace()
  const [card, setCard] = useState<CharacterCard | null>(null)
  const [scene, setScene] = useState<SceneState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [portraitGenerating, setPortraitGenerating] = useState(false)
  const [portraitNonce, setPortraitNonce] = useState(0)

  useEffect(() => {
    let active = true
    const run = async () => {
      if (!campaign.player_character_id) { setLoading(false); return }
      try {
        const [data, sceneState] = await Promise.all([
          api.getCharacterCard(campaign.player_character_id),
          campaign.current_scene_id
            ? api.getSceneState(campaign.id, campaign.current_scene_id)
            : Promise.resolve(null),
        ])
        if (!active) return
        setCard(data)
        setScene(sceneState)
      } catch (err) { if (active) setError(readableError(err)) }
      finally { if (active) setLoading(false) }
    }
    void run()
    return () => { active = false }
  }, [campaign.id, campaign.player_character_id, campaign.current_scene_id])

  const character = card?.character
  const identity = useMemo(() => Object.entries(card?.identity ?? {}).filter(([, v]) => v !== null && v !== ''), [card])
  const participantNames = useMemo(() => {
    const map = new Map<string, string>()
    scene?.participant_ids.forEach((id, index) => {
      const name = scene.participant_names[index]
      if (name) map.set(id, name)
    })
    return map
  }, [scene])

  const visibleRelationships = useMemo(() => {
    if (!card || !character) return []
    return card.relationships.filter((rel) => rel.visibility === 'player' || rel.subject_id === character.id)
  }, [card, character])

  const regeneratePortrait = async () => {
    if (!character || portraitGenerating) return
    setPortraitGenerating(true)
    setError('')
    try {
      const result = await visualApi.generateCharacterPortrait(character.id)
      setPortraitNonce(result.seed || Date.now())
    } catch (err) {
      setError(readableError(err))
    } finally {
      setPortraitGenerating(false)
    }
  }

  return <div className="workspace-page">
    <header className="workspace-topbar"><div><h1>Герой</h1><p>Досье персонажа</p></div></header>
    <div className="page-content hero-content">
      {loading && <LoadingState label="Собираем досье…" />}
      {error && <ErrorState message={error} />}
      {!loading && !error && !campaign.player_character_id && <EmptyState title="Герой ещё не создан" text="Сначала заверши нулевую сессию." />}
      {card && character && <div className="hero-dossier">
        <aside className="hero-summary">
          <div className="portrait-frame">
            <GeneratedPixelArt
              src={`${visualUrls.characterPortrait(character.id)}${portraitNonce ? `?v=${portraitNonce}` : ''}`}
              alt={`Портрет ${character.canonical_name}`}
              fallback={<PixelPortrait seed={character.canonical_name} />}
            />
          </div>
          <button className="btn" type="button" disabled={portraitGenerating} onClick={() => void regeneratePortrait()}>{portraitGenerating ? 'Рисуем…' : 'Перерисовать портрет'}</button>
          <span className="eyebrow">Игрок</span>
          <h2>{character.canonical_name}</h2>
          <p>{character.description || 'Описание пока не задано.'}</p>
          <div className="chips">{character.values?.map((v) => <span className="chip" key={v}>{v}</span>)}</div>
          {card.current_location && <div className="hero-location"><span>Сейчас</span><strong>{card.current_location.canonical_name}</strong></div>}
        </aside>

        <article className="hero-details">
          {identity.length > 0 && <section className="dossier-section"><h3>Кто это</h3><div className="identity-grid">{identity.map(([k, v]) => <div key={k}><span>{k}</span><strong>{String(v)}</strong></div>)}</div></section>}
          {character.appearance && <section className="dossier-section"><h3>Образ</h3><p>{character.appearance}</p></section>}
          {character.personality && <section className="dossier-section"><h3>Характер</h3><p>{character.personality}</p></section>}
          {character.backstory_public && <section className="dossier-section"><h3>Прошлое</h3><p>{character.backstory_public}</p></section>}
          {(character.emotional_state || character.current_intentions?.length) && <section className="dossier-section"><h3>Текущее состояние</h3>{character.emotional_state && <p>{character.emotional_state}</p>}<div className="chips">{character.current_intentions?.map((v) => <span className="chip" key={v}>{v}</span>)}</div></section>}
          {card.goals.length > 0 && <section className="dossier-section"><h3>Цели</h3><div className="knowledge-list">{[...card.goals].sort((a, b) => b.priority - a.priority).map((goal, i) => <div className="knowledge-item" key={goal.id}><span className="bullet">{i === 0 ? '●' : '○'}</span><span>{goal.description}</span></div>)}</div></section>}
          {visibleRelationships.length > 0 && <section className="dossier-section"><h3>Важные связи</h3><div className="relationship-list">{visibleRelationships.map((rel) => {
            const otherId = rel.subject_id === character.id ? rel.object_id : rel.subject_id
            return <div key={rel.id}><strong>{participantNames.get(otherId) || 'Связанный персонаж'}</strong><span>{rel.relation_type} · {rel.description}</span></div>
          })}</div></section>}
          {card.equipment.length > 0 && <section className="dossier-section"><h3>При себе</h3><div className="chips">{card.equipment.map((item) => <span className="chip" key={item.id}>{item.canonical_name}</span>)}</div></section>}
          {card.capabilities.length > 0 && <section className="dossier-section"><h3>Умеет</h3><div className="chips">{card.capabilities.map((v) => <span className="chip" key={v}>{v}</span>)}</div></section>}
          {card.beliefs.length > 0 && <section className="dossier-section"><h3>Убеждения</h3><div className="knowledge-list">{card.beliefs.filter((belief) => belief.is_current).map((belief) => <div className="knowledge-item" key={belief.id}><span className="bullet">•</span><span>{belief.proposition}</span></div>)}</div></section>}
        </article>
      </div>}
    </div>
  </div>
}
