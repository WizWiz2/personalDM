import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import { CampaignWorkspace } from './components/CampaignWorkspace'
import { CampaignLibraryPage } from './pages/CampaignLibraryPage'
import { CampaignSettingsPage } from './pages/CampaignSettingsPage'
import { ChroniclePage } from './pages/ChroniclePage'
import { GalleryPage } from './pages/GalleryPage'
import { HeroPage } from './pages/HeroPage'
import { PlayPage } from './pages/PlayPage'
import { SessionZeroPage } from './pages/SessionZeroPage'
import { WorldPage } from './pages/WorldPage'

export function App() {
  return <HashRouter><Routes>
    <Route path="/" element={<Navigate to="/campaigns" replace />} />
    <Route path="/campaigns" element={<CampaignLibraryPage />} />
    <Route path="/campaigns/:campaignId/session-zero" element={<SessionZeroPage />} />
    <Route path="/campaign/:campaignId" element={<CampaignWorkspace />}>
      <Route index element={<Navigate to="play" replace />} />
      <Route path="play" element={<PlayPage />} />
      <Route path="hero" element={<HeroPage />} />
      <Route path="world" element={<WorldPage />} />
      <Route path="chronicle" element={<ChroniclePage />} />
      <Route path="gallery" element={<GalleryPage />} />
      <Route path="settings" element={<CampaignSettingsPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/campaigns" replace />} />
  </Routes></HashRouter>
}
