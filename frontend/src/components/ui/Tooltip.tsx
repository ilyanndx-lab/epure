import type { ReactNode } from 'react'

interface TooltipProps {
  content: string
  children: ReactNode
  side?: 'top' | 'bottom'
}

export default function Tooltip({ content, children, side = 'top' }: TooltipProps) {
  return (
    <span className="relative inline-flex group/tt">
      {children}
      <span
        className={`pointer-events-none absolute left-1/2 -translate-x-1/2 z-50 whitespace-nowrap px-2 py-1 rounded-sm bg-elevated border border-line shadow-md text-xs text-secondary opacity-0 group-hover/tt:opacity-100 transition-opacity duration-150 delay-300 ${
          side === 'top' ? 'bottom-full mb-1.5' : 'top-full mt-1.5'
        }`}
      >
        {content}
      </span>
    </span>
  )
}
