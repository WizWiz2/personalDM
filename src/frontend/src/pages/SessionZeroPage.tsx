import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, readableError } from '../api/client'
import type { Campaign, SessionZero } from '../api/types'
import { Icons } from '../components/Icons'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

export function SessionZeroPage() {
  const { campaignId = '' } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [setup, setSetup] = useState<SessionZero | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true); setError('')
    try {
      const [c, s] = await Promise.all([api.getCampaign(campaignId), api.getSessionZero(campaignId)])
      setCampaign(c); setSetup(s)
    } catch (err) { setError(readableError(err)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [campaignId])

  return <div className="global-page">
    <header className="global-topbar"><div><h1>Нулевая сессия</h1><p>{campaign?.name || 'Подготовка кампании'}</p></div><button className="btn" onClick={() => navigate('/campaigns')}><Icons.back />К кампаниям</button></header>
    <div className="global-content session-zero-shell">
      {loading && <LoadingState label="Загружаем подготовку…" />}
      {error && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
      {setup && <>
        <section className="session-zero-summary">
          <span className="eyebrow">Статус</span><h2>{setup.status === 'completed' ? 'Кампания готова' : 'Подготовка не завершена'}</h2>
          <p>{setup.premise || setup.world_summary || 'Мир и герой пока заполняются.'}</p>
          <div className="setup-facts"><div><span>Сеттинг</span><strong>{setup.setting_name || '—'}</strong></div><div><span>Жанр</span><strong>{setup.genre || '—'}</strong></div><div><span>Герой</span><strong>{setup.player_character_name || '—'}</strong></div><div><span>Старт</span><strong>{setup.starting_location_name || setup.starting_scene_title || '—'}</strong></div></div>
          {setup.status !== 'completed' && setup.missing_fields.length > 0 && <div className="missing-fields"><strong>До запуска не хватает:</strong><div className="chips">{setup.missing_fields.map((field) => <span className="chip" key={field}>{field}</span>)}</div></div>}
          <div className="session-zero-actions">
            {setup.status === 'completed' ? <button className="btn primary" onClick={() => navigate(`/campaign/${campaignId}/play`)}>Начать приключение</button> : <>
              <a className="btn primary" href="/api/session-zero-ui" target="_blank" rel="noreferrer">Открыть текущий редактор</a>
              <button className="btn" onClick={() => void load()}><Icons.refresh />Обновить</button>
            </>}
          </div>
        </section>
        {setup.status !== 'completed' && <div className="product-gap-note"><strong>Почему здесь пока не чат с мастером?</strong><p>Backend сейчас предоставляет структурированный контракт нулевой сессии и старый служебный редактор, но не разговорный Session Zero endpoint. Новый frontend не имитирует диалог, которого на самом деле нет: разговорный режим будет отдельным следующим интеграционным шагом.</p></div>}
      </>}
      {!loading && !error && !setup && <EmptyState title="Нет данных нулевой сессии" />}
    </div>
  </div>
}
