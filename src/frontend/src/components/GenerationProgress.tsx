import { useEffect, useState } from 'react'
import type { GenerationRun } from '../api/turnRuntime'

function elapsed(raw: string | null | undefined, now: number): string {
  if (!raw) return '—'
  const value = /(?:Z|[+-]\d\d:\d\d)$/.test(raw) ? raw : `${raw}Z`
  const seconds = Math.max(0, Math.floor((now - Date.parse(value)) / 1000))
  if (!Number.isFinite(seconds)) return '—'
  return seconds < 60 ? `${seconds} с` : `${Math.floor(seconds / 60)} мин ${seconds % 60} с`
}

function stageLabel(stage: string | undefined, phase: string | null | undefined): string {
  if (stage?.startsWith('queued:')) return 'Ожидает свободную модель'
  if (stage === 'narration') return 'Пишет продолжение'
  if (stage?.includes('Validation') || stage?.includes('validator')) return 'Проверяет продолжение'
  if (stage?.startsWith('structured:') || stage?.startsWith('planner:')) return 'Определяет результат действий'
  if (phase === 'narrated') return 'Сохраняет продолжение'
  return 'Готовит ход'
}

export function GenerationProgress({ generation }: { generation: GenerationRun }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [generation.id])
  const progress = generation.progress
  return <div role="status">
    <div className="turn-thinking"><span /><span /><span /><em>{stageLabel(progress?.stage, generation.phase)}…</em></div>
    <p>Ход: {elapsed(generation.created_at, now)}{progress?.stage_started_at
      ? ` · текущий этап: ${elapsed(progress.stage_started_at, now)}` : ''}</p>
    {progress?.last_activity_at && <p>Последний ответ модели: {elapsed(progress.last_activity_at, now)} назад{progress.generated_chunks > 0
      ? ` · получено фрагментов: ${progress.generated_chunks}` : ''}</p>}
  </div>
}
