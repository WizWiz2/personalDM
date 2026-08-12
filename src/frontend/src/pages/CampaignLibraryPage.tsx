import { FormEvent, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import type { Campaign } from '../api/types'
import { Icons } from '../components/Icons'
import { PixelScene } from '../components/PixelArt'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

const dateFmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' })

function relativeUpdated(value: string) {
  const then = new Date(value).getTime()
  const diff = Date.now() - then
  if (diff < 24 * 60 * 60 * 1000) return 'Сегодня'
  if (diff < 48 * 60 * 60 * 1000) return 'Вчера'
  return dateFmt.format(new Date(value))
}

export function CampaignLibraryPage() {
  const navigate = useNavigate()
  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modal, setModal] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await api.listCampaigns()
      setCampaigns([...data].sort((a, b) => b.updated_at.localeCompare(a.updated_at)))
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const recent = useMemo(() => campaigns.slice(0, 2), [campaigns])

  const openCampaign = async (campaign: Campaign) => {
    try {
      const setup = await api.getSessionZero(campaign.id)
      if (setup.status === 'completed') navigate(`/campaign/${campaign.id}/play`)
      else navigate(`/campaigns/${campaign.id}/session-zero`)
    } catch {
      navigate(`/campaign/${campaign.id}/play`)
    }
  }

  const createCampaign = async (event: FormEvent) => {
    event.preventDefault()
    if (!name.trim()) return
    setCreating(true)
    try {
      const campaign = await api.createCampaign({
        name: name.trim(),
        description: description.trim() || null,
      })
      setModal(false)
      navigate(`/campaigns/${campaign.id}/session-zero`)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="global-page">
      <header className="global-topbar">
        <div>
          <h1>Кампании</h1>
          <p>Твои истории</p>
        </div>
        <button className="btn primary" onClick={() => setModal(true)}><Icons.plus />Новая кампания</button>
      </header>

      <div className="global-content campaign-library">
        {loading && <LoadingState label="Загружаем кампании…" />}
        {error && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
        {!loading && !error && campaigns.length === 0 && (
          <EmptyState title="Пока нет кампаний" text="Создай первую историю — после этого появится нулевая сессия." action={<button className="btn primary" onClick={() => setModal(true)}>Создать кампанию</button>} />
        )}

        {!loading && !error && campaigns.length > 0 && (
          <>
            <section className="recent-section">
              <h2>Недавнее</h2>
              <div className="recent-list">
                {recent.map((campaign) => (
                  <button key={campaign.id} className="recent-campaign" onClick={() => void openCampaign(campaign)}>
                    <div className="recent-thumb"><PixelScene seed={campaign.name} compact /></div>
                    <div className="recent-copy">
                      <strong>{campaign.name}</strong>
                      <span>{campaign.description || (campaign.current_scene_id ? 'Кампания продолжается' : 'Подготовка кампании')}</span>
                    </div>
                    <span className="recent-time">{relativeUpdated(campaign.updated_at)}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className="all-campaigns-section">
              <h2>Все кампании</h2>
              <div className="campaign-grid">
                {campaigns.map((campaign) => (
                  <article className="campaign-card" key={campaign.id}>
                    <div className="campaign-cover"><PixelScene seed={campaign.name} /></div>
                    <div className="campaign-card-body">
                      <span className="eyebrow">{campaign.current_scene_id ? 'Активная' : 'Подготовка'}</span>
                      <h3>{campaign.name}</h3>
                      <p>{campaign.description || 'Описание кампании пока не задано.'}</p>
                      <div className="campaign-card-state">
                        <span>{campaign.player_character_id ? 'Герой создан' : 'Нужен герой'}</span>
                        <span>·</span>
                        <span>{relativeUpdated(campaign.updated_at)}</span>
                      </div>
                      <div className="campaign-card-footer">
                        <button className="btn primary" onClick={() => void openCampaign(campaign)}>Продолжить</button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          </>
        )}
      </div>

      {modal && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && setModal(false)}>
          <form className="modal" onSubmit={createCampaign}>
            <div className="modal-head">
              <div><span className="eyebrow">Новая история</span><h2>Создать кампанию</h2></div>
              <button type="button" className="icon-btn" onClick={() => setModal(false)} aria-label="Закрыть"><Icons.close /></button>
            </div>
            <label>Название<input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Например: Тени Неонового Города" /></label>
            <label>Короткое описание<textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="О чём эта история?" rows={4} /></label>
            <div className="modal-actions"><button type="button" className="btn" onClick={() => setModal(false)}>Отмена</button><button className="btn primary" disabled={creating || !name.trim()}>{creating ? 'Создаём…' : 'Создать'}</button></div>
          </form>
        </div>
      )}
    </div>
  )
}
