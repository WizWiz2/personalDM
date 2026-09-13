import { ReactNode, useEffect, useState } from 'react'

interface GeneratedPixelArtProps {
  src: string
  alt: string
  fallback: ReactNode
  className?: string
  /** When false, never request src ? show fallback immediately (avoids 404 flash). */
  active?: boolean
  retryOnError?: boolean
  retryIntervalMs?: number
  maxRetries?: number
}

function withRetryToken(src: string, attempt: number): string {
  if (attempt <= 0) return src
  const separator = src.includes('?') ? '&' : '?'
  return `${src}${separator}pdm_retry=${attempt}`
}

export function GeneratedPixelArt({
  src,
  alt,
  fallback,
  className = '',
  active = true,
  retryOnError = false,
  retryIntervalMs = 5000,
  maxRetries = 6,
}: GeneratedPixelArtProps) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    setFailed(false)
    setAttempt(0)
  }, [src, active])

  useEffect(() => {
    if (!active || !failed || !retryOnError || attempt >= maxRetries) return
    const timer = window.setTimeout(() => {
      setAttempt((value) => value + 1)
      setFailed(false)
    }, retryIntervalMs)
    return () => window.clearTimeout(timer)
  }, [active, failed, retryOnError, attempt, maxRetries, retryIntervalMs])

  if (!active || failed) return <>{fallback}</>

  return <img
    className={`generated-pixel-art ${className}`.trim()}
    src={withRetryToken(src, attempt)}
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
