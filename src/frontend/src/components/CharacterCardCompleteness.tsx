const fieldLabels: Record<string, string> = {
  description: 'описание', appearance: 'внешность', personality: 'характер',
  values: 'ценности', fears: 'страхи', desires: 'желания', voice: 'голос',
  speech_patterns: 'манера речи', background: 'прошлое', capabilities: 'умения',
  limitations: 'ограничения', goals: 'цели',
}

export function CharacterCardCompleteness({ missingFields }: { missingFields: string[] }) {
  const labels = missingFields.map((field) => fieldLabels[field]).filter(Boolean)
  if (!labels.length) return null
  return <p className="muted-note">
    Можно дополнить героя: {labels.join(', ')}. Эти детали помогут мастеру учитывать его особенности.
  </p>
}
