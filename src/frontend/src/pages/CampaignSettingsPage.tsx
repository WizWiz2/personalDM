import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError, readableError } from '../api/client'
import type { ProviderConfig } from '../api/types'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { Icons } from '../components/Icons'
import { ErrorState } from '../components/States'

export function CampaignSettingsPage() {
  const { campaign, refreshCampaign } = useCampaignWorkspace()
  const [name, setName] = useState(campaign.name)
  const [description, setDescription] = useState(campaign.description ?? '')
  const [style, setStyle] = useState(campaign.narrative_style ?? '')
  const [provider, setProvider] = useState<ProviderConfig | null>(null)
  const [baseUrl, setBaseUrl] = useState('http://localhost:11434/v1')
  const [model, setModel] = useState('')
  const [contextWindow, setContextWindow] = useState(8192)
  const [apiKey, setApiKey] = useState('')
  const [connected, setConnected] = useState<boolean | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.getProvider(campaign.id).then((data) => { setProvider(data); setBaseUrl(data.base_url); setModel(data.model_name); setContextWindow(data.context_window) }).catch((err) => { if (!(err instanceof ApiError && err.status === 404)) setError(readableError(err)) })
  }, [campaign.id])

  const saveCampaign = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setMessage('')
    try { await api.updateCampaign(campaign.id, { name, description: description || null, narrative_style: style || null }); await refreshCampaign(); setMessage('Настройки кампании сохранены.') } catch (err) { setError(readableError(err)) }
  }

  const saveProvider = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setMessage('')
    try { const data = await api.saveProvider(campaign.id, { base_url: baseUrl, model_name: model, context_window: contextWindow, ...(apiKey ? { api_key: apiKey } : {}) }); setProvider(data); setApiKey(''); setMessage('Провайдер сохранён.') } catch (err) { setError(readableError(err)) }
  }

  const check = async () => {
    setConnected(null)
    try { const result = await api.checkProvider(campaign.id); setConnected(result.connected) } catch (err) { setError(readableError(err)) }
  }

  const exportCampaign = async () => {
    try {
      const data = await api.exportCampaign(campaign.id)
      const blob = new Blob([JSON.stringify(data.archive, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = `${campaign.name.replace(/[^a-zа-я0-9_-]+/gi, '-')}.json`; a.click(); URL.revokeObjectURL(url)
    } catch (err) { setError(readableError(err)) }
  }

  return <div className="workspace-page">
    <header className="workspace-topbar"><div><h1>Кампания</h1><p>Настройки мира и PersonalDM</p></div><button className="btn" onClick={() => void exportCampaign()}><Icons.download />Экспорт</button></header>
    <div className="page-content settings-content">
      {error && <ErrorState message={error} />}{message && <div className="success-note">{message}</div>}
      <div className="settings-grid">
        <form className="settings-section" onSubmit={saveCampaign}><span className="eyebrow">Основное</span><h2>{campaign.name}</h2><label>Название<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>Описание<textarea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} /></label><label>Стиль повествования<textarea rows={3} value={style} onChange={(e) => setStyle(e.target.value)} /></label><button className="btn primary">Сохранить</button></form>
        <form className="settings-section" onSubmit={saveProvider}><span className="eyebrow">Мастер</span><h2>LLM Provider</h2><label>Base URL<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label><label>Модель<input value={model} onChange={(e) => setModel(e.target.value)} placeholder="qwen2.5:7b" /></label><label>Контекст<input type="number" min={1024} step={1024} value={contextWindow} onChange={(e) => setContextWindow(Number(e.target.value))} /></label><label>API key<input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={provider?.has_api_key ? '•••••••• (оставь пустым, чтобы не менять)' : 'необязательно'} /></label><div className="settings-actions"><button className="btn primary">Сохранить</button><button className="btn" type="button" onClick={() => void check()}><Icons.refresh />Проверить</button></div>{connected !== null && <div className={connected ? 'connection-ok' : 'connection-bad'}>{connected ? '● Подключено' : '● Нет соединения'}</div>}</form>
        <section className="settings-section"><span className="eyebrow">Состояние</span><h2>Campaign Truth Engine</h2><div className="settings-row"><span>Текущая сцена</span><strong>{campaign.current_scene_id ? 'есть' : 'не создана'}</strong></div><div className="settings-row"><span>Герой</span><strong>{campaign.player_character_id ? 'привязан' : 'не выбран'}</strong></div><div className="settings-row"><span>Обновлено</span><strong>{new Date(campaign.updated_at).toLocaleString('ru-RU')}</strong></div></section>
        <section className="settings-section"><span className="eyebrow">Иллюстрации</span><h2>Scene Art</h2><p>Фронтенд уже резервирует место для обложек, портретов и иллюстраций сцен. ImageProvider будет отдельным следующим scope.</p><button className="btn" disabled>Настроить генератор</button></section>
      </div>
    </div>
  </div>
}
