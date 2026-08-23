import { useEffect, useState } from 'react'
import { VisualLightbox } from './VisualLightbox'

interface VisualGeneratedDetail {
  url: string
  kind?: string
}

export function VisualOverlayHost() {
  const [visual, setVisual] = useState<VisualGeneratedDetail | null>(null)

  useEffect(() => {
    const onGenerated = (event: Event) => {
      const detail = (event as CustomEvent<VisualGeneratedDetail>).detail
      if (detail?.url) setVisual(detail)
    }
    window.addEventListener('personaldm:visual-generated', onGenerated)
    return () => window.removeEventListener('personaldm:visual-generated', onGenerated)
  }, [])

  if (!visual) return null
  const title = visual.kind === 'scene_illustration'
    ? 'Новая иллюстрация сцены'
    : 'Новая иллюстрация'

  return <VisualLightbox src={visual.url} title={title} subtitle="Сохранено в галерею кампании" onClose={() => setVisual(null)} />
}
