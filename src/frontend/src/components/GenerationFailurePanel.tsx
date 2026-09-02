import { useEffect, useState } from 'react'
import type { GenerationRun } from '../api/turnRuntime'
import { Icons } from './Icons'

function failureSummary(generation: GenerationRun): string {
  if (generation.status === 'cancelled') {
    return 'Обработка была остановлена. Ход сохранён как необработанный и не считается выполненным.'
  }

  const error = (generation.error ?? '').toLocaleLowerCase('ru-RU')
  if (!error) {
    return 'Игровой pipeline не смог завершить этот ход. Ход сохранён как необработанный и не считается выполненным.'
  }
  if (/\b429\b|rate.?limit|quota|too many requests|лимит|квот/.test(error)) {
    return 'Провайдер отклонил один из запросов из-за лимита или квоты. Ход сохранён и не считается выполненным.'
  }
  if (/timeout|timed out|exceeded .*budget|request budget|превыс.*врем/.test(error)) {
    return 'Один из этапов обработки превысил допустимое время. Ход сохранён и не считается выполненным.'
  }
  if (/model.*not found|unknown model|does not exist|model_not_found|\b404\b.*model/.test(error)) {
    return 'Провайдер не нашёл модель, выбранную для одного из этапов обработки. Ход сохранён и не считается выполненным.'
  }
  if (/json|schema|structured|validation error|parse/.test(error)) {
    return 'Один из служебных этапов не получил корректный структурированный ответ от модели. Ход сохранён и не считается выполненным.'
  }
  if (/narrationpublication|turnauthority|authority.*outcome|conservative authority/.test(error)) {
    return 'Защита целостности остановила ход, чтобы не публиковать неподтверждённый результат.'
  }
  if (/provider|http \d{3}|api request|connection|connect/.test(error)) {
    return 'Один из запросов к модели завершился ошибкой провайдера. Ход сохранён и не считается выполненным.'
  }
  return 'Игровой pipeline не смог завершить этот ход. Ход сохранён как необработанный и не считается выполненным.'
}

export function GenerationFailurePanel({ generation }: { generation: GenerationRun }) {
  const [showTechnical, setShowTechnical] = useState(false)

  useEffect(() => {
    setShowTechnical(false)
  }, [generation.id])

  const technical = generation.error?.trim()
    || (generation.status === 'cancelled'
      ? 'Generation was cancelled before completion.'
      : 'Backend did not persist a technical error for this failed generation.')

  return <div className="session-zero-inline-error generation-failure-panel" role="alert">
    <strong>{generation.status === 'cancelled' ? 'Обработка хода остановлена.' : 'Мастер не смог обработать ход.'}</strong>
    <span>{failureSummary(generation)}</span>
    <div className="session-zero-error-actions">
      <button
        className="btn ghost"
        type="button"
        aria-expanded={showTechnical}
        onClick={() => setShowTechnical((value) => !value)}
      >
        <Icons.settings />{showTechnical ? 'Скрыть причину' : 'Техническая причина'}
      </button>
    </div>
    {showTechnical && <pre className="generation-technical-error">{technical}</pre>}
  </div>
}
