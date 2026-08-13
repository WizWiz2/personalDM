import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import { submitDetachedTurn } from '../api/turnRuntime'
import type { SceneState, Turn } from '../api/types'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { Icons } from '../components/Icons'
import { PixelScene } from '../components/PixelArt'
import { ErrorState, LoadingState } from '../components/States'

type Mode = 'action' | 'talk' | 'dm'

function readStoredTurn(key: string): Turn | null {
  try {
    const raw = window.sessionStorage.getItem(key)
    return raw ? JSON.parse(raw) as Turn : null
  } catch {
    return null
  }
}

function readStoredMode(key: string): Mode {
  const value = window.sessionStorage.getItem(key)
  return value === 'talk' || value === 'dm' ? value : 'action'
}

export function PlayPage() {
  const {
    campaign,
    refreshCampaign,
    generation,
    refreshGeneration,
    trackGeneration,
  } = useCampaignWorkspace()
  const navigate = useNavigate()
  const draftKey = `personaldm:${campaign.id}:play-draft`
  const modeKey = `personaldm:${campaign.id}:play-mode`
  const acceptedKey = `personaldm:${campaign.id}:accepted-turn`

  const [turns, setTurns] = useState<Turn[]>([])
  const [acceptedTurn, setAcceptedTurn] = useState<Turn | null>(() => readStoredTurn(acceptedKey))
  const [scene, setScene] = useState<SceneState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<Mode>(() => readStoredMode(modeKey))
  const [input, setInput] = useState(() => window.sessionStorage.getItem(draftKey) ?? '')
  const [drawer, setDrawer] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const previousGeneration = useRef<{ id: string; status: string } | null>(null)

  const busy = generation?.status === 'running'

  const load = async (showLoader = false) => {
    if (showLoader) setLoading(true)
    setError('')
    try {
      const setup = await api.getSessionZero(campaign.id)
      if (setup.status !== 'completed') {
        navigate(`/campaigns/${campaign.id}/session-zero`, { replace: true })
        return
      }
      const freshCampaign = await refreshCampaign()
      const history = await api.listTurns(campaign.id, 150, 'all')
      setTurns(history)
      if (freshCampaign.current_scene_id) {
        setScene(await api.getSceneState(campaign.id, freshCampaign.current_scene_id))
      } else {
        setScene(null)
      }
    } catch (err) {
      setError(readableError(err))
    } finally {
      if (showLoader) setLoading(false)
    }
  }

  useEffect(() => { void load(true) }, [campaign.id])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, generation?.status])

  useEffect(() => {
    if (input) window.sessionStorage.setItem(draftKey, input)
    else window.sessionStorage.removeItem(draftKey)
  }, [draftKey, input])

  useEffect(() => {
    window.sessionStorage.setItem(modeKey, mode)
  }, [modeKey, mode])

  useEffect(() => {
    const current = generation ? { id: generation.id, status: generation.status } : null
    const previous = previousGeneration.current
    previousGeneration.current = current

    if (
      previous
      && current
      && previous.id === current.id
      && previous.status === 'running'
      && current.status !== 'running'
    ) {
      if (current.status === 'completed') {
        setAcceptedTurn(null)
        window.sessionStorage.removeItem(acceptedKey)
      } else if (current.status === 'failed' || current.status === 'cancelled') {
        setError(
          generation?.error
            || (current.status === 'cancelled'
              ? 'Генерация остановлена. Твой отправленный ход сохранён как не обработанный.'
              : 'Мастер не смог обработать сохранённый ход.'),
        )
      }
      void load(false)
    }
  }, [generation?.id, generation?.status])

  const visibleTurns = useMemo(() => turns.filter((turn) => turn.role !== 'system'), [turns])
  const timelineTurns = useMemo(() => {
    if (!acceptedTurn || visibleTurns.some((turn) => turn.id === acceptedTurn.id)) return visibleTurns
    return [...visibleTurns, acceptedTurn]
  }, [acceptedTurn, visibleTurns])

  const send = async (event?: FormEvent) => {
    event?.preventDefault()
    const text = input.trim()
    if (!text) return
    if (busy) {
      setError('Мастер ещё обрабатывает предыдущий ход. Черновик сохранён — можно спокойно открыть другие разделы и вернуться позже.')
      return
    }

    const content = mode === 'dm' ? `/DM ${text}` : text
    setError('')
    try {
      const accepted = await submitDetachedTurn(campaign.id, content)
      setAcceptedTurn(accepted.user_turn)
      window.sessionStorage.setItem(acceptedKey, JSON.stringify(accepted.user_turn))
      setTurns((items) => items.some((turn) => turn.id === accepted.user_turn.id)
        ? items
        : [...items, accepted.user_turn])
      trackGeneration(accepted.generation)
      setInput('')
    } catch (err) {
      // Input is intentionally left untouched until the backend has durably accepted it.
      setError(readableError(err))
      await refreshGeneration().catch(() => undefined)
    }
  }

  const stop = async () => {
    try {
      await api.stopGeneration(campaign.id)
      window.setTimeout(() => { void refreshGeneration() }, 150)
    } catch (err) {
      setError(readableError(err))
    }
  }

  const undo = async () => {
    if (busy) return
    try {
      await api.undoTurn(campaign.id)
      await load(false)
    } catch (err) { setError(readableError(err)) }
  }

  if (loading) return <div className="workspace-page"><LoadingState label="Восстанавливаем сцену…" /></div>

  const failedAccepted = Boolean(
    acceptedTurn
    && generation?.user_turn_id === acceptedTurn.id
    && (generation.status === 'failed' || generation.status === 'cancelled'),
  )

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
            {timelineTurns.length === 0 && <div className="story-empty">История начнётся с твоего первого действия.</div>}
            {timelineTurns.map((turn) => {
              const meta = turn.role.startsWith('meta_')
              const player = turn.role === 'user' || turn.role === 'meta_user'
              const failed = failedAccepted && turn.id === acceptedTurn?.id
              const label = meta
                ? (player ? 'Вопрос мастеру' : 'Мастер вне игры')
                : (player ? 'Ты' : 'Мастер')
              return <article key={turn.id} className={`turn ${player ? 'player' : 'dm'} ${meta ? 'meta' : ''} ${failed ? 'failed' : ''}`}>
                <div className="turn-label">{label}{failed ? ' · не обработано' : ''}</div>
                <div>{meta && player ? turn.content.replace(/^\s*\/(DM|OOC)\s*/i, '') : turn.content}</div>
              </article>
            })}
            {busy && <article className={`turn dm thinking-turn ${generation?.user_turn_id === acceptedTurn?.id && acceptedTurn?.role === 'meta_user' ? 'meta' : ''}`}>
              <div className="turn-label">Мастер</div>
              <div className="turn-thinking"><span /><span /><span /><em>обрабатывает ход…</em></div>
            </article>}
            <div ref={bottomRef} />
          </div>

          <form className="composer" onSubmit={send}>
            <div className="mode-row">
              <button type="button" className={mode === 'action' ? 'active' : ''} onClick={() => setMode('action')}>Действие</button>
              <button type="button" className={mode === 'talk' ? 'active' : ''} onClick={() => setMode('talk')}><Icons.chat />Реплика</button>
              <button type="button" className={mode === 'dm' ? 'active' : ''} onClick={() => setMode('dm')}><Icons.shield />Мастер</button>
            </div>
            <div className="compose-row"><textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder={busy ? 'Можно набросать следующий ход — черновик сохранится…' : mode === 'dm' ? 'Спроси мастера вне игры…' : mode === 'talk' ? 'Что ты говоришь?' : 'Что ты делаешь?'} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} /><button className="btn primary send-btn" disabled={!input.trim() || busy} aria-label="Отправить"><Icons.send /></button></div>
            <div className="composer-footer">
              <button type="button" className="quiet-action" onClick={() => void undo()} disabled={busy}><Icons.undo />Отменить</button>
              {busy
                ? <><span className="turn-runtime-note">Ход сохранён. Можно открыть Героя, Мир или Хронику — мастер продолжит работу.</span><button type="button" className="quiet-action danger" onClick={() => void stop()}><Icons.stop />Остановить</button></>
                : <span className="turn-runtime-note">Черновик ввода сохраняется при переходах между разделами.</span>}
            </div>
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
