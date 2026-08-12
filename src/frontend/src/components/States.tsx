import type { ReactNode } from 'react'

export function LoadingState({ label = 'Загрузка…' }: { label?: string }) {
  return <div className="state-panel"><span className="spinner" />{label}</div>
}

export function ErrorState({ message, action }: { message: string; action?: ReactNode }) {
  return <div className="state-panel error-state"><strong>Не получилось загрузить данные</strong><span>{message}</span>{action}</div>
}

export function EmptyState({ title, text, action }: { title: string; text?: string; action?: ReactNode }) {
  return <div className="state-panel empty-state"><strong>{title}</strong>{text && <span>{text}</span>}{action}</div>
}
