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
  // DB / schema / infra before loose json|schema matching (SQL may contain payload_json).
  if (/no such table|operationalerror|sqlite3\.|sqlalchemy|alembic|undefined table|relation .* does not exist|schema drift/.test(error)) {
    return 'Схема базы данных не совпадает с кодом (нет нужной таблицы или миграция не применена). Обнови схему через play.bat / alembic upgrade head и повтори ход.'
  }
  if (/\b429\b|rate.?limit|quota|too many requests|лимит|квот/.test(error)) {
    return 'Провайдер отклонил запрос из-за лимита или квоты. Ход сохранён и не считается выполненным.'
  }
  if (/timeout|timed out|exceeded .*budget|request budget|бюджет.*врем/.test(error)) {
    return 'Ход не уложился в лимит времени обработки модели. Ход сохранён и не считается выполненным.'
  }
  if (/model.*not found|unknown model|does not exist|model_not_found|\b404\b.*model/.test(error)) {
    return 'Провайдер не нашёл модель, указанную для одного из этапов мастера. Ход сохранён и не считается выполненным.'
  }
  if (/(json\s*decode|jsondecode|invalid json|schema validation|structured output|structured response|validation error|failed to parse|parse error)/.test(error)) {
    return 'Один из служебных этапов не получил корректный структурированный ответ от модели. Ход сохранён и не считается выполненным.'
  }
  if (/narrationpublication|turnauthority|authority.*outcome|conservative authority/.test(error)) {
    return 'Нарратор предложил изменения мира, которые не прошли проверку достоверности кампании.'
  }
  if (/provider|http \d{3}|api request|connection refused|connection reset|\bconnect\b/.test(error)) {
    return 'Сбой на соединении с моделью провайдера во время обработки. Ход сохранён и не считается выполненным.'
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
