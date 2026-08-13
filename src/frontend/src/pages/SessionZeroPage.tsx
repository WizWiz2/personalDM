import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  api,
  readableError,
  type SessionZeroInterviewSnapshot,
} from '../api/client'
import type { Campaign, SessionZero } from '../api/types'
import { Icons } from '../components/Icons'
import { ErrorState, LoadingState } from '../components/States'

export function SessionZeroPage() {
  const { campaignId = '' } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [setup, setSetup] = useState<SessionZero | null>(null)
  const [interview, setInterview] = useState<SessionZeroInterviewSnapshot | null>(null)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const transcriptEnd = useRef<HTMLDivElement | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [c, s, i] = await Promise.all([
        api.getCampaign(campaignId),
        api.getSessionZero(campaignId),
        api.getSessionZeroInterview(campaignId),
      ])
      setCampaign(c)
      setSetup(s)
      setInterview(i)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [campaignId])

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [interview?.state.messages.length, interview?.state.pending_user_message, sending])

  const messages = useMemo(() => {
    if (!interview) return []
    if (interview.state.messages.length) return interview.state.messages
    return [{ role: 'assistant' as const, content: interview.opening_message }]
  }, [interview])

  const refreshAfterTurn = async () => {
    const [s, i] = await Promise.all([
      api.getSessionZero(campaignId),
      api.getSessionZeroInterview(campaignId),
    ])
    setSetup(s)
    setInterview(i)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const message = input.trim()
    if (!message || sending || setup?.status === 'completed') return
    setSending(true)
    setError('')
    setInput('')
    try {
      const result = await api.answerSessionZeroInterview(campaignId, message)
      setInterview((current) => current ? {
        ...current,
        status: result.completed ? 'completed' : current.status,
        state: result.state,
      } : current)
      if (result.completed) {
        setSetup(await api.getSessionZero(campaignId))
      }
    } catch (err) {
      setError(readableError(err))
      await refreshAfterTurn().catch(() => undefined)
    } finally {
      setSending(false)
    }
  }

  const retry = async () => {
    if (sending) return
    setSending(true)
    setError('')
    try {
      const result = await api.retrySessionZeroInterview(campaignId)
      setInterview((current) => current ? {
        ...current,
        status: result.completed ? 'completed' : current.status,
        state: result.state,
      } : current)
      if (result.completed) setSetup(await api.getSessionZero(campaignId))
    } catch (err) {
      setError(readableError(err))
      await refreshAfterTurn().catch(() => undefined)
    } finally {
      setSending(false)
    }
  }

  const draft = interview?.state.draft
  const world = draft?.world
  const hero = draft?.character
  const completed = setup?.status === 'completed' || interview?.status === 'completed'
  const pending = Boolean(interview?.state.pending_user_message)

  return <div className="global-page session-zero-page">
    <header className="global-topbar session-zero-topbar">
      <div>
        <h1>Нулевая сессия</h1>
        <p>{campaign?.name || 'Новая кампания'} · разговор с мастером</p>
      </div>
      <div className="session-zero-top-actions">
        {!completed && <span className="session-zero-save-state">Сохраняется автоматически</span>}
        <button className="btn" onClick={() => navigate('/campaigns')}>
          <Icons.back />К кампаниям
        </button>
      </div>
    </header>

    <main className="global-content session-zero-conversation-shell">
      {loading && <LoadingState label="Подготавливаем нулевую сессию…" />}

      {!loading && interview && <div className="session-zero-conversation-layout">
        <section className="session-zero-conversation" aria-label="Разговор нулевой сессии">
          <div className="session-zero-intro">
            <span className="eyebrow">До первой сцены</span>
            <h2>Соберём игру разговором</h2>
            <p>Можно начать с мира, героя, жанра или просто с ощущения. Мастер сам подхватит важные детали и не будет прогонять тебя по анкете.</p>
          </div>

          <div className="session-zero-transcript" aria-live="polite">
            {messages.map((message, index) => <article
              className={`session-zero-message ${message.role === 'user' ? 'player' : 'dm'}`}
              key={`${message.role}-${index}-${message.content.slice(0, 24)}`}
            >
              <div className="session-zero-speaker">{message.role === 'user' ? 'Ты' : 'Мастер'}</div>
              <div className="session-zero-message-text">{message.content}</div>
            </article>)}

            {sending && <article className="session-zero-message dm thinking">
              <div className="session-zero-speaker">Мастер</div>
              <div className="session-zero-thinking"><span /><span /><span /></div>
            </article>}
            <div ref={transcriptEnd} />
          </div>

          {error && <div className="session-zero-inline-error">
            <strong>Ответ мастера не получен.</strong>
            <span>{error}</span>
            {pending && <button className="btn" disabled={sending} onClick={() => void retry()}><Icons.refresh />Повторить</button>}
          </div>}

          {completed ? <div className="session-zero-ready">
            <div>
              <span className="eyebrow">Готово</span>
              <strong>Нулевая сессия завершена</strong>
              <p>{setup?.starting_scene_title || 'Первая сцена подготовлена.'}</p>
            </div>
            <button className="btn primary" onClick={() => navigate(`/campaign/${campaignId}/play`)}>
              Начать приключение <Icons.chevron />
            </button>
          </div> : pending && !sending ? <div className="session-zero-pending">
            <span>Последний ответ сохранён, но модель не успела его обработать.</span>
            <button className="btn primary" onClick={() => void retry()}><Icons.refresh />Продолжить</button>
          </div> : <form className="session-zero-composer" onSubmit={submit}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Расскажи, во что хочется сыграть…"
              rows={3}
              disabled={sending}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
            />
            <div className="session-zero-composer-footer">
              <span>Enter — отправить · Shift+Enter — новая строка</span>
              <button className="send-btn" type="submit" disabled={sending || !input.trim()} aria-label="Отправить">
                <Icons.send />
              </button>
            </div>
          </form>}
        </section>

        <aside className="session-zero-preview" aria-label="Черновик кампании">
          <div className="session-zero-preview-heading">
            <span className="eyebrow">Собирается по ходу разговора</span>
            <h2>Кампания</h2>
          </div>

          <section className="session-zero-preview-section">
            <h3>Мир</h3>
            <PreviewField label="Сеттинг" value={world?.setting_name || world?.genre} />
            <PreviewField label="Тон" value={world?.tone} />
            <PreviewText value={world?.premise || world?.world_summary} fallback="Пока мастер только знакомится с идеей мира." />
          </section>

          <section className="session-zero-preview-section">
            <h3>Герой</h3>
            <PreviewField label="Имя" value={hero?.name} />
            <PreviewText value={hero?.description || hero?.personality} fallback="Образ героя появится здесь по мере разговора." />
            {hero?.first_goal && <div className="session-zero-goal"><span>Первая цель</span><strong>{hero.first_goal}</strong></div>}
          </section>

          <section className="session-zero-preview-section">
            <h3>Старт</h3>
            <PreviewField label="Место" value={world?.starting_location_name} />
            <PreviewText value={world?.starting_situation} fallback="Стартовая ситуация ещё не определена." />
          </section>

          {interview.state.last_summary && <details className="session-zero-summary-details">
            <summary>Текущая сводка</summary>
            <p>{interview.state.last_summary}</p>
          </details>}
        </aside>
      </div>}

      {!loading && error && !interview && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
    </main>
  </div>
}

function PreviewField({ label, value }: { label: string; value?: string | null }) {
  return <div className={`session-zero-preview-field ${value ? 'filled' : ''}`}>
    <span>{label}</span>
    <strong>{value || '—'}</strong>
  </div>
}

function PreviewText({ value, fallback }: { value?: string | null; fallback: string }) {
  return <p className={value ? '' : 'empty'}>{value || fallback}</p>
}
