import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import type { SceneState, Turn } from '../api/types'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { Icons } from '../components/Icons'
import { PixelScene } from '../components/PixelArt'
import { ErrorState, LoadingState } from '../components/States'

type Mode = 'action' | 'talk' | 'dm'

export function PlayPage() {
  const { campaign, refreshCampaign } = useCampaignWorkspace()
  const navigate = useNavigate()
  const [turns, setTurns] = useState<Turn[]>([])
  const [scene, setScene] = useState<SceneState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<Mode>('action')
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [drawer, setDrawer] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const load = async () => {
    setError('')
    try {
      const setup = await api.getSessionZero(campaign.id)
      if (setup.status !== 'completed') {
        navigate(`/campaigns/${campaign.id}/session-zero`, { replace: true })
        return
      }
      const history = await api.listTurns(campaign.id, 150, 'all')
      setTurns(history)
      if (campaign.current_scene_id) {
        setScene(await api.getSceneState(campaign.id, campaign.current_scene_id))
      } else {
        setScene(null)
      }
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [campaign.id, campaign.current_scene_id])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, streamText])

  const visibleTurns = useMemo(() => turns.filter((turn) => turn.role !== 'system'), [turns])

  const send = async (event?: FormEvent) => {
    event?.preventDefault()
    const text = input.trim()
    if (!text || streaming) return
    const content = mode === 'dm' ? `/DM ${text}` : text
    setStreaming(true)
    setStreamText('')
    setInput('')
    setError('')
    abortRef.current = new AbortController()
    const optimistic: Turn = {
      id: `optimistic-${Date.now()}`,
      campaign_id: campaign.id,
      scene_id: campaign.current_scene_id,
      acting_character_id: campaign.player_character_id,
      role: mode === 'dm' ? 'meta_user' : 'user',
      content,
      parent_turn_id: null,
      status: 'active', model_name: null, token_count: null,
      created_at: new Date().toISOString(),
      channel: mode === 'dm' ? 'meta' : 'narrative',
    }
    setTurns((items) => [...items, optimistic])
    try {
      await api.streamTurn(campaign.id, content, (chunk) => setStreamText((v) => v + chunk), { signal: abortRef.current.signal })
      const freshCampaign = await refreshCampaign()
      const history = await api.listTurns(campaign.id, 150, 'all')
      setTurns(history)
      if (freshCampaign.current_scene_id) {
        setScene(await api.getSceneState(campaign.id, freshCampaign.current_scene_id))
      } else {
        setScene(null)
      }
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) setError(readableError(err))
      await load()
    } finally {
      setStreaming(false)
      setStreamText('')
      abortRef.current = null
    }
  }

  const stop = async () => {
    abortRef.current?.abort()
    try { await api.stopGeneration(campaign.id) } catch { /* best effort */ }
  }

  const undo = async () => {
    if (streaming) return
    try {
      await api.undoTurn(campaign.id)
      await load()
    } catch (err) { setError(readableError(err)) }
  }

  if (loading) return <div className="workspace-page"><LoadingState label="Восстанавливаем сцену…" /></div>

  return (
    <div className="workspace-page play-page">
      <header className="workspace-topbar">
        <div><h1>{campaign.name}</h1><p>{scene?.location_path.join(' · ') || scene?.scene_title || 'Текущая сцена'}</p></div>
        <div className="topbar-actions">
          <button className="btn context-toggle" onClick={() => setDrawer(true)}>Сейчас</button>
          <button className="btn primary scene-generate" disabled title="Генератор изображений будет подключён отдельным провайдером"><Icons.spark /><span>Сгенерировать сцену</span></button>
        </div>
      </header>

      <div className="play-layout">
        <section className="play-column">
          <div className="scene-art"><PixelScene seed={`${campaign.name}:${scene?.scene_title ?? ''}`} /><div className="scene-overlay"><h2>{scene?.scene_title || 'Сцена'}</h2><span>{[scene?.world_time_label, scene?.location_path.at(-1)].filter(Boolean).join(' · ')}</span></div></div>

          {error && <ErrorState message={error} />}

          <div className="timeline" aria-live="polite">
            {visibleTurns.length === 0 && <div className="story-empty">История начнётся с твоего первого действия.</div>}
            {visibleTurns.map((turn) => {
              const meta = turn.role.startsWith('meta_')
              const player = turn.role === 'user' || turn.role === 'meta_user'
              return <article key={turn.id} className={`turn ${player ? 'player' : 'dm'} ${meta ? 'meta' : ''}`}><div className="turn-label">{meta ? (player ? 'Вопрос мастеру' : 'Мастер вне игры') : (player ? 'Ты' : 'Мастер')}</div><div>{meta && player ? turn.content.replace(/^\s*\/(DM|OOC)\s*/i, '') : turn.content}</div></article>
            })}
            {streaming && streamText && <article className={`turn dm ${mode === 'dm' ? 'meta' : ''}`}><div className="turn-label">{mode === 'dm' ? 'Мастер вне игры' : 'Мастер'}</div><div>{streamText}</div></article>}
            <div ref={bottomRef} />
          </div>

          <form className="composer" onSubmit={send}>
            <div className="mode-row">
              <button type="button" className={mode === 'action' ? 'active' : ''} onClick={() => setMode('action')}>Действие</button>
              <button type="button" className={mode === 'talk' ? 'active' : ''} onClick={() => setMode('talk')}><Icons.chat />Реплика</button>
              <button type="button" className={mode === 'dm' ? 'active' : ''} onClick={() => setMode('dm')}><Icons.shield />Мастер</button>
            </div>
            <div className="compose-row"><textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder={mode === 'dm' ? 'Спроси мастера вне игры…' : mode === 'talk' ? 'Что ты говоришь?' : 'Что ты делаешь?'} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} /><button className="btn primary send-btn" disabled={!input.trim() || streaming} aria-label="Отправить"><Icons.send /></button></div>
            <div className="composer-footer"><button type="button" className="quiet-action" onClick={() => void undo()} disabled={streaming}><Icons.undo />Отменить</button>{streaming && <button type="button" className="quiet-action danger" onClick={() => void stop()}><Icons.stop />Остановить</button>}</div>
          </form>
        </section>

        <aside className={`scene-context ${drawer ? 'open' : ''}`}>
          <button className="context-close" onClick={() => setDrawer(false)} aria-label="Закрыть">×</button>
          <h3>Сейчас</h3>
          <div><span className="context-label">Локация</span><strong>{scene?.location_path.at(-1) || 'Не указана'}</strong>{scene?.location_path.length ? <small>{scene.location_path.slice(0, -1).join(' → ')}</small> : null}</div>
          <div className="context-block"><span className="context-label">Кто здесь</span>{scene?.participant_names.length ? scene.participant_names.map((name) => <div className="participant" key={name}><span className="participant-dot" />{name}</div>) : <small>Никто не указан</small>}</div>
          {scene?.scene_goal && <div className="context-block"><span className="context-label">Цель сцены</span><strong>{scene.scene_goal}</strong></div>}
          {scene?.active_conflict && <div className="context-block"><span className="context-label">Напряжение</span><span>{scene.active_conflict}</span></div>}
          {scene?.available_exits.length ? <div className="context-block"><span className="context-label">Куда можно идти</span>{scene.available_exits.filter((x) => x.discovered && x.active).map((exit) => <div className="exit-row" key={exit.id}><span>{exit.label}</span><small>{exit.to_location_name}</small></div>)}</div> : null}
        </aside>
        {drawer && <button className="context-scrim" onClick={() => setDrawer(false)} aria-label="Закрыть контекст" />}
      </div>
    </div>
  )
}
