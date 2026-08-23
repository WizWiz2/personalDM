import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { NavLink, Outlet, useNavigate, useParams } from 'react-router-dom'
import { api, readableError } from '../api/client'
import { getLatestGeneration, type GenerationRun } from '../api/turnRuntime'
import type { Campaign } from '../api/types'
import { Icons } from './Icons'
import { ErrorState, LoadingState } from './States'

type CampaignContextValue = {
  campaign: Campaign
  refreshCampaign: () => Promise<Campaign>
  generation: GenerationRun | null
  refreshGeneration: () => Promise<GenerationRun | null>
  trackGeneration: (generation: GenerationRun | null) => void
}

const CampaignContext = createContext<CampaignContextValue | null>(null)

export function useCampaignWorkspace() {
  const value = useContext(CampaignContext)
  if (!value) throw new Error('Campaign workspace context is unavailable')
  return value
}

const nav = [
  { to: 'play', label: 'Игра', icon: Icons.book },
  { to: 'hero', label: 'Герой', icon: Icons.hero },
  { to: 'world', label: 'Мир и знания', icon: Icons.world },
  { to: 'chronicle', label: 'Хроника', icon: Icons.chronicle },
  { to: 'gallery', label: 'Галерея', icon: Icons.gallery },
  { to: 'settings', label: 'Кампания', icon: Icons.settings },
]

export function CampaignWorkspace() {
  const { campaignId = '' } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [generation, setGeneration] = useState<GenerationRun | null>(null)
  const [error, setError] = useState('')
  const [pinned, setPinned] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  const refreshCampaign = async () => {
    if (!campaignId) throw new Error('Campaign id is missing')
    const fresh = await api.getCampaign(campaignId)
    setCampaign(fresh)
    return fresh
  }

  const refreshGeneration = async () => {
    if (!campaignId) return null
    const fresh = await getLatestGeneration(campaignId)
    setGeneration(fresh)
    return fresh
  }

  useEffect(() => {
    let active = true
    Promise.all([
      api.getCampaign(campaignId),
      getLatestGeneration(campaignId).catch(() => null),
    ])
      .then(([campaignData, generationData]) => {
        if (!active) return
        setCampaign(campaignData)
        setGeneration(generationData)
      })
      .catch((err) => active && setError(readableError(err)))
    return () => { active = false }
  }, [campaignId])

  useEffect(() => {
    if (generation?.status !== 'running') return
    let active = true
    const poll = async () => {
      try {
        const fresh = await getLatestGeneration(campaignId)
        if (!active) return
        setGeneration(fresh)
        if (fresh && fresh.status !== 'running') {
          await refreshCampaign().catch(() => undefined)
        }
      } catch {
        // Runtime status is supplementary. A temporary polling failure must not tear
        // down the campaign workspace while the backend may still be generating.
      }
    }
    const timer = window.setInterval(() => { void poll() }, 900)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [campaignId, generation?.id, generation?.status])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const value = useMemo(() => campaign ? {
    campaign,
    refreshCampaign,
    generation,
    refreshGeneration,
    trackGeneration: setGeneration,
  } : null, [campaign, generation])

  if (error) return <div className="global-page"><ErrorState message={error} /></div>
  if (!campaign || !value) return <div className="global-page"><LoadingState label="Открываем кампанию…" /></div>

  const masterBusy = generation?.status === 'running'

  return (
    <CampaignContext.Provider value={value}>
      <div className="workspace-shell">
        <button
          className="mobile-workspace-menu"
          onClick={() => setMobileOpen((v) => !v)}
          aria-label="Открыть меню кампании"
        >
          <Icons.menu />
        </button>

        {mobileOpen && <button className="mobile-drawer-scrim" aria-label="Закрыть меню" onClick={() => setMobileOpen(false)} />}

        <aside className={`campaign-sidebar ${pinned ? 'pinned' : ''} ${mobileOpen ? 'mobile-open' : ''}`}>
          <button className="rail-control" onClick={() => setPinned((v) => !v)} title={pinned ? 'Открепить меню' : 'Закрепить меню'}>
            <span className="rail-icon"><Icons.menu /></span>
            <span className="nav-label">Навигация</span>
          </button>

          <button className="workspace-back" onClick={() => navigate('/campaigns')}>
            <span className="rail-icon"><Icons.back /></span>
            <span className="nav-label">К кампаниям</span>
          </button>

          <div className="workspace-context">
            <span>Кампания</span>
            <strong>{campaign.name}</strong>
          </div>

          <nav className="workspace-nav">
            {nav.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                onClick={() => setMobileOpen(false)}
                title={to === 'play' && masterBusy ? 'Мастер продолжает обрабатывать ход' : label}
              >
                <span className="rail-icon"><Icon /></span>
                <span className="nav-label">{label}{to === 'play' && masterBusy ? ' · мастер думает' : ''}</span>
              </NavLink>
            ))}
          </nav>
        </aside>

        <main className="workspace-main">
          <Outlet />
        </main>
      </div>
    </CampaignContext.Provider>
  )
}
