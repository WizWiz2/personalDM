import { useEffect, useMemo, useState } from 'react'
import { readableError } from '../api/client'
import { type GalleryAsset, visualApi } from '../api/visuals'
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

export function GalleryPage() {
  const { campaign } = useCampaignWorkspace()
  const [items, setItems] = useState<GalleryAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<GalleryAsset | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setItems(await visualApi.gallery(campaign.id))
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

  const renderCard = (asset: GalleryAsset) => (
    <button className="gallery-card" key={asset.id} onClick={() => setSelected(asset)}>
      <div className="gallery-thumb"><img src={asset.url} alt={kindLabels[asset.kind] || 'Иллюстрация'} /></div>
      <div className="gallery-card-copy">
        <strong>{kindLabels[asset.kind] || 'Иллюстрация'}</strong>
        <span>{dateFmt.format(new Date(asset.created_at))}</span>
      </div>
    </button>
  )

  return (
    <div className="workspace-page">
      <header className="workspace-topbar">
        <div><h1>Галерея</h1><p>Все сохранённые иллюстрации кампании</p></div>
      </header>
      <div className="page-content gallery-content">
        {loading && <LoadingState label="Открываем галерею…" />}
        {error && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>Повторить</button>} />}
        {!loading && !error && items.length === 0 && (
          <EmptyState title="Галерея пока пуста" text="Обложка появится после нулевой сессии, а сцены — после кнопки «Сгенерировать сцену»." />
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
          title={kindLabels[selected.kind] || 'Иллюстрация'}
          subtitle={dateFmt.format(new Date(selected.created_at))}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}
