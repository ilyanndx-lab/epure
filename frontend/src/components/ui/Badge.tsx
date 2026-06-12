import type { HTMLAttributes } from 'react'

type Variant = 'primary' | 'secondary' | 'success' | 'warning' | 'error' | 'neutral'

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: Variant
  /** Police mono (IDs modèles, stats techniques) */
  mono?: boolean
}

const VARIANTS: Record<Variant, string> = {
  primary:   'bg-accent/15 text-accent border-accent/30',
  secondary: 'bg-accent2/15 text-accent2 border-accent2/30',
  success:   'bg-success/15 text-success border-success/30',
  warning:   'bg-warning/15 text-warning border-warning/30',
  error:     'bg-error/15 text-error border-error/30',
  neutral:   'bg-elevated text-muted border-line',
}

export default function Badge({
  variant = 'neutral',
  mono = false,
  className = '',
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs leading-none ${mono ? 'font-mono' : 'font-medium'} ${VARIANTS[variant]} ${className}`}
      {...props}
    >
      {children}
    </span>
  )
}
