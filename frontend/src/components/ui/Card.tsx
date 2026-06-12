import type { HTMLAttributes } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Liseré d'accent à gauche */
  accent?: 'primary' | 'secondary' | 'none'
  /** Fond élevé au lieu de surface */
  elevated?: boolean
  padded?: boolean
}

const ACCENTS = {
  primary: 'border-l-2 border-l-accent',
  secondary: 'border-l-2 border-l-accent2',
  none: '',
}

export default function Card({
  accent = 'none',
  elevated = false,
  padded = true,
  className = '',
  children,
  ...props
}: CardProps) {
  return (
    <div
      className={`${elevated ? 'bg-elevated' : 'bg-surface'} border border-line rounded-md shadow-sm ${ACCENTS[accent]} ${padded ? 'p-4' : ''} ${className}`}
      {...props}
    >
      {children}
    </div>
  )
}
