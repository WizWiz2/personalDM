import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  type ChatGPTModel,
  type RuntimeProviderProfile,
  runtimeProviderApi,
} from '../api/runtimeProviders'
import type { UUID } from '../api/types'

interface Props {
  campaignId: UUID
  disabled?: boolean
  onError: (message: string) => void
}

type TextMode = RuntimeProviderProfile['text']['mode']

const OAUTH_SWITCH_KEY = 'pdm-chatgpt-quick-switch-campaign'

export function TextProviderQuickSwitch({ campaignId, disabled = false, onError }: Props) {
  const navigate = useNavigate()
  const [profile, setProfile] = useState<RuntimeProviderProfile | null>(null)
  const [models, setModels] = useState<ChatGPTModel[]>([])
  const [busy, setBusy] = useState(false)

  const applyText = useCallback((text: RuntimeProviderProfile['text']) => {
    setProfile((current) => current ? { ...current, text } : current)
  }, [])

  const activateChatGPT = useCallback(async (currentModel: string) => {
    const { models: available, default_model } = await runtimeProviderApi.chatGPTModels()
    setModels(available)
    if (!default_model) throw new Error('В ChatGPT plan нет доступных текстовых моделей')
    return runtimeProviderApi.configureText({
      mode: 'chatgpt',
      model: available.some((item) => item.slug === currentModel) ? currentModel : default_model,
      campaign_id: campaignId,
    })
  }, [campaignId])

  const initialize = useCallback(async () => {
    try {
      let next = await runtimeProviderApi.profile()
      setProfile(next)

      const pendingCampaign = window.sessionStorage.getItem(OAUTH_SWITCH_KEY)
      const returningFromOAuth = pendingCampaign === campaignId

      if (next.text.chatgpt.connected && returningFromOAuth) {
        const text = await activateChatGPT(next.text.model)
        window.sessionStorage.removeItem(OAUTH_SWITCH_KEY)
        next = { ...next, text }
        setProfile(next)
      } else if (next.text.chatgpt.connected && next.text.mode === 'chatgpt') {
        setModels((await runtimeProviderApi.chatGPTModels()).models)
      } else if (returningFromOAuth && !next.text.chatgpt.connected) {
        window.sessionStorage.removeItem(OAUTH_SWITCH_KEY)
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Не удалось загрузить текстовый provider')
    }
  }, [campaignId, activateChatGPT, onError])

  useEffect(() => {
    void initialize()
  }, [initialize])

  const switchMode = async (mode: TextMode) => {
    if (!profile || disabled || busy || mode === profile.text.mode) return

    if (mode === 'cloud') {
      navigate(`/campaign/${campaignId}/settings`)
      return
    }

    setBusy(true)
    onError('')
    try {
      if (mode === 'local') {
        const text = await runtimeProviderApi.configureText({
          mode: 'local',
          campaign_id: campaignId,
        })
        applyText(text)
        setModels([])
        return
      }

      if (!profile.text.chatgpt.connected) {
        window.sessionStorage.setItem(OAUTH_SWITCH_KEY, campaignId)
        const { authorization_url } = await runtimeProviderApi.startChatGPTSignIn(window.location.href)
        window.location.assign(authorization_url)
        return
      }

      applyText(await activateChatGPT(profile.text.model))
    } catch (error) {
      window.sessionStorage.removeItem(OAUTH_SWITCH_KEY)
      onError(error instanceof Error ? error.message : 'Не удалось переключить текстовый provider')
    } finally {
      setBusy(false)
    }
  }

  const switchModel = async (model: string) => {
    if (!profile || profile.text.mode !== 'chatgpt' || disabled || busy || !model) return
    if (model === profile.text.model) return
    setBusy(true)
    onError('')
    try {
      const text = await runtimeProviderApi.configureText({
        mode: 'chatgpt',
        model,
        campaign_id: campaignId,
      })
      applyText(text)
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Не удалось переключить модель ChatGPT')
    } finally {
      setBusy(false)
    }
  }

  const locked = disabled || busy || !profile
  const mode = profile?.text.mode ?? 'local'
  const statusTitle = disabled
    ? 'Дождись окончания текущего хода перед сменой модели'
    : profile?.text.status.message ?? 'Загружаем provider…'

  return (
    <div className="text-provider-switch" title={statusTitle}>
      <span
        className={`provider-status-dot ${profile?.text.status.ready ? 'ready' : ''}`}
        aria-hidden="true"
      />
      <select
        className="quick-provider-select"
        aria-label="Текстовый provider"
        value={mode}
        disabled={locked}
        onChange={(event) => void switchMode(event.target.value as TextMode)}
      >
        <option value="local">Ollama</option>
        <option value="chatgpt">ChatGPT</option>
        <option value="cloud">API · настройки</option>
      </select>

      {mode === 'chatgpt' && (
        <select
          className="quick-provider-model"
          aria-label="Модель ChatGPT"
          value={profile?.text.model ?? ''}
          disabled={locked || !models.length}
          onChange={(event) => void switchModel(event.target.value)}
        >
          {!models.length && (
            <option value={profile?.text.model ?? ''}>
              {profile?.text.model || 'Модели…'}
            </option>
          )}
          {models.map((item) => (
            <option key={item.slug} value={item.slug}>{item.display_name}</option>
          ))}
        </select>
      )}
    </div>
  )
}
