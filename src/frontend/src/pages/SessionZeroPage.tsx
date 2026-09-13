import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  api,
  readableError,
  type SessionZeroInterviewSnapshot,
} from '../api/client'
import type { Campaign, SessionZero } from '../api/types'
import { BrandLogo } from '../components/BrandLogo'
import { Icons } from '../components/Icons'
import { ErrorState, LoadingState } from '../components/States'

export function SessionZeroPage() {
  const { campaignId = '' } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [setup, setSetup] = useState<SessionZero | null>(null)
  const [interview, setInterview] = useState<SessionZeroInterviewSnapshot | null>(null)
  const [summary, setSummary] = useState('')
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null)
  const [lastTechnicalError, setLastTechnicalError] = useState('')
  const [notice, setNotice] = useState('')
  const transcriptEnd = useRef<HTMLDivElement | null>(null)
  const autoRetryAttempted = useRef(false)

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
      setSummary(i.summary)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    autoRetryAttempted.current = false
    void load()
  }, [campaignId])

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [interview?.state.messages.length, interview?.state.pending_user_message, sending])

  const messages = useMemo(() => {
    if (!interview) return []
    const base = interview.state.messages.length
      ? [...interview.state.messages]
      : [{ role: 'assistant' as const, content: interview.opening_message }]
    const pending = interview.state.pending_user_message?.trim()
    if (pending && !base.some((message) => message.role === 'user' && message.content.trim() === pending)) {
      base.push({ role: 'user' as const, content: pending })
    }
    if (
      optimisticUser
      && !base.some((message) => message.role === 'user' && message.content.trim() === optimisticUser.trim())
    ) {
      base.push({ role: 'user' as const, content: optimisticUser })
    }
    return base
  }, [interview, optimisticUser])

  const refreshAfterTurn = async () => {
    const [s, i] = await Promise.all([
      api.getSessionZero(campaignId),
      api.getSessionZeroInterview(campaignId),
    ])
    setSetup(s)
    setInterview(i)
    setSummary(i.summary)
  }

  const applyTurnResult = async (result: Awaited<ReturnType<typeof api.retrySessionZeroInterview>>) => {
    setInterview((current) => current ? {
      ...current,
      status: result.completed ? 'completed' : current.status,
      summary: result.summary,
      state: result.state,
    } : current)
    setSummary(result.summary)
    setLastTechnicalError('')
    if (result.completed) setSetup(await api.getSessionZero(campaignId))
  }

  const retry = async () => {
    if (sending) return
    setSending(true)
    setError('')
    setNotice('')
    try {
      await applyTurnResult(await api.retrySessionZeroInterview(campaignId))
    } catch (err) {
      setError(readableError(err))
      setLastTechnicalError(extractTechnicalError(err))
      await refreshAfterTurn().catch(() => undefined)
    } finally {
      setSending(false)
    }
  }

  const completed = setup?.status === 'completed' || interview?.status === 'completed'
  const pending = Boolean(interview?.state.pending_user_message)

  // CLI parity: reopening a conversation with a persisted pending answer retries it once automatically.
  useEffect(() => {
    if (
      !loading &&
      pending &&
      !completed &&
      !sending &&
      !autoRetryAttempted.current
    ) {
      autoRetryAttempted.current = true
      void retry()
    }
  }, [loading, pending, completed, sending])

  const showSummary = () => {
    setNotice(summary || interview?.state.last_summary || 'Сводка пока пуста.')
  }

  const showLastError = () => {
    setNotice(
      lastTechnicalError
        ? `Последняя техническая ошибка: ${lastTechnicalError}`
        : 'Технической ошибки в этой сессии пока нет.',
    )
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const message = input.trim()
    if (!message || sending || completed) return

    const command = message.toLocaleLowerCase('ru-RU')
    if (command === '/later') {
      setInput('')
      navigate('/campaigns')
      return
    }
    if (command === '/summary') {
      setInput('')
      showSummary()
      return
    }
    if (command === '/error') {
      setInput('')
      showLastError()
      return
    }
    if (command === '/retry') {
      setInput('')
      if (pending) void retry()
      else setNotice('Нет сохранённого ответа, ожидающего обработки.')
      return
    }

    setSending(true)
    setError('')
    setNotice('')
    setOptimisticUser(message)
    setInput('')
    try {
      await applyTurnResult(await api.answerSessionZeroInterview(campaignId, message))
      setOptimisticUser(null)
    } catch (err) {
      setError(readableError(err))
      setLastTechnicalError(extractTechnicalError(err))
      await refreshAfterTurn().catch(() => undefined)
    } finally {
      setSending(false)
    }
  }

  const draft = interview?.state.draft
  const world = draft?.world
  const hero = draft?.character

  return <div className="global-page session-zero-page">
    <header className="global-topbar session-zero-topbar">
      <div className="global-brand-heading">
        <BrandLogo compact />
        <span className="brand-divider" aria-hidden="true" />
        <div>
          <h1>Нулевая сессия</h1>
          <p>{campaign?.name || 'Новая кампания'} · разговор с мастером</p>
        </div>
      </div>
      <div className="session-zero-top-actions">
        {!completed && <span className="session-zero-save-state">Сохраняется автоматически</span>}
        {!completed && <button className="btn ghost" onClick={showSummary}>Сводка</button>}
        <button className="btn" onClick={() => navigate('/campaigns')}>
          <Icons.back />{completed ? 'К кампаниям' : 'Продолжить позже'}
        </button>
      </div>
    </header>

    <main className="global-content session-zero-conversation-shell">
      {loading && <LoadingState label="Подготавливаем нулевую сессию…" />}

      {!loading && interview && <div className="session-zero-conversation-layout">
        <section className="session-zero-conversation" aria-label="Разговор нулевой сессии">
          <div className="session-zero-intro">
            {completed ? (
              <>
                <span className="eyebrow">Нулевая сессия завершена</span>
                <h2>Можно начинать приключение</h2>
                <p>Договорённости собраны. Открой игру или вернись к сводке ниже — писать «Начинаем» не нужно.</p>
              </>
            ) : (
              <>
                <span className="eyebrow">До первой сцены</span>
                <h2>Соберём игру разговором</h2>
                <p>Это тот же разговорный режим, что и в CLI: отвечай свободно, исправляй сказанное и начинай с любой стороны будущей игры. Мастер сам решит, когда данных достаточно.</p>
              </>
            )}
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

          {notice && <div className="session-zero-system-note">
            <span>{notice}</span>
            <button type="button" onClick={() => setNotice('')} aria-label="Закрыть"><Icons.close /></button>
          </div>}

          {error && <div className="session-zero-inline-error">
            <strong>Ответ мастера не получен.</strong>
            <span>{error}</span>
            <div className="session-zero-error-actions">
              {pending && <button className="btn" disabled={sending} onClick={() => void retry()}><Icons.refresh />Повторить</button>}
              <button className="btn ghost" type="button" onClick={showLastError}>Техническая причина</button>
            </div>
          </div>}

          {completed ? <>
            <div className="session-zero-final-summary">
              <span className="eyebrow">Итоговые договорённости</span>
              <p>{dedupeSummaryText(summary || interview.state.last_summary || 'Нулевая сессия завершена.')}</p>
            </div>
            <div className="session-zero-ready">
              <div>
                <span className="eyebrow">Готово</span>
                <strong>Нулевая сессия завершена</strong>
                <p>{setup?.starting_scene_title || 'Первая сцена подготовлена.'}</p>
              </div>
              <button className="btn primary" onClick={() => navigate(`/campaign/${campaignId}/play`)}>
                Начать приключение <Icons.chevron />
              </button>
            </div>
          </> : pending && sending ? <div className="session-zero-pending">
            <span>Мастер обрабатывает сохранённый ответ…</span>
          </div> : pending ? <div className="session-zero-pending">
            <span>Последний ответ сохранён. Можно повторить его без повторного ввода.</span>
            <button className="btn primary" onClick={() => void retry()}><Icons.refresh />Повторить</button>
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
              <span>Enter — отправить · Shift+Enter — новая строка · /summary /retry /error /later</span>
              <button className="send-btn" type="submit" disabled={sending || !input.trim()} aria-label="Отправить">
                <Icons.send />
              </button>
            </div>
          </form>}
        </section>

        <aside className="session-zero-preview" aria-label="Черновик кампании">
          <div className="session-zero-preview-heading">
            <span className="eyebrow">{completed ? 'Собрано для старта' : 'Собирается по ходу разговора'}</span>
            <h2>Кампания</h2>
          </div>

          <section className="session-zero-preview-section">
            <h3>Мир</h3>
            {world?.setting_name
              ? <PreviewField label="Сеттинг" value={world.setting_name} />
              : world?.genre
                ? <PreviewField label="Жанр" value={world.genre} />
                : <p className="empty">Сеттинг и жанр появятся, когда обсудите мир.</p>}
            {world?.tone
              ? <PreviewField label="Тон" value={world.tone} />
              : <p className="empty">Тон появится после первых ответов о настроении игры.</p>}
            <PreviewText value={world?.premise || world?.world_summary} fallback="Пока мастер только знакомится с идеей мира." />
          </section>

          <section className="session-zero-preview-section">
            <h3>Герой</h3>
            {hero?.name
              ? <PreviewField label="Имя" value={hero.name} />
              : <p className="empty">Имя героя появится, когда его обсудите.</p>}
            <PreviewText value={hero?.description || hero?.personality} fallback="Образ героя появится здесь по мере разговора." />
            {hero?.first_goal && <div className="session-zero-goal"><span>Первая цель</span><strong>{hero.first_goal}</strong></div>}
          </section>

          <section className="session-zero-preview-section">
            <h3>Старт</h3>
            {world?.starting_location_name
              ? <PreviewField label="Место" value={world.starting_location_name} />
              : <p className="empty">Стартовое место появится ближе к запуску.</p>}
            <PreviewText value={world?.starting_situation} fallback="Стартовая ситуация ещё не определена." />
          </section>

          {summary && <details className="session-zero-summary-details">
            <summary>Текущая сводка</summary>
            <p>{summary}</p>
          </details>}
        </aside>
      </div>}

      {!loading && error && !interview && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
    </main>
  </div>
}

function normalizeDesireItem(value: string): string {
  return value
    .toLocaleLowerCase('ru-RU')
    .replace(/[.…]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function dedupeSemicolonList(line: string): string {
  const colon = line.indexOf(':')
  if (colon < 0 || !line.includes(';')) return line
  const label = line.slice(0, colon + 1)
  const items = line.slice(colon + 1).split(';').map((part) => part.trim()).filter(Boolean)
  const kept: string[] = []
  for (const item of items) {
    const norm = normalizeDesireItem(item)
    let merged = false
    for (let index = 0; index < kept.length; index += 1) {
      const existingNorm = normalizeDesireItem(kept[index])
      if (norm === existingNorm || norm.startsWith(existingNorm) || existingNorm.startsWith(norm)) {
        if (item.length > kept[index].length) kept[index] = item
        merged = true
        break
      }
    }
    if (!merged) kept.push(item)
  }
  return `${label} ${kept.join('; ')}`
}

function dedupeSummaryText(text: string): string {
  const lines = text.split('\n')
  const out: string[] = []
  for (const line of lines) {
    const cleaned = dedupeSemicolonList(line)
    const trimmed = cleaned.trim()
    if (trimmed && out.length && out[out.length - 1].trim() === trimmed) continue
    out.push(cleaned)
  }
  return out.join('\n')
}

function extractTechnicalError(error: unknown): string {
  if (error instanceof ApiError) {
    const payload = error.detail as { detail?: unknown } | null
    const detail = payload?.detail
    if (detail && typeof detail === 'object' && 'technical_detail' in detail) {
      return String((detail as { technical_detail: unknown }).technical_detail)
    }
  }
  return readableError(error)
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
