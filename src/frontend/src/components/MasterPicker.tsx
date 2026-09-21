import { useEffect, useMemo, useState } from 'react'
import { api, readableError } from '../api/client'
import type { CampaignMasterRead, GameMasterPersona, SetCampaignMasterRequest } from '../api/types'

type Props = {
  campaignId: string
  heading?: string
  compact?: boolean
  onChanged?: (current: CampaignMasterRead) => void
}

export function MasterPicker({ campaignId, heading = 'Мастер игры', compact = false, onChanged }: Props) {
  const [presets, setPresets] = useState<GameMasterPersona[]>([])
  const [current, setCurrent] = useState<CampaignMasterRead | null>(null)
  const [selectedId, setSelectedId] = useState<string>('iron_chronicler')
  const [customMode, setCustomMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [customName, setCustomName] = useState('')
  const [customBlurb, setCustomBlurb] = useState('')
  const [customBrief, setCustomBrief] = useState('')
  const [customVoice, setCustomVoice] = useState('')
  const [customPhrases, setCustomPhrases] = useState('')
  const [basePresetId, setBasePresetId] = useState('soft_keeper')

  const selectedPreset = useMemo(
    () => presets.find((item) => item.id === selectedId) ?? null,
    [presets, selectedId],
  )

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [list, campaignMaster] = await Promise.all([
          api.listGameMasterPresets(),
          api.getCampaignGameMaster(campaignId),
        ])
        if (cancelled) return
        setPresets(list)
        setCurrent(campaignMaster)
        setSelectedId(campaignMaster.resolved.id.startsWith('custom_')
          ? (campaignMaster.state.custom?.base_preset_id || list[0]?.id || 'iron_chronicler')
          : campaignMaster.resolved.id)
        setCustomMode(campaignMaster.state.kind === 'custom')
        onChanged?.(campaignMaster)
      } catch (err) {
        if (!cancelled) setError(readableError(err))
      }
    })()
    return () => { cancelled = true }
  }, [campaignId])

  const savePreset = async (presetId: string) => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const payload: SetCampaignMasterRequest = { kind: 'preset', preset_id: presetId }
      const saved = await api.setCampaignGameMaster(campaignId, payload)
      setCurrent(saved)
      setSelectedId(presetId)
      setCustomMode(false)
      setMessage(`Мастер: ${saved.resolved.display_name}`)
      onChanged?.(saved)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setBusy(false)
    }
  }

  const saveCustom = async () => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const phrases = customPhrases.split('\n').map((item) => item.trim()).filter(Boolean)
      const payload: SetCampaignMasterRequest = {
        kind: 'custom',
        custom: {
          display_name: customName.trim(),
          blurb: customBlurb.trim(),
          brief: customBrief.trim(),
          voice_style: customVoice.trim(),
          catchphrases: phrases,
          base_preset_id: basePresetId,
        },
      }
      const saved = await api.setCampaignGameMaster(campaignId, payload)
      setCurrent(saved)
      setCustomMode(true)
      setMessage(`Свой мастер сохранён: ${saved.resolved.display_name}`)
      onChanged?.(saved)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className={`master-picker ${compact ? 'compact' : ''}`}>
      <div className="master-picker-head">
        <div>
          <span className="eyebrow">Ведущий</span>
          <h2>{heading}</h2>
          <p>Не слайдеры «жёсткости», а персона: бриф, голос и закрытый набор режиссёрских ходов.</p>
        </div>
        {current && (
          <div className="master-current-chip">
            <img src={current.resolved.portrait_pixel} alt="" />
            <div>
              <strong>{current.resolved.display_name}</strong>
              <span>{current.state.kind === 'custom' ? 'свой' : 'пресет'}</span>
            </div>
          </div>
        )}
      </div>

      {error && <div className="error-note">{error}</div>}
      {message && <div className="success-note">{message}</div>}

      <div className="master-card-grid">
        {presets.map((preset) => {
          const active = !customMode && selectedId === preset.id
          return (
            <button
              key={preset.id}
              type="button"
              className={`master-card ${active ? 'active' : ''}`}
              disabled={busy}
              onClick={() => void savePreset(preset.id)}
            >
              <img className="master-engraving" src={preset.portrait_engraving} alt={preset.display_name} />
              <div className="master-card-body">
                <strong>{preset.display_name}</strong>
                <span>{preset.blurb}</span>
              </div>
            </button>
          )
        })}
      </div>

      {selectedPreset && !customMode && (
        <details className="master-brief">
          <summary>Бриф: {selectedPreset.display_name}</summary>
          <p>{selectedPreset.brief}</p>
          <p><em>{selectedPreset.voice_style}</em></p>
          <ul>
            {selectedPreset.catchphrases.map((phrase) => <li key={phrase}>{phrase}</li>)}
          </ul>
        </details>
      )}

      <div className="master-custom-panel">
        <div className="master-custom-head">
          <h3>Свой мастер</h3>
          <button type="button" className="btn ghost" disabled={busy} onClick={() => setCustomMode((value) => !value)}>
            {customMode ? 'К пресетам' : 'Создать своего'}
          </button>
        </div>
        {customMode && (
          <div className="master-custom-form">
            <label>Имя<input value={customName} onChange={(e) => setCustomName(e.target.value)} /></label>
            <label>Коротко<textarea rows={2} value={customBlurb} onChange={(e) => setCustomBlurb(e.target.value)} /></label>
            <label>Бриф (1–3 абзаца)<textarea rows={5} value={customBrief} onChange={(e) => setCustomBrief(e.target.value)} /></label>
            <label>Голос<textarea rows={2} value={customVoice} onChange={(e) => setCustomVoice(e.target.value)} /></label>
            <label>Фразы (по одной на строку)<textarea rows={3} value={customPhrases} onChange={(e) => setCustomPhrases(e.target.value)} /></label>
            <label>Базовый пресет политики
              <select value={basePresetId} onChange={(e) => setBasePresetId(e.target.value)}>
                {presets.map((preset) => (
                  <option key={preset.id} value={preset.id}>{preset.display_name}</option>
                ))}
              </select>
            </label>
            <button type="button" className="btn primary" disabled={busy} onClick={() => void saveCustom()}>
              Сохранить своего мастера
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
