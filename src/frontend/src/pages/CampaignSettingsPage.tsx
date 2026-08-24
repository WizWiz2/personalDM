import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { Icons } from '../components/Icons'
import { RuntimeProviderSettings } from '../components/RuntimeProviderSettings'
import { ErrorState } from '../components/States'

export function CampaignSettingsPage() {
  const { campaign, refreshCampaign } = useCampaignWorkspace()
  const navigate = useNavigate()
  const [name, setName] = useState(campaign.name)
  const [description, setDescription] = useState(campaign.description ?? '')
  const [style, setStyle] = useState(campaign.narrative_style ?? '')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const saveCampaign = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setMessage('')
    try {
      await api.updateCampaign(campaign.id, {
        name,
        description: description || null,
        narrative_style: style || null,
      })
      await refreshCampaign()
      setMessage('Настройки кампании сохранены.')
    } catch (err) {
      setError(readableError(err))
    }
  }

  const exportCampaign = async () => {
    try {
      const data = await api.exportCampaign(campaign.id)
      const blob = new Blob([JSON.stringify(data.archive, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${campaign.name.replace(/[^a-zа-я0-9_-]+/gi, '-')}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(readableError(err))
    }
  }

  return <div className="workspace-page">
    <header className="workspace-topbar">
      <div><h1>Кампания</h1><p>Настройки мира, моделей и PersonalDM</p></div>
      <button className="btn" onClick={() => void exportCampaign()}><Icons.download />Экспорт</button>
    </header>
    <div className="page-content settings-content">
      {error && <ErrorState message={error} />}
      {message && <div className="success-note">{message}</div>}
      <div className="settings-grid">
        <form className="settings-section" onSubmit={saveCampaign}>
          <span className="eyebrow">Основное</span>
          <h2>{campaign.name}</h2>
          <label>Название<input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label>Описание<textarea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} /></label>
          <label>Стиль повествования<textarea rows={3} value={style} onChange={(e) => setStyle(e.target.value)} /></label>
          <button className="btn primary">Сохранить</button>
        </form>

        <RuntimeProviderSettings
          campaignId={campaign.id}
          onMessage={setMessage}
          onError={setError}
        />

        <section className="settings-section">
          <span className="eyebrow">Состояние</span>
          <h2>Campaign Truth Engine</h2>
          <div className="settings-row"><span>Текущая сцена</span><strong>{campaign.current_scene_id ? 'есть' : 'не создана'}</strong></div>
          <div className="settings-row"><span>Герой</span><strong>{campaign.player_character_id ? 'привязан' : 'не выбран'}</strong></div>
          <div className="settings-row"><span>Обновлено</span><strong>{new Date(campaign.updated_at).toLocaleString('ru-RU')}</strong></div>
        </section>

        <section className="settings-section">
          <span className="eyebrow">Иллюстрации</span>
          <h2>Галерея</h2>
          <p>Уже созданные обложки, портреты и сцены сохраняются независимо от текущего image provider.</p>
          <button className="btn" type="button" onClick={() => navigate(`/campaign/${campaign.id}/gallery`)}><Icons.gallery />Открыть галерею</button>
        </section>
      </div>
    </div>
  </div>
}
