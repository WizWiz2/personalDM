import { useEffect, useMemo, useState } from 'react'
import { api, readableError } from '../api/client'
import type { Scene, Turn } from '../api/types'
import { useCampaignWorkspace } from '../components/CampaignWorkspace'
import { ErrorState, LoadingState } from '../components/States'

const fmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })

export function ChroniclePage() {
  const { campaign } = useCampaignWorkspace()
  const [scenes, setScenes] = useState<Scene[]>([])
  const [turns, setTurns] = useState<Turn[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    Promise.all([api.listScenes(campaign.id), api.listTurns(campaign.id, 250, 'narrative')])
      .then(([s, t]) => { if (active) { setScenes(s); setTurns(t) } })
      .catch((err) => active && setError(readableError(err)))
      .finally(() => active && setLoading(false))
    return () => { active = false }
  }, [campaign.id])

  const entries = useMemo(() => [...scenes].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((scene) => ({ scene, count: turns.filter((turn) => turn.scene_id === scene.id).length })), [scenes, turns])

  return <div className="workspace-page">
    <header className="workspace-topbar"><div><h1>Хроника</h1><p>История кампании</p></div></header>
    <div className="page-content chronicle-content">
      {loading && <LoadingState label="Читаем хронику…" />}
      {error && <ErrorState message={error} />}
      {!loading && !error && entries.length === 0 && <p className="muted-note">Сцены появятся здесь после начала игры.</p>}
      {entries.map(({ scene, count }) => <article className="chronicle-entry" key={scene.id}><time>{fmt.format(new Date(scene.created_at))}</time><div><div className="chronicle-title-row"><h2>{scene.title}</h2><span className={`status-pill ${scene.status}`}>{scene.status === 'active' ? 'текущая' : scene.status}</span></div><p>{scene.location_description || [scene.mood, scene.tension].filter(Boolean).join(' · ') || 'Описание сцены не задано.'}</p><small>{count} {count === 1 ? 'ход' : 'ходов'}</small></div></article>)}
    </div>
  </div>
}
