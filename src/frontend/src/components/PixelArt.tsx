import { useEffect, useRef } from 'react'

function hashSeed(value: string) {
  let h = 2166136261
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

function rng(seed: number) {
  let x = seed || 1
  return () => {
    x ^= x << 13
    x ^= x >>> 17
    x ^= x << 5
    return (x >>> 0) / 4294967296
  }
}

export function PixelScene({ seed, compact = false }: { seed: string; compact?: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const width = compact ? 96 : 180
    const height = compact ? 64 : 100
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.imageSmoothingEnabled = false
    const random = rng(hashSeed(seed))
    const palette = ['#0b1012', '#172326', '#263c3b', '#5f4932', '#b57b42', '#deb777']
    ctx.fillStyle = palette[0]
    ctx.fillRect(0, 0, width, height)
    ctx.fillStyle = palette[1]
    ctx.fillRect(0, Math.floor(height * 0.62), width, height)
    for (let i = 0; i < 24; i += 1) {
      ctx.fillStyle = random() > 0.6 ? palette[5] : palette[4]
      ctx.fillRect(Math.floor(random() * width), Math.floor(random() * height * 0.55), 1, 1)
    }
    const step = compact ? 12 : 20
    for (let x = -4; x < width + step; x += step) {
      const bh = Math.floor(height * (0.18 + random() * 0.34))
      ctx.fillStyle = palette[2]
      ctx.fillRect(x, Math.floor(height * 0.66) - bh, step - 3, bh)
      ctx.fillStyle = palette[4]
      for (let y = Math.floor(height * 0.66) - bh + 5; y < Math.floor(height * 0.62); y += 8) {
        if (random() > 0.35) ctx.fillRect(x + 4, y, 2, 3)
      }
    }
    ctx.fillStyle = '#10100d'
    ctx.fillRect(0, Math.floor(height * 0.82), width, Math.ceil(height * 0.18))
    ctx.fillStyle = '#6f5334'
    ctx.fillRect(0, Math.floor(height * 0.78), width, 2)
  }, [seed, compact])
  return <canvas ref={ref} className="pixel-canvas" aria-hidden="true" />
}

export function PixelPortrait({ seed }: { seed: string }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    canvas.width = 72
    canvas.height = 92
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.imageSmoothingEnabled = false
    const random = rng(hashSeed(seed))
    const coat = random() > 0.5 ? '#2a2c27' : '#34302b'
    const hair = random() > 0.5 ? '#4c352a' : '#26231f'
    const skin = random() > 0.5 ? '#b77d5c' : '#9f6e55'
    ctx.fillStyle = '#11120f'
    ctx.fillRect(0, 0, 72, 92)
    ctx.fillStyle = coat
    ctx.fillRect(11, 58, 50, 34)
    ctx.fillStyle = skin
    ctx.fillRect(23, 25, 28, 35)
    ctx.fillStyle = hair
    ctx.fillRect(18, 17, 38, 15)
    ctx.fillRect(18, 23, 7, 30)
    ctx.fillRect(50, 23, 7, 27)
    ctx.fillStyle = '#171713'
    ctx.fillRect(29, 37, 3, 2)
    ctx.fillRect(43, 37, 3, 2)
    ctx.fillRect(34, 48, 8, 2)
  }, [seed])
  return <canvas ref={ref} className="pixel-canvas" aria-hidden="true" />
}
