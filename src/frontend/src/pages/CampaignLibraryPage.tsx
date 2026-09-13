import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, readableError } from '../api/client'
import type { Campaign } from '../api/types'
import { visualApi, visualUrls } from '../api/visuals'
import { BrandLogo } from '../components/BrandLogo'
import { GeneratedPixelArt } from '../components/GeneratedPixelArt'
import { Icons } from '../components/Icons'
import { PixelScene } from '../components/PixelArt'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

const dateFmt = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' })

function relativeUpdated(value: string) {
  const then = new Date(value).getTime()
  const diff = Date.now() - then
  if (diff < 24 * 60 * 60 * 1000) return '???????'
  if (diff < 48 * 60 * 60 * 1000) return '?????'
  return dateFmt.format(new Date(value))
}

export function CampaignLibraryPage() {
  const navigate = useNavigate()
  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [coverReady, setCoverReady] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modal, setModal] = useState(false)
  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Campaign | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const nameInputRef = useRef<HTMLInputElement>(null)
  const createDialogRef = useRef<HTMLFormElement>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await api.listCampaigns()
      setCampaigns([...data].sort((a, b) => b.updated_at.localeCompare(a.updated_at)))
    } catch (err) {
      setError(readableError(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  useEffect(() => {
    if (!modal) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setModal(false)
    }
    window.addEventListener('keydown', onKey)
    window.setTimeout(() => nameInputRef.current?.focus(), 0)
    return () => window.removeEventListener('keydown', onKey)
  }, [modal])

  useEffect(() => {
    if (!deleteTarget) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDeleteTarget(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [deleteTarget])

  useEffect(() => {
    if (!modal || !createDialogRef.current) return
    const dialog = createDialogRef.current
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null || el === document.activeElement)
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    dialog.addEventListener('keydown', onKeyDown)
    return () => dialog.removeEventListener('keydown', onKeyDown)
  }, [modal])

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void load()
    }
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [])

  useEffect(() => {
    if (!menuOpenId) return
    const close = () => setMenuOpenId(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menuOpenId])

  const campaignIds = campaigns.map((campaign) => campaign.id).join('|')
  useEffect(() => {
    if (!campaigns.length) return
    let active = true
    let timer: number | undefined
    let attempt = 0

    const checkCovers = async () => {
      attempt += 1
      const results = await Promise.all(campaigns.map(async (campaign) => {
        try {
          return [campaign.id, await visualApi.getCampaignCover(campaign.id)] as const
        } catch {
          return [campaign.id, null] as const
        }
      }))
      if (!active) return

      let waiting = false
      const now = Date.now()
      setCoverReady((current) => {
        const next = { ...current }
        for (const [campaignId, cover] of results) {
          if (cover?.available) next[campaignId] = now
          else {
            delete next[campaignId]
            waiting = true
          }
        }
        return next
      })

      if (waiting && attempt < 45) {
        timer = window.setTimeout(() => { void checkCovers() }, 2000)
      }
    }

    void checkCovers()
    return () => {
      active = false
      if (timer) window.clearTimeout(timer)
    }
  }, [campaignIds])

  const recent = useMemo(() => campaigns.slice(0, 2), [campaigns])

  const coverSrc = (campaign: Campaign) => {
    const version = coverReady[campaign.id]
    if (!version) return null
    return `${visualUrls.campaignCover(campaign.id)}?v=${version}`
  }

  const renderCover = (campaign: Campaign, compact = false) => {
    const src = coverSrc(campaign)
    const fallback = <PixelScene seed={campaign.name} compact={compact} />
    if (!src) return fallback
    return <GeneratedPixelArt src={src} alt={`??????? ${campaign.name}`} fallback={fallback} active />
  }

  const openCampaign = async (campaign: Campaign) => {
    try {
      const setup = await api.getSessionZero(campaign.id)
      if (setup.status === 'completed') navigate(`/campaign/${campaign.id}/play`)
      else navigate(`/campaigns/${campaign.id}/session-zero`)
    } catch (err) {
      setError(readableError(err))
    }
  }

  const createCampaign = async (event: FormEvent) => {
    event.preventDefault()
    if (!name.trim()) return
    setCreating(true)
    try {
      const campaign = await api.createCampaign({
        name: name.trim(),
        description: description.trim() || null,
      })
      setModal(false)
      navigate(`/campaigns/${campaign.id}/session-zero`)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setCreating(false)
    }
  }

  const confirmDelete = async () => {
    if (!deleteTarget) return
    const campaign = deleteTarget
    setDeletingId(campaign.id)
    setError('')
    try {
      await api.deleteCampaign(campaign.id)
      setCampaigns((current) => current.filter((item) => item.id !== campaign.id))
      setDeleteTarget(null)
    } catch (err) {
      setError(readableError(err))
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="global-page">
      <header className="global-topbar">
        <div className="global-brand-heading">
          <BrandLogo />
          <span className="brand-divider" aria-hidden="true" />
          <div>
            <h1>????????</h1>
            <p>???? ???????</p>
          </div>
        </div>
        <div className="session-zero-top-actions">
          <button className="btn" onClick={() => navigate('/settings')}><Icons.settings />???????????</button>
          <button className="btn primary" onClick={() => setModal(true)}><Icons.plus />????? ????????</button>
        </div>
      </header>

      <div className="global-content campaign-library">
        {loading && <LoadingState label="????????? ?????????" />}
        {error && <ErrorState message={error} action={<button className="btn" onClick={() => void load()}>?????????</button>} />}
        {!loading && !error && campaigns.length === 0 && (
          <EmptyState title="???? ??? ????????" text="?????? ?????? ??????? ? ????? ????? ???????? ??????? ??????." action={<button className="btn primary" onClick={() => setModal(true)}>??????? ????????</button>} />
        )}

        {!loading && !error && campaigns.length > 0 && (
          <>
            <section className="recent-section">
              <h2>????????</h2>
              <div className="recent-list">
                {recent.map((campaign) => (
                  <button key={campaign.id} className="recent-campaign" onClick={() => void openCampaign(campaign)}>
                    <div className="recent-thumb">{renderCover(campaign, true)}</div>
                    <div className="recent-copy">
                      <strong>{campaign.name}</strong>
                      <span>{campaign.description || (campaign.current_scene_id ? '???????? ????????????' : '?????????? ????????')}</span>
                    </div>
                    <span className="recent-time">{relativeUpdated(campaign.updated_at)}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className="all-campaigns-section">
              <h2>??? ????????</h2>
              <div className="campaign-grid">
                {campaigns.map((campaign) => (
                  <article className="campaign-card" key={campaign.id}>
                    <div className="campaign-cover">{renderCover(campaign)}</div>
                    <div className="campaign-card-body">
                      <span className="eyebrow">{campaign.current_scene_id ? '????????' : '??????????'}</span>
                      <h3>{campaign.name}</h3>
                      <p>{campaign.description || '???????? ???????? ???? ?? ??????.'}</p>
                      <div className="campaign-card-state">
                        <span>{campaign.player_character_id ? '????? ??????' : '????? ?????'}</span>
                        <span>?</span>
                        <span>{relativeUpdated(campaign.updated_at)}</span>
                      </div>
                      <div className="campaign-card-footer">
                        <div className="campaign-card-menu">
                          <button
                            type="button"
                            className="icon-btn campaign-more-btn"
                            aria-label="??? ????????"
                            aria-expanded={menuOpenId === campaign.id}
                            onClick={(event) => {
                              event.stopPropagation()
                              setMenuOpenId((id) => id === campaign.id ? null : campaign.id)
                            }}
                          >
                            ?
                          </button>
                          {menuOpenId === campaign.id && (
                            <div className="campaign-menu-popover" role="menu" onClick={(e) => e.stopPropagation()}>
                              <button
                                type="button"
                                className="campaign-menu-danger"
                                role="menuitem"
                                disabled={deletingId === campaign.id}
                                onClick={() => {
                                  setMenuOpenId(null)
                                  setDeleteTarget(campaign)
                                }}
                              >
                                ??????? ?????????
                              </button>
                            </div>
                          )}
                        </div>
                        <button className="btn primary" onClick={() => void openCampaign(campaign)}>??????????</button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          </>
        )}
      </div>

      {modal && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && setModal(false)}>
          <form ref={createDialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="create-campaign-title" onSubmit={createCampaign}>
            <div className="modal-head">
              <div><span className="eyebrow">????? ???????</span><h2 id="create-campaign-title">??????? ????????</h2></div>
              <button type="button" className="icon-btn" onClick={() => setModal(false)} aria-label="???????"><Icons.close /></button>
            </div>
            <label>????????<input ref={nameInputRef} value={name} onChange={(e) => setName(e.target.value)} placeholder="????????: ???? ????????? ??????" /></label>
            <label>???????? ????????<textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="? ??? ??? ????????" rows={4} /></label>
            <div className="modal-actions"><button type="button" className="btn" onClick={() => setModal(false)}>??????</button><button className="btn primary" disabled={creating || !name.trim()}>{creating ? '????????' : '???????'}</button></div>
          </form>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && setDeleteTarget(null)}>
          <div className="modal danger-modal" role="dialog" aria-modal="true" aria-labelledby="delete-campaign-title">
            <div className="modal-head">
              <div>
                <span className="eyebrow">??????????</span>
                <h2 id="delete-campaign-title">??????? ?{deleteTarget.name}??</h2>
              </div>
              <button type="button" className="icon-btn" onClick={() => setDeleteTarget(null)} aria-label="???????"><Icons.close /></button>
            </div>
            <p className="modal-body-copy">??? ?????, ????, ????? ? ??????????? ???? ???????? ????? ??????? ??? ??????????? ??????????????.</p>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setDeleteTarget(null)}>??????</button>
              <button
                type="button"
                className="btn danger"
                disabled={deletingId === deleteTarget.id}
                onClick={() => void confirmDelete()}
              >
                {deletingId === deleteTarget.id ? '????????' : '??????? ????????'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
