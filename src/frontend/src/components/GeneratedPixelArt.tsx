import { ReactNode, useEffect, useState } from 'react'

interface GeneratedPixelArtProps {
  src: string
  alt: string
  fallback: ReactNode
  className?: string
}

export function GeneratedPixelArt({
  src,
  alt,
  fallback,
  className = '',
}: GeneratedPixelArtProps) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setFailed(false)
  }, [src])

  if (failed) return <>{fallback}</>

  return <img
    className={`generated-pixel-art ${className}`.trim()}
    src={src}
    alt={alt}
    style={{
      width: '100%',
      height: '100%',
      objectFit: 'cover',
      display: 'block',
      imageRendering: 'pixelated',
    }}
    onError={() => setFailed(true)}
  />
}
