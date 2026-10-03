import { FormEvent, useEffect, useState } from 'react'
import type { UUID } from '../api/types'
import {
  type ChatGPTModel,
  type RuntimeProviderProfile,
  runtimeProviderApi,
} from '../api/runtimeProviders'
import { Icons } from './Icons'

interface Props {
  campaignId?: UUID
  onMessage: (message: string) => void
  onError: (message: string) => void
}

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

export function RuntimeProviderSettings({ campaignId, onMessage, onError }: Props) {
  const [profile, setProfile] = useState<RuntimeProviderProfile | null>(null)
  const [busy, setBusy] = useState('')
  const [loadFailed, setLoadFailed] = useState(false)

  const [textMode, setTextMode] = useState<'local' | 'cloud' | 'chatgpt'>('local')
  const [textBaseUrl, setTextBaseUrl] = useState('')
  const [textModel, setTextModel] = useState('')
  const [textContext, setTextContext] = useState(4096)
  const [textKey, setTextKey] = useState('')
  const [chatGPTModels, setChatGPTModels] = useState<ChatGPTModel[]>([])

  const [imageMode, setImageMode] = useState<'local' | 'cloud' | 'off'>('off')
  const [imageBaseUrl, setImageBaseUrl] = useState('')
  const [imageModel, setImageModel] = useState('gpt-image-2')
  const [imageKey, setImageKey] = useState('')

  const applyProfile = (next: RuntimeProviderProfile) => {
    setProfile(next)
    const returningFromChatGPT = window.sessionStorage.getItem('pdm-chatgpt-connect-return') === '1'
    if (returningFromChatGPT && next.text.chatgpt.connected) {
      setTextMode('chatgpt')
      window.sessionStorage.removeItem('pdm-chatgpt-connect-return')
    } else {
      setTextMode(next.text.mode)
    }
    setTextBaseUrl(next.text.base_url)
    setTextModel(next.text.model)
    setTextContext(next.text.context_window)
    setImageMode(next.image.mode)
    setImageBaseUrl(next.image.base_url)
    setImageModel(next.image.model)
  }

  const load = async () => {
    try {
      applyProfile(await runtimeProviderApi.profile())
      setLoadFailed(false)
    } catch (error) {
      setLoadFailed(true)
      onError(error instanceof Error ? error.message : 'Не удалось загрузить настройки моделей')
    }
  }

  useEffect(() => { void load() }, [campaignId])

  useEffect(() => {
    if (textMode !== 'chatgpt' || !profile?.text.chatgpt.connected) {
      setChatGPTModels([])
      return
    }
    void runtimeProviderApi.chatGPTModels()
      .then(({ models }) => {
        setChatGPTModels(models)
        if (models.length && !models.some((item) => item.slug === textModel)) {
          setTextModel(models[0].slug)
        }
      })
      .catch((error) => onError(
        error instanceof Error ? error.message : 'Не удалось получить модели ChatGPT',
      ))
  }, [textMode, profile?.text.chatgpt.connected])

  const changeTextMode = (mode: 'local' | 'cloud' | 'chatgpt') => {
    setTextMode(mode)
    if (mode === 'local') {
      setTextBaseUrl('http://127.0.0.1:11434/v1')
      setTextModel('gemma4:e4b')
      setTextContext(4096)
    } else if (mode === 'cloud') {
      setTextBaseUrl('https://api.openai.com/v1')
      setTextModel('gpt-4.1-mini')
      setTextContext(128000)
    } else {
      setTextBaseUrl('https://api.openai.com/v1')
      setTextContext(128000)
      if (chatGPTModels.length && !chatGPTModels.some((item) => item.slug === textModel)) {
        setTextModel(chatGPTModels[0].slug)
      }
    }
  }

  const changeImageMode = (mode: 'local' | 'cloud' | 'off') => {
    setImageMode(mode)
    if (mode === 'local') {
      setImageBaseUrl('http://127.0.0.1:8188')
      setImageModel('FLUX.2 Klein 4B FP8')
    } else if (mode === 'cloud') {
      setImageBaseUrl('https://api.openai.com/v1')
      setImageModel('gpt-image-2')
    }
  }

  const saveText = async (event: FormEvent) => {
    event.preventDefault()
    setBusy('text-save'); onError(''); onMessage('')
    try {
      await runtimeProviderApi.configureText({
        mode: textMode,
        ...(campaignId ? { campaign_id: campaignId } : {}),
        ...(textMode === 'cloud' ? {
          base_url: textBaseUrl,
          model: textModel,
          api_key: textKey || undefined,
          context_window: textContext,
        } : {
          model: textModel || undefined,
          context_window: textContext,
        }),
      })
      setTextKey('')
      await load()
      onMessage(textMode === 'local'
        ? 'Локальная текстовая модель выбрана. Проверь или установи runtime.'
        : textMode === 'chatgpt'
          ? 'ChatGPT plan выбран для текстовых запросов.'
          : 'Облачная текстовая модель сохранена.')
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Не удалось сохранить текстовую модель')
    } finally { setBusy('') }
  }

  const connectChatGPT = async () => {
    setBusy('chatgpt-connect'); onError(''); onMessage('')
    try {
      window.sessionStorage.setItem('pdm-chatgpt-connect-return', '1')
      const { authorization_url } = await runtimeProviderApi.startChatGPTSignIn(window.location.href)
      window.location.assign(authorization_url)
    } catch (error) {
      window.sessionStorage.removeItem('pdm-chatgpt-connect-return')
      onError(error instanceof Error ? error.message : 'Не удалось начать вход через ChatGPT')
      setBusy('')
    }
  }

  const disconnectChatGPT = async () => {
    setBusy('chatgpt-disconnect'); onError(''); onMessage('')
    try {
      await runtimeProviderApi.disconnectChatGPT()
      await load()
      onMessage('Локальная сессия ChatGPT отключена.')
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Не удалось отключить ChatGPT')
    } finally { setBusy('') }
  }

  const saveImage = async (event: FormEvent) => {
    event.preventDefault()
    setBusy('image-save'); onError(''); onMessage('')
    try {
      await runtimeProviderApi.configureImage({
        mode: imageMode,
        ...(imageMode === 'cloud' ? {
          base_url: imageBaseUrl,
          model: imageModel,
          api_key: imageKey || undefined,
        } : {}),
      })
      setImageKey('')
      await load()
      onMessage(imageMode === 'off'
        ? 'Новая генерация изображений отключена. Существующая галерея сохранена.'
        : imageMode === 'local'
          ? 'Локальная графика выбрана. Проверь или установи runtime.'
          : 'Облачная генерация изображений сохранена.')
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Не удалось сохранить графический provider')
    } finally { setBusy('') }
  }

  const checkAll = async () => {
    setBusy('check'); onError(''); onMessage('')
    try {
      await runtimeProviderApi.check()
      await load()
      onMessage('Проверка моделей завершена.')
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Проверка моделей не удалась')
    } finally { setBusy('') }
  }

  const install = async (kind: 'text' | 'image') => {
    setBusy(`${kind}-install`); onError(''); onMessage('')
    try {
      let job = await runtimeProviderApi.install(kind)
      while (job.status === 'running') {
        await sleep(1000)
        job = await runtimeProviderApi.installJob(job.id)
      }
      if (job.status === 'failed') throw new Error(job.error || 'Установка завершилась с ошибкой')
      await load()
      onMessage(kind === 'text' ? 'Локальная текстовая модель готова.' : 'Локальная графика готова.')
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Установка не удалась')
    } finally { setBusy('') }
  }

  const status = (ready: boolean, message: string) => (
    <div className={ready ? 'connection-ok' : 'connection-bad'}>● {message}</div>
  )

  if (!profile) {
    return <section className="settings-section">
      <h2>Модели</h2>
      {loadFailed ? (
        <>
          <p>Не удалось загрузить конфигурацию моделей.</p>
          <button className="btn" type="button" onClick={() => { setLoadFailed(false); void load() }}>Повторить</button>
        </>
      ) : (
        <p>Загружаем конфигурацию…</p>
      )}
    </section>
  }

  const imageNeedsHelp = imageMode === 'local' && !profile.image.status.ready

  return <>
    <section className="settings-section models-status-summary">
      <span className="eyebrow">Статус</span>
      <h2>Модели</h2>
      <div className="models-status-grid">
        <div>
          <strong>Текст</strong>
          {status(profile.text.status.ready, profile.text.status.message)}
        </div>
        <div>
          <strong>Графика</strong>
          {status(
            imageMode === 'off' ? true : profile.image.status.ready,
            imageMode === 'off' ? 'Генерация выключена' : profile.image.status.message,
          )}
        </div>
      </div>
      {imageNeedsHelp && (
        <div className="models-runtime-warning">
          <p>Локальный image runtime не готов (часто это ComfyUI не запущен).</p>
          <div className="settings-actions">
            <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void install('image')}>
              <Icons.download />{busy === 'image-install' ? 'Устанавливаем…' : 'Установить / починить'}
            </button>
            <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void checkAll()}>
              <Icons.refresh />{busy === 'check' ? 'Проверяем…' : 'Проверить всё'}
            </button>
          </div>
        </div>
      )}
    </section>

    <form className="settings-section" onSubmit={saveText}>
      <span className="eyebrow">Текст</span>
      <h2>Модель мастера</h2>
      <label>Режим
        <select value={textMode} onChange={(event) => changeTextMode(event.target.value as 'local' | 'cloud' | 'chatgpt')}>
          <option value="local">Локально — Ollama</option>
          <option value="chatgpt">ChatGPT plan — без API key</option>
          <option value="cloud">Облачно — OpenAI-compatible API</option>
        </select>
      </label>
      {textMode === 'cloud' && <label>Base URL<input value={textBaseUrl} onChange={(e) => setTextBaseUrl(e.target.value)} /></label>}
      {textMode === 'chatgpt' ? (
        <label>Модель
          <select value={textModel} onChange={(e) => setTextModel(e.target.value)} disabled={!chatGPTModels.length}>
            {!chatGPTModels.length && <option value="">Сначала подключите ChatGPT</option>}
            {chatGPTModels.map((item) => <option key={item.slug} value={item.slug}>{item.display_name}</option>)}
          </select>
        </label>
      ) : (
        <label>Модель<input value={textModel} onChange={(e) => setTextModel(e.target.value)} placeholder={textMode === 'local' ? 'gemma4:e4b' : 'gpt-4.1-mini'} /></label>
      )}
      <label>Контекст<input type="number" min={1024} step={1024} value={textContext} onChange={(e) => setTextContext(Number(e.target.value))} /></label>
      {textMode === 'cloud' && <label>API key<input type="password" value={textKey} onChange={(e) => setTextKey(e.target.value)} placeholder={profile.text.has_api_key ? '•••••••• (пусто — оставить текущий)' : 'обязательно'} /></label>}
      {textMode === 'chatgpt' && (
        <div className="models-runtime-warning">
          {profile.text.chatgpt.connected ? (
            <>
              <p>
                Подключён ChatGPT{profile.text.chatgpt.email ? ` — ${profile.text.chatgpt.email}` : ''}.
                Запросы используют allowance вашего плана, API key не нужен.
              </p>
              <div className="settings-actions">
                <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void connectChatGPT()}>
                  {busy === 'chatgpt-connect' ? 'Открываем…' : 'Переподключить'}
                </button>
                <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void disconnectChatGPT()}>
                  {busy === 'chatgpt-disconnect' ? 'Отключаем…' : 'Отключить'}
                </button>
              </div>
            </>
          ) : (
            <>
              <p>Войдите в ChatGPT один раз. PersonalDM сохранит OAuth-сессию локально и не будет просить API key.</p>
              <button className="btn primary" type="button" disabled={Boolean(busy)} onClick={() => void connectChatGPT()}>
                {busy === 'chatgpt-connect' ? 'Открываем ChatGPT…' : 'Продолжить с ChatGPT'}
              </button>
            </>
          )}
        </div>
      )}
      {status(
        textMode === 'chatgpt' ? profile.text.chatgpt.connected : profile.text.status.ready,
        textMode === 'chatgpt'
          ? (profile.text.chatgpt.connected ? 'ChatGPT подключён' : 'ChatGPT не подключён')
          : profile.text.status.message,
      )}
      <div className="settings-actions">
        <button
          className="btn primary"
          disabled={Boolean(busy) || (textMode === 'chatgpt' && (!profile.text.chatgpt.connected || !textModel))}
        >Сохранить</button>
        {textMode === 'local' && !profile.text.status.ready && <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void install('text')}><Icons.download />{busy === 'text-install' ? 'Устанавливаем…' : 'Установить / починить'}</button>}
      </div>
    </form>

    <form className="settings-section" onSubmit={saveImage}>
      <span className="eyebrow">Иллюстрации</span>
      <h2>Графическая модель</h2>
      <label>Режим
        <select value={imageMode} onChange={(event) => changeImageMode(event.target.value as 'local' | 'cloud' | 'off')}>
          <option value="local">Локально — ComfyUI + FLUX.2 Klein</option>
          <option value="cloud">Облачно — Images API</option>
          <option value="off">Не использовать генерацию</option>
        </select>
      </label>
      {imageMode === 'cloud' && <>
        <label>Base URL<input value={imageBaseUrl} onChange={(e) => setImageBaseUrl(e.target.value)} /></label>
        <label>Модель<input value={imageModel} onChange={(e) => setImageModel(e.target.value)} placeholder="gpt-image-2" /></label>
        <label>API key<input type="password" value={imageKey} onChange={(e) => setImageKey(e.target.value)} placeholder={profile.image.has_api_key ? '•••••••• (пусто — оставить текущий)' : 'обязательно'} /></label>
      </>}
      {status(imageMode === 'off' ? true : profile.image.status.ready, imageMode === 'off' ? 'Генерация выключена' : profile.image.status.message)}
      <p>{imageMode === 'off' ? 'Новые обложки, портреты и сцены не генерируются. Уже созданные изображения остаются в галерее.' : 'Обложки, портреты и сцены используют выбранный provider.'}</p>
      <div className="settings-actions">
        <button className="btn primary" disabled={Boolean(busy)}>Сохранить</button>
        {imageMode === 'local' && !profile.image.status.ready && <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void install('image')}><Icons.download />{busy === 'image-install' ? 'Устанавливаем…' : 'Установить / починить'}</button>}
      </div>
    </form>

    <section className="settings-section">
      <span className="eyebrow">Диагностика</span>
      <h2>Runtime</h2>
      <p>Проверяет выбранные текстовый и графический provider без переустановки.</p>
      <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void checkAll()}><Icons.refresh />{busy === 'check' ? 'Проверяем…' : 'Проверить всё'}</button>
    </section>
  </>
}
