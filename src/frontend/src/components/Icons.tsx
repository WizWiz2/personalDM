import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

function Base({ children, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

export const Icons = {
  menu: (p: IconProps) => <Base {...p}><path d="M4 7h16M4 12h16M4 17h16" /></Base>,
  back: (p: IconProps) => <Base {...p}><path d="M19 12H5" /><path d="m11 18-6-6 6-6" /></Base>,
  book: (p: IconProps) => <Base {...p}><path d="M4.5 5A2 2 0 0 1 6.5 3H11v17H6.5a2 2 0 0 0-2 2V5Z" /><path d="M19.5 5A2 2 0 0 0 17.5 3H13v17h4.5a2 2 0 0 1 2 2V5Z" /></Base>,
  hero: (p: IconProps) => <Base {...p}><circle cx="12" cy="8" r="3.5" /><path d="M5 21c.8-4.7 3.1-7 7-7s6.2 2.3 7 7" /></Base>,
  world: (p: IconProps) => <Base {...p}><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.2 2.3 3.5 5.4 3.5 9S14.2 18.7 12 21c-2.2-2.3-3.5-5.4-3.5-9S9.8 5.3 12 3Z" /></Base>,
  chronicle: (p: IconProps) => <Base {...p}><path d="M6 3h11a2 2 0 0 1 2 2v16H7a3 3 0 0 1-3-3V5a2 2 0 0 1 2-2Z" /><path d="M8 8h7M8 12h6M8 16h4" /></Base>,
  gallery: (p: IconProps) => <Base {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="9" r="1.5" /><path d="m5 18 4.5-4.5 3 3L15 14l4 4" /></Base>,
  settings: (p: IconProps) => <Base {...p}><path d="M4 7h9M17 7h3M4 17h3M11 17h9M13 5v4M9 15v4" /></Base>,
  spark: (p: IconProps) => <Base {...p}><path d="m12 3 1.3 4.2L17.5 9l-4.2 1.3L12 14.5l-1.3-4.2L6.5 9l4.2-1.8L12 3Z" /><path d="m18.5 14 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7.7-2.3Z" /></Base>,
  send: (p: IconProps) => <Base {...p}><path d="m4 4 16 8-16 8 3-8-3-8Z" /><path d="M7 12h13" /></Base>,
  undo: (p: IconProps) => <Base {...p}><path d="M9 8H4V3" /><path d="M4 8c2-3 5-4.5 8.4-4 4.6.5 8.1 4.7 7.6 9.3-.5 4.6-4.7 8.1-9.3 7.6-2.7-.3-5-1.8-6.3-4" /></Base>,
  chat: (p: IconProps) => <Base {...p}><path d="M21 12a8 8 0 0 1-8 8H8l-5 2 1.5-4A8 8 0 1 1 21 12Z" /></Base>,
  shield: (p: IconProps) => <Base {...p}><path d="M12 3 4 7v5c0 4.7 3.1 8 8 9 4.9-1 8-4.3 8-9V7l-8-4Z" /><path d="M9 12h6M12 9v6" /></Base>,
  stop: (p: IconProps) => <Base {...p}><rect x="6" y="6" width="12" height="12" rx="1" /></Base>,
  chevron: (p: IconProps) => <Base {...p}><path d="m9 18 6-6-6-6" /></Base>,
  download: (p: IconProps) => <Base {...p}><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></Base>,
  refresh: (p: IconProps) => <Base {...p}><path d="M20 7v5h-5" /><path d="M4 17v-5h5" /><path d="M6.1 8A7 7 0 0 1 18 6l2 6M18 16a7 7 0 0 1-11.9 2L4 12" /></Base>,
  plus: (p: IconProps) => <Base {...p}><path d="M12 5v14M5 12h14" /></Base>,
  close: (p: IconProps) => <Base {...p}><path d="m6 6 12 12M18 6 6 18" /></Base>,
}
