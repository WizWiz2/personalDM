import { useEffect, useState } from 'react'
import type { GenerationRun, GenerationTrace, GenerationTraceEntry } from '../api/turnRuntime'
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
  if (/scene development|agenda source|unpublished npc|npc initiative/.test(error)) {
    return 'Служебный этап развития сцены не смог безопасно зафиксировать инициативу NPC. Ход сохранён как необработанный.'
  }
  const raw = (generation.error ?? '').trim()
  if (raw) {
    const short = raw.length > 220 ? `${raw.slice(0, 220)}…` : raw
    return `Игровой pipeline не смог завершить этот ход. Техническая причина: ${short}`
  }
  return 'Игровой pipeline не смог завершить этот ход. Ход сохранён как необработанный и не считается выполненным.'
}

type Decision = Extract<GenerationTraceEntry, { kind: 'decision' }>

const CHECK_LABELS: Record<string, string> = {
  narration_validator: 'валидатор',
  evaluator: 'арбитр',
}

/** Latest saga attempt of the run: a resumed run keeps its id, so start after the previous final. */
function failureTrace(trace: GenerationTrace | null) {
  const decisions = (trace?.timeline ?? []).filter(
    (entry): entry is Decision => entry.kind === 'decision',
  )
  const finals = decisions.flatMap((entry, index) => (entry.step === 'final' ? [index] : []))
  const current = decisions.slice(finals.length > 1 ? finals[finals.length - 2] + 1 : 0)
  const attempts: Decision[][] = []
  for (const entry of current) {
    if (entry.step === 'validate') attempts.push([])
    if ((entry.step === 'validate' || entry.step === 'evaluate') && attempts.length) {
      attempts[attempts.length - 1].push(entry)
    }
  }
  return {
    reason: current.find((entry) => entry.step === 'final')?.payload.reason ?? null,
    attempts: attempts.filter((checks) => checks.some((check) => check.payload.violations?.length)),
  }
}

export function GenerationFailurePanel({
  generation,
  trace = null,
}: {
  generation: GenerationRun
  trace?: GenerationTrace | null
}) {
  const [showTechnical, setShowTechnical] = useState(false)
  const { reason, attempts } = failureTrace(trace)

  useEffect(() => {
    setShowTechnical(false)
  }, [generation.id])

  const technicalRaw = generation.error?.trim()
    || (generation.status === 'cancelled'
      ? 'Generation was cancelled before completion.'
      : 'Backend did not persist a technical error for this failed generation.')
  const technical = /cancellation requested/i.test(technicalRaw)
    ? 'Остановку запросил пользователь.'
    : /generation was cancelled before completion/i.test(technicalRaw)
      ? 'Генерация остановлена до завершения.'
      : technicalRaw

  return <div className="generation-failure-panel" role="alert">
    <strong>{generation.status === 'cancelled' ? 'Обработка хода остановлена.' : 'Мастер не смог обработать ход.'}</strong>
    <span>{failureSummary(generation)}</span>
    {reason && <span className="generation-failure-reason">Итог: {reason}</span>}
    {attempts.length > 0 && <details className="generation-failure-checks">
      <summary>Нарушения проверок · попыток: {attempts.length}</summary>
      <ol>
        {attempts.map((checks, attempt) => <li key={attempt}>
          {checks.map((check, index) => <div key={index}>
            <em>{CHECK_LABELS[check.role ?? ''] ?? check.role}: {check.outcome}</em>
            <ul>
              {(check.payload.violations ?? []).map((violation, item) => <li key={item}>
                <code>{violation.code}</code> {violation.evidence}
              </li>)}
            </ul>
          </div>)}
        </li>)}
      </ol>
    </details>}
    <div className="generation-failure-tech">
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
