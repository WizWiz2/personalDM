import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { readableError } from '../api/client'
import { type GalleryAsset, type VisualStatus, visualApi } from '../api/visuals'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { VisualLightbox } from '../components/VisualLightbox'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

const kindLabels: Record<string, string> = {
  campaign_cover: 'Обложка кампании',
  character_portrait: 'Портрет персонажа',
  scene_illustration: 'Сцена',
}

const dateFmt = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

function assetTitle(asset: GalleryAsset, campaignName: string): string {
  const meta = asset.metadata || {}
  const name = typeof meta.character_name === 'string' ? meta.character_name.trim()
    : typeof meta.name === 'string' ? meta.name.trim()
    : typeof meta.title === 'string' ? meta.title.trim()
    : typeof meta.scene_title === 'string' ? meta.scene_title.trim()
    : ''
  if (asset.kind === 'character_portrait' && name) return name
  if (asset.kind === 'scene_illustration' && name) return name
  if (asset.kind === 'campaign_cover') return `Обложка: ${campaignName}`
  return kindLabels[asset.kind] || 'Иллюстрация'
}

export function GalleryPage() {
  const { campaign } = useCampaignWorkspace()
  const [items, setItems] = useState<GalleryAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<GalleryAsset | null>(null)
  const [visualStatus, setVisualStatus] = useState<VisualStatus | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [gallery, status] = await Promise.all([
        visualApi.gallery(campaign.id),
        visualApi.status().catch(() => null),
      ])
      setItems(gallery)
      setVisualStatus(status)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [campaign.id])

  const grouped = useMemo(() => ({
    scenes: items.filter((item) => item.kind === 'scene_illustration'),
    other: items.filter((item) => item.kind !== 'scene_illustration'),
  }), [items])

  const runtimeReady = Boolean(visualStatus?.enabled && visualStatus?.connected)
  const runtimeHint = !visualStatus
    ? null
    : !visualStatus.enabled
      ? 'Генерация изображений выключена в настройках моделей.'
      : !visualStatus.connected
        ? 'Image runtime сейчас недоступен — генерация не запустится, пока provider не поднимется.'
        : null

  const renderCard = (asset: GalleryAsset) => {
    const title = assetTitle(asset, campaign.name)
    return (
      <button className="gallery-card" key={asset.id} onClick={() => setSelected(asset)}>
        <div className="gallery-thumb"><img src={asset.url} alt={title} /></div>
        <div className="gallery-card-copy">
          <strong>{title}</strong>
          <span>{dateFmt.format(new Date(asset.created_at))}</span>
        </div>
      </button>
    )
  }

  return (
    <div className="workspace-page">
      <header className="workspace-topbar">
        <div><h1>Галерея</h1><p>Все сохранённые иллюстрации кампании</p></div>
      </header>
      <div className="page-content gallery-content">
        {loading && <LoadingState label="Открываем галерею…" />}
        {error && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
        {!loading && !error && items.length === 0 && (
          <EmptyState
            title="Галерея пока пуста"
            text={
              runtimeHint
                ? `${runtimeHint} Обложка, портреты и сцены появятся после успешной генерации.`
                : 'Обложка, портреты и сцены появятся после успешной генерации изображений, когда image runtime доступен. Сцену можно запросить кнопкой «Сгенерировать сцену» на экране Игра.'
            }
            action={
              !runtimeReady ? (
                <Link className="btn primary" to={`/campaign/${campaign.id}/settings`}>Открыть настройки моделей</Link>
              ) : undefined
            }
          />
        )}
        {!loading && !error && grouped.other.length > 0 && (
          <section className="gallery-section">
            <h2>Кампания и персонажи</h2>
            <div className="gallery-grid">{grouped.other.map(renderCard)}</div>
          </section>
        )}
        {!loading && !error && grouped.scenes.length > 0 && (
          <section className="gallery-section">
            <h2>Сцены</h2>
            <div className="gallery-grid">{grouped.scenes.map(renderCard)}</div>
          </section>
        )}
      </div>
      {selected && (
        <VisualLightbox
          src={selected.url}
          title={assetTitle(selected, campaign.name)}
          subtitle={dateFmt.format(new Date(selected.created_at))}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}
