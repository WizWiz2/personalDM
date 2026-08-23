import { useEffect } from 'react'
import { Icons } from './Icons'

interface VisualLightboxProps {
  src: string
  title: string
  subtitle?: string
  onClose: () => void
}

export function VisualLightbox({ src, title, subtitle, onClose }: VisualLightboxProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="visual-lightbox" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="visual-lightbox-dialog" role="dialog" aria-modal="true" aria-label={title}>
        <button className="visual-lightbox-close" onClick={onClose} aria-label="Закрыть"><Icons.close /></button>
        <img src={src} alt={title} />
        <div className="visual-lightbox-caption">
          <strong>{title}</strong>
          {subtitle && <span>{subtitle}</span>}
        </div>
      </div>
    </div>
  )
}
