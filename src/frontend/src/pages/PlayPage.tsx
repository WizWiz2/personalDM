import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import { submitDetachedTurn } from '../api/turnRuntime'
import type { SceneState, Turn } from '../api/types'
import { friendlyVisualError, visualApi, visualUrls } from '../api/visuals'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { GeneratedPixelArt } from '../components/GeneratedPixelArt'
import { GenerationFailurePanel } from '../components/GenerationFailurePanel'
import { Icons } from '../components/Icons'
import { PixelScene } from '../components/PixelArt'
import { ErrorState, LoadingState } from '../components/States'

type Mode = 'play' | 'dm'

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
  return value === 'dm' ? 'dm' : 'play'
}

function uniquePath(path: string[] | undefined | null): string[] {
  if (!path?.length) return []
  const out: string[] = []
  for (const part of path) {
    const trimmed = part.trim()
    if (!trimmed) continue
    if (out.length && out[out.length - 1].toLocaleLowerCase('ru-RU') === trimmed.toLocaleLowerCase('ru-RU')) continue
    out.push(trimmed)
  }
  return out
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
  const [playerName, setPlayerName] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<Mode>(() => readStoredMode(modeKey))
  const [input, setInput] = useState(() => window.sessionStorage.getItem(draftKey) ?? '')
  const [drawer, setDrawer] = useState(false)
  const [sceneGenerating, setSceneGenerating] = useState(false)
  const [sceneArtNonce, setSceneArtNonce] = useState(0)
  const [sceneArtAvailable, setSceneArtAvailable] = useState(false)
  const [stickToBottom, setStickToBottom] = useState(true)
  const [showJumpLatest, setShowJumpLatest] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLFormElement>(null)
  const [jumpBottom, setJumpBottom] = useState(96)
  const previousGeneration = useRef<{ id: string; status: string } | null>(null)
  const stickToBottomRef = useRef(true)

  const busy = generation?.status === 'running'
  const failedGeneration = generation
    && (generation.status === 'failed' || generation.status === 'cancelled')
    ? generation
    : null

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
      const [history, characterCard] = await Promise.all([
        api.listTurns(campaign.id, 150, 'all'),
        freshCampaign.player_character_id
          ? api.getCharacterCard(freshCampaign.player_character_id).catch(() => null)
          : Promise.resolve(null),
      ])
      setTurns(history)
      setPlayerName(characterCard?.character.canonical_name ?? '')
      if (freshCampaign.current_scene_id) {
        const nextScene = await api.getSceneState(campaign.id, freshCampaign.current_scene_id)
        setScene(nextScene)
        const visual = await visualApi.getSceneVisual(campaign.id, freshCampaign.current_scene_id).catch(() => null)
        setSceneArtAvailable(Boolean(visual?.available))
      } else {
        setScene(null)
        setSceneArtAvailable(false)
      }
    } catch (err) {
      setError(readableError(err))
    } finally {
      if (showLoader) setLoading(false)
    }
  }

  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    stickToBottomRef.current = true
    setStickToBottom(true)
    setShowJumpLatest(false)
    bottomRef.current?.scrollIntoView({ behavior, block: 'end' })
  }, [])

  const updateScrollAffinity = useCallback(() => {
    const marker = bottomRef.current
    if (!marker) return
    const rect = marker.getBoundingClientRect()
    const viewport = window.innerHeight || document.documentElement.clientHeight
    const distanceFromBottom = rect.top - viewport
    const nearBottom = distanceFromBottom < 140
    stickToBottomRef.current = nearBottom
    setStickToBottom(nearBottom)
    const jumpedUp = distanceFromBottom > Math.max(viewport * 0.9, 360)
    setShowJumpLatest(jumpedUp)
  }, [])

  useEffect(() => { void load(true) }, [campaign.id])

  useEffect(() => {
    const node = composerRef.current
    if (!node) return
    const update = () => {
      const height = Math.ceil(node.getBoundingClientRect().height)
      setJumpBottom(Math.max(height + 16, 72))
    }
    update()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null
    ro?.observe(node)
    window.addEventListener('resize', update)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [loading, busy, failedGeneration, mode, error])

  useEffect(() => {
    const onScroll = () => updateScrollAffinity()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    onScroll()
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [updateScrollAffinity, loading, turns.length, generation?.status])

  useEffect(() => {
    if (!stickToBottomRef.current) return
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns.length, generation?.status, acceptedTurn?.id])

  useEffect(() => {
    if (input) window.sessionStorage.setItem(draftKey, input)
    else window.sessionStorage.removeItem(draftKey)
  }, [draftKey, input])

  useEffect(() => {
    window.sessionStorage.setItem(modeKey, mode)
  }, [modeKey, mode])

  useEffect(() => {
    if (!drawer) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawer(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawer])

  useEffect(() => {
    if (
      acceptedTurn
      && generation?.user_turn_id === acceptedTurn.id
      && generation.status === 'completed'
    ) {
      setAcceptedTurn(null)
      window.sessionStorage.removeItem(acceptedKey)
    }
  }, [acceptedKey, acceptedTurn, generation?.status, generation?.user_turn_id])

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
        stickToBottomRef.current = true
        setStickToBottom(true)
      }
      void load(false)
    }
  }, [generation?.id, generation?.status])

  const visibleTurns = useMemo(() => turns.filter((turn) => turn.role !== 'system'), [turns])
  const latestMasterTurnId = useMemo(
    () => [...visibleTurns].reverse().find((turn) => turn.role === 'assistant')?.id,
    [visibleTurns],
  )
  const timelineTurns = useMemo(() => {
    if (!acceptedTurn || visibleTurns.some((turn) => turn.id === acceptedTurn.id)) return visibleTurns
    return [...visibleTurns, acceptedTurn]
  }, [acceptedTurn, visibleTurns])

  const locationParts = useMemo(() => uniquePath(scene?.location_path), [scene?.location_path])
  const locationLeaf = locationParts.at(-1) || null
  const locationAncestors = locationParts.slice(0, -1)
  const sceneTitle = scene?.scene_title?.trim() || 'Сцена'
  const titleLooksLikeLocation = Boolean(
    locationLeaf
    && sceneTitle.toLocaleLowerCase('ru-RU') === locationLeaf.toLocaleLowerCase('ru-RU'),
  )
  const topbarSubtitle = titleLooksLikeLocation
    ? (locationAncestors.join(' · ') || 'Начало приключения')
    : (locationParts.join(' · ') || sceneTitle)

  const send = async (event?: FormEvent) => {
    event?.preventDefault()
    const text = input.trim()
    if (!text) return
    if (failedGeneration) {
      setError('Сначала повтори или убери неудачный ход — иначе следующий ход встанет в очередь поверх ошибки.')
      return
    }
    if (busy) {
      setError('Мастер ещё обрабатывает предыдущий ход. Черновик сохранён — можно спокойно открыть другие разделы и вернуться позже.')
      return
    }

    const content = mode === 'dm' ? `/DM ${text}` : text
    setError('')
    stickToBottomRef.current = true
    setStickToBottom(true)
    setShowJumpLatest(false)
    try {
      const accepted = await submitDetachedTurn(campaign.id, content)
      setAcceptedTurn(accepted.user_turn)
      window.sessionStorage.setItem(acceptedKey, JSON.stringify(accepted.user_turn))
      setTurns((items) => items.some((turn) => turn.id === accepted.user_turn.id)
        ? items
        : [...items, accepted.user_turn])
      trackGeneration(accepted.generation)
      setInput('')
      window.setTimeout(() => scrollToLatest('smooth'), 40)
    } catch (err) {
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
      setAcceptedTurn(null)
      window.sessionStorage.removeItem(acceptedKey)
      await load(false)
      await refreshGeneration()
    } catch (err) { setError(readableError(err)) }
  }

  const retryFailedTurn = async () => {
    if (busy || !failedGeneration) return
    const failedTurn = timelineTurns.find((turn) => turn.id === failedGeneration.user_turn_id)
      || acceptedTurn
    const raw = failedTurn?.content?.trim()
    if (!raw) {
      setError('Не удалось найти текст неудачного хода для повтора.')
      return
    }
    setError('')
    stickToBottomRef.current = true
    setStickToBottom(true)
    try {
      await api.undoTurn(campaign.id)
      setAcceptedTurn(null)
      window.sessionStorage.removeItem(acceptedKey)
      const accepted = await submitDetachedTurn(campaign.id, raw)
      setAcceptedTurn(accepted.user_turn)
      window.sessionStorage.setItem(acceptedKey, JSON.stringify(accepted.user_turn))
      trackGeneration(accepted.generation)
      await load(false)
      await refreshGeneration()
    } catch (err) {
      setError(readableError(err))
      await load(false)
      await refreshGeneration()
    }
  }

  const generateScene = async () => {
    if (!scene || sceneGenerating || busy) return
    setSceneGenerating(true)
    setError('')
    try {
      const result = await visualApi.generateScene(campaign.id, scene.scene_id)
      setSceneArtAvailable(true)
      setSceneArtNonce(result.seed || Date.now())
    } catch (err) {
      setError(friendlyVisualError(err))
    } finally {
      setSceneGenerating(false)
    }
  }

  if (loading) return <div className="workspace-page"><LoadingState label="Восстанавливаем сцену…" /></div>

  const fallbackScene = <PixelScene seed={`${campaign.name}:${scene?.scene_title ?? ''}`} />
  const sceneArtSrc = scene
    ? `${visualUrls.scene(scene.scene_id)}${sceneArtNonce ? `?v=${sceneArtNonce}` : ''}`
    : ''

  return (
    <div className="workspace-page play-page">
      <header className="workspace-topbar">
        <div><h1>{campaign.name}</h1><p>{topbarSubtitle}</p></div>
        <div className="topbar-actions">
          <button className="btn primary context-toggle" type="button" aria-expanded={drawer} aria-controls="play-scene-context" onClick={() => setDrawer(true)}>Сейчас</button>
          <button className="btn primary scene-generate" type="button" disabled={!scene || sceneGenerating || busy} onClick={() => void generateScene()} title={busy ? 'Дождись окончания хода: текстовая и графическая модели делят видеопамять' : sceneGenerating ? 'Рисуем в фоне. Можно отправлять ход — графика подождёт и продолжит после ответа мастера.' : 'Собрать пиксель-арт сцену по последним ходам и портретам присутствующих персонажей'} aria-label={sceneGenerating ? 'Рисуем сцену' : 'Сгенерировать сцену'}><Icons.spark /><span>{sceneGenerating ? 'Рисуем…' : 'Сгенерировать сцену'}</span></button>
        </div>
      </header>

      <div className="play-layout">
        <section className="play-column">
          <div className="scene-art">
            {scene && sceneArtSrc
              ? <GeneratedPixelArt src={sceneArtSrc} alt={`Сцена: ${scene.scene_title}`} fallback={fallbackScene} active={sceneArtAvailable} retryOnError={sceneArtAvailable} />
              : fallbackScene}
            <div className="scene-overlay">
              <h2>{sceneTitle}</h2>
              <span>{[scene?.world_time_label, titleLooksLikeLocation ? null : locationLeaf].filter(Boolean).join(' · ')}</span>
            </div>
          </div>

          {failedGeneration && (
            <div className="generation-failure-stack" role="alert">
              <GenerationFailurePanel generation={failedGeneration} />
              <div className="session-zero-error-actions">
                <button className="btn primary" type="button" disabled={busy} onClick={() => void retryFailedTurn()}>
                  Повторить ход
                </button>
                <button className="btn" type="button" disabled={busy} onClick={() => void undo()}>
                  Убрать неудачный ход
                </button>
              </div>
            </div>
          )}
          {error && <ErrorState message={error} />}

          <div className="timeline" aria-live="polite">
            {timelineTurns.length === 0 && <div className="story-empty">История начнётся с твоего первого действия.</div>}
            {timelineTurns.map((turn) => {
              const meta = turn.role.startsWith('meta_')
              const player = turn.role === 'user' || turn.role === 'meta_user'
              const failed = failedGeneration?.user_turn_id === turn.id
              const label = meta
                ? (player ? 'Вопрос мастеру' : 'Мастер вне игры')
                : (player ? (playerName || 'Персонаж') : 'Мастер')
              const showMasterUndo = !meta
                && turn.id === latestMasterTurnId
                && !busy
                && !failedGeneration
              return <article key={turn.id} className={`turn ${player ? 'player' : 'dm'} ${meta ? 'meta' : ''} ${failed ? 'failed' : ''}`}>
                <div className="turn-label">{label}{failed ? ' · не обработано' : ''}</div>
                <div>{meta && player ? turn.content.replace(/^\s*\/(DM|OOC)\s*/i, '') : turn.content}</div>
                {showMasterUndo && <button type="button" className="btn primary turn-undo" onClick={() => void undo()}><Icons.undo />Откатить последний ход мастера</button>}
              </article>
            })}
            {busy && <article className={`turn dm thinking-turn ${generation?.user_turn_id === acceptedTurn?.id && acceptedTurn?.role === 'meta_user' ? 'meta' : ''}`}>
              <div className="turn-label">Мастер</div>
              <div className="turn-thinking"><span /><span /><span /><em>обрабатывает ход…</em></div>
            </article>}
            <div ref={bottomRef} />
          </div>

          {showJumpLatest && (
            <button type="button" className="jump-latest-btn" style={{ bottom: jumpBottom }} onClick={() => scrollToLatest('smooth')}>
              К последнему ходу
            </button>
          )}

          <form className="composer" ref={composerRef} onSubmit={send}>
            <div className="mode-row">
              <span className="composer-hint">Опиши ход персонажа свободно — действие и речь можно сочетать.</span>
              <button type="button" className={`btn primary composer-mode ${mode === 'dm' ? 'active' : ''}`} onClick={() => setMode(mode === 'dm' ? 'play' : 'dm')}><Icons.shield />{mode === 'dm' ? 'Вернуться в игру' : 'Обращение к мастеру'}</button>
            </div>
            <div className="compose-row"><textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder={failedGeneration ? 'Сначала повтори или убери неудачный ход…' : busy ? 'Можно набросать следующий ход — черновик сохранится…' : mode === 'dm' ? 'Спроси мастера вне игры…' : 'Что делает и говорит персонаж?'} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} /><button className="btn primary send-btn" disabled={!input.trim() || busy || Boolean(failedGeneration)} aria-label="Отправить"><Icons.send /></button></div>
            <div className="composer-footer">
              {busy
                ? <><span className="turn-runtime-note">Ход сохранён. Можно открыть Героя, Мир или Хронику — мастер продолжит работу.</span><button type="button" className="quiet-action danger" onClick={() => void stop()}><Icons.stop />Остановить</button></>
                : <span className="turn-runtime-note">{stickToBottom ? 'Черновик ввода сохраняется при переходах между разделами.' : 'Лента отвязана от низа — новые ответы не утянут скролл.'}</span>}
            </div>
          </form>
        </section>

        <aside id="play-scene-context" className={`scene-context ${drawer ? 'open' : ''}`}>
          <button className="context-close" onClick={() => setDrawer(false)} aria-label="Закрыть">×</button>
          <h3>Сейчас</h3>
          <div>
            <span className="context-label">Локация</span>
            <strong>{locationLeaf || 'Не указана'}</strong>
            {locationAncestors.length ? <small>{locationAncestors.join(' → ')}</small> : null}
          </div>
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

