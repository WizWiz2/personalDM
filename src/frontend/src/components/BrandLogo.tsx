import type { SVGProps } from 'react'

type BrandLogoProps = {
  compact?: boolean
  className?: string
}

type MarkProps = SVGProps<SVGSVGElement>

export function BrandMark({ className, ...props }: MarkProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 28 32"
      fill="none"
      shapeRendering="crispEdges"
      aria-hidden="true"
      {...props}
    >
      <rect x="11" y="1" width="6" height="2" className="brand-mark-metal" />
      <rect x="9" y="3" width="10" height="2" className="brand-mark-metal" />
      <rect x="6" y="6" width="16" height="2" className="brand-mark-metal" />
      <rect x="4" y="8" width="20" height="2" className="brand-mark-metal" />
      <rect x="3" y="10" width="3" height="15" className="brand-mark-metal" />
      <rect x="22" y="10" width="3" height="15" className="brand-mark-metal" />
      <rect x="6" y="24" width="16" height="3" className="brand-mark-metal" />
      <rect x="8" y="27" width="12" height="2" className="brand-mark-metal" />
      <rect x="7" y="11" width="14" height="12" className="brand-mark-glass" />
      <rect x="12" y="13" width="4" height="3" className="brand-mark-flame-soft" />
      <rect x="10" y="16" width="8" height="5" className="brand-mark-flame" />
      <rect x="12" y="14" width="4" height="7" className="brand-mark-flame-hot" />
      <rect x="13" y="12" width="2" height="3" className="brand-mark-flame-hot" />
    </svg>
  )
}

export function BrandLogo({ compact = false, className = '' }: BrandLogoProps) {
  return (
    <div className={`brand-logo ${compact ? 'compact' : ''} ${className}`.trim()} aria-label="PersonalDM">
      <BrandMark className="brand-logo-mark" />
      {!compact && <span className="brand-logo-wordmark">Personal<span>DM</span></span>}
    </div>
  )
}
