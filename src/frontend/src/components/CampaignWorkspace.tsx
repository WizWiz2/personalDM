import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { NavLink, Outlet, useNavigate, useParams } from 'react-router-dom'
import { api, readableError } from '../api/client'
import type { Campaign } from '../api/types'
import { Icons } from './Icons'
import { ErrorState, LoadingState } from './States'

type CampaignContextValue = {
  campaign: Campaign
  refreshCampaign: () => Promise<Campaign>
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
  { to: 'settings', label: 'Кампания', icon: Icons.settings },
]

export function CampaignWorkspace() {
  const { campaignId = '' } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [error, setError] = useState('')
  const [pinned, setPinned] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  const refreshCampaign = async () => {
    if (!campaignId) throw new Error('Campaign id is missing')
    const fresh = await api.getCampaign(campaignId)
    setCampaign(fresh)
    return fresh
  }

  useEffect(() => {
    let active = true
    api.getCampaign(campaignId)
      .then((data) => active && setCampaign(data))
      .catch((err) => active && setError(readableError(err)))
    return () => { active = false }
  }, [campaignId])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const value = useMemo(() => campaign ? { campaign, refreshCampaign } : null, [campaign])

  if (error) return <div className="global-page"><ErrorState message={error} /></div>
  if (!campaign || !value) return <div className="global-page"><LoadingState label="Открываем кампанию…" /></div>

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
              >
                <span className="rail-icon"><Icon /></span>
                <span className="nav-label">{label}</span>
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
