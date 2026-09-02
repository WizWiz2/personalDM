import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { Icons } from '../components/Icons'
import { RuntimeProviderSettings } from '../components/RuntimeProviderSettings'
import { ErrorState } from '../components/States'

export function GlobalSettingsPage() {
  const navigate = useNavigate()
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  return <div className="global-page">
    <header className="global-topbar">
      <div className="global-brand-heading">
        <BrandLogo />
        <span className="brand-divider" aria-hidden="true" />
        <div>
          <h1>Подключения</h1>
          <p>Модели и локальный runtime PersonalDM</p>
        </div>
      </div>
      <button className="btn" onClick={() => navigate('/campaigns')}>
        <Icons.back />К кампаниям
      </button>
    </header>

    <div className="global-content settings-content">
      {error && <ErrorState message={error} />}
      {message && <div className="success-note">{message}</div>}
      <div className="settings-grid">
        <RuntimeProviderSettings onMessage={setMessage} onError={setError} />
      </div>
    </div>
  </div>
}
